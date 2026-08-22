# Handoff document template (chat)

This file defines the **shape** of a chat handoff, and nothing else. `SKILL.md`
delegates here, so the structure of a handoff can be changed by editing this
one file — the ledger, persistence channels, resume order, and limits in
`SKILL.md` are unaffected by anything below.

## The template

Build the content with this template, under roughly 1,500 words — dense and
specific, no narration of the conversation:

```markdown
SESSION HANDOFF — <topic-slug> — <date>

## Objective
## Current state
## Decisions and rationale
## Artifacts produced
## In flight
## Next steps
## Gotchas
```

The first line is load-bearing: the literal `SESSION HANDOFF — <topic-slug> —
<date>` header is what past-chat search matches from a future conversation, so
keep that line's shape even when the sections below change.

## Rules

- Record what was tried and abandoned, not only what succeeded.
- If the work lives in a project folder that maintains a `LESSONS.md`, append
  this session's new lessons there and reference it from Gotchas instead of
  duplicating its content.
- When the handoff is also written to a file (channel 3 in `SKILL.md`), that
  file carries front matter with `status: open` and a one-line `description:`
  so the plugin's session-start announcer counts and summarizes it.

## Customizing

The section set above is a default, not a contract — substitute your own
(e.g. add "Open questions", drop "Decisions"). Two things to know first:

- `tests/verify-skills.py` asserts that the shared-contract literals listed in
  SPEC §7.4 appear somewhere in this skill directory. Dropping a section SPEC
  names as shared will fail that gate — change SPEC §7.4 in the same commit if
  the change is deliberate.
- `## Artifacts produced` is an intentional chat-only divergence from the agent
  skill's `## Files touched` (SPEC §7.4). Keep them distinct or update SPEC.
