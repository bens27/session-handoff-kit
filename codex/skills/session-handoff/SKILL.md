---
name: session-handoff
description: >
  This skill should be used immediately whenever a "[context-watch]" message
  appears in the conversation — that is the deterministic signal that context
  usage has crossed its threshold, or that open handoffs are awaiting resume at
  session start — and whenever the user says "hand off", "handoff", "wrap up
  the session", "prepare a handoff", "write a handoff doc", "park this work",
  or "continue this in a new session", or when context is nearly exhausted or
  auto-compaction is imminent. Also use it to resume: at session start when
  open handoffs are announced, or whenever the user asks to resume or pick up
  parked work.
metadata:
  version: "0.3.0"
---

# Session Handoff

Preserve working state across a context boundary. Produce a handoff document a
fresh session can resume from with zero shared context, then stop. Handoffs
carry an `open`/`resumed` status so session starts can announce untransferred
work automatically and stay silent about work already picked up.

## When the trigger fires mid-task

1. Do not start new work. Complete only the single atomic action already in
   flight (finish the current file edit or the command that is running).
2. Write the handoff document as specified below.
3. Tell the user the handoff is complete and give the exact resume path: start
   a new session in this directory and the open handoff will be announced
   automatically (or `claude "resume"` / `codex "resume"`).
4. Stop. Do not begin any of the "Next steps" in this session.

## Writing the handoff

Write to `./.handoffs/<topic-slug>.md`, where the slug is a short kebab-case
name for the thread of work (`auth-refactor`, `pantry-import`). One file per
thread; overwrite the same file when handing off the same thread again. The
legacy single-file location `./HANDOFF.md` remains supported and is treated as
topic "default".

Start the file with this front matter — the `status: open` line is what marks
it untransferred for the session-start announcer:

```markdown
---
topic: <topic-slug>
created: <ISO date-time>
status: open
---
# Session Handoff — <topic> — <date>

## Objective
<the overall goal of this work, one or two sentences>

## Current state
<what is done and verified working; what is built but not yet verified>

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

Rules:

- Target under 1,500 words: dense and specific, no narration of the
  conversation. Prefer facts a fresh session can verify (paths, commands,
  test names).
- Record what was tried and abandoned, not only what succeeded.
- If a `LESSONS.md` is maintained in this project (for example by a
  mistake-learning skill), append this session's new lessons there and
  reference it from Gotchas instead of duplicating its content.

## Resuming

At session start, a `[context-watch]` notice lists any open handoffs.

- **One open handoff**: read the file in full before any other action, restate
  the objective and the first next step in one or two sentences, confirm with
  the user unless configuration or the user has said to proceed, then continue
  from "Next steps".
- **Multiple open handoffs**: before any other work, present the list and ask
  which one to resume — use an interactive question tool if available —
  including a "none of these" option. Then resume the chosen one as above.
- **Either way**: if the user's opening request is an unrelated explicit task,
  mention the open handoff(s) in one sentence and do their task instead; the
  handoffs stay open for next time.

Once a handoff is actually resumed, mark it transferred by running the exact
`resume` command included in the notice (it invokes `handoff_ledger.py resume
<path>`). This flips `status: open` to `status: resumed` so future sessions
stop announcing it. Do not mark a handoff resumed merely because it was
announced or listed. Read `LESSONS.md` as well if the handoff references it.

## Notes

- The deterministic trigger is a lifecycle hook (`hooks/context_watch.py`)
  that reads the session's own transcript token usage and fires once per
  session; a `SessionStart` hook runs the announcer. `hooks/handoff_ledger.py`
  tracks open vs resumed and can be run directly: `list` and `resume <path>`.
- Thresholds are absolute tokens per model (quality degrades at an absolute
  occupancy, not a percentage of the window): CONTEXT_WATCH_TOKENS_MAP, then
  ./.context-watch.json, then ~/.context-watch/thresholds.json, then a global
  CONTEXT_WATCH_TOKENS, with 130,000 as the built-in default. Other knobs:
  _PENDING, _LOG (analytics; `context_watch.py stats`), _WINDOW, _SKILL,
  _MODE, _AGENT, _DISABLE, _MAX_AGE_DAYS, _AUTORESUME — see the plugin
  README. With CONTEXT_WATCH_AUTORESUME=1 a single open handoff is resumed at
  session start without asking.
- In environments without hooks, this skill still works: check `./.handoffs/`
  and `./HANDOFF.md` for `status: open` entries whenever beginning work in a
  folder, announce them in one sentence, and follow the same resume procedure.
