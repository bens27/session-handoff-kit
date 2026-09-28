# Updating local projects

A release updates its checkout and archives. Copied plugins, vendored source and separate runtime profiles have their own update paths.

## Shared installation

Keep one canonical skill directory and point user-level skills/hooks at it. On the machine audited for v0.13.0:

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

The audit found ten profile config files below `~/.agentsroom/codex/`, with no handoff `hooks.json` or inline hook registrations there. The active environment used one of these custom homes. Hook support was enabled in the installed Codex 0.157.1, but feature availability does not prove a hook is registered and trusted in each host session.

Register the shared hook in every existing profile:

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

## Copies and vendored consumers

The audit also found:

- `Portskill/vendor/session-handoff-kit`: v0.11.0. Re-vendor the v0.13.0 tag through Portskill's normal update/tests before expecting its embedded integration to use these fixes.
- The separate `handoff-manager` checkout: v0.8.0. Update it if you still launch its own skill/hooks.
- Older Claude plugin cache directories. Their presence alone does not establish an active installation; check the installed/enabled plugin registry before updating or removing anything.

For any other project that has copied the skill or plugin, update that copy or use the canonical shared installation. A GitHub release does not overwrite vendored code in other repositories.

## Verification

```sh
rg 'version:' "$HOME/.agents/skills/session-handoff/SKILL.md"
python3 "$HOME/.agents/skills/session-handoff/hooks/handoff_ledger.py" save --template
```

Expect version `0.13.0` and a JSON draft. These checks establish the files and CLI; combine them with a fresh session's skill catalog and `/hooks` trust inspection to establish runtime use. The project scan covered known development directories and runtime profiles, not every possible external checkout or host configuration.
