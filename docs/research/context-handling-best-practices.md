# Context-boundary best practices for coding agents (primary sources, 2024–2026)

Researched 2026-09-27. All vendor docs were fetched live (Claude Code and Codex docs as raw markdown), so the doc facts below reflect the pages as of that date. Evidence labels:
**VD** = vendor doc (normative, describes shipped behaviour) · **VB** = vendor engineering blog (vendor's own experiments/opinion) · **S** = peer-reviewed or preprint study · **P** = practitioner opinion.

Scope note: this is a checklist to audit the kit against. I did not look at the kit's code.

---

## Sources (fetched)

| Short name | URL | Date | Type |
|---|---|---|---|
| Anthropic context engineering | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents | 2025-09-29 | VB |
| Anthropic long-running harnesses | https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents | 2025-11-26 | VB |
| Anthropic building effective agents | https://www.anthropic.com/engineering/building-effective-agents | 2024-12 | VB |
| Anthropic writing tools for agents | https://www.anthropic.com/engineering/writing-tools-for-agents | 2025 | VB |
| Anthropic context-management launch | https://claude.com/blog/context-management | 2025-09-29 | VB |
| CC hooks reference | https://code.claude.com/docs/en/hooks | live | VD |
| CC hooks guide | https://code.claude.com/docs/en/hooks-guide | live | VD |
| CC context window / what survives compaction | https://code.claude.com/docs/en/context-window | live | VD |
| CC model config (auto-compact window) | https://code.claude.com/docs/en/model-config | live | VD |
| CC statusline (context_window fields) | https://code.claude.com/docs/en/statusline | live | VD |
| CC memory (CLAUDE.md, auto memory) | https://code.claude.com/docs/en/memory | live | VD |
| CC best practices | https://code.claude.com/docs/en/best-practices | live | VD |
| CC how it works | https://code.claude.com/docs/en/how-claude-code-works | live | VD |
| CC changelog | https://code.claude.com/docs/en/changelog | live | VD |
| Agent SDK sessions | https://code.claude.com/docs/en/agent-sdk/sessions | live | VD |
| API context editing | https://platform.claude.com/docs/en/build-with-claude/context-editing | live | VD |
| API compaction overview / threshold / on-demand | https://platform.claude.com/docs/en/build-with-claude/compaction (+ `/compaction-threshold`, `/compaction-on-demand`) | live | VD |
| API memory tool | https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool | live | VD |
| API context windows (context awareness) | https://platform.claude.com/docs/en/build-with-claude/context-windows | live | VD |
| API prompting best practices (multi-window workflows) | https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices | live | VD |
| API prompt caching | https://platform.claude.com/docs/en/build-with-claude/prompt-caching | live | VD |
| Codex hooks | https://developers.openai.com/codex/hooks | live | VD |
| Codex config reference | https://developers.openai.com/codex/config-reference | live | VD |
| Codex AGENTS.md guide | https://developers.openai.com/codex/guides/agents-md | live | VD |
| Codex slash commands | https://developers.openai.com/codex/cli/slash-commands | live | VD |
| Codex memories | https://developers.openai.com/codex/memories | live | VD |
| Chroma "Context Rot" | https://www.trychroma.com/research/context-rot | 2025-07-14 | S (tech report) |
| Lost in the Middle (Liu et al., TACL) | https://arxiv.org/abs/2307.03172 | 2023/24 | S (pre-2024 arXiv; TACL 2024) |
| NoLiMa (Modarressi et al., ICML 2025) | https://arxiv.org/abs/2502.05167 | 2025-02 | S |
| Context Length Alone Hurts (Du et al., EMNLP Findings 2025) | https://arxiv.org/abs/2510.05381 | 2025-10 | S |
| Manus context engineering | https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus | 2025-07-18 | P |
| Cognition "Don't build multi-agents" | https://cognition.com/blog/dont-build-multi-agents | 2025-06-12 | P |
| LangChain context engineering | https://www.langchain.com/blog/context-engineering-for-agents | 2025-07-02 | P |

---

## A. Trigger timing and measurement

**A1. Trigger on absolute tokens, well before the window is full, not on percentage of a 1M window.**
Why: degradation tracks input length, not fraction of the window. Chroma found performance "grows increasingly unreliable as input length grows" across 18 models, and Du et al. found 13.9–85% drops with length even with perfect retrieval and masked distractors. NoLiMa: 11 of 13 models fell below 50% of baseline by 32K tokens. The API's own defaults sit in the same range: context-editing trigger 100k input tokens, threshold compaction 150k (minimum 50k).
Sources: Chroma; arXiv 2510.05381; arXiv 2502.05167; API context editing; API compaction-threshold.
Evidence: S + VD. The kit's 130k default falls inside the vendor default range. No source gives an optimal number.

**A2. Count context as `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` from the latest main-thread API response. Exclude output tokens.**
Why: this is how Claude Code computes `used_percentage`. It says to use "the same input-only formula" when you calculate it yourself.
Source: CC statusline, "Context window fields". Evidence: VD.

**A3. Treat the transcript as a lagging, unstable input.**
Why: in Claude Code, the transcript "is written asynchronously and may lag the in-memory conversation". Codex says its transcript format "isn't a stable interface for hooks and may change over time". The measurement should fail safe (skip, don't fire) when usage is missing or unparseable. `current_usage` is also `null` right after `/compact` until the next API call.
Sources: CC hooks (common input fields); Codex hooks (common input fields); CC statusline. Evidence: VD.

**A4. Keep sidechain and advisor turns out of the main-context number, and re-baseline after compaction or /clear.**
Why: Claude Code itself shipped a bug that counted advisor-tool turns at about twice their real size, which "made auto-compact fire at about half the real window" (fixed in 2.1.273, Sept 2026). Subagent usage arrives separately in PostToolUse `tool_response.usage`.
Sources: CC changelog 2.1.273; CC hooks (Agent tool_response). Evidence: VD.

**A5. Make sure the handoff trigger fires before native auto-compact, and know where that is per model.**
Why: Claude Code's default auto-compact now fires near the model limit: about 967K on native 1M models, and at the 200K boundary for 200K models or when `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` is set. Users can move it with `/autocompact`, `--autocompact`, `autoCompactWindow` or `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100K–1M). Codex uses `model_auto_compact_token_limit`, which is unset by default and falls back to model defaults. It also has a new `model_auto_compact_token_limit_scope` (`total` | `body_after_prefix`). If a user sets auto-compact below the kit's threshold, compaction wins and the handoff never fires.
Sources: CC model-config; Codex config-reference. Evidence: VD.

**A6. Leave enough headroom for the handoff to be written.**
Why: writing the handoff costs tool calls and output. Anthropic tells agents not to "run out of context with significant uncommitted work". The API compaction docs list `model_context_window_exceeded` as a failure when there's "no room for the summarization prompt".
Sources: API prompting best practices; API compaction-on-demand. Evidence: VD.

**A7. Fire once per threshold crossing (a latch), and give up after repeated failures.**
Why: Claude Code added circuit breakers for its own compaction: it stops after 3 consecutive failures and detects a "thrash loop" after 3 immediate refills (changelog, ~Mar–Apr 2026). Stop-hook continuations are capped at 8 consecutive and expose `stop_hook_active`.
Sources: CC changelog; CC hooks (Stop decision control). Evidence: VD.

## B. What goes into the handoff

**B1. Aim for maximum recall first, then tighten for precision.**
Why: Anthropic's compaction guidance: "Start by maximizing recall … then iterate to improve precision." Keep "architectural decisions, unresolved bugs, and implementation details", and drop redundant tool output.
Source: Anthropic context engineering. Evidence: VB.

**B2. Cover, at minimum: state, next steps, learnings, decisions, and the user's latest open request.**
Why: the API's default compaction prompt asks for "the state, next steps, learnings etc.". Its custom-instruction example keeps "every entity and field name agreed so far, and the user's latest open request". Cognition names "key details, events, and decisions". Claude Code's own compaction keeps "code patterns, file states, and key decisions".
Sources: API compaction-threshold; API compaction-on-demand; Cognition; CC best practices. Evidence: VD + P. The kit's sections cover all of these.

**B3. Size the handoff like a distilled sub-agent return: about 1–2k tokens.**
Why: Anthropic says sub-agents return a "condensed, distilled summary … (often 1,000–2,000 tokens)". Claude Code's CLAUDE.md guidance is under 200 lines, because "longer files … reduce adherence". Auto-memory `MEMORY.md` loads only its first 200 lines or 25KB.
Sources: Anthropic context engineering; CC memory. Evidence: VB + VD. 1,500 words is about 2,000 tokens, at the top of this range.

**B4. Keep verifiable state separate from narrative, and prefer pointers to checkable artifacts (git SHA, test status, file paths) over prose claims.**
Why: Anthropic's harness uses git history, a progress file and a feature list with a `passes` boolean. It says to mark a feature done "only after end-to-end verification confirms it works". The prompting guide says "use git for state tracking".
Sources: Anthropic harnesses; API memory tool ("Multisession software development pattern"); API prompting best practices. Evidence: VB + VD.

**B5. Put machine-checked or tamper-sensitive state in a structured format, and free-form progress notes in prose.**
Why: "the model is less likely to inappropriately change or overwrite JSON files compared to Markdown files". The prompting guide: "structured formats for state data", "unstructured text for progress notes".
Sources: Anthropic harnesses; API prompting best practices. Evidence: VB + VD. Markdown with YAML front matter fits this split, provided status fields are only changed by scripts.

**B6. Record failed approaches and gotchas explicitly.**
Why: Manus: "Leave the wrong turns in the context … shifting its prior away from similar actions." Across a boundary, the only way to carry these forward is to write them down.
Source: Manus. Evidence: P. See disagreement X3.

**B7. Use semantic identifiers the next agent can act on (paths, branch names, test names), not opaque IDs.**
Why: semantic identifiers "significantly improve Claude's precision in retrieval tasks by reducing hallucinations".
Source: Anthropic writing tools. Evidence: VB.

**B8. Put the most important content first.**
Why: after compaction, Claude Code truncates re-injected skill bodies "keep[ing] the start of the file", so "put the most important instructions near the top". Lost in the Middle shows the best recall at the beginning and end of a context. Hook previews show only the first 2,000 characters.
Sources: CC context-window; Liu et al.; CC hooks. Evidence: VD + S.

## C. Resume and re-hydration

**C1. Use a fixed opening sequence: orient (pwd, git log/status), read the progress file, pick one task, then verify before doing new work.**
Why: the harness post gives this exact order. The next agent should "run a basic test on the development server to catch any undocumented bugs" before starting a new feature. The prompting guide repeats it: "Review progress.txt, tests.json, and the git logs", then "manually run through a fundamental integration test".
Sources: Anthropic harnesses; API prompting best practices. Evidence: VB + VD.

**C2. Load the handoff up front and everything else just in time.**
Why: Anthropic recommends "lightweight identifiers (file paths, stored queries, web links)" loaded on demand, with "hybrid" strategies that preload a small core. Claude Code's own compaction re-reads only the 5 most recently modified files and passes files over 5,000 tokens as path references.
Sources: Anthropic context engineering; CC context-window. Evidence: VB + VD. Audit point: does "read referenced files" mean all of them? Prefer reading on first need, with a cap.

**C3. Load skills by name after reading the handoff, and expect the size limits.**
Why: after compaction, Claude Code re-injects skill bodies with caps of 5,000 tokens per skill and 25,000 in total, dropping the oldest first. A fresh session does not re-inject them at all. SessionStart can return `reloadSkills: true` when a hook installs skills.
Sources: CC context-window; CC hooks (SessionStart). Evidence: VD.

**C4. Check the handoff's recorded state against reality before acting.**
Why: agents need "ground truth from the environment at each step". Du et al. found that reciting the evidence before solving helps (up to +4%). Manus uses todo.md recitation to steer attention.
Sources: Anthropic building effective agents; arXiv 2510.05381; Manus. Evidence: VB + S + P. Concretely: compare branch@sha with `git rev-parse`, rerun the tests listed as "verified", and treat "unverified" items as hypotheses.

**C5. A fresh session built from files is a documented alternative to compaction.**
Why: "Claude's latest models are extremely effective at discovering state from the local filesystem … you may want to take advantage of this over compaction."
Source: API prompting best practices. Evidence: VD. This is the basis for the kit's approach. See X1.

**C6. Handle the compact source as well as startup, resume and clear.**
Why: both CLIs fire SessionStart with `source: "compact"` after compaction. Codex delivers that context "to the immediate continuation" even after compaction mid-turn. This is the documented place to re-inject critical context if native compaction fires before the kit does.
Sources: CC hooks guide ("Re-inject context after compaction"); Codex hooks (SessionStart). Evidence: VD.

## D. Injection mechanics

**D1. Keep each injected string well under the hook caps, and never let the must-see part depend on a spill file.**
- Claude Code: `additionalContext`, `systemMessage`, `initialUserMessage` and plain stdout are "capped at 10,000 characters", each measured separately. Anything over is saved to a file and replaced with the path plus a preview of up to 2,000 characters. The docs say: "Claude Code doesn't ask Claude to read the file, so keep anything Claude must always see within the cap." The cap can't be raised. Introduced in 2.1.89 (2026-04-01).
- Codex: each model-visible hook message is limited to "roughly 2,500 tokens" by default. Excess is saved to `<temp_dir>/hook_outputs/<session_id>/<uuid>.txt` with a head-and-tail preview. The limit is configurable per handler with `additionalContextLimit` (0 means unlimited, which is discouraged).
- Claude Code PostToolUse `classifierContext` notes are capped at 2,000 characters per tool call, shared across hooks.
Sources: CC hooks ("JSON output", "Add context for Claude"); Codex hooks ("Large hook output"); CC changelog 2.1.89. Evidence: VD.

**D2. Write injected text as facts, not as system-style orders.**
Why: Claude Code says: "Write the text as factual statements rather than imperative system instructions … Text framed as out-of-band system commands can trigger Claude's prompt-injection defenses, which causes Claude to surface the text to you instead of treating it as context." Audit the trigger message, for example "Context is at 134k tokens; the session-handoff skill applies" rather than "SYSTEM: STOP NOW".
Source: CC hooks. Evidence: VD.

**D3. In Codex, use PostToolUse JSON `additionalContext` rather than exit 2.**
Why: Codex PostToolUse now supports `hookSpecificOutput.additionalContext`, added "as extra developer context". Exit 2 or `decision: "block"` means "Codex records the feedback, replaces the tool result with that feedback". **An exit-2 trigger throws away the real output of the tool call that crossed the threshold.** Plain-text stdout on Codex PostToolUse is ignored.
Source: Codex hooks (PostToolUse). Evidence: VD. High-priority audit item.

**D4. Mid-session injections are replayed on resume. Only SessionStart runs again.**
Why: "For mid-session events like PostToolUse or UserPromptSubmit, when you resume with --continue or --resume, Claude Code replays the saved text rather than re-running the hook … values like timestamps or commit SHAs become stale." A resumed old session will contain an old "write a handoff now" instruction, so the skill should check the current state instead of obeying that text.
Source: CC hooks. Evidence: VD.

**D5. Keep the start of the context stable, and inject late and small, for cache hits.**
Why: Manus: "KV-cache hit rate is the single most important metric", "keep your prompt prefix stable", "make your context append-only". Anthropic's cache TTL is 5 minutes by default (1 hour at extra cost), and cache writes cost 1.25x. Tool-result clearing "invalidates cached prompt prefixes", hence `clear_at_least`. Claude Code fixed a bug in 2.1.277 (2026-09-18) where SessionStart output after /clear caused "a full prompt-cache miss". Resume hooks now receive `prompt_cache_likely_expired`, `context_tokens` and `estimated_cache_write_usd` (2.1.251).
Sources: Manus; API prompt caching; API context editing; CC changelog; CC hooks (SessionStart input). Evidence: P + VD. Keep SessionStart output deterministic, meaning no timestamps or random ordering, so repeated starts share a prefix.

**D6. Choose the hook event by where its text lands.**
Where it lands: SessionStart and SubagentStart at the start of the conversation; UserPromptSubmit next to the prompt; Pre/PostToolUse and PostToolBatch next to the tool result; Stop and SubagentStop at the end of the turn, which keeps the turn going.
- Stop `additionalContext` (since 2.1.163) continues the turn without a hook-error label, under the 8-continuation cap.
- PreCompact can block compaction. If compaction was proactive it is skipped; if it was error-recovery, the request fails. PreCompact can't add context.
- PostCompact receives `compact_summary`, has no decision control, and was added in 2.1.76 (2026-03-14).
Sources: CC hooks; CC changelog. Evidence: VD.

**D7. Static rules belong in CLAUDE.md or AGENTS.md. Hooks are for dynamic state.**
Why: "For instructions that never change, prefer CLAUDE.md." Codex AGENTS.md loading stops at `project_doc_max_bytes`, 32 KiB by default.
Sources: CC hooks; Codex AGENTS.md guide. Evidence: VD.

## E. Safety and robustness

**E1. Treat a handoff as evidence, not authority.**
Why: LangChain lists context poisoning ("when a hallucination makes it into the context") and context clash. Claude Code's classifier treats hook-supplied notes as "unverified, application-provided context" that "never establishes user intent". After a resume, restored notes are treated as unverified. Codex says to treat memories as "a helpful recall layer, not as the only source for rules".
Sources: LangChain; CC hooks (classifierContext); Codex memories. Evidence: P + VD.

**E2. Detect stale handoffs.**
Why: SHAs and timestamps in replayed context go stale (D4). The memory tool docs recommend expiring files that haven't been used in a long time. Claude Code now exposes `seconds_since_last_response` on resume.
Sources: CC hooks; API memory tool. Evidence: VD. Compare branch@sha with HEAD and the working tree, check age, and show any drift before acting.

**E3. Scope everything by session and repo, and assume concurrent sessions.**
Why: Claude Code auto memory is shared by "all worktrees and subdirectories within the same git repository". Claude Code had a bug where `/model` in one session changed the auto-compact threshold in other sessions. Codex subagent hooks report the parent `session_id`.
Sources: CC memory; CC changelog; Codex hooks. Evidence: VD. Claims should be atomic and owner-stamped, and latches should be keyed by session ID with a fallback when that ID is missing.

**E4. Validate every path read or written from a handoff.**
Why: memory tool docs: reject `../` and URL-encoded traversal, resolve to canonical form, and cap file sizes. Codex spill files land in temp directories, so "avoid returning secrets … in hook output".
Sources: API memory tool; Codex hooks. Evidence: VD.

**E5. Keep secrets out of handoffs.**
Why: Codex memories "redact secrets from generated memory fields". The memory tool docs suggest removing sensitive data before writing. Hook output may be saved to disk (D1).
Sources: Codex memories; API memory tool; Codex hooks. Evidence: VD.

**E6. Keep a hard stop on any automatic loop.**
Why: "stopping conditions (such as a maximum number of iterations) to maintain control". Claude Code applies the same rule to itself: 3-attempt compaction breaker, thrash detection, 8-continuation Stop cap.
Sources: Anthropic building effective agents; CC changelog; CC hooks. Evidence: VB + VD. This applies to the automatic /clear+resume loop.

**E7. Respect hook trust and timeouts.**
Why: Codex hooks require review and trust, and are enabled via `[features].hooks`; `codex_hooks` is the old alias. Claude Code UserPromptSubmit hooks time out after 30s, and their context is discarded on timeout. SessionEnd hooks share a 1.5s budget.
Sources: Codex hooks; CC hooks. Evidence: VD.

---

## Where sources disagree

- **X1. Fresh session versus compaction.** Anthropic's prompting guide suggests a new context window that rediscovers state from files. Claude Code's default is automatic compaction, and its best-practices page says to `/clear` often between tasks. Cognition prefers a compression model inside one linear agent. The API page on memory with compaction says "consider using both". Net: vendors support both approaches, and nobody has published a comparison on coding tasks.
- **X2. Stop and hand off, or keep working.** Anthropic's prompting guide warns that models "may sometimes naturally try to wrap up work as it approaches the context limit" and recommends telling them "do not stop tasks early due to token budget concerns", because the harness will compact. The kit deliberately stops. This is a design choice, but the trigger text should make clear that stopping means writing the handoff, not abandoning the task.
- **X3. Keep errors or clear them.** Manus keeps wrong turns in context. Claude Code best practices: "If you've corrected Claude more than twice … /clear and start fresh". These fit together across a boundary: drop the noisy history but keep the distilled lesson (the kit's Gotchas section).
- **X4. JSON or Markdown for state.** Anthropic's harness post prefers JSON for feature lists because it gets overwritten less. The prompting guide allows prose for progress notes. Split by who edits the field.
- **X5. Where to measure.** The statusline gets `context_window.*` directly, but hooks don't. In both CLIs, hooks must parse the transcript, which Codex calls unstable and Claude Code says may lag. There is no documented token-usage field in PostToolUse input, apart from subagent `tool_response.usage` in Claude Code.

## Recent changes and overlap with native features

| Change | Date / version | Relevance |
|---|---|---|
| CC hook output over 10,000 chars saved to file with a 2,000-char preview | 2.1.89, 2026-04-01 | Hard cap on injection size |
| CC `PostCompact` hook (receives `compact_summary`) | 2.1.76, 2026-03-14 | Could capture the native summary as a fallback handoff |
| CC PreCompact can block compaction | 2.1.105, 2026-04-13 | Safety net: block proactive auto-compact until a handoff exists (not possible for error-recovery compaction) |
| CC Stop/SubagentStop `additionalContext` (non-error continuation) | 2.1.163, 2026-06-04 | Alternative channel for the trigger |
| CC SessionStart source `fork` split from `resume` | 2.1.214, 2026-07-18 | Matcher lists need `fork` |
| CC SessionStart resume staleness and cache-cost fields; Pre/PostModelSwitch hooks | 2.1.251, 2026-08-28 | Staleness check; per-model thresholds can follow model switches |
| CC advisor-turn double counting fixed | 2.1.273, 2026-09-15 | Measurement check |
| CC SessionStart output after /clear no longer breaks the cache | 2.1.277, 2026-09-18 | Automatic /clear loop cost |
| CC auto-compact window configurable (`/autocompact`, flag, env) | 2026 | May fire before the kit's threshold |
| CC auto memory (`MEMORY.md`, first 200 lines/25KB, shared per repo) | current | Overlaps with cross-session notes, but not task handoffs |
| Codex PreCompact/PostCompact, SessionStart `compact` source, PostToolUse `additionalContext`, `additionalContextLimit` (default ~2,500 tokens) | current docs | Replace exit-2 injection |
| Codex `compact_prompt` / `experimental_compact_prompt_file`, `model_auto_compact_token_limit_scope` | current docs | Native compaction can be steered toward the handoff sections |
| Codex local Memories (background, idle-triggered, redacted) | current docs | Overlaps with durable learning; not for in-flight task state |
| API server-side compaction: threshold `compact-2026-01-12` (default 150k, min 50k), on-demand `compact-2026-09-04`; custom `instructions` up to 16,384 chars; SDK `compaction_control` deprecated | 2026 | Same idea as the kit, done natively for API agents |
| API context editing (`clear_tool_uses_20250919`, `clear_thinking_20251015`) plus memory tool; memory+editing +39% on Anthropic's agentic-search eval, 84% fewer tokens | 2025-09-29 | Evidence that write-outside-context approaches help |
| API context awareness (`<budget:token_budget>`, per-tool `system_warning` usage) on Sonnet 5/4.6/4.5 and Haiku 4.5; **not** on Opus 4.7+, Fable or Mythos, which use "task budgets" (beta) | current | On some models the agent already sees its usage; on Opus/Fable it doesn't, so the kit's hook signal is still needed |
