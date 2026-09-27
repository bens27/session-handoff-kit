#!/usr/bin/env python3
"""handoff-ledger: track which session handoffs are still open (untransferred).

Layout:
  ./.handoffs/<YYYYMMDD-HHMM>-<topic>.md   handoff files, named by ending
                                           date/time, with front matter
                                           (status: open|resumed|superseded|abandoned,
                                           description: one-line summary,
                                           skills: comma-separated skill names,
                                           references: comma-separated paths,
                                           project: absolute project root,
                                           git: branch@sha when it was written)
  ./.handoffs/<topic>.md                   legacy undated naming, still scanned
  ./HANDOFF.md                             legacy single file, topic "default"

A handoff is OPEN until a session actually resumes it and marks it, so session
starts can announce untransferred work without ever re-announcing what has
already been picked up. Re-handing-off the same thread writes a new dated file
and marks the previous one superseded.

The project root is the nearest ancestor of the working directory that has a
./.handoffs/ directory (or whose per-project fallback directory holds a handoff
recording it as `project:`), stopping at $HOME; otherwise the directory itself.
A session started in a subfolder therefore still finds the project's handoffs.

Subcommands:
  list [dir] [--json] [--max-age-days N]   print open handoffs (default dir: .)
  resolve <topic-or-path> [dir] [--json]   print every handoff for a topic
                                            oldest first, across all statuses
  new-path <topic> [dir] [--json]          print deterministic path data for a new handoff
  claim <path>                             stamp a handoff as being resumed now, so a
                                            parallel session start does not announce it
  resume <path>                            mark a handoff resumed (transferred)
  supersede <path> [--by <new-path>]       mark a handoff replaced by a newer one
  abandon <path>                           mark a handoff dropped (never to be resumed)
  save-path [dir]                          print where new handoffs should be saved

Stdlib only. Also importable: scan(root, max_age_days, stats=None),
project_root(start), mark_resumed(path), mark_superseded(path).
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

# 20260811-1902-pantry-cli(.md) -> ended 2026-08-11T19:02, topic pantry-cli
DATED_NAME = re.compile(r"^(\d{8})-(\d{4}|\d{6})-(.+)$")
STAMP = "%Y-%m-%dT%H:%M"
CLAIM_TTL = timedelta(hours=2)  # a claim older than this is a crashed session


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


def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(os.path.expanduser(a))) == \
        os.path.normcase(os.path.realpath(os.path.expanduser(b)))


def _fallback_dir(root):
    """The per-project directory save_path() writes to when a project has no
    local .handoffs convention."""
    return os.path.expanduser(os.path.join("~", ".claude", "handoffs",
                                           os.path.basename(os.path.abspath(root))))


def _read_fm(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_front_matter(f.read())


def _fallback_claims(d):
    """True when d's fallback directory holds a handoff recording d as its project."""
    fdir = _fallback_dir(d)
    try:
        for name in os.listdir(fdir):
            if name.endswith(".md"):
                project = _read_fm(os.path.join(fdir, name)).get("project")
                if project and _same_dir(project, d):
                    return True
    except Exception:
        pass
    return False


def project_root(start):
    """Nearest ancestor (inclusive) with a .handoffs/ dir or a fallback-dir handoff
    naming it as project, stopping at $HOME; else start. Deliberately NOT the git
    toplevel: a folder of projects can itself be a git repo."""
    start = os.path.abspath(start)
    home = os.path.abspath(os.path.expanduser("~"))
    d = start
    while d != home:
        if os.path.isdir(os.path.join(d, ".handoffs")) or (d != start and _fallback_claims(d)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


def _handoff_paths(root):
    """Every handoff readable for root, across BOTH locations a handoff can be
    written to: the local ./.handoffs convention and the per-project fallback.
    Reading only one of them silently hides whole projects' handoffs from
    list, resolve, and the session-start announcer. Yields (path, in_fallback)."""
    paths = []
    seen = set()
    fallback = _fallback_dir(root)
    for hdir in (os.path.join(root, ".handoffs"), fallback):
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
            paths.append((path, hdir == fallback))
    legacy = os.path.join(root, "HANDOFF.md")
    if os.path.isfile(legacy):
        paths.append((legacy, False))
    return paths, legacy


def _is_legacy_single_file(path, legacy):
    """The undated single-file convention, in either location."""
    return path == legacy or os.path.basename(path) == "HANDOFF.md"


def _split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def _records(root):
    """Every handoff belonging to root: {path, fm, text, mtime, topic, ended}.
    Fallback-dir files that name a different `project:` belong to another
    project sharing the same basename and are skipped."""
    paths, legacy = _handoff_paths(root)
    out = []
    for path, in_fallback in paths:
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            fm = parse_front_matter(text)
            if in_fallback and fm.get("project") and not _same_dir(fm["project"], root):
                continue
            name_ended, name_topic = _name_parts(path)
            topic = fm.get("topic") or name_topic
            if _is_legacy_single_file(path, legacy) and "topic" not in fm:
                topic = "default"
            out.append({
                "path": path, "fm": fm, "text": text, "mtime": mtime, "topic": topic,
                "legacy": _is_legacy_single_file(path, legacy),
                "ended": (name_ended or fm.get("created")
                          or datetime.fromtimestamp(mtime).strftime(STAMP)),
            })
        except Exception:
            continue
    return out


def _claimed_recently(fm, now):
    try:
        return now - datetime.strptime(fm.get("claimed", ""), STAMP) < CLAIM_TTL
    except ValueError:
        return False


def scan(root, max_age_days=14, stats=None):
    """Return open handoffs under root as [{path, topic, ended, description,
    skills, age_days}], newest (most recently ended) first, one per topic.
    If a dict is passed as stats it receives {"stale": n, "duplicates": n}:
    open handoffs hidden for age, and older open handoffs of a newer one's topic."""
    now = time.time()
    root = project_root(root)
    stale = 0
    found = []
    for r in _records(root):
        fm = r["fm"]
        if (fm.get("status") or "open").lower() != "open":
            continue
        # A bare root HANDOFF.md is only a handoff if it says so near the top.
        if r["legacy"] and not fm and "handoff" not in "\n".join(
                r["text"].splitlines()[:5]).lower():
            continue
        age_days = (now - r["mtime"]) / 86400.0
        if age_days > max_age_days:
            stale += 1
            continue
        found.append({
            "path": r["path"],
            "topic": r["topic"],
            "ended": r["ended"],
            "description": fm.get("description") or "",
            "skills": fm.get("skills") or "",
            "age_days": round(age_days, 1),
            "_claimed": _claimed_recently(fm, datetime.now()),
        })
    found.sort(key=lambda h: h["ended"], reverse=True)
    newest, seen = [], set()
    for h in found:
        if h["topic"] not in seen:
            seen.add(h["topic"])
            newest.append(h)
    # A claimed topic is being resumed by another session right now: hide it,
    # and (having collapsed first) hide its older open handoffs with it.
    newest = [h for h in newest if not h.pop("_claimed")]
    if stats is not None:
        stats["stale"] = stale
        stats["duplicates"] = len(found) - len(newest)
    return newest


def _topic_for_path(path, legacy=None):
    fm = _read_fm(path)
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
    root = project_root(root)

    chain = [{
        "path": r["path"],
        "status": (r["fm"].get("status") or "open").lower(),
        "ended": r["ended"],
        "description": r["fm"].get("description") or "",
        "references": _split_csv(r["fm"].get("references") or ""),
    } for r in _records(root) if r["topic"] == topic]
    chain.sort(key=lambda h: h["ended"])

    # Only the authoritative (newest) handoff's references are current: earlier
    # handoffs' references were either carried forward or deliberately dropped.
    must_also_read = list(dict.fromkeys(chain[-1]["references"])) if chain else []
    missing = [ref for ref in must_also_read
               if not os.path.exists(os.path.join(root, os.path.expanduser(ref)))]
    return {
        "topic": topic,
        "chain": chain,
        "authoritative": chain[-1]["path"] if chain else None,
        "must_also_read": must_also_read,
        "missing_references": missing,
    }


def save_path(root):
    abs_root = project_root(root)
    hdir = os.path.join(abs_root, ".handoffs")
    if os.path.isdir(hdir):
        return hdir
    return _fallback_dir(abs_root)


def _git_position(root):
    """'branch@shortsha' for root, or '' when it is not a git work tree."""
    def git(*args):
        return subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                              text=True, timeout=5).stdout.strip()
    try:
        sha = git("rev-parse", "--short", "HEAD")
        return "%s@%s" % (git("rev-parse", "--abbrev-ref", "HEAD"), sha) if sha else ""
    except Exception:
        return ""


def new_path(topic, root):
    now = datetime.now()
    abs_root = project_root(root)
    directory = save_path(abs_root)
    filename = "%s-%s.md" % (now.strftime("%Y%m%d-%H%M"), topic)
    return {
        "directory": directory,
        "filename": filename,
        "path": os.path.join(directory, filename),
        "created": now.strftime(STAMP),
        "project": abs_root,
        "git": _git_position(abs_root),
    }


def _set_fields(path, fields, drop):
    """Rewrite front matter: remove lines whose key starts with any of drop,
    then append fields in order. Adds a header to files without one."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    new = ["%s: %s" % kv for kv in fields]
    lines = text.splitlines()
    end = None
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is not None:
        block = [l for l in lines[1:end] if not l.strip().lower().startswith(drop)]
        new_text = "\n".join(["---"] + block + new + lines[end:]) + (
            "\n" if text.endswith("\n") else "")
    else:
        _, topic = _name_parts(path)
        if os.path.basename(path) == "HANDOFF.md":
            topic = "default"
        new_text = "---\ntopic: %s\n%s\n---\n" % (topic, "\n".join(new)) + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def _mark(path, status, superseded_by=None):
    """Flip a handoff's status and stamp the time. Idempotent."""
    stamp = datetime.now().strftime(STAMP)
    fields = [("status", status), (status, stamp)]
    drop = ("status:", "resumed:", "superseded:", "abandoned:", "claimed:")
    if superseded_by is not None:
        drop += ("superseded_by:",)
        fields.append(("superseded_by", superseded_by))
    _set_fields(path, fields, drop)
    return stamp


def claim(path):
    stamp = datetime.now().strftime(STAMP)
    _set_fields(path, [("claimed", stamp)], ("claimed:",))
    return stamp


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
        stats = {}
        handoffs = scan(root, max_age, stats)
        if as_json:
            print(json.dumps(handoffs, indent=2))
        else:
            for h in handoffs:
                print("%s\t%s\t%s\t%s" % (h["ended"], h["topic"], h["path"],
                                          h["description"]))
        if stats["stale"]:
            print("%d open handoff(s) older than %d days hidden; raise --max-age-days to "
                  "see them, or `abandon <path>` to close them" % (stats["stale"], max_age),
                  file=sys.stderr)
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
            if resolved["missing_references"]:
                print("missing_references: %s" % ", ".join(resolved["missing_references"]))
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
            for key in ("directory", "filename", "path", "created", "project", "git"):
                print("%s: %s" % (key, result[key]))
        return 0
    if cmd in ("resume", "supersede", "abandon", "claim"):
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
        if cmd == "claim":
            print("claimed (%s): %s" % (claim(path), path))
            return 0
        status = {"resume": "resumed", "supersede": "superseded",
                  "abandon": "abandoned"}[cmd]
        stamp = _mark(path, status, superseded_by)
        print("marked %s (%s): %s" % (status, stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
