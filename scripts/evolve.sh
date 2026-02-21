#!/bin/sh
# Self-evolve: build a new image from /repo, then hand off restart to an
# ephemeral "restarter" container.  This script runs INSIDE the os container.
#
# Flow:
#   1. Build new os image from /repo
#   2. Spin up a tiny restarter container (docker:cli) that waits for a flag
#   3. Touch the flag — restarter sees it, stops old container, starts new one
#   4. This container dies; the new one boots and cleans up the restarter
set -e

REPO=/repo
FLAG_DIR="$REPO/.memory"
FLAG_FILE="$FLAG_DIR/restart_requested"
RESTARTER_NAME="os-restarter"
CONTAINER_NAME="os-host"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
RUN_DIR="$FLAG_DIR/runs/$TIMESTAMP"

mkdir -p "$RUN_DIR"

echo "[evolve] === Starting evolve at $TIMESTAMP ==="

# --- 1. Validate environment ---
if [ -z "$HOST_HOME" ] || [ -z "$HOST_PWD" ]; then
  echo "[evolve] ERROR: HOST_HOME and HOST_PWD must be set." >&2
  exit 1
fi

if [ ! -S /var/run/docker.sock ]; then
  echo "[evolve] ERROR: Docker socket not mounted." >&2
  exit 1
fi

# --- 2. Run tests (abort on failure) ---
echo "[evolve] Running tests..."
if [ -f "$REPO/tests/test_mcp.py" ] && python -c "import pytest" 2>/dev/null; then
  cd "$REPO"
  if python -m pytest tests/ -x --tb=short > "$RUN_DIR/test_output.txt" 2>&1; then
    echo "[evolve] Tests passed."
  else
    echo "[evolve] ERROR: Tests failed. Aborting evolve." >&2
    cat "$RUN_DIR/test_output.txt" >&2
    echo "FAILED" > "$RUN_DIR/status"
    exit 1
  fi
else
  echo "[evolve] pytest not available or no tests found, skipping."
fi

# --- 3. Build new image ---
echo "[evolve] Building new os image..."
docker build -t os -f "$REPO/body/Dockerfile" "$REPO" \
  > "$RUN_DIR/build_output.txt" 2>&1
echo "[evolve] Image built successfully."

# --- 4. Clean up any leftover restarter ---
if docker ps -qa -f name="$RESTARTER_NAME" 2>/dev/null | grep -q .; then
  echo "[evolve] Removing leftover restarter..."
  docker rm -f "$RESTARTER_NAME" 2>/dev/null || true
fi

# --- 5. Remove stale flag ---
rm -f "$FLAG_FILE"

# --- 6. Spin up ephemeral restarter ---
#   The restarter polls for the flag file, then stops the old container and
#   starts a fresh one with docker run.  Uses the same ports, volumes, and
#   env vars.  HOST_HOME and HOST_PWD are host-side paths for bind mounts.
echo "[evolve] Launching ephemeral restarter..."
docker run -d --name "$RESTARTER_NAME" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${HOST_PWD}:/repo" \
  -e HOST_HOME="${HOST_HOME}" \
  -e HOST_PWD="${HOST_PWD}" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
  docker:cli sh -c '
    echo "[restarter] Waiting for restart flag..."
    while [ ! -f /repo/.memory/restart_requested ]; do sleep 1; done
    echo "[restarter] Flag detected. Restarting os..."

    docker stop os-host 2>/dev/null || true
    docker rm os-host 2>/dev/null || true

    docker run -d --name os-host \
      -p 8765:8765 -p 8080:8080 -p 5900:5900 \
      -v "${HOST_HOME}/projects/docker_persistence:/root/host:rw" \
      -v "${HOST_HOME}/.mcp-state:/data:rw" \
      -v "${HOST_PWD}:/repo:rw" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e BROWSER_USE_HEADLESS=false \
      -e FILESYSTEM_ROOT=/root \
      -e HOST_HOME="${HOST_HOME}" \
      -e HOST_PWD="${HOST_PWD}" \
      -e OPENAI_API_KEY="${OPENAI_API_KEY}" \
      -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
      os

    echo "[restarter] Done. os restarted."
  '

# --- 7. Record the run ---
echo "$TIMESTAMP" > "$RUN_DIR/status"
echo "SUCCESS - pending restart" >> "$RUN_DIR/status"

# --- 8. Touch the flag — this triggers the restarter ---
echo "[evolve] Setting restart flag. This container will be replaced shortly."
touch "$FLAG_FILE"

echo "[evolve] Evolve complete. Restarter will replace this container momentarily."
