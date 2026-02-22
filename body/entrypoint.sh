#!/bin/sh
set -e

MCP_PORT="${MCP_PORT:-8765}"
KERNEL_PORT="${KERNEL_PORT:-8080}"
FILESYSTEM_ROOT="${FILESYSTEM_ROOT:-/}"

# --- Load .env from /repo if present (API keys survive evolve restarts) ---
if [ -f /repo/.env ]; then
  set -a
  . /repo/.env
  set +a
  echo "[entrypoint] Loaded environment from /repo/.env"
fi

# --- Post-evolve cleanup: remove ephemeral restarter + flag ---
if [ -S /var/run/docker.sock ]; then
  if docker ps -qa -f name=os-restarter 2>/dev/null | grep -q .; then
    echo "[entrypoint] Cleaning up ephemeral restarter from previous evolve..."
    docker rm -f os-restarter 2>/dev/null || true
  fi
fi
rm -f /repo/.memory/restart_requested 2>/dev/null || true

# Browser flags
export BROWSER_USE_HEADLESS="${BROWSER_USE_HEADLESS:-false}"
export BROWSER_USE_CHROME_ARGS="${BROWSER_USE_CHROME_ARGS:---no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled}"
export BROWSER_USE_LOGGING_LEVEL="${BROWSER_USE_LOGGING_LEVEL:-INFO}"

# Use host DISPLAY if USE_HOST_DISPLAY=true (skip Xvfb + VNC)
if [ "$USE_HOST_DISPLAY" = "true" ] && [ -n "$DISPLAY" ]; then
  echo "[entrypoint] Using host display: DISPLAY=$DISPLAY"
else
  export DISPLAY=:99
  Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
  sleep 1

  # Create Desktop icons if not present (persisted on host via mount)
  mkdir -p /root/Desktop /root/.config/openbox
  if [ ! -f /root/Desktop/Browser.desktop ]; then
    printf '[Desktop Entry]\nType=Application\nName=Browser\nExec=chromium --no-sandbox --disable-dev-shm-usage --disable-gpu\nIcon=chromium\nTerminal=false\n' > /root/Desktop/Browser.desktop
    chmod +x /root/Desktop/Browser.desktop
  fi
  if [ ! -f /root/Desktop/Terminal.desktop ]; then
    printf '[Desktop Entry]\nType=Application\nName=Terminal\nExec=xterm -fa "Noto Sans Mono" -fs 12 -bg black -fg white -geometry 120x35\nIcon=utilities-terminal\nTerminal=false\n' > /root/Desktop/Terminal.desktop
    chmod +x /root/Desktop/Terminal.desktop
  fi
  cp -n /etc/xdg/openbox/menu.xml /root/.config/openbox/menu.xml 2>/dev/null || true

  # Window manager (title bars, move/resize)
  openbox &
  sleep 0.5

  # Desktop icons (Browser + Terminal)
  pcmanfm --desktop &

  # Start VNC server on :5900 so macOS Screen Sharing can connect
  VNC_PORT="${VNC_PORT:-5900}"
  if command -v x11vnc >/dev/null 2>&1; then
    VNC_PASSWORD="${VNC_PASSWORD:-os}"
    x11vnc -display :99 -rfbport "$VNC_PORT" -passwd "$VNC_PASSWORD" -forever -shared -bg -q 2>/dev/null || true
    echo "[entrypoint] VNC server on port $VNC_PORT (password: $VNC_PASSWORD)"
  fi

  echo "[entrypoint] Desktop ready — Browser + Terminal icons on VNC"
fi

# Start inner kernel (HTTP API for Python exec + CRIU checkpoint/restore) in background
echo "[entrypoint] Starting kernel on port $KERNEL_PORT..."
cd /repo/core && python criu_wrapper.py &
KERNEL_PID=$!
sleep 2

# Start autonomous agent daemon in background (optional: set DAEMON_ENABLED=false to turn off)
DAEMON_ENABLED="${DAEMON_ENABLED:-true}"
case "$DAEMON_ENABLED" in
  0|false|no|off) DAEMON_ENABLED="";;
esac
if [ -n "$DAEMON_ENABLED" ]; then
  echo "[entrypoint] Starting autonomous agent daemon..."
  python /repo/core/daemon.py &
else
  echo "[entrypoint] Daemon disabled (set DAEMON_ENABLED=true to enable automatic run)"
fi

echo "[entrypoint] MCP Server on port $MCP_PORT | Kernel on port $KERNEL_PORT"
echo "  DISPLAY=$DISPLAY | HEADLESS=$BROWSER_USE_HEADLESS"

exec mcp-proxy --host 0.0.0.0 --port "$MCP_PORT" --pass-environment \
  --named-server browser-use 'uvx --from browser-use[cli] browser-use --mcp' \
  --named-server linux 'linux-mcp-server' \
  --named-server filesystem "npx -y @modelcontextprotocol/server-filesystem $FILESYSTEM_ROOT" \
  --named-server context7 'npx -y @upstash/context7-mcp'
