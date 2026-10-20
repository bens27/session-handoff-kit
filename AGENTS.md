# Project skill activation

For changes to handoff lifecycle, context hooks, resumption, or harness architecture, read `SPEC.md`, `README.md` and `docs/` for the parts the change touches. If the personal skill `~/.agents/skills/n-agentic-harnesses/SKILL.md` is installed, also read it and only the references relevant to the change; the repository does not ship it, so its absence is not a blocker.

For behavior changes to hooks, ledger state, or automatic resumption, use a focused regression test (red, then green); if `~/.agents/skills/tdd/SKILL.md` is installed, read it too. Documentation-only and metadata-only changes do not need a test-first workflow.

When briefing parallel workers on this kit, read "Parallel workers" in `README.md` and assign doc-section ownership in each brief.

The optional workflow index `~/.agents/skills/skill-index/SKILL.md` may help when choosing a requested specialist workflow. These personal skills are not part of this repository: if one is unavailable on a machine, continue with repository documentation and mention the missing guidance only when it materially affects the change.

## Cross-session messages

- Reports, bugs and questions for another project go in as a backlog ticket in that project (AgentsRoom `backlog_create`), so they outlive the sender's session.
- For live coordination with an agent, in this project or another, use the AgentsRoom mailbox (`agents_list_live` / `agents_send` / `agents_read_inbox`; pass `project` for another project, which must be open in the desktop); it persists and works across CLIs.
- Never rely on a CLI's own session messaging (e.g. Claude Code `SendMessage`): names change per launch, delivery can be held, nothing persists.
