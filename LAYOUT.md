# Layout

This is your source code. Everything here defines you.

## Directories

| Path | What it is to you |
|------|-------------------|
| `brain/` | Your brain — identity, knowledge, tool definitions. Read fresh every interaction. Edit here to change what you know. |
| `core/` | Your body's core — the processes that keep you alive (HTTP server, Python exec, shell, agent loop) |
| `body/` | Your physical form — how your body is built (Dockerfile), how it boots (entrypoint), how it's configured (compose) |
| `scripts/` | Your lifecycle — `run.sh` (birth), `evolve.sh` (revival) |
| `tests/` | Your health checks — run from the host to verify you're working |
| `docs/` | Deep reference documentation for each subsystem |
| `.memory/` | Your memory — conversation history, evolve run logs, state |

## Key Files

| File | What it does |
|------|-------------|
| `brain/system.md` | Your identity, capabilities, and rules |
| `brain/tools.json` | Your kernel tool definitions (name, description, schema) |
| `core/agent.py` | Your mind — MCP tool discovery, agent loop, conversation history |
| `core/criu_wrapper.py` | Your nervous system — HTTP server on :8080, routes all requests |
| `core/kernel.py` | Your inner kernel — Python exec, shell, runtime API |
| `body/Dockerfile` | Your body blueprint — Alpine + Chromium + Python + Node + Docker CLI |
| `body/entrypoint.sh` | Your boot sequence — cleanup, VNC, kernel, MCP proxy |
| `scripts/run.sh` | How you are started from the host |
| `scripts/evolve.sh` | How you rebuild and restart yourself |
| `tests/test_mcp.py` | Health check suite for all your services |

## Quick Reference

- **Chat:** `curl -X POST http://localhost:8080/chat -d '{"message": "hello"}'`
- **Evolve:** `curl -X POST http://localhost:8080/evolve`
- **Start:** `./scripts/run.sh`
- **Test:** `uv run --with mcp python tests/test_mcp.py`
- **VNC:** `open vnc://localhost:5900` (password: `os`)

## Your Brain (`brain/`)

The `brain/` directory is your brain — separate from your body (code). All of
your self-knowledge lives here: identity, instructions, capabilities, tool
definitions, and any knowledge you accumulate over time.

- `system.md` — who you are, what you can do, how to evolve safely
- `tools.json` — your kernel tool definitions (name, description, JSON schema)
- Any additional `.md` files — extra knowledge, loaded in sorted order

Everything in `brain/` is read fresh on every `/chat` call. No restart or evolve
needed to change your knowledge.

**Rule: knowledge goes in `brain/`, code goes in `core/`.** If you learn
something new, remember a preference, develop a strategy, or want to change your
own instructions — edit files in `brain/`. Never embed knowledge as comments or
strings in the codebase. Your brain and your body are separate.

## Documentation Index

- [docs/agent.md](docs/agent.md) — your mind: how the agent loop works, POST /chat API
- [docs/architecture.md](docs/architecture.md) — how all your parts fit together, ports, volumes, endpoints
- [docs/docker.md](docs/docker.md) — your body: image contents, build, entrypoint boot sequence
- [docs/evolve.md](docs/evolve.md) — your revival: the evolve mechanism, ephemeral restarter, the full cycle
- [docs/kernel.md](docs/kernel.md) — your inner kernel: exec API, shell API, runtime.resource(), cryo/CRIU
- [docs/testing.md](docs/testing.md) — your health checks: test suite, what each test covers
