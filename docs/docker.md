# Docker

## Image: `os`

Built from `body/Dockerfile`. Alpine 3.20 base with:

- **Python 3 + pip + uv** -- package management, kernel runtime
- **Node.js + npm** -- filesystem MCP server
- **Chromium + Xvfb + x11vnc + Openbox** -- non-headless browser for browser-use MCP
- **Docker CLI + Docker Compose** -- for self-evolve (talks to host daemon via socket)
- **git, curl, bash, coreutils** -- general tooling

### MCP tools installed

- `mcp-proxy` (via uv tool)
- `browser-use[cli]` (via uvx)
- `linux-mcp-server` (via pip)
- `@modelcontextprotocol/server-filesystem` (via npm)
- `dill` (via pip, for cryo state persistence)

### Build

```bash
docker build -t os -f body/Dockerfile .
```

Build context is the project root. The only file COPYed into the image is `body/entrypoint.sh` -- everything else is read from the `/repo` bind mount at runtime.

## Container: `os-host`

### Start

```bash
./scripts/run.sh
```

Or manually:

```bash
docker run -d --name os-host \
  -p 8765:8765 -p 8080:8080 -p 5900:5900 \
  -v "$HOME/projects/docker_persistence:/root/host:rw" \
  -v "$HOME/.mcp-state:/data:rw" \
  -v "$PWD:/repo:rw" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BROWSER_USE_HEADLESS=false \
  -e FILESYSTEM_ROOT=/root \
  -e HOST_HOME="$HOME" \
  -e HOST_PWD="$PWD" \
  -e OPENAI_API_KEY="..." \
  -e ANTHROPIC_API_KEY="..." \
  os
```

### Entrypoint boot sequence (`body/entrypoint.sh`)

1. Clean up leftover `os-restarter` container and `restart_requested` flag (post-evolve cleanup)
2. Start Xvfb on `:99`, create desktop icons, start Openbox window manager, start PCManFM desktop, start x11vnc on port 5900
3. Start kernel (`cd /repo/core && python criu_wrapper.py`) in background
4. Wait 2 seconds for kernel to be ready
5. `exec mcp-proxy` (becomes PID 1) -- multiplexes browser-use, linux, filesystem MCP servers on port 8765

### docker-compose.yml

Located at `docker/docker-compose.yml`. Defines the same container config as the manual `docker run` above. Requires `HOST_HOME` and `HOST_PWD` environment variables on the host.

```bash
HOST_HOME="$HOME" HOST_PWD="$PWD" docker compose -f docker/docker-compose.yml up -d
```

Note: `scripts/run.sh` uses `docker run` directly (no compose dependency).
