# Session Handoff context-delivery evaluation

Date: 2026-09-28. Code baseline: `a57e23a` (0.11.0). The initial evaluation below describes that baseline; the implementation follow-up records the v0.12.0 changes.

## Conclusion

The sampled handoff bodies are not oversized. The larger opportunity is to bound the complete resume package: workflow instructions, inherited skills, required references, ledger output, verification output, and recovery searches. Some of that material is necessary; reducing it must preserve authorization, constraints, verified state, and the next action.

The eleven determinism findings from the preceding audit have been filed as TODO items in the session-handoff-kit AgentsRoom backlog; see [the ticket index](determinism-backlog-2026-09-28.md). The new context findings below are recommendations, not implemented changes.

## Evidence and limits

Read-only analysis covered the 25 top-level JSONL files in the local Claude project transcript directory, 14 saved handoffs, and the context-watch event log. Four transcript openings were inspected closely after scanning handoff-related tool calls across the project logs. The event log contained 350 records, of which 12 named this project's exact working directory, dated 2026-09-27. This is a historical convenience sample, not a representative benchmark across clients. Existing Codex logs were discoverable but were not used for the measured examples.

Local evidence:
- Claude transcripts: `~/.claude/projects/-Users-bens-Development-session-handoff-kit/`.
- Handoffs: `~/.claude/handoffs/session-handoff-kit/`.
- Telemetry: `~/.context-watch/events.jsonl`.

No raw transcript copies or unrelated personal content are included here. Memory-service recall was unavailable; this report makes no claim of remote memory synchronization.

Handoff body word counts exclude front matter: 390–1,093 words, median 804, across 14 files. None exceeds 1,500. Five files declare references; three point to earlier handoffs. One referenced research file is now missing from its former worktree path.

Input-token measurements below sum Claude's `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` at the specified model calls. Repeated usage values for blocks of the same call are not summed. These are context-size changes, not billed-token totals, and include intervening reasoning, messages and tool results; they do not isolate avoidable tokens.

## Findings, ordered by expected leverage

### C1 — High: budget the full resume package and load skills by phase

Observed: transcript `1f09a4d2-0994-4af9-bb18-319cfa89c1d7.jsonl` begins with “Retrieve your handoff” at line 10. Lines 33–39 load session-handoff and writing-for-agents: 8,354 and 10,809 characters of injected skill text. The handoff read result at line 54 adds 7,772 characters. Input context rises from 24,758 tokens at line 31 to 35,969 at line 62: +11,211 during preparation, before the mark-resumed command's result.

These skills were relevant to the subsequent writing task; this does not prove either can be removed entirely. It does show that eager skill loading is a substantial portion of the resume package, including when the user's current request is retrieval.

Current code gathers every Claude Skill call from the transcript (`handoff_ledger.py:transcript_skills`); the startup hook says to load the listed skills first, and SKILL section 5 also requires loading them. There is no shared budget covering those skills plus references and the checkpoint. Section 2 says never drop a collected skill, while section 5 permits dropping skills the work no longer depends on.

Proposed outcome: separate retrieval preparation from execution preparation; load each required skill once per session by canonical identity, at the phase that needs it. Persist required dependencies, not all historically used skills. Produce a deterministic package manifest with estimated size per component and a total limit; preserve required policy even when it needs an explicit budget exception.

Acceptance: replay this opening and a retrieval-only fixture; verify no duplicate canonical skills, no unrelated historical skills, and deferred execution guidance. Compare measured input growth and successful continuation, not just shorter text.

### C2 — High: required references can dwarf the checkpoint

Observed: `f5ec0613-c999-45e8-92f3-4bee078a48f9.jsonl` loads the current ports handoff at lines 34–35, then an earlier plan at lines 46–47; the latter result adds 4,723 characters. Input context grows from 47,196 at line 30 to 57,676 at line 49 during retrieval/preparation. Some predecessor content may be necessary; the trace establishes additional loading, not that every predecessor fact is redundant.

The 11:06 ports checkpoint is 6,226 bytes and still requires a 6,694-byte earlier plan. Thus the reference more than doubles the checkpoint-file payload. The current skill orders every `must_also_read` reference to be read before continuing.

Current limits are eight references / 256,000 bytes, excluding the checkpoint and skills. An isolated reproduction with a 250,000-byte reference confirms it is returned in `must_also_read`. Bytes are not tokens; this can consume substantial context even when the body is short.

Proposed outcome: distinguish required excerpts from optional background, consolidate essential predecessor facts into the authoritative snapshot, and enforce an aggregate resume budget. Read sections/ranges when sufficient; deduplicate canonical paths and avoid recursive history expansion. Missing references should produce an exact outcome, not broad searching.

Acceptance: large references, aliases to the same file, old handoff references, missing worktree paths, and a short checkpoint with a large dependency set. Required constraints must survive excerpting.

### C3 — High: empty retrieval creates costly recovery searches

Observed: `d4a89350-afe6-4926-aa51-d18c56b7aa8c.jsonl` starts with “resume” at line 8. It loads 8,290 characters of skill text, locates ledger/directories, and prints historical status/description data. At line 47 it reports all eleven handoffs already resumed or superseded. Input context grows from 21,304 at line 26 to 28,584 at line 46: +7,280 to reach a no-work outcome.

Our preceding interactive audit also required manual historical inspection after an empty list. This is a concrete context cost of missing deterministic outcomes, not a need for a more elaborate handoff narrative.

Proposed outcome: a single status command returning `none`, `claimed`, `stale`, `error`, or `available`, with bounded metadata and exact next action. Empty retrieval should not require loading historical bodies or the full wind-down workflow.

Acceptance: empty and closed-only fixtures return one bounded response; assert no history/body reads and no unnecessary skill loads. Covered by backlog item `886ba881-f81a-4953-95d9-9f84196328ab`.

### C4 — Medium: ledger and announcement output is not bounded as a package

Code finding: normal `resolve` prints every historical record's metadata; JSON includes the complete chain. The skill says to read only the authoritative body, but the metadata chain is still delivered. An isolated fixture with 100 historical records produced 58,233 characters of JSON from `resolve`. No comparable live chain of that size was found.

Startup limits announcements to five records, but does not limit each description or skills field by characters/tokens. Listing limits therefore do not establish a payload limit.

Proposed outcome: compact default resolution containing only the authoritative record and required references; explicit paginated history mode. Bound announcement fields and total serialized output without truncating paths into unusable commands.

Acceptance: thousands of historical records, long descriptions/skill lists, malformed metadata, and five maximal-size entries. Default response size must stay bounded as history grows.

### C5 — Medium: the body limit diagnoses oversize but does not prevent loading it

All sampled bodies comply, so this is a code-level exposure, not an observed production incident. A synthetic 1,610-word body emits a problem but remains resolvable. The agent can still read the full file. The template's roughly 1,000-word target may also encourage filling sections for a small checkpoint.

Proposed outcome: size based on task complexity, a concise core snapshot, and optional detail outside the mandatory load. Validate before publication and before serving the resume package. Oversize recovery must preserve the last valid checkpoint and surface an actionable result; do not simply discard context or block saving without a recovery path.

Acceptance: short tasks need no filler; oversized body/code blocks cannot bypass the effective package limit; malformed files cannot force unlimited output. Verify retention of approval boundaries and unresolved decisions.

### C6 — Medium: required verification can import unlimited output

Code finding: SKILL section 5 requires the recorded verification command, or the agent's choice of smallest relevant check. There is no standard compact result, output cap, or durable log pointer. Some sampled sessions already use `tail`, which is an agent workaround and can mask an upstream exit status when a pipeline lacks pipefail.

Proposed outcome: a verification runner preserving the true command exit status and storing complete output outside the prompt. Return a short pass summary or bounded failure excerpt plus the log location. Avoid forcing a full suite merely to inspect a handoff; execution preparation and retrieval are different phases.

Acceptance: noisy passing/failing commands, pipeline failures, timeouts, and missing verification commands. Failure must remain visible even when most output is withheld.

### C7 — Measurement gap: startup context is a separate cost

The six project trigger records with a recorded startup floor report 21,384–46,308 tokens. Those are session-wide startup measurements, not handoff payload attribution. They must not be reported as handoff waste or claimed as recoverable by shortening a checkpoint.

Existing telemetry records trigger occupancy and floor but not resume components, bytes read, skill duplication, verification output, or context at preparation completion. Add per-resume component metrics and record whether cached content remains in context; caching changes billing, not context occupancy.

Acceptance: controlled no-handoff, empty-retrieval and successful-resume fixtures distinguish baseline from incremental resume growth. Log only IDs, sizes, timing and outcomes by default, not document contents.

## Improvement sequence

1. Deliver explicit lookup outcomes and correct lifecycle state first; this removes demonstrated recovery searches.
2. Introduce a bounded resume package with compact resolution, required excerpts, and phase-specific skill loading.
3. Add safe size enforcement and bounded verification output.
4. Replay the sampled openings and fixtures, comparing context growth, preparation latency, retained constraints, and continuation correctness.

Existing strengths to preserve: authoritative-body-only guidance, reference caps, five-entry announcements, bounded second notices, and startup-floor protection. None currently provides a total resume-package budget.

## Implementation follow-up — 2026-09-28

All eleven determinism topics and context findings C1–C7 are implemented for v0.12.0. The preferred CLI is now lookup → prepare (read-only or authorized execution) → verify when required → acknowledge, with save for validated publication.

- C1/C2: preparation budgets the serialized package, workflow instructions, required references and installed execution skills; aliases and already-loaded skills deduplicate. Retrieval emits no execution skills. Required references support line ranges; background can remain optional.
- C3/C4: explicit empty/stale/claimed/error/incomplete outcomes, no checkpoint-skill trigger for empty startup, deterministic explicit-command routing, compact resolution, bounded metadata and paginated history.
- C5: publication validates 1,500 words / 24,000 bytes; preparation defaults to a 32,000-byte total budget. Oversize preserves the draft/checkpoint and returns an action instead of truncating constraints. Publication receipts make interrupted saves retryable without premature automatic clearing.
- C6: verification preserves pipeline failures, bounds returned output, logs the complete output, and times out recoverably. Changed dependencies and superseded checkpoints cannot be acknowledged.
- C7: content-free resume metrics include component sizes and separately labeled available startup/later host input measurements; bytes/4 is explicitly an estimate.

Validation: 25 public protocol regression tests plus ledger, watcher, skills, packaging, trigger and extension suites pass. The plugin was extracted and its CLI lookup exercised in an isolated directory. Tests cover concurrent owners/saves, both destinations denied, publication interrupted before its receipt, repeat acknowledgment, reference/metadata limits, and verification failure/timeout.

Historical replay used an isolated copy of the 14:36 restructure checkpoint, leaving the original untouched. Retrieval returned 7,553 bytes and zero execution skills; total accounted package including workflow instructions was 13,522 bytes. Execution preparation returned 18,783 bytes with one execution skill; total accounted package was 24,752 bytes. These are byte measurements of the new command, not a live model benchmark or an exact comparison with historical input-token growth.

Operational scope: legacy direct-write/state commands remain for compatibility, but automatic clearing requires the new validated publication receipt. Keep generated `.published` sidecars with their checkpoints. New regression tests run in CI. Validation did not include a live provider session restart or Portskill re-vendoring.
