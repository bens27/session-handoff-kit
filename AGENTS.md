# Project skill activation

For changes to handoff lifecycle, context hooks, resumption, or harness architecture, read the canonical `~/.agents/skills/n-agentic-harnesses/SKILL.md` and only the references relevant to the change. This project activates that otherwise explicit-only skill; runtime variants remain optional supporting material.

For behavior changes to hooks, ledger state, or automatic resumption, read `~/.agents/skills/tdd/SKILL.md` and use a focused regression test. Documentation-only and metadata-only changes do not need a test-first workflow.

The optional workflow index is `~/.agents/skills/skill-index/SKILL.md`; consult it when choosing a requested specialist workflow. If a referenced personal skill is unavailable on another machine, continue with repository documentation and report the missing guidance when material.

## Cross-session messages

- Reports, bugs and questions for another project go in as a backlog ticket in that project (AgentsRoom `backlog_create`), so they outlive the sender's session.
- For live coordination with an agent, use the AgentsRoom mailbox (`agents_list_live` / `agents_send` / `agents_read_inbox`); it persists and works across CLIs.
- Never rely on a CLI's own session messaging (e.g. Claude Code `SendMessage`): names change per launch, delivery can be held, nothing persists.
