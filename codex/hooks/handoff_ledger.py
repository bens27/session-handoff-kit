#!/usr/bin/env python3
"""handoff-ledger: track which session handoffs are still open (untransferred).

Layout:
  ./.handoffs/<YYYYMMDD-HHMM>-<topic>.md   handoff files, named by ending
                                           date/time, with front matter
                                           (status: open|resumed|superseded,
                                           description: one-line summary)
  ./.handoffs/<topic>.md                   legacy undated naming, still scanned
  ./HANDOFF.md                             legacy single file, topic "default"

A handoff is OPEN until a session actually resumes it and marks it, so session
starts can announce untransferred work without ever re-announcing what has
already been picked up. Re-handing-off the same thread writes a new dated file
and marks the previous one superseded.

Subcommands:
  list [dir] [--json] [--max-age-days N]   print open handoffs (default dir: .)
  resume <path>                            mark a handoff resumed (transferred)
  supersede <path>                         mark a handoff replaced by a newer one

Stdlib only. Also importable: scan(root, max_age_days), mark_resumed(path),
mark_superseded(path).
"""

import json
import os
import re
import sys
import time
from datetime import datetime

# 20260811-1902-pantry-cli(.md) -> ended 2026-08-11T19:02, topic pantry-cli
DATED_NAME = re.compile(r"^(\d{8})-(\d{4}|\d{6})-(.+)$")


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


def _name_parts(path):
    """(ended_iso_or_None, topic_from_name) from a dated or undated basename."""
    base = os.path.splitext(os.path.basename(path))[0]
    m = DATED_NAME.match(base)
    if not m:
        return None, base
    d, t, topic = m.groups()
    iso = "%s-%s-%sT%s:%s" % (d[:4], d[4:6], d[6:8], t[:2], t[2:4])
    if len(t) == 6:
        iso += ":" + t[4:6]
    return iso, topic


def scan(root, max_age_days=14):
    """Return open handoffs under root as [{path, topic, ended, description,
    age_days}], newest (most recently ended) first."""
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
            mtime = os.path.getmtime(path)
            age_days = (now - mtime) / 86400.0
            if age_days > max_age_days:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                fm = parse_front_matter(f.read())
            status = (fm.get("status") or "open").lower()
            if status != "open":
                continue
            name_ended, name_topic = _name_parts(path)
            topic = fm.get("topic") or name_topic
            if path == legacy and "topic" not in fm:
                topic = "default"
            ended = (name_ended or fm.get("created")
                     or datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M"))
            found.append({
                "path": path,
                "topic": topic,
                "ended": ended,
                "description": fm.get("description") or "",
                "age_days": round(age_days, 1),
            })
        except Exception:
            continue
    found.sort(key=lambda h: h["ended"], reverse=True)
    return found


def _mark(path, status):
    """Flip a handoff's status and stamp the time. Idempotent."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    stamps = ("status:", "resumed:", "superseded:")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            end = None
        if end is not None:
            block = lines[1:end]
            block = [l for l in block if not l.strip().lower().startswith(stamps)]
            block += ["status: %s" % status, "%s: %s" % (status, stamp)]
            lines = ["---"] + block + lines[end:]
            new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        else:
            new_text = _legacy_header(path, status, stamp) + text
    else:
        new_text = _legacy_header(path, status, stamp) + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return stamp


def _legacy_header(path, status, stamp):
    _, topic = _name_parts(path)
    if os.path.basename(path) == "HANDOFF.md":
        topic = "default"
    return "---\ntopic: %s\nstatus: %s\n%s: %s\n---\n" % (topic, status, status, stamp)


def mark_resumed(path):
    return _mark(path, "resumed")


def mark_superseded(path):
    return _mark(path, "superseded")


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
                print("%s\t%s\t%s\t%s" % (h["ended"], h["topic"], h["path"],
                                          h["description"]))
        return 0
    if cmd in ("resume", "supersede"):
        if not args:
            print("usage: handoff_ledger.py %s <path>" % cmd, file=sys.stderr)
            return 1
        path = args[0]
        if not os.path.isfile(path):
            print("no such handoff: %s" % path, file=sys.stderr)
            return 1
        status = "resumed" if cmd == "resume" else "superseded"
        stamp = _mark(path, status)
        print("marked %s (%s): %s" % (status, stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
