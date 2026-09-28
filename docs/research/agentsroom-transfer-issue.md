# AgentsRoom: transfer completed task state instead of terminal UI transcripts

Observed with the locally installed AgentsRoom 1.193.0; supporting Codex sessions are from September 27, 2026. The installed app still contains the transcript-resume prompt. Session Handoff Kit cannot fix this host-owned transition without an upstream change.

## Problem

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
