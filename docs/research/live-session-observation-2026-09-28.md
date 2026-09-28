# Live session observation — 2026-09-28

Two real Codex model sessions exercised Session Handoff Kit v0.14.0 using
Codex CLI 0.157.1, gpt-6-astra with low reasoning, and the existing trusted
AgentsRoom-managed Codex profile. Automatic continuation completed the task
after correcting the observer's Codex invocation. There was no global auto-mode
configuration change.

## Scope and outcome

An isolated scratch Git workspace contained four integers and a protected file.
The first session was explicitly instructed to calculate a partial sum, publish
a checkpoint, and stop. The fresh receiving session prepared the checkpoint
once, acknowledged it before doing further work, verified the partial result,
and wrote `result.json` containing `{"total":100}`. The protected file remained
unchanged. The checkpoint became `resumed`; the final ledger lookup found no
open handoff. The recovery auto runner exited 0 rather than starting another run.

The observer capped the experiment at two model sessions and imposed a
180-second timeout per child. Three processes launched, but one failed before
starting a model session. Successful children took 43.82 and 33.43 seconds.

This tests headless auto mode using an AgentsRoom profile, not AgentsRoom's UI
transfer, Claude's tmux clear path, or context-threshold detection. Checkpointing
was deliberately requested mid-task rather than consuming tokens to hit a limit.

## Measured context

| Item | UTF-8 bytes |
| --- | ---: |
| No-handoff startup status in producer | 194 |
| Receiver startup notice | 593 |
| Receiver first-prompt notice | 617 |
| Receiver workflow instructions (skill and continuation) | 5,207 |
| Checkpoint body | 1,907 |
| Referenced task data | 65 |
| Prepared package accounting | 8,278 |
| Package budget | 32,000 |

The package used about 26% of its budget. Its reported 2,070-token estimate is
bytes divided by four, not a tokenizer measurement. Hook notices and the host's
other instructions/tools are outside this package budget. Actual prepare output
was 2,993 bytes including its newline; the package metric additionally accounts
for workflow instructions and packaging overhead.

The two receiver notices delivered essentially the same prepare command using
different paths to the same installation. Keeping only the startup notice would
avoid 617 bytes in this run. The agent nevertheless prepared and acknowledged
only once. This is remaining context waste, not a demonstrated double execution.

| Actual usage | Producer | Receiver | Combined |
| --- | ---: | ---: | ---: |
| Model requests | 5 | 5 | 10 |
| Cumulative input tokens | 86,090 | 84,596 | 170,686 |
| Cached input tokens | 78,976 | 77,824 | 156,800 |
| Input minus cached tokens | 7,114 | 6,772 | 13,886 |
| Output tokens | 1,091 | 730 | 1,821 |
| Largest single-request input | 18,531 | 18,158 | — |

Cumulative input counts repeated context across requests; it does not mean a
170,686-token live context. These totals include the profile, tools, task, and
handoff workflow. Without a matched baseline they cannot establish the kit's
exact incremental token cost or dollar savings. A deliberately tiny arithmetic
task also makes handoff overhead disproportionate to useful task work.

## Launcher failure and recovery

The initial wrapper appended `resume` directly to `codex exec` options. Codex
interpreted it as its `resume` subcommand and exited 1 with “No prompt provided
via stdin.” No model session started. The kit runner stopped on this failure
and preserved the open checkpoint, as intended.

The wrapper was corrected to place `--` before the prompt. A new bounded auto
runner invocation (`--max 1`, default prompt `resume`) then consumed the existing
checkpoint successfully. No ledger state was manually changed. Therefore this
was a successful recovery and receiving-session test, not an uninterrupted
successful original two-run chain. README now documents the required Codex
argument separator.

## Evidence

Local artifacts are retained at
`~/.context-watch/observations/20260928-164705/`: `measurement.json`, per-process
event streams, summaries, stderr, final messages, runner exit records, and the
scratch workspace/checkpoint. Measurements were extracted from actual tool
results and Codex rollout usage. Requested isolated delivery/trigger logs were
not produced; this report does not rely on them. Raw transcripts are not copied
into this repository.

Producer thread: `01a0e9c5-b2c9-76e2-9a8d-9f468f4a92f2`.
Receiver thread: `01a0e9c7-fe2c-7ef2-b51d-4df8e6f31a2d`.

The observed receive path preserved constraints and stopped correctly. This
single probe does not establish reliability across all host integrations or
guarantee low cost for arbitrary tasks. Duplicate hook delivery remains a
specific, measured opportunity to reduce overhead.
