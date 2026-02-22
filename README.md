# os

A self-contained, self-evolving autonomous agent living inside a Docker container. It runs continuously — thinking, acting, sleeping, and learning — without requiring external prompts.

## Architecture

The agent operates on a biological sleep-wake cycle driven by sleep pressure:

```
    AWAKE (continuous)                    SLEEP (continuous)
 ┌──────────────────────┐            ┌───────────────────┐
 │ heartbeat heartbeat  │            │ chunk chunk chunk  │
 │ heartbeat heartbeat  │──pressure──│ chunk chunk replay │
 │ heartbeat ...        │   wins     │ ...until done      │
 └──────────────────────┘            └───────────────────┘
         ▲                                    │
         └────────────────────────────────────┘
```

**AWAKE** — Continuous stretch of heartbeats (`/chat` calls) with full tool access. After waking, there's a guaranteed window of clear wakefulness where sleep is impossible. Then sleep pressure builds exponentially — each heartbeat has an increasing probability of triggering sleep. The exact moment is stochastic.

**SLEEP** — Continuous digestion (`/digest` call). No tools, no actions — pure reflection. Processes all new experiences in chunks plus random replay of old memories. Doesn't wake until everything is digested. Each chunk incrementally updates long-term memory (`.memory/learnings.md`).

### Maturity

The sleep-wake ratio evolves over the agent's lifetime:

```
maturity = (cycles / MATURITY_CYCLES) ^ GROWTH_CURVE
```

- Young agent: short awake, heavy sleep, lots of memory replay
- Mature agent: long awake stretches, efficient sleep, minimal replay
- Every parameter has random jitter — no two cycles are identical
- `GROWTH_CURVE` (default `0.5`): `< 1` = fast early growth, `> 1` = late bloomer
- `MATURITY_CYCLES` (default `500`): timescale to full maturity

## Capabilities

- **MCP tools**: browser-use (Chromium automation), filesystem, linux (system admin), context7 (library docs)
- **Kernel tools**: Python execution (persistent namespace), shell commands, self-evolve
- **Self-evolution**: Can modify its own source code, rebuild its Docker image, and restart
- **State persistence**: Cryo save/restore for Python namespace across restarts
- **Long-term memory**: Accumulated learnings from sleep digestion, maturity-aware

## Quick Start

```bash
./scripts/run.sh                              # start (daemon on by default)
DAEMON_ENABLED=false ./scripts/run.sh         # start without autonomous daemon
curl -X POST http://localhost:8080/chat -d '{"message": "hello"}'  # talk
curl -X POST http://localhost:8080/digest     # trigger digest manually
curl http://localhost:8080/digest/learnings   # read accumulated learnings
curl -X POST http://localhost:8080/evolve     # trigger self-evolve
open vnc://localhost:5900                     # VNC (password: os)
uv run --with mcp python tests/test_mcp.py   # test
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/chat` | POST | Chat with the agent (full tool access) |
| `/digest` | POST | Trigger sleep digest (accepts optional `replay_ratio`) |
| `/digest/learnings` | GET | Read accumulated learnings |
| `/chat/history` | GET | Full conversation history |
| `/chat/log` | GET | Tool call and thinking log |
| `/exec` | POST | Execute Python code |
| `/shell` | POST | Run shell command |
| `/evolve` | POST | Trigger self-evolve |
| `/cryo/store` | POST | Save Python namespace to disk |
| `/cryo/reload` | POST | Restore Python namespace |
| `/ping` | GET | Health check |

## Project Structure

See [LAYOUT.md](LAYOUT.md) for full project structure, the heartbeat cycle details, brain/memory organization, and documentation index.
