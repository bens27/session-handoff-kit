# Report-back drafts

Successful one-shot `resume` and restart `adopt` return a `report_back_draft`
object with `status: draft` and the path of a local JSON file. Open that file to
review or edit its `message`. The kit never sends it: there is no transport,
MCP send/reply call, recipient lookup, or network write in draft generation.
The successor decides whether to send, using its own authorization.

The file contains `status: draft`, `sent: false`, `requester`, `ticket`,
`checkpoint`, and `message`. `sent` records only that the kit did not send it;
it does not track later actions by the successor. The draft includes the saved
Objective, Current state, and Next steps without truncating them. Saved state
is labelled historical and next steps are labelled planned, not completed.
It does not claim that work has landed, that tests passed, or that the ticket
is current beyond what the checkpoint itself says.

Use an optional `## Requester` section in the checkpoint body to name the
intended recipient. The ticket comes from the validated `ticket:` header.
Missing requester or ticket values remain JSON `null` and appear as
`[unspecified]` in the message. The kit does not infer a recipient from the
checkpoint's author, role, session, or prose elsewhere. These values are draft
content, not authenticated routing instructions; confirm them before sending.

Drafts live in a separate subdirectory of transfer state under
`~/.local/state/session-handoff/transfers/drafts/<transfer-key>.json.report-back.json`.
The key binds the project, successor session, and checkpoint path. Creation
uses the existing atomic local JSON writer while holding the checkpoint lock.
The draft adds no checkpoint-header or body mutations. It is written after
preparation and verification but before acknowledgment so a successful transfer
always has a saved draft. A later acknowledgment failure can leave a draft;
its existence alone does not prove that resumption succeeded. A draft-write
failure blocks acknowledgment with stage `report-back` and allows retry.

Replay returns the same file path without overwriting successor edits or
sending anything. Deleting that file during the replay window does not cause
replay to regenerate it. Older deliveries created before this feature retain
their original payload and do not gain drafts retroactively. Drafts are kept
when replay payloads expire; the user controls their retention. `--fields`
projection preserves the path on successful transfers. Plain retrieval,
selection, and refusal before draft generation do not create drafts.

Automatic checkpoint closure is a separate feature. Generating this draft
performs no backlog read, Git fetch, landing detection, or closure operation.
