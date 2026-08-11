# session-handoff (plugin)

Deterministic context-usage watcher + structured handoff skill + open/resumed
handoff ledger.

- **Watcher** (`hooks/context_watch.py` on `PostToolUse` + `UserPromptSubmit`):
  reads the session transcript's latest token usage and, once per session past
  the threshold, injects an instruction to invoke the `session-handoff` skill.
- **Announcer** (same script on `SessionStart`): scans `./.handoffs/*.md` and
  legacy `./HANDOFF.md` for `status: open` entries. One open handoff is
  announced for immediate resume (or resumed without asking when
  `CONTEXT_WATCH_AUTORESUME=1`); several are enumerated with an instruction to
  ask the user which to resume. Sessions started with `source: resume` are
  skipped, and unrelated opening requests are deferred to, so the announcer
  informs without hijacking.
- **Ledger** (`hooks/handoff_ledger.py`): `list` prints open handoffs; `resume
  <path>` flips `status: open` to `status: resumed` so future sessions stop
  announcing transferred work. The skill runs this after actually resuming.
- **Skill** (`skills/session-handoff/SKILL.md`): writes
  `./.handoffs/<topic>.md` with `status: open` front matter (objective, state,
  decisions, files touched, in-flight work, next steps, gotchas), then stops.
  Handles both the single- and multiple-handoff resume flows.

Configuration via environment variables (`CONTEXT_WATCH_TOKENS`, `_PERCENT`,
`_WINDOW`, `_SKILL`, `_MODE`, `_AGENT`, `_DISABLE`, `_MAX_AGE_DAYS`,
`_AUTORESUME`) — see the kit README. Fail-open by design: the watcher and
announcer can never block a session.
