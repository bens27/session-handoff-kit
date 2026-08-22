---
name: session-handoff
description: >
  Use immediately whenever a "[context-watch]" message appears in conversation.
  Also run whenever the user says a variation of "hand off", "wrap up the
  session", "park this work", or "save this for later", or when context is
  nearly exhausted or auto-compaction is imminent. Also use it to resume: at
  session start when open handoffs are announced, or whenever the user asks to
  resume or pick up parked work. Do not use for ordinary progress summaries,
  commit messages, or status updates while the session is continuing, and do
  not use it as a general note-taking or memory tool.
metadata:
  version: "0.7.0"
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
- **§3 Document structure** — delegates to `handoff-template.md`. Replace the
  body outline with your own preferred structure there, not here.
- **§4 Resuming** — how announced handoffs are retrieved and adopted.
- **§5 After resuming** — the extension point for actions that should always
  run right after retrieval (loading another skill, running a status
  command, opening a tracker). Empty by default; add yours here.
- **§6 Mechanics** — how the hooks and ledger work. Reference, not policy.

**Invariants the hooks depend on — keep these through any customization:**
handoff files end in `.md` and live in the directory
`handoff_ledger.py save-path` prints (`./.handoffs/` when it exists, else the
per-project fallback `~/.claude/handoffs/<project>/`) — the ledger reads both
locations plus legacy `./HANDOFF.md`; front matter is fenced by `---` lines; new handoffs carry `status: open` plus a one-line
`description:`; state changes go through the ledger commands
(`handoff_ledger.py resume|supersede <path>`), never by hand-editing status
on a whim. Everything else — section names, body structure, naming pattern,
post-resume actions — is yours to change.

## §1 Wind-down protocol (when the trigger fires mid-task)

1. Do not start new work. Complete only the single atomic action already in
   flight (finish the current file edit or the command that is running).
2. Write the handoff document per §2 and §3.
3. Verify before reporting: run
   `python3 <hooks-dir>/handoff_ledger.py resolve <topic-slug> [dir]` and
   confirm its `authoritative:` line is the path you just wrote. If it is
   not (front matter malformed, wrong directory, older file still winning),
   fix the file or supersede the older one and re-run — a handoff the
   announcer cannot find is not complete.
4. Tell the user the handoff is complete and give the exact resume path. If
   the trigger notice said autoresume is active, the resume path is one
   keystroke: tell the user to type `/clear` — the cleared session will
   announce this handoff and resume it automatically. Otherwise: start a new
   session in this directory and the open handoff will be announced
   automatically (or `claude "resume"` / `codex "resume"`).
5. Stop. Do not begin any of the "Next steps" in this session.

## §2 Naming and location

Choose a short kebab-case `<topic-slug>` for the thread of work, then run
`python3 <hooks-dir>/handoff_ledger.py new-path <topic-slug> [dir] --json`.
Write to the `path` field verbatim, and use the `created` field verbatim for
the front-matter `created:` value (§3) and the title date (§3); never compute
or guess either value independently. One dated file per handoff, so the
directory reads as a chronology. When handing off the same thread again,
write a new dated file and mark the previous one replaced:
`python3 <hooks-dir>/handoff_ledger.py supersede <old-path> --by <new-path>`.
Legacy undated files and the single-file `./HANDOFF.md` (topic "default")
remain supported.

Customizing: every `.md` file in either scanned location (`./.handoffs/` and
the per-project fallback `~/.claude/handoffs/<project>/`) is read, so a
project may impose its own filename convention (ticket ids, sprint prefixes). Keep the ending date/time
recoverable — either in the filename prefix or a `created:` front-matter
line — so announcements can order handoffs newest first.

## §3 Document structure

The shape of the handoff document — front matter, body outline, length rules —
lives in `handoff-template.md`, beside this file. Read it and write the handoff
to that template.

It is a separate file so the structure of a handoff can be experimented with on
its own: rewrite `handoff-template.md` and nothing in this file changes. The
front matter is the load-bearing part (`status: open` plus a one-line
`description:` are what the session-start announcer reads); the body outline
under it is yours to replace.

## §4 Resuming

At session start, a `[context-watch]` notice lists any open handoffs, each
with its description.

- **One open handoff**: run
  `python3 <hooks-dir>/handoff_ledger.py resolve <topic> [dir]` using the
  topic from the announcement, then read the `authoritative` file and every
  path listed in `must_also_read` before any other action, restate the
  objective and the first next step in one or two sentences, confirm with the
  user unless configuration or the user has said to proceed, then continue
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
announced or listed. Then perform the §5 post-resume actions.

## §5 After resuming (extension point)

Actions to run immediately after a handoff is retrieved and marked resumed,
before continuing the work. Defaults:

- Load every skill named in the handoff's `skills:` front-matter line (via
  the Skill tool), in order, before touching the work. When writing a
  handoff, populate `skills:` with the skills this session had loaded that
  the work depends on — that is what makes a `/clear` cycle come back with
  the right skills and only those.
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
  `resolve <topic-or-path>`, `resume <path>`,
  `supersede <path> [--by <new-path>]`, `save-path [dir]`, and
  `new-path <topic> [dir] [--json]`.
- Thresholds are absolute tokens per model, set by the operator via `HANDOFF_AT`
  (per-launch) or the `CONTEXT_WATCH_*` knobs; `AUTORESUME=1` resumes a single
  open handoff at session start without asking. Precedence and the full knob
  list are owner configuration, documented in the plugin README — the trigger
  notice already tells this skill whether autoresume is active (§1).
- In environments without hooks, this skill still works: run
  `python3 <hooks-dir>/handoff_ledger.py list [dir] --json --max-age-days <N>`
  whenever beginning work in a folder, using the same 14-day default as
  `CONTEXT_WATCH_MAX_AGE_DAYS`; announce the JSON entries it returns exactly
  like the hooked announcer does, and follow the same resume procedure.
