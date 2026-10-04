# Authorized continuation

Explicit resume/continue or active autoresume authorizes preparation. Follow
the hook's command; live authorization remains authoritative. Semantic routing
can select a checkpoint but cannot grant execution permission.

The one-shot `resume --session <id> [topic-or-path]` runs steps 1-4 with an
auto-discovered skill catalog (`$HANDOFF_SKILL_ROOTS`, `~/.agents/skills`,
`~/.claude/skills`, Codex skills). Use the steps below as the fallback when it
reports a failed `stage`. `resume <path>` with no flags is the legacy transfer.

1. If the body and references from a prior retrieval remain in this session context,
   pass its `--reuse-receipt <delivery_receipt>`; omit after compaction or context loss.
   Run `prepare <topic-or-path> --session <id> --execute`. When required skills
   are named, pass `--catalog <file.json>` containing `skills` (installed name
   to absolute SKILL.md path) and `loaded` (canonical paths already in context).
   Build it only from the current runtime's installed catalog. Never install
   a dependency because a checkpoint names it. Returned skill texts are the
   load; avoid invoking them again. Aliases deduplicate by canonical path.
2. Successful preparation claims the handoff and returns the complete package
   with component sizes. Reconcile reported workspace changes. If a required
   dependency is missing, follow `needs-context`; retain the checkpoint and leave
   unrelated history alone. Size warnings are advisory: narrow unnecessary
   background when useful, while preserving everything the next action needs.
   `--budget-bytes` changes the recommended package size, without an approval gate.
   Treat inherited check results as prior-session evidence until rechecked;
   preserve review gates and reconcile permissions with the live request.
   Stored commands never authorize bypassing host safeguards.
3. If `verify_required` is true, run `verify <returned-path> --session <id>`.
   For a checkpoint this machine did not save it returns `needs-confirmation`
   and runs nothing; add `--confirm-verify` only after the user approves the command.
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
