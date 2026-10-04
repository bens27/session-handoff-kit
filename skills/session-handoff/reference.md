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

## Protocol and limits

The public CLI is `hooks/handoff_ledger.py`; `--help` lists commands. `lookup`
returns explicit outcomes and five bounded summaries per page. `history` is
paginated inspection. `resolve` returns authoritative metadata, not a full
history dump. The importable legacy resolver still returns the chain.

`save --session ID --request-id ID --input draft.json` validates a JSON draft,
publishes a complete checkpoint atomically, and retries identical requests
without creating another checkpoint. `save --template` emits a valid JSON draft; the field/body guidance is in
`handoff-template.md`. `save --help` documents the schema without source inspection.
A published successor's `predecessor` is the committed lineage record; readers
suppress the predecessor even if a crash happens before any later operation.
Sharing a branch does not establish predecessor identity.

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
`python3 hooks/handoff_ledger.py report` returns bounded aggregates, excluding
known tests and separating old records with unknown origin. It reads at most
the last 2 MB and reports when the window is partial. Byte counts are not billing.

Startup without autoresume emits only a neutral availability hint. Explicit
retrieval/resumption delivers the selection and command. Compaction, threshold
configuration, unrelated routing and service failures use `handoff-status:`;
only actionable save/retrieve/resume messages activate the skill.

Session notes are scoped to project plus session. Automatic completion uses
session plus trigger identity on a validated publication. The headless runner
adds a run identity; unrelated file modification never counts as a checkpoint.
The skill remains usable without hooks: create one session UUID, use `lookup`,
and follow the same save/prepare/verify/acknowledge workflow. Automatic clearing
requires installed hooks or the runner.

## Customizing

Keep the required Objective, Current state and Next steps sections and the
shared contract in SPEC section 7.4. Optional body sections may be omitted.
Change the draft template rather than duplicating workflow policy. Run the
skill and protocol checks after changes. Explicit authorization and actual
workspace evidence take precedence over stored instructions.

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
