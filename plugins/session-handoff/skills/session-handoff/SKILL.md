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
  version: "0.4.0"
---

# Session Handoff

Preserve working state across a context boundary. Produce a handoff document a
fresh session can resume from with zero shared context, then stop. Handoffs
carry an `open`/`resumed`/`superseded` status so session starts can announce
untransferred work automatically and stay silent about work already picked up.

## How this file is organized

Each numbered section below is a self-contained policy, deliberately bounded
so it can be customized on its own without touching the others:

- **§1 Wind-down protocol** — what to do the moment the trigger fires.
- **§2 Naming and location** — where handoff files live and how they are
  named. Swap in your own naming convention here.
- **§3 Document structure** — the handoff template. Replace the body outline
  with your own preferred structure here.
- **§4 Resuming** — how announced handoffs are retrieved and adopted.
- **§5 After resuming** — the extension point for actions that should always
  run right after retrieval (loading another skill, running a status
  command, opening a tracker). Empty by default; add yours here.
- **§6 Mechanics** — how the hooks and ledger work. Reference, not policy.

**Invariants the hooks depend on — keep these through any customization:**
handoff files end in `.md` and live under `./.handoffs/`; front matter is
fenced by `---` lines; new handoffs carry `status: open` plus a one-line
`description:`; state changes go through the ledger commands
(`handoff_ledger.py resume|supersede <path>`), never by hand-editing status
on a whim. Everything else — section names, body structure, naming pattern,
post-resume actions — is yours to change.

## §1 Wind-down protocol (when the trigger fires mid-task)

1. Do not start new work. Complete only the single atomic action already in
   flight (finish the current file edit or the command that is running).
2. Write the handoff document per §2 and §3.
3. Tell the user the handoff is complete and give the exact resume path. If
   the trigger notice said autoresume is active, the resume path is one
   keystroke: tell the user to type `/clear` — the cleared session will
   announce this handoff and resume it automatically. Otherwise: start a new
   session in this directory and the open handoff will be announced
   automatically (or `claude "resume"` / `codex "resume"`).
4. Stop. Do not begin any of the "Next steps" in this session.

## §2 Naming and location

Write to `./.handoffs/<YYYYMMDD-HHMM>-<topic-slug>.md`, where the timestamp
is the handoff's ending date/time (now) and the slug is a short kebab-case
name for the thread of work (`20260811-1430-auth-refactor`). One dated file
per handoff, so the directory reads as a chronology. When handing off the
same thread again, write a new dated file and mark the previous one
replaced: `python3 <hooks-dir>/handoff_ledger.py supersede <old-path>`.
Legacy undated files and the single-file `./HANDOFF.md` (topic "default")
remain supported.

Customizing: any `.md` filename under `./.handoffs/` is scanned, so a
project may impose its own convention (ticket ids, sprint prefixes). Keep
the ending date/time recoverable — either in the filename prefix or a
`created:` front-matter line — so announcements can order handoffs newest
first.

## §3 Document structure

Start the file with this front matter — `status: open` is what marks it
untransferred for the session-start announcer, and `description` is the
one-line summary the announcer shows so handoffs can be told apart without
opening them (make it specific: what is parked and where it stands):

```markdown
---
topic: <topic-slug>
created: <ISO date-time>
status: open
description: <one line: what is parked here and where it stands>
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

Customizing: the body outline above is a default, not a contract — projects
may substitute their own section set (e.g. add "Open questions", drop
"Decisions"). Only the front-matter block is load-bearing; everything below
the second `---` is free-form.

## §4 Resuming

At session start, a `[context-watch]` notice lists any open handoffs, each
with its description.

- **One open handoff**: read the file in full before any other action,
  restate the objective and the first next step in one or two sentences,
  confirm with the user unless configuration or the user has said to
  proceed, then continue from "Next steps".
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
announced or listed. Then perform the §5 post-resume actions.

## §5 After resuming (extension point)

Actions to run immediately after a handoff is retrieved and marked resumed,
before continuing the work. Defaults:

- Read `LESSONS.md` if the handoff references it.

Projects and users add their own always-run actions here — the pattern is
one imperative bullet each, for example:

- Load the `<skill-name>` skill before touching the code.
- Run `<status command>` and reconcile its output against "Current state".
- Re-open the tracking issue named in the handoff.

If this section lists no custom actions, continue straight into the
handoff's "Next steps".

## §6 Mechanics (reference)

- The deterministic trigger is a lifecycle hook (`hooks/context_watch.py`)
  that reads the session's own transcript token usage and fires once per
  session; a `SessionStart` hook runs the announcer. `hooks/handoff_ledger.py`
  tracks open vs resumed vs superseded and can be run directly: `list`,
  `resume <path>`, and `supersede <path>`.
- Thresholds are absolute tokens per model (quality degrades at an absolute
  occupancy, not a percentage of the window): HANDOFF_AT (per-launch, highest
  precedence), then CONTEXT_WATCH_TOKENS_MAP, then ./.context-watch.json,
  then ~/.context-watch/thresholds.json, then a global CONTEXT_WATCH_TOKENS,
  with 130,000 as the built-in default. Other knobs: _PENDING, _LOG
  (analytics; `context_watch.py stats`), _WINDOW, _SKILL, _MODE, _AGENT,
  _DISABLE, _MAX_AGE_DAYS — see the plugin README. With AUTORESUME=1 (legacy
  CONTEXT_WATCH_AUTORESUME=1) a single open handoff is resumed at session
  start without asking; `HANDOFF_AT=<n> AUTORESUME=1 claude` makes the whole
  hand-off-and-continue cycle cost the user one `/clear`.
- In environments without hooks, this skill still works: check `./.handoffs/`
  and `./HANDOFF.md` for `status: open` entries whenever beginning work in a
  folder, announce them in one sentence, and follow the same resume procedure.
