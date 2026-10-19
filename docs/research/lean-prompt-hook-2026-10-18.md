# Lean prompt-hook evidence, 2026-10-18

Tickets: 3f04a045-e747-47a2-8614-fc10261ba587 (lean resume/quiet startup) and
 af6de0ea-fbb1-4247-89a5-eb935f6789f3 (recorded session identity).

## Public hook fixture

Measured `context_watch.py` via JSON stdin/stdout on this Mac with Python 3,
throwaway HOME/TMPDIR and a real CLI-saved trusted checkpoint. Baseline is commit
641011f; after is the uncommitted implementation. These are single-run observations,
not token counts or a throughput benchmark. Production telemetry was disabled.

| Version | Event | Injected context bytes | Hook stdout bytes | Elapsed ms |
| --- | --- | ---: | ---: | ---: |
| before | empty startup | 194 | 277 | 188.1 |
| before | owned startup | 635 | 721 | 104.5 |
| before | explicit prompt | 568 | 658 | 63.8 |
| after | empty startup | 0 | 0 | 201.2 |
| after | owned startup | 0 | 0 | 151.7 |
| after | explicit prompt | 418 | 505 | 140.3 |

The extra bounded worker adds process-launch latency while eliminating ordinary
startup text and skill-body loading on the direct-command path. A FIFO regression
checks that a stalled read returns silently in under 2.5 seconds; the worker timeout
is one second.

Before, empty startup said:

> handoff-status: No open handoff found. Continue the current task; for an explicit retrieval request report this result. Use history to inspect closed work. This status requires no skill loading.

After, empty and ordinary owned startup emit no output. An incomplete owned
publication emits only its checkpoint path and save-repair guidance. Explicit
`continue the handoff` emits the exact owned path, current session and
`--fields path,body`, stating that the skill does not need to load. Matching prompts
still receive pressure-save guidance when over the configured threshold.

## Verification and remaining acceptance

The protocol CLI and hook stdin/stdout are the regression seams. Coverage includes
foreign/ambiguous/untrusted stores, worker timeout/failure, read-only selection,
publication repair, direct command execution before a transcript exists, normalized
session IDs, save after compaction, idempotent retry, same-terminal transfer
continuation, foreign-terminal transfer refusal and a newer live claim.

The original-session before/after token measurement must come from the
clearmethodstudio owner; this fixture does not resume or claim that terminal's work.
Independent review is required before merge. No QA bot was used. A separate
user-authorized AgentsRoom worker runs the lifecycle experiment below.

A pre-fix transfer receipt lacks terminal/runner provenance. Such an untraceable
transfer remains protected when its resumed_by differs from the recorded session;
the original saver terminal alone cannot prove who performed the resume. Current
transfers record provenance in the local preparation receipt. Cleanup or replacement
of that receipt removes this additional same-terminal proof; same-session
continuation remains supported.

## Real-agent pre-clear inspection

The user-authorized worker `agent-1792338715664-iy0ghc` saved its own
`lifecycle-probe-iy0ghc` checkpoint and held before reset. Its actual transcript
was inspected before authorizing the retrieval phase. It showed repeated etiquette
and source reads, broad directory/help output and inline Python imports to discover
session identity. The canonical `n-agentic-harnesses` skill was absent; repository
guidance remained the fallback.

The audit artifact `/tmp/lifecycle-probe-preclear-audit.json` records 161289 bytes
of serialized tool outputs and last reported cumulative usage of 601871 input
tokens (549376 cached), 4509 output tokens, 606380 total. These are cumulative
request counters including repeated context, not context occupancy, checkpoint
size or measured savings. The transcript is
`~/.agentsroom/codex/proj-1790515192997-y67dfo/sessions/2026/10/18/rollout-2026-10-18T11-52-24-01a14fb6-d2bd-7213-b973-b323f28d74b9.jsonl`.

The resulting reduction is the read-only public `handoff_ledger.py session`
command: one deterministic lookup returns terminal identity, hook/environment
identity source, separate host thread ID, recorded transcript and pressure flag.
It avoids source imports and transcript-directory searches. CLI regression tests
cover absent hooks, recorded identity precedence, transcript reporting, unchanged
hook state and field selection.

The worker had no recorded session pointer or note before/after save, so used its
actual Codex thread UUID explicitly. The coordinator's fresh session instead sees
an old hook pointer alongside a different current `CODEX_THREAD_ID`. These are
runtime observations, not evidence that fresh-session hooks ran. Neither case
authorizes synthesizing hook state or transferring a foreign checkpoint.
Retrieval transcript inspection and original-session comparison remain pending.
The coordinator's restart authorization (`msg-0mve0e2yb-989791bb`) is persisted
but queued: `agents_message_status` reports `recipientRuntime: waiting_input`.
AgentsRoom holds mail until the user answers in the worker tab (or their configured
waiting-agent delivery delay elapses). No duplicate instruction was sent.
