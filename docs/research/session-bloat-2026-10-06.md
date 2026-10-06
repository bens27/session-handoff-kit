# Session bloat corroboration — 2026-10-06

Reviewed backlog `3a781b7b-4da3-4ccd-a062-6d0966308eb9` against local Claude
transcripts, saved checkpoints, the 0.17.3 handoff skill and its implementation.
Only usage fields, timestamps, checkpoint metadata and one captured resume
result were measured; raw conversations are not reproduced here.

## Confirmed evidence

Occupancy sums fresh input, cache creation, cache reads and the last output,
without summing repeated calls. Assistant records were deduplicated by message ID.

| Session prefix | First occupancy | Later occupancy | Elapsed |
| --- | ---: | ---: | ---: |
| `8d5e3bb6` | 32,150 | 143,278 | 235 seconds |
| `ed5037c0` | 32,141 | 145,605 | 140 seconds |
| `a0a9da2a` | 31,438 | first crossing: 130,739 | 149 seconds |

The third transcript later continued to 173,381 tokens. These are deduplicated
API calls (20, 13 and 39 in the available transcripts), not an independently
verified count of roughly 60 turns. Attribution of every transcript to one
agent remains uncertain. Evidence is in Claude's `clearmethodstudio` project
transcripts under these prefixes.

Three `repo-convergence-assembly-line` checkpoints in that project's `.handoffs`
were created at 16:23:42, 16:27:42 and 16:31:21, with the same terminal identity
and `reason: context-pressure`. The first two were resumed; the last remained
open when inspected. This corroborates the repeated short handoff cycle.

One captured resume tool result measured 15,946 UTF-8 bytes, comprising a
6,648-byte body, 6,667 bytes of required references, a duplicated 1,405-byte
`next_step`, and serialization/metadata. Its `package_bytes: 21944` includes
7,262 accounted workflow bytes. Those workflow bytes were not printed as
workflow text by `resume`. The 17,588-token growth around that result also
included schema/instruction loads; it cannot all be attributed to the package.
The report's 60k re-entry / 40k work split was not independently established.

AgentsRoom's advertised `agents_restart` contract confirms a two-minute
minimum and six restarts/hour per agent. The kit's old consecutive-clear
guard covered tmux, while AgentsRoom transitions always requested restart.
Thresholds were already configurable by model, global/project files and
environment; percentage triggering was already opt-in. The real window for
the reported Claude model is unverified, so the default was not increased.

## Changes in 0.18.0

- Linked ticket `3ef5c5e3-2089-40e1-beb4-2d5d5cd7ecd0`: pause automatic
  transitions on rapid resumed-session pressure, repeated topic saves and
  terminal-scoped restart intents approaching host limits. Save full content
  before pausing; preserve the open checkpoint for explicit continuation.
  Startup, tmux Stop and the runner respect the pause. Durable resume timestamps
  retain the guard when temporary notes are lost.
- Linked ticket `4b5724b4-73e6-4d20-b22c-e40d4d96db14`: deliver next steps once
  in the body, expose a section pointer, and retain an explicit legacy text
  option. One-shot resume accepts validated same-context receipt reuse and
  loaded-skill catalogs. Exact delivery bytes are separate from context accounting.
- Linked ticket `1ea5e2d9-c1d3-4458-b014-7eea17bee9f9`: expose threshold/window
  provenance, estimate remaining working room, warn about costly rereads and
  broad excerpts, provide a minimal pressure-save contract, and document recovery
  after host refusal. Required content and size recommendations stay advisory.

These changes address kit overhead and unbounded restart behavior. They cannot
reduce a host's initial briefing/schema costs or a coordinator's later document
reads. Restart-intent counts are conservative local observations, not access to
the host's actual counter. Bytes/4 estimates are neither tokenizer counts nor
billing measurements. Long-term improvement in useful work per session requires
fresh production traces; the original transcripts establish the baseline only.

## Validation

`tests/test_session_bloat.py` exercises real ledger commands and hook events
against temporary projects, homes and transcripts. It covers rapid re-entry,
terminal isolation, repeated topics, hourly/cooldown recovery with a fake external
clock, lost notes, paused startup/Stop/runner behavior, full large payloads,
receipt reuse/fallback, working-room telemetry and advisory reread warnings.
Existing protocol and safety suites retain trust, ownership, verification and
large-content coverage. Final command results are recorded in the linked backlog.

## Controlled live comparison after the change

On 2026-10-06, four live Claude CLI sessions compared `b2bc158` (0.17.3) with
`c2c0a9b` (0.18.0), in old/new/new/old order. Each session received an actual
ledger `prepare` delivery, reviewed six JavaScript snippets, then received a
same-context `resume` delivery and reviewed six more. The new version supplied
its validated reuse receipt; the old version repeated the checkpoint and reference.
Both versions initially delivered identical fixture body and reference text.
The fixture covered six contract violations and six correct controls, including
tenant isolation, asynchronous persistence, concurrent loading, and cleanup.

All eight requests used `claude-fable-5-1`, low effort, the same explicit system
prompt, tools disabled, and Claude CLI 2.1.292 safe mode. This isolates ledger
delivery from production briefings, MCP schemas, hooks, skills, and tool loops.
No raw production conversation was sent as the review fixture.

| Run | Version | Input tokens across two requests | Continuation input tokens | Correct reviews | API-equivalent session cost |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | 0.17.3 | 9,136 | 6,137 | 12/12 | $0.22073 |
| 2 | 0.18.0 | 6,872 | 3,869 | 12/12 | $0.17205 |
| 3 | 0.18.0 | 6,897 | 3,894 | 12/12 | $0.17610 |
| 4 | 0.17.3 | 9,098 | 6,099 | 12/12 | $0.21967 |

Mean processed input fell from 9,117 to 6,884.5 tokens (24.5%). Mean continuation
input fell from 6,118 to 3,881.5 (36.6%). Mean API-equivalent cost fell from
$0.22020 to $0.174075 (20.9%). Every classification matched the fixture oracle,
and the explanations were manually reviewed against the supplied contracts.
Correct reviews per thousand processed input tokens increased from 1.316 to
1.743 (32.4%). These are measured tokenizer counts, not bytes/4 estimates.

Input includes fresh input, cache creation, and cache reads. Every request had
two fresh input tokens, one-hour cache creation, and zero cache reads, so the
comparison did not mix cache-hit and cache-miss pricing. Output averaged 758
tokens before and 728.5 after. Completed-result usage was cross-checked against
the final cumulative `modelUsage`; streaming assistant events contain partial
output counts. `total_cost_usd` is cumulative across CLI `--resume`, so only the
last result per session was counted. The whole experiment cost $0.78855 at the
CLI's list-price equivalent. Actual incremental Max subscription billing was
not measured, and warm-cache pricing can yield different savings.

The observed benefit applies to retrieval followed by validated same-context
receipt reuse. Two two-turn sessions per version do not establish longer
production sessions, fewer pressure-triggered restarts, statistical significance,
or a lower subscription bill. No qualifying new production assistant usage was
found after deployment in the latest fifteen `clearmethodstudio` transcripts
checked; those original long sessions remain a baseline rather than a matched
after sample. Summed input processing across requests is not live occupancy.

Local reproducibility artifacts are under
`~/.context-watch/observations/20261006-live-ab/`: `runner.py`, `analyze.py`,
`manifest.json`, `oracle.json`, versioned ledger source snapshots and payloads,
per-turn prompts and provider events, `results.json`, and `summary.json`.
The saved Dev Command is **Live handoff A/B measurement**. Its $1 cap applies to
each invocation; the successful experiment made eight invocations.
