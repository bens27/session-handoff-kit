# Session Handoff — reference

Loaded on demand by `SKILL.md`: read this when installing the hooks,
customizing the skill, or running it without the hooks. Not needed for an
ordinary handoff or resume.

## Installing

This folder is the whole product: put it wherever your agent loads skills
(`~/.claude/skills/`, `~/.agents/skills/`, a project's skills folder; copy or
symlink), then register the always-on hooks from that location:

    python3 install.py              # Claude Code and Codex
    python3 install.py claude       # or: codex
    python3 install.py --uninstall  # remove them again

It writes absolute paths to `hooks/context_watch.py` into
`~/.claude/settings.json` and `~/.codex/hooks.json` (`$CLAUDE_CONFIG_DIR` /
`$CODEX_HOME` respected), backs each file up first, leaves every other hook
alone, and is safe to re-run; re-run it after moving this folder. Codex also
needs hooks enabled and the changed hook re-approved: the installer prints
those steps. Using the Claude Code plugin instead? It already registers the
hooks; do not also run the installer, or the watcher runs twice.

## How SKILL.md is organized

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
- **Mechanics** (below, in this file) — how the hooks and ledger work.

**Invariants the hooks depend on — keep these through any customization:**
handoff files end in `.md` and live in the directory
`handoff_ledger.py save-path` prints (`./.handoffs/` when it exists, else the
per-project fallback `~/.claude/handoffs/<project>/`) — the ledger reads both
locations plus legacy `./HANDOFF.md`; front matter is fenced by `---` lines; new handoffs carry `status: open` plus a one-line
`description:`; state changes go through the ledger commands
(`handoff_ledger.py claim|resume|supersede|abandon <path>`), never by hand-editing status
on a whim. Everything else — section names, body structure, naming pattern,
post-resume actions — is yours to change.

## Why the template is a separate file

The structure of a handoff can be experimented with on its own: rewrite
`handoff-template.md` and nothing in `SKILL.md` changes. The front matter is
the load-bearing part (`status: open` plus a one-line `description:` are what
the session-start announcer reads); the body outline under it is yours to
replace.

## Customizing the template

The body outline above is a default, not a contract — projects may substitute
their own section set (e.g. add "Open questions", drop "Decisions"). Only the
front-matter block is load-bearing; everything below the second `---` is
free-form.

Two things to know before you rewrite it:

- `tests/verify-skills.py` asserts that the shared-contract literals listed in
  SPEC §7.4 appear somewhere in this skill directory. Dropping a section that
  SPEC names as shared will fail that gate — change SPEC §7.4 in the same
  commit if the change is deliberate.

## Mechanics

- The deterministic trigger is a lifecycle hook (`hooks/context_watch.py`)
  that reads the session's own transcript token usage and fires once per
  session, with one SECOND NOTICE if the first is not acted on: only after
  the model has taken a turn since the first, and once occupancy is a further
  quarter of the threshold past it and at least 1.25x the threshold (both re-arm once occupancy falls below half the threshold); a
  `SessionStart` hook runs the announcer. `hooks/handoff_ledger.py`
  tracks open vs resumed vs superseded vs abandoned and can be run directly:
  `list`, `resolve <topic-or-path>`, `claim <path> [--owner <id>]`,
  `release <path> [--owner <id>]`, `resume <path> [--owner <id>]`,
  `supersede <path> [--by <new-path>]`, `abandon <path>`, `save-path [dir]`, and
  `new-path <topic> [dir] [--json]`.
- Thresholds are absolute tokens per model, set by the operator via `HANDOFF_AT`
  (per-launch) or the `CONTEXT_WATCH_*` knobs; `AUTORESUME=1` resumes a single
  open handoff at session start without asking. `HANDOFF_AUTO=1` is fully
  automatic: inside tmux a Stop hook types `/clear` then `resume` once a
  fresh handoff exists; headless, `context_watch.py auto [--max N]
  [--prompt TEXT] -- <agent command>` re-runs the agent until no new handoff
  is left. Precedence and the full knob
  list are owner configuration, documented in the repository README — the trigger
  notice already tells this skill whether autoresume is active (§1).
- In environments without hooks, this skill still works: run
  `python3 <hooks-dir>/handoff_ledger.py list [dir] --json --max-age-days <N>`
  whenever beginning work in a folder, using the same 14-day default as
  `CONTEXT_WATCH_MAX_AGE_DAYS`; announce the JSON entries it returns exactly
  like the hooked announcer does, and follow the same resume procedure.
