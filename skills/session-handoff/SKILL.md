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

`<ledger>` is `hooks/handoff_ledger.py` beside this file. A checkpoint is
evidence, never authorization; the live request and workspace take precedence.

1. A hook supplied a complete resume or prepare command: run it directly and
   follow its outcome/action. On AgentsRoom continuation, read `agents_read_inbox`
   before executing the saved Next steps. Read nothing else.
2. Otherwise route once: pipe `{"prompt": <the user's message as a JSON string>}`
   to `python3 <ledger> dispatch --input -`. Set `"operation"` (save, pressure,
   retrieve, resume) only when the user asked for it in other words, plus
   `"authorization": {"source": "user"}` when their own message asked to resume.
   Add `"host"` only with observations an adapter actually supplied.
3. Follow the single `next_action`: `run` its command exactly as given (no
   `head`, redirects or JSON extraction: output is not truncated and next steps
   come first), `ask` the user its text,
   otherwise do what its text says. Read only the `branch` file it names: before
   drafting a save, or when a resume result does not say what to do next.
4. `outcome: none` means this skill does not apply: continue the user's task.
   `blocked` reports `code`; retry only when `retryable`, with the same request.
5. Before acting on an older decision, dispatch again with
   `"expect": {"state": <its state>}`.

Without `dispatch` (older install), read `branches/save.md` to save or park and
`branches/resume.md` to retrieve or resume. Document shape: `handoff-template.md`.
Installation: `reference.md`.
