# Context

Domain terms used across this repository.

## Identity module

`skills/session-handoff/hooks/handoff_identity.py`, the leaf module every hook
imports to answer three questions:

- **Who am I?** Session identity (host session id, session key) and terminal
  identity (`HANDOFF_TERMINAL_ID`, else `AGENTSROOM_AGENT_ID`).
- **Where is my project?** The canonical project root, resolving linked
  worktrees and fallback directories.
- **Where is my state?** Every temp-state path (latches, session notes,
  receipts, pointers, auto counts) under the state directory:
  `HANDOFF_STATE_DIR`, else the system temp dir.

It imports nothing from the other hooks, so any hook can depend on it.
