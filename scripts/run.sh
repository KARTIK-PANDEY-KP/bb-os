#!/bin/sh
# One command to spin up everything.
# Usage: ./scripts/run.sh
set -e

cd "$(dirname "$0")/.."

CONTAINER_NAME="os-host"

# Host paths — passed into the container for the ephemeral restarter
export HOST_HOME="$HOME"
export HOST_PWD="$PWD"

# Load .env if present (API keys, config overrides)
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

# Create host dirs
mkdir -p "$HOME/projects/docker_persistence"
mkdir -p "$HOME/.mcp-state"

# Stop and remove existing container if present
if docker ps -qa -f name="$CONTAINER_NAME" 2>/dev/null | grep -q .; then
  echo "[run-host] Stopping existing $CONTAINER_NAME..."
  docker stop "$CONTAINER_NAME" 2>/dev/null || true
  docker rm "$CONTAINER_NAME" 2>/dev/null || true
fi

# Clean up any leftover restarter from a previous evolve
if docker ps -qa -f name=os-restarter 2>/dev/null | grep -q .; then
  echo "[run-host] Removing leftover os-restarter..."
  docker rm -f os-restarter 2>/dev/null || true
fi

# Build image if not present
if ! docker images -q os 2>/dev/null | grep -q .; then
  echo "[run-host] Building os image..."
  docker build -t os -f body/Dockerfile .
fi

# Start container
echo "[run-host] Starting $CONTAINER_NAME..."
docker run -d --name "$CONTAINER_NAME" \
  -p 8765:8765 -p 8080:8080 -p 5900:5900 \
  -v "$HOME/projects/docker_persistence:/root/host:rw" \
  -v "$HOME/.mcp-state:/data:rw" \
  -v "$PWD:/repo:rw" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BROWSER_USE_HEADLESS=false \
  -e FILESYSTEM_ROOT="/root" \
  -e HOST_HOME="$HOME" \
  -e HOST_PWD="$PWD" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  os

# Wait for kernel to be ready
echo "[run-host] Waiting for services..."
for i in $(seq 1 30); do
  if curl -s http://localhost:8080/ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Verify
if curl -s http://localhost:8080/ping >/dev/null 2>&1; then
  echo "[run-host] Kernel ready"
else
  echo "[run-host] Warning: kernel not responding yet (may still be starting)"
fi

echo ""
echo "Everything is up:"
echo "  MCP server: http://localhost:8765"
echo "  Kernel:     http://localhost:8080"
echo "  VNC:        vnc://localhost:5900  (password: os)"
echo "  Host files: $HOME/projects/docker_persistence -> /root/host"
echo "  Repo mount: $PWD -> /repo"
echo ""
echo "Open VNC:  open vnc://localhost:5900"
