# Handoff integrity and cost review — 2026-09-28

Reviewed kit commit `585d80f` (runtime v0.13.0), including the ledger, public
protocol, watcher, installer, automatic runner, skill instructions and extension.

**Initial verdict on v0.13.0: six reproducible gaps blocked sign-off.** They are
kit-owned behaviors, separate from the earlier AgentsRoom transfer defects.
The initial review did not modify production configuration or checkpoints.

**Resolution in v0.14.0:** all six findings below are fixed. Each original failure
was observed before its fix. The safety suite now has 11 passing tests, including
concurrent publication, read-only phrase routing, one-attempt optional inference,
failed-child stopping and explicit run-budget exhaustion. The existing 30 public
protocol tests and six supporting suites also pass. The findings below preserve
the original evidence; they no longer describe the corrected runtime.

The bounded claim is that these reproduced failure paths are controlled. Summary
accuracy and total host spend remain outside what these tests can guarantee.

## Reproduction and scope

```sh
python3 docs/research/repro/kit-safety-review.py
```

The original v0.13.0 audit exited 1: seven tests, one positive control passed,
and six tests failed (seven assertions because the phrase test covered two
phrases). The command now runs the expanded CI regression suite and exits 0.
Three repeat runs produced identical results. Fixtures isolate HOME, TMPDIR,
project files, logs and hook registration. The routing HTTP boundary returns a
fixture response; no API requests, real credentials or model calls are used.
The failed-runner fixture is a tiny local Python program, not an agent.

The existing 30 public protocol tests and all six supporting verification suites
(packaging, ledger, watcher, skills, trigger corpus, extension) passed. Those
checks do not cover the new cases. The reproduction now delegates to `tests/test_safety.py`, which is included in
CI; the original failure results are recorded below.

## Findings, ordered by priority

### R1 — P1: installation masks an existing hook's failure

Location: `skills/session-handoff/install.py:114`.

When an event already has a hook, installation combines its command and the
watcher using sequential shell commands. The final command determines the exit
status. Installing beside a hook whose command is `exit 2` produces a combined
command that exits **0**. The original failure signal is lost; a host relying on
that status cannot enforce the original block.

Fix: register a separate hook on supported modern runtimes, or implement a
version-specific adapter that preserves the original status and response
contract. Test both blocking failures and structured output. Do not silently
rewrite another hook's behavior. The eleven profiles just audited contain only
the standalone watcher command, so this reproduction does not establish a
currently masked hook in those profiles.

### R2 — P1: a second writer can hide a claimed topic

Location: `skills/session-handoff/hooks/handoff_protocol.py:90`.

Writer A saves topic `work`; a receiving session prepares and claims it. Writer B
saves a new `work` checkpoint without `predecessor`. That save succeeds and
becomes authoritative. The original owner's acknowledgment is now blocked.
The old file survives on disk, but its work disappears from normal topic
selection. A publisher lock serializes the writes; it does not enforce topic
ownership when no predecessor was supplied.

Fix: require explicit, validated lineage to replace an existing topic; reject
conflicting ownership under the topic/publication lock. Independent work needs
an independent topic. Test concurrent publishers as well as this sequential
ownership violation.

### R3 — P1: an empty legacy checkpoint can be acknowledged

Location: `skills/session-handoff/hooks/handoff_ledger.py:224`.

A `.handoffs/work.md` containing only valid front matter (`topic: work`,
`status: open`) and no body is accepted by `prepare --execute`. `acknowledge`
then marks it **resumed**. Section checks run only when the body is nonempty.
Thus an incomplete manual/legacy write can be treated as a complete transfer.
This is distinct from the publication receipt protection on modern `save` files.

Fix: validate nonempty mandatory content before every executable preparation;
keep incomplete legacy work visible as incomplete and leave it unacknowledged.
Retain compatibility for valid legacy handoffs.

### R4 — P2: automatic execution continues after child failure

Location: `skills/session-handoff/hooks/context_watch.py:822`.

A child writes a valid checkpoint and exits 1. `auto --max 3` launches it **three
times**, continuing solely because a fresh checkpoint exists. A real agent that
fails after checkpointing could therefore consume repeated sessions. The default
run-count ceiling is ten; there is no aggregate token, money or wall-time ceiling.
A run-count limit alone cannot bound the cost of an individual child.

Fix: stop on nonzero child status by default, preserve its checkpoint, and make
any retry policy explicit and bounded. Surface exhaustion distinctly from normal
completion. Require a host-enforced budget for any promise about total spend.
Automatic mode and autoresume were unset in this reviewing process; this finding
requires opting into the automatic runner.

### R5 — P2: semantic routing defaults to network use and can repeat

Location: `skills/session-handoff/hooks/context_watch.py:682` and `:943`.

With a routing key present and no `CONTEXT_WATCH_JEV` setting, an unrelated
opening request is sent to the external classifier. Repeating the same prompt
and session when transcript usage is unavailable generates **two HTTP requests**;
there is no persisted once-per-session guard. Missing/unreadable usage is treated
as an opening prompt, not as unknown state. Explicit supported routing can be
local, so key presence alone is too broad a trigger for this extra inference.

Fix: make semantic routing explicit opt-in; use deterministic routing by default,
then deduplicate/bound optional classification per session and expose its usage.
The current reviewing process has a routing key and no opt-out setting. This
establishes eligibility, not proof that a historical billed call occurred.
`CONTEXT_WATCH_JEV=0` is the existing opt-out; it was not changed during this audit.

### R6 — P2: common retrieval phrases incorrectly report absence

Location: `skills/session-handoff/hooks/context_watch.py:718`.

With a valid `work` checkpoint present, both `retrieve handoff` and `resume
handoff` return **No open handoff found**. The direct-topic regex treats `handoff`
as a literal topic and filters out `work` before the shared policy recognizes the
generic phrase. Bare `resume` passes the control. This can provoke unnecessary
searching, repeated prompting or abandonment of recoverable work.

Fix: recognize generic commands before topic filtering; retain exact named-topic
and path selection. Add coverage for the documented phrase vocabulary.

## What the kit already protects

- Modern saves have bounded document size, atomic publication and a receipt.
- Default preparation budgets the body, required references, workflow
  instructions, loaded dependencies and JSON overhead together at 32,000 bytes.
- Optional references are not automatically delivered; unchanged same-context
  retrieval can reuse a delivery receipt.
- Preparation, verification and acknowledgment are distinct operations.
- Changed checkpoint/dependency fingerprints and expired claims block acknowledgment.
- Normal retrieval is read-only and does not authorize continuation.
- Trigger latches, a startup-context floor check and automatic-run caps reduce
  repeated handoffs. They are mitigations, not guarantees of semantic progress.
- The extension defaults to no automatic sending and no always-on injection.

## Cost evidence and limits

The live protocol report contains only **three classified live records**: two
saves and one retrieval (6,091 package bytes). It excludes 49 test records and
leaves ten legacy records unclassified. That is insufficient to estimate savings,
retry frequency or a reliable cost ceiling. The watcher log contains 360 older
trigger records spanning versions; they are not billing records and lack the
provenance needed to attribute current-version waste confidently.

Byte estimates are not tokenizer measurements or prices. The kit cannot prove
that an agent wrote a semantically accurate summary, kept every user constraint,
or made useful progress. Nor does it govern the host's unrelated model calls or
AgentsRoom's separate transfer path. These limits should remain explicit even
after all six defects are fixed.

## Acceptance before sign-off

R1–R6 are repaired, their public-interface regressions are in CI, and the
complete existing suite remains green. Verify one real save → retrieve → authorized prepare → acknowledge →
continue cycle in each supported host, including an interrupted write and a
failed child. Collect correctly classified live delivery/retry measurements
before claiming economic improvement. A reasonable sign-off is that tested
failure paths are controlled; a guarantee of no bad handoffs or excessive spend
would still be unsupported.
