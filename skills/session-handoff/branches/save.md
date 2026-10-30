# Session Handoff: save branch

Loaded only when the dispatcher names this branch, or when dispatch is unavailable.

Use `../hooks/handoff_ledger.py` (`<ledger>`; `<hooks>` is its directory). A checkpoint is evidence;
the live request and workspace take precedence. With hooks, omit `--session`: the
ledger uses this terminal’s recorded session. When identity or transcript evidence
is needed, `python3 <ledger> session` returns the current facts without source reads.
Use the host's actual session ID when available. Generate one UUID and pass it as
`--session` only if the ledger reports no recorded session. A differing explicit
ID cannot override a hook-recorded identity.
For installation/customization, read `../reference.md`; AgentsRoom wind-down uses
its restart section as directed below.


## §1 Wind-down protocol

1. Finish the atomic action in flight. A process that must outlive this turn
   (suite, verifier, build) runs detached with a log; register it before saving:
   `python3 <ledger> run-register --pid N --log L --cmd C` (or `--command-id`).
   The save carries it, and the successor sees its live state. Never kill it to save. When a pressure notice supplies the minimal
   save contract, draft from it; otherwise read `../handoff-template.md`.
   `python3 <ledger> save --template` prints a valid JSON draft. Replace its
   facts with the authorized objective, constraints, evidence and exact next step.
   Include `## Requester` when known: who asked and where to reply, per the template.
   Record durable goals, constraints and follow-ups in `continuity` (schema in
   `handoff-template.md`); successor saves retain them until explicitly updated.
   Build a minimal continuation core, not a session report. Require only excerpts
   needed for that next action; keep source files and background as optional paths.
   Continuing an existing topic requires its authoritative `predecessor` path;
   use a distinct topic for independent work.
2. Run `python3 <ledger> save --input <draft.json>` (or `--topic T
   --description D --body file.md`; `--attach <file>[#Lstart-Lend]` adds a required reference).
   Session, request ID, project, your own predecessor and `verify` are deduced;
   pass flags only to override. Identical retries are idempotent.
   Success requires `outcome: saved` and a path; metadata and publication are
   generated. Keep `.published` sidecars with their checkpoints.
3. On any failure, follow the returned action and preserve the draft/session.
   A permission denial never authorizes bypassing the host or clearing. If no
   destination is allowed, give the checkpoint text to the user and report that
   automatic resumption is blocked.
4. Tell the user the checkpoint path. For an AgentsRoom `agents_restart`
   transition, follow [AgentsRoom restart](../reference.md#agentsroom-restart): observe
   supported capability, owned and local workers, and human holds; run
   `restart_preflight.py restart --root <root> --workers '<JSON list>' --host-capable
   [--hold]`, which reads identity, persists observations and preflight intent;
   then make the supported `agents_restart` call as the final tool action using
   the returned `restart_prompt`. End the turn immediately.
   Unknown observations, `blocked`, `pending`, `refused` and human holds preserve
   the checkpoint/session. `registered-process-running` (state `running`, or
   `unknown` when `ps` cannot read it) means wait for the run to exit or release
   it with `run-abandon <run_id> --reason TEXT`; never kill it. Reconcile pending intent instead of redispatching.
   For other transition kinds follow the save result's permitted action.
5. Stop. Remaining authorized work belongs to the receiving session.

To record anything after publishing, use note; never edit `.handoffs/*.md`:
`python3 <ledger> note <checkpoint-or-topic> --text "..."` appends a sealed line
that resume delivers after the body and a successor save folds in once.
