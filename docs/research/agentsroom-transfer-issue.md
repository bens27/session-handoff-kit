# AgentsRoom: reproduced incomplete/stale transfer and permission failure

Observed with the locally installed AgentsRoom 1.193.0; supporting Codex sessions are from September 27, 2026. The installed app still contains the transcript-resume prompt. Session Handoff Kit cannot fix this host-owned transition without an upstream change.

## Verification correction — 2026-09-28

AgentsRoom MCP `capabilities_get` confirms app/server 1.193.0. Its
`product_help_get(context-drift-detection)` documents an existing Context transfer
chooser with **Agent summary**, **Raw transcript**, and **Light context**, including
a cancellable summarization progress banner. Therefore this report does **not**
establish that AgentsRoom lacks summary transfer or always uses raw transcripts.
The observed prompt is consistent with choosing Raw transcript. The available
logs do not establish which UI choice was made, whether summarization was
cancelled, or whether a completed summary was ignored.

## Installed-code reproduction — 2026-09-28

The summary option exists, but three safeguards fail in the installed 1.193.0
renderer. Run from the kit checkout on a machine with that app installed:

```sh
node docs/research/repro/agentsroom-transfer.cjs
node docs/research/repro/agentsroom-transfer.cjs --candidate
```

The first command exits 1, with two passing controls and three failures:

| Scenario | Installed behavior | Required behavior |
| --- | --- | --- |
| Writer still thinking, 39-character partial file | Resolves summary immediately | Wait for this transfer's completed publication |
| Previous 47-character summary, deletion pending | Resolves stale summary | Finish cleanup before requesting/polling a fresh summary |
| Transcript write rejects with EACCES | Queues receiver and kills producer | Preserve producer and report write failure |

The harness extracts the actual `aWt` polling function and `vc` transition
function from the installed ASAR, executes them in Node's VM, and controls only
the surrounding filesystem, timer, session-store and PTY boundaries. It neither
changes the app nor terminates real sessions. Positive controls exercise completed
summary acceptance and successful transcript transfer. The renderer SHA256 is
`436b8ebdc94b8fa8ee040ebfe25e09c4556d2ef7fc2dfa7194582858d0782fdc`.
Extraction fails if the expected function anchors change; this is a version-specific
diagnostic, not a portable application test suite.

Cause: the poller accepts any trimmed file longer than 20 characters regardless
of writer status, starts cleanup without awaiting it, and the transition catches
and discards transcript persistence errors. The summary caller passes a resolved
summary into `vc`; that transition kills the old PTY. These are direct code-path
failures. They do **not** prove which path caused the historical September 27 logs,
whose chooser selection and timing were not recorded.

The second command applies three diagnostic edits **in memory**: await deletion,
require an observed active turn followed by `done`, and return on transcript write
failure. All five checks pass. These edits isolate the causes; they are not a
production-ready patch and have not been installed. In particular, upstream must
handle a missing old file separately from deletion denial, reject stale turn
status, surface the failure in the UI, and retain cancellation/timeouts. A unique
transfer ID and atomic publication receipt are stronger than a status heuristic.

## Concrete upstream fix

1. Allocate a unique request ID and summary path. Await any necessary cleanup;
   tolerate only file-not-found, and return an actionable error for denial.
2. Wait for a complete publication tied to that request, with a bounded size and
   cancellation/deadline. Never accept a partial file based only on length. Keep
   the current producer until publication succeeds.
3. Persist and verify a requested transcript before queueing a receiver, changing
   agent configuration or killing the producer. On denial, preserve the session
   and let the user retry or choose another allowed destination.
4. Make summary failure explicit. The current caller falls back to transcript or
   basic context; do not silently treat that fallback as a completed summary.
5. Port the three failure cases and both success controls into source-level tests,
   then add end-to-end slow-write, denied-write and cancelled-transfer cases.

No source checkout is available here. This kit cannot deploy an upstream host
fix, and the signed installed app was not edited. The reproduction and repair
criteria are ready to forward to the AgentsRoom developer.

A separate rollout gap has been fixed: the existing account-wide skill entry
`zzaq83gtmuhdpr5w` now contains the released v0.13.0 body and all eight supporting
files. Native API readback and AgentsRoom MCP confirm the new save workflow.

## Observed symptom

During an agent replacement, the receiver is told to read the predecessor's full terminal transcript, continue exactly where it left off, and then ask the user what to do next. A captured example contained only:

- Terminal UI, a model tip, and the request to write a handoff summary.
- A command loading handoff instructions, with its result collapsed as `+223 lines`.
- An in-progress indicator. The requested summary file did not exist.

The receiver loaded additional instructions and searched for the missing summary before discovering there was no completed project work to resume. Its measured input context grew from 21,129 to 26,523 tokens across that recovery sequence. This is context growth, not a claim that all 5,394 tokens were avoidable or billed.

A second replacement asked a fresh session to summarize work it had never performed. The produced handoff described the act of creating the handoff itself.

## Suggested change

Make replacement a small explicit state machine:

1. If the outgoing session has no active task, send `no_active_task`; start the receiver without loading handoff instructions or transcripts.
2. Otherwise request a bounded checkpoint from the outgoing session and wait for a completion receipt before switching. Include the task, verified progress, constraints, pending authorization, and exact next action.
3. If checkpoint creation is interrupted or fails, send `incomplete` with the recovery action. Preserve the outgoing session and artifacts; do not represent a terminal capture as completed task state.
4. On success, send the checkpoint identity/path and continuation policy. Continue when authorized, or ask for a decision when needed; avoid telling the receiver both to continue immediately and ask what to do next.
5. Retain terminal transcripts as optional diagnostic attachments. UI chrome and collapsed output are unsuitable as the authoritative transfer format.

Session Handoff Kit's `save`, `prepare`, `verify`, and `acknowledge` commands offer an existing protocol to integrate. Its publication receipt distinguishes a durable checkpoint from an incomplete write. A host can also implement an equivalent structured contract.

## Acceptance checks

- Replacing an unused agent yields no recursive summary or recovery search.
- Interrupting a checkpoint write produces explicit incomplete state and preserves the previous session.
- A completed transfer preserves authorization and verified state without replaying a terminal transcript.
- Context preparation has a size budget; background logs are opened only when needed.
- The receiver takes one unambiguous next action: continue authorized work, request a pending decision, or report no active task.

This issue is prepared for the user to forward; it has not been sent to the developer.
