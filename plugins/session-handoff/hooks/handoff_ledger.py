#!/usr/bin/env python3
"""handoff-ledger: track which session handoffs are still open (untransferred).

Layout:
  ./.handoffs/<YYYYMMDD-HHMM>-<topic>.md   handoff files, named by ending
                                           date/time, with front matter
                                           (status: open|resumed|superseded,
                                           description: one-line summary,
                                           skills: comma-separated skill names,
                                           references: comma-separated paths)
  ./.handoffs/<topic>.md                   legacy undated naming, still scanned
  ./HANDOFF.md                             legacy single file, topic "default"

A handoff is OPEN until a session actually resumes it and marks it, so session
starts can announce untransferred work without ever re-announcing what has
already been picked up. Re-handing-off the same thread writes a new dated file
and marks the previous one superseded.

Subcommands:
  list [dir] [--json] [--max-age-days N]   print open handoffs (default dir: .)
  resolve <topic-or-path> [dir] [--json]   print every handoff for a topic
                                            oldest first, across all statuses
  new-path <topic> [dir] [--json]          print deterministic path data for a new handoff
  resume <path>                            mark a handoff resumed (transferred)
  supersede <path> [--by <new-path>]       mark a handoff replaced by a newer one
  save-path [dir]                          print where new handoffs should be saved

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


def _fallback_dir(root):
    """The per-project directory save_path() writes to when a project has no
    local .handoffs convention."""
    return os.path.expanduser(os.path.join("~", ".claude", "handoffs",
                                           os.path.basename(os.path.abspath(root))))


def _handoff_paths(root):
    """Every handoff readable for root, across BOTH locations a handoff can be
    written to: the local ./.handoffs convention and the per-project fallback.
    Reading only one of them silently hides whole projects' handoffs from
    list, resolve, and the session-start announcer."""
    paths = []
    seen = set()
    for hdir in (os.path.join(root, ".handoffs"), _fallback_dir(root)):
        if not os.path.isdir(hdir):
            continue
        for name in sorted(os.listdir(hdir)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(hdir, name)
            key = os.path.normcase(os.path.realpath(path))
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    legacy = os.path.join(root, "HANDOFF.md")
    if os.path.isfile(legacy):
        paths.append(legacy)
    return paths, legacy


def _is_legacy_single_file(path, legacy):
    """The undated single-file convention, in either location."""
    return path == legacy or os.path.basename(path) == "HANDOFF.md"


def _split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def scan(root, max_age_days=14):
    """Return open handoffs under root as [{path, topic, ended, description,
    skills, age_days}], newest (most recently ended) first."""
    now = time.time()
    paths, legacy = _handoff_paths(root)

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
            if _is_legacy_single_file(path, legacy) and "topic" not in fm:
                topic = "default"
            ended = (name_ended or fm.get("created")
                     or datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M"))
            found.append({
                "path": path,
                "topic": topic,
                "ended": ended,
                "description": fm.get("description") or "",
                "skills": fm.get("skills") or "",
                "age_days": round(age_days, 1),
            })
        except Exception:
            continue
    found.sort(key=lambda h: h["ended"], reverse=True)
    return found


def _topic_for_path(path, legacy=None):
    with open(path, encoding="utf-8", errors="replace") as f:
        fm = parse_front_matter(f.read())
    _, name_topic = _name_parts(path)
    topic = fm.get("topic") or name_topic
    if _is_legacy_single_file(path, legacy) and "topic" not in fm:
        topic = "default"
    return topic


def resolve(topic_or_path, root):
    if os.path.isfile(topic_or_path):
        topic = _topic_for_path(topic_or_path)
    else:
        topic = topic_or_path

    paths, legacy = _handoff_paths(root)
    chain = []
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                fm = parse_front_matter(f.read())
            name_ended, name_topic = _name_parts(path)
            path_topic = fm.get("topic") or name_topic
            if _is_legacy_single_file(path, legacy) and "topic" not in fm:
                path_topic = "default"
            if path_topic != topic:
                continue
            ended = (name_ended or fm.get("created")
                     or datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M"))
            chain.append({
                "path": path,
                "status": (fm.get("status") or "open").lower(),
                "ended": ended,
                "description": fm.get("description") or "",
                "references": _split_csv(fm.get("references") or ""),
            })
        except Exception:
            continue
    chain.sort(key=lambda h: h["ended"])

    must_also_read = []
    seen = set()
    for entry in chain:
        for ref in entry["references"]:
            if ref not in seen:
                seen.add(ref)
                must_also_read.append(ref)

    authoritative = chain[-1]["path"] if chain else None
    return {
        "topic": topic,
        "chain": chain,
        "authoritative": authoritative,
        "must_also_read": must_also_read,
    }


def save_path(root):
    abs_root = os.path.abspath(root)
    hdir = os.path.join(abs_root, ".handoffs")
    if os.path.isdir(hdir):
        return hdir
    return _fallback_dir(abs_root)


def new_path(topic, root):
    now = datetime.now()
    directory = save_path(root)
    filename = "%s-%s.md" % (now.strftime("%Y%m%d-%H%M"), topic)
    return {
        "directory": directory,
        "filename": filename,
        "path": os.path.join(directory, filename),
        "created": now.strftime("%Y-%m-%dT%H:%M"),
    }


def _mark(path, status, superseded_by=None):
    """Flip a handoff's status and stamp the time. Idempotent."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M")
    stamps = ("status:", "resumed:", "superseded:")
    if superseded_by is not None:
        stamps += ("superseded_by:",)
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
            if superseded_by is not None:
                block += ["superseded_by: %s" % superseded_by]
            lines = ["---"] + block + lines[end:]
            new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        else:
            new_text = _legacy_header(path, status, stamp, superseded_by) + text
    else:
        new_text = _legacy_header(path, status, stamp, superseded_by) + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return stamp


def _legacy_header(path, status, stamp, superseded_by=None):
    _, topic = _name_parts(path)
    if os.path.basename(path) == "HANDOFF.md":
        topic = "default"
    header = "---\ntopic: %s\nstatus: %s\n%s: %s\n" % (topic, status, status, stamp)
    if superseded_by is not None:
        header += "superseded_by: %s\n" % superseded_by
    return header + "---\n"


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
    if cmd == "resolve":
        as_json = "--json" in args
        args = [a for a in args if a != "--json"]
        if not args:
            print("usage: handoff_ledger.py resolve <topic-or-path> [dir] [--json]",
                  file=sys.stderr)
            return 1
        root = args[1] if len(args) > 1 else "."
        resolved = resolve(args[0], root)
        if not resolved["chain"]:
            print("no handoffs found for topic: %s" % resolved["topic"], file=sys.stderr)
            return 1
        if as_json:
            print(json.dumps(resolved, indent=2))
        else:
            for h in resolved["chain"]:
                print("%s\t%s\t%s\t%s" % (h["ended"], h["status"], h["path"],
                                          h["description"]))
            print("authoritative: %s" % resolved["authoritative"])
            if resolved["must_also_read"]:
                print("must_also_read: %s" % ", ".join(resolved["must_also_read"]))
        return 0
    if cmd == "save-path":
        root = args[0] if args else "."
        print(save_path(root))
        return 0
    if cmd == "new-path":
        as_json = "--json" in args
        args = [a for a in args if a != "--json"]
        if not args:
            print("usage: handoff_ledger.py new-path <topic> [dir] [--json]",
                  file=sys.stderr)
            return 1
        root = args[1] if len(args) > 1 else "."
        result = new_path(args[0], root)
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            print("directory: %s" % result["directory"])
            print("filename: %s" % result["filename"])
            print("path: %s" % result["path"])
            print("created: %s" % result["created"])
        return 0
    if cmd in ("resume", "supersede"):
        if not args:
            print("usage: handoff_ledger.py %s <path>" % cmd, file=sys.stderr)
            return 1
        superseded_by = None
        if cmd == "supersede" and "--by" in args:
            i = args.index("--by")
            try:
                superseded_by = args[i + 1]
            except IndexError:
                print("usage: handoff_ledger.py supersede <path> [--by <new-path>]",
                      file=sys.stderr)
                return 1
            args = args[:i] + args[i + 2:]
        path = args[0]
        if not os.path.isfile(path):
            print("no such handoff: %s" % path, file=sys.stderr)
            return 1
        status = "resumed" if cmd == "resume" else "superseded"
        stamp = _mark(path, status, superseded_by)
        print("marked %s (%s): %s" % (status, stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
