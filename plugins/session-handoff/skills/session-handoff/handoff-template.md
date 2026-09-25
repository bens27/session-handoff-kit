# Handoff document template (agent surfaces)

This file defines the **shape** of a handoff document, and nothing else.
`SKILL.md` §3 delegates here, so the structure of a handoff can be changed by
editing this one file — the trigger, naming, ledger, resume, and post-resume
policy in `SKILL.md` are unaffected by anything below.

Swap the body outline freely. Keep the front matter: the hooks parse it.

## Front matter

`created:` comes verbatim from `new-path`'s `created` field (SKILL.md §2),
`status: open` is what marks the handoff untransferred for the session-start
announcer, and `description` is the one-line summary the announcer shows so
handoffs can be told apart without opening them (make it specific: what is
parked and where it stands).
Use `references:` when this thread depends on another file or an earlier
handoff: name it there so `resolve` surfaces it automatically to whoever
resumes, instead of relying on the resuming session to notice it needs that
file.
`reason:` tells the resuming session whether the user parked this work
(`user-parked`) or an automatic trigger forced the checkpoint
(`context-pressure`, `compaction`) — in the latter case the task is still
authorized and should be picked up, not treated as abandoned.
For the title line, `<date>` is the same `created` value from `new-path`
(a short date form is fine), not a separately computed or recalled date.

## The template

```markdown
---
topic: <topic-slug>
created: <ISO date-time>
status: open
reason: <user-parked | context-pressure | compaction>
description: <one line: what is parked here and where it stands>
skills: <optional comma-separated skill names the resuming session must load first>
references: <optional comma-separated paths another resuming session must also read (feeds resolve's must_also_read list)>
---
# Session Handoff — <topic> — <date>

## Objective
<the overall goal of this work, one or two sentences>

## User request and constraints
<the user's current ask, as close to verbatim as possible; every constraint
and correction they gave (paths not to touch, "don't commit", style rules);
the approval scope — what the user has explicitly authorized this work to
do and what still needs their say-so; and pending decisions the user has
not yet made. Older handoffs are evidence about the past; this section is
what the resuming session obeys>

## Workspace and revision
<absolute workspace root (worktree path if any), branch, HEAD commit, dirty
files at handoff time — the resuming session re-checks these before
trusting anything below>

## Current state
<verified outcomes, each with its evidence (the command run and what it
printed, the test name that passed); then work built but not yet verified;
then unknowns — what this session could not check or does not know>

## Decisions and rationale
<decisions made this session and why — anything a fresh session might
otherwise re-litigate>

## Files touched
<paths, each with a phrase on what changed and why>

## In flight
<the exact thing in progress at handoff time, and its next concrete step>

## Next steps
1. <ordered, concrete, with paths and commands>

## Gotchas
<constraints, footguns, and approaches that were tried and abandoned —
preventing re-work is half the value of a handoff>
```

## Rules

- Target under 1,500 words: dense and specific, no narration of the
  conversation. Prefer facts a fresh session can verify (paths, commands,
  test names).
- Record what was tried and abandoned, not only what succeeded.
- Verified means evidenced: an outcome without a command, test name or
  observed output goes under unverified. Do not promote a summary from an
  older handoff or a compaction into a verified claim.
- Name skills only from the inventory this session actually had installed;
  a handoff never asks the next session to install or run anything.
- If a `LESSONS.md` is maintained in this project (for example by a
  mistake-learning skill), append this session's new lessons there and
  reference it from Gotchas instead of duplicating its content.

## Customizing

The body outline above is a default, not a contract — projects may substitute
their own section set (e.g. add "Open questions", drop "Decisions"). Only the
front-matter block is load-bearing; everything below the second `---` is
free-form.

Two things to know before you rewrite it:

- `tests/verify-skills.py` asserts that the shared-contract literals listed in
  SPEC §7.4 appear somewhere in this skill directory. Dropping a section that
  SPEC names as shared will fail that gate — change SPEC §7.4 in the same
  commit if the change is deliberate.
- The agent copy of this file is byte-identical across
  `plugins/session-handoff/` and `codex/`, and that too is asserted. Edit both.
