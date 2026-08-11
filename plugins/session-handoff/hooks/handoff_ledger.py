#!/usr/bin/env python3
"""handoff-ledger: track which session handoffs are still open (untransferred).

Layout:
  ./.handoffs/<topic>.md   handoff files with front matter (status: open|resumed)
  ./HANDOFF.md             legacy single-file handoff, treated as topic "default"

A handoff is OPEN until a session actually resumes it and marks it, so session
starts can announce untransferred work without ever re-announcing what has
already been picked up.

Subcommands:
  list [dir] [--json] [--max-age-days N]   print open handoffs (default dir: .)
  resume <path>                            mark a handoff resumed (transferred)

Stdlib only. Also importable: scan(root, max_age_days) and mark_resumed(path).
"""

import json
import os
import sys
import time
from datetime import datetime


def parse_front_matter(text):
    """Very small line-based front matter parser: key: value between --- fences."""
    fm = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}
    for line in lines[1:end]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip().lower()] = value.strip()
    return fm


def scan(root, max_age_days=14):
    """Return open handoffs under root as [{path, topic, age_days}], oldest first."""
    now = time.time()
    paths = []
    hdir = os.path.join(root, ".handoffs")
    if os.path.isdir(hdir):
        for name in sorted(os.listdir(hdir)):
            if name.endswith(".md"):
                paths.append(os.path.join(hdir, name))
    legacy = os.path.join(root, "HANDOFF.md")
    if os.path.isfile(legacy):
        paths.append(legacy)

    found = []
    for path in paths:
        try:
            age_days = (now - os.path.getmtime(path)) / 86400.0
            if age_days > max_age_days:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                fm = parse_front_matter(f.read())
            status = (fm.get("status") or "open").lower()
            if status != "open":
                continue
            topic = fm.get("topic") or os.path.splitext(os.path.basename(path))[0]
            if path == legacy and "topic" not in fm:
                topic = "default"
            found.append({"path": path, "topic": topic, "age_days": round(age_days, 1)})
        except Exception:
            continue
    found.sort(key=lambda h: -h["age_days"])
    return found


def mark_resumed(path):
    """Flip a handoff's status to resumed and stamp the time. Idempotent."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            end = None
        if end is not None:
            block = lines[1:end]
            block = [l for l in block if not l.strip().lower().startswith(("status:", "resumed:"))]
            block += ["status: resumed", "resumed: %s" % stamp]
            lines = ["---"] + block + lines[end:]
            new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        else:
            topic = os.path.splitext(os.path.basename(path))[0]
            if os.path.basename(path) == "HANDOFF.md":
                topic = "default"
            header = "---\ntopic: %s\nstatus: resumed\nresumed: %s\n---\n" % (topic, stamp)
            new_text = header + text
    else:
        topic = os.path.splitext(os.path.basename(path))[0]
        if os.path.basename(path) == "HANDOFF.md":
            topic = "default"
        header = "---\ntopic: %s\nstatus: resumed\nresumed: %s\n---\n" % (topic, stamp)
        new_text = header + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return stamp


def _cli(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "list":
        as_json = "--json" in args
        args = [a for a in args if a != "--json"]
        max_age = 14
        if "--max-age-days" in args:
            i = args.index("--max-age-days")
            try:
                max_age = int(args[i + 1])
            except (IndexError, ValueError):
                pass
            args = args[:i] + args[i + 2:]
        root = args[0] if args else "."
        handoffs = scan(root, max_age)
        if as_json:
            print(json.dumps(handoffs, indent=2))
        else:
            for h in handoffs:
                print("%.0f\t%s\t%s" % (h["age_days"], h["topic"], h["path"]))
        return 0
    if cmd == "resume":
        if not args:
            print("usage: handoff_ledger.py resume <path>", file=sys.stderr)
            return 1
        path = args[0]
        if not os.path.isfile(path):
            print("no such handoff: %s" % path, file=sys.stderr)
            return 1
        stamp = mark_resumed(path)
        print("marked resumed (%s): %s" % (stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
