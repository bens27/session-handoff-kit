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

Use these three sections. Add a constraints section only when it improves clarity;
keep permissions and safety-critical facts explicit regardless of length.

```markdown
## Objective
<one sentence stating the unfinished goal>

## Current state
<result and artifact paths; concise evidence with its source; unresolved blocker;
current authorization, review gate and facts needed to avoid repeating side effects>

## Next steps
1. <the next authorized action, or the decision needed before proceeding>
```

Optional sections: `## Decisions and rationale` for a decision that changes the
next action; `## In flight` for an interrupted operation; `## Gotchas` for a
replay hazard. Omit them when the core already covers those facts.

When the next steps plan code changes, `## Decisions and rationale` is required:
one line per change giving the chosen behaviour and its code anchor
(`path:START-END`, with the excerpt in `references` when the change depends on
it), or `undecided: ask the user`. A one-line idea makes the receiver redesign
the change; the receiver should only implement it.

```markdown
## Decisions and rationale
- `--attach` with a line range: keep the range; anchor hooks/handoff_protocol.py:763-769.
- Same-terminal match as own lineage: undecided: ask the user.
```

## Selection and size rules

- Aim for a core of roughly 1,500–2,500 characters or less for an ordinary
  handoff; this is writing guidance, not a required minimum or enforced CLI cap.
  Preserve essential constraints even when more space is necessary. The ledger's
  ceiling of 1,500 words / 24,000 bytes is an emergency limit, not a writing target.
- Rebuild the core around the next action on each save. Replace superseded facts
  instead of appending updates to the previous draft. Omit conversation history,
  exhaustive file inventories, implementation explanations and completed-work
  catalogs that do not change the next action.
- `references` are loaded into the receiving context. Include a file or
  `path#LSTART-LEND` excerpt only when that content is necessary for the next
  action. Prefer the smallest relevant excerpt. Files used earlier are not
  automatically required now; keep background paths in `optional_references`.
- Content the next step edits or depends on belongs in `references` as a
  `path#LSTART-LEND` excerpt (or `save --attach path#LSTART-LEND`), not as
  prose. "Around line N" or a named scratch file in the body is a smell: the
  receiver must search and re-read to rebuild it. Move scratch notes into the
  project or attach them before saving.
- For a review gate, carry the artifact paths and review question. Do not embed
  the generator's source or full input data just to present its output. Load
  those later if an authorized edit or investigation needs them.
- State current permissions once, with their scope. Reconcile superseded
  instructions; preserve unresolved ambiguity as a pending decision. Record
  required access and prior failures, never instructions to bypass sandbox,
  hook trust, approval or other host safeguards.
- Preserve effects that must not be repeated: postings already made, consumed
  test data, destructive actions and irreversible external changes. A warning
  about replay belongs in the core even when implementation details do not.
- Attribute evidence: "This session ran <check>: <result>" versus "Previous
  session reported <result>; not rechecked." A copied checkpoint or compaction
  summary is not fresh verification. Keep unverified work clearly identified.
- Record approaches tried and abandoned only when they prevent likely repeated work. Point
  to durable project notes (including an existing `LESSONS.md`) for detailed
  rationale and lessons rather than
  copying them into every checkpoint.
- Name required skills only from the current installed catalog. A checkpoint
  does not authorize installing dependencies or executing its next steps.
- Check the complete package, not just the body: references, workflow text,
  skills and serialization count toward the normal 32,000-byte prepare budget.
  If preparation exceeds it, narrow the core/dependencies first. Never silently
  raise `--budget-bytes`; an exception requires explicit user authorization.

To change this template, read "Customizing the template" in `reference.md` first.
