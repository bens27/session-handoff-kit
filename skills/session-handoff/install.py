#!/usr/bin/env python3
"""Register (or remove) the session-handoff hooks for Claude Code and Codex.

Usage: python3 install.py [claude|codex ...] [--uninstall]

Hook commands point at hooks/context_watch.py beside this file by absolute
path, so run this from where the skill folder is installed, and re-run it
after moving the folder. Idempotent: every run first removes any watcher
entry it finds (by the context_watch.py marker), then adds the current one;
all other hooks are left alone. Each config file is backed up once
(<file>.bak-session-handoff) before its first change.
"""
import json
import os
import re
import shlex
import shutil
import sys

WATCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "context_watch.py")
CMD = "python3 " + shlex.quote(WATCHER)
MARKER = "context_watch.py"
# Recognize fan-outs written by older installers so upgrades restore the
# original command. Current Codex runs independent matching hook groups.
FANOUT = re.compile(r'^payload=\$\(cat\); printf %s "\$payload" \| (?P<base>.*); '
                    r'printf %s "\$payload" \| [^;]*context_watch\.py\S*$', re.S)

CLAUDE_EVENTS = {"PostToolUse": 20, "UserPromptSubmit": 20, "SessionStart": 10, "Stop": 10}
CODEX_EVENTS = {"SessionStart": 20, "UserPromptSubmit": 20, "PostToolUse": 20}  # no Stop in Codex


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)  # malformed config: fail loudly rather than overwrite it


def save(path, data):
    path = os.path.realpath(path)  # write through a dotfiles symlink, don't replace it
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = path + ".bak-session-handoff"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def strip(hooks):
    """Remove every watcher entry from a {event: [group]} map, un-fanning
    Codex fan-outs back to their original command. Returns events touched."""
    touched = set()
    for event, groups in list(hooks.items()):
        kept_groups = []
        for g in groups:
            kept = []
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if MARKER not in cmd:
                    kept.append(h)
                    continue
                touched.add(event)
                m = FANOUT.match(cmd)
                if m:
                    base = m.group("base")
                    if base.startswith("(") and base.endswith(")"):
                        base = base[1:-1]
                    kept.append(dict(h, command=base))
            if kept:
                kept_groups.append(dict(g, hooks=kept))
        if kept_groups or not groups:
            hooks[event] = kept_groups  # an empty list we found stays as found
        else:
            del hooks[event]
    return touched


def claude(uninstall):
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                        "settings.json")
    if uninstall and not os.path.exists(path):
        return
    data = load(path)
    hooks = data.setdefault("hooks", {})
    strip(hooks)
    if not uninstall:
        for event, timeout in CLAUDE_EVENTS.items():
            group = {"hooks": [{"type": "command", "command": CMD, "timeout": timeout}]}
            if event == "PostToolUse":
                group = dict(matcher="", **group)
            hooks.setdefault(event, []).append(group)
    if not hooks:
        del data["hooks"]
    save(path, data)
    print("%s Claude Code hooks: %s" % ("Removed" if uninstall else "Registered", path))


def codex(uninstall):
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    path = os.path.join(home, "hooks.json")
    if uninstall and not os.path.exists(path):
        return
    data = load(path)
    hooks = data.setdefault("hooks", {})
    strip(hooks)
    if not uninstall:
        for event, timeout in CODEX_EVENTS.items():
            # Preserve foreign commands, matchers, outputs and exit statuses.
            hooks.setdefault(event, []).append({
                "hooks": [{"type": "command", "command": CMD, "timeout": timeout}]})
    save(path, data)
    print("%s Codex hooks: %s" % ("Removed" if uninstall else "Registered", path))
    if not uninstall:
        print("""  Codex also needs, once:
  - hooks enabled in %s/config.toml:  [features] hooks = true
  - the changed hook re-approved: Codex trusts hooks by command hash and
    silently skips a changed one. Launch `codex` once and approve it
    using its native hook review.
  - optional: model_auto_compact_token_limit above the watcher threshold,
    so the handoff fires before auto-compaction.""" % home)


def main(argv):
    uninstall = "--uninstall" in argv
    targets = [a for a in argv if a != "--uninstall"] or ["claude", "codex"]
    unknown = set(targets) - {"claude", "codex"}
    if unknown:
        sys.exit(__doc__)
    for t in targets:
        {"claude": claude, "codex": codex}[t](uninstall)


if __name__ == "__main__":
    main(sys.argv[1:])
