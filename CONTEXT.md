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

## Workspace probe

`skills/session-handoff/hooks/handoff_workspace.py`, the one module that asks
Git about a workspace and the one owner of Git error handling. `GitWorkspace`
runs `git -C root ...`; `MemoryWorkspace` answers from dicts so tests describe
a workspace instead of building a repository. Callers (protocol, dispatch,
ledger, pointers, lane) use the module functions; `use(adapter)` swaps the
adapter for a block. Each probe keeps its callers' failure contract: unknown
status is `None`/`False`/`''`, pointer tracking fails closed with
`git-check-indeterminate`, and bad worktree metadata raises `OSError`.
Linked-worktree root detection in `handoff_identity` stays file-based by design.

## Restart-intent store

`skills/session-handoff/hooks/handoff_intents.py`, the one owner of durable
restart intents: layout (`.handoffs/restart/<sha256(request_id)>.json` under the
canonical root, `.lock` sidecars, `registry`, `.observations`), the `TTL` table
(`pending` adoption window, `settled` retention, `delivery_receipt` age), the
states (`pending -> refused | retrieved`, a refusal may be re-recorded), strict
record validation (`status`), retention (`prune_settled`), and the successor
commands (`adopt_command`, the short restart handle). `restart_preflight`
decides when an intent is created or moved; `handoff_dispatch` and
`handoff_pointers` only read. The store imports neither caller, and preflight
and dispatch do not import each other: dispatcher holds live in
`handoff_holds.py`, which both read.

## Watch decision

`skills/session-handoff/hooks/watch_decision.py`, the pure core of the context
watcher: when it speaks and what latch state follows. `WatchConfig` reads the
pressure path's `CONTEXT_WATCH_*` settings once; `claude_observation` and
`codex_observation` turn each host's transcript usage into one `Observation`
(window, model, pending tokens); `cap_threshold` applies a reported window's
reserve; `decide` takes the observation, threshold and a `Latch` (the latch
files as read) and returns `rearm`, `quiet`, `notice` (1 or 2) or `floor-note`,
plus the floor latch to record. `context_watch.main` is the adapter: it reads
the event, transcript and latch files, calls the core, then removes or writes
latches, session notes, the log and the notice. The core imports no hook and
touches no file, environment or subprocess, so its rules are table tests.
