# Restart intent isolation and recovery

`restart_preflight.py restart` checks the attempted request's own intent before
scanning `.handoffs/restart/`. Request IDs are deterministic per checkpoint;
existing dispatch evidence must survive every blocked or skipped result.

## Unrelated foreign roots

A readable record for another canonical absolute root is skipped only when it
is settled (`retrieved` or `refused`) and passes the normal field and host-evidence
validation against that recorded root. Validation uses the copied record; the
foreign worktree and its registry need not exist. `retrieved` requires a different
successor session and a retrieval fingerprint; `refused` requires a refusal
string. Both states require valid native acceptance evidence if `host_confirmed`
is true. A definitive refusal with `host_confirmed: false` remains valid.
Its request ID must match its filename, its checkpoint path must be canonical and absolute,
and its request ID, checkpoint path and SHA256 checkpoint ID all differ from the
current attempt. Local records, related checkpoints and uncertain bindings still
undergo strict validation. Pending, missing-state, invalid-state and malformed
settled foreign records still block. Missing JSON or identity data cannot prove isolation.

The verdict lists at most the first 10 skipped records, including on a later gate
failure. `skipped_total` counts all skipped entries. The output cap never stops
the registry scan: a later pending or unsafe record still blocks.

```json
{
  "skipped_intents": [{
    "path": "/project/.handoffs/restart/<sha256-request-id>.json",
    "request_id": "agentsroom:<foreign-request-digest>",
    "field": "root",
    "detail": "Foreign root; this record does not apply to this restart.",
    "recorded": "/project/.worktrees/lane",
    "expected": "/project"
  }],
  "skipped_total": 1
}
```

The foreign files remain untouched. Valid settled records need no quarantine or
registry repair for an unrelated fresh save and restart. A skip does not bypass publication,
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
