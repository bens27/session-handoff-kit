#!/usr/bin/env python3
"""Idempotently merge the context-watch hook into an existing ~/.codex/hooks.json.

Codex (verified against 0.145.0) executes only the FIRST hook group per
event. Appending our watcher as a second group — which a naive install once
did by hand — means it silently never runs. This script instead fans our
watcher into the existing first group's command via a stdin fan-out, so both
commands see the same event payload and Codex only has to run one group.

Usage: merge_hooks.py <CODEX_HOME>

Safe to re-run: detects its own fan-out marker and no-ops if already merged,
and strips any stray watcher-only group left over from the old append bug.
"""
import json
import os
import sys

TARGET_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse"]
WATCHER_MARKER = "context_watch.py"
FANOUT_MARKER = 'payload=$(cat)'


def is_watcher_only(group):
    hooks = group.get("hooks", [])
    return bool(hooks) and all(
        WATCHER_MARKER in h.get("command", "") and FANOUT_MARKER not in h.get("command", "")
        for h in hooks
    )


def already_merged(groups):
    return any(FANOUT_MARKER in h.get("command", "")
               for g in groups for h in g.get("hooks", []))


def merge(codex_home):
    hook_cmd = "python3 %s/hooks/context_watch.py" % codex_home
    path = os.path.join(codex_home, "hooks.json")
    data = json.load(open(path)) if os.path.exists(path) else {"hooks": {}}
    hooks = data.setdefault("hooks", {})

    changed = []
    for event in TARGET_EVENTS:
        groups = hooks.setdefault(event, [])
        if already_merged(groups):
            continue
        others = [g for g in groups if not is_watcher_only(g)]
        if others:
            first_hook = others[0]["hooks"][0]
            base_cmd = first_hook["command"]
            first_hook["command"] = (
                'payload=$(cat); printf %%s "$payload" | (%s); '
                'printf %%s "$payload" | %s' % (base_cmd, hook_cmd)
            )
            first_hook["timeout"] = max(first_hook.get("timeout", 10), 20)
            hooks[event] = others
        else:
            hooks[event] = [{"hooks": [{"type": "command", "command": hook_cmd, "timeout": 20}]}]
        changed.append(event)

    json.dump(data, open(path, "w"), indent=2)
    if changed:
        print("Merged context-watch into: %s" % ", ".join(changed))
    else:
        print("Already merged for all target events — no changes.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    merge(sys.argv[1])
