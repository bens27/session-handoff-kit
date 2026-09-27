# session-handoff (plugin)

Deterministic context-usage watcher + structured handoff skill + open/resumed
handoff ledger.

The plugin is a thin wrapper: `skills/session-handoff/` is a symlink to the
repo's skill folder (installs copy the target), and `hooks/hooks.json`
registers `skills/session-handoff/hooks/context_watch.py`. Script paths below
are relative to that skill folder. Without the plugin, the same folder plus
its `install.py` does the same job for Claude Code and Codex.

- **Watcher** (`hooks/context_watch.py` on `PostToolUse` + `UserPromptSubmit`):
  reads the session transcript's latest token usage and, once per session past
  the threshold, injects an instruction to invoke the `session-handoff` skill.
- **Announcer** (same script on `SessionStart`): scans `./.handoffs/*.md`, the
  per-project fallback `~/.claude/handoffs/<project-basename>/*.md`, and
  legacy `./HANDOFF.md` for `status: open` entries. One open handoff is
  announced with its description for immediate resume (or resumed without
  asking when `AUTORESUME=1`); several are enumerated newest-first with an
  instruction to ask the user which to resume. Sessions started with
  `source: resume`/`compact`/`fork` are skipped, and unrelated opening
  requests are deferred to, so the announcer informs without hijacking.
- **Ledger** (`hooks/handoff_ledger.py`): `list` prints open handoffs
  newest-first; `resolve <topic>` returns the full chain for a topic plus
  must-also-read references (and any that are missing or over the caps);
  `claim <path> [--owner X]` hides a handoff from parallel sessions for two
  hours while one resumes it, and `release <path>` drops the claim;
  `abandon <path>` closes it for good; `resume <path>` marks a handoff transferred;
  `supersede <path> [--by <new-path>]` marks it replaced by a newer one
  and can record the forward link, so future sessions stop announcing it;
  `save-path` prints where to write new handoffs (`./.handoffs/` when it
  exists, else the per-project fallback; reads always cover both);
  `new-path <topic> [dir]
  [--json]` uses one clock read to return the directory, filename, path, and
  `created` value, so a new handoff's timestamp is never independently
  guessed. The skill runs the state transitions after resuming /
  re-handing-off.
- **Skill** (`skills/session-handoff/SKILL.md`): writes
  `<save-path>/<YYYYMMDD-HHMM>-<topic>.md` — named by ending date/time, with
  `status: open` and a one-line `description:` in front matter (objective,
  state, decisions, files touched, in-flight work, next steps, gotchas),
  then stops. Handles both the single- and multiple-handoff resume flows.

What it touches (review before installing, as with any hook that runs code):
stdlib-only Python, no network, no subprocesses except in fully automatic
mode (`HANDOFF_AUTO=1`), where the `Stop` hook runs `tmux send-keys` to type
`/clear` and `resume` into your own pane, and the `auto` runner launches the
agent command you give it. The watcher reads the session
transcript path it is handed on stdin and keeps a once-per-session latch (and
optional analytics log) under the system temp dir; the announcer and ledger
read the handoff locations above, and the only files the ledger writes are
the status front-matter lines (`status:`, `resumed:`/`superseded:` stamps,
`superseded_by:`) of handoff files you point it at.

Configuration via environment variables: `HANDOFF_AT` (per-launch threshold,
highest precedence) and `AUTORESUME` (with `HANDOFF_AT`, makes the whole
hand-off/`/clear`/resume cycle one keystroke), plus `CONTEXT_WATCH_TOKENS`,
`_PERCENT`, `_WINDOW`, `_SKILL`, `_MODE`, `_AGENT`, `_DISABLE`,
`_MAX_AGE_DAYS`, and `HANDOFF_AUTO` (fully automatic: resume the newest
handoff and clear the session for you, capped at `HANDOFF_AUTO_MAX` clears in
a row, default 10) — see the kit README. Fail-open by design: the watcher and
announcer can never block a session.
