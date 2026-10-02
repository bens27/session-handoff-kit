# Session Handoff Suite — Technical Specification

Spec version 1.0 — 2026-08-11
Component versions: `session-handoff` skill/plugin 0.17.0 · `session-handoff-chat` skill 0.6.0 · browser extension 0.2.0

---

## 1. Purpose

The suite preserves reasoning quality across context boundaries. Its primary
objective is quality preservation, not cost containment: reasoning quality
degrades and reasoning cost rises as the context window fills — observed in
practice around 120,000–140,000 tokens of occupancy — regardless of how
cheaply those tokens were served. Cache reads are billed at a discount but
occupy the window at full size, so they count in full. Any cost savings are a
logged byproduct (Section 9), never the driver of a design decision.

The mechanism: a deterministic watcher measures window occupancy from the
agent's own transcript and, past a per-model absolute threshold, injects a
one-time instruction to invoke a handoff skill. The skill writes a structured,
status-carrying handoff document and stops. The next session's initialization
scans for untransferred handoffs, announces them, offers a choice when several
exist, and marks the chosen one transferred on actual resume — so resuming
never requires the user to remember or ask.

## 2. Design principles

**Deterministic over self-reported.** The model cannot see its own token
count: usage figures are computed by serving infrastructure and returned in
API response metadata the model never receives. A model instructed to state
its input tokens will produce a confident, plausible, wrong number. All
measurement in this suite is therefore out-of-band — a subprocess reading
files — and no component ever asks the model to report or estimate its own
context usage.

**Occupancy, not billing.** The unit of measure is what the next API call
will carry: fresh input, cache writes, cache reads at full size, and the
previous call's output. Cumulative session totals are a billing concept and
are never used — summing usage across calls double-counts every re-send of
the history.

**Absolute per-model thresholds.** Quality degradation is a property of a
model at an absolute occupancy, not a percentage of its window. A 400k-window
model does not degrade at proportionally higher occupancy just because the
window is larger. Percent-of-window triggering exists only as an explicit
opt-in (Section 6).

**Inform, don't hijack.** Session initialization announces open handoffs and
offers a choice; it does not seize the session. If the user opens with an
unrelated explicit task, open handoffs get one sentence and the user's task
proceeds. Auto-resume without asking is available but off by default.

**Fail open.** The watcher and announcer exit 0 on any internal error. A
broken hook must never block a session. Memory- and search-based channels in
chat degrade honestly: the skill never claims a handoff was saved to a
channel that was not available.

**Fire once, remind once.** The threshold trigger fires at most once per
session, enforced by a latch file keyed on session id. One SECOND NOTICE
fires if the first was not acted on: only after the model has taken a turn
since the first (parallel tool results arriving together do not count), and
once occupancy is a further 25% of the threshold past the first and at least
1.25x the threshold, capped at 5,000 tokens under the window so it stays
reachable. Both latches re-arm when occupancy falls below half the threshold
(after a compaction or a clear). A floored session (threshold under its
startup context) is watched against window minus reserve instead and re-arms
below half of that, or once occupancy is back within 10,000 tokens of its
startup floor.

## 3. Architecture

Every deterministic deployment assembles the same three primitives:

1. A **token feed** the host writes as a side effect of operating — the
   Claude Code session transcript or the Codex rollout file. Reading it costs
   zero tokens.
2. A **lifecycle hook** that runs a subprocess at the right moments —
   `PostToolUse` and `UserPromptSubmit` for the threshold watch,
   `SessionStart` for the announcer.
3. An **instruction-injection channel** back into the model's context. Hooks
   cannot invoke tools or skills directly; the hook injects an imperative and
   the model performs the skill invocation.

Where hooks do not exist (chat, Cowork native tasks, Warp Agent Mode), the
skill layer and the file/memory ledger still operate, triggered
conversationally or by standing convention; file-ledger fallback checks run
`handoff_ledger.py list --json` rather than manually inspecting files.

### Components

| Component | Role |
| --- | --- |
| `hooks/context_watch.py` | Watcher (threshold trigger) + announcer (session-start handoff scan) + analytics logger + `stats` CLI. Stdlib Python, single file, used by both Claude Code and Codex from the skill folder. |
| `hooks/handoff_ledger.py` | State and chain tracking over handoff files. Importable module + CLI (`list`, `resolve`, `claim`, `resume`, `supersede`, `abandon`, `save-path`, `new-path`). |
| `skills/session-handoff/SKILL.md` | Agent-surface skill: writes the handoff, handles resume, single- and multi-handoff flows. The folder also holds `hooks/` and `install.py`. |
| `chat/session-handoff-chat/SKILL.md` | Chat-surface skill: memory ledger, past-chat marker, file fallback, resume resolution. |
| `chrome-extension/` | MV3 extension for Chrome/Edge: pre-populates new-chat initialization prompts on claude.ai. |
| Packaging | `.claude-plugin/marketplace.json` (kit root), plugin manifest, `.plugin` (Cowork), `.skill` (chat), `skills/session-handoff/install.py` (hook registration for Claude Code `settings.json` and Codex `hooks.json`). |

## 4. Measurement specification

### 4.1 Claude Code / Cowork

Source: the session transcript JSONL (path supplied in every hook's stdin
payload). The watcher reads only the final 2,000,000 bytes of the file,
discards the partial first line, and parses line-wise, skipping unparseable
lines. From the parsed tail it takes the **last main-chain entry** carrying
`message.usage` — entries with `isSidechain: true` (subagents, which occupy
their own windows) are excluded.

```
occupancy = input_tokens
          + cache_creation_input_tokens
          + cache_read_input_tokens
          + output_tokens
```

The first three terms are the input-only formula Claude Code itself uses for
its used-percentage figure; `output_tokens` is added because the next call's
input includes the previous output. Model id is read from the same entry's
`message.model`.

### 4.2 Codex

Source: the session rollout JSONL. The watcher takes the **last
`token_count` event** (`payload.type == "token_count"`) and reads
`payload.info.last_token_usage`:

```
occupancy = total_tokens          (fallback: input_tokens + output_tokens)
```

`total_tokens` includes input (with cached as a subset), output, and
reasoning tokens. The context window is read from
`payload.info.model_context_window` when present, overriding
`CONTEXT_WATCH_WINDOW`. Model id comes from the hook stdin `model` field,
falling back to the last `turn_context` event's `payload.model`. Cumulative
totals in the rollout are never used.

### 4.3 Pending-content estimate (lag closure)

The last usage entry lags the hook: on Codex by one call, on Claude by
one call plus every tool result of the current assistant turn (see below),
and on both by the response that issued the call, thinking included: that
message and its usage are written only after the hook returns (measured on
Claude Code; the statusline refreshes ~300 ms after PostToolUse too, so no
earlier source exists). The first missing piece is what the hook is already
holding on stdin: the just-produced tool
result (`PostToolUse`) or the new prompt (`UserPromptSubmit`). The watcher
estimates its weight at ~3 characters per token (measured ~3.2 on
line-numbered code; 4 undershot by ~30%) from the first present key
among `tool_response`, `tool_output`, `tool_result`, `prompt`, and adds it to
occupancy before the comparison. The estimate deliberately biases the trigger
early — the correct direction for a quality guard. Claude Code writes an
assistant message and its usage only after that call's PostToolUse hook
runs, so on Claude the estimate also adds tool results already in the
transcript after the last usage entry (each capped, the current call's
excluded), plus a thinking allowance for the not-yet-written current
response: the session's largest `output_tokens` so far, capped at 25k
(`CONTEXT_WATCH_THINKING` overrides it; a single 36k-token thinking block was
observed moving occupancy 52k → 105k in one call). Disable everything with
`CONTEXT_WATCH_PENDING=0`. Exact pre-flight counting via a token-counting API
is rejected by design: a hook cannot reconstruct the request payload, and the
early-biased estimate achieves the same protection with no network call.

Media is priced per item, not by length: each image or audio item counts a
flat 1,600 tokens. Recognized forms are `data:image|audio/...;base64,` URLs
inside strings, values under image/audio URL keys, MCP/Claude envelopes
(`{type: image|audio, data, mimeType}` or `source: {type: base64, data}`),
OpenAI `input_audio: {data}`, and a bare string that really looks like
base64. That means it has no spaces, uses `+/` or `-_`, and mixes upper
case, lower case and digits. A long word such as `'T'*520000` is text, not
media. A string that holds JSON is decoded before counting. Keys that are
hook metadata and never enter context (an Edit's full original file) are
skipped. The whole pending estimate is capped at 25,000 tokens.

### 4.4 Cross-agent semantics

The two measures are not identical: Claude Code counts input-side plus last
output; Codex counts total including reasoning. A shared absolute number
therefore means slightly different things per agent. Per-model threshold
entries (Section 6) are the normalization mechanism — tune each model's entry
against its own agent's measure.

### 4.5 Retrieval cost

Zero tokens. Every check is local file I/O in a subprocess; nothing enters
model context and nothing is billed. Compute cost is milliseconds and bounded
by the 2 MB tail read regardless of session length. The suite's entire token
expenditure is its speech: the injected trigger instruction (~80 tokens, once
per session) and the session-start announcement (zero when nothing is open,
roughly a line per open handoff). On Codex's `PostToolUse` channel the
message *replaces* a tool result, making that path token-neutral or better.

## 5. Trigger and injection

### 5.1 Watch events and latch

The watcher runs on `PostToolUse` (covers long agentic turns, where context
actually burns) and `UserPromptSubmit` (per-turn check). On first threshold
crossing it writes a latch file
(`$TMPDIR/context-watch-<key>.fired`, containing `occupancy/limit/base`,
where `base` is the transcript's own usage at that moment) and emits. State
files in the shared temp dir are opened without following symlinks (temp
files exclusively), and the auto-clear counter is keyed by
sha1(realpath(project root))[:20]. The key is the sanitized `session_id`. When that is missing, the key
is `t-` + sha1(transcript_path)[:16], so sessions without an id never share
a latch. Nothing is latched when neither is known.

One SECOND NOTICE follows if the first is not acted on. It needs a model turn
after the first notice, and occupancy at or above
max(1.25 × threshold, first occupancy + 0.25 × threshold), capped at 5,000
tokens under the window so it stays reachable. It stays quiet
if a validated checkpoint was published for this session's trigger (any
status: it was acted on, whether or not it has been resumed since). After that, checks
exit silently until occupancy falls below half the threshold, which means a
compaction happened. That re-arms both notices. For a floored session
(threshold under the startup context) the half is taken of the effective
limit, window minus reserve, and occupancy within 10,000 tokens of the
recorded startup floor also re-arms. Usage recorded before a
`compact_boundary` transcript entry is ignored. On `SessionStart` with
source `compact` the announcer adds a one-line note: context was just
compacted, so check the work against the handoff or the files instead of
relying on memory.

### 5.2 Injection channels

| Agent | Event | Channel |
| --- | --- | --- |
| Claude Code / Cowork | any watch event, `warn` mode | JSON `hookSpecificOutput.additionalContext`, exit 0 |
| Claude Code / Cowork | `PostToolUse`, `block` mode | JSON `{"decision": "block", "reason": …}`, exit 0 |
| Codex | `UserPromptSubmit` | plain stdout, exit 0 (becomes turn context) |
| Codex | `PostToolUse`, `warn` mode | JSON `hookSpecificOutput.additionalContext`, exit 0 — added as developer context; the tool result is kept |
| Codex | `PostToolUse`, `block` mode | JSON `{"decision": "block", "reason": …}`, exit 0 — replaces that one tool result |

The injected message is worded as factual statements, not system commands
(hosts may treat command-framed injected text as prompt injection). It names
the occupancy, the threshold and which rule selected it, the model, the
cache-read share, any pending estimate, the skill that applies, what it
implies (finish only the action in progress, write the handoff, stop; the
remaining work stays authorized for the resuming session), and the absolute
path of `handoff_ledger.py`, so the writer need not search for it. When
autoresume is active (§7.3), the message additionally instructs the agent
to tell the user to type `/clear` after the handoff is written — the
cleared session's announcer then resumes the handoff automatically,
closing the loop with a single user keystroke. When the newest usage entry
is more than 600 s older than the event, the message says the telemetry may
be stale.

Floor check: when the whole transcript fits the scanned tail, the watcher
takes the occupancy of the first usage entry as the session's startup floor.
If the threshold is below `floor + max(10,000, 10% of the threshold)`, a
handoff would free nothing, so instead of the notice it sends one factual
note naming the floor and a suggested minimum threshold, occupies both
latches (no handoff notice this session; a compaction re-arms) and logs
`notice: "floor"` with `floor`.

Codex contract note: the `PostToolUse` JSON channel is verified against the
Codex hooks documentation (additionalContext is added as developer context;
`decision: block` or exit 2 replaces the tool result; plain stdout is
ignored), not in a live trusted Codex session, because headless Codex will
not run untrusted hooks.

## 6. Threshold resolution

First match wins; model ids are matched by longest case-insensitive
substring key ("claude-opus" beats "claude"):

1. `HANDOFF_AT` — env, global absolute; the ergonomic per-launch knob
   (`HANDOFF_AT=20000 claude`), so it beats every map and config file
2. `CONTEXT_WATCH_TOKENS_MAP` — env, e.g. `opus=120000,sonnet=140000,gpt-5.5=160000`
   (model keys only; a `default=` entry is ignored, use `CONTEXT_WATCH_TOKENS`
   or the config files' `"default"` key)
3. `./.context-watch.json` — project-local per-model config
4. `~/.context-watch/thresholds.json` — user-global per-model config
5. `CONTEXT_WATCH_TOKENS` — global absolute, env
6. `"default"` key in the config files
7. `CONTEXT_WATCH_PERCENT` × window — only when PERCENT is explicitly set
8. Built-in default: **130,000 tokens**

When the host reports the model's context window (Codex does), the
resolved threshold is then capped at window − `CONTEXT_WATCH_RESERVE`
(default 20,000), which leaves room to write the handoff. The resolution
source records the cap. An assumed window is never used for this cap. An
assumed 200k would clamp every 1M-context model to 180k.

The config files also carry `"auto"`, `"at"` and `"auto_max"` (same meaning as
`HANDOFF_AUTO`, `HANDOFF_AT`, `HANDOFF_AUTO_MAX`). Precedence: non-empty env,
then project file, then user file, then default. Wrongly typed values are
ignored; these keys are never read as model thresholds.

Config files are flat JSON; user-global loads first and project-local
overrides on key collision:

```json
{ "claude-opus": 120000, "claude-sonnet": 140000, "gpt-5.5": 160000, "default": 130000 }
```

The active model is re-detected on every check, so a mid-session model
switch re-resolves the threshold on the next tool call. Every trigger records
its resolution source (`TOKENS_MAP:claude-opus`, `config:default`,
`builtin-default`) in both the injected message and the analytics record.
Sizing guidance: set each entry where that model's observed reasoning quality
drops; leave headroom below auto-compaction (Codex defaults to ~80% of the
window; raise `model_auto_compact_token_limit` if a threshold approaches it,
or disable Claude Code auto-compact in `/config`).

## 7. Handoff documents and ledger

### 7.1 File format (agent surfaces)

New handoffs are published by `save` to a dated, uniquely suffixed Markdown file
in `.handoffs/` or the per-project fallback. The command generates metadata
from one timestamp and binds retries to session/request identity. Predecessor
replacement is explicit. Legacy dated/undated files and HANDOFF.md remain
readable. Required execution skills and references are optional metadata;
preparation loads them only in the applicable phase and within a total budget.
A stored document resembles:

```markdown
---
topic: auth-refactor
created: 2026-08-11T14:30
status: open
reason: context-pressure
description: JWT refresh rotation half-built; middleware done, tests failing on expiry edge.
skills: tdd, diagnosing-bugs
references: docs/auth-notes.md, tests/auth_refresh_test.py
---
# Session Handoff — auth-refactor — 2026-08-11
```

`reason:` records why the handoff was written (`context-pressure` from the
matching session trigger, or `user-parked`). Live user authorization wins.

Body sections, in order: Objective; User request and constraints (the ask
verbatim, constraints, approval scope, pending decisions); Current state,
split into verified-with-evidence and unverified/unknown; Decisions and rationale;
Files touched; In flight; Next steps (ordered, concrete, with paths and
commands); Gotchas (including approaches tried and abandoned). Target under
1,500 words, facts a fresh session can verify, no conversational narration.
The body outline is a default, not a contract: the skill is organized into
independently customizable sections (naming convention, document structure,
post-resume actions), and Objective, Current state and Next steps are required for validated publication. The outline and its rules live in `handoff-template.md`, a file
bundled beside each `SKILL.md` rather than inlined in it, so the shape of a
handoff can be rewritten without touching trigger, ledger, or resume policy;
`SKILL.md` wind-down delegates to it.
If a `LESSONS.md` exists (e.g. maintained by a mistake-learning skill), new
lessons append there and Gotchas references it rather than duplicating.
Before reporting completion the skill requires `outcome: saved` from the
validated publication command. A failure preserves the draft and current session.

### 7.2 Deterministic checkpoint protocol

`handoff_ledger.py lookup [dir]` returns `none`, `available`, `stale`, `claimed`,
or `error`, each with a concrete action. It chooses authoritative topic state
before filtering availability, scans local and fallback directories plus legacy
HANDOFF.md, and reports read failures rather than turning them into absence.
The latest record uses created time and a stable path tie-breaker. Five summaries
are returned per page; `--offset` paginates. Descriptions are bounded. Malformed metadata never crashes
scan: a missing `created` falls back to the filename date, then mtime, and a
non-object `.published` receipt makes the checkpoint `incomplete`.

`save [dir] [--session ID] [--request-id ID] --input draft.json` validates the
agent-authored core and generates timestamps, project/Git state, session/trigger
identity and publication identity. Identical request retries return the same
checkpoint; changed content under the same ID is refused. Temporary-file fsync
and exclusive atomic publication prevent partial Markdown files. Directory
failures try the other supported location, then return `blocked` with recovery
instructions. Neither CLI nor skill bypasses host permission denial.

Lineage is explicit through `predecessor`; branch equality never supersedes work. A resumed predecessor
can be continued only by the session that resumed it (others get `conflict`), and a
successor never rewrites a `resumed` record to `superseded`.
The published successor is the committed replacement record, so a crash after
publication cannot resurrect its predecessor. All state changes check owner
under a sidecar lock. Omitted owners do not bypass another session's claim.

`prepare TOPIC-OR-PATH --session ID` is read-only retrieval. With `--execute`
it claims a validated, bounded package and records a recoverable preparation
receipt. Claims expire after two hours or can be released explicitly. Checkpoint
and dependency fingerprints bind preparation to the material actually delivered.
`verify PATH --session ID` preserves pipeline failure, stores complete output in
a log, and returns a bounded excerpt. A `verify_baseline` match is accepted only when pytest printed its
summary line and no `ERROR` lines; otherwise the failure is unknown and stays `verification-failed`. `acknowledge PATH --session ID` transfers
only after preparation and any recorded verification pass. Failure/expiry/change
requires retry or release; it never silently marks work resumed.
`resume [TOPIC-OR-PATH] --session ID` chains selection, `prepare --execute`
(auto skill catalog), verify when recorded and acknowledge, returning
`resumed`, `choose`, `none`, or the failing `stage`. The final output is size-checked
against `--budget-bytes` before `acknowledge`; over budget returns `needs-context`
and the checkpoint stays open; the CLI never replaces an acknowledged `resumed`
result with a budget error. `--session` defaults to
`$HANDOFF_SESSION_ID`, then the hook's per-terminal pointer (24 h); save's
request ID defaults to a hash of the draft. Save resolves the project from the
predecessor (an explicit root that disagrees is a `conflict` naming both),
treats this session's or terminal's own authoritative checkpoint as the default
predecessor, defaults `verify` to the user-level config `"verify"` then the trusted predecessor's,
accepts `--topic/--description/--body/--attach`, and returns `transition`
(`new-session`, `clear`, `tmux`, `runner`, `agents_restart`) with its action.

Trust. A checkpoint is trusted only when this machine's own `save` published it:
the `.published` receipt carries an HMAC-SHA256 of the checkpoint id and content
fingerprint under a per-user key (`~/.context-watch/receipt.key`, mode 0600,
created at first save, never inside a repo). A cloned repo cannot read that key,
so a committed or hand-written checkpoint, a plain sha256 receipt, a pre-HMAC
receipt, or any edit to a saved file is untrusted. Untrusted checkpoints remain
retrievable as text, but `resume` returns `needs-confirmation` (naming the
`verify` command, claiming nothing) instead of running it, and only `verify` or
`resume` with `--confirm-verify`, after the user approves, executes it.
Their required references are confined to the project root; others are listed
in `withheld_references`, not inlined. Trusted checkpoints keep absolute
`--attach` references. Save defaults `verify` from `~/.context-watch/thresholds.json`
only (never the repo's `.context-watch.json`) and inherits a predecessor's `verify`
only when the predecessor is trusted, so a successor cannot launder a command.

States remain open, resumed, superseded and abandoned; claimed is a temporary
lease on open work. Legacy `claim`, `resume`, `supersede`, `abandon`, `release`,
`new-path`, `save-path` and `list` remain available. Legacy direct writes do not
produce automatic checkpoint receipts. `new-path` no longer inherits unrelated
session facts, skill history, or same-branch predecessors. It rejects a topic that is not
kebab-case, as `save` does. `prepare` on a topic whose newest checkpoint is an
unpublished (interrupted) save returns `incomplete`, not `closed`. CLI `resolve` is compact;
`history --topic TOPIC --offset N --limit N` is explicit paginated metadata.
The importable `resolve` API retains full-chain compatibility.

### 7.3 Startup and opening requests

Startup emits a compact next action, including when no handoff exists. Native
resume/fork continues existing context; compaction emits its existing evidence
revalidation notice. Retrieval requests run read-only preparation. Explicit
resume authorizes execution preparation. Multiple choices include none;
AUTORESUME only selects a single candidate, while HANDOFF_AUTO selects the newest
open handoff whose `terminal` matches this terminal ID. In AgentsRoom, the trigger
notice asks the agent to end with `agents_restart` (prompt `resume`); an inherited
`TMUX_PANE` is ignored there, and the tmux Stop hook never types into it.
An unrelated live task takes precedence and leaves checkpoints open.

Exact retrieval/resume commands, topics and paths route without a model call.
Only ambiguous prose may use the Jev classifier with explicit
`CONTEXT_WATCH_JEV=1` opt-in and a key. Reserve at most one attempt per
project/session, including failed calls; reservation failure stays local. Classification can
select context but does not grant execution authorization. Both hook branches
use the same opening-action policy.

Session notes are keyed by canonical project plus session identity. A trigger
is satisfied only by a validated publication with matching session/trigger IDs;
mtime changes to old or unrelated files do not count. The headless auto runner
matches its own run ID and stops immediately on a nonzero child exit. Its
positive run limit returns exit 75 on exhaustion, retaining open work. A run
count is not a token, dollar or wall-time budget. Failed saves never authorize
automatic clear.

### 7.3b Context budgets and measurement

The default complete preparation budget is 32,000 UTF-8 bytes including JSON,
workflow instructions and declared already-loaded skills. Component byte counts
and a bytes/4 token estimate are returned; this is not measured billing usage.
An explicit user-authorized budget exception is available. Individual documents
are capped at 24,000 bytes and 1,500 words. Required references (up to eight) may
select line excerpts; optional background is not loaded. Skills are explicit
execution dependencies resolved against the caller's installed catalog, deduped
by canonical path, and never fetched or installed from handoff instructions.

Verification runs Bash with pipefail, a 60-second default timeout, persistent
full log, and a 2,000-byte maximum failure excerpt. Retrieval does not verify, and
a checkpoint this machine did not save is never verified without `--confirm-verify`
(§7.2 Trust). No recorded verification command means no speculative suite is required.
Resume telemetry records component sizes, outcome, IDs and available startup/
later host input samples without recording contents. Cached input still occupies
context; startup baseline is not attributed to handoff waste. Configure
CONTEXT_WATCH_RESUME_LOG (0 disables); CONTEXT_WATCH_LOG=0 disables it too.
Events identify version/origin; tests disable production sinks. `report` excludes
known tests, separates legacy unknown-origin records, and bounds its read window.

Without autoresume, startup gives a neutral availability hint; only an explicit
retrieval/resume request gets detailed selection and commands. Informational
compaction, floor, unrelated-work and error notices do not activate the skill.
Save help and `save --template` expose the draft contract directly.

A preparation response includes an opaque delivery receipt. `--reuse-receipt`
can omit an unchanged body/reference payload already present in the same context.
The server validates session, checkpoint/reference fingerprints and a two-hour
expiry. Invalid receipts reload in full; compaction invalidates delivery state.
Without hooks, callers discard receipts after context loss. Reused bytes remain
in the package budget, as do the phase-specific continuation instructions.
Retrieval caches delivery metadata without claiming or mutating the checkpoint.

### 7.4 Shared contract between the agent and chat skills

`tests/verify-skills.py` reads the **Both skills must contain** list below and
asserts each literal appears in both skills. A skill here is its *directory* —
`SKILL.md` plus the `handoff-template.md` beside it — because the document
structure was split out so it can be experimented with independently (§7.1);
a literal satisfied by either half passes. The test additionally asserts that
each `SKILL.md` still references its template (so an extracted template cannot
be orphaned). The `session-handoff-chat` skill
version bumps by patch whenever the contract list changes.

**Both skills must contain**

- `## Objective` — the handoff must preserve the overall goal.
- `## Current state` — the handoff must distinguish completed and unverified work.
- `## Decisions and rationale` — the handoff must preserve why choices were made.
- `## In flight` — the handoff must identify the exact interrupted work.
- `## Next steps` — the handoff must give ordered concrete continuation actions.
- `## Gotchas` — the handoff must capture hazards and rework-prevention notes.
- `status: open` — persisted handoffs must start in the transferable open state.
- `description` — persisted handoffs need a one-line announcement summary.
- `1,500 words` — handoffs should stay dense enough for reliable resumption.
- `tried and abandoned` — handoffs must record discarded approaches as well as successes.
- `LESSONS.md` — projects with a lesson log should reference it from Gotchas.
- `none` — multiple-open-handoff prompts must offer a no-selection option.
- `one sentence` — unrelated opening requests defer open work without derailing the task.
- `Do not mark` — listing or announcing a handoff must not count as resuming it.

**Intentional divergences**

- Agent handoffs use `## Files touched`; chat handoffs use `## Artifacts produced`.
- Agent handoffs use front-matter files and the filesystem ledger; chat handoffs use a memory ledger.
- The literal `SESSION HANDOFF — <topic> — <date>` header line exists for chat past-chat search only.
- The `[context-watch]` trigger is agent-only because chat has no lifecycle hook or token feed.
- The `resolve` and `must_also_read` chain is agent-only; chat has no equivalent chain/reference command today.

## 8. Chat surface

Chat has no hooks and no token feed; the trigger is conversational and the
persistence substrate is memory plus the conversation itself.

**Memory ledger.** All chat handoffs live in one memory file (suggested name
`open-handoffs`). Each handoff is a section headed
`## <topic-slug> — Status: open — <date>`; resumed entries flip to
`Status: resumed <date>` and remain until the user clears them. The file's
one-line description is the announcement channel: it must always enumerate
the open topics ("Open handoffs awaiting resume: pantry-import, kit-v2 …")
because memory descriptions surface at the start of every new conversation.
An optional user-preference line ("at the start of each conversation, if my
open-handoffs file lists open handoffs, mention them in one line before
answering") upgrades the passive listing to an unconditional first-reply
announcement — the chat equivalent of the SessionStart hook.

**Persistence order on handoff.** (1) memory ledger upsert + description
update; (2) always print the handoff block in-conversation, headed by the
literal line `SESSION HANDOFF — <topic-slug> — <date>` so past-chat search
can find it; (3) downloadable `HANDOFF.md` as the portable cross-surface
copy (saved into a project folder as `.handoffs/<YYYYMMDD-HHMM>-<topic>.md`
with `status: open`, the plugin's announcer counts it); (4) a connected
note-capture tool, if any. The skill never claims a save to an unavailable
channel.

**Resume resolution order.** Attached/pasted content → memory ledger →
past-chat search for `SESSION HANDOFF <topic>` → ask for the file. Multiple
open with no topic named: list one line each with topic, stored date, and
description, then ask. After actual resume: flip the entry's status and
remove the topic from the description. This is
coarser than the agent-surface bounded `prepare` package and explicit paginated
`history`; the chat
surface has no equivalent chain/reference concept today.

**Settings caveats.** Past-chat retrieval requires the "Search and reference
past chats" setting; chats inside a Project search only that Project. Memory
and files cross that boundary.

**Honesty rules.** No invented context-usage numbers; no automatic threshold
claims in chat.

## 9. Analytics

Each trigger appends one JSON line to `~/.context-watch/events.jsonl`
(override path via `CONTEXT_WATCH_LOG`; `0` disables):

```json
{"ts": "2026-08-10T23:44:27", "agent": "codex", "model": "gpt-5.5",
 "occupancy": 160000, "pending_estimate": 0, "threshold": 150000,
 "threshold_source": "TOKENS_MAP:gpt-5.5",
 "breakdown": {"input": 155000, "cache_read": 120000, "output": 4000,
               "reasoning": 1000, "occupancy": 160000},
 "window": 272000, "window_source": "reported", "usage_age_s": 4,
 "session_id": "c1", "cwd": "/repo"}
```

`context_watch.py stats` summarizes: trigger count, per-model average
trigger occupancy, per-model cache-read share of occupancy, last event. The
cache-read share is the quality-relevant figure: how much of the window was
"cheap" context still doing full-weight damage.

## 10. Browser extension (chat initialization)

MV3, identical in Chrome and Edge (load unpacked). Scope:
`https://claude.ai/*`; permissions: `storage` only.

**Behavior.** A content script polls the URL (~400 ms) to catch SPA soft
navigations. On a new-chat page (`/` or `/new`) — or any page carrying the
`#handoff-check` hash, which the toolbar button appends when it opens
`claude.ai/new#handoff-check` — it waits for the composer (MutationObserver,
8 s timeout), and injects the template only into an **empty** composer, once
per new-chat visit, never on existing chats. Insertion uses
`execCommand("insertText")` (fires the app's input handlers) with a
textContent + InputEvent fallback. With auto-send enabled, it clicks the send
button 400 ms later.

**Settings** (options page, `chrome.storage.sync`): template text (default
asks Claude to check the open-handoffs ledger, list open items one line each
with stored dates and ask which to resume, or reply only "No open handoffs");
the extension injects a real computed timestamp into that default prompt
before insertion; always-on vs button-only; auto-send (default **off** —
pre-fill preserves the human veto
and stays on the typing-assistance side of automating the site; on is the
zero-keystroke mode).

**Failure posture.** Selectors (`findComposer`, `findSendButton`) are
defensive and fail silently; a claude.ai DOM change disables injection
rather than corrupting input.

**Zero-install alternatives.** The desktop app's supported deep link
`claude://claude.ai/new?q=<encoded prompt>` prefills for review (bindable to
an OS hotkey). The web `?q=` parameter has come and gone historically — test
before relying on it; the extension does not depend on it.

**Optional estimator mode (specified, not shipped).** The browser
transiently holds the conversation JSON, files, project knowledge, and the
SSE stream; internal payloads expose exact quota data but not exact context
occupancy, so a chat-side gauge is an estimator: page-world `fetch`/XHR
wrapper (MV3 `webRequest` cannot read response bodies), local tokenization
(~4 chars/token; no public Claude tokenizer), producing a **floor** estimate
blind to server-side assembly (system scaffolding, web search results,
Research, memory injections). Undercount biases late, so estimator
thresholds must be compensated downward — roughly 90–110k estimated for a
120–140k true band. All data stays local and in memory.

## 11. Environment variable reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `HANDOFF_AT` | — | Global absolute threshold; highest precedence, meant for per-launch use (`HANDOFF_AT=20000 claude`) |
| `AUTORESUME` | — | `1` = resume a single open handoff at start without asking; with `HANDOFF_AT` forms the one-keystroke `/clear` cycle (§7.3) |
| `CONTEXT_WATCH_TOKENS_MAP` | — | Per-model absolute thresholds, `key=tokens` comma list |
| `CONTEXT_WATCH_TOKENS` | — | Global absolute threshold |
| `CONTEXT_WATCH_PERCENT` | unset | Percent-of-window trigger, only when explicitly set |
| `CONTEXT_WATCH_WINDOW` | `200000` | Window for the PERCENT path; Codex reports its own |
| `CONTEXT_WATCH_SKILL` | `session-handoff` | Skill named in the injected instruction |
| `CONTEXT_WATCH_MODE` | `warn` | `warn` = additionalContext; `block` = blocking channel on PostToolUse |
| `CONTEXT_WATCH_AGENT` | auto | Force `claude` or `codex` |
| `CONTEXT_WATCH_RESERVE` | `20000` | Tokens kept free below a host-reported window; the threshold is capped at window − reserve (§6) |
| `CONTEXT_WATCH_PENDING` | on | `0` disables the pending-content estimate |
| `CONTEXT_WATCH_LOG` | `~/.context-watch/events.jsonl` | Analytics path; `0` disables |
| `CONTEXT_WATCH_DISABLE` | — | `1` = no-op without uninstalling |
| `CONTEXT_WATCH_MAX_AGE_DAYS` | `14` | Announcer ignores older open handoffs |
| `CONTEXT_WATCH_AUTORESUME` | — | Legacy alias for `AUTORESUME` |
| `HANDOFF_AUTO` | — | `1` = fully automatic: implies `AUTORESUME`, the newest open handoff is resumed even when several are open, and the session is cleared for you (tmux Stop hook, or the `context_watch.py auto` runner) |
| `HANDOFF_TERMINAL_ID` | `AGENTSROOM_AGENT_ID`; `claude-auto` generates `tmux-<uuid>` | Terminal identity recorded on each save; in fully automatic mode the cleared session resumes only that terminal's newest open handoff (legacy handoffs without a terminal are not auto-picked) |
| `HANDOFF_AUTO_MAX` | `10` | Fully automatic mode: automatic clears in a row per project before the Stop hook stops and tells you (a session that starts near the threshold would otherwise loop); a turn that ends without the trigger resets the count |

## 12. Surface compatibility

| Feature | Claude Code | Codex | Cowork | Chat | Chat + extension |
| --- | --- | --- | --- | --- | --- |
| Token-threshold trigger | ✅ | ✅ ¹ | ⚠️ ² | ❌ | ❌ (⚠️ with estimator mode) |
| Per-model absolute thresholds | ✅ | ✅ ¹ | ⚠️ ² | ❌ | ❌ |
| Lag closure (output + pending) | ✅ | ✅ ¹ | ⚠️ ² | ❌ | ❌ |
| Trigger analytics | ✅ | ✅ ¹ | ⚠️ ² | ❌ | ❌ |
| Structured handoff skill | ✅ | ✅ | ✅ | ✅ | ✅ |
| Open/resumed ledger state | ✅ | ✅ | ✅ | ✅ ³ | ✅ ³ |
| Session-start announcement | ✅ | ✅ ¹ | ⚠️ ² | ⚠️ ⁴ | ✅ ⁵ |
| Choice menu on multiple | ✅ | ✅ | ✅ | ✅ | ✅ |
| Mark-transferred on resume | ✅ | ✅ | ✅ | ✅ ³ | ✅ ³ |
| Auto-resume without asking | ✅ | ✅ | ⚠️ ² | ❌ | ⚠️ ⁶ |
| Runs before first message | ✅ | ✅ ¹ | ⚠️ ² | ❌ | ✅ ⁷ |
| HANDOFF.md portability | ✅ | ✅ | ✅ | ✅ | ✅ |

¹ Behind Codex's experimental hooks flag; historically unavailable on
Windows. ² Hooks in-schema but rarely exercised in Cowork; the skill layer
is the reliable one. ³ Via the memory ledger. ⁴ Passive (ledger description)
by default; preference line makes it unconditional. ⁵ Injected as the
literal first message. ⁶ By editing the template to instruct immediate
resume. ⁷ With auto-send; pre-fill leaves one Enter.

**Host environments.** Warp: CLIs in Warp tabs inherit the full Claude
Code/Codex columns verbatim (hooks are process-level); Warp's notification
plugin surfaces the choice menu; per-worktree `.handoffs/` ledgers are the
default. Warp Agent Mode inherits the Cowork column via an AGENTS.md/Warp
Rule convention:

```markdown
## Session handoffs
Use the bundled session-handoff skill and its complete hooks directory.
Run handoff_ledger.py lookup and follow its explicit outcome/action.
Retrieve with prepare --session ID; authorized continuation uses
resume --session ID [topic]. Save with --input draft.json; only outcome saved completes a checkpoint.
```

Oz / cloud harnesses: commit the repo-local form of everything (project
`.claude/` hooks config, the complete hooks directory, `.context-watch.json`,
`.handoffs/`) — a fresh managed environment has no user-level config; verify
hooks fire before trusting the threshold there.

## 13. Installation summary

**Skill folder (Claude Code, Codex):** copy or symlink
`skills/session-handoff/` into a skills directory, then run
`python3 <skill>/install.py [claude|codex] [--uninstall]`. It writes
absolute-path hook entries into `~/.claude/settings.json` (`PostToolUse`,
`UserPromptSubmit`, `SessionStart`, `Stop`) and `~/.codex/hooks.json`
(`SessionStart`, `UserPromptSubmit`, `PostToolUse`, as separate groups),
backing each up once; idempotent, foreign outputs and exit statuses preserved.
Current Codex multi-group behavior was verified with 0.157.1; older first-group
compatibility fan-outs are unwrapped during migration and require native trust
review when definitions change. Skill-scoped
`hooks` frontmatter is not used: it activates only after the skill is invoked,
and the watcher must run from session start.
**Claude Code plugin:** `/plugin marketplace add <repo-or-path>` →
`/plugin install session-handoff@session-handoff-kit`. The plugin symlinks
the skill folder (installs copy the target) and registers the same hooks via
`${CLAUDE_PLUGIN_ROOT}/skills/session-handoff/hooks/context_watch.py`. Use
the plugin or the installer, not both.
**Cowork:** open `session-handoff.plugin` (built from a checkout with
`bash scripts/package.sh`), one-click install (shared plugin schema).
**Codex:** after `install.py codex` (wrapped `hooks.json` shape; some
builds expect event names at top level — remove the wrapper if hooks don't
register), enable `[features] hooks = true` (older builds:
`codex_hooks = true`).
**Chat:** save `session-handoff-chat.skill` (built by the same
`scripts/package.sh`; or upload in Settings → Capabilities).
**Extension:** load `chrome-extension/` unpacked at `chrome://extensions` /
`edge://extensions`.

## 14. Known limitations

Codex hooks are experimental: the feature flag name and the accepted
`hooks.json` shape have varied across versions, and Windows support has
lagged. Cowork hook execution is best-effort. The extension and any future
estimator mode couple to claude.ai's DOM and internal API shapes, which
change without notice; both are built to fail silently rather than
interfere. Token estimation (pending content, chat estimator) is ±20%-class
and deliberately early-biased. Claude Code and Codex occupancy measures
differ slightly in composition, so identical numeric thresholds are not
identical semantics — normalize via per-model entries. Codex `PostToolUse`
injection sacrifices one tool result by design. The model-self-report
approach to token counting is rejected permanently, not pending improvement:
the information is structurally unavailable to the model.

## 15. Kit layout

```
session-handoff-kit/
├── SPEC.md                              this document
├── README.md                            install matrix + configuration
├── .claude-plugin/marketplace.json      kit as a Claude Code marketplace
├── scripts/package.sh                   builds dist/session-handoff.plugin
│                                        and dist/session-handoff-chat.skill
├── skills/session-handoff/              the product (Claude Code + Codex)
│   ├── SKILL.md                         trigger, naming, resume
│   ├── handoff-template.md              the document's shape, swappable alone
│   ├── reference.md                     install, customizing, mechanics
│   ├── install.py                       registers/removes the hooks
│   └── hooks/
│       ├── context_watch.py             watcher + announcer + analytics
│       ├── handoff_ledger.py            state and chain tracking
│       └── thresholds.example.json      per-model starting values
├── plugins/session-handoff/             Claude Code + Cowork plugin wrapper
│   ├── .claude-plugin/plugin.json       manifest; no "hooks" field (hooks.json
│   │                                    is auto-loaded; re-referencing it is a
│   │                                    duplicate-hooks install error)
│   ├── README.md                        plugin-level install notes
│   ├── hooks/hooks.json                 PostToolUse, UserPromptSubmit, SessionStart, Stop
│   └── skills/session-handoff           symlink -> ../../../skills/session-handoff
├── chat/session-handoff-chat/
│   ├── SKILL.md                         chat-surface skill
│   └── handoff-template.md              chat document shape, swappable alone
└── chrome-extension/                    new-chat initialization prefill

`dist/` (the built `.plugin`/`.skill` artifacts) is generated by
`scripts/package.sh` and gitignored.
```

Publication receipts: generated checkpoints carry a `.published` sidecar binding
the publication ID to its content fingerprint. Until that receipt is durable,
lookup reports `incomplete` and automatic clearing is blocked. Retrying the same
request completes an interrupted receipt. Legacy checkpoints without publication
IDs remain readable. Keep receipt sidecars with generated checkpoint files.

### Integrity and cost safeguards (0.14.0)

Saving over any existing topic requires its authoritative path as `predecessor`;
publication serializes this check and enforces predecessor ownership. Independent
work uses another topic. Empty legacy documents are incomplete evidence and
cannot pass preparation or acknowledgment. Generic retrieval/resume phrases are
recognized before named-topic filtering.

The integrity/cost regressions run through public CLI, hook and installer
interfaces in `tests/test_safety.py`. Their external HTTP boundary is stubbed:
CI incurs no semantic-routing requests. The kit cannot validate the semantic
accuracy of a summary or guarantee total host spend.
