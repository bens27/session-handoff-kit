# Session Handoff — reference

Loaded on demand by `SKILL.md`: read this when installing the hooks,
customizing the skill, or running it without the hooks. Not needed for an
ordinary handoff or resume.

## Installing

This folder is the whole product: put it wherever your agent loads skills
(`~/.claude/skills/`, `~/.agents/skills/`, a project's skills folder; copy or
symlink), then register the always-on hooks from that location:

    python3 install.py              # Claude Code and Codex
    python3 install.py claude       # or: codex
    python3 install.py --uninstall  # remove them again

It writes absolute paths to `hooks/context_watch.py` into
`~/.claude/settings.json` and `~/.codex/hooks.json` (`$CLAUDE_CONFIG_DIR` /
`$CODEX_HOME` respected), backs each file up first, leaves every other hook
alone, and is safe to re-run; re-run it after moving this folder. Codex also
needs hooks enabled and the changed hook re-approved: the installer prints
those steps. Using the Claude Code plugin instead? It already registers the
hooks; do not also run the installer, or the watcher runs twice.

### Runtime snapshots and recovery

Keep development edits separate from installed execution with the opt-in runtime
CLI (Python standard library; macOS/Linux). From this skill folder:

```sh
python3 install.py runtime --root ~/.local/share/session-handoff/runtime create v1
python3 install.py runtime --root ~/.local/share/session-handoff/runtime validate RELEASE
python3 install.py runtime --root ~/.local/share/session-handoff/runtime activate RELEASE codex
python3 install.py runtime --root ~/.local/share/session-handoff/runtime rollback codex
```

Use the complete `release` ID returned by creation, including the SHA-256 suffix;
choose `claude` instead of `codex` for that host. Creation copies the declared product files,
rejects symlinks and special files, checks required files/Python syntax/ledger
CLI loading, then publishes a read-only payload with a versioned manifest.
The ID binds the manifest; validation checks every payload hash and mode and
rejects missing/extra files. Run project regression checks before creation;
these integrity and smoke checks do not certify all runtime behavior.
Creation and validation do not modify host hook configuration.
The declared payload includes `lane.py` and `hooks/thresholds.example.json`.
Undeclared files (including secrets, local configuration and editor files) cause
creation to fail before publication; bytecode caches are excluded. Keep local
configuration outside the skill folder. Storage root and `releases/` are private
(`0700`); sealed directories/executables use `0500` and data files use `0400`.

Activation is explicit and handles one host at a time, respecting
`CLAUDE_CONFIG_DIR` and `CODEX_HOME`. It validates the release, preserves foreign
hooks, writes a private durable receipt, then atomically replaces that host's
configuration with commands pinned to the immutable release path. A process
interruption leaves a complete old or new config. Retrying is safe. Filesystem
locks serialize kit runtime switches; coordinate other config editors separately.
Codex still needs its hooks flag and native approval for the changed command.
An interrupted creation between publication and the final directory seal is
recovered by repeating the same `create VERSION`. Only a fully verified payload
with an unsealed top-level directory is repaired. Corrupt releases are retained
with an explicit instruction to inspect and remove that release before retrying.

Configure host skill discovery to use the returned `snapshot` directory too;
preserve the prior skill folder and use a symlink if appropriate. No skill
directory or plugin marketplace registration is changed automatically. Avoid
duplicate Claude plugin hooks. Existing sessions may retain previously loaded
instructions until their next normal replacement; no live restart is performed.

Rollback restores exact prior config bytes only when they still match a recorded
activation, and validates a prior snapshot before restoring it. If a later edit
exists, reconcile it explicitly rather than overwriting it. A first rollback
can restore a legacy mutable-tree install (`release: null`); skill discovery is
restored separately. Keep runtime `releases/` and private `transactions/` intact
while in use. Receipts contain prior configuration, so protect the runtime root
like host config. Snapshots consume local disk per version with no automatic
pruning. Read-only modes protect against accidents, not an owner rewriting files.
Malformed or unsupported receipts are skipped after schema/format validation;
successful rollback reports their count as `skipped_receipts`. Receipts are
recovery metadata; rollback does not create an undo receipt.

## Protocol and limits

The public CLI is `hooks/handoff_ledger.py`; `--help` lists commands. `lookup`
returns explicit outcomes and five bounded summaries per page. `history` is
paginated inspection. `resolve` returns authoritative metadata, not a full
history dump. The importable legacy resolver still returns the chain.

`session` reads live identity, transcript path and pressure state in one call.
A valid hook identity takes precedence while bound to the current host session.
Otherwise `CODEX_THREAD_ID` supplies `session_id` with `identity_source: host`;
`HANDOFF_SESSION_ID` is the fallback when neither supplies identity. `unrecorded`
means no valid hook identity, even when the host supplies a session ID.
`host_session_id` also exposes `CODEX_THREAD_ID` separately. This diagnostic does
not scan transcript directories or write hook state. Use it when identity or
transcript evidence is needed, rather than inspecting hook internals.

`save --session ID --request-id ID --input draft.json` validates a JSON draft,
publishes a complete checkpoint atomically, and retries identical requests
without creating another checkpoint. `save --template` emits a valid JSON draft; the field/body guidance is in
`handoff-template.md`. `save --help` documents the schema without source inspection.
A published successor's `predecessor` is the committed lineage record; readers
suppress the predecessor even if a crash happens before any later operation.
Sharing a branch does not establish predecessor identity.
Trusted predecessor dependencies inherit when omitted; explicit lists replace
individual fields, including `[]` to clear them. The optional `continuity` register
merges by stable ID; omission or `[]` preserves prior entries. See
`handoff-template.md` for schema and explicit resolution/retirement. Untrusted
predecessors supply neither implicit dependencies nor register entries.

Save tries the established directory, then the other supported location:
project `.handoffs/` and `~/.claude/handoffs/<project-basename>/`. Both are read,
as is legacy `HANDOFF.md`. Failure returns an explicit action; host sandbox
or approval denial cannot be bypassed by filesystem fallback.

`prepare TOPIC --session ID` retrieves only. `--execute` claims for preparation;
`verify PATH --session ID` runs the recorded check; `acknowledge PATH --session ID`
marks transfer only after successful preparation. Required dependencies are
fingerprinted; changed files require preparation again. `release PATH --owner ID`
makes failed or abandoned preparation immediately available again. Leases last
two hours. Preparation returns a `delivery_receipt`. When its body/references
remain in the same session context, pass `--reuse-receipt TOKEN` on continuation.
The server checks session, checkpoint and reference fingerprints and a two-hour
expiry; invalid receipts cause a full reload. Compaction hooks invalidate the
receipt. Without hooks, omit it after compaction or any context loss. Retrieval
writes only an ephemeral delivery cache; checkpoint state remains read-only.
Reused content still counts toward the full package size. Execution additionally
counts the phase-specific `continuation.md` instructions.

`resume` accepts the same catalog and receipt options as `prepare`. Its default
JSON carries a complete body and a `next_step_section` pointer to `Next steps`;
`--legacy-next-step` (or `--fields next_step`) also emits the older text field.
Use catalogs only for installed skills, and declare `loaded` only for content
still in this context. `--fields` selects optional output, while successful prepare/retrieve/resume
always retain available body, required references/skills, workspace and verification
facts, receipt/reuse markers, predecessor and continuity. Diagnostics also survive
filtering. Serialization and delivery-byte accounting use the same projection.
Workflow sizes account for instructions, not a workflow
text field emitted by resume. Read continuation instructions for staged recovery
or custom catalogs, rather than loading them on every successful one-shot resume.

Save returns a `paused` transition when automatic handoffs would thrash: pressure
within ten minutes of resume, a third pressure checkpoint of one topic within
30 minutes, five previous AgentsRoom restart intents within an hour, or another
session's restart intent within two minutes. The hourly budget deliberately
stops before AgentsRoom's advertised six/hour limit. Intent counts are local to
the terminal and cannot observe restarts outside this kit. Resume timestamps
remain in the durable ledger even if temporary hook notes disappear.
An identical retry preserves a pause; a retry of a transferred/closed checkpoint
returns transition `none` so it cannot request a fresh restart of completed work.
AgentsRoom transition candidates carry `requires_preflight: true`; host dispatch
requires authoritative current identity/capability/inventory/hold observations
and a ready/dispatch result. Unknown observations require manual continuation.
The kit supplies no host observation or serialized dispatch adapter.

Pausing preserves a full, published, open checkpoint. Automatic startup, tmux
Stop and the headless runner respect the pause; the runner returns exit 75.
After reducing re-entry overhead or waiting for the host budget, open a fresh
session and explicitly ask to `continue the handoff`. A refused host restart
uses that same recovery: keep the checkpoint open and report the refusal. Avoid
retrying the restart or marking the work resumed without delivery.

State mutations use sidecar locks and atomic replacement, and all
callers respect ownership, including those omitting an owner.

The complete serialized package, workflow instructions, and declared already-loaded
skills have a recommended size of 32,000 bytes. The estimated token count is bytes/4,
not a tokenizer measurement or billing total. `--budget-bytes` changes this advisory
threshold; exceeding it reports a warning and still delivers all required content.
Aim for checkpoints under 1,500 words / 24,000 bytes, about eight required references
and sixteen skill names. These are recommendations, not rejection or truncation
limits. Required references support `path#LSTART-LEND`; background belongs in
`optional_references`. Skill catalogs are caller-supplied installed paths, never
downloaded dependencies. Aliases and already-loaded skills are deduplicated by
canonical path. There is no minimum checkpoint length. Choose required excerpts
for the immediate next action; preserve essential context even when it exceeds
these targets. Keep published checkpoints intact and use a successor save when
revising their contents.

Verification uses Bash pipefail, a default 60-second timeout (configurable to
one hour), a durable full log in `.verification/`, and at most 2,000 bytes of
failure excerpt. It runs only for authorized execution preparation. The logged
command must be reviewed against live task authorization like any other shell
command; checkpoint text does not expand permissions.

Resume telemetry defaults to `~/.context-watch/resumes.jsonl`; override with
`CONTEXT_WATCH_RESUME_LOG`, or disable with `0` (global `CONTEXT_WATCH_LOG=0`
also disables it). Events contain IDs, outcomes, component sizes, and available
host input samples, never checkpoint or verification contents. Startup baseline
and later input samples are separate; cached tokens still occupy context.
Events carry protocol `version` and `origin` (`live` by default; fixture callers
set `CONTEXT_WATCH_ORIGIN=test`). Tests disable the production sinks.
`metrics.delivery_bytes` is the actual serialized resume JSON size, including
metrics. `estimated_delivery_tokens` uses UTF-8 bytes/4. When a hook sample exists,
resume also reports its occupancy, threshold, window and source, then adds only
estimated delivery tokens to estimate post-resume occupancy and working room.
Under 60,000 remaining tokens produces an advisory warning. Schema loads, future
reads and stale samples limit this estimate; absent telemetry stays unknown.
`python3 hooks/handoff_ledger.py report` returns bounded aggregates, excluding
known tests and separating old records with unknown origin. It reads at most
the last 2 MB and reports when the window is partial. Byte counts are not billing.

Startup stays quiet except for an incomplete owned publication needing repair.
An explicit “continue the handoff” prompt gets an exact-path lean command for one
trusted owned checkpoint; ambiguous or untrusted owned state gets a skill pointer.
Manual retrieval remains available for portable handoffs and other CLIs. Compaction, threshold
configuration, unrelated routing and service failures use `handoff-status:`;
only actionable save/retrieve/resume messages activate the skill.

Session notes are scoped to project plus session. Automatic completion uses
session plus trigger identity on a validated publication. The headless runner
adds a run identity; unrelated file modification never counts as a checkpoint.
With hooks, omit `--session`; the recorded terminal identity wins over a differing
explicit ID with a warning. Without a recorded identity, create one session UUID, use `lookup`,
and follow the same save/prepare/verify/acknowledge workflow. Automatic clearing
requires installed hooks or the runner.

## Customizing

Keep the required Objective, Current state and Next steps sections and the
shared contract in SPEC section 7.4. Optional body sections may be omitted.
Change the draft template rather than duplicating workflow policy. Run the
skill and protocol checks after changes. Explicit authorization and actual
workspace evidence take precedence over stored instructions.

Host replacement summaries must respect host-owned storage rules. This kit's
supported durable checkpoint interface is `hooks/handoff_ledger.py save` with
project `.handoffs/` or the documented home fallback; `save --template` and
`save --help` describe its draft format. A host that specifically requests
`.agentsroom/handoff-summary-*.md` while prohibiting agent writes there must fix
its replacement workflow. The kit cannot redirect that request silently: the
successor must receive the actual saved checkpoint path or a host-persisted
summary. The host can persist summary text itself and deliver it to its successor,
or request a permitted durable path and read that same path on replacement.
Keep the general protection of `.agentsroom/` runtime files intact. Until the
host producer and consumer agree on a permitted transfer, report the dependency
and keep the existing session/checkpoint available instead of claiming transfer.

Publication receipts: generated checkpoints carry a `.published` sidecar binding
the publication ID to its content fingerprint. Until that receipt is durable,
lookup reports `incomplete` and automatic clearing is blocked. Retrying the same
request completes an interrupted receipt. Legacy checkpoints without publication
IDs remain readable. Keep receipt sidecars with generated checkpoint files.


## Integrity and cost controls

Continuing an existing topic requires its authoritative `predecessor` path;
a conflicting save returns that path without replacing the current checkpoint.
Empty legacy files cannot be acknowledged. `CONTEXT_WATCH_JEV=1` explicitly opts
into semantic routing, at most once per project/session; the default is local
routing only. The automatic runner stops on child failure and returns exit 75
when its positive run limit is exhausted. It has no aggregate spending or
wall-time guarantee; enforce those through the host.

Codex installation uses separate hook groups (verified on 0.157.1), preserving
other commands' outputs, matchers and failure statuses. Upgrade older Codex builds
before relying on this registration. Reinstalling an old fan-out restores the
original command and may require native `/hooks` trust review.

Live delegated workers: a session that saves while Agent-tool subagents are
running saves now but defers the transition (`/clear`, `agents_restart`, runner
restart) until they report or their results are confirmed durable in commits or
the AgentsRoom mailbox; their reports reach only the spawning session. Prefer
AgentsRoom workers (`agents_spawn` + mailbox) for long parallel lanes. The
watcher ignores hook events that carry `agent_id` (subagents).

Consoles without tmux: automatic clearing needs tmux (`claude-auto`) or the
headless runner. With auto mode on in AgentsRoom, the context notice has the
agent call `agents_restart` with prompt `continue the handoff`; this avoids native
CLI subcommands such as Codex's `resume`. No typing is needed. Typing
`/clear` then `resume` yourself applies only with auto mode off, or in another
console without `TMUX_PANE`, where the Stop hook announces the saved handoff
once per trigger.

### No-handoff and interrupted-publication recovery

A clean session or premature exit before saving has no saved kit context.
`lookup`/`resume` report `none` when no eligible checkpoint exists; an explicitly
missing target does not fall back to other topics. Native conversation recovery
is a separate host feature: the kit does not rewrite its transcript or invent
unsaved state. Hook noninterference tests are not proof that a real host restored
its conversation. Use supported native history/resume for that recovery.

Ordinary startup and native-resume events do not consume an older owned or foreign
checkpoint. An explicit handoff request can offer the actual older owned state;
it cannot reconstruct later unsaved work. With a known terminal, a sole foreign
checkpoint requires explicit selection, including an untagged checkpoint whose
session differs or is unknown. Matching session ownership can select untagged
state; no-current-terminal portable selection remains supported. Pass the actual
session to `lookup --session` for consistent read-only hints. Missing caller
identity never fabricates ownership, and resume still requires a session. Existing claim and trust checks still
apply to deliberate cross-terminal adoption.

If publication stopped before the document reached disk, retain the draft and
retry the same save request. If the document exists without its publication
receipt, it remains incomplete; use authenticated `repair` or the original save
request. A missing/deleted or truncated checkpoint is not silently rebuilt from
an intent or transfer receipt. Restore verified original bytes/draft explicitly
before repair; preserve evidence and native history meanwhile. Pending restart
intent is not proof of saved context or permission to redispatch; keep host-side
reconciliation with the lifecycle owner.
