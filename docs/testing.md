# Testing

## Test Suite

`tests/test_mcp.py` -- a single script that tests all services against a running container.

### Run

```bash
uv run --with mcp python tests/test_mcp.py
```

Against a remote host:

```bash
uv run --with mcp python tests/test_mcp.py http://remote-host:8765
```

### What It Tests

| Test | What |
|------|------|
| filesystem | Create, read, append, read via filesystem MCP server |
| linux | `get_system_information` via linux MCP server |
| browser-use | Navigate to Hacker News, extract content via browser-use MCP |
| kernel | `/exec`, `/reset`, `/status`, `/ping`, context persistence |
| shell | `/shell`, script create/run, `/shell/cd`, `/shell/env`, `runtime.shell.run` |
| shell-cryo | Shell context + scripts persist across cryo store/reload |
| cryo | Dill state store/reload (`/cryo/store`, `/cryo/reload`) |
| cryo-resources | `runtime.resource()` handles persist and reconnect after cryo |
| CRIU | Checkpoint/restore (skipped if CRIU unavailable) |

### Screenshot Tool

```bash
uv run --with mcp python tests/test_mcp.py --screenshot
uv run --with mcp python tests/test_mcp.py --screenshot https://example.com output.png
```

### Save Results

```bash
uv run --with mcp python tests/test_mcp.py 2>&1 | tee tests/results/test_results.txt
```
