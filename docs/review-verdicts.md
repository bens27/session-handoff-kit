# Checkpoint review verdicts

A review is an observation about a specific saved checkpoint, not permission to
execute its next steps or evidence that its ticket is still current. The kit
returns `reviewed: accept|reject|none` during preparation, one-shot resume and
holder replay. The accompanying `review` object identifies the source, ticket
(if supplied), observation time, checkpoint ID and content fingerprint.

## Source contract

Use `handoff_review.resolve_review(native_review=None, tags=())` to resolve an
observation without I/O. Source order is:

1. An explicit native observation (`accept`, `reject`, or `none`). The string
   `none` records a known lack of completed review and supersedes older tags.
2. Exact `review:accept` or `review:reject` labels when the native source is
   unavailable (`None`). Duplicate identical labels are harmless; contradictory
   labels produce `none` with source `conflicting-tags`.
3. `none` when neither source establishes a verdict.

These are ticket/checkpoint labels supplied by the caller, not Git refs (Git
ref names cannot contain `:`). There is no network dependency. With the app
unavailable, pass the available labels only; no approval is inferred from Git
history, a clean worktree, a commit message, ticket status or reviewer IDs.
The future resume staleness adapter (#99) owns Git comparisons and live MCP
reads; this feature supplies its verdict-reading boundary.

`ticket_review(ticket=None, previous=None, reviewer_feedback_added=False,
tags=())` adapts already-read `backlog_get` **task objects**, not the response
wrapper. It performs no remote call and does not read AgentsRoom runtime files:

- A directly observed transition for the same ticket from `in_review` to `done`
  supplies native `accept`.
- A directly observed transition from `in_review` to `in_progress` or `todo`
  supplies native `reject` only when the caller verified newly appended
  **reviewer feedback**, setting `reviewer_feedback_added=True`.
- Other transitions and isolated status snapshots supply no native verdict;
  the current task's `tags` are the fallback. `reviewerAgentId` and
  `reviewSubmittedBy` alone do not establish a review.
- With no ticket snapshot, the explicit `tags` argument supplies the offline
  fallback. Snapshots for different ticket IDs are refused.

The caller must observe the actual review operation/transition; comparing
arbitrary old snapshots cannot distinguish reopening, manual moves or unseen
intermediate states. The kit does not infer feedback authorship from prose.
A normalized result is evidence supplied by that caller, not authenticated
proof from AgentsRoom. The review gate remains independent.

## Record and read

First read the checkpoint and its fingerprint during the review:

```sh
python3 skills/session-handoff/hooks/handoff_ledger.py review PATH
```

Then record the observation, using that exact fingerprint in a JSON file:

```json
{
  "fingerprint": "<fingerprint observed during review>",
  "ticket_id": "<ticket UUID>",
  "native_review": null,
  "tags": ["review:accept"]
}
```

```sh
python3 skills/session-handoff/hooks/handoff_ledger.py review PATH --input verdict.json
# --input - reads strict UTF-8 JSON from stdin instead.
```

`native_review` accepts `accept`, `reject`, `none`, or JSON `null`. Omit it to
use tags. `ticket_id` is optional; recording does not mutate the ticket. To
record a native adapter result, supply its `reviewed` value as `native_review`
**only if its source is `native`**. Otherwise retain the fallback labels; do
not relabel tag evidence as native. Re-record `native_review: "none"` to clear
an earlier observation, or record a new rejection/acceptance.

The Python equivalents are `record_review(path, fingerprint=..., native_review=...,
tags=..., ticket_id=...)` and read-only `read_review(path)`. Record returns the
saved observation; read returns the current projection. All dependencies are
Python standard library or kit modules.

The ledger stores JSON version 1 in `PATH.review`, under the checkpoint's
existing lock with an atomic write. It requires a published checkpoint and an
exact fingerprint match; a stale writer fails without overwriting evidence.
The checkpoint Markdown and publication receipt are unchanged. This avoids the
frontmatter writer owned by #95; header integration is deferred.

The fingerprint includes content and durable metadata but excludes lifecycle
fields such as claims and resumed status. The verdict survives acknowledgment,
while changed content or a different checkpoint ID invalidates it. New successor
checkpoints do not inherit review observations. Sidecars travel with their exact
checkpoint when copying evidence; they are not signed review attestations.
Missing, malformed, oversized (over 16 Ki characters), or mismatched records
project `none`; `source` distinguishes absent, invalid and stale evidence.
A valid stored verdict is a historical snapshot, not a live ticket query.

Preparation and resume include both `reviewed` and `review`, even with
`--fields path,body`. Replay refreshes the local review snapshot instead of
repeating an old verdict from its saved delivery package. Review status is
informational: rejection does not block retrieval, and acceptance bypasses no
claim, trust, verification, authorization or staleness check.

## Validation

`tests/test_handoff_review.py` covers source precedence, native transition
requirements, offline tags, corrupt/stale evidence, stale-writer refusal,
header preservation, retrieval/resume/replay, field projection and the CLI.
