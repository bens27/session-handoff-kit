# Session Handoff — reference

Loaded on demand by `SKILL.md` or a branch: read this when installing the hooks,
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

Project discovery starts from the caller's cwd (or an explicitly selected root),
not the installed script's location. A linked Git worktree is a discovery
boundary: the nearest handoff convention inside it wins, otherwise its own Git
worktree root is used. An established home-fallback claim remains in use;
otherwise new worktree checkpoints prefer local `.handoffs/` even before it
exists. The `.git` file bounds discovery even without a working Git executable;
submodules and other non-worktree Git-file checkouts retain ancestor
convention discovery, including superproject holds. Ordinary folders
and Git-directory repositories retain ancestor convention discovery. Checkpoints
previously misplaced in main are not moved: intentionally inspect/continue them
with protocol `prepare`/`resume --session ID` and explicit `--root <main>`.
Holds are shared controls, not isolated checkpoint state; see Dispatcher below.

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
JSON carries a complete body, a `next_step_section` pointer to `Next steps` and a
bounded `next_step` lead (cut at 3000 encoded bytes with a `continues in the body`
marker); `--legacy-next-step` (or `--fields next_step`) emits the full text instead.
Use catalogs only for installed skills, and declare `loaded` only for content
still in this context. `--fields` selects optional output, while successful prepare/retrieve/resume
always retain available body, required references/skills, workspace and verification
facts, receipt/reuse markers, predecessor and continuity. Diagnostics also survive
filtering. Serialization and delivery-byte accounting use the same projection.
Workflow sizes account for instructions, not a workflow
text field emitted by resume. Read continuation instructions for staged recovery
or custom catalogs, rather than loading them on every successful one-shot resume.

Save pauses for rapid/repeated context-pressure saves only when the current
checkpoint shows no progress against its trusted same-topic pressure predecessor.
Progress is a changed draft content hash AND a changed effective continuity
register, forward Git commits, changed observed working-file hashes, or a strictly larger count of grounded
source anchors declared under `## Findings` as ``- `relative/file.ext:42`: conclusion``.
Audit anchors require readable regular files under the save workspace root and
in-range positive line numbers. Save resolves the selected workspace root once,
including a symlinked root; source paths are relative to that resolved root.
Relative path segments allow word characters, dots and hyphens, including dotted
directories and extensionless filenames; absolute paths, empty/`.`/`..` segments
and symlinks in any source-path component
beneath the root are excluded. Fenced examples are excluded; closing fences allow
only whitespace after the matching marker. Extraction bounds are 64 KiB of body,
1024 bytes per bullet, 256 KiB per
source and 1 MiB total source content (plus a one-byte oversize probe). Metadata
stores a sorted sample of up to 64 distinct path/line hashes plus an overflow
count, so progress can continue past 64 anchors. The total count must grow:
rewording, reordering, removing, bumping a line or rotating anchors at the same
count does not count. This grounds declared evidence, not the conclusion's
correctness. Ordinary body rewrites and unknown evidence do not count; legacy
valid lists without overflow use zero. File hashing skips
Git directory entries and excludes only top-level `.handoffs`, `.agentsroom` and
`.git` paths. With no same-topic pressure predecessor, the baseline is the
checkpoint this session resumed under a valid transfer receipt. A branch name change alone does not count; a newer descendant
commit does, even on another branch. The exact bounds,
exclusions and evidence rules are defined in [SPEC](../../SPEC.md#72-deterministic-checkpoint-protocol)
under automatic transition guards (`save_progress` is the implementation source).
A save within ten minutes of resume or the third same-topic pressure save within
30 minutes still reports diagnostics and `thrash_warning` front matter; with
progress these are advisory and automatic continuation proceeds. The hook warns
before saving; the save result decides whether progress warrants continuation.
Long-running orchestrators that resume one topic by design can list it in
`"continuous_topics": ["<topic>"]` in `~/.context-watch/thresholds.json` or
`<root>/.context-watch.json`: for those topics `rapid-resume` and `repeated-topic`
stay in the diagnostics (`continuous_topic: true`) but never pause. That flag means
an explicit configured exemption; sharing a predecessor's topic alone leaves it false.
Required headings
match `## Objective`, `## Current state`, `## Next steps` exactly at their start;
trailing text after a space (for example `## Current state (main abc123)`) is allowed.
When this terminal owns several open checkpoints, resume asks the user to choose
from a list that shows each one's created, parked and last-resumed times. Set
`"owned_selection": "newest"` in `~/.context-watch/thresholds.json` (or
`HANDOFF_OWNED_SELECTION=newest`) to resume the newest one automatically and list
the others as alternatives. A project `.context-watch.json` cannot set it.
Without that setting, one exception skips the question: if this terminal saved its
newest checkpoint within the last hour and no other owned checkpoint falls in that
hour (the park, `/clear`, "Continue the handoff" case), that one is selected as
`just_parked` and the older ones are listed as alternatives. Two fresh parks, or a
newest checkpoint from another terminal, still ask.
Trade-off: `<root>/.context-watch.json` is project scope, so a repository file can relax the pause for
the topics it lists; `restart-budget` and `restart-cooldown` remain hard stops regardless.
Five previous AgentsRoom restart intents within an hour or another session's
intent within two minutes remain hard stops, even with progress. Intent counts
are terminal-local and cannot observe restarts outside this kit. Durable resume
timestamps back up temporary notes. Restart adoption records the original
acknowledgement clock and topic in the observed successor's root/session note;
same-successor replay preserves that clock, including worktree-bound transfers. An identical retry preserves a recorded pause;
a transferred/closed checkpoint returns transition `none` and cannot restart work.
AgentsRoom transition candidates carry `requires_preflight: true`; host dispatch
requires authoritative current identity/capability/inventory/hold observations
and a ready/dispatch result. Unknown observations require manual continuation.
Follow [AgentsRoom restart](#agentsroom-restart) to gather the observations
with supported tools and dispatch as the final tool action using preflight's
exact `restart_prompt`. The agent orchestrates this; no atomic host transaction
or installed-app change is implied.

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
`metrics.delivery_bytes` is the actual serialized resume/adopt JSON size, including
metrics, excluding the final newline. Adopt includes its restart envelope and ASCII
escaping. `estimated_delivery_tokens` uses UTF-8 bytes/4 rounded up; successful
delivery stores it as `resume_delivery_tokens` for successor save diagnostics.
`estimated_tokens` estimates the complete package instead. When a hook sample exists,
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
`save --help` describe its draft format.

The AgentsRoom boot prompt carries an allowance: when the host explicitly
requests a handoff summary during agent replacement, the agent may write the
exact `.agentsroom/handoff-summary-<agent-id>.md` path named in that request,
and only that file; all other `.agentsroom/` runtime-file restrictions remain.
The request and write steps are proven in one 2026-10-21 host-initiated
replacement; successor delivery is not. In a 2026-10-20 self-restart test no
summary request or path was delivered and no file was written. On 2026-10-21
the user switched the agent's provider from its pill (Switch Provider ->
relaunch with Agent summary); the host sent the explicit request naming the
exact path, and the predecessor wrote it there, relative to the predecessor's
working directory. The successor's boot prompt named only a transcript file
(`.agentsroom/handoff-transcript-<agent-id>.txt`), not the summary, and the
transcript existed only under the primary checkout's `.agentsroom/`, not the
successor's working directory; the successor found the summary only by reading
the transcript. Do not describe summary delivery as working. The kit cannot
redirect a host request silently: the successor must receive the actual saved
checkpoint path or a host-persisted summary. Write the summary file only when
the host names its exact path. The predecessor records the summary's absolute
path in the saved checkpoint, and the summary names the checkpoint's absolute
path; the ledger (`hooks/handoff_ledger.py save`) stays the source of truth.
A successor looks in its working directory and in `.agentsroom/` under the
ledger's project root (the root the checkpoint was saved under); do not rely
on `git rev-parse`, because the predecessor's working directory need not be a
registered worktree. Keep the existing session/checkpoint available instead of
claiming transfer, and report the unproven delivery as a dependency rather
than stay silent.

Publication receipts: generated checkpoints carry a `.published` sidecar binding
the publication ID to its content fingerprint. Until that receipt is durable,
lookup reports `incomplete` for open work and automatic clearing is blocked. Closed
records stay closed even when edited after publication; inspect them through history.
Their content remains subject to trust checks. Retrying the same
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
agent follow the AgentsRoom restart procedure and call `agents_restart` with
the ready preflight's exact `restart_prompt`; its initial prose avoids native
CLI subcommands such as Codex's `resume`. No typing is needed when all observations
are known and preflight permits dispatch. Typing
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
request. If no authenticated intent matches the current file and the original
request is unavailable, ask before running the emitted `abandon PATH` command. It
needs only that file, closes the record and inherits nothing from it. Hooks and
dispatch emit the same recovery choice; `publication_incomplete` is non-retryable
without a state change. A missing/deleted or truncated checkpoint is not silently rebuilt from
an intent or transfer receipt. Restore verified original bytes/draft explicitly
before repair; preserve evidence and native history meanwhile. Pending restart
intent is not proof of saved context or permission to redispatch; keep host-side
reconciliation with the lifecycle owner.

## AgentsRoom restart

Use after a successful save returns transition kind `agents_restart`, in active
automatic mode or under explicit restart authorization. This is an agent procedure
using the host's MCP tools, not a Python host client. A saved checkpoint alone
does not authorize dispatch. A live request to pause or wait takes precedence.

If successor adoption blocks at `prepare`, report its reason. For
`prepare-target-mismatch`, take the objective and next step from the recorded
`path`; `preparation.body` describes the other checkpoint. Otherwise use the
retrieval-only `preparation.body`, or inspect the recorded `path` if no body is
returned. Never treat a returned preparation as adopted. `prepare-path-alias`
means equivalent canonical paths need spelling reconciliation;
`prepare-target-mismatch` means different work.
Preserve the pending intent and resolve the cause before running the returned
exact `command`. Explicit adoption authorizes only its recorded checkpoint and
does not bypass identity, trust, ownership or holds; ask before switching work.

### Observe before intent

1. Read the current host's restart tool description and restart etiquette.
   `agents_restart` must support restarting the caller into a fresh conversation
   with an initial `prompt`. `capabilities_get` reports installed app/server
   versions; it does not prove that a restart occurred. AgentsRoom 1.222.0
   advertised this contract when this procedure was prepared. Use the runtime's
   fully qualified tool names. Missing/refused capabilities require manual
   continuation; a public support-board status is not a capability observation.
2. Run `python3 <ledger> session --root <project>`. Require an actual hook or host
   session ID and this caller's stable terminal. If `host_session_id` is present,
   it must agree with `session_id`. Reject missing/conflicting identities; a
   generated UUID or environment-only fallback cannot prove native identity for
   this procedure. Keep the observation and its provenance with the checkpoint.
3. Inventory this caller's delegations. `agents_spawned_list(includeClosed:true)`
   returns only owned ephemeral runs retained during the current desktop session.
   Require `ok:true`, the full `runs` array and a consistent count. Read individual
   runs with `agents_spawned_get` as needed. This list excludes saved agents,
   ticket workers and native harness subagents. Combine it with this session's
   delegation records and any still-pending assignments recorded in continuity.
   Use the AgentsRoom mailbox for saved/ticket-worker results. For native harness
   subagents use the harness's supported inventory/results interface. A known
   absence of those delegations is evidence; lack of a tool or a lost delegation
   record is unknown inventory. After desktop replacement, the owned-run list
   cannot establish that older runs did not exist. Resolve those records first.
4. Preserve results before transitioning. Wait for active workers to settle and
   retrieve their complete results; a host `done` flag or summary is insufficient.
   Materialize each needed result locally with its source ID and SHA256, or use
   its already durable report file. Supply every worker once as
   `{id, report:{path,sha256}}`. Include closed runs whose results still matter.
   A verified empty union is `workers:[]`; unknown or partial inventory blocks.
   `needs_input` requires preserving and reporting the human question. Never
   close a worker merely to make a restart eligible. Another agent's runs are
   neither yours to close nor automatically your dependent workers.
5. Check the live request, current session `needsInput`/hold state, checkpoint
   `waiting_input` and `automatic_paused`, plus dependent workers' questions.
   Supply `hold:false` only when each applicable source is known and clear.
   Unknown state blocks. A human hold remains in force until explicitly lifted.

### Persist and dispatch once

Run `python3 <hooks>/restart_preflight.py restart --root <root> --workers '<JSON list>'
--host-capable [--hold]` (add `--path` only when this session owns several open
checkpoints). It reads hook/host identity, derives the request ID below, adds
durable holds, stores the observations and runs preflight in one call. Pass
`--workers '[]'` when the inventory is known and nothing is outstanding; a worker
that finished with no report file is `{"id": "...", "settled": true, "report": null}`
or an in-flight mailbox delegation is
`{"id": "...", "state": "in_flight", "via": "mailbox"}` when its reply persists
in the AgentsRoom inbox for the successor. Other workers lacking a report and
`settled: true` block as `worker-report-not-durable`.
Omitting `--workers` means the inventory is unknown and blocks with
`worker-inventory-unknown`. The lower-level
`preflight --input <observations.json>` remains for callers that build the
object themselves; the rules below apply to both.
A `blocked` result leaves a `.restart-blocked` marker beside the
context-watch latch, and any other result removes it. While this session's
context-pressure checkpoint stays open, context-watch then re-offers the
restart once the human hold has cleared (caller `--hold`: the next user
prompt; durable holds: none applies; checkpoint: `waiting_input` and
`automatic_paused` unset). A mailbox notification is a prompt too: if a
question to the user is still unanswered, keep waiting or pass `--hold`.
A block before preflight removes the marker. For other reasons it reminds every 5 user prompts
(`CONTEXT_WATCH_REMIND_TURNS`) while occupancy stays over the threshold.
Refresh the checkpoint if the state moved, then run `restart` again.
Before creating a request, reconcile existing intents for this terminal in
`.handoffs/restart/` and any request ID retained in continuity. A legacy intent
with a different ID remains binding; a new naming convention never overrides it.
If any earlier dispatch outcome is uncertain, stop and reconcile actual identity
and host history instead of creating a fresh request.

The JSON object contains actual `root`, exact saved `path`, observed `session`
and `terminal`, `request_id`, complete `workers`, `host_capable:true`, and
observed `hold`. Use the deterministic request ID
`agentsroom:` followed by the lowercase SHA256 hex digest of
`terminal + "\0" + checkpoint_id` (UTF-8) for this checkpoint. This remains
75 ASCII characters even for long terminal IDs. Reuse it after an
interruption; never mint a new ID to evade pending/refused state. The preflight
validates publication, authority, ownership, checkpoint holds and report hashes;
the caller remains responsible for observation freshness and completeness.

Only `outcome:ready` with `dispatch:true` permits a host call. Save the input,
returned binding and exact `restart_prompt` durably outside any sealed checkpoint
body. Write the required session status and every result worth retaining before
dispatch. Recheck that no human hold or new delegation appeared while preparing;
if state changed, keep the intent pending and report instead of dispatching.
The kit serializes intent creation with a durable project registry lock as well
as publisher/checkpoint locks. Another pending intent for this terminal or path,
and a retrieved/refused intent for this checkpoint, blocks a new request ID.
Retrieved/refused intents older than 30 days whose checkpoint is no longer open
and authoritative are pruned at the next preflight; pending intents are never
pruned. A retrieved intent keeps `retrieval_receipt` (token SHA-256 only).
Every persisted intent is validated; malformed or noncanonical records block
without replacement. This serializes kit permission, not the external host call.

Call `agents_restart(prompt=<returned restart_prompt>)` as the final tool action,
then end the turn immediately with one short line. The host queues replacement
until the turn ends. Do not substitute the bare `continue the handoff` prompt:
the returned prompt carries exact checkpoint/request identity and orders receipt
recording before resume. A successful tool reply proves acceptance/scheduling
only. Retain the original call/response and timestamp from the native transcript
or supported host history; if unavailable, report that evidence gap explicitly.

A returned refusal/error that explicitly schedules no restart may be persisted
with `restart_preflight.py refusal` using `{root,request_id,reason}`. Keep the
checkpoint open. A lost response, timeout or ambiguous outcome leaves the intent
pending: reconcile actual native identity and host history, never dispatch again
blindly. A definitive refusal remains non-dispatchable under the same ID and
checkpoint, including under a different request name. Once the host explicitly
confirms that no restart was scheduled and the cause is resolved, an authorized
caller may save a new checkpoint and create its new deterministic request.
Ambiguous outcomes are pending, not definitive refusals; a new checkpoint must
not bypass that uncertainty. A pending/refused/retrieved preflight response is not new permission.
For manual recovery, open a fresh session and provide the exact recovery binding;
manual delivery proves recovery separately from automatic host replacement.

New `session`, `terminal` and `request_id` inputs must match
`[A-Za-z0-9._:@-]{1,128}` in full. Native UUIDs, actual AgentsRoom terminal IDs,
legacy safe request IDs and the bounded deterministic ID above fit this format.
Old intents remain readable without retroactively applying that input rule.
The completed prompt must fit the host's 20,000-character limit before pending
intent persistence; identifier or prompt rejection creates no intent. The exact
prompt is retained in the intent for audit via `status`; finding it in status
never grants new dispatch permission.

### Project hook drift

New saves record both project-scope Claude settings files at the observed cwd
(or project root). Preflight compares their bytes, including absent files, before
creating a restart intent. By default (`hook_drift: "warn"`) drift does not stop
the restart: the result carries a `hooks-changed` warning listing each changed
file with its saved and current SHA256. Review the change if it was not expected.
The warning is also stored in the restart intent, so a duplicate preflight and
`status` still show it after the first call.
Set `"hook_drift": "block"` in `~/.context-watch/thresholds.json` or the
project's `.context-watch.json` to block instead; then review the change and
save again to record a new baseline. A user-scope `block` cannot be relaxed by
project config, and an invalid value blocks with `invalid-hook-drift-policy`.
User-scope Claude settings are outside this check. Older checkpoints without a
baseline retain existing behavior. A save-time hash is a drift baseline only.

What is proven about that approval (b7ec7020, Claude Code 2.1.306, 2026-10-21):
a pty capture of launches with AgentsRoom's flags (`--dangerously-skip-permissions`,
`--mcp-config`, `--plugin-dir`) in a scratch git worktree showed no startup prompt.
That held for a new `.claude/settings.local.json`, a touched or rewritten one,
an added hook, a changed hook script, changed plugin hooks, a project `.mcp.json`,
and a hook edit mid-session; a hook change without the bypass flag also showed
none. The only dialog seen was workspace trust, for a plain untrusted directory
without bypass; the scratch worktree of the trusted repository showed no trust
dialog (observed for that worktree only). On Claude Code 2.1.306 there was no
host hook approval to resolve: `hooks-changed` reported drift only, which is
why drift now warns by default (9a3f7656). A host that does prompt for changed
hooks should preserve that approval across same-terminal replacement. The
reported successor-start prompts were not reproduced and their source is
unknown. Untested: older Claude Code versions, AgentsRoom's own UI dialogs,
and Codex's changed-command approval for a Codex successor.


### Inbox continuity

AgentsRoom saves prepend an inbox read to Next steps and carry an open
`agentsroom-inbox` continuity item. Detection uses `AGENTSROOM_AGENT_ID`, or the
current terminal's session file. The fallback says mail status is unknown; it
never invents an unread count. Successors read `agents_read_inbox` before saved
work and handle relevant mail within the live request's scope. Plain CLI saves
are unchanged. Existing checkpoints may predate this marker: AgentsRoom
successors still perform the inbox read before continuing.

### Successor delivery

The restart prompt is one sentence: `Continue the handoff: run exactly python3
<hooks>/restart_preflight.py adopt h-<24 hex>`. Run that command bare, with
no pipe or clipping, and follow its returned action, holds and authorization.
The prefix is `handoff_contract.RESTART_PROMPT_PREFIX`. When a prompt starts with
it, the UserPromptSubmit hook records the successor session in the terminal
pointer before the first tool call (PostToolUse and later prompts refresh it
afterwards). `adopt` reads that identity from its own working-directory root
first: a successor launched at the live root while the intent is bound to a
linked worktree is still found. It falls back to the bound root's pointer only
when that pointer names a session other than the predecessor; the binding root
still supplies ledger and intent. A hook pointer naming another session counts
only if written after `dispatch_started_at`; an older one is reported as
`pointer at <root> predates dispatch`. When no root names a successor, the
refusal says `no successor observation at <roots>; pointer still names the
predecessor`, listing every root checked. Run `adopt` from the directory the
successor was launched in, before any `cd`: a moved cwd hides the launch root
and adopt then refuses rather than guessing.
The handle resolves through `~/.local/state/session-handoff/restart-handles/`
to root and request_id, then the ledger supplies the checkpoint, predecessor and
terminal. This uses the same HOME across predecessor and successor and works from
any cwd with pinned snapshot hooks. `adopt` recomputes the six-field binding's
SHA256 and checks it against the handle, locator and recorded intent (stage
`binding`). Preserve state and report on mismatching locators. If a locator is
missing because HOME changed, recover with `adopt --root <root> --request-id <id>`
using the recorded intent's root and request ID. Legacy full-binding JSON is
accepted for one release. Identity must come from the
hook or host, differ from the predecessor session, sit on the intent's terminal
(read from the environment, never from input) and not conflict with
`host_session_id` (stage `identity`). It then prepares the exact checkpoint
internally, records the delivery receipt (stage `prepare`/`successor`) and resumes,
emitting the body once. Its `restart` object and `report` line give the observed
predecessor -> successor identity apart from host acceptance. Receipt confirmation
alone leaves `host_confirmed:false`. Successor confirmation automatically discovers
the predecessor's Codex completed MCP event or Claude tool-use/result pair.
Explicit import through `restart_preflight.py acceptance` uses JSON input
`{root,request_id,transcript}` and the same validation.
The importer requires the exact saved prompt, predecessor thread, terminal,
post-intent completion time and an `ok:true,scheduled:true` tool response.
It retains the event and SHA256 in the intent, without dispatching or changing
retrieval state. Only that evidence sets `host_confirmed:true`, meaning host
acceptance/scheduling, not a host-attested process replacement. Local transcripts
are auditable evidence, not cryptographic attestations. Native-root confinement,
predecessor filename, regular-file and bounded-read checks apply. Expired pending
intents cannot import. Other harness formats
remain unconfirmed until a supported evidence reader exists; never substitute a
caller boolean. Preserve native successor identity and delivery evidence separately.
A rerun by the same successor replays; another successor is refused.
On identity mismatch, inspect `identity.expected` (predecessor session and
terminal) and `identity.observed` (session, terminal, host session, source).
A valid successor whose recorded workspace is gone gets `workspace-missing`
at stage `workspace` before retrieval. Preserve state; explicitly recover the
body from `workspace.recovery_root` and reconcile paths, or recreate the workspace
from its recorded Git position, then retry. Older checkpoints without cwd
metadata can only check their recorded project root. Host-side cwd fallback
must be reported by AgentsRoom; the kit does not silently choose a directory.

Dispatch given `host.restart_request_id` and no target selects that pending,
unexpired intent's checkpoint on this terminal exactly, even among several open ones.

A pending intent older than 7 days is refused (stage `intent`), also by the
stepwise `successor` command. Dispatch reports it as a stale warning instead of
routing a successor, and it no longer blocks a new checkpoint's restart on this
terminal; save a new checkpoint to restart. A dispatcher
hold returns `outcome:held` (stage `hold`) after the receipt is recorded and before
resume: report it and rerun only after an explicit release. A resume that does not
succeed returns `outcome:blocked` at stage `resume`.
Any `outcome:blocked` preserves the intent and checkpoint. It never authorizes
acknowledgement, another checkpoint or another restart; report its `stage`.
Do not run a generic hook-provided resume before `adopt`: closing the checkpoint
early loses the delivery receipt and leaves the intent pending for
reconciliation. The dispatcher hands a pending successor the same command in its
`--root R --request-id X` form. The stepwise `successor` operation
(`{root,request_id,session,terminal,delivery_receipt}` after a manual prepare)
remains for manual recovery and audits.

### One-call save (v3 slice 1)

Write the session status first, then run:
`python3 <hooks>/handoff_ledger.py dispatch --input -`, with
`{"operation": "save", "draft": {topic, description, body, suggested_skills[, task_id, ...]},
"host": {"restart_capable": <bool>, "workers": [...], "hold": <bool>}}`.
It publishes the checkpoint and, inside AgentsRoom, runs the restart preflight.
Only `outcome: proceed` with `code: restart` permits a host call. In that case,
call `agents_restart` for `agent` with the exact `prompt` as the final tool
action, then end the turn. `suggested_skills` are advisory: an uninstalled one
only warns. Outside AgentsRoom, the result is `code: saved`. `restart_blocked`
and `publication_failed` name a `stage` and authorize no restart. The checkpoint
stays published after a restart block. A retry blocked with `dispatch_pending`
means reconcile, never re-dispatch. A dispatched intent with no host evidence
after 30 minutes is reported as stale (`dispatch_stale`), not auto-adopted.
Recover with a new save or an explicit adopt.

### Handoff v3 outline (proposal; not available yet)

The review contract is [SPEC §7.3d](../../SPEC.md#73d-handoff-v3-proposal--one-call-lifecycle-e3ef793a).
Use the shipped commands above until the approved implementation is installed.
The proposed reference follows these branches:

1. **Save:** extend dispatch's save operation with a draft; one script publishes,
   writes the project pointer before the intent and reserves dispatch durably.
   On AgentsRoom it returns `{call: agents_restart, prompt: <short-handle prompt>}`;
   the agent's only second action is that exact final-tool call, then turn end.
2. **Recovery:** no dispatch marker permits one validated attempt; a marker means
   reconcile, never re-dispatch. Pointer failure leaves no pending intent.
   Uncertain invocation is normal while AgentsRoom awaits turn end.
3. **Startup:** require private HOME-locator provenance, untracked local records,
   pending intent, terminal ownership and user-scope continuation authority;
   several candidates use ask/latest-owned/manual. Settled pointers stay silent;
   retrieved delivery replays only to its recorded successor under receipt rules.
4. **Skills:** AgentsRoom delivers suggested installed skills as adopt context
   with handoff source identity; native activation needs a capability probe.
5. **Pressure:** warning lead and Claude user-scope PreToolUse/Stop mandatory gate;
   supported read/status/recovery remains available; verify Codex gate support
   separately and report mandatory-unenforced only where no gate is available.
6. **Installation:** kit hooks already use user scope. New handlers fast no-op
   outside opted-in projects, isolate failures and load only protected user-owned
   runtime code outside projects. Hook-prompt diagnosis is separate host work.

The handle reuses the existing six-field intent binding. A project pointer is
an index, never continuation authority; project config cannot enable autoresume.
A host bridge is an optional external feature, not a startup prerequisite. Full
schemas, crash rules, config consumers and ordered slices live in the spec.

### Controlled native proof (requires separate authorization)

1. Independently review the isolated patch. Pin a clean runtime snapshot and an
   isolated profile with no production keys/config copied. The host operation
   still replaces a real production agent tab: isolated HOME/runtime does not
   isolate the AgentsRoom desktop, account, tab or native process. Authorization
   must explicitly cover that actual tab and its interruption. Record production
   inventory and preserve an original open recovery checkpoint and its hash.
   Distinguish inherited managed hooks from candidate wiring acceptance.
2. In the native worker, publish a fresh marked checkpoint and required reference
   preserving the real objective, constraints and exact next step. Record actual
   predecessor identity, complete inventory/holds, ready intent and returned
   prompt. Retain original host call/response with timestamp and provenance.
3. Under explicit scoped authorization, dispatch once as the final tool action
   and end the turn. A successor must arrive without manually injecting its
   recovery prompt. Observe its actual new native session on the same terminal.
4. Observe exact preparation and marker delivery, durable successor receipt,
   then resume. Capture failures as pending evidence, not a retry opportunity.
   Validate the pinned snapshot, original recovery status/hash and production
   inventory after. Report inventory changes with unknown causal attribution
   unless independent evidence establishes the cause.
5. Report host acceptance, native identity change, exact delivery and kit receipt
   separately. A manual continuation or same-session canary does not establish
   automatic recovery. Preserve artifacts and stop for independent review;
   managed-wiring acceptance needs its own authorized activation proof.

## Dispatcher

`SKILL.md` routes lifecycle requests through `hooks/handoff_ledger.py dispatch
--input -`, which returns one decision with a single `next_action` and names
the branch to load (`branches/save.md` or `branches/resume.md`). Holds:
`handoff_ledger.py hold --scope project|topic:<t>|terminal:<id> --reason R`
and `release --scope ...`; `release <path>` keeps its legacy meaning. The
request and result fields are in SPEC §7.3c. Without `dispatch`, read a
branch directly.

Linked workers observe the union of main-checkout and local holds; identical
records are deduplicated, and a local hold cannot mask a main hold. Shared Git
metadata and hold-read failures block execution instead of dropping controls.
The write/release root remains explicit: `hold --scope project` in a worktree
creates a local restriction. To control all linked workers, use
`hold --scope project --root <main> --reason R`; after user approval release that
shared hold with `release --scope project --root <main>`. Local release removes
only the local record. Hold results expose origin and name it in release guidance.
Save/retrieve stay available while resume/adoption and restart are held.
