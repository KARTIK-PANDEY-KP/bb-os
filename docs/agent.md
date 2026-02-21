# LLM Agent

An LLM agent that runs inside the container with access to every MCP tool and kernel endpoint.

## Usage

```bash
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What files are in /root?"}'
```

### Request

```json
{
  "message": "your message here",
  "provider": "anthropic",
  "reset": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | string | required | The user's message |
| `provider` | string | auto | `"anthropic"` or `"openai"` (auto-detects from API keys) |
| `reset` | bool | false | Clear conversation history before this message |

### Response

```json
{
  "response": "The agent's text response",
  "provider": "anthropic",
  "tool_count": 47
}
```

### Conversation History

```bash
curl http://localhost:8080/chat/history
```

Returns `{"messages": [...]}` with the full conversation. History is stored at `.agent/chat_history.json` and persists across container restarts (it's on the `/repo` mount).

Send `"reset": true` to clear history and start a new conversation.

## How It Works

On each `/chat` request:

1. The agent connects to all three MCP servers via SSE and discovers their tools
2. It combines MCP tools with kernel tools (exec_python, run_shell, self_evolve)
3. It sends the conversation history + all tool definitions to the LLM
4. If the LLM returns tool calls, the agent executes them and sends results back
5. This repeats until the LLM returns a final text response

## Available Tools

The agent has access to every tool from all three MCP servers plus kernel tools:

### Browser-use tools
Navigate, click, type, screenshot, extract content, get HTML, scroll, manage tabs/sessions.

### Linux tools
System info, processes, services, logs, network interfaces/connections/ports, block devices, files.

### Filesystem tools
Read, write, edit, create directory, list directory, search, move, get file info.

### Kernel tools (direct)
| Tool | Description |
|------|-------------|
| `exec_python` | Execute Python code in persistent namespace |
| `run_shell` | Run shell command with persistent cwd/env |
| `self_evolve` | Rebuild and restart the container |

## Provider Selection

The agent picks a provider in this order:
1. `provider` field in the request
2. `LLM_PROVIDER` environment variable
3. `anthropic` if `ANTHROPIC_API_KEY` is set
4. `openai` if `OPENAI_API_KEY` is set

### Model Override

Set environment variables to change the model:
- `ANTHROPIC_MODEL` (default: `claude-sonnet-4-20250514`)
- `OPENAI_MODEL` (default: `gpt-4o`)

## Architecture

```
POST /chat
    |
    v
criu_wrapper.py (routes request)
    |
    v
agent.py
    |
    +---> MCP servers via SSE (localhost:8765)
    |       - browser-use: 15+ browser tools
    |       - linux: 15+ system tools
    |       - filesystem: 14+ file tools
    |
    +---> Kernel via HTTP (localhost:8080)
    |       - exec_python -> POST /exec
    |       - run_shell -> POST /shell
    |       - self_evolve -> POST /evolve
    |
    +---> LLM API (Anthropic or OpenAI)
            - sends tools + messages
            - receives tool_use blocks
            - executes tools, sends results
            - repeats until final text
```

## Implementation

- `core/agent.py` -- MCP client, tool discovery, agent loop, history persistence
- `core/criu_wrapper.py` -- routes `POST /chat` and `GET /chat/history`
