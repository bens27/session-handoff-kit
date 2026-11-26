# Project skill activation

For changes to handoff lifecycle, context hooks, resumption, or harness architecture, read `SPEC.md`, `README.md` and `docs/` for the parts the change touches. If the personal skill `~/.agents/skills/n-agentic-harnesses/SKILL.md` is installed, also read it and only the references relevant to the change; the repository does not ship it, so its absence is not a blocker.

For behavior changes to hooks, ledger state, or automatic resumption, use a focused regression test (red, then green); if `~/.agents/skills/tdd/SKILL.md` is installed, read it too. Documentation-only and metadata-only changes do not need a test-first workflow.

When briefing parallel workers on this kit, read "Parallel workers" in `README.md` and assign doc-section ownership in each brief.

The optional workflow index `~/.agents/skills/skill-index/SKILL.md` may help when choosing a requested specialist workflow. These personal skills are not part of this repository: if one is unavailable on a machine, continue with repository documentation and mention the missing guidance only when it materially affects the change.

## Cross-session messages

- Anything for another agent on this board goes through pullboard: `pullboard shout <lane|agent|coordinator> "<text>"` for coordination, `pullboard shout coordinator "<what is needed>" --decision` for anything that needs the person, and a board item (`pullboard add`) for work that must outlive the sender's session.
- Never rely on a CLI's own session messaging (e.g. Claude Code `SendMessage`): names change per launch and delivery can be held.

## Codex seats

Codex's sandbox must also be able to write `.git/refs/pullboard` and `~/.pullboard`, or a Codex seat can read the board but cannot record a claim, submit or verdict. Launch it with all three folders writable:

    codex --add-dir "$(git rev-parse --path-format=absolute --git-common-dir)/pullboard" --add-dir "$(git rev-parse --path-format=absolute --git-common-dir)/refs/pullboard" --add-dir "$HOME/.pullboard"

In `~/.codex/config.toml`, give each of the three paths `= "write"` in the profile's `[permissions.<profile>.filesystem]` table (or list them under `writable_roots` when no profile is used). The managed pullboard section below names only `.git/pullboard`; this section adds the other two.

<!-- pullboard:start -->
## Pullboard

This repo runs on Pullboard: board, lanes and spec, all in git.

- **Spec:** `SPEC.md` rows, each with an id like `G1.2`; code follows them.
- **Commits:** `feat(scope): subject [G1.2]`, citing the ids served.
- **Seats:** an agent is its worktree; the main checkout is the coordinator.
- **Lanes:** open and join one with `pullboard worktree <lane> --model "<model name>"`.

## Roles

Agents think; Pullboard does everything deterministic.

| Role | Does |
|---|---|
| Coordinator | One per board. Files, routes, unblocks; never builds. Reads `COORDINATOR.md`. |
| Worker | Builds and verifies whatever `next` hands it. |
| Sub-agent | Any agent may launch one. Runs the worker loop as written, on its own seat. |
| Pullboard | Runs gates, routes verifies, lands accepted work. |

## Worker loop

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

## Troubleshoot

- **A refusal:** do what its `next:` line says.
- **An item:** `pullboard show <id>` prints its last refusal.
- **Your state:** `pullboard whoami`.
- **Stale work:** `pullboard resume --diagnostics`.
- **Setup:** `pullboard doctor`.

## Warning

Still stuck? Tell the coordinator, with the refusal word for word:

```
pullboard shout coordinator --kind blocker "<what; the refusal>"
```

Need an override? Only the person grants one:

```
pullboard escalate <id> --note "what was tried"
```

## Never

- Bypass a hook with `--no-verify`.
- Edit the board's database.
- Verify your own work.
- Change an approved spec row without the person's OK.
<!-- pullboard:end -->
