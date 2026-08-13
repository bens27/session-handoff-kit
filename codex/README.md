# Codex CLI Packaging

This directory contains the Codex CLI packaging of the session-handoff system: a deterministic context-usage watcher, structured handoff skill, and open/resumed handoff ledger.

## Installation

Run `codex/install.sh` to install the Codex-specific hooks and skill. The installer uses `merge_hooks.py` to safely merge this plugin's hook configuration into an existing Codex hooks setup (if any) rather than overwriting it, preserving any pre-existing hooks.

## Behavior

The runtime behavior (watcher thresholds, announcer logic, ledger states, and skill output) is identical to the Claude Code plugin. Refer to the plugin's documentation for details:

- See `plugins/session-handoff/README.md` for the full mechanism description.
- See the root `README.md`'s "Codex CLI" install section for context on how this packaging fits into the broader kit.

The `hooks/` directory and `skills/session-handoff/SKILL.md` are kept byte-identical to their Claude-plugin counterparts, so the plugin's documentation accurately describes this directory's runtime behavior.