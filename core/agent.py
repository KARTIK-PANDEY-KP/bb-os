"""
LLM Agent with full MCP tool access.

Connects to all MCP servers via SSE, discovers tools, and runs an agent loop
that lets Claude or GPT-4 call any MCP tool plus kernel endpoints.

Usage (from criu_wrapper.py):
    from agent import handle_chat
    result = asyncio.run(handle_chat(messages, provider="anthropic"))
"""

import asyncio
import json
import math
import os
import random
import sys
import traceback
from typing import Any

import httpx
from mcp.client.sse import sse_client
from mcp import ClientSession

MCP_BASE = "http://localhost:8765"
KERNEL_BASE = "http://localhost:8080"

MCP_SERVERS = ["browser-use", "linux", "filesystem", "context7"]

# Remote MCP servers: (name, url, optional_headers). 1mcpserver = MCP-of-MCPs (discover/configure other MCP servers).
REMOTE_MCP_SERVERS: list[tuple[str, str, dict[str, str]]] = [
    (
        "1mcpserver",
        "https://mcp.1mcpserver.com/mcp/",
        {"Accept": "text/event-stream", "Cache-Control": "no-cache"},
    ),
]
# Populated by _discover_mcp_tools for use in _call_mcp_tool.
_remote_server_config: dict[str, dict[str, Any]] = {}

HISTORY_PATH = "/repo/.memory/chat_history.json"
LEARNINGS_PATH = "/repo/.memory/learnings.md"
TOOL_LOG_PATH = "/repo/.memory/tool_log.jsonl"
DIGEST_STATE_PATH = "/repo/.memory/digest_state.json"

PROMPTS_DIR = "/repo/brain"

DIGEST_SYSTEM_PROMPT = """\
You are the sleeping mind of an autonomous agent. While the agent rests between \
cycles of activity, you process its recent experiences — like a brain during sleep \
consolidating memories.

You will receive:
1. Recent conversation history (what the agent said and was told)
2. Recent tool calls and their results (what the agent did)
3. The agent's current accumulated learnings (what it already knows)
4. The agent's knowledge base / brain files (its identity and instructions)

Your job is to digest all of this and produce an UPDATED learnings file. This file \
is the agent's long-term memory — condensed wisdom that persists across interactions.

Extract and organize:
- **Mistakes & Lessons**: Things that went wrong. What to avoid. What to do differently.
- **Successful Patterns**: Approaches that worked well. Repeat these.
- **Insights & Conclusions**: New understanding about the environment, tools, or tasks.
- **Open Questions**: Unresolved things worth investigating later.
- **Self-Knowledge**: Observations about own capabilities, limitations, or tendencies.

Rules:
- Be concise. Each learning should be 1-2 sentences max.
- Merge duplicates. If an old learning and a new one say the same thing, keep the better version.
- Drop stale entries. If something is no longer relevant, remove it.
- Preserve important old learnings even if there's nothing new about them.
- Output ONLY the updated learnings file content in markdown. No preamble, no explanation.
- Use the section headers exactly as listed above.
- If there's nothing meaningful to extract, return the existing learnings unchanged.
"""


def _load_system_prompt() -> str:
    """Load and concatenate all prompt files from /repo/brain/.
    Files are read in sorted order so naming controls priority (e.g. 00-core.md, 10-extra.md).
    Falls back to a minimal default if the directory is missing."""
    try:
        if os.path.isdir(PROMPTS_DIR):
            parts = []
            for fname in sorted(os.listdir(PROMPTS_DIR)):
                fpath = os.path.join(PROMPTS_DIR, fname)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        parts.append(f.read().strip())
            if parts:
                return "\n\n".join(parts)
    except Exception as e:
        print(f"[agent] Warning: could not read prompts from {PROMPTS_DIR}: {e}", file=sys.stderr)
    return "You are an AI agent running inside a Docker container. Use the tools to accomplish tasks."

TOOLS_PATH = os.path.join(PROMPTS_DIR, "tools.json")


def _load_kernel_tools() -> list[dict]:
    """Load kernel tool definitions from /repo/brain/tools.json.
    Read fresh on every call so the agent can modify its own tools."""
    try:
        if os.path.isfile(TOOLS_PATH):
            with open(TOOLS_PATH) as f:
                tools = json.load(f)
            if isinstance(tools, list):
                return tools
    except Exception as e:
        print(f"[agent] Warning: could not read tools from {TOOLS_PATH}: {e}", file=sys.stderr)
    return []


async def _discover_mcp_tools() -> tuple[list[dict], dict[str, str]]:
    """Connect to all MCP servers (local proxy + remote) and discover their tools.
    Returns (tools_list, tool_to_server_map)."""
    global _remote_server_config
    all_tools = []
    tool_server_map = {}
    _remote_server_config = {}

    async def _discover_one_local(server_name: str):
        url = f"{MCP_BASE}/servers/{server_name}/sse"
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = []
                for tool in result.tools:
                    raw_schema = tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}}
                    schema = {
                        "name": f"{server_name}__{tool.name}",
                        "description": f"[{server_name}] {tool.description or tool.name}",
                        "input_schema": _sanitize_schema(raw_schema),
                    }
                    tools.append(schema)
                return tools

    async def _discover_one_remote(server_name: str, url: str, headers: dict):
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools = []
                for tool in result.tools:
                    raw_schema = tool.inputSchema if tool.inputSchema else {"type": "object", "properties": {}}
                    schema = {
                        "name": f"{server_name}__{tool.name}",
                        "description": f"[{server_name}] {tool.description or tool.name}",
                        "input_schema": _sanitize_schema(raw_schema),
                    }
                    tools.append(schema)
                return tools

    for server_name in MCP_SERVERS:
        try:
            tools = await asyncio.wait_for(_discover_one_local(server_name), timeout=15)
            for schema in tools:
                all_tools.append(schema)
                tool_server_map[schema["name"]] = server_name
        except asyncio.TimeoutError:
            print(f"[agent] Warning: timeout connecting to {server_name}", file=sys.stderr)
        except Exception as e:
            print(f"[agent] Warning: could not connect to {server_name}: {e}", file=sys.stderr)

    for server_name, url, headers in REMOTE_MCP_SERVERS:
        try:
            _remote_server_config[server_name] = {"url": url, "headers": headers}
            tools = await asyncio.wait_for(
                _discover_one_remote(server_name, url, headers), timeout=20
            )
            for schema in tools:
                all_tools.append(schema)
                tool_server_map[schema["name"]] = server_name
        except asyncio.TimeoutError:
            print(f"[agent] Warning: timeout connecting to remote {server_name}", file=sys.stderr)
        except Exception as e:
            print(f"[agent] Warning: could not connect to remote {server_name}: {e}", file=sys.stderr)

    return all_tools, tool_server_map


async def _call_mcp_tool(server_name: str, tool_name: str, arguments: dict) -> str:
    """Call a tool on a specific MCP server (local proxy or remote)."""
    async def _call():
        if server_name in _remote_server_config:
            cfg = _remote_server_config[server_name]
            url, headers = cfg["url"], cfg.get("headers") or {}
            async with sse_client(url, headers=headers) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    parts = []
                    for content in result.content:
                        if hasattr(content, "text"):
                            parts.append(content.text)
                        else:
                            parts.append(str(content))
                    return "\n".join(parts) if parts else "(no output)"
        url = f"{MCP_BASE}/servers/{server_name}/sse"
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                parts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        parts.append(content.text)
                    else:
                        parts.append(str(content))
                return "\n".join(parts) if parts else "(no output)"
    return await asyncio.wait_for(_call(), timeout=90)


async def _call_kernel_tool(name: str, arguments: dict) -> str:
    """Call a kernel endpoint (exec, shell, evolve)."""
    async with httpx.AsyncClient(timeout=300) as client:
        if name == "exec_python":
            resp = await client.post(f"{KERNEL_BASE}/exec", json={"code": arguments.get("code", "")})
            data = resp.json()
            parts = []
            if data.get("stdout"):
                parts.append(data["stdout"])
            if data.get("stderr"):
                parts.append(f"STDERR: {data['stderr']}")
            if data.get("error"):
                err = data["error"]
                parts.append(f"ERROR ({err.get('type', 'unknown')}): {err.get('message', '')}")
            return "\n".join(parts) if parts else "(no output)"

        elif name == "run_shell":
            resp = await client.post(f"{KERNEL_BASE}/shell", json={"command": arguments.get("command", "")})
            data = resp.json()
            parts = []
            if data.get("stdout"):
                parts.append(data["stdout"])
            if data.get("stderr"):
                parts.append(f"STDERR: {data['stderr']}")
            if data.get("returncode") and data["returncode"] != 0:
                parts.append(f"(exit code: {data['returncode']})")
            return "\n".join(parts) if parts else "(no output)"

        elif name == "self_evolve":
            resp = await client.post(f"{KERNEL_BASE}/evolve")
            return resp.json().get("message", "Evolve triggered.")

        return f"Unknown kernel tool: {name}"


def _sanitize_schema(schema: Any, _top: bool = True) -> Any:
    """Recursively remove oneOf/allOf/anyOf from schemas (Anthropic rejects them)."""
    if not isinstance(schema, dict):
        return schema
    result = {}
    for k, v in schema.items():
        if k in ("oneOf", "allOf", "anyOf"):
            continue
        elif isinstance(v, dict):
            result[k] = _sanitize_schema(v, _top=False)
        elif isinstance(v, list):
            result[k] = [_sanitize_schema(item, _top=False) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    if _top:
        if "type" not in result:
            result["type"] = "object"
        if "properties" not in result:
            result["properties"] = {}
    return result


def _tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Convert tool schemas to Anthropic format."""
    return [{"name": t["name"], "description": t["description"], "input_schema": _sanitize_schema(t["input_schema"])} for t in tools]


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert tool schemas to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


async def _agent_loop_anthropic(messages: list[dict], tools: list[dict], tool_server_map: dict[str, str]) -> str:
    """Run the agent loop using Anthropic Claude."""
    import anthropic

    client = anthropic.Anthropic()
    anthropic_tools = _tools_to_anthropic(tools)

    api_messages = [m for m in messages if m.get("role") != "system"]

    while True:
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=8192,
            system=_load_system_prompt(),
            messages=api_messages,
            tools=anthropic_tools,
        )

        for block in response.content:
            if block.type == "text" and block.text.strip():
                _log_thinking(block.text)

        if response.stop_reason == "end_turn" or response.stop_reason != "tool_use":
            text_parts = [b.text for b in response.content if b.type == "text"]
            return "\n".join(text_parts) if text_parts else "(no response)"

        api_messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            arguments = block.input

            try:
                if tool_name in ("exec_python", "run_shell", "self_evolve"):
                    result_text = await _call_kernel_tool(tool_name, arguments)
                elif tool_name in tool_server_map:
                    server = tool_server_map[tool_name]
                    real_name = tool_name.split("__", 1)[1]
                    result_text = await _call_mcp_tool(server, real_name, arguments)
                else:
                    result_text = f"Unknown tool: {tool_name}"
            except Exception as e:
                result_text = f"Tool error: {e}\n{traceback.format_exc()}"

            _log_tool_call(tool_name, arguments, result_text)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        api_messages.append({"role": "user", "content": tool_results})


async def _agent_loop_openai(messages: list[dict], tools: list[dict], tool_server_map: dict[str, str]) -> str:
    """Run the agent loop using OpenAI GPT-4."""
    import openai

    client = openai.OpenAI()
    openai_tools = _tools_to_openai(tools)

    api_messages = [{"role": "system", "content": _load_system_prompt()}]
    api_messages.extend([m for m in messages if m.get("role") != "system"])

    while True:
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            messages=api_messages,
            tools=openai_tools if openai_tools else None,
            max_tokens=8192,
        )

        choice = response.choices[0]

        if choice.message.content and choice.message.content.strip():
            _log_thinking(choice.message.content)

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            return choice.message.content or "(no response)"

        api_messages.append(choice.message)

        for tc in choice.message.tool_calls:
            tool_name = tc.function.name
            try:
                arguments = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                arguments = {}

            try:
                if tool_name in ("exec_python", "run_shell", "self_evolve"):
                    result_text = await _call_kernel_tool(tool_name, arguments)
                elif tool_name in tool_server_map:
                    server = tool_server_map[tool_name]
                    real_name = tool_name.split("__", 1)[1]
                    result_text = await _call_mcp_tool(server, real_name, arguments)
                else:
                    result_text = f"Unknown tool: {tool_name}"
            except Exception as e:
                result_text = f"Tool error: {e}\n{traceback.format_exc()}"

            _log_tool_call(tool_name, arguments, result_text)

            api_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text,
            })


def _log_entry(entry: dict) -> None:
    """Append an entry to the tool log. JSONL format, one per line."""
    try:
        os.makedirs(os.path.dirname(TOOL_LOG_PATH), exist_ok=True)
        with open(TOOL_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _log_thinking(text: str) -> None:
    """Log the agent's reasoning/thinking text."""
    import time as _time
    _log_entry({
        "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "thinking",
        "text": text[:4000],
    })


def _log_tool_call(tool_name: str, arguments: Any, result: str) -> None:
    """Log a tool call with its arguments and result."""
    import time as _time
    _log_entry({
        "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "tool",
        "tool": tool_name,
        "args": arguments,
        "result": result[:2000],
    })


def _resolve_provider(provider: str | None) -> str:
    """Determine which LLM provider to use."""
    if provider:
        return provider
    env_provider = os.environ.get("LLM_PROVIDER", "").lower()
    if env_provider in ("anthropic", "openai"):
        return env_provider
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"


def _load_history() -> list[dict]:
    """Load conversation history from disk."""
    try:
        if os.path.isfile(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_history(messages: list[dict]) -> None:
    """Save conversation history to disk."""
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        with open(HISTORY_PATH, "w") as f:
            json.dump(messages, f, indent=2, default=str)
    except Exception as e:
        print(f"[agent] Warning: could not save history: {e}", file=sys.stderr)


async def handle_chat(user_message: str, provider: str | None = None, reset: bool = False) -> dict:
    """Main entry point. Called from criu_wrapper.py on POST /chat.

    Args:
        user_message: The user's message text
        provider: "anthropic" or "openai" (auto-detected if None)
        reset: If True, clear conversation history before this message

    Returns:
        {"response": str, "provider": str, "tool_count": int}
    """
    provider = _resolve_provider(provider)

    history = [] if reset else _load_history()
    history.append({"role": "user", "content": user_message})

    mcp_tools, tool_server_map = await _discover_mcp_tools()
    kernel_tools = _load_kernel_tools()
    all_tools = kernel_tools + mcp_tools

    print(f"[agent] {len(all_tools)} tools available ({len(mcp_tools)} MCP + {len(kernel_tools)} kernel), provider={provider}", file=sys.stderr)

    try:
        if provider == "anthropic":
            response_text = await _agent_loop_anthropic(history, all_tools, tool_server_map)
        elif provider == "openai":
            response_text = await _agent_loop_openai(history, all_tools, tool_server_map)
        else:
            return {"response": f"Unknown provider: {provider}", "provider": provider, "tool_count": 0}
    except Exception as e:
        return {"response": f"Agent error: {e}\n{traceback.format_exc()}", "provider": provider, "tool_count": len(all_tools)}

    history.append({"role": "assistant", "content": response_text})
    _save_history(history)

    return {
        "response": response_text,
        "provider": provider,
        "tool_count": len(all_tools),
    }


# ---------------------------------------------------------------------------
# Sleep / Digest — chunked, cursor-tracked
# ---------------------------------------------------------------------------

HISTORY_CHUNK_SIZE = 10
TOOL_LOG_CHUNK_SIZE = 20
DEFAULT_REPLAY_RATIO = 0.15


def _load_digest_state() -> dict:
    """Load digest cursors — tracks how far we've digested."""
    try:
        if os.path.isfile(DIGEST_STATE_PATH):
            with open(DIGEST_STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {"history_cursor": 0, "tool_log_cursor": 0}


def _save_digest_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(DIGEST_STATE_PATH), exist_ok=True)
        with open(DIGEST_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[agent] Warning: could not save digest state: {e}", file=sys.stderr)


def _load_all_history() -> list[dict]:
    """Load full chat history as a list of message dicts."""
    try:
        if os.path.isfile(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _load_all_tool_log() -> list[dict]:
    """Load full tool log as a list of entry dicts."""
    entries = []
    try:
        if os.path.isfile(TOOL_LOG_PATH):
            with open(TOOL_LOG_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    except Exception:
        pass
    return entries


def _format_history_chunk(msgs: list[dict]) -> str:
    parts = []
    for m in msgs:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            content = json.dumps(content, default=str)[:2000]
        parts.append(f"[{role}] {str(content)[:3000]}")
    return "\n".join(parts)


def _format_tool_log_chunk(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        if entry.get("type") == "tool":
            parts.append(
                f"[{entry.get('ts', '?')}] {entry.get('tool', '?')}"
                f"({json.dumps(entry.get('args', {}), default=str)[:500]})"
                f" → {str(entry.get('result', ''))[:800]}"
            )
        elif entry.get("type") == "thinking":
            parts.append(
                f"[{entry.get('ts', '?')}] THINKING: {str(entry.get('text', ''))[:800]}"
            )
        elif entry.get("type") == "digest":
            parts.append(
                f"[{entry.get('ts', '?')}] DIGEST: updated learnings ({entry.get('learnings_length', '?')} chars)"
            )
    return "\n".join(parts)


def _read_existing_learnings() -> str:
    try:
        if os.path.isfile(LEARNINGS_PATH):
            with open(LEARNINGS_PATH) as f:
                return f.read().strip()
    except Exception:
        pass
    return "(no learnings yet)"


def _read_brain_files() -> str:
    parts = []
    try:
        if os.path.isdir(PROMPTS_DIR):
            for fname in sorted(os.listdir(PROMPTS_DIR)):
                fpath = os.path.join(PROMPTS_DIR, fname)
                if os.path.isfile(fpath) and not fname.endswith(".json"):
                    with open(fpath) as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"--- {fname} ---\n{content}")
    except Exception:
        pass
    return "\n\n".join(parts) if parts else "(no brain files)"


def _save_learnings(content: str) -> None:
    try:
        os.makedirs(os.path.dirname(LEARNINGS_PATH), exist_ok=True)
        with open(LEARNINGS_PATH, "w") as f:
            f.write(content)
    except Exception as e:
        print(f"[agent] Warning: could not save learnings: {e}", file=sys.stderr)


async def _digest_one_chunk(provider: str, chunk_text: str, learnings: str, brain_text: str) -> str:
    """Send one chunk + current learnings to the LLM, return updated learnings."""
    digest_input = (
        f"## Experience Chunk\n\n{chunk_text}\n\n"
        f"## Current Accumulated Learnings\n\n{learnings}\n\n"
        f"## Agent Identity (brain/)\n\n{brain_text}"
    )

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=4096,
            system=DIGEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": digest_input}],
        )
        text_parts = [b.text for b in response.content if b.type == "text"]
        return "\n".join(text_parts) if text_parts else ""
    elif provider == "openai":
        import openai
        client = openai.OpenAI()
        response = client.chat.completions.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            messages=[
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": digest_input},
            ],
            max_tokens=4096,
        )
        return response.choices[0].message.content or ""
    return ""


def _build_replay_chunks(
    old_history: list[dict],
    old_tool_log: list[dict],
    new_chunk_count: int,
    replay_ratio: float = DEFAULT_REPLAY_RATIO,
) -> list[tuple[str, str]]:
    """Randomly sample old memories for replay.

    Picks a fraction (replay_ratio) of the new-chunk effort from
    already-digested data. Like a brain during sleep replaying random
    older memories to find new connections and reinforce learnings.
    """
    replay_count = max(1, math.ceil(new_chunk_count * replay_ratio))
    replays: list[tuple[str, str]] = []

    pool: list[tuple[str, list]] = []
    if old_history:
        for i in range(0, len(old_history), HISTORY_CHUNK_SIZE):
            batch = old_history[i : i + HISTORY_CHUNK_SIZE]
            pool.append(("replay_conversation", batch))
    if old_tool_log:
        for i in range(0, len(old_tool_log), TOOL_LOG_CHUNK_SIZE):
            batch = old_tool_log[i : i + TOOL_LOG_CHUNK_SIZE]
            pool.append(("replay_tool_activity", batch))

    if not pool:
        return []

    selected = random.sample(pool, min(replay_count, len(pool)))
    for chunk_type, batch in selected:
        if "conversation" in chunk_type:
            replays.append((chunk_type, _format_history_chunk(batch)))
        else:
            replays.append((chunk_type, _format_tool_log_chunk(batch)))

    return replays


async def handle_digest(
    provider: str | None = None,
    replay_ratio: float | None = None,
) -> dict:
    """Sleep digest — process ALL new experiences in chunks, then replay
    random old memories, before waking.

    Two phases:
      1. New data: Process all undigested history and tool log entries
         in chunks. Cursors in .memory/digest_state.json track progress.
      2. Replay: Randomly sample old already-digested memories. The
         replay_ratio controls what fraction of effort goes to replay
         (sampled by the daemon based on maturity, or defaults to
         DEFAULT_REPLAY_RATIO for manual calls).

    The agent does not wake up until both phases are complete.

    Returns:
        {"status": str, "chunks_processed": int, "replays": int, "provider": str}
    """
    if replay_ratio is None:
        replay_ratio = DEFAULT_REPLAY_RATIO
    provider = _resolve_provider(provider)
    state = _load_digest_state()
    brain_text = _read_brain_files()
    learnings = _read_existing_learnings()

    all_history = _load_all_history()
    all_tool_log = _load_all_tool_log()

    h_cursor = min(state.get("history_cursor", 0), len(all_history))
    t_cursor = min(state.get("tool_log_cursor", 0), len(all_tool_log))

    new_history = all_history[h_cursor:]
    new_tool_log = all_tool_log[t_cursor:]
    old_history = all_history[:h_cursor]
    old_tool_log = all_tool_log[:t_cursor]

    # --- Phase 1: New data chunks ---
    new_chunks: list[tuple[str, str]] = []

    for i in range(0, len(new_history), HISTORY_CHUNK_SIZE):
        batch = new_history[i : i + HISTORY_CHUNK_SIZE]
        new_chunks.append(("conversation", _format_history_chunk(batch)))

    for i in range(0, len(new_tool_log), TOOL_LOG_CHUNK_SIZE):
        batch = new_tool_log[i : i + TOOL_LOG_CHUNK_SIZE]
        new_chunks.append(("tool_activity", _format_tool_log_chunk(batch)))

    # --- Phase 2: Random replay of old memories ---
    replay_chunks = _build_replay_chunks(
        old_history, old_tool_log, max(len(new_chunks), 1),
        replay_ratio=replay_ratio,
    )

    all_chunks = new_chunks + replay_chunks
    if not all_chunks:
        print("[agent] Sleep digest: nothing to process.", file=sys.stderr)
        return {"status": "nothing_new", "chunks_processed": 0, "replays": 0, "provider": provider}

    total = len(all_chunks)
    print(
        f"[agent] Sleep digest: {len(new_chunks)} new + {len(replay_chunks)} replay = {total} chunks (provider={provider}).",
        file=sys.stderr,
    )

    processed = 0
    for idx, (chunk_type, chunk_text) in enumerate(all_chunks, 1):
        print(f"[agent] Digesting chunk {idx}/{total} ({chunk_type})...", file=sys.stderr)
        try:
            result = await _digest_one_chunk(provider, chunk_text, learnings, brain_text)
            result = result.strip()
            if result:
                learnings = result
                _save_learnings(learnings)
            processed += 1
        except Exception as e:
            print(f"[agent] Chunk {idx}/{total} error: {e}", file=sys.stderr)

    _save_digest_state({
        "history_cursor": len(all_history),
        "tool_log_cursor": len(all_tool_log),
    })

    _log_entry({
        "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "type": "digest",
        "chunks_new": len(new_chunks),
        "chunks_replay": len(replay_chunks),
        "chunks_processed": processed,
        "learnings_length": len(learnings),
    })

    print(
        f"[agent] Sleep digest complete. {processed}/{total} chunks "
        f"({len(new_chunks)} new + {len(replay_chunks)} replay). "
        f"Learnings: {len(learnings)} chars.",
        file=sys.stderr,
    )
    return {
        "status": "ok",
        "chunks_processed": processed,
        "replays": len(replay_chunks),
        "provider": provider,
    }
