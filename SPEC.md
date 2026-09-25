# Session Handoff Suite — Technical Specification

Spec version 1.1 — 2026-09-25
Component versions: `session-handoff` plugin 0.8.0 · `session-handoff-chat` skill 0.4.3 · browser extension 0.1.0

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

**Fire once, but never silently never again.** The threshold trigger fires
at most once per window, enforced by a latch file keyed on session id (or on
the transcript path when the host sends no id, so unknown sessions never
share one latch). The latch is re-armed by a compaction — same session id,
new window — and by failure: a notice that produced no handoff repeats once
occupancy has grown another `CONTEXT_WATCH_REARM_TOKENS`.

**A handoff is evidence, not authority.** The current user's request and the
live workspace win over anything a checkpoint says. Checkpoints record the
user's objective, constraints and corrections, the approval scope, pending
decisions, the workspace and revision, verified outcomes with their evidence,
and unknowns, and they say whether the user parked the work (`reason:
user-parked`) or automatic pressure forced the checkpoint (`context-pressure`,
`compaction`) — in the latter case the authorized task continues. Skills a
handoff names are resolved only against the resuming session's installed
inventory; nothing is installed or run because a handoff named it.

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
| `hooks/context_watch.py` | Watcher (threshold trigger) + announcer (session-start handoff scan) + analytics logger + `stats` CLI. Stdlib Python, single file, shared verbatim between Claude Code and Codex. |
| `hooks/handoff_ledger.py` | State and chain tracking over handoff files. Importable module + CLI (`list`, `resolve`, `resume`, `supersede`, `save-path`, `new-path`). |
| `skills/session-handoff/SKILL.md` | Agent-surface skill: writes the handoff, handles resume, single- and multi-handoff flows. |
| `chat/session-handoff-chat/SKILL.md` | Chat-surface skill: memory ledger, past-chat marker, file fallback, resume resolution. |
| `chrome-extension/` | MV3 extension for Chrome/Edge: pre-populates new-chat initialization prompts on claude.ai. |
| Packaging | `.claude-plugin/marketplace.json` (kit root), plugin manifest, `.plugin` (Cowork), `.skill` (chat), `codex/install.sh`. |

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

The last usage entry is at most one call stale. The missing content is
precisely what the hook is already holding on stdin: the just-produced tool
result (`PostToolUse`) or the new prompt (`UserPromptSubmit`). The watcher
estimates its weight at ~4 characters per token from the first present key
among `tool_response`, `tool_output`, `tool_result`, `prompt`, and adds it to
occupancy before the comparison. The estimate deliberately biases the trigger
early — the correct direction for a quality guard. Disable with
`CONTEXT_WATCH_PENDING=0`. Exact pre-flight counting via a token-counting API
is rejected by design: a hook cannot reconstruct the request payload, and the
early-biased estimate achieves the same protection with no network call.

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
(`$TMPDIR/context-watch-<sanitized session_id>.fired`, containing
`occupancy/limit`) and emits; while the latch exists, all subsequent checks
in that session exit silently until the latch is re-armed (compaction, or
occupancy growth past the latched value with no handoff file written since
the latch, checked against the ledger's write locations).

### 5.2 Injection channels

| Agent | Event | Channel |
| --- | --- | --- |
| Claude Code / Cowork / Codex | any watch event, `warn` mode | JSON `hookSpecificOutput.additionalContext`, exit 0 — the tool result is preserved on both hosts |
| Claude Code / Cowork / Codex | `PostToolUse`, `block` mode (opt-in) | JSON `{"decision": "block", "reason": …}`, exit 0 — on both hosts this replaces the tool result with the reason |

Contracts verified 2026-09-25 against Claude Code 2.1 (`code.claude.com/docs/en/hooks`)
and Codex 0.157 (`codex-rs/hooks/src/events/*.rs`): Codex parses JSON stdout on
every event and records `additionalContext` as a developer message without
touching the tool output; its exit-2/stderr channel and `decision: block` both
replace the result, so the watcher no longer uses exit 2. Since 0.157 Codex
runs every matching hook group (the first-group-only behaviour was 0.145).

The injected message names the occupancy, the threshold and which rule
selected it, the model, the cache-read share, any pending estimate, and the
window (and whether it was reported by the host or assumed), the age of the
usage entry when it is old enough to be stale telemetry, and the exact skill
to invoke, ending with: this is automatic pressure, not the user parking the
work; finish only the action in progress, invoke the skill, write the handoff
with `reason: context-pressure`, stop, begin no new work. When
autoresume is active (§7.3), the message additionally instructs the agent
to tell the user to type `/clear` after the handoff is written — the
cleared session's announcer then resumes the handoff automatically,
closing the loop with a single user keystroke.

## 6. Threshold resolution

First match wins; model ids are matched by longest case-insensitive
substring key ("claude-opus" beats "claude"):

1. `HANDOFF_AT` — env, global absolute; the ergonomic per-launch knob
   (`HANDOFF_AT=20000 claude`), so it beats every map and config file
2. `CONTEXT_WATCH_TOKENS_MAP` — env, e.g. `opus=120000,sonnet=140000,gpt-5.5=160000`
3. `./.context-watch.json` — project-local per-model config
4. `~/.context-watch/thresholds.json` — user-global per-model config
5. `CONTEXT_WATCH_TOKENS` — global absolute, env
6. `"default"` key in the config files
7. `CONTEXT_WATCH_PERCENT` × window — only when PERCENT is explicitly set
8. Built-in default: **130,000 tokens**

Then, whichever rule won, the limit is capped at `window −
CONTEXT_WATCH_RESERVE` (default 20,000) so the handoff itself has room to be
written; a capped limit reports `<rule> (capped from N to window W - reserve
R)` as its source. The window is the host's reported one (Codex
`model_context_window`) or `CONTEXT_WATCH_WINDOW` (assumed; the notice and
the analytics record say which). Non-positive values and a `PERCENT` outside
(0, 100) are ignored and fall through to the next rule. Missing telemetry
(no usage entry) produces no notice — the watcher never guesses a number.

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

Handoffs live at `./.handoffs/<YYYYMMDD-HHMM>-<topic-slug>.md`, named by the
handoff's **ending date/time** so a directory listing is a chronology
(`20260811-1430-auth-refactor.md`). The filename stamp and `created:` front
matter come from one `handoff_ledger.py new-path <topic> [dir] [--json]`
call, not independent timestamp computation. Re-handoff of the same thread
writes a new dated file and marks the previous one `superseded`
(`handoff_ledger.py supersede <old-path>`). Legacy undated
`./.handoffs/<topic-slug>.md` files and `./HANDOFF.md` (topic `default`)
remain supported; their ending time falls back to front-matter `created`,
then file mtime. Front matter carries the state plus a one-line
`description` of what is parked — the announcer surfaces it so handoffs can
be told apart without opening them — plus optional comma-separated `skills`
and `references` lists. `skills` names the skills the parked work depends
on; `references` names other paths the next session must also read when
resolving the thread. Skills cannot be added to or removed from a session's
roster at runtime (the roster is fixed at session start), but skill
*content* only enters context on invocation, so a fresh or cleared session
that loads exactly the listed skills first restores the working context
deliberately rather than by accident:

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

Body sections, in order: Objective; User request and constraints (the
current ask, constraints and corrections, approval scope, pending decisions);
Workspace and revision; Current state (verified outcomes with evidence,
unverified work, unknowns); Decisions and rationale; Files touched; In
flight; Next steps (ordered, concrete, with paths and commands); Gotchas
(including approaches tried and abandoned). Target under
1,500 words, facts a fresh session can verify, no conversational narration.
The body outline is a default, not a contract: the skill is organized into
independently customizable sections (naming convention, document structure,
post-resume actions), and only the front-matter block is load-bearing for
the hooks. The outline and its rules live in `handoff-template.md`, a file
bundled beside each `SKILL.md` rather than inlined in it, so the shape of a
handoff can be rewritten without touching trigger, ledger, or resume policy;
`SKILL.md` §3 delegates to it.
If a `LESSONS.md` exists (e.g. maintained by a mistake-learning skill), new
lessons append there and Gotchas references it rather than duplicating.
Before reporting completion the skill runs `handoff_ledger.py resolve
<topic>` and confirms the `authoritative:` line is the file it just wrote;
a handoff the announcer cannot find is not complete, so a malformed front
matter or a wrong directory is caught by the session that wrote it, not
by the next one.

### 7.2 States

| State | Meaning | Transition |
| --- | --- | --- |
| `open` | Written, not yet transferred; announced every session start | Skill writes the file |
| `resuming` | Claimed by a session (`claim_owner`, `claim_session`, `claim_at`); not offered while the claim is live; offered again as recoverable once the claim is older than `CONTEXT_WATCH_CLAIM_TTL_MIN` (120) or unparseable | `handoff_ledger.py claim <path> --owner <id>` at pickup; `release` returns it to `open` |
| `resumed` | Transferred; silent forever | `handoff_ledger.py resume <path> [--owner <id>]` completes the claim and stamps `resumed:`; refused while another owner's claim is live |
| `superseded` | Replaced by a newer handoff of the same thread; silent forever | `handoff_ledger.py supersede <path>`, run by the skill on re-handoff |
| stale | Older than `CONTEXT_WATCH_MAX_AGE_DAYS` (default 14); still announced, flagged `stale`, never auto-resumed | Time |
| quarantined | Found in the ambiguous pre-0.8 basename-only fallback directory; announced as such, never offered, never deleted | `handoff_ledger.py recover-legacy [dir]` moves it into the workspace directory |

Announced ≠ claimed ≠ transferred; listed ≠ transferred. `claim` is the
recoverable in-progress mark; only an explicit `resume` — run after the
session has actually read and adopted the handoff — is terminal.
`claim` (same owner), `resume` and `supersede` are idempotent, and each
status write is atomic (temp file + rename) and refuses to clobber a file
that changed since it was read. Malformed metadata (unknown `status` or
`reason`, non-ISO `created`, an unclosed fence) is reported per entry in
`problems` and the entry is treated as open; a file with no front matter at
all is the documented legacy form and stays open without complaint. CLI: `handoff_ledger.py list [dir] [--json]
[--max-age-days N]` prints open handoffs newest-first (ended, topic, path,
description); `resolve <topic-or-path> [dir] [--json]` prints the full
oldest-first chain for that topic across all statuses (metadata only — the
resuming session reads the authoritative file, never the chain), with the
most recent entry as `authoritative` and a `must_also_read` list built from
that entry's `references:` alone: each reference is resolved relative to the
handoff's directory or the workspace root, must exist inside the workspace
(absolute paths and traversal are rejected), and at most 8 files / 256 KB
in total are loaded; everything else is returned under
`unresolved_references` with a reason; `resume <path>` marks transfer;
`supersede <path> [--by <new-path>]` marks replacement and, with `--by`,
records the newer handoff as a forward link; `save-path [dir]` prints where
new handoffs should be written: `<dir>/.handoffs` when it exists, otherwise
the per-workspace fallback `~/.claude/handoffs/<basename>-<id>/`, where
`<id>` is the first 12 hex digits of the SHA-1 of the resolved workspace root
(symlinks followed; a git worktree keeps its own identity and records the
main repository as `worktree_of`). `new-path` creates that directory with a
`workspace.json` provenance file (root, gitdir, worktree_of, created). The
pre-0.8 basename-only directory is ambiguous — any project with that folder
name may have written it — so it is quarantined unless its `workspace.json`
names this root; `recover-legacy [dir]` adopts it by moving files (never
deleting). `workspace [dir] [--json]` prints the identity.
`save-path` chooses ONE write location, but every read — `list`, `resolve`,
and the session-start announcer — scans all of them, plus `<dir>/HANDOFF.md`,
so a project that gains a local `.handoffs/` later does not lose sight of the
handoffs it already wrote to the fallback. `HANDOFF.md` carries topic
`default` in either location.
`new-path <topic> [dir] [--json]` takes one clock read and returns
`directory`, `filename`, `path`, and `created` for a new handoff, using the
same directory as `save-path`, a filename of
`<YYYYMMDD-HHMM>-<topic>.md` (suffixed `-2`, `-3`… when that name already
exists, so two handoffs in one minute never overwrite each other), and
`created` formatted `%Y-%m-%dT%H:%M`.

### 7.3 Session-start announcer

Runs on `SessionStart`; skips sessions started with `source: resume` or
`fork` (continuations already carry their context — Claude Code's documented
sources are `startup`/`resume`/`clear`/`compact`/`fork`; Codex's are
`startup`/`resume`/`clear`/`compact`). On `compact` it re-arms the trigger
latch and injects one note: the summary is evidence, re-verify live state,
and an authorized in-progress task continues. On `startup`/`clear` it also
re-arms the latch, then scans every ledger location for open entries and:

- **0 open** — silent.
- **1 open** — announce (topic, stored `ended` date/time, age, path,
  description, flags) with "read it in full and continue", or resume
  immediately without asking when autoresume is active (`AUTORESUME=1`, or
  the legacy `CONTEXT_WATCH_AUTORESUME=1`) and the entry carries no flag —
  a stale, stale-claimed or malformed entry is never auto-resumed.
  When the handoff's front matter names `skills`, the announcement also
  instructs loading exactly those skills (via the Skill tool) before
  resuming, so a `/clear` cycle comes back with the right skills loaded.
- **N open** — enumerate newest-first (topic, stored `ended` date/time, age,
  path, description) with an instruction to present the list and ask which
  to resume before any other work, using an interactive question tool where
  available, with "none" as an option. Autoresume never guesses among
  several.

Because `clear` is not a skipped source, autoresume closes a one-keystroke
cycle: `HANDOFF_AT=<n> AUTORESUME=1 claude` triggers the handoff at the
threshold, the skill tells the user to type `/clear`, and the cleared
session announces and resumes the open handoff automatically.

Handoffs with a live claim by another session are named but not offered;
stale claims are offered as recoverable; quarantined legacy entries are
named with the `recover-legacy` command and never offered. Every
announcement includes the exact `claim` and `resume` commands carrying this
session's id, the rule that named skills resolve only against the installed
inventory, and the defer clause: if the user's opening request is an unrelated explicit task, mention
the open handoff(s) in one sentence and proceed with their task; the ledger
is untouched.

### 7.4 Shared contract between the agent and chat skills

`tests/verify-skills.py` reads the **Both skills must contain** list below and
asserts each literal appears in both skills. A skill here is its *directory* —
`SKILL.md` plus the `handoff-template.md` beside it — because the document
structure was split out so it can be experimented with independently (§7.1);
a literal satisfied by either half passes. The test additionally asserts that
each `SKILL.md` still references its template (so an extracted template cannot
be orphaned) and that the agent template is byte-identical across
`plugins/session-handoff/` and `codex/`. The `session-handoff-chat` skill
version bumps by patch whenever the contract list changes.

**Both skills must contain**

- `## Objective` — the handoff must preserve the overall goal.
- `## Current state` — the handoff must distinguish completed and unverified work.
- `## Decisions and rationale` — the handoff must preserve why choices were made.
- `## In flight` — the handoff must identify the exact interrupted work.
- `## Next steps` — the handoff must give ordered concrete continuation actions.
- `## Gotchas` — the handoff must capture hazards and rework-prevention notes.
- `## User request and constraints` — the handoff must preserve the current ask, constraints, corrections, approval scope and pending decisions.
- `## Workspace and revision` — the handoff must say where the work lives so the resuming session can re-verify it.
- `evidence` — verified outcomes carry the command or test that proves them.
- `reason: context-pressure` — a forced checkpoint is distinguishable from the user parking the work.
- `install` — skills a handoff names are never installed or run on its say-so.
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
coarser than the agent-surface ledger's `resolve` command, which returns a
full oldest-first chain plus a `must_also_read` reference list; the chat
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
| `CONTEXT_WATCH_WINDOW` | `200000` | Assumed window when the host reports none (Codex reports its own); bounds the threshold cap and the PERCENT path |
| `CONTEXT_WATCH_RESERVE` | `20000` | Tokens kept below the window for writing the handoff; every threshold is capped at window − reserve |
| `CONTEXT_WATCH_REARM_TOKENS` | `20000` | Re-notice when occupancy grows this much past the latched notice and no handoff file was written since |
| `CONTEXT_WATCH_CLAIM_TTL_MIN` | `120` | A `resuming` claim older than this is stale and offered again as recoverable |
| `CONTEXT_WATCH_SKILL` | `session-handoff` | Skill named in the injected instruction |
| `CONTEXT_WATCH_MODE` | `warn` | `warn` = additionalContext; `block` = blocking channel on PostToolUse |
| `CONTEXT_WATCH_AGENT` | auto | Force `claude` or `codex` |
| `CONTEXT_WATCH_PENDING` | on | `0` disables the pending-content estimate |
| `CONTEXT_WATCH_LOG` | `~/.context-watch/events.jsonl` | Analytics path; `0` disables |
| `CONTEXT_WATCH_DISABLE` | — | `1` = no-op without uninstalling |
| `CONTEXT_WATCH_MAX_AGE_DAYS` | `14` | Open handoffs older than this are flagged stale (still announced, never auto-resumed) |
| `CONTEXT_WATCH_AUTORESUME` | — | Legacy alias for `AUTORESUME` |

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
Before starting any task, run: python3 scripts/handoff_ledger.py list --json
If open handoffs print, mention them in one line and ask which to resume, or none.
After actually resuming one: python3 scripts/handoff_ledger.py resume <path>
When wrapping up or parking work, run: python3 scripts/handoff_ledger.py new-path <topic-slug> --json
Then write the returned path with front matter (topic, created, status: open,
description) using the session-handoff template.
```

Oz / cloud harnesses: commit the repo-local form of everything (project
`.claude/` hooks config, the two scripts, `.context-watch.json`,
`.handoffs/`) — a fresh managed environment has no user-level config; verify
hooks fire before trusting the threshold there.

## 13. Installation summary

**Claude Code:** `/plugin marketplace add <repo-or-path>` →
`/plugin install session-handoff@session-handoff-kit`. Plugin ships hooks
(`PostToolUse`, `UserPromptSubmit`, `SessionStart` via
`${CLAUDE_PLUGIN_ROOT}`), both scripts, and the skill.
**Cowork:** open `session-handoff.plugin` (built from a checkout with
`bash scripts/package.sh`), one-click install (shared plugin schema).
**Codex:** `bash codex/install.sh` — copies both scripts to
`~/.codex/hooks/`, the skill to `~/.codex/skills/session-handoff/`,
generates `~/.codex/hooks.json` with absolute paths (wrapped shape; some
builds expect event names at top level — remove the wrapper if hooks don't
register), then enable `[features] hooks = true` (older builds:
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
identical semantics — normalize via per-model entries. `CONTEXT_WATCH_MODE=block` replaces one tool result with the notice on both
hosts, which is why it is opt-in. The model-self-report
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
├── plugins/session-handoff/             Claude Code + Cowork plugin
│   ├── .claude-plugin/plugin.json       manifest; no "hooks" field (hooks.json
│   │                                    is auto-loaded; re-referencing it is a
│   │                                    duplicate-hooks install error)
│   ├── README.md                        plugin-level install notes
│   ├── hooks/hooks.json                 PostToolUse, UserPromptSubmit, SessionStart
│   ├── hooks/context_watch.py           watcher + announcer + analytics (shared)
│   ├── hooks/handoff_ledger.py          state and chain tracking (shared)
│   ├── hooks/thresholds.example.json    per-model starting values
│   └── skills/session-handoff/
│       ├── SKILL.md                     trigger, naming, resume, mechanics
│       └── handoff-template.md          the document's shape, swappable alone
├── codex/
│   ├── install.sh
│   ├── hooks/                           same two scripts + thresholds example
│   └── skills/session-handoff/          same skill (both files, byte-identical)
├── chat/session-handoff-chat/
│   ├── SKILL.md                         chat-surface skill
│   └── handoff-template.md              chat document shape, swappable alone
└── chrome-extension/                    new-chat initialization prefill

`dist/` (the built `.plugin`/`.skill` artifacts) is generated by
`scripts/package.sh` and gitignored.
```
