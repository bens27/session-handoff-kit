# Updating local projects

v0.14.0 fixes six reproduced integrity/cost defects. Semantic routing now requires
explicit `CONTEXT_WATCH_JEV=1`; continuing an existing topic requires its
`predecessor`; the automatic runner stops on failure and returns exit 75 on
run-limit exhaustion. Codex installation preserves other hooks in separate
groups (verified on 0.157.1). Old fan-out registrations need reinstallation and
native trust review if definitions change.

A release updates its checkout and archives. Copied plugins, vendored source and separate runtime profiles have their own update paths.

## Shared installation

Keep one canonical skill directory and point user-level skills/hooks at it. On the machine verified for v0.14.0:

- `~/.agents/skills/session-handoff` points to this repository's `skills/session-handoff`.
- `~/.claude/skills/session-handoff` points to that shared directory.
- User-level Claude and Codex hook registrations call the shared directory.

Those paths read updated hook code from disk. Start a fresh agent session after updating so previously loaded instructions are replaced. Codex detects skill changes automatically; restarting is the fallback when an update does not appear. See [official skill discovery guidance](https://developers.openai.com/codex/skills).

To register or repair the ordinary user-level installation explicitly, independently of a host's custom `CODEX_HOME`:

```sh
CODEX_HOME="$HOME/.codex" CLAUDE_CONFIG_DIR="$HOME/.claude" \
  python3 "$HOME/.agents/skills/session-handoff/install.py" claude codex
```

The installer preserves other hooks and backs up config files. Use one handoff installation per active runtime: either the standalone registration or a plugin that registers the same hooks. The audited Claude plugin registry contained no installed Session Handoff plugin, despite old cache directories on disk.

## AgentsRoom-managed Codex profiles

The initial audit found ten profile config files below `~/.agentsroom/codex/`, with no handoff `hooks.json` or inline hook registrations there. The active environment used one of these custom homes. Hook support was enabled in the installed Codex 0.157.1, but feature availability does not prove a hook is registered and trusted in each host session.

On September 28 the shared hook was registered in all **11** profiles then
present (including a newly created profile). Codex 0.157.1 app-server `hooks/list`
reported all three intended entries enabled, with no warnings or errors, but
**untrusted** in every profile initially. After the user's explicit activation
request, all 33 definitions were trusted through Codex's native CLI review.
Fresh app-server processes then reported **enabled and trusted** for all three
hooks in every profile, with no warnings or errors. No trust bypass was used.
Where required by the CLI, the kit checkout itself was also trusted in that
profile to open the review UI. Existing AgentsRoom sessions were not interrupted.
The audit used the kit working directory in fresh app-server processes; a running
agent may retain older configuration, and another project's overrides can differ.

To repeat registration in existing or newly created profiles:

```sh
for profile_dir in "$HOME"/.agentsroom/codex/*; do
  [ -f "$profile_dir/config.toml" ] || continue
  CODEX_HOME="$profile_dir" \
    python3 "$HOME/.agents/skills/session-handoff/install.py" codex
done
```

Then start/restart the relevant Codex sessions. In the CLI, use `/hooks` to verify the SessionStart, UserPromptSubmit and PostToolUse entries, their shared `context_watch.py` path, and trust status. If the host has no hook-review UI, open the same profile with:

```sh
CODEX_HOME="$HOME/.agentsroom/codex/PROFILE_DIRECTORY" codex
```

Review and trust the intended hook definitions using `/hooks`. New or changed untrusted hooks are skipped. See [official hook configuration and trust guidance](https://developers.openai.com/codex/hooks).

Repeat registration for newly created profiles, or ask AgentsRoom's developer to register the shared hook when provisioning profiles. Host configuration regeneration or restrictions can affect these files; `/hooks` in the actual profile is the acceptance check. This setup does not repair AgentsRoom's separate transcript-transfer workflow; see [the upstream issue](research/agentsroom-transfer-issue.md).

## AgentsRoom Skills Library copy

The existing account-wide `session-handoff` entry (`zzaq83gtmuhdpr5w`) was
refreshed on September 28 from tag v0.14.0, preserving its ID and attachments.
Its body is now 2,605 characters (originally 7,548), with eight supporting files,
including `hooks/handoff_protocol.py` and `continuation.md`. Both native API and
MCP readback verified the result. The previous library was backed up locally at
`~/.agentsroom/global-skills.before-shk-v014.json`; the on-disk library cache was
also refreshed. Reload the Skills Library and start a fresh agent to replace
previously loaded instructions.

The filesystem symlink does not update this independent library copy. For future
releases, update the existing entry including its bundled files, or configure its
Git source and explicitly Fetch/save. Avoid attaching the full skill universally
merely to make it discoverable: attached bodies consume startup context.

The MCP check found no default skill attachments in this project or its three
saved agents, so availability of that stale library entry does not establish
that it was injected into those sessions. Check whichever project/agent actually
loads it. The registration commands above configure automatic kit hooks; they
do not fix AgentsRoom's separate Context transfer chooser. That chooser already
offers Agent summary as an alternative to Raw transcript.

## Copies and vendored consumers

The audit also found:

- `Portskill/vendor/session-handoff-kit`: updated verbatim to v0.14.0 (`a19fa9f`, Portskill commit `3e5a645`); all 18 Portskill handoff integration tests pass. Its integration remains experimental and disabled by default.
- The separate `handoff-manager` checkout: v0.8.0. Update it if you still launch its own skill/hooks.
- Older Claude plugin cache directories. Their presence alone does not establish an active installation; check the installed/enabled plugin registry before updating or removing anything.

For any other project that has copied the skill or plugin, update that copy or use the canonical shared installation. A GitHub release does not overwrite vendored code in other repositories.

## Verification

```sh
rg 'version:' "$HOME/.agents/skills/session-handoff/SKILL.md"
python3 "$HOME/.agents/skills/session-handoff/hooks/handoff_ledger.py" save --template
```

Expect version `0.14.0` and a JSON draft. These checks establish the files and CLI; combine them with a fresh session's skill catalog and `/hooks` trust inspection to establish runtime use. The project scan covered known development directories and runtime profiles, not every possible external checkout or host configuration.


## v0.14.0 acceptance record

Release commit `a19fa9f` passed 30 protocol tests, 11 integrity/cost regressions
and all six supporting suites locally and in CI. A plugin built from tracked
files completed save, retrieve, receipt reuse, acknowledge and successor save.
AgentsRoom MCP confirmed the refreshed body and all eight bundled files. All
11 existing Codex profile registrations were unchanged by the installer and
fresh `hooks/list` checks reported all 33 definitions enabled and trusted.
Portskill's 18 integration tests passed against the verbatim release copy.
Automatic continuation settings were not enabled or changed by this rollout.
