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
