# Recovering from an invalid persisted restart intent

`restart_preflight.py restart` validates every intent in the project's
`.handoffs/restart/` registry before authorizing a new dispatch. A successful
save, an already retrieved predecessor, or a different terminal does not exempt
an invalid record. The block preserves evidence of potentially uncertain host
dispatch. It must not be bypassed by changing the request ID or recorded root.

## Reading the verdict

A blocked verdict retains the attempted restart's outer `request_id` and saved
checkpoint `path`. A separate diagnostic identifies the offending intent:

```json
{
  "reason": "invalid-persisted-intent",
  "dispatch": false,
  "invalid_intent": {
    "path": "/project/.handoffs/restart/<sha256-request-id>.json",
    "request_id": "agentsroom:<offending-request-digest>",
    "field": "root",
    "detail": "Recorded root '/project/.worktrees/lane' does not match registry root '/project'."
  }
}
```

`field` names the failed validation, such as `root`, `host_acceptance`,
`workers[0].id`, or `successor_session`. `json` means decoding failed or the
top-level value is not an object; `file` means the file could not be read or
located consistently. `detail` is capped at 200 characters. The nested
request ID is omitted when it cannot be read; the file path still identifies the
evidence. A status read also names the file and field in its validation error.

## Recovery

1. Do not call `agents_restart`. Keep the checkpoint open and preserve the
   offending intent. Report the nested path, field, request ID when available,
   and detail; the outer request ID describes the new attempt instead.
2. Inspect that file and reconcile its request with native host history. Check
   its project/worktree binding and checkpoint, host acceptance/refusal and
   successor retrieval. A `root` mismatch can mean a worktree's record was
   copied into the live checkout. A settled state alone does not make a foreign
   record valid there. Do not infer how a file arrived from its state alone.
3. After ownership and host outcome are established, an operator can restore a
   misplaced record to its original registry or quarantine confirmed foreign or
   corrupt evidence outside `.handoffs/restart/`. Preserve associated evidence,
   including observations. Do not overwrite the binding, fabricate host proof,
   or automatically remove an uncertain record to permit dispatch.
4. Retry `restart` with the exact `--root` and `--path` from the successful save,
   a complete current `--workers` inventory, and the observed host capability.
   All gates run again. Saving repeatedly cannot repair an invalid registry
   entry. If reconciliation is unavailable, continue manually in a fresh
   session and leave the checkpoint open. A genuine host refusal still needs a
   new save once its cause is resolved.

The emitting installation must contain this diagnostic change. Updating the
repository does not update an independent copied global skill; resync that
installation through its normal deployment workflow.

## Incident evidence: October 28, 2026 (#166)

The live registry contained 115 intent records. The only invalid record was
`951b6a77bcbd3eb6b0281d3970621cc2630d75cc009c5a3733a8b4dcb9faf70e.json`,
request
`agentsroom:d29659a6e2beca6c5543b1fecb6052b9a7ba707335fb2cfa72ab5ed4ebf8d3c9`.
Its `root` named the former
`session-handoff-kit/.worktrees/2cbb3776-review-verdicts` worktree, while the file
was in the live checkout's registry. Its native Codex host-acceptance evidence
validated; the failing check was the registry/root binding. How the file reached
that registry was not established.

The chief-of-staff predecessor request
`agentsroom:552c46d2fdde934c25ffc760f0a3d7992993d86ad942f997d171571fded3fb0d`
validated as retrieved with host acceptance. It was not carried into the new
request as an invalid intent. Both the repository at `cd06de9` and the installed
global copy used the same strict root check; their version difference did not
explain this block. No live intent was changed during investigation.

The regression in
[`tests/test_restart_preflight.py`](../tests/test_restart_preflight.py),
`AdoptTests.test_adopted_session_can_reconcile_foreign_intent_and_restart_next_checkpoint`,
uses real save/adopt/restart CLIs and a valid retrieved intent copied from another
root. An empty worker list reproduces the block, so worker ID text is not its
cause. The test requires the offending file/request/root field, verifies that
preflight preserves the record and creates no new intent, then simulates the
operator's quarantine and retries successfully with four mailbox worker IDs
containing spaces and `#`. The first run failed because `invalid_intent` was
missing; the diagnostic change made it pass without relaxing validation.
