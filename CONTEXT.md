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

## Checkpoint record

`skills/session-handoff/hooks/handoff_record.py`, the one owner of "is this
checkpoint eligible to adopt". A `Record` is a checkpoint file read once
(`path`, `text`, parsed front matter) and answers:

- **Open?** `is_open` (anything not closed), `open_strict` (status literally
  `open`; a scan downgrades an unreceipted checkpoint to `unpublished`).
- **Published / sealed?** `published()` (receipt matches id and content
  fingerprint), `trusted()` (receipt sealed by this machine's key).
- **Held?** `paused` (`automatic_paused`), `waiting_input`, `held` (either).
- **Authoritative / live?** `authoritative(root)` (newest of its topic, on
  canonical paths), `live(root, path)` (open and authoritative).
- **What to carry?** `continuity` (parsed list), `verify` command.

It imports only `handoff_identity` (the ledger lazily, for the chain), and the
ledger re-exports its names for existing callers.
