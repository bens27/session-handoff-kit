---
name: session-handoff
description: "Save a checkpoint when the user parks work or an actionable [context-watch] or context-watch: notice requests a handoff. Retrieve or resume parked work on an explicit request or active autoresume. Neutral handoff-status notices, and compaction summaries do not activate this skill. Do not use for ordinary progress summaries, commit messages, status updates, or general memory."
metadata:
  version: "0.16.1"
---

# Session Handoff

Use `hooks/handoff_ledger.py` beside this skill. A checkpoint is evidence;
the live request and workspace take precedence. Reuse the session ID supplied
by the hook; without hooks, generate one UUID for this session.
For installation/customization only, read `reference.md`.

## §1 Wind-down protocol

1. Finish the atomic action in flight, then read `handoff-template.md`.
   `python3 <ledger> save --template` prints a valid JSON draft. Replace its
   facts with the authorized objective, constraints, evidence and exact next step.
   Build a minimal continuation core, not a session report. Require only excerpts
   needed for that next action; keep source files and background as optional paths.
   Continuing an existing topic requires its authoritative `predecessor` path;
   use a distinct topic for independent work.
2. Run `python3 <ledger> save --input <draft.json>` (or `--topic T
   --description D --body file.md`; `--attach <file>[#Lstart-Lend]` adds a required reference).
   Session, request ID, project, your own predecessor and `verify` are deduced;
   pass flags only to override. Identical retries are idempotent.
   Success requires `outcome: saved` and a path; metadata and publication are
   generated. Keep `.published` sidecars with their checkpoints.
3. On any failure, follow the returned action and preserve the draft/session.
   A permission denial never authorizes bypassing the host or clearing. If no
   destination is allowed, give the checkpoint text to the user and report that
   automatic resumption is blocked.
4. Tell the user the handoff is complete and give its path, then follow the
   save result's `transition.action` (new session, `/clear`, tmux/runner, or
   AgentsRoom `agents_restart` with prompt `resume`).
5. Stop. Remaining authorized work belongs to the receiving session.

## §2 Retrieval

Run `python3 <ledger> lookup` unless the hook already supplied a prepare command.
Follow its outcome/action; empty lookup means report no open handoff, without
searching history. For multiple candidates ask which (including none).
`history` is explicit paginated inspection of closed work.

Run `python3 <ledger> prepare <topic-or-path> --session <id>`.
Read its body and required excerpts, then report the objective and next step.
Treat inherited verification as prior-session evidence, not a fresh check.
A budget failure calls for a smaller core and narrower dependencies; increasing
`--budget-bytes` requires explicit user authorization.
Retain `delivery_receipt` only while that content remains in this context.
Retrieval does not claim, verify, load execution skills, acknowledge or execute.
Do not mark a checkpoint resumed merely because it was listed or retrieved.
For unrelated work, mention pending work in one sentence, then continue the user's current task and leave checkpoints open.

## §3 Authorized continuation

Only explicit resume/continue or active autoresume authorizes execution.
Run `python3 <ledger> resume --session <id> [topic-or-path]`: one call selects,
prepares, verifies if recorded and acknowledges, then returns `next_step`.
On `choose`, ask which; on a failed `stage`, follow its action. Read [continuation.md](continuation.md) for catalog, preparation, verification
and acknowledgment. Semantic routing alone selects context, not authorization.
