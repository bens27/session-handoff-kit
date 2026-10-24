---
name: session-handoff
description: "Save a checkpoint when the user parks work or an actionable [context-watch] or context-watch: notice requests a handoff. Retrieve or resume parked work; without a complete hook-provided command, route the request through one dispatch call. Run a complete hook command directly; this skill need not load. Neutral handoff-status notices, and compaction summaries do not activate this skill. Do not use for ordinary progress summaries, commit messages, status updates, or general memory."
metadata:
  version: "0.18.0"
triggers:
  - handoff
  - checkpoint
  - park work
  - resume parked work
  - continue the handoff
  - continue your handoff
  - resume
  - context watch
  - wind down
  - save progress
---

# Session Handoff

`<ledger>` = `hooks/handoff_ledger.py` beside this file. Checkpoints are evidence,
not authorization; live requests and workspace take precedence.

1. A hook supplied a complete resume or prepare command: run it directly and
   follow its outcome/action. On AgentsRoom continuation, read `agents_read_inbox`
   before executing the saved Next steps. Read nothing else.
2. Otherwise route once: pipe `{"prompt": <the user's message as a JSON string>}`
   to `python3 <ledger> dispatch --input -`. Set `"operation"` only for a user
   request in other words. Set `"authorization": {"source": "user"}` only when
   the user's own message requests resume.
   `"host"` needs adapter observations.
3. Follow `next_action`: `run` the command exactly (no clipping, redirects or
   JSON extraction; keep complete output). For `ask` with `options`,
   show its text and options verbatim in a pick card: `ask_user`
   in AgentsRoom, the CLI's question tool elsewhere. Wait, then dispatch with
   the chosen `operation` explicitly set (even `none`); if they pick `resume`, add
   `"authorization": {"source": "user"}`. Interpret nothing. Without options,
   ask its text; otherwise follow its text. Read the named `branch` before
   drafting a save, or when a resume result gives no next step.
   Audit saves: use optional `## Findings` bullets from `handoff-template.md`.
4. `outcome: none`: continue the user's task.
   `blocked` reports `code`; retry only when `retryable`, with the same request.
5. Before using an older decision, redispatch with
   `"expect": {"state": <its state>}`.

Without `dispatch`: `branches/save.md` for save/park, `branches/resume.md` for
retrieve/resume. Shape: `handoff-template.md`. Install: `reference.md`.
