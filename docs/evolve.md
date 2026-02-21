# Self-Evolve

The container can rebuild and replace itself without any host-side orchestration. Everything happens through the Docker socket.

## How It Works

### Trigger

```
POST http://localhost:8080/evolve
```

The kernel's HTTP server receives the request, sends back `{"status": "started"}`, then runs `scripts/evolve.sh` in a background thread.

### The Evolve Pipeline (`scripts/evolve.sh`)

1. **Validate** -- checks `HOST_HOME`, `HOST_PWD`, and Docker socket are available
2. **Test** -- runs `pytest` if available (skips if not installed)
3. **Build** -- `docker build -t os -f /repo/body/Dockerfile /repo` (talks to host Docker daemon via socket)
4. **Spawn restarter** -- `docker run -d --name os-restarter docker:cli ...` (a tiny Alpine container with Docker CLI)
5. **Touch flag** -- creates `/repo/.memory/restart_requested`
6. **Exit** -- the script is done; the restarter takes over

### The Ephemeral Restarter

The restarter is a one-shot container. Its entire script:

```sh
while [ ! -f /repo/.memory/restart_requested ]; do sleep 1; done
docker stop os-host
docker rm os-host
docker run -d --name os-host ... os
```

It polls for the flag file (step 5 above creates it), then stops the old container and starts a new one from the freshly built image with the same ports, volumes, and env vars. Then the restarter container exits (it's done).

### Cleanup on Boot

When the new container starts, `body/entrypoint.sh` runs these before anything else:

```sh
docker rm -f os-restarter    # remove the restarter container
rm -f /repo/.memory/restart_requested  # delete the flag file
```

No trace of the restarter remains.

## The Full Cycle

```
POST /evolve
    |
    v
[evolve.sh runs inside os-host]
    |
    +--> docker build -t os ...        (builds new image)
    +--> docker run os-restarter ...   (creates restarter)
    +--> touch restart_requested       (signals restarter)
    |
    v
[restarter sees flag]
    |
    +--> docker stop os-host           (kills old container)
    +--> docker run os-host ...        (starts new container from new image)
    +--> restarter exits
    |
    v
[new os-host boots]
    |
    +--> docker rm -f os-restarter     (cleanup)
    +--> rm restart_requested          (cleanup)
    +--> starts Xvfb, kernel, MCP proxy (normal boot)
```

## Why an Ephemeral Restarter?

A container cannot restart itself -- `docker stop` would kill the process running the command before the new container could start. The restarter is an independent container that outlives the old `os-host` long enough to start the new one.

It only exists during an evolve (~10 seconds). There is no persistent sidecar.

## Evolve Run Logs

Each evolve creates a directory under `.memory/runs/<timestamp>/` containing:
- `status` -- SUCCESS/FAILED
- `build_output.txt` -- Docker build stdout/stderr
- `test_output.txt` -- pytest output (if tests ran)

## Modifying the Evolve Process

Since `evolve.sh` lives in `/repo/scripts/` (the bind mount), the container reads the live version on every evolve. Edit the file on the host (or from inside the container), and the next evolve uses the updated script. No rebuild needed.

The `evolve` command is available anywhere inside the container (installed to `/usr/local/bin/evolve`).
