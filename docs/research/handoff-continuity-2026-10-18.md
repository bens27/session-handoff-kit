# Handoff continuity evaluation — 2026-10-18

## Evidence and causes

The audited predecessor transcript scheduled an AgentsRoom restart at
18:29:30Z; the new session started at 18:29:46Z. The final checkpoint retained
its v3 hash, 19-test evidence and ownership boundaries, but compressed away
scriptability direction, measurement/reply follow-ups, known lifecycle gaps,
exact commands and guidance contents. That checkpoint declared no references
or skills. This instance therefore combined authoring loss with hidden metadata;
it was not the separate older case of five declared references disappearing.

Public hook and lifecycle CLI regressions reproduced two architectural failures:
1. The actual hook-generated `resume --fields path,body` command could report
   success after required references and skills had been removed from output.
2. A shorter successor body discarded obligations; omitted dependency fields
   also replaced previous declarations with empty metadata.

Three initial focused regressions failed before the fixes; final review added
a failing public regression for filtered verification recovery details. The user confirmed hook and
lifecycle CLI as the public regression interfaces. Tests exercise real hooks,
subprocesses and temporary installed skill catalogs, without internal mocks.

## Implemented changes

`hooks/handoff_contract.py` owns the durable continuity and delivery contract.
`handoff_protocol.py` keeps lifecycle orchestration and trust decisions.

- A shared output projection preserves the available successful delivery
  envelope despite optional field selection, plus verification logs, exit codes
  and commands requiring confirmation on failed transfers. Metrics count that exact projected
  UTF-8 JSON, including Unicode and metric fields.
- An optional continuity register carries explicitly recorded goals, constraints,
  follow-ups and delegation across shorter successor summaries. Stable IDs merge;
  resolution/retirement requires evidence or a reason, and delegation an owner.
- Trusted predecessor dependencies inherit when omitted; explicit lists replace
  them. Untrusted predecessors supply no implicit declarations or register items.
- Delivery includes lineage and continuity. Skill-loaded declarations and receipt
  reuse remain valid only for content actually present in the current context.

The CLI template and agent guidance explain how to author and update these
records. Existing checkpoints remain readable without the new register.

## Limits and ownership

The register cannot recover meaning never recorded. An author can still write an
incomplete or misleading item, and evidence strings are declarations rather than
proof checks. A predecessor pointer permits targeted investigation; it does not
automatically replay the transcript. Current user instructions, authorization and
workspace evidence take precedence over stored obligations.

This work does not establish end-to-end host lifecycle acceptance. Restart
provenance, pointer freshness, acknowledgment/retry gaps and originating-session
measurements remain separate host/core acceptance work. Historical follow-ups may
have moved to those owners; this change does not reclaim or dispatch them.
The separately reviewed kit-only v3 patch remains with core for integration.

## Validation

Focused protocol coverage includes the generated hook resume command, fresh
context skill delivery, shorter successor summaries, inherited and explicitly
replaced dependencies, explicit disposition and retries, untrusted inheritance
isolation, and exact filtered output sizes. Final isolated-worktree checks:

- `python3 -m unittest discover -s tests`: 216 tests passed in 106.222 seconds.
- Focused protocol suite: 68 tests passed in 35.319 seconds.
- `verify-skills.py`, `verify-watcher.py`, `verify-packaging.py`: all passed.
  Packaging executes the ledger from the extracted standalone archive, verifying
  the new runtime module ships with it.
- The first full run exposed two existing assertions for the old field-filtering
  contract; those now verify preserved context/diagnostics and exact byte counts.

Logs: `/tmp/handoff-continuity-unittest-final.log`,
`/tmp/handoff-continuity-protocol-final.log`, and
`/tmp/handoff-continuity-{skills,watcher,packaging}.log`.
The full run emits an existing unclosed-counter-file ResourceWarning in
`context_watch.py`; it does not fail the suite and is outside this patch.

Integration applied only the ten-file delta against the saved dirty baseline.
The shared checkout matches the tested worktree for those files. Three focused
root smoke regressions passed (generated hook delivery, shorter successor
continuity, filtered failure recovery); log:
`/tmp/handoff-continuity-root-smoke.log`. No commit or push was made.
