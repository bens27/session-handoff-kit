# Session Handoff Kit

When an AI Agent reaches a certain amount of cumulative context usage in a session, you'll notice the quality of their reasoning degrades, and you may even notice a cost increase compared to the same questions with less context usage. This is lovingly referred to as 'the dumb zone'. 

This kit is my attempt to simplify this. Here are its components:
1. A context monitor - watches your session's context usage, and allows you to set a limit
2. Context-limit hooks - when you get to your limit, automatically create a handoff document for a new session to retrieve. Also nudge you to clear your current session context usage (Anthropic - if you're reading this - please allow for programmatic context clearing)
3. Session-start actions - when you start or clear a session, automatically check for open handoff documents in the current repo and list them for a user to select (if desired)
4. Handoff customizations - specify skills to be loaded when a handoff is retrieved 
5. Basic state management on handoff documents - open/resumed

One handoff system, packaged for every surface it can run on. A deterministic
hook watches the session's own token usage and — at a configurable threshold —
injects a one-time instruction to invoke the `session-handoff` skill, which
writes a structured `HANDOFF.md` and stops. A `SessionStart` hook announces an
existing `HANDOFF.md` to the next session, closing the loop.

Where hooks don't exist (chat), a behavioral variant of the skill does the same
job on request.

## Install matrix

| Environment | Install | Trigger |
| --- | --- | --- |
| **Claude Code** (CLI, VS Code, JetBrains) | Add this repo as a plugin marketplace, install the `session-handoff` plugin | Deterministic — hook fires at the token threshold |
| **Claude Cowork** (desktop) | Open `session-handoff.plugin` and click install | Skill on request; hooks are in-schema but rarely exercised in Cowork — treat the threshold trigger as best-effort there |
| **Codex CLI** (terminal, IDE extension) | Run `codex/install.sh` | Deterministic — same hook, behind Codex's experimental hooks feature flag |
| **Claude chat** (claude.ai web, **Claude Desktop**, mobile apps) | Open `session-handoff-chat.skill` and click **Save skill** (or upload in Settings → Capabilities) | Conversational — no token feed exists in chat |
| **Claude chat, before your first message** | Load `chrome-extension/` unpacked in Chrome/Edge (see its README) | Pre-fills — optionally auto-sends — the handoff-check prompt into every new chat |

### Claude Code

From a hosted copy (push this folder to GitHub first):

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
tellable-apart as they accumulate.

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
bash codex/install.sh
```

Then enable hooks in `~/.codex/config.toml` (`[features]` → `hooks = true`;
older builds used `codex_hooks = true`). The hook reads the session rollout's
`token_count` events (`last_token_usage`, with the window taken from
`model_context_window` when reported). Two Codex-specific notes:

- Hooks are experimental and have been unavailable on Windows; the accepted
  `hooks.json` shape has varied across versions (see the installer's note).
- On `PostToolUse`, the only documented injection channel is exit code 2 with
  the message on stderr — which replaces that one tool's result. Acceptable,
  since the instruction is to stop and hand off anyway. `UserPromptSubmit`
  uses plain stdout and is non-destructive.
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
Project.)

## Configuration (hook environments)

The watcher measures **window occupancy** — everything the next call will
carry: fresh input, cache writes, **cache reads at full size** (discounted on
the bill, full-weight in the window), and the last call's output. This is a
quality-preservation gauge, not a cost gauge: thresholds should sit where
reasoning quality drops for a given model, which is an absolute-token
property, not a percentage of the window.

Threshold resolution — first match wins; model ids matched by longest
case-insensitive substring:

1. `CONTEXT_WATCH_TOKENS_MAP` — e.g. `opus=120000,sonnet=140000,gpt-5.5=160000`
2. `./.context-watch.json` — project-local per-model config
3. `~/.context-watch/thresholds.json` — user-global per-model config
4. `CONTEXT_WATCH_TOKENS` — global absolute
5. `"default"` key in the config files
6. `CONTEXT_WATCH_PERCENT` x window — only if PERCENT is explicitly set
7. Built-in default: 130,000 tokens

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
| `CONTEXT_WATCH_PENDING` | on | Adds an estimate (~4 chars/token) for the tool result or prompt already in the hook's stdin but not yet in any usage entry; set `0` to disable |
| `CONTEXT_WATCH_LOG` | `~/.context-watch/events.jsonl` | Per-trigger analytics (model, occupancy, cache-read share, threshold); `0` disables. Summarize with `python3 context_watch.py stats` |
| `CONTEXT_WATCH_WINDOW` | `200000` | Used only by the PERCENT path; Codex reports its own window |
| `CONTEXT_WATCH_SKILL` | `session-handoff` | Skill named in the injected instruction |
| `CONTEXT_WATCH_MODE` | `warn` | `warn` injects context; `block` uses the blocking channel on `PostToolUse` |
| `CONTEXT_WATCH_AGENT` | auto | Force `claude` or `codex` if auto-detection guesses wrong |
| `CONTEXT_WATCH_DISABLE` | — | Set `1` to disable without uninstalling |
| `CONTEXT_WATCH_MAX_AGE_DAYS` | `14` | Open handoffs older than this are not announced |
| `CONTEXT_WATCH_AUTORESUME` | — | Set `1` to resume a single open handoff at session start without asking |

## How automated does it get

Fully closed-loop on the hook surfaces. Handoffs carry `status: open` until a
session actually resumes one and marks it (`handoff_ledger.py resume`), so
session starts can check for **untransferred** work every time and never
re-announce what was already picked up:

- **0 open** — silence.
- **1 open** — announced with "read it and continue" (or resumed outright with
  `CONTEXT_WATCH_AUTORESUME=1`).
- **Several open** — enumerated, with an instruction to ask the user which one
  to resume before any other work.

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
├── plugins/session-handoff/             # Claude Code + Cowork plugin
│   ├── .claude-plugin/plugin.json
│   ├── hooks/hooks.json                 # PostToolUse, UserPromptSubmit, SessionStart
│   ├── hooks/context_watch.py           # the unified watcher (also used by Codex)
│   ├── hooks/handoff_ledger.py          # tracks open and resumed handoffs
│   ├── hooks/thresholds.example.json    # sample per-model threshold config
│   └── skills/session-handoff/SKILL.md
├── scripts/package.sh                   # builds dist/*.plugin and dist/*.skill artifacts
├── codex/
│   ├── install.sh                       # copies hook + skill, generates ~/.codex/hooks.json
│   ├── hooks/context_watch.py           # same script
│   └── skills/session-handoff/SKILL.md  # same skill (SKILL.md is portable)
├── chat/session-handoff-chat/SKILL.md   # behavioral variant for claude.ai
└── chrome-extension/                    # Chrome/Edge: pre-populate the new-chat init prompt
```

## What "any environment" honestly means

The deterministic token-threshold trigger requires two things: lifecycle hooks
and a readable token feed. Claude Code has both natively; Codex has both behind
an experimental flag. Cowork installs the same plugin but exercises hooks
rarely, so the skill is the reliable surface there. Chat has neither, so the
chat skill is trigger-by-request with a proactive offer — anything more would
be the skill pretending to know numbers it can't see.
