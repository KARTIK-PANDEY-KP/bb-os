# Architecture

## What This Is

A single Docker container that runs a complete development environment: MCP servers for browser automation / Linux diagnostics / filesystem access, a Python kernel with persistent state, a VNC desktop, and a self-evolve mechanism that lets the container rebuild and replace itself.

## Container Internals

When the container starts (`body/entrypoint.sh`), four things launch in order:

1. **Xvfb + VNC + Openbox desktop** (background) -- virtual display on `:99`, VNC on port 5900, window manager + desktop icons
2. **Kernel** (background) -- `criu_wrapper.py` on port 8080, which internally starts `kernel.py` on port 8081
3. **MCP proxy** (PID 1) -- `mcp-proxy` on port 8765, multiplexing three MCP servers: browser-use, linux, filesystem

The kernel is actually two processes:
- **`criu_wrapper.py`** (port 8080, public-facing) -- receives all HTTP requests, handles CRIU/cryo/evolve endpoints directly, proxies everything else to the inner kernel
- **`kernel.py`** (port 8081, internal) -- the actual Python exec engine with notebook-style persistent namespace

## Ports

| Port | Service | Protocol |
|------|---------|----------|
| 8080 | Kernel (criu_wrapper) | HTTP |
| 8765 | MCP proxy | HTTP/SSE |
| 5900 | VNC server | VNC |

## Volume Mounts

| Host Path | Container Path | Purpose |
|-----------|---------------|---------|
| `$HOST_HOME/projects/docker_persistence` | `/root/host` | Persistent host files |
| `$HOST_HOME/.mcp-state` | `/data` | Kernel checkpoint data |
| `$HOST_PWD` (project root) | `/repo` | Live project source code (read-write) |
| `/var/run/docker.sock` | `/var/run/docker.sock` | Docker daemon access (for evolve) |

`/repo` is a bind mount -- writes inside the container are immediately visible on the host and vice versa. This is how the container reads its own source code and how evolve edits persist across restarts.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `HOST_HOME` | Real host `$HOME` path (for restarter volume mounts) |
| `HOST_PWD` | Real host `$PWD` path (for restarter volume mounts) |
| `OPENAI_API_KEY` | Passed through to MCP servers |
| `ANTHROPIC_API_KEY` | Passed through to MCP servers |
| `BROWSER_USE_HEADLESS` | `false` = visible browser in VNC |
| `FILESYSTEM_ROOT` | Root path for filesystem MCP server |

## MCP Servers

Three MCP servers run behind `mcp-proxy` on port 8765:

- **browser-use** (`/servers/browser-use/sse`) -- Chromium browser automation via `browser-use` CLI
- **linux** (`/servers/linux/sse`) -- System info, processes, services, logs, network via `linux-mcp-server`
- **filesystem** (`/servers/filesystem/sse`) -- File read/write/search within `FILESYSTEM_ROOT` via `@modelcontextprotocol/server-filesystem`

## Kernel Endpoints

All on port 8080.

### Code Execution
- `POST /exec` -- Execute Python in persistent GLOBAL namespace (notebook semantics)
- `POST /reset` -- Clear namespace (keeps shell context and runtime)
- `GET /status` -- Exec count, globals count, busy flag
- `GET /ping` -- Health check

### Shell
- `POST /shell` -- Run shell command `{"command": "ls"}` with persistent cwd/env
- `POST /shell/cd` -- Set working directory `{"path": "/tmp"}`
- `POST /shell/env` -- Set env var `{"key": "K", "value": "V"}`

### State Persistence (Cryo)
- `POST /cryo/store` -- Serialize kernel state to disk via dill
- `POST /cryo/reload` -- Deserialize state back (starts fresh kernel if needed)

### CRIU (optional, rarely works in Docker)
- `POST /criu/checkpoint` -- Freeze kernel process to disk
- `POST /criu/restore` -- Restore frozen process
- `GET /criu/status` -- CRIU availability info

### Evolve
- `POST /evolve` -- Trigger self-evolve (returns immediately, runs in background)
- `GET /evolve/status` -- Check if evolve is in progress, latest run info
