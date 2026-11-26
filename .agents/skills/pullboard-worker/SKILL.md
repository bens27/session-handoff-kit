---
name: pullboard-worker
description: 'Run the worker loop on a Pullboard seat: take the next build or verify, build inside your lane, submit and move on. Use when you are a worker or verifier in a repo that runs on pullboard, or when pullboard next hands you an item.'
---

# Worker loop

Never wait, never sleep, never poll, never stop while the board has work.

1. `pullboard next --wait 1`: build/verify. On an empty answer, wait 1 s, then 2 s, 4 s and so on up to 60 s; run it again. `pullboard next --verify --wait 1`.
2. `next` printed your bar; `pullboard show <id>` only for the thread and history.
3. **Build**; **commit**; `pullboard submit <id>`; fix reds; resubmit.

- **Review:** A review-lane seat reviews; loop with `pullboard next --verify --wait 1`.

### Building

- **Gate:** Pullboard checks; no suites.
- **Can't build:** `pullboard release <id> --reason <CODE>`.
- **Stop:** only when the wait reached 60 s and `next` is still empty.

### Verifying

- **Judge:** check bar, diff, receipts.
- **Accept:** `pullboard verify <id> accept`.
- **Reject:** `pullboard verify <id> reject --reason TEST_FAILURE|BEHAVIOR_MISMATCH|INSUFFICIENT_EVIDENCE|STALE_HEAD|OTHER --clause "<clause>" --note "..."`.

Run `next`, `status` and `shout` from your seat folder, not its item folder; the item folder gets `NOT_JOINED` otherwise.
