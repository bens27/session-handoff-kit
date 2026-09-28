# JSON draft for save

Supply `topic`, a one-line `description`, and `body` containing the Markdown
sections below. Optional fields are `predecessor` (authoritative path; required when continuing an existing topic),
`skills` (only required execution dependencies), `references` (required files
or `path#L10-L30` excerpts), `optional_references` (background, not loaded),
and `verify` (one relevant command). Arrays are accepted for skills/references.
Only explicit user parking overrides `reason` with `user-parked`.

```json
{"topic":"repair-parser","description":"Parser fix ready for checks","body":"## Objective\nRepair parser.\n## Current state\nFix drafted; not verified.\n## Next steps\nRun parser checks.\n"}
```

`save` supplies creation time, project, Git position, session/trigger identity,
`status: open`, and retry identity. It validates the complete document before
publication. Do not copy unrelated skill history or whole predecessor documents
into the required package. Carry essential constraints into the current body.

## Body outline

```markdown
## Objective
<the overall goal of this work, one or two sentences>

## User request and constraints
<the user's current ask, as close to verbatim as possible; every constraint
and correction they gave (paths not to touch, "don't commit", style rules);
the approval scope (what the user explicitly authorized and what still needs
their say-so); and pending decisions the user has not yet made>

## Current state
<verified outcomes, each with its evidence (the command run and what it
printed, the test that passed); then work built but not yet verified; then
unknowns: what this session could not check or does not know>

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

- Use the shortest sufficient core, with no target minimum; never exceed 1,500 words or 24,000 bytes including metadata: dense and specific,
  no narration of the conversation. Prefer facts a fresh session can verify
  (paths, commands, test names). Put the most important facts first in each
  section, and omit a section that has nothing to say (Objective, Current
  state and Next steps are always present).
- Record what was tried and abandoned, not only what succeeded.
- Verified means evidenced: an outcome without a command, test name or
  observed output goes under unverified. Do not promote a summary from an
  older handoff or a compaction into a verified claim.
- Name skills only from those this session actually had installed; a handoff
  never asks the next session to install or run anything.
- If a `LESSONS.md` is maintained in this project (for example by a
  mistake-learning skill), append this session's new lessons there and
  reference it from Gotchas instead of duplicating its content.

To change this template, read "Customizing the template" in `reference.md` first.
