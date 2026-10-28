# Restart intent isolation and recovery

`restart_preflight.py restart` checks the attempted request's own intent before
scanning `.handoffs/restart/`. Request IDs are deterministic per checkpoint;
existing dispatch evidence must survive every blocked or skipped result.

## Unrelated foreign roots

A readable record for another canonical absolute root is skipped only when its
request ID matches its filename, its checkpoint path is canonical and absolute,
and its request ID, checkpoint path and SHA256 checkpoint ID all differ from the
current attempt. Local records, related checkpoints and uncertain bindings still
undergo strict validation. Missing JSON or identity data cannot prove isolation.

The verdict lists every skipped record, including on a later gate failure:

```json
{
  "skipped_intents": [{
    "path": "/project/.handoffs/restart/<sha256-request-id>.json",
    "request_id": "agentsroom:<foreign-request-digest>",
    "field": "root",
    "detail": "Foreign root; this record does not apply to this restart.",
    "recorded": "/project/.worktrees/lane",
    "expected": "/project"
  }]
}
```

The foreign files remain untouched. No quarantine or registry repair is needed
for an unrelated fresh save and restart. A skip does not bypass publication,
identity, worker inventory, holds, host capability or duplicate-dispatch gates.
Only `outcome: ready` with `dispatch: true` permits the host restart call.

## A blocked invalid intent

`reason: invalid-persisted-intent` carries a separate `invalid_intent` diagnostic
with the offending file `path`, failed `field`, bounded `detail` and request ID
when known. The outer `path` and `request_id` describe the attempted restart.
The own-request lookup ID is retained even if its JSON or stored ID is damaged.
`json` means malformed/nonobject state; `file` means I/O failure and includes the
exception class and strerror. Other fields include `root`, `host_acceptance`,
`workers[0].id` and `successor_session`. Detail is capped at 200 characters;
root failures put full `recorded` and `expected` roots in separate data fields.

1. Do not call `agents_restart`. Keep the checkpoint open and preserve the
   offending intent. Report its nested path, field, request ID and detail.
2. Reconcile that request with native host history, its checkpoint binding and
   successor retrieval. Do not infer host acceptance from a saved checkpoint or
   from a settled state alone. For I/O errors, restore access or retry after the
   transient error clears; an I/O failure is not a reason to discard evidence.
3. If the nested request ID matches the outer one, it is the attempted request's
   own intent. Do not quarantine it and retry the same checkpoint: removing its
   evidence could dispatch the same deterministic request twice. If the host
   accepted it or its outcome is unknown, continue manually or save a new
   checkpoint for a new request ID. Preserve identity, trust and hold gates.
4. If scope cannot be established, treat dispatch as uncertain and preserve the
   record. Any later restoration or quarantine of confirmed foreign evidence
   requires an operator's reconciliation. The kit never performs that move or
   rewrites a binding. A genuine host refusal still needs a new save once its
   cause is resolved.

Updating the repository does not update an independent copied global skill.
The installation emitting the restart command must be resynced through its
normal deployment workflow. Investigation evidence and the regression history
for the copied-worktree incident are recorded on ticket #166.
