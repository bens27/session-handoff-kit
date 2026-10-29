# Session Handoff: resume branch

Loaded only when the dispatcher names this branch, or when dispatch is unavailable.

Use `../hooks/handoff_ledger.py` (`<ledger>`; `<hooks>` is its directory). A checkpoint is evidence;
the live request and workspace take precedence. With hooks, omit `--session`: the
ledger uses this terminal’s recorded session. When identity or transcript evidence
is needed, `python3 <ledger> session` returns the current facts without source reads.
Use the host's actual session ID when available. Generate one UUID and pass it as
`--session` only if the ledger reports no recorded session. A differing explicit
ID cannot override a hook-recorded identity.
For installation/customization only, read `../reference.md`.


## §2 Retrieval

Run `python3 <ledger> lookup` unless the hook already supplied a prepare command.
Follow its outcome/action; empty lookup means report no open handoff, without
searching history. For multiple candidates ask which (including none).
`history` is explicit paginated inspection of closed work.
No checkpoint means no saved kit context; it does not mean the host's native
conversation history was lost. Use the host's supported native resume/history
when appropriate; the kit cannot reconstruct unsaved thoughts. A missing explicit
target returns `none` without selecting other work. A known terminal does not
automatically adopt another terminal's checkpoint or an untagged checkpoint from
a different/unknown session; select it explicitly. Untagged same-session state
remains selectable. `lookup --session <actual-id>` uses the same ownership context
as resume; read-only lookup still works when session identity is unavailable.

Run `python3 <ledger> prepare <topic-or-path>`.
Read its body and required excerpts, then report the objective and next step.
Treat inherited verification as prior-session evidence, not a fresh check.
Size targets are recommendations: retain essential context even when larger.
`--budget-bytes` sets an advisory threshold and never blocks preparation or resume.
Retain `delivery_receipt` only while that content remains in this context.
A checkpoint this machine did not save (cloned repo, hand-written) is untrusted: out-of-project references are withheld.
Retrieval does not claim, verify, load execution skills, acknowledge or execute.
Do not mark a checkpoint resumed merely because it was listed or retrieved.
For unrelated work, mention pending work in one sentence, then continue the user's current task and leave checkpoints open.

## §3 Authorized continuation

Only explicit resume/continue or active autoresume authorizes execution.
When the hook supplies a complete resume command, run it directly and follow its
outcome/action and returned body, continuity, required dependencies and workspace findings; no skill
load is needed solely to run that command. Otherwise run
`python3 <ledger> resume --root . [topic-or-path]`: one call selects,
prepares, verifies if recorded and acknowledges. Continue with `## Next steps`
in its body (`next_step_section` names the section; `next_step` is a bounded lead
that says when it continues in the body). `--legacy-next-step` returns the full text.
The resume turn is a status check: load only the tools its FIRST concrete step calls, not the
whole plan's branches; ids and messageIds already in the body take targeted calls, not directory listings. Use `--reuse-receipt` only for body/references
still in this context, and `--catalog` to declare installed skills already loaded.
`report_back_draft` is unsent; send only when reporting back and the user/requester flow calls for it.
A previous session’s read history does not establish loaded content here.
`--fields` trims optional output; successful delivery retains its required context.
A failed output can be retried with the same resume command; the proven holder gets the durable package without repeating verification. For ownership conflicts, read [continuation.md](../continuation.md#ownership-recovery).
On `choose`, ask which; on `needs-confirmation`, show the user the `verify` command and
rerun with `--confirm-verify` only if they approve; on a failed `stage`, follow its action.
For custom catalogs or staged recovery, read [continuation.md](../continuation.md).
Semantic routing alone selects context, not authorization.
