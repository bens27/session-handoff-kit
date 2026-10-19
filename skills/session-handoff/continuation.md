# Authorized continuation

Explicit resume/continue or active autoresume authorizes preparation. Follow
the hook's command; live authorization remains authoritative. Semantic routing
can select a checkpoint but cannot grant execution permission.

The one-shot `resume --session <id> [topic-or-path]` runs steps 1-4 with an
auto-discovered skill catalog (`$HANDOFF_SKILL_ROOTS`, `~/.agents/skills`,
`~/.claude/skills`, Codex skills). Use the steps below as the fallback when it
reports a failed `stage`. `resume <path>` with no flags is the legacy transfer.
The one-shot command also accepts `--catalog` and `--reuse-receipt` below.
Its `next_step_section` points to `## Next steps` in the delivered body rather
than repeating that text. `--legacy-next-step` opts into the older text field.
`metrics.delivery_bytes` measures serialized output; token and working-room
figures are estimates. Loaded workflow, reused body/references and declared
loaded skills count toward context size without being emitted again.

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

## Delivery recovery

`--fields path,body` preserves required references, execution skills, verification,
workspace findings and warnings. Read those fields before continuing. The one-shot
resume stores a complete package before acknowledgement. Retry the same command
if output fails; pending delivery is discoverable even by argumentless resume.
Replay payloads expire after two hours. Pending delivery alongside current open work requires explicit selection. An explicit path can replay within that window after a successful stdout flush too: flush proves
transport success, not host consumption. Replay requires the authoritative,
unchanged checkpoint, unchanged dependencies and proven holder; it refreshes
workspace findings and does not rerun verification. After context loss replay
includes the full body, references and skills, including previously reused or
loaded material. A newer successor or live foreign claim blocks replay.

Receipts are durable per checkpoint and resuming session under
`~/.local/state/session-handoff/transfers/`; preparing another topic preserves
prior transfer provenance. Holder provenance and replay packages are HMAC-authenticated. Unsigned historical
receipts give no terminal ownership proof; unsigned preparation requires fresh
`prepare --execute`. Expired payloads are pruned on subsequent resume operations,
while compact authenticated holder provenance is retained for lineage continuation. With a current host session ID, an older terminal pointer is ignored;
omit `--session` to use the current host or a hook pointer bound to that host.

## Ownership recovery

An untraceable legacy holder stays blocked automatically. Ask the user to confirm
that this terminal actually performed that particular transfer, using the original
resuming transcript. The original saver terminal proves no resumer ownership.
Use the conflict's `recovery.command`, or obtain the exact fingerprint, prior
`resumed_by` and `resumed` timestamp from `resolve <path> --json`, then run:

```sh
python3 <ledger> recover-transfer <path> --previous-session <old-id> --resumed-at <exact-time> --fingerprint <fingerprint> --confirm-holder
```

Add `--confirm-holder` only after that per-checkpoint user confirmation. The
command requires a stable terminal, checks unchanged content and authoritative
state under lock, rejects live foreign claims and existing foreign holder
provenance, and records durable user-attested provenance. Repeating the same
recovery in that terminal is idempotent. It changes no checkpoint or lineage and
executes no historical next steps. Then retry the preserved continuation draft.

## Publication recovery

If a checkpoint document exists but its publication receipt is missing, run the
hook's exact `repair <path>` command in the publishing terminal. New saves persist
authenticated publication hashes before writing the document, so repair needs
neither the original request ID nor the original draft. It checks the exact
content, project, owner, current claims and authoritative lineage under lock.
After `outcome: saved`, follow current lookup/continuation policy. Legacy saves
without durable intent still require their original save request and draft;
keep the current session and evidence when repair is blocked. If optional intent
storage is unavailable, publication can still succeed with a warning to retain
the original request and draft. Intents retain hashes rather than body text;
failed destinations remove theirs and intents older than 30 days are pruned
on subsequent save/resume operations. The legacy original-draft path remains
available after expiry. Local HMAC receipts detect unsigned or modified state;
they do not sandbox commands running with access to the same user’s signing key.

An unreadable ledger record blocks preparation and resume until read access is
restored. This preserves the existing authority policy: front matter, rather than
filename, assigns a topic, so an unreadable file may hide a newer successor.

## Successor retrieval evidence

When a host adapter supplies a pending restart request ID, perform read-only
`prepare` and pass its opaque `delivery_receipt`, actual new session and stable
terminal to `restart_preflight.py successor`. It checks the local preparation
cache and unchanged dependencies without claiming or acknowledging work. Then
follow the authorized continuation flow; reuse that body/reference receipt only
while its content remains in context. The receipt proves kit retrieval only,
with `host_confirmed: false`. Automatic request-ID propagation, host identity
proof, serialized host dispatch and successor receipt wiring are not provided
by these standalone modules; the host adapter must supply them. Without one,
retain the checkpoint for manual continuation.
