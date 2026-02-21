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
import os
import sys
import traceback
from typing import Any

import httpx
from mcp.client.sse import sse_client
from mcp import ClientSession

MCP_BASE = "http://localhost:8765"
KERNEL_BASE = "http://localhost:8080"

MCP_SERVERS = ["browser-use", "linux", "filesystem"]

HISTORY_PATH = "/repo/.memory/chat_history.json"

PROMPTS_DIR = "/repo/brain"


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
    """Connect to all MCP servers and discover their tools.
    Returns (tools_list, tool_to_server_map)."""
    all_tools = []
    tool_server_map = {}

    async def _discover_one(server_name: str):
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

    for server_name in MCP_SERVERS:
        try:
            tools = await asyncio.wait_for(_discover_one(server_name), timeout=15)
            for schema in tools:
                all_tools.append(schema)
                tool_server_map[schema["name"]] = server_name
        except asyncio.TimeoutError:
            print(f"[agent] Warning: timeout connecting to {server_name}", file=sys.stderr)
        except Exception as e:
            print(f"[agent] Warning: could not connect to {server_name}: {e}", file=sys.stderr)

    return all_tools, tool_server_map


async def _call_mcp_tool(server_name: str, tool_name: str, arguments: dict) -> str:
    """Call a tool on a specific MCP server."""
    async def _call():
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
    return await asyncio.wait_for(_call(), timeout=60)


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

            api_messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_text,
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
