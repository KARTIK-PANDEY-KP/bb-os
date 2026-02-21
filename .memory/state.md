# Agent State

## Status
Idle — no evolve in progress.

## Last Evolve
None yet.

## Notes
- Evolve runs are logged under `.agent/runs/<timestamp>/`
- The `restart_requested` flag file is created by evolve and cleaned up on boot
- The ephemeral `os-restarter` container is created during evolve and removed on next boot
