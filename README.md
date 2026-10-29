# Session Handoff Kit

When an AI Agent reaches a certain amount of cumulative context usage in a session, you'll notice the quality of their reasoning degrades, and you may even notice a cost increase compared to the same questions with less context usage. This is lovingly referred to as 'the dumb zone'. 

This kit is my attempt to simplify this. Here are its components:
1. A context monitor - watches your session's context usage, and allows you to set a limit
2. Context-limit hooks - when you get to your limit, automatically create a handoff document for a new session to retrieve. With `AUTORESUME=1`, also nudge you to clear your current session context usage (Anthropic - if you're reading this - please allow for programmatic context clearing). With `HANDOFF_AUTO=1`, clear and resume for you (inside tmux, or through a headless runner)
3. Resume actions - startup stays quiet; an explicit “continue the handoff” prompt gets a direct command for one trusted checkpoint owned by this terminal, or a selection pointer when needed
4. Handoff customizations - specify skills to be loaded when a handoff is retrieved 
5. Basic state management on handoff documents - open/resumed/superseded/abandoned, plus a short claim so two sessions don't resume the same handoff

One handoff system, packaged for every surface it can run on. A deterministic
hook watches the session's own token usage and — at a configurable threshold —
injects a one-time instruction to invoke the `session-handoff` skill, which
writes a structured checkpoint and stops. In the next session, “continue the
handoff” gets a lean resume command; manual retrieval handles portable handoffs.

Where hooks don't exist (chat), a behavioral variant of the skill does the same
job on request; file-ledger fallback conventions use
`handoff_ledger.py list --json` rather than manual file inspection.

## Install matrix

| Environment | Install | Trigger |
| --- | --- | --- |
| **Claude Code** (CLI, VS Code, JetBrains) | Copy `skills/session-handoff/` into your skills folder and run its `install.py`, or install the `session-handoff` plugin from this repo's marketplace | Deterministic — hook fires at the token threshold |
| **Claude Cowork** (desktop) | Open `session-handoff.plugin` and click install | Skill on request; hooks are in-schema but rarely exercised in Cowork — treat the threshold trigger as best-effort there |
| **Codex CLI** (terminal, IDE extension) | Copy `skills/session-handoff/` into your skills folder and run `install.py codex` | Deterministic — same hook, behind Codex's experimental hooks feature flag |
| **Claude chat** (claude.ai web, **Claude Desktop**, mobile apps) | Open `session-handoff-chat.skill` and click **Save skill** (or upload in Settings → Capabilities) | Conversational — no token feed exists in chat |
| **Claude chat, before your first message** | Load `chrome-extension/` unpacked in Chrome/Edge (see its README) | Pre-fills — optionally auto-sends — the handoff-check prompt into every new chat |

### Complete standalone skill package

The primary standalone artifact is `dist/session-handoff.skill`, a ZIP archive
containing the complete `session-handoff/` folder: skill instructions,
continuation protocol, templates, references, Codex metadata, installer, ledger,
and context hooks. It requires Python 3 and a host with local file/tool access;
no plugin or repository checkout is needed to use the extracted package.

Build the distributable with `bash scripts/package.sh`. To install that artifact:

```sh
mkdir -p ~/.agents/skills
unzip dist/session-handoff.skill -d ~/.agents/skills
python3 ~/.agents/skills/session-handoff/install.py codex   # or: claude
```

Extract into the skill directory your host discovers (for example,
`~/.claude/skills` for Claude Code), then run the installer from that permanent
location. The installer registers hooks; host feature flags and hook trust still
apply as described below. Extraction alone provides on-request handoffs but does
not activate automatic context monitoring. For upgrades, replace the existing
skill folder and rerun the installer.

The `.skill` suffix identifies the ZIP package; it does not imply that every
host supports one-click importing or running its scripts. The separate
`session-handoff-chat.skill` is the conversational variant for chat-only hosts.
The `.plugin` artifact remains an optional Claude plugin distribution.

### Skill folder + installer (Claude Code and Codex)

`skills/session-handoff/` is the whole product: skill, template, and hook
scripts. Put it where your agent loads skills, then register the always-on
hooks from there:

```
cp -R skills/session-handoff ~/.agents/skills/     # or ~/.claude/skills/, or symlink
python3 ~/.agents/skills/session-handoff/install.py            # Claude Code + Codex
python3 ~/.agents/skills/session-handoff/install.py claude     # or: codex
python3 ~/.agents/skills/session-handoff/install.py --uninstall
```

The installer writes absolute paths into `~/.claude/settings.json` and
`~/.codex/hooks.json`, backs each up once, leaves other hooks alone, and is
safe to re-run (re-run it after moving the folder). Use either this or the
plugin below, not both: both would run the watcher twice.

Hook events, timeouts and matchers are defined once per host in `install.py`.
Standalone settings, runtime activation and the Claude plugin use those tables.
After changing the registrations, regenerate the plugin file from the repo root:

```sh
python3 skills/session-handoff/install.py plugin-hooks > plugins/session-handoff/hooks/hooks.json
```

`python3 tests/verify-packaging.py` rejects any difference between the generated
and committed plugin JSON, including timeout drift.

### Opt-in Ticket commit trailer

Git attribution is separate from the Claude/Codex watcher registration. Install
it explicitly for each repository, from the kit's permanent location:

```sh
python3 ~/.agents/skills/session-handoff/install.py git-hook --project /path/to/project
HANDOFF_TICKET_ID=222a49df-730b-437d-99e3-05c3acda0a29 git commit -m "Fix resume"
python3 ~/.agents/skills/session-handoff/install.py git-hook --project /path/to/project --uninstall
```

The stdlib Python `hooks/commit_ticket.py MESSAGE_FILE` adds `Ticket: <id>`.
Precedence is an existing valid trailer, then `HANDOFF_TICKET_ID`, then no
attribution. Set the variable for the current commit or ticket-specific session;
clear it when switching tasks. Checkpoint `ticket` metadata is **not**
automatically read, and the hook never guesses from the newest handoff or branch.
The environment ID uses `handoff_contract.METADATA_FORMATS['ticket']`: a full
lowercase ticket UUID or its 8-hex short ID. Existing trailer IDs retain the
broader 1–128 ASCII format: an alphanumeric first character followed by
alphanumerics, `.`, `_`, `/` or `-` (for example `PROJ-123`).

The hook checks the final trailer paragraph, separated from the subject/body
by a blank line, with case-insensitive `Ticket:` keys. Exactly one single-line
ID is allowed when present. Empty, malformed, multiline or duplicate Ticket
trailers reject the commit without editing its message. Missing or invalid
environment IDs leave the message unchanged and never block a commit; invalid
nonempty IDs emit one diagnostic with the expected format. Body
mentions are not trailers. Other trailers, Git comments and scissors/diff content
are preserved; `core.commentChar` is supported for a single configured character,
with `#` as the default. With `auto`, a line starting with any Git auto candidate
(`#;@!$%^&|:`) makes the comment character ambiguous, so the hook skips insertion and
validation without changing the message. Normal editor commits include Git's
comment template and therefore skip automatic attribution under `auto`.
Messages supplied with `-m` or `-F` can receive attribution when they have no
candidate-prefixed lines; Markdown `-` and `*` bullets are supported. Configure
an explicit comment character or add a trailer manually for editor commits.
This preserves real `#` body lines when Git selected another comment character.
Empty messages remain empty.

Installation writes only the selected repository's default `commit-msg` hook;
linked worktrees share this repository hook. No Git configuration is changed.
Reinstall/uninstall is idempotent. Kit ownership requires the exact generated
wrapper: `#!/bin/sh`, then `# session-handoff Ticket hook`, and the unchanged
body with the same safely quoted absolute script path in both places. Such a
wrapper may be replaced or removed from a new install path. Foreign or edited
hooks (even with the marker), symlinked hooks or parent `.git` directories, and
any configured `core.hooksPath` are refused without modification. Regular linked
worktree `.git` indirection files remain supported. To compose with an
existing hook, call `python3 /permanent/kit/hooks/commit_ticket.py "$1"` from that
hook and propagate its exit status, preserving the other hook's failure behavior.
After moving the kit, rerun `git-hook` from its new location to update the
wrapper; do the same before retiring a runtime release it points at. Uninstall
from a different install path also works. Missing Python or a removed kit script
is a quiet no-op; message I/O, encoding or missing contract-module errors warn
and allow the commit. Message updates use atomic replacement
so a failed write preserves the original. With no source or Ticket candidate,
the hook reads only the message and does not probe Git or handoff state.

### Validated runtime snapshots

For development checkouts, create a separate runtime instead of registering
hooks against the working tree. This is opt-in; none of these operations run
automatically during packaging or a legacy install.

```sh
python3 skills/session-handoff/install.py runtime --root ~/.local/share/session-handoff/runtime create v1
# Copy the complete "release" ID from the JSON result, including its digest:
python3 skills/session-handoff/install.py runtime --root ~/.local/share/session-handoff/runtime validate RELEASE
python3 skills/session-handoff/install.py runtime --root ~/.local/share/session-handoff/runtime activate RELEASE codex
# Or activate the same release for Claude Code, separately:
python3 skills/session-handoff/install.py runtime --root ~/.local/share/session-handoff/runtime activate RELEASE claude
python3 skills/session-handoff/install.py runtime --root ~/.local/share/session-handoff/runtime rollback codex
```

Creation copies the declared product files into a private, read-only, content-addressed release,
checks required files, Python syntax and the packaged ledger/preflight CLIs, and writes a
versioned integrity manifest. The payload includes the restart preflight module, lane helper and threshold
example; undeclared files such as `.env`, local threshold copies and editor
files cause creation to fail before publication. Keep those outside the product
folder. Runtime root and release storage use mode `0700`; payload files use
`0400` or `0500`. It leaves host configuration unchanged. Run the
repository's regression checks before creating a release: snapshot validation
checks payload integrity and CLI loading, not every behavioral regression.
Activation revalidates the release and atomically replaces one host's hook
configuration, preserving foreign hooks. Commands pin the exact release path;
there is no changing runtime symlink. Codex feature flags and native hook trust
still apply; approve the new command through Codex's hook review.

Claude runtime activation also registers `mandatory_gate.py` for `PreToolUse`
and `Stop` in user settings, with isolated Python (`-I -B`). These gates are
excluded from checkout/plugin registration, whose code may live in a project.
The gate checks the sealed payload, ownership, write permissions and location
before importing any kit module. A project without `"handoff_v3": true` returns
before importing kit code or reading its transcript.

Also point your host's discovered `session-handoff` skill folder at the returned
`snapshot` path (a symlink to that immutable directory is suitable), preserving
the old skill folder first. Activation only registers hooks: it does not move
skill folders, change plugin marketplace sources, or rewrite existing sessions.
For Claude directory plugins, explicitly migrate to this standalone installation
and remove the duplicate plugin registration before using snapshot hooks.

Rollback restores the exact prior hook configuration from a private durable
receipt. It refuses if configuration has changed since activation or if the
previous snapshot fails validation. An interrupted activation leaves a complete
old or new config; retry activation or roll back the matching completed switch.
The first rollback may restore a legacy working-tree hook path; inspect the
returned release (`null` for legacy) and restore skill discovery separately.
If creation is interrupted after publication but before its final directory
seal, repeat the same `create VERSION`: a fully verified payload is re-sealed.
A corrupt existing release is left intact with an explicit removal instruction.
Rollback validates receipt format and skips malformed entries, reporting their
count in `skipped_receipts`.
Retain releases and receipts needed by running sessions or rollback. They use
local disk per release; there is no automatic garbage collection. Read-only
modes prevent accidental edits, not deliberate modification by the file owner.

### Claude Code plugin

The plugin is a thin wrapper around the same folder (it symlinks
`skills/session-handoff/`; plugin installs copy the target). From a hosted copy (push this folder to GitHub first):

```
/plugin marketplace add <your-github-user>/<repo>
/plugin install session-handoff@session-handoff-kit
```

Or from a local checkout:

```
/plugin marketplace add /path/to/session-handoff-kit
/plugin install session-handoff@session-handoff-kit
```

No further setup. The hook fires on every tool call and user prompt, reads the
transcript's latest usage entry (`input_tokens + cache_creation_input_tokens +
cache_read_input_tokens`), and injects the handoff instruction once per session
when the threshold is crossed. Consider turning auto-compact off in `/config`
so compaction never races the handoff, or leave it on as a fallback.

The one-keystroke cycle:

```
HANDOFF_AT=120000 AUTORESUME=1 claude
```

`HANDOFF_AT` sets the trigger threshold for this launch (highest precedence).
With `AUTORESUME=1`, crossing it writes the handoff and tells you to type
`/clear`; the cleared session announces the open handoff and resumes it
immediately without asking — the full wind-down/pick-up cycle costs one
keystroke. Handoff files are named by ending date/time
(`.handoffs/20260811-1430-auth-refactor.md`) and carry a one-line
`description:` in front matter, so announcements and directory listings stay
tellable-apart as they accumulate. The filename timestamp and `created:`
front matter are produced together by `handoff_ledger.py save`, so the
agent never guesses them independently.

**Easy Claude Code launcher.** From your project directory, run:

```sh
~/.agents/skills/session-handoff/claude-auto
# Optional threshold and Claude prompt:
~/.agents/skills/session-handoff/claude-auto --at 120000 -- "Continue this project"
```

The launcher requires `claude` on PATH (and `tmux` outside AgentsRoom), makes the
skill discoverable in Claude's configuration directory, registers its hooks, and
opens tmux when you are neither already in a pane nor in AgentsRoom (where it
runs `claude` directly). It enables the save → clear → retrieve → resume
cycle for this launch. It preserves normal Claude permissions and uses three
consecutive automatic clears by default (`--max-clears N`); this is not a total
session or spending limit. Existing project threshold configuration still applies
unless `--at` overrides it. A configured session-handoff plugin supplies its own
skill/hooks instead of registering a duplicate standalone installation.

For a short command, link the launcher into a directory on your PATH:

```sh
mkdir -p ~/.local/bin
ln -s ~/.agents/skills/session-handoff/claude-auto ~/.local/bin/claude-auto
claude-auto
```

**Easy Codex launcher.** Install the short command once:

```sh
mkdir -p ~/.local/bin
ln -s ~/.agents/skills/session-handoff/codex-auto ~/.local/bin/codex-auto
```

With `~/.local/bin` on PATH, prepare the active Codex profile and complete its
native hook review once:

```sh
codex-auto --setup
codex --enable hooks
```

Then, from your project directory:

```sh
codex-auto -- "Continue this project"
codex-auto                         # resume an existing handoff
codex-auto --at 120000 --max-runs 3 -- "Implement the next task"
```

This registers the skill and hooks in `CODEX_HOME` (default `~/.codex`), enables
hooks for the launch, and runs Codex with the workspace-write sandbox. It uses
fresh headless sessions rather than clearing an interactive UI; tmux is not
required. Three sessions is the default run limit, not a token or spending cap.
Child failures stop immediately; reaching the limit with work outstanding
returns exit 75. Use the same `CODEX_HOME` for setup, hook approval, and launch.
Hook trust is not bypassed or verified by this wrapper: an untrusted hook can be
skipped by Codex, so complete native review before unattended use. Model and
other settings come from that profile. No global auto-mode setting is changed.

**Automatic transition settings.** `HANDOFF_AUTO=1` requests an automatic
transition after saving. Each save records its terminal (`HANDOFF_TERMINAL_ID`,
else `AGENTSROOM_AGENT_ID`), so continuation stays scoped to that terminal.
The kit trusts that variable as given: separate AgentsRoom tabs, even of the
same role, carry distinct agent IDs and so separate terminals, but whether an
ID survives a host relaunch is the host's promise, not something the kit can
check.
AgentsRoom defaults this setting to on (`HANDOFF_AUTO=0` or `"auto": false`
opts out); elsewhere it is opt-in. The default requests a gated transition,
not permission or proof that a host restart can occur.

In AgentsRoom, save returns an `agents_restart` candidate with
`requires_preflight: true`. Follow the bundled
[restart procedure](skills/session-handoff/reference.md#agentsroom-restart) to observe
current capability, identity, complete worker inventory and hold state with
supported host tools. Unknown observations preserve the checkpoint for manual
fresh-session continuation; inventing observations cannot satisfy preflight.
Only `outcome: ready` and `dispatch: true` permit a final `agents_restart` call
using the exact returned `restart_prompt`, then immediate turn completion.
That prompt passes checkpoint/request identity to successor preparation and
receipt recording before resume. Host acceptance, native session replacement
and successor delivery remain separate evidence and acceptance checks.
AgentsRoom takes precedence over inherited `TMUX_PANE`, so its fallback never
types into another terminal.

As an optional fallback inside tmux outside AgentsRoom, a `Stop` hook types `/clear` and then `resume` into the pane once the handoff
is written (Claude Code hooks cannot clear a session themselves). Outside
tmux, or for Codex, use the headless runner, which re-launches the agent with
`resume` while successful runs leave a new open handoff. A nonzero child exit
stops immediately and preserves the checkpoint. `--max` must be positive;
exhaustion returns exit 75 (unfinished work), not success. The run count does
not impose a token, dollar or wall-time budget; use host limits for those.

```
python3 skills/session-handoff/hooks/context_watch.py auto --max 10 --prompt "build X" -- claude -p
```

For Codex, keep the final `--` so the runner's next `resume` prompt is text
rather than Codex's `exec resume` subcommand:

```sh
python3 skills/session-handoff/hooks/context_watch.py auto --max 2 --prompt "build X" -- codex exec --sandbox workspace-write --
```

If the first notice is ignored, a SECOND NOTICE fires. It waits until the
model has taken a turn since the first notice, and until occupancy is a
further quarter of the threshold past where the first fired (and at least
1.25x the threshold). The Stop hook stops after `HANDOFF_AUTO_MAX` (default
10) automatic clears in a row, so a session that starts near the threshold
cannot loop forever; set `HANDOFF_AT` well above a fresh session's starting
context (about 50k in an interactive Claude Code session with many tools).
If the threshold is not at least 10k (or 10%) above the session's first
measured occupancy, the watcher sends one note suggesting a minimum
threshold instead of a handoff notice, since a handoff would free nothing.
Both notices re-arm once occupancy drops below half the threshold, for
example after a compaction.
An unattended run still stops at Claude Code's own first-launch dialogs (folder
trust, project MCP servers): answer them once before leaving it running.

### Claude Cowork

Build the one-click artifact from a checkout:

```
bash scripts/package.sh
```

Open `session-handoff.plugin` in a Cowork conversation (or share it into one) —
the file card offers a one-click install. The skill triggers on request
("hand off", "wrap up the session") and on any `[context-watch]` notice. The
plugin schema is shared with Claude Code, so the hooks ship too; Cowork's hook
behavior is not guaranteed, so rely on the skill there and treat the automatic
threshold as a bonus if it fires.

### Codex CLI

```
python3 ~/.agents/skills/session-handoff/install.py codex
```

Then enable hooks in `~/.codex/config.toml` (`[features]` → `hooks = true`;
older builds used `codex_hooks = true`). The hook reads the session rollout's
`token_count` events (`last_token_usage`, with the window taken from
`model_context_window` when reported). Codex-specific notes (verified against
0.157.1; upgrade older builds before relying on multiple hook groups):

- **Hook commands stay independent.** The installer adds a separate group,
  preserving other hooks' matchers, outputs and exit statuses. It unwraps
  fan-outs made by older kit installers. Current Codex executes matching groups;
  the old 0.145.0 compatibility workaround is no longer used.
- **Installed hooks still require trust.** Review new or changed definitions
  through Codex's native `/hooks` UI. Reinstalling a legacy fan-out changes
  definitions and may require review again. See the
  [official hook documentation](https://developers.openai.com/codex/hooks).
- On `PostToolUse`, the notice goes out as JSON
  `hookSpecificOutput.additionalContext` with exit 0, so the tool result is
  kept (`CONTEXT_WATCH_MODE=block` sends `decision: block`, which replaces
  it). `UserPromptSubmit` uses plain stdout. Both are non-destructive.
- Threshold checks run on `PostToolUse` and `UserPromptSubmit`. A tool-triggered
  notice can only act after that tool call completes. A model that does a large chunk of work in one big tool
  call (e.g. a single `apply_patch` touching many files) can cross the
  threshold and finish the work in the same step — the hook still fires and
  a handoff still gets written, just after the work is already done rather
  than interrupting mid-task.
- Keep `model_auto_compact_token_limit` above the watcher threshold so the
  handoff always wins the race against auto-compaction.

### Claude chat (including Claude Desktop)

Build the chat skill artifact from a checkout:

```
bash scripts/package.sh
```

Save `session-handoff-chat.skill` to your profile. This covers **Claude
Desktop** too: the desktop app is a chat surface, so the same `.skill` applies
(skills sync with your account across web, desktop, and mobile). The one
Desktop-specific fork: if you're in a **Cowork** session inside the desktop
app, use the `session-handoff.plugin` route from the Cowork section instead —
Cowork accepts the full plugin, chat mode takes the skill. Chat has no hooks and no
token counter, so the trigger is conversational: "hand off", "wrap up this
chat", "continue this in a new chat". Chat → chat needs no file shuttle: the
skill saves the full handoff to persistent memory when available and always
prints a `SESSION HANDOFF — <project>` block into the conversation so
past-chat search can find it. In the next chat, "resume the <project> handoff"
resolves it from attachment → memory → past-chat search, in that order. The
downloadable `HANDOFF.md` remains the portable copy for crossing surfaces:
drop it in a project folder and ask to retrieve the handoff in Claude Code or
Cowork. (Past-chat retrieval requires the "Search and
reference past chats" setting, and Project chats only search within the same
Project.) When several chat handoffs are open, the skill lists topic, stored
date, and description rather than asking the model to invent an age. The
Chrome/Edge extension also injects a real computed timestamp into its default
new-chat prompt before insertion.

## Configuration (hook environments)

The watcher measures **window occupancy** — everything the next call will
carry: fresh input, cache writes, **cache reads at full size** (discounted on
the bill, full-weight in the window), and the last call's output. This is a
quality-preservation gauge, not a cost gauge: thresholds should sit where
reasoning quality drops for a given model, which is an absolute-token
property, not a percentage of the window.

Threshold resolution — first match wins; model ids matched by longest
case-insensitive substring:

1. `HANDOFF_AT` — per-launch global absolute, highest precedence (`HANDOFF_AT=20000 claude`)
2. `CONTEXT_WATCH_TOKENS_MAP` — e.g. `opus=120000,sonnet=140000,gpt-5.5=160000`
3. `./.context-watch.json` — project-local per-model config
4. `~/.context-watch/thresholds.json` — user-global per-model config
5. `CONTEXT_WATCH_TOKENS` — global absolute
6. `"default"` key in the config files
7. `CONTEXT_WATCH_PERCENT` x window — only if PERCENT is explicitly set
8. Built-in default: 130,000 tokens

The same config files also accept `"auto"` (bool, like `HANDOFF_AUTO`), `"at"`
(int tokens, like `HANDOFF_AT`) and `"auto_max"` (int, like `HANDOFF_AUTO_MAX`).
A non-empty environment variable wins, then the project file, then the user file.
`"auto"` is disable-only: a project file may set `"auto": false` to turn
automatic continuation off, but never `true` to turn it on (enable it in
`~/.context-watch/thresholds.json` or `HANDOFF_AUTO`). A project file that sets
`"auto": true` gets a one-time `handoff-status:` note saying it is ignored. Under
`"handoff_v3": true`, an unset `"auto"` is off, even inside AgentsRoom, and an
invalid mandatory timing configuration turns automatic continuation off. Values of the wrong JSON type are ignored, and these keys
are never thresholds.

`"warning_window_tokens"` (int, default `30000`, like `CONTEXT_WATCH_WARNING_WINDOW`)
sets an early warning that lead before the resolved threshold (130k gives a
warning at 100k): finish the current slice and prepare notes, without saving or
restarting. It fires once per window, re-arms after compaction, and `0` turns it
off. A malformed value (negative, non-integer, boolean) disables it with one
diagnostic; it is never a model threshold.

`"resume_pointer_policy"` (`"ask"` default, `"latest-owned"` or `"manual"`) decides
what a fresh session does when several restart checkpoints for its terminal
are pending: `ask` and `manual` list them with their adopt commands and adopt
nothing, while `latest-owned` adopts the newest. One pending checkpoint is
adopted at startup only when continuation is authorized in user scope
(`"autoresume": true` in `~/.context-watch/thresholds.json` or `AUTORESUME=1`).
The project file can choose the policy but never authorize continuation. A
malformed value turns automatic selection off and reports it. The body is
delivered once: a later restart prompt for the same session answers
`already-delivered`.

`"mandatory_at_tokens"` is file-only (no environment override). A positive
integer creates a save obligation for a project with `"handoff_v3": true`; `null` (default) disables it. Project values override
`~/.context-watch/thresholds.json`; a project `null` disables the gate even when
the user file sets a point. The point must be at or above the warning point and,
when the host reports its window, at or below window minus reserve. Invalid
timing disables automatic v3 transitions with a named diagnostic. Timing keys
never enter model threshold maps. No setting here grants restart permission.
For opted-in v3 projects, project `auto: true` alone cannot request automatic
continuation: use launch-level `HANDOFF_AUTO` or user-scope `auto: true`.

Only Claude's validated runtime installation enforces this obligation. At the
point it denies new work, while allowing `Read`, `Glob`, `Grep`, `TaskGet`,
`TaskList`, `AskUserQuestion`, and the exact save command printed in the denial.
Shell reads and status commands are not classified as read tools. Supply the
JSON draft through a quoted literal heredoc, without additional shell commands:

```sh
# Use the exact command printed by the hook, including its root and session:
HANDOFF_AUTO=0 python3 -B /runtime/RELEASE/hooks/handoff_ledger.py save /project --session SESSION --input - <<'HANDOFF_JSON'
{"topic":"work","description":"Continue work","body":"## Objective\nContinue.\n## Current state\nReady.\n## Next steps\nReview."}
HANDOFF_JSON
```

The printed command binds `HANDOFF_AUTO` to the launch/user authorization so a
project-local auto setting cannot grant continuation through the save result.

The obligation remains latched until a trusted publication matches its session
and trigger. A normal Stop asks for publication; recursive Stop, lack of save
progress, or two blocked Stop attempts permits reporting a human blocker and
the checkpoint text. Runtime/state failures below the point without a latched obligation report a
diagnostic and allow work. Reads and user decisions remain available on state errors;
an exact save command remains available when gate state is damaged. Publication
releases only the save gate, preserving restart holds. Missing/stale telemetry
reports enforcement unavailable without inventing occupancy; a previously
latched obligation remains active. Claude window assumptions are not reported
model specifications. Codex has no supported gate in this kit's host table and
reports `mandatory-unenforced`; checkout/plugin Claude installations do too.
To roll back, set project `handoff_v3` false, `mandatory_at_tokens` null, use
`CONTEXT_WATCH_DISABLE=1`, or restore the prior runtime configuration. Checkpoint
records remain available for explicit adoption. No live rollout is automatic.

Config file format (see `hooks/thresholds.example.json`):

```json
{ "claude-opus": 120000, "claude-sonnet": 140000, "gpt-5.5": 160000, "default": 130000 }
```

The active model is re-detected on every check (from the hook input on Codex,
from the transcript's latest entry on Claude Code), so mid-session model
switches re-resolve the threshold automatically.

Pressure notices show the context window and whether it is host-reported or
assumed, the startup input baseline and room below the threshold. A Claude
window labeled `assumed` is not a verified model specification. Keep per-model
quality thresholds deliberate; percent-of-window triggering remains opt-in.

Other variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONTEXT_WATCH_RESERVE` | `20000` | When the host reports the window (Codex), caps the threshold at window − reserve so there is room to write the handoff |
| `CONTEXT_WATCH_PENDING` | on | Adds an estimate (~3 chars/token, images/audio a flat 1,600 each) for the tool result or prompt already in the hook's stdin but not yet in any usage entry; set `0` to disable |
| `CONTEXT_WATCH_THINKING` | largest response so far, ≤25k | Tokens assumed for the current response (thinking included), which is written to the transcript only after the hook returns; Claude only |
| `CONTEXT_WATCH_WARNING_WINDOW` | `30000` | Advisory lead before the threshold (overrides `warning_window_tokens`); `0` disables |
| `CONTEXT_WATCH_JEV` | off | `1` opts into semantic routing through Jev (requires `TYPESAFE_API_KEY`), at most one attempt per project/session, including failures. Classification selects read-only context, never execution permission. Exact commands always route locally. |
| `CONTEXT_WATCH_LOG` | `~/.context-watch/events.jsonl` | Per-trigger analytics (model, occupancy, cache-read share, threshold); `0` disables. Summarize with `python3 context_watch.py stats` |
| `CONTEXT_WATCH_WINDOW` | `200000` | Used only by the PERCENT path; Codex reports its own window |
| `CONTEXT_WATCH_SKILL` | `session-handoff` | Skill named in the injected instruction |
| `CONTEXT_WATCH_MODE` | `warn` | `warn` injects context; `block` uses the blocking channel on `PostToolUse` |
| `CONTEXT_WATCH_AGENT` | auto | Force `claude` or `codex` if auto-detection guesses wrong |
| `CONTEXT_WATCH_DISABLE` | — | Set `1` to disable without uninstalling |
| `CONTEXT_WATCH_MAX_AGE_DAYS` | `14` | Open handoffs older than this are not announced |
| `CONTEXT_WATCH_AUTORESUME` | — | Set `1` to resume a single open handoff at session start without asking |
| `HANDOFF_AUTO` | on in AgentsRoom, else off | Requests automatic transition (`0` opts out). AgentsRoom requires authoritative preflight and falls back to manual continuation when unavailable; outside AgentsRoom, tmux Stop or the headless runner performs the configured transition. |
| `HANDOFF_TERMINAL_ID` | `AGENTSROOM_AGENT_ID`; `claude-auto` generates `tmux-<uuid>` (outside AgentsRoom only); the `context_watch.py auto` runner assigns each runner's children a fresh `runner-<uuid>`, replacing any inherited value | Terminal identity recorded on each save; in fully automatic mode the cleared session resumes only that terminal's newest open handoff (legacy handoffs without a terminal are not auto-picked). A blank value falls through to the next; ids over 120 characters keep 100 plus a 16-hex SHA-256 suffix |
| `HANDOFF_AUTO_MAX` | `10` | Fully automatic mode: automatic clears in a row per project before the Stop hook stops and tells you (a session that starts near the threshold would otherwise loop); a turn that ends without the trigger resets the count |
| `HANDOFF_STATE_DIR` | system temp dir (`$TMPDIR`) | Absolute path of the directory for per-session temp state (latches, session notes, receipts, pointers, auto counts, the publisher lock). A blank or relative value falls back to the system temp dir; file names are unchanged. Set it in the environment that hooks and your shell share, or publication serialisation is lost |

## How automated does it get

Automatic handoffs pause when pressure returns within ten minutes of resume,
when a topic produces a third pressure checkpoint within 30 minutes, or when
AgentsRoom restart intents approach its two-minute / six-per-hour host limits.
The kit allows five saved restart intents per hour per terminal; it cannot
observe host restarts made outside the kit. A `paused` save still publishes the
full checkpoint and keeps it open. Automatic startup and tmux/runner clearing
respect the pause. Reduce re-entry reads or wait for the budget, then start a
fresh session and explicitly ask to `continue the handoff`. If AgentsRoom refuses
a restart, report that refusal and use the same manual recovery.

The ledger returns explicit lookup outcomes: no work, available work, stale work,
claimed work, or a read error. An empty retrieval ends with a clear message.
Retrieval is read-only; explicit continuation prepares, verifies, and acknowledges
transfer. Multiple candidates require a choice (including none), except fully
automatic mode, which selects the newest. Execution skills load only during
execution preparation, not to discover that there is nothing to resume.

Checkpoint preparation and resume also report `reviewed: accept|reject|none`.
Use `handoff_ledger.py review PATH` to inspect recorded evidence, or add
`--input verdict.json` to record a fingerprint-bound observation. Native review
observations take precedence over exact `review:accept|reject` fallback labels;
ticket status alone is not a verdict. Raw recorded verdicts carry `caller`
provenance rather than proof of a native transition. See [review verdicts](docs/review-verdicts.md)
for the offline reader, input format and #99 integration boundary.

The preferred workflow is:

```sh
python3 skills/session-handoff/hooks/handoff_ledger.py lookup
# Inspect this terminal's identity, transcript path and pressure state (read-only):
python3 skills/session-handoff/hooks/handoff_ledger.py session
# One-shot authorized continuation (select, prepare, verify, acknowledge):
python3 skills/session-handoff/hooks/handoff_ledger.py resume --session SESSION [TOPIC]
# Reuse a previous retrieval only while its content remains in this context:
python3 skills/session-handoff/hooks/handoff_ledger.py resume TOPIC --session SESSION --reuse-receipt RECEIPT --catalog installed-skills.json
# Or step by step:
python3 skills/session-handoff/hooks/handoff_ledger.py prepare TOPIC --session SESSION
# After continuation is authorized, reuse content still in this context:
python3 skills/session-handoff/hooks/handoff_ledger.py prepare TOPIC --session SESSION --execute --reuse-receipt RECEIPT
python3 skills/session-handoff/hooks/handoff_ledger.py verify PATH --session SESSION
python3 skills/session-handoff/hooks/handoff_ledger.py acknowledge PATH --session SESSION
# At the next checkpoint, generate a draft and replace its example facts:
python3 skills/session-handoff/hooks/handoff_ledger.py save --template
python3 skills/session-handoff/hooks/handoff_ledger.py save --input draft.json
```

A successful `resume` also returns a `staleness` verdict (`fresh`, `progressed`,
`superseded`, `conflicting` or `unclassified`) with a one-line `reason`, built
from the checkpoint's `ticket:`/`base:` lines, `Ticket:` trailers on the local
`origin/main` and the ticket's AgentsRoom backlog state. Without
`AGENTSROOM_MCP_SESSION` or a reachable app it is git-only (`source:
"git-only"`) and the resume still succeeds; stale verdicts lead the `action`.
Details: [docs/staleness-verdict.md](docs/staleness-verdict.md).

`lookup` also retires local checkpoints whose work has landed: the ticket is
`done` (not won't-fix) and every commit named in the header is on `origin/main`
after a `git fetch origin`. A save with uncommitted work, a failed fetch or an
unreachable backlog leaves the checkpoint open. Retiring writes one sealed line to
`<checkpoint>.notes.jsonl`; the checkpoint itself is never edited. Newly retired
checkpoints are listed in `auto_closed`, and `history` shows them as `retired`.
`resume` never retires. See SPEC §7.2.2.

`save --input -` reads the JSON draft from stdin, as does omitting `--input`
unless `--topic` or `--body` supplies the draft.

Save from the project directory, or pass the project explicitly as `save <root>`.
From a temporary base or scratch directory inside one, save uses a valid private
project pointer recorded by the session hooks. Without one, it refuses unless
the root was explicit (`save .` also counts). Existing `.handoffs/` or `.git`
directories do not lift this refusal. Redirects appear in JSON `warnings`.

The hook's per-terminal pointer takes precedence over explicit `--session` and
`$HANDOFF_SESSION_ID`; without it, explicit `--session` takes precedence over the
environment. `save --request-id` defaults to a hash of the draft. `lookup` marks
this terminal's handoffs `mine` and names a `selected` path when unambiguous.
`--fields a,b` trims any command's JSON output; `wait TOPIC --status
saved|resumed` polls the ledger. A draft's optional `verify_baseline` lists
known failing test ids so `verify` passes when only those fail; the
baseline is trusted only when pytest printed its summary line and no `ERROR`
lines.
`save` also takes `--topic/--description/--body file.md` instead of a JSON
draft and `--attach FILE[#Lstart-Lend]` for required references
(a line range delivers only that excerpt). It saves into the
predecessor's project from any directory, continues this session's or terminal's
own lineage without `predecessor`, defaults `verify` to the user-level
`~/.context-watch/thresholds.json` `"verify"` (a repo's `.context-watch.json` never
chooses it) or the predecessor's if that checkpoint is trusted, and returns a `transition` naming how this
session should end.
Trust: `save` seals each checkpoint with an HMAC receipt keyed to this machine.
A checkpoint without a valid receipt (hand-written, cloned, edited, or from
another machine) is untrusted: `verify` on it runs nothing and returns
`needs-confirmation` (non-zero exit) with the command; rerun with
`--confirm-verify` only after the user approves it. Untrusted checkpoints also
withhold references outside the project root, and `workspace` flags their
`verify:` line as untrusted. A save retry never seals a file whose
front matter differs from the submitted draft (`conflict`).
Release versions are set with `scripts/bump-version X.Y.Z` (`--check` compares).

For the local AgentsRoom etiquette split, see [drift checking and synchronization](docs/etiquette-sync.md).

Run verify only when preparation says
it is required. Omit `--reuse-receipt` unless the preceding retrieval content
is still in this session context; compaction invalidates it. Supply an installed skill catalog when the checkpoint names
execution dependencies; see [the protocol reference](skills/session-handoff/reference.md).

Save validates and atomically publishes a checkpoint. Identical retries are
idempotent; explicit predecessor identity replaces same-branch guesses. It tries
the other supported location on filesystem write failure and returns a concrete
blocked action if neither works. Host permission denials remain authoritative.
Read failures are reported, and every state mutation respects existing ownership.

The complete resume package has a recommended size of 32,000 bytes, including references and
skills. Required references can select line ranges; optional background stays
out of context. Size targets are advisory; larger content is delivered in full.
Aim for checkpoints under 1,500 words / 24,000 bytes, with no minimum length. Verification preserves pipeline failures while keeping full
output in a log and returning only a bounded excerpt. Resume metrics distinguish
package sizes from host startup/input samples and never record checkpoint text.
`handoff_ledger.py report` summarizes live records separately from tests and
legacy records with unknown origin.

`resolve` returns compact authoritative metadata; `history` provides paginated
historical inspection. Legacy commands remain available, but automatic clearing
requires a validated `save` publication matching this session and trigger.
Changes to unrelated file timestamps cannot satisfy the checkpoint requirement.

Deliberate ceiling: the announcer informs and offers, it does not hijack — if
the session opens with an unrelated explicit task, open handoffs get one
sentence and the user's task proceeds. In chat, the same ledger lives in one
memory file whose one-line description enumerates the open topics; that
description surfaces automatically in every new conversation, and an optional
user-preference line ("mention open handoffs at the start of each chat")
makes the announcement unconditional — the chat equivalent of the
SessionStart hook.

Design guarantees: stdlib-only Python, fail-open (any error exits 0), fires
once per session via a temp-dir latch keyed on `session_id`, and only scans
the tail of large transcripts so it stays fast on every tool call.

### Resume

When requester or ticket is known, successful `resume` and `adopt` return `report_back_draft.path`, a local JSON
file containing an unsent message for the requester and ticket. Review its
saved state and planned next steps before choosing whether to send it.
An optional `## Requester` body section names the recipient; absent addresses
stay unspecified. With neither address, no draft is created. Replay preserves edits. See [report-back drafts](docs/report-back-drafts.md).

## Parallel workers

When a coordinator splits kit work across parallel workers, each worker brief
names the user-facing doc sections that worker owns (in `SKILL.md`,
`continuation.md`, `reference.md`, `README.md` or `SPEC.md`). The worker
updates those sections in its own change, alongside the code and tests. The
coordinator then reconciles overlaps only. When two workers would touch the same
section, assign each a distinct section or give all doc edits to one doc lane.
Interface notes kept in scratch files leave the coordinator to rebuild the docs
from them, which costs a full extra read of every changed interface.

A coordinator that is asked to save while workers are live saves immediately,
then waits for them to report (or confirms their results are durable in commits
or the AgentsRoom mailbox) before it follows the transition. An Agent-tool subagent's
report reaches only the session that spawned it, so `/clear` or `agents_restart`
would discard it. For long-running parallel lanes prefer AgentsRoom workers
(`agents_spawn`, reported back through the mailbox): they outlive the
coordinator. Workers report with `agents_send`, not `backlog_comment`, which
only works on user-feedback backlog items; the coordinator records each report
on the backlog item with a `descriptionAppend` update. The context watcher stays silent inside subagents (it
detects them by the `agent_id` field Claude Code adds to their hook events), so
a subagent never receives the parent's handoff notice.

### Deterministic restart preflight

`skills/session-handoff/hooks/restart_preflight.py` is a stdlib callable module
and JSON CLI. It never invokes restart. Pass an argument object on stdin or
with `--input file.json`; operations are `preflight`, `status`, `refusal`,
and `successor`. Callable names are `preflight`, `status`, `record_refusal`,
and `confirm_successor` respectively. Two argv operations wrap a whole side of a
native cycle in one command with compact output:

- `restart --root R [--workers '<JSON list>'] [--host-capable] [--hold] [--path P]`
  (predecessor, callable `restart`) reads hook/host identity, selects this
  session's single open checkpoint. Save output supplies `--path` for the exact
  published checkpoint, including retries. Without it, several checkpoints block
  with `ambiguous-checkpoint`, up to ten path/ID/command candidates and a total
  count. Match the successful save path or ask the user; never pick by recency.
  Candidate commands preserve caller observations and recheck all gates. It
  derives the deterministic request ID, unions `--hold` with durable dispatcher
  holds, runs `preflight` and, only when it creates a ready intent, stores the
  observations in `.handoffs/restart/<sha256-request-id>.observations` under the
  intent lock (blocked attempts and reruns never overwrite them). It returns
  `{outcome, dispatch, request_id, path, call, agent, restart_prompt}`.
  `--workers '[]'` means known, nothing outstanding; a settled worker without a
  report file is `{id, settled: true, report: null}`. An in-flight mailbox
  delegation is `{id, state: "in_flight", via: "mailbox"}`: its reply must persist
  in the AgentsRoom inbox for the successor. Omitting `--workers` means
  unknown inventory and blocks (`worker-inventory-unknown`). The request ID is
  deterministic, so a rerun after a host refusal returns `refused`: a retry
  needs a new save once the cause is resolved.
- `adopt '<binding JSON>'` (successor, from the restart prompt) or
  `adopt --root R --request-id X` (dispatcher), callable `adopt`, first requires
  a given binding to match the recorded intent exactly (stage `binding`), then
  checks hook/host identity, the environment terminal and a session different
  from the predecessor, prepares internally, records the receipt and resumes the exact
  checkpoint. The body is emitted once, by resume. Output is the resume result
  plus `restart: {request_id, predecessor_session, successor_session, terminal,
  state, host_confirmed}` and a `report` line that states the observed
  identity apart from host acceptance (false until native acceptance is imported). A rerun by the same successor replays; any
  failure, including a failed resume, returns `outcome: blocked` with its `stage`
  (`identity`, `intent`, `binding`, `prepare`, `successor`, `hold`, `resume`) and
  preserves state. A prepare refusal includes the recorded `path`, exact adopt
  `command` and available retrieval-only `preparation` (body, dependencies,
  continuity and predecessor; no delivery receipt). For `prepare-target-mismatch`,
  take the objective/next step from the recorded `path`; `preparation.body`
  describes the other checkpoint. Otherwise inspect that path if no body is
  available. `prepare-path-alias` distinguishes
  equivalent canonical paths from `prepare-target-mismatch`; both leave the
  intent pending. Resolve the cause before retrying; an explicit adopt command
  authorizes only the recorded checkpoint, never a different checkpoint or a
  bypass of identity, trust, ownership or holds.
  A pending intent older than 7 days is refused (stage `intent`,
  `restart-intent-expired`), also by the stepwise `successor`; dispatch reports it
  as a stale warning rather than routing a successor, and it blocks only its own
  checkpoint's restart, so a new save can restart. The retrieved intent keeps a
  `retrieval_receipt` (token SHA-256, path, fingerprint, created, references,
  confirmed_at; never the token), still verifiable after a later resume rewrites
  `.delivery`. Retrieved/refused intents older than 30 days whose checkpoint is no
  longer open and authoritative (compared by real path, so symlinked roots agree)
  are pruned at the next preflight, skipping busy records; pending intents never are. A dispatcher hold applying to the project, topic or
  terminal yields `outcome: held` (stage `hold`) after the receipt is recorded and
  before any resume; rerun after an explicit release.

Clearly unrelated foreign-root records are skipped only when settled (`retrieved`
or `refused`) and valid under the normal field and host-evidence checks, using
the recorded root without reading its registry. Their bytes remain unchanged.
`skipped_intents` lists at most 10 files' `path`, `field: root`, `request_id`,
`recorded` root and `expected` registry root. The current request and checkpoint
must differ, and the filename and root/path bindings must be canonical;
`skipped_total` counts all skipped entries, including on later gate failures.
The full registry is checked after the output cap. Pending, invalid-state,
malformed or uncertain foreign records still block with `invalid-persisted-intent`.
Its `invalid_intent` object names the offending file and field. Preserve it and
follow the [restart recovery procedure](docs/restart-recovery.md).
An invalid intent for the attempted request itself must never be quarantined
and retried for the same checkpoint: preserve its dispatch evidence and, if
the host accepted it or its outcome is unknown, continue manually or save a new
checkpoint for a new request ID.

`acceptance` accepts JSON `{root,request_id,transcript}` on stdin or `--input`.
Successor confirmation automatically searches the predecessor's Codex sessions
or Claude projects transcript and imports a matching native AgentsRoom restart
call/response. Codex completed MCP events and Claude tool-use/result pairs are
supported. Explicit import through this operation uses the same checks,
retaining the event and its SHA256. Exact prompt, predecessor thread, terminal,
completion after intent, and `ok:true,scheduled:true` are required. It records
`host_confirmed:true` for acceptance/scheduling only; it never dispatches or
claims replacement/delivery. Files must resolve under a native sessions root,
name the predecessor session, be regular files opened without following a final
symlink, and fit the bounded read limits (64 MiB per file, 2 MiB per line).
Expired pending intents cannot import evidence.
Without supported native evidence, the flag remains false. Local transcript
provenance is evidence, not cryptographic host attestation.

`preflight(root, path, session, terminal, request_id, workers=None,
host_capable=False, hold=False)` validates authoritative open publication and
checkpoint identity and live claim ownership under the publisher and checkpoint
locks, then durably writes an intent before returning
`outcome: ready, dispatch: true`. The caller supplies observed current identity,
capability, complete worker inventory and intentional hold state, including
`waiting_input`. Unknown or malformed inventory or absent host capability blocks. Inventory is
a list of objects with unique nonempty `id` strings. Each worker
entry requires `report: {path, sha256}` for a durable local result; a mailbox
adapter first retrieves and materializes the report. A settled worker may use
`{id, settled: true, report: null}`; an in-flight mailbox delegation may use
`{id, state: "in_flight", via: "mailbox"}` when its reply persists for the successor. Local report hashes prove bytes, not correctness
or that a running worker cannot produce further results.

**Project pointer index (v3-2).** Before persisting a new intent, preflight
writes a private, body-free pointer to
`.handoffs/pointers/<binding-sha256>.json` (schema version 1), then the intent
and HOME locator, then a durable `dispatch_started_at` marker before returning
`dispatch: true`. A failed pointer write blocks with `stage: pointer` and leaves
no pending intent; a pointer without an intent is inert, and an identical retry
finishes it. A pending intent with a pointer but no marker may reserve dispatch
once after every gate is rechecked; with a marker the result is
`reason: dispatch-reserved` and the agent reconciles, never re-dispatches.
Re-entry requires the intent's own `pointer_version` plus a readable pointer
matching its binding; a pointer file alone grants nothing, so legacy intents
keep reconcile-only behavior even beside a matching pointer.
`handoff_pointers.scan(root, session)` lists candidates only when the handle
resolves through this user's HOME locator, the binding matches the intent, the
publication is sealed by the existing `~/.context-watch/receipt.key`, the
pointer's workspace and task ID equal the sealed checkpoint's (a missing
workspace is `workspace-missing`), `.handoffs/` is a real, owned, not other-writable
directory and `.handoffs/pointers/` is private (else `unsafe-pointer-dir`), no
pointer, intent or checkpoint is tracked by Git (indeterminate checks fail
closed) and continuation is authorized in user scope (`AUTORESUME` /
`CONTEXT_WATCH_AUTORESUME` or `autoresume` in `~/.context-watch/thresholds.json`;
project config never authorizes). Anything else is a diagnostic whose recovery
is explicit `adopt`. Retrieved intents list only for their recorded successor
(`mode: replay`); settled pointers are skipped silently and pruned with their
intents. `.handoffs/` is not gitignored: committing it (for example with
`git add -A`) makes every candidate ineligible as `git-tracked`. Startup
selection is a later slice.

The agent calls the supported host tool only for `dispatch: true`, using the
returned `restart_prompt`, one sentence: `Continue the handoff: run exactly
python3 …/restart_preflight.py adopt h-<24 hex>`. A per-user locator resolves the
handle to its ledger intent; `adopt` verifies the binding hash before enforcing
identity, retrieval and receipt rules. Root and checkpoint paths stay out of the
prompt. Pinned snapshot hooks work independently of cwd; predecessor and successor
share HOME. Legacy full-binding JSON remains accepted for one release. See
[Successor delivery](skills/session-handoff/reference.md#successor-delivery).
Pending/refused/retrieved
responses carry no new restart prompt. The exact prompt is persisted for audit;
new identifiers are bounded ASCII tokens and the final prompt is checked against
the host limit before intent persistence. A durable registry lock also prevents
renaming requests from bypassing pending same-terminal/path or retrieved/refused
same-checkpoint intents, including across different checkpoint paths. Invalid
persisted records fail closed. Repeated requests never dispatch again, except
that an intent recorded with a pointer (`pointer_version`) and without
`dispatch_started_at` (a crash before the marker, so no host call was ever
returned) may reserve dispatch once after every gate is rechecked; with the
marker, reconcile. For intents without a pointer, a crash between intent and
host response leaves `pending`; reconcile it with the host before creating a
new request. `record_refusal(root, request_id, reason)` persists a returned
failure without closing the checkpoint. `status(root, request_id)`
reads `.handoffs/restart/<sha256-request-id>.json`. Reusing an ID with another
identity or checkpoint blocks. An existing malformed intent also blocks and is
preserved for inspection; only a missing record permits a new intent.

`adopt` composes the following steps. After read-only `prepare`, the successor calls
`confirm_successor(root, request_id, session, terminal, delivery_receipt)` with
its new session ID and the returned opaque delivery token. The terminal that
counts is the environment's: the call is refused when it is unset or differs
from the intent's, and a supplied `terminal` must equal it. The module validates
the local delivery receipt, checkpoint and required dependency fingerprints.
The receipt must have been created strictly after the durable intent and be no
more than two hours old; an already confirmed retry remains idempotent. It
durably records `retrieved`. Retrieval authorizes no execution and clears
no human hold. Without imported native acceptance evidence, `host_confirmed`
stays false: caller-provided
JSON cannot prove host replacement, inventory completeness or agent reading.
The agent must gather those observations separately; JSON fields are assertions,
not an authority source. In AgentsRoom the adapter is the agent procedure in
`reference.md` ## AgentsRoom restart: observations are model-gathered with
supported tools, not host-attested. Save transitions now
mark `requires_preflight: true`, and pressure guidance requires ready/dispatch
before a host call. Transition kind alone is not dispatch permission. For AgentsRoom, follow the bundled [restart procedure](skills/session-handoff/reference.md#agentsroom-restart).
It maps supported host tools to these inputs and makes the final self-restart
call with the exact returned prompt. It is agent orchestration, not a Python MCP
client or atomic host transaction. Unknown observations require manual continuation;
the module never restarts a host. Advertised support, installed version, observed
dispatch and native successor proof remain separate evidence. The controlled
proof procedure in that document is required before claiming automatic recovery.

### Lane ownership and side branches

Concurrent workers never write the same file region. Each lane owns its files
and doc sections for the whole batch. A worker that needs to change something it
doesn't own keeps going, but puts that change on a side branch: the lane branch
never carries it, and the worker never picks the wording of a shared section.
The coordinator merges the lane branches, then the side branches, and resolves
shared sections itself.

`skills/session-handoff/lane.py` (stdlib only, ships with the skill) enforces
this. The coordinator writes the manifest once per batch to
`<git-common-dir>/lanes.json` (`git rev-parse --git-common-dir`). Every worktree
shares it and it's never committed. Pass `--manifest` to use another path.

```json
{"base": "<sha of local main>", "target": "main", "order": ["D", "E"],
 "checks": ["python3 -m pytest -q", "bash tests/verify-extension.sh"],
 "lanes": {"D": {"branch": "batch4/lane-d",
                 "paths": ["skills/session-handoff/hooks/context_watch.py", "tests/test_context_watch_*.py"],
                 "sections": ["SPEC.md#5", "README.md#Parallel workers"]}}}
```

`paths` are files, directory prefixes or globs. `FILE#PREFIX` owns each heading
whose title is PREFIX, or starts with PREFIX followed by a non-alphanumeric
character (`SPEC.md#5` owns `5.1 Retrieval`, not `50`), plus everything nested
under it. Give each shared section to exactly one lane, or all doc edits to one
doc lane.

| Command | Who | What it does |
|---|---|---|
| `lane.py start LANE` | worker, first | Resets a fresh branch to `base`. `agents_spawn` branches from `origin/main`, which can trail local main. Refuses a branch that has diverged from `base`. |
| `lane.py check LANE` | worker | Classifies each change since the merge-base with `target` as owned or not (`owner` is the lane that owns it, null if none). Exits 1 if anything isn't owned. |
| `lane.py split LANE` | worker | Moves committed changes the lane doesn't own onto `<branch>-shared-<file>` side branches (one per file, cut from the merge-base), and removes them from the lane. Refuses a hunk that spans sections. |
| `lane.py sync LANE` | worker | Merges `target`. Runs `git merge --abort` when any conflict falls outside the lane, so the worker carries on from its starting point. |
| `lane.py report LANE` | worker, last | Prints branch SHAs and `Starting point: <sha> (expected <sha>); shared-file branches: none \| <branch: file#section>`. Exits 1 on a wrong start. |
| `lane.py integrate` | coordinator | On `target`: merges the lanes in `order`, then their side branches, then runs `checks`. Stops at the first conflict (naming the file, section and owner) or failing check. Already-merged branches are skipped, so it can be re-run after resolving. |

Every command prints one JSON line and exits 0 (ok), 1 (action needed) or 2
(usage).

A worker brief therefore contains:

- the lane name, its branch, and the files and sections it owns (as in the manifest);
- run `lane.py start LANE` before editing, `check` and `split` before reporting,
  and `sync` instead of a bare `git merge main`;
- report through the AgentsRoom mailbox (`agents_send`), quoting the `report`
  line verbatim along with the commands run and their results.

Before integrating, the coordinator checks that every reported starting point
matches `base`.

## Layout

```
session-handoff-kit/
├── .claude-plugin/marketplace.json      # makes this repo a Claude Code marketplace
├── skills/session-handoff/              # the product: one folder for Claude Code + Codex
│   ├── SKILL.md                         # small pointer: fast path, then one dispatch
│   ├── branches/save.md                 # wind-down protocol, loaded only for save/pressure
│   ├── branches/resume.md               # retrieval + authorized continuation, loaded only for those
│   ├── continuation.md                  # authorized continuation: prepare, verify, acknowledge
│   ├── handoff-template.md              # the handoff's shape — edit this to experiment
│   ├── reference.md                     # installing, customizing, mechanics; read on demand
│   ├── install.py                       # registers/removes the hooks (settings.json, hooks.json)
│   ├── lane.py                          # parallel-lane ownership: start/check/split/sync/report/integrate
│   ├── claude-auto                      # launcher: Claude Code with automatic save, clear, resume in tmux
│   ├── codex-auto                       # launcher: bounded Codex runs with automatic resumption
│   ├── agents/openai.yaml               # Codex skill metadata
│   └── hooks/
│       ├── context_watch.py             # the unified watcher
│       ├── handoff_ledger.py            # tracks handoff state and chains
│       ├── handoff_protocol.py          # deterministic save/prepare/verify/acknowledge protocol
│       ├── handoff_dispatch.py          # one validated lifecycle decision per request (dispatch/hold/release)
│       └── thresholds.example.json      # sample per-model threshold config
├── plugins/session-handoff/             # Claude Code + Cowork plugin wrapper
│   ├── .claude-plugin/plugin.json
│   ├── hooks/hooks.json                 # PostToolUse, UserPromptSubmit, SessionStart, Stop
│   └── skills/session-handoff -> ../../../skills/session-handoff
├── scripts/package.sh                   # builds dist/*.plugin and dist/*.skill artifacts
├── scripts/bump-version                 # rewrites (or --check) the version in every file that carries it (incl. product page)
├── chat/session-handoff-chat/
│   ├── SKILL.md                         # behavioral variant for claude.ai
│   └── handoff-template.md              # chat handoff shape — edit this to experiment
└── chrome-extension/                    # Chrome/Edge: pre-populate the new-chat init prompt
```

## Lifecycle dispatcher

`SKILL.md` is a small pointer. A complete hook-supplied resume or prepare
command still runs directly. Anything else goes through one call:

    echo '{"prompt": "continue the handoff"}' | python3 hooks/handoff_ledger.py dispatch --input -

The result carries `outcome` (`proceed`, `ask`, `none`, `blocked`), a typed
`code` with `retryable`, the resolved `operation` and `identity` (with its
provenance), `authorized_to_execute`, the selected `target`, active `holds`,
`restart` eligibility, one `next_action`, and a `state` token. The agent loads
only the `branch` file the result names.

Under AgentsRoom (`AGENTSROOM_AGENT_ID` set), a `blocked` result with a
non-retryable `code` also carries `report_to: {"to": "inbox", "project":
"session-handoff-kit"}`: report the failure to the kit's project inbox rather
than retrying. The key is absent on retryable, successful and non-AgentsRoom
results.

Ledger `save` and `resume` failures (exit 1) follow the same rule: they carry
`retryable`, `true` only when publication was interrupted after the document
was written (retry the same request ID), and a non-retryable `blocked`,
`conflict` or `invalid` result adds `report_to` under AgentsRoom. Outcomes
that ask the caller to act (`needs-confirmation`, `needs-context`,
`needs-original-draft`, `verification-failed`, `verification-timeout`,
`timeout`) are `retryable: false` with no `report_to`; follow their `action`.

- **Vocabulary.** Save requests include `session handoff`, optionally followed
  by `to yourself`, `to your successor`, `to successor` or `to self`;
  `hand off [this|the|my] work|session|handoff`; and bare `handoff` or `hand off`
  (a trailing `.` or `!` is allowed). Quoted mentions (including single and
  curly-single quotes), neutral notices and maintenance requests keep their
  existing guards. Unrecognised commands of at most four words, starting with
  `handoff`, `handoffs`, `resume`, `retrieve`, `checkpoint`, `park` or `hand off`,
  and containing no `?`, return `ask`/`lifecycle_unrecognised`. For `park`, only
  bare `park` or a lifecycle object such as `it` or `your checkpoint` qualifies;
  `park the car` stays `none`. Longer unmatched commands, prose and questions
  also stay `none`.
  The ask action carries user-facing `text` and an `options` array, ordered
  `save`, `resume`, `retrieve`, `none`; each option has `operation`, `label`
  and `description`. Show these verbatim as a pick card, then dispatch with
  the chosen operation explicitly set. `operation: "none"` suppresses prompt
  inference, ignores any explicit target or targeted prompt, and continues the
  current request. A chosen `resume` also needs
  `authorization: {"source": "user"}`. Asking grants no execution authority.
- **One-call save.** `{"operation": "save", "draft": {...}, "host": {...}}`
  publishes the draft in the same call. Inside AgentsRoom, it also runs the
  restart preflight and returns `call: "agents_restart"` with the exact
  `prompt`; that host call is the only second action. Without `draft`, save
  stays routing-only. See SPEC §7.3d slice 1.
- **Authorization.** Only the user's exact lifecycle command, an explicit
  `{"source": "user"}` authorization, or configured autoresume authorizes
  execution. Checkpoint text never does.
- **Targets.** Exact `resume <target>` and `retrieve <target>` prompts select
  an unquoted, whitespace-free `.md` checkpoint path, a kebab-case topic,
  or a readable name such as `Resume Slice 5A review`. Readable names must
  match a stored topic after save's slug normalization, or the full description
  of an open checkpoint (case-insensitive, no partial matching). If topic and
  description name different checkpoints, both are offered and neither takes
  precedence. Multiple open topic heads or duplicate descriptions also ask.
  Only exact kebab-case topic spelling selects explicitly. A slug-normalized
  readable topic or unique description match proceeds only when owned by this
  terminal (or, for an untagged checkpoint, this session). A foreign readable
  name gives `ask`/`foreign_only`; name its exact path or kebab-case topic to
  select it explicitly.
  Unmatched non-path prose such as `resume later` gives
  `ask`/`lifecycle_unrecognised`, without selecting a target.
  A bare readable choice is also accepted with an explicit `resume`/`retrieve`
  operation; resume still needs `{"source": "user"}` authorization.
  Command verbs are case-insensitive; path targets keep their case. Relative
  paths resolve under the dispatch root. Paths containing spaces require JSON
  `target`, which always takes precedence over a prompt target. Other prose
  and quoted mentions do not select a target or authorize execution.
  An exact target never falls back to other work. Zero, one or
  many owned checkpoints give `none`, `proceed` or `ask` with a stable list.
- **Holds.** `handoff_ledger.py hold --scope project|topic:<t>|terminal:<id>
  --reason R` blocks resume and autoresume until `release --scope ...`. Saves
  and retrievals still proceed.
- **Restart eligibility** is `true`, `false` or `"unknown"`, computed only from
  `host` observations the caller supplies (`restart_capable`, `workers`,
  `hold`, `restart_request_id`). Delegated continuity items whose owner is
  missing from the worker inventory block it, as do workers with no
  delegation. `host_confirmed` in dispatch eligibility stays `false`: dispatch does not observe the
  host.
- **Recheck.** Pass `"expect": {"state": <token>}` before you act on an older
  decision. Changed authorization, holds or workspace give `state_changed`.
- **Delivery.** A resume `next_action` is one command with `--fields
  path,body`, run exactly as given (no pipe or output extraction). The prompt
  hook recognizes `continue`/`resume` + `the`/`your` + `handoff`, so both
  common phrasings take the hook fast path without loading the skill. Both
  the hook and the dispatcher say the output is the complete required package
  and must not be clipped. Resume output still leads with a bounded
  `next_step` (at most 3000 encoded bytes, marked when cut) right after
  `action`. A trusted checkpoint paused by the restart guard gets a
  pointer that names the paused state and its reason tokens, never the fast path;
  a lean command for paused state waits until the legacy injected phrase is
  retired.
- **Pending restarts** are found through a supplied `restart_request_id` or a
  read-only scan of `.handoffs/restart/` for this session, terminal or target.
  Without an explicit target, a `restart_request_id` naming a valid, unexpired
  pending intent of this terminal selects that intent's checkpoint exactly,
  before the several-open-checkpoints ambiguity check.
  The requesting session is blocked (`dispatch_pending`); its successor on the
  intent's terminal, as the environment reports it, gets `restart_successor`:
  one `restart_preflight.py adopt` command that prepares, records the successor
  receipt and resumes. Another terminal naming
  the checkpoint gets `dispatch_pending`. Only relevant invalid intents block,
  and only resume and retrieve; save and pressure never block, and foreign,
  unattributable or stale (moved-project) records come back as `warnings`.
- **Known limits.** Recovery overrides ignore holds (they execute no next
  steps); retrieve may write identity and lock sidecars; `hold`/`release` and
  `{"source": "user"}` rely on model compliance, not enforcement.

Trigger fixtures in `tests/triggers/` are static evidence: `tests/test_dispatch.py`
classifies the should-not corpus locally, with no model call.

## Changing the shape of a handoff

The handoff document's structure is deliberately not inside `SKILL.md`. Each
skill ships a `handoff-template.md` next to it holding the front matter, the
body outline, and the length rules; the save branch (`branches/save.md` §1) just points at it. To try a
different handoff shape — extra sections, fewer sections, a different order —
edit `handoff-template.md` alone. Trigger thresholds, file naming, the ledger,
and the resume flow are untouched by that edit.

Two constraints when you rewrite it:

- **Keep the front matter.** The hooks parse it. `status: open` and the
  one-line `description:` are what the session-start announcer reads; drop them
  and your handoffs stop being announced.
- **Keep the shared sections.** `tests/verify-skills.py` asserts the shared
  agent/chat section contract in SPEC §7.4. Run `python3
  tests/verify-skills.py` after editing.

## Adding facts after publishing

A published checkpoint is sealed: its receipt fingerprints the content, so
editing `.handoffs/*.md` makes it untrusted. To record anything learned after
publishing (a review verdict, a landed sha), use `note`:

```bash
python3 skills/session-handoff/hooks/handoff_ledger.py note <checkpoint-or-topic> --text "Opus review passed for 3b19e7e"
```

Each note is one HMAC-sealed line in `<checkpoint>.notes.jsonl`. Resume delivers
open notes after the body under `## Updates since checkpoint`; a successor save
folds them into its body once and closes the old sidecar. A note is refused
above 16000 bytes; delivery shortens each note to 2000 bytes and the section to
8000. Every `saved` result repeats the rule: record later facts with `note`, never
edit `.handoffs/*.md`. See SPEC §7.2.1.

## What "useful on any environment" means

The deterministic token-threshold trigger requires two things: lifecycle hooks
and a readable token feed. Claude Code has both natively; Codex has both behind
an experimental flag. Cowork installs the same plugin but exercises hooks
rarely, so the skill is the reliable surface there. Chat has neither, so the
chat skill is trigger-by-request with a proactive offer — anything more would
be the skill pretending to know numbers it can't see.
