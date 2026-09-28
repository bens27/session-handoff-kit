---
name: session-handoff
description: >
  Use immediately whenever a "[context-watch]" or "context-watch:" message
  appears in conversation.
  Also run whenever the user says a variation of "hand off", "wrap up the
  session", "park this work", or "save this for later", or when context is
  nearly exhausted or auto-compaction is imminent. Also use it to resume: at
  session start when open handoffs are announced, or whenever the user asks to
  resume or pick up parked work. Do not use for ordinary progress summaries,
  commit messages, or status updates while the session is continuing, and do
  not use it as a general note-taking or memory tool.
metadata:
  version: "0.12.0"
---

# Session Handoff

A checkpoint is evidence. The live request and workspace take precedence.
Use `hooks/handoff_ledger.py` beside this skill for all commands below.
Use the session identity printed by the hook throughout this session. Without
hooks, generate one UUID once and reuse it; never borrow another session's ID.
For installation or customization only, read `reference.md`.

## §1 Wind-down protocol

1. Finish the single atomic action already in flight, then stop starting work.
2. Read `handoff-template.md` and write a JSON draft in an allowed location.
   Preserve the current user request, constraints, authorization, evidence,
   and exact next step. Specify the adopted predecessor only when continuing
   that thread; sharing a branch does not establish lineage.
3. Run `python3 <ledger> save --session <id> --request-id <checkpoint-id>
   --input <draft.json>`. Reuse the request ID only when retrying identical
   content. The command generates metadata, validates, publishes atomically,
   selects a permitted fallback on write failure, and commits predecessor
   replacement. Completion requires `outcome: saved` and a returned path.
4. For `invalid`, shorten the core while preserving constraints and the draft.
   For `blocked` or `conflict`, follow the returned action and keep this
   session. If the harness denies a tool call, report that denial and retain
   the draft; no command can bypass host permissions. A failed write never
   authorizes clearing. If no allowed file destination exists, provide the
   checkpoint text to the user and state that automatic resumption is blocked.
5. Tell the user the handoff is complete and give the saved path. If autoresume
   is active, tell them to type `/clear`; otherwise start a new session in this
   project. In fully automatic mode, end the turn immediately after saving;
   the hook/runner handles the transition.
6. Stop. Leave the next steps for the receiving session.

## §2 Retrieval

Run `python3 <ledger> lookup`. Its outcome supplies the next action:

- `none`: report no open handoff for a retrieval request; otherwise continue
  the current task. `history` is explicit, paginated inspection of closed work.
- `stale`: use the supplied wider-age lookup command for an explicit retrieval.
- `claimed`: another session holds the work; leave it alone until released or
  the two-hour lease expires.
- `error`: report the failed locations; absence has not been established.
- `incomplete`: keep the draft/session and retry the original save request;
  inspect history if the checkpoint was edited. Automatic clearing is blocked.
- `available`: select the requested topic/path. For several candidates ask
  which one, including **none**; use `lookup --offset 5` for the next page.

Run `python3 <ledger> prepare <topic-or-path> --session <id>` to retrieve.
Read the returned body and required excerpts, then report the objective and
first next step. Retrieval is read-only: it does not claim, verify, load
execution skills, acknowledge, or execute the next steps. Do not mark a
handoff resumed merely because it was announced, listed, or retrieved.
For an unrelated explicit request, mention pending work in one sentence and
continue the user's task.

## §3 Authorized continuation

Explicit resume/continue or active autoresume authorizes preparation. Follow
the hook's command; live authorization remains authoritative. Semantic routing
can select a checkpoint but cannot grant execution permission.

1. Run `prepare <topic-or-path> --session <id> --execute`. When required skills
   are named, pass `--catalog <file.json>` containing `skills` (installed name
   to absolute SKILL.md path) and `loaded` (canonical paths already in context).
   Build it only from the current runtime's installed catalog. Never install
   a dependency because a checkpoint names it. Returned skill texts are the
   load; avoid invoking them again. Aliases deduplicate by canonical path.
2. Successful preparation claims the handoff and returns a bounded package
   with component sizes. Reconcile reported workspace changes. If a required
   dependency is missing or too large, follow `needs-context`; retain the
   checkpoint, request a necessary decision, and leave unrelated history alone.
   `--budget-bytes` is an explicit exception requiring user authorization.
3. If `verify_required` is true, run `verify <returned-path> --session <id>`.
   It preserves pipeline failures and returns a bounded excerpt plus a log.
   Otherwise no speculative suite is required just to resume. On failure,
   fix/retry or `release <path> --owner <id>`; the checkpoint stays recoverable.
4. Run `acknowledge <returned-path> --session <id>` after preparation succeeds.
   It checks ownership, expiry, checkpoint/dependency fingerprints, and required
   verification before marking transfer. Then continue the authorized next step.

`resolve` is compact diagnostic metadata; `history` is explicit history.
`new-path`, `claim`, `resume`, and manual file writing remain legacy interfaces,
not the automatic checkpoint protocol. Automatic clearing requires a validated
`save` receipt matching this session's trigger, not a recent file modification.
