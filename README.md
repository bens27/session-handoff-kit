# Session Handoff Kit

When an AI Agent reaches a certain amount of cumulative context usage in a session, you'll notice the quality of their reasoning degrades, and you may even notice a cost increase compared to the same questions with less context usage. This is lovingly referred to as 'the dumb zone'. 

This kit is my attempt to simplify this. Here are its components:
1. A context monitor - watches your session's context usage, and allows you to set a limit
2. Context-limit hooks - when you get to your limit, automatically create a handoff document for a new session to retrieve. With `AUTORESUME=1`, also nudge you to clear your current session context usage (Anthropic - if you're reading this - please allow for programmatic context clearing). With `HANDOFF_AUTO=1`, clear and resume for you (inside tmux, or through a headless runner)
3. Session-start actions - when you start or clear a session, automatically check for open handoff documents in the current repo and list them for a user to select (if desired)
4. Handoff customizations - specify skills to be loaded when a handoff is retrieved 
5. Basic state management on handoff documents - open/resumed/superseded/abandoned, plus a short claim so two sessions don't resume the same handoff

One handoff system, packaged for every surface it can run on. A deterministic
hook watches the session's own token usage and — at a configurable threshold —
injects a one-time instruction to invoke the `session-handoff` skill, which
writes a structured `HANDOFF.md` and stops. A `SessionStart` hook announces an
existing `HANDOFF.md` to the next session, closing the loop.

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

The launcher requires `claude` and `tmux` on PATH, makes the skill discoverable
in Claude's configuration directory, registers its hooks, and opens tmux when
you are not already in a pane. It enables the save → clear → retrieve → resume
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

**Fully automatic.** `HANDOFF_AUTO=1` removes the keystroke too. Each save
records the terminal it came from (`HANDOFF_TERMINAL_ID`, else
`AGENTSROOM_AGENT_ID`), and the cleared session resumes the newest open handoff
of its own terminal, not the project's newest. In AgentsRoom (the default path),
the context notice tells the agent to finish by calling `agents_restart` with
prompt `resume`, which reopens its tab on that handoff. As an optional fallback inside tmux,
a `Stop` hook types `/clear` and then `resume` into the pane once the handoff
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
drop it in a project folder and the plugin's `SessionStart` hook announces it
in Claude Code or Cowork. (Past-chat retrieval requires the "Search and
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

Config file format (see `hooks/thresholds.example.json`):

```json
{ "claude-opus": 120000, "claude-sonnet": 140000, "gpt-5.5": 160000, "default": 130000 }
```

The active model is re-detected on every check (from the hook input on Codex,
from the transcript's latest entry on Claude Code), so mid-session model
switches re-resolve the threshold automatically.

Other variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CONTEXT_WATCH_RESERVE` | `20000` | When the host reports the window (Codex), caps the threshold at window − reserve so there is room to write the handoff |
| `CONTEXT_WATCH_PENDING` | on | Adds an estimate (~3 chars/token, images/audio a flat 1,600 each) for the tool result or prompt already in the hook's stdin but not yet in any usage entry; set `0` to disable |
| `CONTEXT_WATCH_THINKING` | largest response so far, ≤25k | Tokens assumed for the current response (thinking included), which is written to the transcript only after the hook returns; Claude only |
| `CONTEXT_WATCH_JEV` | off | `1` opts into semantic routing through Jev (requires `TYPESAFE_API_KEY`), at most one attempt per project/session, including failures. Classification selects read-only context, never execution permission. Exact commands always route locally. |
| `CONTEXT_WATCH_LOG` | `~/.context-watch/events.jsonl` | Per-trigger analytics (model, occupancy, cache-read share, threshold); `0` disables. Summarize with `python3 context_watch.py stats` |
| `CONTEXT_WATCH_WINDOW` | `200000` | Used only by the PERCENT path; Codex reports its own window |
| `CONTEXT_WATCH_SKILL` | `session-handoff` | Skill named in the injected instruction |
| `CONTEXT_WATCH_MODE` | `warn` | `warn` injects context; `block` uses the blocking channel on `PostToolUse` |
| `CONTEXT_WATCH_AGENT` | auto | Force `claude` or `codex` if auto-detection guesses wrong |
| `CONTEXT_WATCH_DISABLE` | — | Set `1` to disable without uninstalling |
| `CONTEXT_WATCH_MAX_AGE_DAYS` | `14` | Open handoffs older than this are not announced |
| `CONTEXT_WATCH_AUTORESUME` | — | Set `1` to resume a single open handoff at session start without asking |
| `HANDOFF_AUTO` | — | `1` = fully automatic: implies `AUTORESUME`, the newest open handoff is resumed even when several are open, and the session is cleared for you (tmux Stop hook, or the `context_watch.py auto` runner) |
| `HANDOFF_TERMINAL_ID` | `AGENTSROOM_AGENT_ID`; `claude-auto` generates `tmux-<uuid>` | Terminal identity recorded on each save; in fully automatic mode the cleared session resumes only that terminal's newest open handoff (legacy handoffs without a terminal are not auto-picked) |
| `HANDOFF_AUTO_MAX` | `10` | Fully automatic mode: automatic clears in a row per project before the Stop hook stops and tells you (a session that starts near the threshold would otherwise loop); a turn that ends without the trigger resets the count |

## How automated does it get

The ledger returns explicit lookup outcomes: no work, available work, stale work,
claimed work, or a read error. An empty retrieval ends with a clear message.
Retrieval is read-only; explicit continuation prepares, verifies, and acknowledges
transfer. Multiple candidates require a choice (including none), except fully
automatic mode, which selects the newest. Execution skills load only during
execution preparation, not to discover that there is nothing to resume.

The preferred workflow is:

```sh
python3 skills/session-handoff/hooks/handoff_ledger.py lookup
python3 skills/session-handoff/hooks/handoff_ledger.py prepare TOPIC --session SESSION
# After continuation is authorized, reuse content still in this context:
python3 skills/session-handoff/hooks/handoff_ledger.py prepare TOPIC --session SESSION --execute --reuse-receipt RECEIPT
python3 skills/session-handoff/hooks/handoff_ledger.py verify PATH --session SESSION
python3 skills/session-handoff/hooks/handoff_ledger.py acknowledge PATH --session SESSION
# At the next checkpoint, generate a draft and replace its example facts:
python3 skills/session-handoff/hooks/handoff_ledger.py save --template
python3 skills/session-handoff/hooks/handoff_ledger.py save --session SESSION --request-id CHECKPOINT --input draft.json
```

Use the session ID printed by the hook. Run verify only when preparation says
it is required. Omit `--reuse-receipt` unless the preceding retrieval content
is still in this session context; compaction invalidates it. Supply an installed skill catalog when the checkpoint names
execution dependencies; see [the protocol reference](skills/session-handoff/reference.md).

Save validates and atomically publishes a checkpoint. Identical retries are
idempotent; explicit predecessor identity replaces same-branch guesses. It tries
the other supported location on filesystem write failure and returns a concrete
blocked action if neither works. Host permission denials remain authoritative.
Read failures are reported, and every state mutation respects existing ownership.

The complete resume package defaults to 32,000 bytes, including references and
skills. Required references can select line ranges; optional background stays
out of context. Checkpoints are limited to 1,500 words / 24,000 bytes, with no
minimum length. Verification preserves pipeline failures while keeping full
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

## Layout

```
session-handoff-kit/
├── .claude-plugin/marketplace.json      # makes this repo a Claude Code marketplace
├── skills/session-handoff/              # the product: one folder for Claude Code + Codex
│   ├── SKILL.md                         # trigger, naming, resume
│   ├── handoff-template.md              # the handoff's shape — edit this to experiment
│   ├── reference.md                     # installing, customizing, mechanics; read on demand
│   ├── install.py                       # registers/removes the hooks (settings.json, hooks.json)
│   └── hooks/
│       ├── context_watch.py             # the unified watcher
│       ├── handoff_ledger.py            # tracks handoff state and chains
│       └── thresholds.example.json      # sample per-model threshold config
├── plugins/session-handoff/             # Claude Code + Cowork plugin wrapper
│   ├── .claude-plugin/plugin.json
│   ├── hooks/hooks.json                 # PostToolUse, UserPromptSubmit, SessionStart, Stop
│   └── skills/session-handoff -> ../../../skills/session-handoff
├── scripts/package.sh                   # builds dist/*.plugin and dist/*.skill artifacts
├── chat/session-handoff-chat/
│   ├── SKILL.md                         # behavioral variant for claude.ai
│   └── handoff-template.md              # chat handoff shape — edit this to experiment
└── chrome-extension/                    # Chrome/Edge: pre-populate the new-chat init prompt
```

## Changing the shape of a handoff

The handoff document's structure is deliberately not inside `SKILL.md`. Each
skill ships a `handoff-template.md` next to it holding the front matter, the
body outline, and the length rules; `SKILL.md` §3 just points at it. To try a
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

## What "useful on any environment" means

The deterministic token-threshold trigger requires two things: lifecycle hooks
and a readable token feed. Claude Code has both natively; Codex has both behind
an experimental flag. Cowork installs the same plugin but exercises hooks
rarely, so the skill is the reliable surface there. Chat has neither, so the
chat skill is trigger-by-request with a proactive offer — anything more would
be the skill pretending to know numbers it can't see.
