# Handoff context waste: follow-up evidence

Reviewed 2026-09-28 against released commit `e4f374f` (v0.12.0). Review only; no runtime changes.

## Scope and limits

- Scanned 58 Codex JSONL files: September 28 files under `~/.codex/sessions/`, plus available AgentsRoom-managed Codex session files. Thirteen contained the targeted handoff command/message patterns. Inspected selected openings closely; counts are a convenience sample, not all machine history. This audit's own session is included in discovery but its investigative reads are not treated as ordinary handoff overhead.
- Scanned short handoff-message blocks in the twelve most recently modified top-level Claude transcripts; inspected two real September 28 checkpoint-recovery sequences in `clearmethodstudio`.
- Revisited the earlier project audit and fourteen historical project checkpoints. Project Claude logs predate this release.
- Inspected current skill, template, hooks and protocol. Ran isolated, content-free hook probes and a synthetic retrieval-to-execution sequence; no real checkpoint was changed.
- Snapshot of resume telemetry: 59 records, 49 clearly from tests (`verify-*` or `runner`), zero successful `prepared`/`retrieved` records. This cannot establish production resume savings. No raw transcript copies, credentials or unrelated task details are reproduced here.
- Memory recall was unavailable. Findings are retained here; no remote-memory synchronization is claimed.

Character/byte counts below are not exact token costs. Claude input measurements include cache-read and cache-creation input; Codex `input_tokens` already includes cached input. Context growth includes intervening reasoning/messages and must not be equated with avoidable or billed tokens.

## 1. High: agents reverse-engineer the save format under context pressure

Observed in Claude session `c0ca64f2-7760-41a4-9815-0072431f9e68.jsonl`, lines 427–484, under `~/.claude/projects/-Users-bens-Development-clearmethodstudio/`:

- A stop warning required publication through `save`, after an agent had used legacy `new-path` and direct Markdown writing.
- The next nine tool calls included help, unsuccessful source searches, dispatch tracing, reading the save implementation, and finally converting the document into a JSON draft.
- Those calls returned 8,216 characters. The measured model input grew from 139,088 to 144,056 tokens between calls at lines 430 and 479 (+4,968; not all attributable to unnecessary reads).

A second session, `8a1e00c0-5ceb-4a97-9b25-98794a0f5504.jsonl`, lines 897–966, repeated the pattern: ten calls, 12,056 returned characters, a malformed source-inspection command and a rejected draft missing mandatory headings. Its measured first/last input across the recovery/follow-through sequence grew by 8,171 tokens.

These occurred while the implementation was changing before release. They demonstrate migration/recovery waste, not a controlled post-release failure rate. The same discoverability gap persists in current code: `save --help` documents flags but not the JSON schema or template path, and the stop warning supplies neither. The template already explains the schema, but the recovery action does not direct agents there.

Fix direction: make `save --help` or a small `save --template` response self-contained; give the stop recovery message the exact template command/path. Consider a deterministic, explicit legacy-document import path. Do not force source inspection or weaken publication checks.

Acceptance: an agent with old in-context instructions can recover using only the recovery response and public CLI; no implementation reads, guessed fields, or hand-written front matter.

## 2. High: informational hooks still activate the complete checkpoint skill

Current code, independently reproduced:

- `handle_session_start(source='compact')` emits the skill-trigger label while telling the agent to continue authorized work.
- `build_floor_message` uses that label while stating that no handoff is needed.
- The unrelated-opening branch and outer service-failure response also retain it.
- The installed skill description says to use the skill immediately whenever that label appears. The installed skill is 5,892 bytes.

The isolated compact response was 399 bytes including its JSON wrapper; the floor message was 299 bytes. The main waste is the potential 5.9 KB instruction load and resulting deliberation, not these short notices. Current probes establish the trigger mismatch; they do not establish how often clients actually reload the skill.

An ordinary startup with an available handoff also uses the trigger before the user's first request establishes relevance. SessionStart and opening-prompt routing can both announce the same checkpoint. This creates an eager-loading and repeated-announcement path even for unrelated work.

Fix direction: reserve the activation label for an actual save/retrieve/resume operation. Use a neutral status for compaction, floor/configuration advice, failures and unrelated work. Offer a short availability hint at startup; deliver the command/details once relevance is known.

Acceptance: informational/unrelated cases do not activate the skill or wind-down protocol; explicit handoff actions still do. Measure instructions loaded, not merely message length.

## 3. High: AgentsRoom's transcript handoff bypasses the kit's bounded protocol

Observed in Codex file `rollout-2026-09-27T15-49-55-01a0e46a-bd85-7063-b4ec-c90fe8151748.jsonl`, under `~/.agentsroom/codex/proj-1790538559716-alsfvs/sessions/2026/09/27/`:

- Line 9 tells the receiver to read the predecessor's full terminal transcript, pick up exactly where it left off, then ask the user what to do next.
- The referenced `.agentsroom/handoff-transcript-agent-1790538559736-h68nzh.txt` is 2,379 bytes / 48 lines. It contains UI chrome, a model tip, the handoff-writing request, skill-reading activity, and `+223 lines` of collapsed tool output. It ends while work is still in progress.
- The receiver performs tool discovery, loads the transcript and the handoff skill, and searches for a summary that does not exist. It concludes there were no completed project changes. Input rises from 21,129 at line 17 to 26,523 at line 33 (+5,394 across the sequence, not pure waste).

Another Codex file, `rollout-2026-09-27T09-19-57-01a0e305-b55e-7cb1-9df1-dee5b17aa10b.jsonl`, records a replacement-summary request in an otherwise empty task context. The resulting summary describes the task of making a summary. This is a handoff with no useful project state.

The scanned Codex sample contains eleven replacement-summary requests and two transcript-resume prompts. That does not mean every replacement was unnecessary.

Fix direction: have the host wait for a completed, structured checkpoint and pass its identity/status to the receiver. Represent `no active task` directly. Keep terminal transcripts optional forensic evidence; do not treat a rendered screen capture with collapsed output as an authoritative checkpoint. Resolve the contradictory continue-versus-ask instruction in the host's transition policy.

Acceptance: empty replacement requires no skill/file recovery; interrupted summary creation yields an explicit incomplete state; successful transfer uses the bounded checkpoint, with transcript access only on demand. This fix belongs partly to AgentsRoom, outside this repository.

## 4. Medium: retrieve, then resume repeats the same content

Current protocol reproduction in a temporary project:

| Call | JSON output bytes |
| --- | ---: |
| `prepare ... --session audit` | 4,073 |
| `prepare ... --session audit --execute` | 4,033 |

Both responses contained the identical body and required reference: 3,064 bytes of repeated substantive content. The sequence is legitimate when a user first asks to see the checkpoint and later authorizes continuation. `prepare` has skill deduplication but no session receipt for already-delivered body/reference content. A per-call 32 KB limit does not bound cumulative replay across calls.

Fix direction: return a retrieval receipt with dependency fingerprints. On authorized continuation in the same context, validate that receipt and return only new execution instructions/skills, workspace changes and acknowledgment requirements. After compaction, a new session, or changed content, send the necessary content again.

Acceptance: unchanged retrieve→resume delivers the body/references once; changed dependencies or lost context cause a full safe reload. Do not merely trust a client-supplied boolean saying the content is loaded.

## 5. Medium: workflow and template overhead remains broader than each operation

The current 5,892-byte skill carries approximately 1,564 bytes of wind-down instructions, 1,281 bytes of retrieval instructions, and 1,963 bytes of continuation instructions, plus shared policy/front matter. Retrieval-only sessions consume substantial instructions for phases they are explicitly forbidden to perform yet.

The template starts with stale guidance (`SKILL.md §3` delegates here; “Keep the front matter”), followed by the new JSON-save contract, followed by a full manual front-matter example. Actual wind-down delegation is now §1 and metadata is generated by `save`. These competing presentations can prolong decisions about what to write and encourage the legacy path seen above.

Fix direction: retain a short shared policy/router; expose compact operation-specific public contracts. Make the save template primarily the JSON draft contract; put rendered-document details in reference material. Preserve authorization and failure rules even when shortening.

Acceptance: compare small retrieve/save/resume fixtures for instruction bytes and successful behavior. No omitted constraints or extra source lookups. Existing checkpoint body lengths alone do not justify aggressive compression.

## 6. Medium: test telemetry obscures real context delivery

Forty-nine of the 59 resume records are identifiable fixture saves. There are no successful production prepare measurements in this snapshot. The log repeats explanatory `context_note` prose per record. Disk verbosity alone is not LLM context waste; it becomes such when agents use raw `cat`/`tail` output as their analytics interface.

Fix direction: disable production telemetry in all tests or use a dedicated sink; record origin/version explicitly; add a compact report grouped by operation/outcome/version with test exclusion. Keep the explanatory text in the schema/report header.

Acceptance: running the test suite leaves the production log unchanged; a report separates live and synthetic events and reports missing evidence instead of a misleading success/savings rate.

## Priority

First fix public save-format discoverability and neutral informational hook labels. Next address AgentsRoom's empty/incomplete transcript transfers and repeated retrieve→resume payloads. Then reduce phase-specific instructions and clean up measurement. Preserve the v0.12.0 ownership, publication, verification and authorization safeguards.

## Resolution follow-up

The five kit-owned findings are implemented for v0.13.0:

- Save help documents draft fields and `save --template` emits valid JSON. Stop recovery names that exact command.
- Informational hooks use neutral labels; ordinary startup sends only an availability hint. Explicit retrieval/resume retains detailed routing.
- `prepare --reuse-receipt` validates a server-side same-session delivery receipt, content fingerprints and expiry. Compaction invalidates it. Reused content remains in the context budget; invalid receipts reload in full.
- The main skill is 2,947 bytes (previously 5,892); continuation-only guidance is disclosed separately and accounted for during execution preparation. The template no longer presents manual front matter as the save format.
- Tests isolate/disable production telemetry. New events identify origin/version; `report` groups live records, excludes known tests and identifies legacy/unavailable evidence without dumping logs.

Validation: 30 public protocol tests pass, along with ledger, watcher, skill, packaging, trigger and extension suites. Production event/resume log hashes were unchanged across the verification run. An extracted-plugin save→retrieve→prepare→acknowledge smoke test passed: retrieval returned 4,193 bytes; authorized continuation returned 819 bytes while reusing 3,064 bytes of unchanged substantive content. These are fixture byte measurements, not a live model benchmark.

The AgentsRoom-owned transition remains upstream: the user confirmed no source checkout is available. [A developer issue](agentsroom-transfer-issue.md) contains evidence, a proposed transition contract and acceptance checks. No installed application binary was modified and no issue was sent externally. These follow-up fixes are included in v0.13.0.
