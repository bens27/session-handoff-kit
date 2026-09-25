#!/usr/bin/env python3
"""handoff-ledger: track which session handoffs are still open (untransferred).

Layout:
  ./.handoffs/<YYYYMMDD-HHMM>-<topic>.md   handoff files, named by ending
                                           date/time, with front matter
                                           (status: open|resuming|resumed|superseded,
                                           description: one-line summary,
                                           skills: comma-separated skill names,
                                           references: comma-separated paths)
  ./.handoffs/<topic>.md                   legacy undated naming, still scanned
  ./HANDOFF.md                             legacy single file, topic "default"
  ~/.claude/handoffs/<basename>-<id>/      per-workspace fallback when the project
                                           has no ./.handoffs; <id> hashes the
                                           resolved workspace root so two projects
                                           sharing a folder name never collide.
                                           workspace.json records provenance.
  ~/.claude/handoffs/<basename>/           legacy basename-only fallback: ambiguous
                                           (any same-named project may have written
                                           it), so it is QUARANTINED — listed and
                                           announced as such, never auto-resumed,
                                           never deleted. `recover-legacy` adopts it.

A handoff is OPEN until a session actually resumes it and marks it, so session
starts can announce untransferred work without ever re-announcing what has
already been picked up. Pickup is a two-step, recoverable transition:
`claim` marks it `resuming` with an owner/session/time (a stale claim, older
than the TTL, is recoverable by the next session); `resume` completes the
transfer. Re-handing-off the same thread writes a new dated file and marks the
previous one superseded. All status writes are atomic (temp + rename) and
refuse to clobber a file that changed underneath them.

Subcommands:
  list [dir] [--json] [--max-age-days N]   print open handoffs (default dir: .);
                                            entries older than N days are flagged
                                            stale, not hidden
  resolve <topic-or-path> [dir] [--json]   chain for a topic oldest first, the
                                            authoritative entry, and its validated
                                            must_also_read references
  new-path <topic> [dir] [--json]          unique path data for a new handoff
  claim <path> --owner <id> [--session <id>]
                                            mark a handoff resuming (recoverable)
  release <path> --owner <id>              give a claim back (resuming -> open)
  resume <path> [--owner <id>]             mark a handoff resumed (transferred)
  supersede <path> [--by <new-path>]       mark a handoff replaced by a newer one
  save-path [dir]                          print where new handoffs should be saved
  workspace [dir] [--json]                 print the workspace identity/provenance
  recover-legacy [dir]                     move quarantined legacy fallback files
                                            into this workspace's fallback dir

Stdlib only. Also importable: scan(root, max_age_days), resolve(topic, root),
claim(path, owner), mark_resumed(path), mark_superseded(path).
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime

# 20260811-1902-pantry-cli(.md) -> ended 2026-08-11T19:02, topic pantry-cli
DATED_NAME = re.compile(r"^(\d{8})-(\d{4}|\d{6})-(.+)$")
ISO_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
STATUSES = ("open", "resuming", "resumed", "superseded")
REASONS = ("user-parked", "context-pressure", "compaction")
MAX_REFS = 8              # references loaded on resume, per authoritative handoff
MAX_REF_BYTES = 256_000   # total bytes of references loaded on resume
PROVENANCE = "workspace.json"


def _claim_ttl_s():
    try:
        return int(os.environ.get("CONTEXT_WATCH_CLAIM_TTL_MIN", "120")) * 60
    except ValueError:
        return 7200


def _now():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _parse_stamp(value):
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except (ValueError, TypeError):
            continue
    return None


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


# ---------------------------------------------------------------- workspace

def workspace_identity(root):
    """Stable identity for a workspace: the resolved real path (symlinks
    followed) hashed into a short id, plus provenance a human can check.
    A git worktree keeps its own identity (its own checkout, its own work)
    but records the main repository it belongs to."""
    real = os.path.realpath(os.path.abspath(root))
    ident = {
        "root": real,
        "basename": os.path.basename(real) or "root",
        "id": hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()[:12],
    }
    dotgit = os.path.join(real, ".git")
    try:
        if os.path.isfile(dotgit):
            with open(dotgit, encoding="utf-8", errors="replace") as f:
                first = f.readline().strip()
            if first.startswith("gitdir:"):
                gitdir = os.path.realpath(os.path.join(real, first[7:].strip()))
                ident["gitdir"] = gitdir
                parts = gitdir.split(os.sep)
                if "worktrees" in parts:
                    common = os.sep.join(parts[:parts.index("worktrees")])
                    ident["worktree_of"] = os.path.dirname(common)
    except Exception:
        pass
    return ident


def _fallback_root():
    return os.path.expanduser(os.path.join("~", ".claude", "handoffs"))


def _fallback_dir(root):
    """The per-workspace directory save_path() writes to when a project has no
    local .handoffs convention. Keyed by basename AND resolved-path hash."""
    ident = workspace_identity(root)
    return os.path.join(_fallback_root(), "%s-%s" % (ident["basename"], ident["id"]))


def _legacy_fallback_dir(root):
    """Pre-0.8 basename-only fallback: ambiguous across same-named projects."""
    return os.path.join(_fallback_root(), workspace_identity(root)["basename"])


def _write_provenance(directory, root):
    path = os.path.join(directory, PROVENANCE)
    if os.path.exists(path):
        return
    ident = workspace_identity(root)
    ident["created"] = _now()
    ident["ledger"] = os.path.abspath(__file__)
    try:
        _atomic_write(path, json.dumps(ident, indent=2) + "\n")
    except Exception:
        pass


def _legacy_is_ours(root):
    """A legacy dir is unambiguous only if a provenance file names this root."""
    try:
        with open(os.path.join(_legacy_fallback_dir(root), PROVENANCE), encoding="utf-8") as f:
            return json.load(f).get("root") == workspace_identity(root)["root"]
    except Exception:
        return False


def _handoff_paths(root):
    """Every handoff readable for root: the local ./.handoffs convention, the
    per-workspace fallback, and (quarantined) the ambiguous legacy fallback.
    Returns ([(path, quarantined)], legacy_single_file)."""
    paths = []
    seen = set()
    legacy_dir = _legacy_fallback_dir(root)
    legacy_quarantined = not _legacy_is_ours(root)
    for hdir, quarantined in ((os.path.join(root, ".handoffs"), False),
                              (_fallback_dir(root), False),
                              (legacy_dir, legacy_quarantined)):
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
            paths.append((path, quarantined))
    legacy = os.path.join(root, "HANDOFF.md")
    if os.path.isfile(legacy):
        paths.append((legacy, False))
    return paths, legacy


def _is_legacy_single_file(path, legacy):
    """The undated single-file convention, in either location."""
    return path == legacy or os.path.basename(path) == "HANDOFF.md"


def _split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------- reading

def _read_entry(path, legacy, now, max_age_days, quarantined=False):
    """One handoff's metadata, validated. Malformed metadata is reported in
    `problems`, never used to hide the file."""
    st = os.stat(path)
    mtime = st.st_mtime
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    fm = parse_front_matter(text)
    problems = []
    if text.startswith("---") and not fm:
        problems.append("front matter has no closing fence")
    status = (fm.get("status") or "open").lower()
    if status not in STATUSES:
        problems.append("unknown status %r (treated as open)" % status)
        status = "open"
    name_ended, name_topic = _name_parts(path)
    topic = fm.get("topic") or name_topic
    if _is_legacy_single_file(path, legacy) and "topic" not in fm:
        topic = "default"
    created = fm.get("created") or ""
    if created and not ISO_STAMP.match(created):
        problems.append("created %r is not ISO date-time (using file mtime)" % created)
        created = ""
    ended = (name_ended or created
             or datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M"))
    reason = (fm.get("reason") or "").lower()
    if reason and reason not in REASONS:
        problems.append("unknown reason %r" % reason)
    age_days = (now - mtime) / 86400.0
    claim = None
    stale_claim = False
    if status == "resuming":
        claim = {"owner": fm.get("claim_owner") or "", "session": fm.get("claim_session") or "",
                 "at": fm.get("claim_at") or ""}
        at = _parse_stamp(claim["at"])
        if at is None:
            problems.append("resuming without a parseable claim_at (treated as stale)")
            stale_claim = True
        else:
            stale_claim = (now - at) > _claim_ttl_s()
    return {
        "path": path,
        "topic": topic,
        "status": status,
        "ended": ended,
        "description": fm.get("description") or "",
        "skills": fm.get("skills") or "",
        "reason": reason,
        "references": _split_csv(fm.get("references") or ""),
        "age_days": round(age_days, 1),
        "stale": age_days > max_age_days,
        "quarantined": quarantined,
        "provenance": "legacy-basename-fallback" if quarantined else "workspace",
        "claim": claim,
        "stale_claim": stale_claim,
        "problems": problems,
        "_stat": (st.st_mtime_ns, st.st_size),
        "_text": text,
    }


def scan(root, max_age_days=14):
    """Return open (or resuming) handoffs under root, newest first. Nothing is
    hidden by age or quarantine: `stale`, `quarantined`, `stale_claim` and
    `problems` are flags for the announcer to surface."""
    now = time.time()
    paths, legacy = _handoff_paths(root)
    found = []
    for path, quarantined in paths:
        try:
            e = _read_entry(path, legacy, now, max_age_days, quarantined)
        except Exception:
            continue
        if e["status"] not in ("open", "resuming"):
            continue
        e.pop("_stat"), e.pop("_text")
        found.append(e)
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


def _validate_references(refs, handoff_path, root):
    """Resolve `references:` of the authoritative handoff only, inside the
    workspace (or the handoff's own directory), existing, capped in count and
    total bytes. Returns (ok_paths, rejected[{ref, reason}])."""
    allowed = [os.path.realpath(os.path.dirname(handoff_path)),
               workspace_identity(root)["root"]]
    ok, rejected, total = [], [], 0
    for i, ref in enumerate(refs):
        if i >= MAX_REFS:
            rejected.append({"ref": ref, "reason": "over count cap (%d)" % MAX_REFS})
            continue
        if os.path.isabs(os.path.expanduser(ref)):
            rejected.append({"ref": ref, "reason": "absolute paths are not loaded"})
            continue
        found = None
        for base in allowed:
            cand = os.path.realpath(os.path.join(base, ref))
            if os.path.isfile(cand):
                found = cand
                break
        if not found:
            rejected.append({"ref": ref, "reason": "not found"})
            continue
        if not any(found == a or found.startswith(a + os.sep) for a in allowed):
            rejected.append({"ref": ref, "reason": "outside workspace"})
            continue
        size = os.path.getsize(found)
        if total + size > MAX_REF_BYTES:
            rejected.append({"ref": ref, "reason": "over byte cap (%d)" % MAX_REF_BYTES})
            continue
        total += size
        if found not in ok:
            ok.append(found)
    return ok, rejected


def resolve(topic_or_path, root):
    if os.path.isfile(topic_or_path):
        topic = _topic_for_path(topic_or_path)
    else:
        topic = topic_or_path

    now = time.time()
    paths, legacy = _handoff_paths(root)
    chain = []
    for path, quarantined in paths:
        if quarantined:
            continue
        try:
            e = _read_entry(path, legacy, now, 14)
        except Exception:
            continue
        if e["topic"] != topic:
            continue
        chain.append({k: e[k] for k in ("path", "status", "ended", "description",
                                         "references", "problems")})
    chain.sort(key=lambda h: h["ended"])

    authoritative = chain[-1] if chain else None
    must_also_read, rejected = [], []
    if authoritative:
        must_also_read, rejected = _validate_references(
            authoritative["references"], authoritative["path"], root)
    return {
        "topic": topic,
        "chain": chain,
        "authoritative": authoritative["path"] if authoritative else None,
        "must_also_read": must_also_read,
        "unresolved_references": rejected,
        "reference_caps": {"max_refs": MAX_REFS, "max_bytes": MAX_REF_BYTES},
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
    stem = "%s-%s" % (now.strftime("%Y%m%d-%H%M"), topic)
    filename = stem + ".md"
    n = 1
    while os.path.exists(os.path.join(directory, filename)):
        n += 1
        filename = "%s-%d.md" % (stem, n)
    if directory == _fallback_dir(root):
        try:
            os.makedirs(directory, exist_ok=True)
            _write_provenance(directory, root)
        except Exception:
            pass
    return {
        "directory": directory,
        "filename": filename,
        "path": os.path.join(directory, filename),
        "created": now.strftime("%Y-%m-%dT%H:%M"),
    }


def recover_legacy(root):
    """Adopt quarantined legacy fallback files into this workspace's fallback
    directory. Moves (never deletes); a name clash keeps both by suffixing."""
    src = _legacy_fallback_dir(root)
    dst = _fallback_dir(root)
    moved = []
    if not os.path.isdir(src):
        return moved
    os.makedirs(dst, exist_ok=True)
    _write_provenance(dst, root)
    for name in sorted(os.listdir(src)):
        if not name.endswith(".md"):
            continue
        target = os.path.join(dst, name)
        n = 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(dst, "%s-%d.md" % (name[:-3], n))
        os.rename(os.path.join(src, name), target)
        moved.append(target)
    return moved


# ---------------------------------------------------------------- writing

class LedgerConflict(Exception):
    pass


def _atomic_write(path, text, expect_stat=None):
    """Write via temp + rename. With expect_stat, refuse when the file changed
    since it was read (mtime_ns, size)."""
    if expect_stat is not None:
        st = os.stat(path)
        if (st.st_mtime_ns, st.st_size) != expect_stat:
            raise LedgerConflict("%s changed on disk since it was read" % path)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # ponytail: stat-then-replace window remains; a lock file if two agents
    # ever race the same handoff in the same second.


def _rewrite_front_matter(path, text, drop_prefixes, add_lines):
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            end = None
        if end is not None:
            block = [l for l in lines[1:end]
                     if not l.strip().lower().startswith(drop_prefixes)]
            lines = ["---"] + block + add_lines + lines[end:]
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    _, topic = _name_parts(path)
    if os.path.basename(path) == "HANDOFF.md":
        topic = "default"
    return "---\ntopic: %s\n%s\n---\n" % (topic, "\n".join(add_lines)) + text


CLAIM_KEYS = ("claim_owner:", "claim_session:", "claim_at:")


def _load(path):
    st = os.stat(path)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_front_matter(text), text, (st.st_mtime_ns, st.st_size)


def claim(path, owner, session=None):
    """open -> resuming, recorded with owner/session/time. Idempotent for the
    same owner; refuses a live claim by another owner; takes over a stale one."""
    fm, text, st = _load(path)
    status = (fm.get("status") or "open").lower()
    if status in ("resumed", "superseded"):
        return {"ok": False, "reason": "already %s" % status, "path": path}
    add = []
    if status == "resuming":
        holder = fm.get("claim_owner") or ""
        at = _parse_stamp(fm.get("claim_at") or "")
        live = at is not None and (time.time() - at) <= _claim_ttl_s()
        if holder == owner:
            return {"ok": True, "status": "resuming", "path": path, "idempotent": True}
        if live:
            return {"ok": False, "reason": "claimed by %s at %s" % (holder, fm.get("claim_at")),
                    "path": path}
        add.append("recovered_from: %s %s" % (holder, fm.get("claim_at") or "?"))
    stamp = _now()
    add = ["status: resuming", "claim_owner: %s" % owner,
           "claim_session: %s" % (session or owner), "claim_at: %s" % stamp] + add
    new_text = _rewrite_front_matter(path, text, ("status:",) + CLAIM_KEYS + ("recovered_from:",), add)
    _atomic_write(path, new_text, st)
    return {"ok": True, "status": "resuming", "path": path, "claim_at": stamp}


def release(path, owner):
    fm, text, st = _load(path)
    if (fm.get("status") or "").lower() != "resuming":
        return {"ok": False, "reason": "not resuming", "path": path}
    if (fm.get("claim_owner") or "") != owner:
        return {"ok": False, "reason": "claimed by %s" % fm.get("claim_owner"), "path": path}
    new_text = _rewrite_front_matter(path, text, ("status:",) + CLAIM_KEYS, ["status: open"])
    _atomic_write(path, new_text, st)
    return {"ok": True, "status": "open", "path": path}


def _mark(path, status, superseded_by=None, owner=None):
    """Flip a handoff's status and stamp the time. Idempotent. A live claim by
    another owner blocks `resumed` unless that owner marks it."""
    fm, text, st = _load(path)
    current = (fm.get("status") or "open").lower()
    if current == status and ("%s:" % status) in text.lower().split("\n---", 1)[0]:
        return fm.get(status) or _now()  # already marked: no write, no clock bump
    if status == "resumed" and current == "resuming":
        holder = fm.get("claim_owner") or ""
        at = _parse_stamp(fm.get("claim_at") or "")
        live = at is not None and (time.time() - at) <= _claim_ttl_s()
        if live and owner is not None and holder != owner:
            raise LedgerConflict("claimed by %s at %s" % (holder, fm.get("claim_at")))
    stamp = _now()
    drop = ("status:", "resumed:", "superseded:") + CLAIM_KEYS
    if superseded_by is not None:
        drop += ("superseded_by:",)
    add = ["status: %s" % status, "%s: %s" % (status, stamp)]
    if superseded_by is not None:
        add.append("superseded_by: %s" % superseded_by)
    _atomic_write(path, _rewrite_front_matter(path, text, drop, add), st)
    return stamp


def mark_resumed(path, owner=None):
    return _mark(path, "resumed", owner=owner)


def mark_superseded(path, superseded_by=None):
    return _mark(path, "superseded", superseded_by)


# ---------------------------------------------------------------- cli

def _take_opt(args, flag):
    if flag in args:
        i = args.index(flag)
        try:
            value = args[i + 1]
        except IndexError:
            return args, None, True
        return args[:i] + args[i + 2:], value, False
    return args, None, False


def _cli(argv):
    if not argv:
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]

    if cmd == "list":
        args, raw, _ = _take_opt(args, "--max-age-days")
        try:
            max_age = int(raw) if raw is not None else 14
        except ValueError:
            max_age = 14
        root = args[0] if args else "."
        handoffs = scan(root, max_age)
        if as_json:
            print(json.dumps(handoffs, indent=2))
        else:
            for h in handoffs:
                flags = [f for f, on in (("stale", h["stale"]), ("quarantined", h["quarantined"]),
                                         ("resuming", h["status"] == "resuming"),
                                         ("stale-claim", h["stale_claim"]),
                                         ("malformed", bool(h["problems"]))) if on]
                print("%s\t%s\t%s\t%s%s" % (h["ended"], h["topic"], h["path"], h["description"],
                                            ("\t[%s]" % ",".join(flags)) if flags else ""))
        return 0

    if cmd == "resolve":
        if not args:
            print("usage: handoff_ledger.py resolve <topic-or-path> [dir] [--json]", file=sys.stderr)
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
                print("%s\t%s\t%s\t%s" % (h["ended"], h["status"], h["path"], h["description"]))
            print("authoritative: %s" % resolved["authoritative"])
            if resolved["must_also_read"]:
                print("must_also_read: %s" % ", ".join(resolved["must_also_read"]))
            for r in resolved["unresolved_references"]:
                print("unresolved_reference: %s (%s)" % (r["ref"], r["reason"]))
        return 0

    if cmd == "save-path":
        print(save_path(args[0] if args else "."))
        return 0

    if cmd == "workspace":
        ident = workspace_identity(args[0] if args else ".")
        ident["save_path"] = save_path(args[0] if args else ".")
        ident["legacy_fallback"] = _legacy_fallback_dir(args[0] if args else ".")
        print(json.dumps(ident, indent=2) if as_json else
              "\n".join("%s: %s" % kv for kv in ident.items()))
        return 0

    if cmd == "recover-legacy":
        moved = recover_legacy(args[0] if args else ".")
        print(json.dumps(moved) if as_json else
              ("recovered %d file(s):\n%s" % (len(moved), "\n".join(moved)) if moved
               else "nothing to recover"))
        return 0

    if cmd == "new-path":
        if not args:
            print("usage: handoff_ledger.py new-path <topic> [dir] [--json]", file=sys.stderr)
            return 1
        result = new_path(args[0], args[1] if len(args) > 1 else ".")
        if as_json:
            print(json.dumps(result, indent=2))
        else:
            for key in ("directory", "filename", "path", "created"):
                print("%s: %s" % (key, result[key]))
        return 0

    if cmd in ("claim", "release", "resume", "supersede"):
        args, owner, bad = _take_opt(args, "--owner")
        args, session, bad2 = _take_opt(args, "--session")
        args, superseded_by, bad3 = _take_opt(args, "--by")
        if not args or bad or bad2 or bad3 or (cmd in ("claim", "release") and not owner):
            print("usage: handoff_ledger.py %s <path> [--owner <id>] [--session <id>] [--by <path>]"
                  % cmd, file=sys.stderr)
            return 1
        path = args[0]
        if not os.path.isfile(path):
            print("no such handoff: %s" % path, file=sys.stderr)
            return 1
        try:
            if cmd == "claim":
                result = claim(path, owner, session)
            elif cmd == "release":
                result = release(path, owner)
            else:
                status = "resumed" if cmd == "resume" else "superseded"
                stamp = _mark(path, status, superseded_by, owner)
                result = {"ok": True, "status": status, "path": path, "stamp": stamp}
        except LedgerConflict as e:
            result = {"ok": False, "reason": str(e), "path": path}
        if as_json:
            print(json.dumps(result))
        elif result["ok"]:
            print("marked %s (%s): %s" % (result["status"], result.get("stamp") or result.get("claim_at") or "", path))
        else:
            print("refused: %s: %s" % (result["reason"], path), file=sys.stderr)
        return 0 if result["ok"] else 2

    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
