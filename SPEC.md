# Session Handoff Suite — Technical Specification

Spec version 1.0 — 2026-08-11
Component versions: `session-handoff` skill/plugin 0.18.0 · `session-handoff-chat` skill 0.6.0 · browser extension 0.2.0

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

**A blocked restart is offered again.** When `restart_preflight.py restart`
returns `blocked` from preflight, it leaves a marker beside the latch
(`context-watch-<key>.fired.restart-blocked`); a block before preflight and
any other outcome remove it, so it always describes the latest attempt;
a malformed marker counts as missing,
and so do a re-arm and a checkpoint that is no longer literally `open`, belongs
to another session, or was not saved for context pressure. With reason
`human-hold` the watcher re-offers the restart once, on the first
`PostToolUse` or `UserPromptSubmit` where the hold has cleared. Cleared means
all three: a caller hold (`--hold`, such as an open ask_user card) has seen
the next `UserPromptSubmit` after the block; no durable hold applies to the
checkpoint's topic or terminal (unreadable holds count as held); and the
checkpoint's own hold (`waiting_input` or `automatic_paused`) is unset.
The hook cannot see an ask_user card, and a mailbox notification also
arrives as a prompt, so after a caller hold the re-offer does not claim the
hold cleared: it says a new prompt arrived and to keep waiting (or pass
`--hold`) while a question to the user is unanswered. Each notice gives the
full retry command: `restart --root <project> --path <checkpoint>` with the complete `--workers`
inventory and `--host-capable`.
For any other block reason, or after the re-offer, the watcher reminds
every N `UserPromptSubmit` turns (N = 5, `CONTEXT_WATCH_REMIND_TURNS`)
while occupancy is at or above the threshold. Without a blocked restart the
plain case still fires once. Every notice says what occupancy counts: the
last model call's input + cache-creation + cache-read + output tokens, with
the cache-read share.

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
| `hooks/commit_ticket.py` | Optional project Git `commit-msg` hook: validate an existing Ticket trailer or append one from `HANDOFF_TICKET_ID`; never infer attribution from ledger state. |
| `hooks/handoff_ledger.py` | State and chain tracking over handoff files. Importable module + CLI (`list`, `resolve`, `claim`, `resume`, `supersede`, `abandon`, `save-path`, `new-path`). |
| `skills/session-handoff/SKILL.md` | Agent-surface skill pointer: runs a complete hook command directly, otherwise routes one request through `handoff_ledger.py dispatch` and loads only the named branch (`branches/save.md`, `branches/resume.md`). The folder also holds `hooks/` and `install.py`. |
| `hooks/handoff_dispatch.py` | Deterministic lifecycle dispatcher (§7.3c): operation precedence, identity resolution, target selection, holds, restart eligibility, one next action. Writes only synthetic identity, explicit holds and lock sidecars (§7.3c item 14). |
| `chat/session-handoff-chat/SKILL.md` | Chat-surface skill: memory ledger, past-chat marker, file fallback, resume resolution. |
| `chrome-extension/` | MV3 extension for Chrome/Edge: pre-populates new-chat initialization prompts on claude.ai. |
| Packaging | `.claude-plugin/marketplace.json` (kit root), plugin manifest, `.plugin` (Cowork), `.skill` (chat), `skills/session-handoff/install.py` (hook registration for Claude Code `settings.json` and Codex `hooks.json`). |

### Optional Git ticket attribution

`install.py git-hook --project PATH [--uninstall]` explicitly installs/removes
only a repository's default `commit-msg` wrapper. Linked worktrees share it.
It changes no local/global Git configuration, refuses `core.hooksPath`, foreign
hooks that do not match the generated wrapper, symlinked
hooks and parent `.git` directories. Regular linked-worktree `.git` files remain
supported. Ownership requires `#!/bin/sh` on line one, the exact
`# session-handoff Ticket hook` marker on line two, and the unchanged generated
body with the same safely quoted absolute script path in both positions.
An incidental marker or edited body does not confer ownership. Generated
wrappers can be reinstalled or removed from a different
kit path; rerun installation from the new path after moving the kit or before
retiring an old runtime release. Runtime snapshots and standalone/plugin archives include `hooks/commit_ticket.py`, but ordinary host
hook registration never installs it automatically.

The public hook CLI is `python3 hooks/commit_ticket.py MESSAGE_FILE`. Existing
valid final `Ticket:` trailer > `HANDOFF_TICKET_ID` > no attribution. The
variable is the only automatic source; checkpoint `ticket`, latest handoffs,
branch names and ledger scans are not attribution sources. Environment IDs use
`handoff_contract.METADATA_FORMATS['ticket']` (full lowercase UUID or its 8-hex
short ID). Existing trailer IDs retain `[A-Za-z0-9][A-Za-z0-9._/-]{0,127}` so
other ticket systems such as `PROJ-123` remain valid. Ticket keys are
case-insensitive in a final trailer paragraph separated by a blank line; empty, invalid, multiline or
multiple Ticket values return 1 with a diagnostic and no message mutation.
Other trailer fields and body mentions are preserved. Git comments (single
`core.commentChar`, default `#`) and scissors/diff content are excluded from
validation and retained after an inserted trailer. With `core.commentChar=auto`,
a line starting with any Git auto candidate (`#;@!$%^&|:`) triggers a quiet no-op
(including validation): the actual editor-selected character is unknown, and real `#` body lines must
remain intact. Normal editor commits include Git's comment template, so they
skip automatic attribution under `auto`. Messages from `-m` or `-F` with no
candidate-prefixed lines still receive attribution, including Markdown `-` and
`*` bullets. Use an explicit comment character or a manual trailer for editor
commits.

Missing/invalid environment IDs are nonblocking and do not change the message;
invalid nonempty IDs emit one diagnostic using the shared ticket format hint,
without echoing the environment value. Missing IDs are quiet.
Empty messages remain empty. Existing valid attribution is never replaced by the
environment. The stdlib-only hook imports only shared format/hint tables from
`handoff_contract`, not checkpoint or project executable code; the no-source,
no-Ticket path only reads the commit message. A bounded Git config probe reads
the comment character when needed. Missing Python/script makes the wrapper a
no-op; I/O/encoding or missing contract-module failures warn and return 0.
Message updates are atomic so a failed write leaves the original intact. Manual composition with foreign
hooks must propagate both hooks' statuses. This is optional attribution metadata,
not authority or proof of ticket completion. See README for install examples.

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
excluded, each `tool_use_id` counted once per window between usage or
compaction resets, and `api-request-blob` rows skipped because they mirror
canonical rows of the serialized request), plus a thinking allowance for the not-yet-written current
response: the session's largest `output_tokens` so far, capped at 25k
(`CONTEXT_WATCH_THINKING` overrides it; a single 36k-token thinking block was
observed moving occupancy 52k → 105k in one call). Disable everything with
`CONTEXT_WATCH_PENDING=0`. Exact pre-flight counting via a token-counting API
is rejected by design: a hook cannot reconstruct the request payload, and the
early-biased estimate achieves the same protection with no network call.

Occupancy and the pending estimate are different quantities. Occupancy is
usage-derived: the last usage entry's input, cache-creation and cache-read
tokens, as the API counted them. The pending estimate is a character-based
guess for content the API has not counted yet. Only the pending part is
deduplicated. Usage rows are never summed: streamed blocks of one response
repeat its usage and `message.id`, and the pending window resets at a new
`message.id` (or at any usage row without one).

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
status: it was acted on, whether or not it has been resumed since). A blocked
restart of that checkpoint is the exception: see "A blocked restart is
offered again" in section 2. After that, checks
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
then project file, then user file, then default. `"auto"` is disable-only: a
project `.context-watch.json` may set it to `false` to disable automatic
continuation, never to `true` to enable it. The watcher and the save transition
apply one rule (`mandatory_gate.auto_setting`): with `handoff_v3`, an invalid
timing config is off; a non-empty `HANDOFF_AUTO` wins; project `false` disables;
otherwise the user file decides. Unset means on inside AgentsRoom and off
elsewhere, except under `handoff_v3`, where an unset value is off, overriding
the AgentsRoom default. Both judge v3 timing with the same inputs: the model
and reported window the watcher recorded in the session note. The watcher reports a project `"auto": true`
once per session with a `handoff-status:` diagnostic. Wrongly
typed values are ignored; these keys are never read as model thresholds.

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
preparation loads them only in the applicable phase and with advisory size measurement.

Project resolution uses the caller's cwd or explicit root, independent of the
installed script path. A linked Git worktree bounds ancestor convention search:
use the nearest handoff convention within that worktree, or its Git worktree
root if none exists. An established home-fallback claim is honored; otherwise
new saves prefer its local `.handoffs/` even when absent. The `.git` file is a
boundary without invoking Git, so an unavailable/slow executable cannot cause
discovery to climb into main; submodules and other non-worktree Git-file checkouts retain ancestor
convention discovery, including superproject holds. Ordinary Git-directory repositories keep convention-based discovery.
Existing misplaced checkpoints require an explicit `--root <main>` for protocol
preparation/continuation and are not implicitly migrated. Holds are a shared
control plane, not worktree-local checkpoint state; see §7.3c item 8.
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

**Header format.** The header is the block between the first two `---` lines:
one `key: value` per line, key lowercase, value a single line taken verbatim
after the first `:` with surrounding whitespace trimmed. There is no quoting,
nesting or continuation, so it is not YAML and needs no YAML library; save
refuses any value containing a newline. Readers ignore keys they do not know.
A reader in a few lines of stdlib (`handoff_identity.parse_front_matter`):

```python
lines = text.splitlines()
fences = [i for i, line in enumerate(lines) if line.strip() == '---']
end = fences[1] if len(fences) > 1 and fences[0] == 0 else 0  # no header -> {}
fm = {k.strip().lower(): v.strip() for k, _, v in
      (line.partition(':') for line in lines[1:end] if ':' in line)}
```

**Work metadata.** Three optional lines tie a checkpoint to the work it belongs
to, for the resume staleness check. Each is written only when the draft sets
it, so a checkpoint without them is a legacy checkpoint that still resumes, not
an invalid one.

| Line | Value written | Draft accepts | Checked on save |
|---|---|---|---|
| `ticket:` | AgentsRoom backlog ticket id | lowercase UUID, or its first 8 lowercase hex digits | format only; the backlog is not stored locally, so existence is not checked |
| `base:` | full commit sha (40 or 64 hex) | 7–64 lowercase hex digits | must be an unambiguous prefix of one existing commit's own sha in the project (`git rev-parse --verify <sha>^{commit}`, and the result must start with the value); a missing, ambiguous or non-commit object, a tag sha or a ref name that merely looks like hex, or a project that is not a Git repository, is refused |
| `role:` | role slug, e.g. `backend` | kebab-case, at most 40 characters | format only |

An invalid value fails the save with `outcome: invalid` and an `action` naming
the field, before any file is written: formats are checked before the lock,
and `base` is resolved only for a new write, so a retry of an already-saved
checkpoint still finishes after its base commit is gone. An empty string means "not set".
`ticket` and `role` inherit from a trusted predecessor when the draft omits
them (an empty string clears them); `base` is a fact of one save and never
inherits. `handoff_contract.checkpoint_metadata(fm)` returns only the
well-formed values of a parsed header, so a hand-edited or malformed line reads
as absent.

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
the save branch's wind-down delegates to it.
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
agent-authored core (`--input -` reads stdin, matching `dispatch`; omitting
`--input` also reads stdin unless `--topic` or `--body` supplies the draft) and generates timestamps, project/Git state, session/trigger
identity and publication identity. Identical request retries return the same
checkpoint; changed content under the same ID is refused. Temporary-file fsync
and exclusive atomic publication prevent partial Markdown files. Directory
failures try the other supported location, then return `blocked` with recovery
instructions. New saves persist authenticated, exact-content publication hashes
in `~/.local/state/session-handoff/publications/` before publishing Markdown.
`repair PATH` checks it under publisher/checkpoint locks, verifies the original
publishing terminal (session when no terminal exists), current claims and
current authoritative lineage, and seals only unchanged open content. It needs
no original draft or request ID. Intent storage failure degrades to publication
with an explicit retain-original-request/draft warning. Intents contain hashes
rather than checkpoint text, failed destinations remove theirs, and subsequent
save/resume operations prune intents older than 30 days. Missing intent on legacy saves remains blocked
and offers the original save request or explicit `abandon PATH`; abandonment needs only the checkpoint file
and closes it without trusting its content. Hooks, lookup and dispatch share concrete recovery guidance:
use `repair PATH` only with authenticated intent matching the current bytes; otherwise ask before
abandoning. `publication_incomplete` is not retryable by an unchanged resume request. That save retry
keeps its original `--session` despite a newer hook
pointer only when session and request ID name an unpublished checkpoint this same terminal wrote; otherwise
hook identity still wins. A save whose topic or predecessor resolves to unpublished work returns
`incomplete` with the repair/original-save command, the explicit `abandon PATH` escape and the
distinct-topic option, and publishes nothing, so no successor silently inherits from an untrusted,
unfinished record. Explicit abandonment lists the interrupted record as `abandoned` (it stays
untrusted and unresumable) and lifts the gate; a successor of it inherits nothing and is warned. Neither CLI nor skill bypasses host permission denial.

Closed records (`resumed`, `superseded`, `abandoned`) stay closed when publication receipts
are missing or their body has changed. They remain available through history, cannot be
selected for a fresh resume, and cannot create an incomplete-publication gate. Preserving
closed state does not establish content trust or transfer ownership.

Lineage is explicit through `predecessor`; branch equality never supersedes work. A resumed predecessor
can be continued only by the session that resumed it (others get `conflict`), and a
successor never rewrites a `resumed` record to `superseded`.
The published successor is the committed replacement record, so a crash after
publication cannot resurrect its predecessor. All state changes check owner
under a sidecar lock. Omitted owners do not bypass another session's claim.

`prepare TOPIC-OR-PATH --session ID` is read-only retrieval. With `--execute`
it claims a validated, complete package and records a recoverable preparation
receipt. Claims expire after two hours or can be released explicitly. Checkpoint
and dependency fingerprints bind preparation to the material actually delivered.
`verify PATH --session ID` preserves pipeline failure, stores complete output in
a log, and returns a bounded excerpt. A `verify_baseline` match is accepted only when pytest printed its
summary line and no `ERROR` lines; otherwise the failure is unknown and stays `verification-failed`. `acknowledge PATH --session ID` transfers
only after preparation and any recorded verification pass. Failure/expiry/change
requires retry or release; it never silently marks work resumed.
`resume [TOPIC-OR-PATH] --session ID` chains selection, `prepare --execute`
(auto skill catalog), verify when recorded and acknowledge, returning
`resumed`, `choose`, `none`, or the failing `stage`. `--budget-bytes` sets an
advisory package-size threshold; exceeding it emits a warning, delivers the
complete result and still acknowledges successful resumption. `--session` defaults to
`$HANDOFF_SESSION_ID`, then the hook's per-terminal pointer (24 h); save's
request ID defaults to a hash of the draft. The hooks refresh that pointer on
SessionStart (`startup`/`clear`), on any UserPromptSubmit or PostToolUse once the
transcript reports usage, and before any transcript exists on two prompts: a lean
"continue the handoff" prompt, and a prompt starting with the restart prompt
prefix `handoff_contract.RESTART_PROMPT_PREFIX` (case and leading space ignored).
The second only records identity, before the first tool call; it changes no routing (14752baa). `adopt` resolves the successor identity from its own cwd root first and falls back to the binding root's pointer only when that names a session other than `record['session']`; the binding root still keys ledger and intent. A hook-sourced candidate naming another session counts only if its pointer was written after `dispatch_started_at` (detail `pointer at <root> predates dispatch` otherwise); host-sourced identity is unchanged. adopt must run from the successor's launch directory. An observed session equal to the predecessor blocks `successor-identity-mismatch` with detail `no successor observation at <roots>; pointer still names the predecessor`, whatever the pointer timestamp (the predecessor's own PostToolUse of the restart call may rewrite it after dispatch). Save resolves the project from the
predecessor (an explicit root that disagrees is a `conflict` naming both),
treats this session's or terminal's own authoritative checkpoint as the default
predecessor, defaults `verify` to the user-level config `"verify"` then the trusted predecessor's,
accepts `--topic/--description/--body/--attach`, and returns `transition`
(`new-session`, `clear`, `tmux`, `runner`, `agents_restart`, `paused`) with its action.
Retrying an already transferred/closed save returns transition `none` and never
requests a restart; retrying a paused publication preserves its pause.

Before acknowledgement, one-shot resume persists a full replay package with the
per-transfer receipt. Failed stdout delivery leaves pending replay discoverable
by the same command, including argumentless selection. A successful flush marks
transport success; it does not prove host consumption. Explicit path replay
remains available for two hours to the proven holder while checkpoint/dependencies are unchanged
and authoritative, with current workspace findings and no repeated verification.
A newer successor or live foreign claim blocks replay. Full replay restores
previously reused body/references and previously loaded required skills. Expired
payloads are pruned on subsequent resume; compact holder provenance is retained.
Pending replay alongside current open work requires explicit selection.

AgentsRoom save transition candidates include `requires_preflight: true`.
Dispatch requires the standalone preflight module to return ready/dispatch from
current authoritative host observations; unknown capability, identity, inventory
or hold state requires manual continuation. The predecessor runs one command,
`restart_preflight.py restart --root R --path P --workers '<JSON>' --host-capable [--hold]`,
where save supplies its exact published path (including on an idempotent retry).
Without `--path`, one owned open checkpoint is selected; several return blocked
`ambiguous-checkpoint` with up to ten path/ID/command candidates and a total count,
never an implicit latest choice. Candidate commands preserve caller observations
and rerun all preflight gates; match the successful save path or ask the user.
The command reads hook/host identity, validates the checkpoint and derives the
deterministic request ID, adds durable holds and preflights; observations are
stored under the intent lock only with a new ready intent. A rerun after a host
refusal returns `refused` (a retry needs a new save). Inventory shapes: `[]` is a
known empty inventory; an entry is `{id, report: {path, sha256}}` or, for a worker
that finished without a report file, `{id, settled: true, report: null}`; an entry
may also be `{id, state: "in_flight", via: "mailbox"}` when its reply is persisted
in the AgentsRoom inbox for the successor. Other workers without a report or
`settled: true` block (`worker-report-not-durable`); an
omitted `--workers` is unknown and blocks (`worker-inventory-unknown`).
New ready intents return `restart_prompt`: one sentence naming one successor
command, `restart_preflight.py adopt h-<24 hex>`. The handle is a prefix of the
SHA256 of the canonical six-field binding (checkpoint_id, path, request_id, root,
session, terminal). A private per-user locator under
`~/.local/state/session-handoff/restart-handles/` records root, request_id and the
full hash; the intent stores that hash too. The successor resolves the locator
without consulting cwd or taking root from the prompt, reloads the ledger intent
and recomputes its binding hash. Missing, redirected or mismatching locators and
bindings fail at stage `binding` before delivery. Collisions refuse overwrite.
Locators may outlive pruned intents; they confer no adoption authority without a
valid intent. The hooks path stays pinned to the emitting installation, including
candidate snapshots. Legacy full-binding JSON remains accepted for one release.
After binding validation, adopt checks
identity, refuses a pending intent older than 7 days (as does the stepwise
`successor`; dispatch warns on it instead of routing, and it blocks only its own
checkpoint's restart), prepares internally, records
the durable receipt, stops with `outcome: held` under a dispatcher hold, then
resumes, emitting the body once; a failed resume is `blocked` at stage `resume`.
If preparation blocks, return the recorded checkpoint `path`, the exact adopt
`command`, and available retrieval-only output under `preparation`, excluding
the delivery receipt. Preserve objective/next-step context, continuity and
predecessor when retrieved; otherwise direct inspection to the recorded path.
Use `prepare-<outcome>` for an unsuccessful retrieval, `prepare-path-alias` for
an equivalent canonical path with different spelling, and
`prepare-target-mismatch` for a different checkpoint. Both path refusals retain
the pending intent without confirming a successor, claiming or executing work.
Resolve the cause before rerunning; explicitly running the command authorizes
only the recorded checkpoint under existing identity, trust, ownership and hold
gates. Switching to different work requires a separate user decision.
The retrieved intent keeps `retrieval_receipt` (`token_sha256`, `path`,
`fingerprint`, `created`, `references`, `confirmed_at`), so the confirmed
delivery stays verifiable after a later resume rewrites the live `.delivery`
file; only the token's SHA-256 is stored, never the token. Each preflight first
prunes, under the registry lock, retrieved/refused intents older than 30 days
whose checkpoint is no longer open and authoritative (compared by real path),
without waiting on a busy record's lock and removing its lock sidecar; pending intents (expired
or not) and invalid records are never pruned.
New saves record the hook-observed workspace (or project root) and SHA256 of
`.claude/settings.json` and `.claude/settings.local.json`, including absence.
Preflight compares that save-time baseline before creating an intent. The
`hook_drift` key (`warn` default, or `block`) in `~/.context-watch/thresholds.json`
or the restart root's `.context-watch.json` sets the result: `block` in either
scope wins, so project config cannot relax a user-scope block; any other present
value blocks with `invalid-hook-drift-policy`. Under `warn`, drift proceeds with
`warnings: [{reason: hooks-changed, policy, changed, action}]`, which are also
stored in the intent, so `status` and duplicate or reconcile responses return
them, and a re-entry replaces them with freshly computed ones; under `block` it
returns `blocked`, `hooks-changed`, the first changed `path` and the full
`changed` list. Each `changed` entry gives the path with saved and current
SHA256 (null for absent); `restart` passes `warnings`, `changed` and `policy`
through, with the action text as `detail`. Unreadable hook files and invalid
baselines still block, as does a policy config file that exists but cannot be
read or parsed, or is not a JSON object. Legacy checkpoints without a baseline retain existing
behavior. This comparison detects drift only. Claude Code 2.1.306 showed no host
hook approval (b7ec7020), so the action text asks for a review and, when
blocked, a new save; it names no approval step.

AgentsRoom saves (environment agent ID or terminal session file) prepend
`agents_read_inbox` to Next steps and persist an open `agentsroom-inbox`
continuity item. Unknown mail status is explicit; no unread count is fabricated.
Plain CLI checkpoints gain no mail obligation. Save retries remain idempotent.

Adopt identity mismatch diagnostics include expected predecessor session and
terminal, observed session/terminal/host session and identity source. New saves
record the hook-observed workspace and its Git position. A missing recorded
workspace blocks adopt at stage `workspace`, reason `workspace-missing`, before
retrieval, with the path, recovery root and recorded Git position. Identity
mismatches remain primary and also carry workspace existence findings. Legacy
checkpoints use their recorded project path; a stale host cwd absent from old
checkpoint metadata cannot be reconstructed. Recovery requires explicit path
reconciliation or recreating the recorded workspace; no workspace is selected
or created automatically. AgentsRoom should launch at an existing cwd, falling
back to the root with a notice when its saved cwd has disappeared.

Successor confirmation automatically imports a Codex completed MCP event or
Claude tool-use/result pair from the predecessor native transcript, matching the exact saved prompt, predecessor thread, terminal,
post-intent completion time and `ok:true,scheduled:true` AgentsRoom response.
The event and SHA256 remain in `host_acceptance`; `host_confirmed:true` means
acceptance/scheduling only. Successor retrieval preserves that evidence and
reports acceptance separately from observed native identity and kit delivery.
No caller boolean or receipt can establish host acceptance. Explicit imports
use the same matcher. Files must name the predecessor session, resolve under
Codex sessions or Claude projects roots, and be regular files opened with
O_NOFOLLOW and O_NONBLOCK. Reads are capped at 64 MiB/file and 2 MiB/line.
Expired pending intents cannot import. Persisted acceptance is checked again
against the retained event and hash on every status read. Local evidence is
not cryptographic host attestation.
Only the agent invokes the MCP restart tool, as its final tool action before ending
the turn. Pending/refused/retrieved states supply no new dispatch prompt. This
orchestration provides no atomic host transaction or host replacement guarantee.
Intent creation is serialized by a durable project registry lock across paths
and temporary directories. Different request IDs cannot bypass pending intents
for the terminal/path or retrieved/refused intents for the checkpoint. Invalid
persisted state blocks. New identity tokens are ASCII `[A-Za-z0-9._:@-]{1,128}`;
the complete prompt is limited to 20,000 characters before persistence and is
retained exactly for audit. Definitive refusal requires a new checkpoint before
another authorized attempt; uncertain dispatch remains pending. Runtime snapshots include the preflight module and smoke-check
its CLI alongside the ledger; its state parsers share the core corruption decoder.

Default resume JSON delivers the full body once and identifies its next-action
section with `next_step_section`, and leads with a bounded `next_step` copy (at
most 3000 JSON-encoded bytes, cut with a `continues in the body` marker).
`--legacy-next-step` or `--fields next_step` opts into the older full text field. One-shot resume also accepts the
installed `--catalog` and same-context `--reuse-receipt` options. Existing
dependency, expiry, trust and verification checks govern reuse.

`--fields` trims optional output, preserving diagnostics and the available
successful delivery envelope: body, references, skills, next-step section,
verification/trust/workspace facts, optional/withheld reference paths, receipt
and reuse markers, predecessor and continuity. Delivery metrics use the same
projection as CLI serialization.

Optional draft `continuity` is a list of items with nonempty `id`, `text` and
`status`; optional keys are `owner` and `evidence`. Status is open, delegated,
resolved or retired. Delegation requires owner; resolution/retirement requires
evidence (result or reason). A trusted predecessor's register merges by ID;
omission and an empty list preserve prior items. Explicit updates record their
new disposition. Metadata stores the register as one-line JSON. Trusted
predecessor skills/references/optional_references/ticket/role inherit only when the draft
omits the field; explicit lists replace it, including `[]`. Untrusted
predecessors supply neither implicit dependencies nor register entries. The raw
draft still defines retry identity. These records preserve declared obligations,
not unrecorded transcript meaning or new authorization.


Automatic transition guards operate on trusted terminal-scoped records.
Pressure within 600 seconds of resume (`rapid-resume`) or a third pressure save
of one topic within 1800 seconds (`repeated-topic`) pauses only without progress.
`save_progress` defines progress once: the draft `content_hash` changed AND at
least one of these changed since the trusted same-topic pressure predecessor:
the effective continuity register (ignoring item order), a strictly newer Git
commit descending from its recorded commit, observed Git working-file evidence,
or a strictly larger count of grounded source anchors in audit findings.
Working-file evidence hashes modified/deleted/untracked nonignored file names
and contents (symlinks use their link text), excluding top-level `.handoffs`,
`.agentsroom` and `.git` paths only. Git directory entries ending in `/` (such as
untracked nested repositories or linked worktrees) are skipped; their contents
are not evidence of progress in the parent repository. Hashing is bounded to
2 MB of content and 200 KB of paths. Git failures,
missing legacy hashes, or exceeded bounds are unknown and do not prove progress.
Audit evidence uses bullets in `## Findings` of the form
``- `relative/file.ext:42`: conclusion``.
The selected save workspace root is resolved once at the save boundary, so a
symlinked workspace root is supported. Paths are relative to that resolved root,
with slash-separated segments of word characters, dots and hyphens; extensionless
filenames and dotted directories are accepted. Empty, `.` and `..` segments and
absolute paths are rejected. Every source-path component beneath the root is
opened relative to a root directory descriptor without following symlinks.
Only readable regular files qualify; directories, FIFOs and other special files
do not. Line numbers are
positive decimal integers within the source's line count; a nonempty unterminated
final line counts. The conclusion must be nonempty.
Fenced code is excluded: fences use at most three leading spaces, matching
backticks or tildes, and a closing run at least as long as the opener with only
whitespace after it. An info-bearing marker does not close a fence.
SHA-256 identities of distinct path/line anchors are stored as a deterministic
sorted sample of at most 64 in the sealed `audit_anchors` JSON list, plus the
string-valued `audit_anchor_overflow` count beyond the sample. Progress requires
the total count to grow strictly from the trusted baseline. Rewording, reordering,
removing, bumping one valid line number or rotating anchors at the same count
does not establish audit progress. This is conservative declared evidence, not
semantic proof of a new finding or a correct conclusion.
Extraction is bounded to 64 KiB of UTF-8 body, 1024 bytes per qualifying bullet,
256 KiB per source and 1 MiB of source content in total, with a one-byte oversize
probe. Each distinct source is read once per extraction. An oversized bullet is
skipped; exceeding the body or source-read bounds stores unknown evidence.
An empty JSON list with zero overflow is a known empty baseline. Missing or
malformed anchor lists/counts are unknown; legacy valid lists without overflow
use zero. Overflow is valid only with a full 64-identity sample.
Ordinary body/description changes alone do not establish
progress. An absent or untrusted
same-topic pressure predecessor cannot prove progress on its own; the baseline then
falls back to the newest trusted checkpoint whose transfer this session holds a valid receipt for (any topic or reason),
and the same changed-hash-plus-evidence rule applies against it. Only a newer commit
descending from the recorded commit counts; a branch name change alone does not.
Changing branches to a descendant commit does count; unrelated Git history does not.

With progress, rapid/repeated reasons remain advisory: restart proceeds subject
to other gates. Transition diagnostics and JSON `thrash_warning` front matter
retain reasons, progress evidence, topic count, elapsed resume time and available
startup/observed/resume token figures. The pressure hook emits one warning line;
before the draft exists, soft reasons warn and save makes the progress decision.
A topic listed in the `continuous_topics` setting (string list, from
`~/.context-watch/thresholds.json` or `<root>/.context-watch.json`) is exempt from
pausing on `rapid-resume` and `repeated-topic`; the reasons are still reported and
`continuous_topic` is true in the diagnostics. Hard limits below are unaffected.
Sharing a topic with a predecessor alone does not set `continuous_topic`.
A repository `.context-watch.json` is honored for this key (project scope is intentional), so a repository
can relax the soft pause for its listed topics; `restart-budget`, `restart-cooldown` and `invalid-timing` stay hard stops.
Required body headings (`## Objective`, `## Current state`, `## Next steps`) match
at line start; text after a space following the title is allowed.
Five prior AgentsRoom restart intents within 3600 seconds (`restart-budget`) or
a different session's intent within 120 seconds (`restart-cooldown`) always
returns `paused`, regardless of progress. These hard limits are unchanged.
Both read checkpoint timestamps, so a stamp more than 60 seconds in the future
(the clock moved back) would hide an intent from them. In AgentsRoom, such a
stamp on one of this terminal's checkpoints, or a future `resumed_at`, pauses
with `invalid-timing`, a third hard stop, instead of failing open.
Intent counts cannot observe restarts outside the kit; the host remains the
authority for its limits. Durable resume timestamps back up ephemeral notes.
Successful restart adoption also writes `resumed_at` and `resumed_topic` into the
observed successor's root/session note, while ledger/receipt ownership stays at
the binding root. Adoption replays retain the checkpoint's original sealed
acknowledgement timestamp, preserving the rapid-resume clock.
A paused checkpoint is fully published and remains open. SessionStart, tmux
Stop and the headless runner honor its pause; explicit continuation can resume
it. The runner exits 75. Host refusal preserves the open checkpoint for the
same manual recovery instead of repeated restart attempts.

Resume and restart adoption measure exact serialized `delivery_bytes` (excluding
the terminating newline) separately from complete
context `package_bytes` (which also accounts for workflow and reused content).
`estimated_tokens` estimates that package; `estimated_delivery_tokens` estimates
the emitted JSON, including its own metrics and, for adoption, the restart
envelope and ASCII escaping. Both token estimates use UTF-8 bytes/4, rounded up.
Successful deliveries persist the latter as `resume_delivery_tokens` in the
successor's root/session note for later save diagnostics, including adopt replays.
With an available hook occupancy sample,
post-resume occupancy adds only newly delivered tokens; remaining working room
is the handoff threshold minus that estimate, with a warning below 60000 tokens.
Window/source and threshold/source are reported; unmeasured future schemas and
reads are excluded. Size, broad-reread and >80-line excerpt warnings stay advisory.

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
notice routes to the bundled AgentsRoom restart procedure. After a ready preflight,
the agent ends with `agents_restart` using the returned exact `restart_prompt`,
whose initial prose avoids native CLI subcommands such as Codex's `resume`; an inherited
`TMUX_PANE` is ignored there, and the tmux Stop hook never types into it.
An unrelated live task takes precedence and leaves checkpoints open.

Exact retrieval/resume commands, topics and paths route without a model call.
Only ambiguous prose may use the Jev classifier with explicit
`CONTEXT_WATCH_JEV=1` opt-in and a key. Reserve at most one attempt per
project/session, including failed calls; reservation failure stays local. Classification can
select context but does not grant execution authorization. Both hook branches
use the same opening-action policy.

Topics: `save` slugifies a non-kebab-case topic (lowercase, non-alphanumerics to
single hyphens, 80 characters) and reports the slug in `warnings`; a topic with no
usable characters is `invalid`, and the pressure contract text states the kebab-case
rule. Save root: when no explicit root argument or predecessor names the project and the current
directory lies inside a temp base, the project recorded for the session by the hooks
(a root-independent pointer in the temp directory, keyed by session id) is used
instead. The pointer must be a private, user-owned regular file naming a
user-owned project directory outside temporary bases. Without a valid pointer,
a temp-base root or scratch directory inside one is refused, even if it holds
`.handoffs/` or `.git`; pass an explicit root (`save <root>`, including `save .`)
to intentionally save there. Redirects appear in JSON `warnings`.
Root discovery ignores fallback handoffs that name a temp base itself as their
project, so scratch directories below it never inherit that fallback ledger.
At a temp base itself, `.handoffs/` is discovered, listed, read or written only
when it is a real directory (not a symlink) owned by the current user with
owner-only write (no group or other write bits; the project pointer's owner
rule); otherwise it is ignored and saves use the fallback. The same rule
covers its state subdirectories (`dispatch/`, `restart/`, `holds/`, `pointers/`):
an untrusted copy is never read or written, synthetic identities and holds
ignore it, and restart preflight blocks with `untrusted-temp-handoffs`. When
a save creates `.handoffs/` at a temp base it uses mode 0700 and re-checks it
just before writing, so one planted after the destination check is refused. Emitted
commands name one hooks directory: the installed `~/.agents/skills/session-handoff/hooks`
when it resolves to these hooks, else their real path.

Session notes are keyed by canonical project plus session identity. A trigger
is satisfied only by a validated publication with matching session/trigger IDs;
mtime changes to old or unrelated files do not count. The headless auto runner
matches its own run ID and stops immediately on a nonzero child exit. Its
positive run limit returns exit 75 on exhaustion, retaining open work. A run
count is not a token, dollar or wall-time budget. Failed saves never authorize
automatic clear.

### 7.3b Context budgets and measurement

The recommended complete preparation size is 32,000 UTF-8 bytes including JSON,
workflow instructions and declared already-loaded skills. Component byte counts
and a bytes/4 token estimate are returned; this is not measured billing usage.
All size targets are advisory, including `--budget-bytes`; no approval is required
to exceed them. Aim for documents under 24,000 bytes and 1,500 words, about eight
required references and sixteen required skills. Larger content is read and delivered
in full without blocking save, preparation or acknowledgment. Required references may
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

SessionStart is silent for ordinary empty, owned, foreign and paused checkpoint
state, including automatic-mode startup/clear. An incomplete owned publication
names only that checkpoint and its save-repair command; compaction recovery remains.
An anchored `continue`/`resume` + `the`/`your` + `handoff` prompt (case-insensitive,
optional final period/exclamation mark) inspects ownership read-only in a worker
with a one-second timeout. Exactly one owned, trusted, open, authoritative checkpoint
gets an exact-path `resume --fields path,body` command. This projection always
retains required references/skills, workspace findings, verification and warnings.
It includes an instruction that the
skill need not load and that the command runs exactly as given, with no head, pipe
or redirect: its output is the complete required package. Output is not
truncated; resume output always puts `next_step` right after `action`, then
`path` and `next_step_section`, before references, skills and body. `next_step`
is the Next steps section measured as JSON-encoded bytes and, above 3000, cut
with a `continues in the body` marker, so the next step or that marker lands in
the first 4000 bytes even of a clipped read; a bounded lead copy is the price of
clip safety. No owned checkpoint
produces no output; ambiguous, untrusted or `automatic_paused` owned state gives one
skill pointer (a host-injected restart prompt cannot be told apart from a typed one). Inspection failure/timeout is silent. Selection
never claims, acknowledges or verifies. Manual retrieval, other CLI flows and
pressure-save guidance remain available, including pressure on a matching prompt.
Informational compaction, floor, unrelated-work and error notices do not activate
the skill.

With hooks, omit `--session`. The terminal’s recorded identity takes precedence
over an explicit ID or HANDOFF_SESSION_ID only while bound to the current host
session. Pointers record the host session at hook time; a mismatching current
CODEX_THREAD_ID invalidates an older pointer (legacy pointers require an exact
session match). The current host ID then takes precedence over explicit and
HANDOFF_SESSION_ID values; differing explicit hook/host IDs produce a warning.
The headless runner removes inherited host/session IDs before launching each
fresh child; the child supplies its own actual session identity.
Without a recorded identity, explicit IDs remain supported. Resume preparation
receipts record the terminal/runner that performed the transfer and its exact time.
A later save may continue its own session or a transfer proven by that local receipt
to belong to the same terminal (runner when no terminal was recorded). The original
saver’s terminal is not proof of the resumer’s ownership. A newer live claim still
blocks, and foreign or untraceable transfers remain conflicts with holder details
and a save command to run in the holding terminal. Receipts persist per checkpoint
and resuming session in `~/.local/state/session-handoff/transfers/`; a different
topic's preparation cannot overwrite them. Existing temporary receipts remain a
read fallback, but unsigned receipts supply no terminal ownership evidence and
unsigned preparation must be repeated. Transfer receipts authenticate their full
payload, including project/session/path, fingerprints, transfer timestamp and
terminal/runner/provenance. The local key remains within the same-user trust
boundary; this does not isolate arbitrary commands from that user’s credentials.
Malformed or excessively nested shared state is treated as corrupt: cleanup skips
it, receipt reads supply no ownership proof, and repair returns a structured
refusal. Cleanup authenticates receipt metadata before creating a path lock.
Terminal provenance takes precedence even over a matching session ID.
Acknowledgement marks the checkpoint `transfer_receipt: signed` (excluded from the
content fingerprint). A marked transfer whose receipt is missing, corrupt or fails
authentication is untraceable: no session ID proves ownership, and save returns the
holder conflict with the `recover-transfer` command. Only unmarked true-legacy transfers
keep session-ID ownership. Transfers acknowledged before the marker existed are not
migrated and keep that legacy fallback until their next transfer.
For untraceable legacy holders, `recover-transfer` requires explicit per-checkpoint
user confirmation of the actual resuming terminal, exact prior session/timestamp
and checkpoint fingerprint. Locked checks reject changed/newer state, live foreign
claims and proven foreign holders; durable user-attested provenance makes retry
idempotent without rewriting the checkpoint or lineage. Saver identity never
supplies resumer evidence.
Save help and `save --template` expose the draft contract directly.

A preparation response includes an opaque delivery receipt. `--reuse-receipt`
can omit an unchanged body/reference payload already present in the same context.
The server validates session, checkpoint/reference fingerprints and a two-hour
expiry. Invalid receipts reload in full; compaction invalidates delivery state.
Without hooks, callers discard receipts after context loss. Reused bytes remain
in the package size, as do the phase-specific continuation instructions.
Retrieval caches delivery metadata without claiming or mutating the checkpoint.

### 7.3c Lifecycle dispatcher

`handoff_ledger.py dispatch --input -` takes one JSON request with the keys
`operation`, `prompt`, `target`, `topic`, `session`, `authorization`,
`override`, `host`, `request_id`, `reuse` and `expect`. Unknown keys give
`invalid_request`. It returns one decision and launches nothing.

1. An explicit `operation` takes precedence over prompt classification.
   Classification routes only exact lifecycle commands. Quoted mentions,
   neutral notices, status, summary and commit requests, and skill-maintenance
   text give `none`. Save commands also include `session handoff`, optionally
   followed by `to yourself`, `to your successor`, `to successor` or `to self`;
   `hand off [this|the|my] work|session|handoff`; and bare `handoff` or `hand off`
   with no other text (a trailing `.` or `!` is allowed). After the quoted,
   neutral, pressure and maintenance guards and recognised commands,
   `ask`/`lifecycle_unrecognised` applies only to command-shaped prompts:
   at most four whitespace-separated words, no `?`, and the first token is
   `handoff`, `handoffs`, `resume`, `retrieve`, `checkpoint` or `park`, or the
   opening is `hand off`. The two-word `hand off` counts only in this form.
   Single and curly-single quotes count as quoting alongside double quotes
   and backticks. For `park`, only bare `park` or a lifecycle object (`work`,
   `session`, `handoff`, `checkpoint`, `this`, `it`, `that`, optionally preceded
   by `this`, `the`, `my`, `a` or `your`) qualifies; `park the car` gives `none`.
   Prose, questions and longer unmatched commands give `none` as before.
   The ask action has user-facing `text` and additive `options`: an ordered
   array for `save`, `resume`, `retrieve`, `none`, each with `operation`,
   `label` and a one-line `description`. Show these verbatim as a pick card,
   then dispatch the chosen operation explicitly. `operation: "none"` returns
   `none`/`no_operation`, ignores any explicit target or targeted prompt,
   and suppresses prompt inference. Choosing `resume`
   also supplies `authorization: {"source": "user"}`. The clarification itself
   grants no execution authority and does not retry.
   Execution needs user authorization (an exact prompt or
   `{"source": "user"}`) or configured autoresume. Checkpoint content never
   authorizes. Exact `resume <target>` and `retrieve <target>` also classify
   and select a target: one unquoted, whitespace-free `.md` path, kebab-case
   topic, or readable name. A readable name must match an existing topic
   after save's slug normalization, or the full stored description of an open
   checkpoint, case-insensitively. Partial matches do not select. If topic and
   description identify different checkpoints, neither takes precedence:
   return `ask`/`target_ambiguous` with both. Multiple open topic heads give
   `ask`/`topic_ambiguous`; multiple exact descriptions also give
   `ask`/`target_ambiguous`, never execution authority. Only exact kebab-case
   topic spelling selects explicitly. A slug-normalized readable topic or a
   unique description match proceeds only if owned: a tagged checkpoint must
   match this terminal; an untagged one must match the resolved session. A
   foreign readable name returns `ask`/`foreign_only` with candidates and no
   execution authority; the user must explicitly select its exact path or
   kebab-case topic. Without a target, one open checkpoint owned by this
   terminal is selected (`owned`). Several give `ask`/`target_ambiguous` by
   default; with the `owned_selection` setting `"newest"` (string, from
   `HANDOFF_OWNED_SELECTION` or `~/.context-watch/thresholds.json`; a project
   `.context-watch.json` is ignored; any other value means `"ask"`) the newest
   is selected as `newest_owned`, the next five are returned as `alternatives`
   and `alternatives_total` counts all the others. Under `"ask"`, a just-parked
   checkpoint is still selected, as `just_parked`, with the same `alternatives`:
   the newest owned checkpoint must record this terminal in its frontmatter
   (legacy session-only ownership never qualifies), its frontmatter `created`
   must be at most 3600 s old (`JUST_PARKED_SECONDS`), and it must be the only
   owned open checkpoint saved inside that window. Two or more inside the window,
   an unparseable `created`, or no owned checkpoint inside it keep
   `target_ambiguous`; foreign checkpoints are never owned, so a newer foreign
   park cannot be selected or displace an owned one. The prompt hook applies the
   same rule, over the same candidates as lookup (newest per topic, unclaimed,
   at most 14 days old), before its single-checkpoint trust checks. A
   `just_parked` selection is acted on only for a typed prompt: a promptless
   autoresume start lists the candidates instead.
   Every candidate, alternative and selected target carries `created` (save
   time), `parked` (last write to the checkpoint file) and `last_resumed`
   (newest resume of a checkpoint with the same topic, empty if never). A
   selected target saved over 24h ago (frontmatter `created`, else the last
   write) also carries `age_notice` (`parked N days (saved X)`), repeated in
   the opening text; claim and release do not reset it. Non-path text matching
   no topic or description (such as `resume later`) gives
   `ask`/`lifecycle_unrecognised`, without selecting a target.
   A bare readable choice can accompany an explicit `resume`/`retrieve`
   operation; bare choice text alone does not authorize resume, which still
   needs `{"source": "user"}`. Verbs are case-insensitive; path case is preserved.
   Relative paths resolve under the dispatch root. An explicit JSON `target`
   wins; an explicit `operation` still wins over the prompt verb.
   Paths containing spaces require JSON `target`. Missing, superseded and
   closed prompt targets follow the same exact-target rules with no fallback.
2. Identity resolves in this order: hook record, then host
   (`CODEX_THREAD_ID`), then explicit, then `HANDOFF_SESSION_ID`, then a
   synthetic id. The synthetic id is persisted per terminal and only for save,
   pressure and retrieve. An explicit id that differs from an authoritative one
   gives `identity_conflict`. Autoresume needs a hook or host identity.
3. Pressure returns a save action that starts no new work and records
   unfinished operations under `## Pending operations`.
4. A save that names a topic binds that topic's authoritative predecessor. More
   than one open head gives `topic_ambiguous`.
5. Overrides are `repair` or `recover-transfer`. `recover-transfer` needs
   `user_confirmed: true`. Neither bypasses an identity conflict.
6. Every result has a `code` from a closed set and `retryable`. Only transient
   read or state failures retry, at most 3 times with the same request.
7. Restart eligibility is `true`, `false` or `"unknown"` and lists its
   blockers. Absent host evidence is never assumed. Delegated continuity
   items are reconciled against the supplied worker inventory, and any gap
   blocks. Dispatch eligibility alone keeps `host_confirmed` false; native acceptance
   evidence is imported separately by restart preflight. For AgentsRoom, the adapter is the
   agent procedure in `reference.md` ## AgentsRoom restart: the agent gathers
   the observations with supported tools, so they are model-gathered, not
   host-attested.
8. Holds are scoped (`project`, `topic:<t>`, `terminal:<id>`) and stay in
   force until explicitly released. A corrupt hold fails closed as a project
   hold. A linked worker reads the deduplicated union of its local holds and
   main-checkout holds (the common Git directory's parent); shared metadata or
   hold-read failures block execution. Identical records are reported once with
   the shared origin preferred; differing local records never mask a main hold.
   Writes/releases stay under the explicitly selected root: `hold --scope project`
   from a worktree is an additional worktree-local restriction, while a repo-wide
   hold/release must select `--root <main>`. Releasing a local hold cannot remove
   a main hold. Hold reports include origin, and release guidance names that root.
   Holds gate resume/autoresume and restart; saving and retrieval remain available.
9. A pending restart intent (`restart_preflight` state) blocks re-dispatch
   until the host reconciles it. Besides a caller-supplied
   `host.restart_request_id`, the dispatcher scans `.handoffs/restart/`
   read-only for pending intents of this session, this terminal or the target
   checkpoint; a missing directory is not created. Without an explicit target,
   a supplied `restart_request_id` naming a valid, unexpired pending intent of
   this terminal makes that intent's checkpoint the exact target before the
   ambiguity check, so several open checkpoints do not give
   `target_ambiguous`; any other intent leaves selection unchanged. Only intents relevant to the
   caller (its session, terminal or target, read from the raw record) can fail
   a dispatch: an invalid relevant one gives `dispatch_state_invalid` naming the
   file for resume and retrieve. Save and pressure never block on intent state,
   and unattributable or foreign invalid records are returned in `warnings`. A
   record whose stored root differs from this project (moved or copied) is a
   stale intent, reported in `warnings`, not invalid. The requesting session gets
   `dispatch_pending` for resume. A different session on the intent's terminal,
   taken from the environment (`HANDOFF_TERMINAL_ID`/`AGENTSROOM_AGENT_ID`),
   never from a target path or stdin, is the successor: it gets
   `restart_successor` with the single `restart_preflight.py adopt` command
   (identity, preparation, receipt and resume in one call); a foreign terminal
   naming the checkpoint gets `dispatch_pending`, and `adopt` from it is blocked.
   `confirm_successor` refuses when the environment has no
   terminal, it differs from the intent's, or a supplied `terminal` differs from
   the environment's. `authorized_to_execute` stays false until the successor
   receipt makes the intent `retrieved`. `restart_preflight` itself still refuses a conflicting
   second intent.
   `authorization_source` is informational on every outcome and never execution authority; only `authorized_to_execute` gates execution.
10. An exact target never falls back. Closed history is reached only by an
    explicit target.
11. Delivery is fresh by default. A `reuse` receipt is honored only when its
    `context_hash` matches the live checkpoint snapshot and the session's
    delivery record. A resume `next_action` command requests
    `--fields path,body`; required references, skills, workspace findings and
    verification are always retained.
12. Each result has exactly one `next_action` and a `recheck` list with a
    `state` token. `expect.state` detects changed authorization, holds or
    workspace (`state_changed`). A `run` command is executed exactly as given:
    one call, no pipe, redirect or output extraction; a resume action's text says
    so, because clipped output is invalid JSON that silently drops next steps.
13. A paused owned checkpoint (`automatic_paused`) gets a pointer naming the
    paused state and its reasons, never the lean resume command (Ben's option
    3). Only a trusted checkpoint gets that pointer, and it echoes only
    guard-reason tokens (`[a-z0-9-]+`, at most 120 bytes); an untrusted one gets
    the generic trust-review pointer, so front matter never reaches the model raw. Deferred: give paused state the lean command (option 2) only after the
    legacy injected bare phrase is retired, because the hook cannot tell a
    host-injected restart prompt from a typed one.
14. Known limits. Recovery overrides run before holds are read; like retrieve
    and save they execute no next steps. Retrieve is not strictly read-only: it
    may write `.handoffs/dispatch/identity-*.json` and sidecar lock files.
    `hold`/`release` are unrestricted CLI commands, and
    `"authorization": {"source": "user"}` is asserted by the caller; both rely
    on the model following the skill, not on enforcement.

### 7.3d Handoff v3 proposal — one-call lifecycle (e3ef793a)

**Spec v2 for review; not implemented.** Lane E re-reviews; Ben approves before
implementation. Sections 7.2–7.3c remain shipped behavior. The chat contract is
unchanged. “One-call” means one kit script call; the AgentsRoom profile requires
one additional host tool call. A future bridge is optional, not a prerequisite.

#### Reminder and save entry point

The reminder has one required action: “Run `python3 <pinned-hooks>/handoff_ledger.py
dispatch --input -` once with operation=save and a draft containing topic,
description, body and suggested_skills.” Extend the existing `handoff_dispatch.py`
`save` operation to call `handoff_protocol.save` when a `draft` is supplied;
retain the current routing-only behavior when absent. There is no new `handoff`
CLI surface. The agent supplies reasoning and skill names; scripts own identity,
metadata, publication, ledger, pointer and preflight. Telemetry is diagnostic.

The request extends §7.3c with `draft`: required `topic`, `description`, `body`,
`suggested_skills` (unique installed names; `[]` allowed); optional `ticket`,
`base`, `role`, `predecessor`, `references`, `optional_references`, `continuity` and existing
required `skills`. Request ID remains in the dispatch envelope. Body retains
Objective, Current state and Next steps, authorization, unresolved decisions,
holds and completed effects. Existing validation, trusted inheritance and
verification policy apply. Reuse dispatch/protocol retry identity; unchanged
retries return the same publication, changed content under one ID conflicts.
Task ID is an opaque correlation label, unknown when absent, never authority.
Suggestions are advisory and distinct from required `skills`: missing suggestions
warn; missing required skills block. Preserve the distinction on inheritance.

**AgentsRoom profile:** the script gathers deterministic state and consumes the
current host observations (capability, complete worker/report inventory and
intentional hold) through the existing `host` envelope. Unknown observations
block; `workers: []` means observed empty, not an assumed default. It returns
`{call: "agents_restart", prompt: <exact short-handle prompt>, ...}` only after
all gates pass and dispatch is marked started. The agent's only second action
is that exact host call as its final tool action, then it ends the turn. Required
session status is written before the script call. No intervening agent-led
ledger/preflight steps are required. A blocked result preserves the checkpoint
and names the stage; it authorizes no restart. A future capability-probed bridge
may invoke the host from the same script, preserving the same state contract.

**Slice 1 implemented (8ae43aa3).** `handoff_ledger.py dispatch` accepts `draft`
only with `operation: "save"`; a draft-less save keeps its routing-only result.
Required draft keys are `topic`, `description`, `body` and `suggested_skills`;
optional ones are `predecessor`, `references`, `optional_references`,
`continuity`, `skills`, `verify`, `verify_baseline`, `reason`, and `ticket`,
`base` and `role` (the #95 header fields, format-checked and written by save). Any other key is
`invalid_request`. Without `request_id`, the ID is derived from the session and
the draft fingerprint. An unchanged retry therefore returns the same
publication, even after a restart block, and keeps the predecessor its first
attempt recorded. The draft topic becomes the request topic, and the target
predecessor is injected when the draft omits one. After every existing gate
(identity, holds, pending intents), dispatch calls `handoff_protocol.save`.
`suggested_skills` is written to front matter only when
supplied, so it is sealed with the content. An uninstalled suggestion only
warns. A failed publication returns `publication_failed` (stage `publication`).
Off AgentsRoom, the result is `proceed`/`saved` with path and transition. On
AgentsRoom (`transition.kind == agents_restart`), host observations must
include a boolean `restart_capable`, a `workers` list and a boolean `hold`. Otherwise the
checkpoint stays published and the result is `restart_blocked` with
`stage: host_observations`. Next, `restart_preflight.py restart --path <saved>`
(run out of process; dispatch and preflight never import each other) writes the pointer and intent, then `dispatch_started_at`, before the result
returns:

    {"operation": "save", "request_id": "r-1",
     "host": {"restart_capable": true, "workers": [], "hold": false},
     "draft": {"topic": "parser", "description": "Parser work", "body": "...",
               "suggested_skills": ["tdd"]}}
    -> {"outcome": "proceed", "code": "restart", "call": "agents_restart",
        "agent": "<terminal>", "prompt": "<... adopt h-<24 hex> ...>",
        "request_id": "agentsroom:<sha256>", "save_request_id": "r-1",
        "path": "...", "checkpoint_id": "...", "suggested_skills": ["tdd"],
        "warnings": [], ...}

Any other preflight result is `restart_blocked` with the preflight reason as
`stage`. An identical retry finds the session's own pending intent and blocks
with `dispatch_pending`, so it never emits a second call. A marked intent
(`dispatch_started_at` set, `host_confirmed` false) older than 30 minutes
(`HOST_WINDOW`) is stale. Dispatch announces it as a warning, and a resume or
autoresume returns `dispatch_stale` rather than `restart_successor`. That
intent no longer blocks a new save or a new checkpoint's preflight on the
terminal, but it still guards its own checkpoint. Recovery is a new save or an
explicit adopt.

#### Durable identity and project pointer

Reuse checkpoint_id, the six-field restart binding (checkpoint_id, path,
request_id, root, session, terminal), its full SHA256, deterministic restart ID
and 884a1469 `h-<24 hex>` HOME locator. Agent identity is observed terminal/host
agent ID plus native session. Bind task_id and suggestions to sealed checkpoint
content; do not change the six-field digest or create a parallel ledger.

Proposed index: `<canonical-root>/.handoffs/pointers/<full-binding-sha256>.json`.
Schema version 1 contains checkpoint_id, checkpoint path, root, recorded
workspace, predecessor session, terminal, optional task_id, request_id, full
binding hash, handle and created time. Task/workspace equal the sealed checkpoint;
other binding fields equal the intent. No body or skill text belongs here.
The checkpoint may live in the existing fallback location. The temporary
`context_watch.write_terminal_pointer` remains solely a current-session record.

**Provenance is required before content injection:** resolve the pointer handle
through the private per-user HOME locator, then recompute and match the binding
against pointer, locator and authoritative intent. Require valid local publication
HMAC and user-scope continuation authorization. Refuse pointer, intent or
checkpoint tracked by Git (`git ls-files` in the owning worktree); check each
path, including fallback destinations, and fail closed on indeterminate checks.
A cloned set of pointer/intent/checkpoint files is never automatic resume input.
Project config cannot enable autoresume. Pointer reads use bounded regular files,
no symlink/FIFO following, schema validation and private permissions. A pointer
never supplies live terminal identity. Preserve canonical-root, dependency and
workspace checks; a missing workspace needs explicit reconciliation.

**Slice 2 implemented (cf143858).** `hooks/handoff_pointers.py` writes and
evaluates the index; the pointer stores the checkpoint path as
`checkpoint_path` and `binding_sha256`, `handle`, `created_at` and `version`
alongside the fields above. User-scope continuation authorization is
`AUTORESUME`/`CONTEXT_WATCH_AUTORESUME` or `autoresume: true` in
`~/.context-watch/thresholds.json`. A missing or rotated receipt key, a foreign
HOME, a binding mismatch, expiry, a Git-tracked or indeterminate path, or a
symlink/FIFO/non-private pointer is ineligible with a diagnostic; explicit adopt
is the recovery. No second key exists. Pointer workspace and task_id must equal
the sealed checkpoint's (missing workspace: `workspace-missing`); a symlinked
or foreign index directory, an other-writable `.handoffs/` or a non-private
`.handoffs/pointers/` is `unsafe-pointer-dir` (group write follows the umask).
Both directories are opened without following symlinks and validated with fstat;
pointer enumeration, reads, locks, atomic writes and pruning use the pinned
directory descriptor so a subsequent rename cannot redirect pointer I/O.
The marker re-entry below applies only to intents that record
`pointer_version` and have a matching readable pointer; a pointer file alone is
never authority, so legacy intents stay reconcile-only.

#### Restart and crash boundaries

Under registry/publication locks, validate the published checkpoint and all
preflight gates, derive the binding digest, atomically write/fsync the pointer,
then persist the pending intent and HOME locator, then durably write
`dispatch_started_at` before returning a host call or invoking a bridge.
Factor existing preflight validation/persistence as needed; do not create a
pending intent before a successful pointer write. Conflicting bindings refuse
overwrite. Separate pointer files allow concurrent terminals without clobbering.

| Boundary/failure | Retry/recovery |
| --- | --- |
| Publication failed | Preserve original draft; never clear. |
| Pointer write failed | Keep published checkpoint, return stage pointer; no pending intent exists to block seven days. |
| Pointer exists, intent absent | Inert incomplete index; no startup candidate; identical retry validates and finishes intent creation. |
| Pending intent, locator missing, no dispatch marker | Repair derived locator after validation; identical retry may acquire the one dispatch permission. |
| Pending intent, no dispatch marker | Recheck holds, identity and observations under lock; one retry can atomically write the marker and dispatch once. |
| Marker exists, including response lost before agent receives it | Reconcile host history; never re-emit a call or re-dispatch. |
| Definitive host refusal | Keep checkpoint; new save required after resolving cause. |
| Retrieved intent | Replay only to its recorded successor under existing receipt rules. |

The marker reserves a single attempt, not proof the tool ran. On AgentsRoom it
is written before returning the call to the agent; a crash in that gap is
conservatively uncertain. Uncertain invocation is the normal post-call state:
AgentsRoom schedules replacement only after the turn ends. Its documented limits
are one restart per two minutes and six per hour; live team/QA runs and terminals
shown in another window refuse it. A second call before turn end replaces the
prompt; an unended turn drops the request after 30 minutes. Neither limit expiry
nor a missing successor permits replay of a marked dispatch. Preserve the kit's
stricter existing cooldown/budget guards and native acceptance evidence checks.

Fresh authorization and unioned project/topic/terminal holds must be rechecked
before reserving dispatch; an eventual bridge also rechecks before invocation.
All existing claims, worker report requirements, publication trust, hook drift,
identity and waiting-input gates apply. No setting releases a hold. Keep seven-day
pending expiry, two-hour claim/delivery limits and settled-intent retention.
Expired/invalid relevant entries remain diagnostic without fallback. Settled
pointers are skipped silently and may be pruned with eligible settled intents.
No record of acceptance, scheduling, delivery or a marker proves host replacement.

#### Successor startup and skills

SessionStart records fresh native identity before reading the project index.
Automatic candidates must pass provenance checks above, name **pending**,
unexpired, authoritative intents, belong to this environment terminal and have
a different predecessor session. An exact host request ID selects its intent
before policy; explicit targets never fall back. One candidate loads via `adopt`
with user-scope continuation authorization. Several use `ask` (default, includes
none), `latest-owned` (created time then canonical path), or `manual` (announce).
Project settings may choose policy but never authorize continuation. Foreign
pointers are never automatic candidates. Invalid relevant pointers block automatic
selection with diagnostics; incomplete pointer-without-intent is inert. Zero
candidates use the existing announcer. Native resume/fork/compaction and unrelated
explicit opening tasks preserve their current precedence.

Retrieved intents are excluded from selection; only an exact request by the
recorded successor can replay within the existing receipt contract. Settled
pointers stay silent. Startup calls the adopt implementation programmatically,
emits full delivery once and does not inject another generic resume command.
A later exact restart prompt reuses the same successor receipt without duplicate
body/skills. Keep binding, environment-terminal, native-session, workspace,
verification, dependency, hold and receipt checks. Transcript acceptance,
observed identity and transport delivery remain separate evidence.

**Slice 3 implemented (c25292d2).** `context_watch.handle_session_start`
(startup/clear only) records the session note, then runs
`restart_preflight.py startup --cwd --session` in an 8 s subprocess (below the
10 s hook timeout) when `<project_root(cwd)>/.handoffs/pointers/` exists; a lone
hook skips it. Without user-scope continuation authorization startup returns at
once, silent to the model and logged as a `startup-adoption`/`unauthorized`
event: the manual `agents_restart` prompt (`adopt h-...`) is the only path.
Candidates are `pointers.scan` results whose terminal is the environment
terminal, whose predecessor session differs and whose intent is pending and not
stale-marked (#87 host window). `HANDOFF_RESTART_REQUEST_ID` is the exact host
request: it selects before policy, an unsettled absent or ineligible one reports
`requested-intent-not-eligible` and never falls back, and a settled one is
ignored. Non-quiet diagnostics (all except `incomplete-index` and
`continuation-not-authorized`) block selection as `invalid-relevant-pointer`.
`ask`/`manual` output `choose` with each exact adopt command; `latest-owned`
picks the largest (created_at, pointer path); a malformed value reports
`invalid-resume-pointer-policy` and selects nothing. Only after the hook has
emitted and flushed a resumed delivery does it write the `O_EXCL` marker
`<intent>.startup-<sha256(request_id\0session)[:24]>` beside the intent. A later
exact adopt by that successor returns `already-delivered` without the body;
without the marker (hook killed, output lost) the existing replay delivers the
body again, so a failure duplicates rather than loses it. A scratch cwd under a temp base finds no
index and creates nothing (ce8a995f). The save side keeps one pending intent per
terminal, so several same-terminal candidates come only from older ledgers or
races; tests stage that case.

On AgentsRoom, suggested skills use adopt's existing `skills` context output;
report `delivered-as-context`, not native activation. With user-scope
`activate_suggested_skills=true`, resolve installed catalogs, deduplicate canonical
paths/hashes and deliver advisory suggestions before next-step execution. Attach
source `{kind: "session-handoff", checkpoint_id, request_id, binding_sha256}`
and successor session/terminal; record per-skill hash/result in a delivery
extension without changing receipt tokens. Missing suggestions warn; required
skills retain their current blocking semantics. Fingerprint changes invalidate
retry; partial delivery retries only to the proven successor without restart.
A skill/body grants no permission to install or execute. Native activation is
optional behind a host capability probe, with its own result evidence. Unsupported
native activation uses context delivery; no context delivery capability gives a
skills-stage diagnostic and manual recovery, not a restart loop.

#### Configuration, mandatory gate and hook hygiene

Add keys only in the ticket that implements their consumer, using the existing
flat project `.context-watch.json` and user `~/.context-watch/thresholds.json`:

| Key | Meaning and default |
| --- | --- |
| `handoff_v3` | Boolean false; project opt-in for the lifecycle. |
| `resume_pointer_policy` | ask (default), latest-owned or manual; implemented (c25292d2). |
| `activate_suggested_skills` | Boolean false; skills ticket, user-scope opt-in required. |
| `warning_window_tokens` | Nonnegative lead before resolved §6 threshold, default 30000 (0 disables); advisory implemented (23079574). |
| `mandatory_at_tokens` | Positive absolute occupancy or null (default); Claude validated-runtime save gate (a8550ff5). |

Project overrides global timing/selection values; report effective value/source.
Strict validation names malformed keys and disables automatic v3 transitions;
reserved keys never enter model threshold maps. Project values cannot enable
continuation: use explicit live user authorization or user-scope autoresume,
independent of project auto settings. Suggested activation requires true in user
scope and effective config; project can disable it. Existing environment/threshold
precedence and reserve cap remain §6. Warn at max(0, resolved threshold minus lead).
Implemented advisory: `CONTEXT_WATCH_WARNING_WINDOW` env, then project, then user
file; any malformed present value disables the advisory with one handoff-status
diagnostic. The advisory fires once per window on the notice's occupancy basis,
only while the notice is quiet and the point is above startup context plus the
floor margin, has its own latch (re-armed below min(half the threshold, the
point)), fails closed when that latch cannot be written, and never publishes,
restarts or touches the notice latch. Advisory rows are excluded from `stats`.
Mandatory point must be at least that warning point and within a reported window
minus reserve; invalid combinations disable automatic v3 transitions. Missing
telemetry reports enforcement unavailable, never invented occupancy.

For Claude Code, add a user-scope **PreToolUse** gate that, after the mandatory
point, denies new ordinary work via `hookSpecificOutput.permissionDecision:
"deny"` (or exit 2). Allow only the exact handoff save command, classified read
and status tools and user decisions; arbitrary shell strings are not read tools.
Already issued operations may finish. A **Stop** hook blocks normal completion
until validated publication satisfies the trigger; bound recovery with
stop_hook_active/progress checks so a blocked save permits reporting a human
blocker rather than endless continuation. After publication, release the save
gate without releasing restart holds. These hook controls are documented in the
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks).
Verify Codex's installed hook support separately before advertising the same
gate; use `mandatory-unenforced` only where no supported gate exists.

Implemented gate deployment (a8550ff5): only runtime activation registers the
isolated `mandatory_gate.py` handler, using shared host registration tables.
Checkout/plugin installs remain advisory. Classified tools are the exact builtin
names Read, Glob, Grep, TaskGet, TaskList and AskUserQuestion; MCP names and shell
strings are never inferred to be reads. The printed save command accepts only
literal JSON through a quoted HANDOFF_JSON heredoc, without extra shell syntax.
Private per-project/session gate state records the trigger and save attempts.
Stop blocks once without progress, allows at most two blocks with progress, and
always yields on stop_hook_active. Yielding permits human-blocker reporting;
ordinary tool work stays denied. A trusted matching publication releases the
save obligation without altering restart holds. Missing or stale telemetry
cannot create an obligation; an already latched obligation survives it. A host
reported window caps mandatory timing; Claude's assumed window is never
presented as reported telemetry.

Kit Claude hooks **already** register in user-scope settings through
`install.py claude` (`CLAUDE_CONFIG_DIR/settings.json`). The inspected main and
Lane E worktree project hooks are AgentsRoom-owned `notify-agentsroom.py` hooks.
No kit hook migration is proposed, and user-scope registration is not a new v3
feature. Prompt diagnosis belongs to the separate host investigation; this spec
cannot guarantee prompt-free startup or bypass native trust/approval.

For new user-scope gate/startup handlers, fast no-op before loading project state
when the project has not opted in; isolate failures from foreign hooks. Command
targets and imports must be user-owned, protected from group/other writes and
outside all project trees (validated runtime installation); never import project
code or treat project config as executable. Watcher errors remain isolated;
an active mandatory gate reports state errors and permits recovery/read/status,
not silent ordinary-work authorization. This hygiene requires no rewriting of
AgentsRoom's notification hooks. Keep existing drift checks and profile trust.

Compatibility is additive: old saves/dispatch/adopt, 884 handles and the existing
one-release JSON-binding window remain. Derive pointers only from validated
local records, never alter pending prompts/digests, and apply pending-only
selection. Rollback disables v3 routing/gates while retaining ledger records and
explicit adoption recovery. No production config or live proof is authorized.

#### Ordered implementation slices (proposed, not created)

Each lands alone after spec approval, with regression tests for its state changes:

1. **Save operation:** dispatch/protocol one-call save returning the AgentsRoom
   two-action shape, advisory suggestions; config keys only with their consumer.
2. **Pointers:** HOME-locator provenance, Git-tracked refusal, pending-only
   eligibility, pointer-before-intent and durable dispatch marker.
3. **Startup:** pointer selection and adoption, usable with manual agents_restart
   before any bridge; no duplicate delivery or foreign-terminal selection.
4. **Skills:** suggested context delivery with source identity and fingerprinting;
   native activation only behind a capability probe.
5. **Pressure gate:** warning window and mandatory Claude PreToolUse/Stop gate,
   corresponding config keys, isolated handlers and verified host support limits.
6. **Bridge:** external AgentsRoom feature request; kit integration only when a
   capability probe succeeds. Prompt diagnosis remains a separate existing ticket.

Anchors: `handoff_dispatch.py` `dispatch`/save branch; `handoff_protocol.py`
`save`, `project_setting`, `project_hook_snapshot`, `prepare`, `skill_roots`;
`restart_preflight.py` `preflight`, `restart`, `binding_digest`, `binding_command`,
`adopt_handle`, `adopt`; `context_watch.py` `write_terminal_pointer`,
`handle_session_start`, `main`; `install.py` `claude`, `codex`. Hook modules live
under `skills/session-handoff/hooks/`.

Acceptance covers crash boundaries on both sides of each write, same/changed
retries, concurrent dispatch/terminals, forged or tracked pointers/intents/saves,
HOME mismatch, expiry, settled silence and recorded-successor replay; late holds,
missing workspace, no bridge, skill dedup/source/partial delivery; mandatory
gate allowlist and Stop recovery; fast no-op and failure isolation outside opted-in
projects. Use isolated roots; a live restart proof needs separate authorization.

### 7.4 Shared contract between the agent and chat skills

`tests/verify-skills.py` reads the **Both skills must contain** list below and
asserts each literal appears in both skills. A skill here is its *directory* —
`SKILL.md`, its `branches/` files and the `handoff-template.md` beside it — because the document
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
| `HANDOFF_AUTO` | on in AgentsRoom, else off | `1` = fully automatic (`0` turns the AgentsRoom default off): implies `AUTORESUME`, the newest open handoff is resumed even when several are open, and the session is cleared for you (tmux Stop hook, or the `context_watch.py auto` runner) |
| `HANDOFF_TERMINAL_ID` | `AGENTSROOM_AGENT_ID`; `claude-auto` generates `tmux-<uuid>`; the `context_watch.py auto` runner assigns each runner's children a fresh `runner-<uuid>`, replacing any inherited value | Terminal identity recorded on each save; in fully automatic mode the cleared session resumes only that terminal's newest open handoff (legacy handoffs without a terminal are not auto-picked). Each value is stripped and its first line used; a blank one falls through to the next. Over 120 characters it becomes the first 100 plus `-` and 16 hex of its SHA-256, so long ids sharing a prefix stay distinct |
| `HANDOFF_AUTO_MAX` | `10` | Fully automatic mode: automatic clears in a row per project before the Stop hook stops and tells you (a session that starts near the threshold would otherwise loop); a turn that ends without the trigger resets the count |
| `HANDOFF_STATE_DIR` | system temp dir (`$TMPDIR`) | Absolute path of the directory for per-session temp state (latches, session notes, receipts, pointers, auto counts, the publisher lock). A blank or relative value falls back to the system temp dir; file names are unchanged. Set it in the environment that hooks and your shell share, or publication serialisation is lost |

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
resume --session ID [topic]. Save with --input draft.json (or --input - for stdin); only outcome saved completes a checkpoint.
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
│   ├── SKILL.md                         pointer: fast path, then dispatch
│   ├── branches/                        save.md, resume.md; one loaded per request
│   ├── handoff-template.md              the document's shape, swappable alone
│   ├── reference.md                     install, customizing, mechanics
│   ├── install.py                       registers/removes the hooks
│   └── hooks/
│       ├── context_watch.py             watcher + announcer + analytics
│       ├── handoff_ledger.py            state and chain tracking
│       ├── handoff_dispatch.py          lifecycle dispatcher
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
