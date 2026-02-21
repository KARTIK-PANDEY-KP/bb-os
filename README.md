# os

Self-contained, self-evolving Docker environment with MCP servers, a Python kernel, VNC desktop, and an ephemeral restarter evolve loop.

```bash
./scripts/run.sh                              # start
curl -X POST http://localhost:8080/evolve     # self-evolve
uv run --with mcp python tests/test_mcp.py    # test
open vnc://localhost:5900                     # VNC (password: os)
```

See [LAYOUT.md](LAYOUT.md) for project structure and documentation index.
