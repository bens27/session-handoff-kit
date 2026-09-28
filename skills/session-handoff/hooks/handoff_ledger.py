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
  resolve <topic-or-path> [dir] [--json]   compact authoritative metadata;
                                            use history for older records
  new-path <topic> [dir] [--json]          print deterministic path data for a new handoff
                                           (--json adds the handoff template)
  claim <path> [--owner X]                 stamp a handoff as being resumed now, so a
                                            parallel session start does not announce it;
                                            refuses a live claim held by another owner
  release <path> [--owner X]               drop a claim without resuming
  resume <path> [--owner X]                legacy direct transfer; all callers
                                            respect a live claim's owner
  supersede <path> [--by <new-path>]       mark a handoff replaced by a newer one
  abandon <path>                           mark a handoff dropped (never to be resumed)
  save-path [dir]                          print where new handoffs should be saved

Stdlib only. Also importable: scan(root, max_age_days, stats=None),
project_root(start), mark_resumed(path), mark_superseded(path).
"""

import json
import fcntl
from contextlib import contextmanager
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

# 20260811-1902-pantry-cli(.md) -> ended 2026-08-11T19:02, topic pantry-cli
DATED_NAME = re.compile(r"^(\d{8})-(\d{4}|\d{6})-(.+)$")
STAMP = "%Y-%m-%dT%H:%M"
CLAIM_TTL = timedelta(hours=2)  # a claim older than this is a crashed session
KNOWN_STATUSES = ("open", "resumed", "superseded", "abandoned")
REQUIRED_SECTIONS = ("Objective", "Current state", "Next steps")
MAX_WORDS = 1500
CLOSED_STATUSES = ("resumed", "superseded", "abandoned", "unpublished")
MAX_REFS = 8              # references a resume is asked to read, at most
MAX_REF_BYTES = 256_000   # combined size of the existing referenced files


class ConflictError(RuntimeError):
    """The file changed between read and write, or a claim is held by another owner."""


@contextmanager
def locked(path):
    """Stable sidecar lock: rename never changes the inode being locked."""
    with open(str(path) + ".lock", "a") as f:
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConflictError("busy; retry after the current operation finishes")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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


def _handoff_paths(root, errors=None):
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
        try:
            names = sorted(os.listdir(hdir))
        except OSError as exc:
            if errors is not None:
                errors.append({"path": hdir, "error": type(exc).__name__})
            continue
        for name in names:
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


def _problems(text, fm):
    """Front-matter defects worth surfacing instead of silently defaulting."""
    out = []
    lines = text.splitlines()
    if lines and lines[0].strip() == "---" and not fm:
        out.append("front matter has no closing --- fence")
    status = (fm.get("status") or "open").lower()[:240]
    if status not in KNOWN_STATUSES:
        out.append("unknown status %r (treated as open)" % status)
    created = (fm.get("created") or "")[:240]
    if created:
        try:
            datetime.fromisoformat(created)
        except ValueError:
            out.append("created %r is not ISO 8601" % created)
    body = text.split("\n---\n", 1)[1] if fm and "\n---\n" in text else ""
    if body.strip():
        for section in REQUIRED_SECTIONS:
            if not re.search(r"^## %s\s*$" % re.escape(section), body, re.M):
                out.append("missing section '## %s'" % section)
        words = len(body.split())
        if words > MAX_WORDS:
            out.append("body is %d words (limit %d)" % (words, MAX_WORDS))
    return out


def _is_open(fm):
    return (fm.get("status") or "open").lower() not in CLOSED_STATUSES


def _split_csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


def content_fingerprint(text):
    import hashlib
    fm = parse_front_matter(text)
    lines = text.splitlines()
    if fm:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
        volatile = ('claimed:', 'claim_owner:', 'status:', 'resumed:', 'resumed_by:', 'superseded:', 'superseded_by:', 'abandoned:')
        lines = [l for l in lines[1:end] if not l.startswith(volatile)] + lines[end + 1:]
    return hashlib.sha256('\n'.join(lines).encode()).hexdigest()


def publication_valid(path, text, fm):
    try:
        with open(path + '.published') as f:
            receipt = json.loads(f.read(4096))
        return (receipt.get('checkpoint_id') == fm.get('checkpoint_id')
                and receipt.get('fingerprint') == content_fingerprint(text))
    except (OSError, ValueError):
        return False


def _records(root, errors=None):
    """Every handoff belonging to root: {path, fm, text, mtime, topic, ended}.
    Fallback-dir files that name a different `project:` belong to another
    project sharing the same basename and are skipped."""
    paths, legacy = _handoff_paths(root, errors)
    out = []
    for path, in_fallback in paths:
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(64_001)
            fm = parse_front_matter(text)
            if in_fallback and fm.get("project") and not _same_dir(fm["project"], root):
                continue
            if fm.get('checkpoint_id') and not publication_valid(path, text, fm):
                fm['status'] = 'unpublished'
            name_ended, name_topic = _name_parts(path)
            topic = fm.get("topic") or name_topic
            if _is_legacy_single_file(path, legacy) and "topic" not in fm:
                topic = "default"
            out.append({
                "path": path, "fm": fm, "text": text, "mtime": mtime, "topic": topic,
                "legacy": _is_legacy_single_file(path, legacy),
                "problems": _problems(text, fm) + (["document exceeds bounded read limit"] if len(text) > 64_000 else []),
                "ended": (fm.get("created") if fm.get("checkpoint_id") else name_ended or fm.get("created")
                          or datetime.fromtimestamp(mtime).strftime(STAMP)),
            })
        except OSError as exc:
            if errors is not None:
                errors.append({"path": path, "error": type(exc).__name__})
    replaced = {os.path.realpath(r["fm"]["predecessor"]) for r in out
                if r["fm"].get("checkpoint_id") and r["fm"].get("predecessor") and not r["problems"]}
    for r in out:
        if os.path.realpath(r["path"]) in replaced:
            r["fm"]["status"] = "superseded"
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
    errors = []
    records = _records(root, errors)
    latest = {}
    duplicates = 0
    for r in sorted(records, key=lambda r: (r["ended"], r["path"])):
        if r["legacy"] and not r["fm"] and "handoff" not in "\n".join(r["text"].splitlines()[:5]).lower():
            continue
        if r["topic"] in latest and _is_open(latest[r["topic"]]["fm"]) and _is_open(r["fm"]):
            duplicates += 1
        latest[r["topic"]] = r
    found, stale, claimed, incomplete = [], 0, 0, 0
    for r in latest.values():
        fm = r["fm"]
        if fm.get('status') == 'unpublished':
            incomplete += 1
            continue
        if not _is_open(fm):
            continue
        if _claimed_recently(fm, datetime.now()):
            claimed += 1
            continue
        age = (now - r["mtime"]) / 86400
        if age > max_age_days:
            stale += 1
            continue
        found.append(dict(path=r["path"], topic=r["topic"], ended=r["ended"][:64],
                          description=(fm.get("description") or "")[:240],
                          skills=(fm.get("skills") or "")[:240], git=(fm.get("git") or "")[:240],
                          age_days=round(age, 1), problems=r["problems"][:8]))
    found.sort(key=lambda h: (h["ended"], h["path"]), reverse=True)
    if stats is not None:
        stats.update(stale=stale, claimed=claimed, incomplete=incomplete, duplicates=duplicates, errors=errors)
    return found


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
        "description": (r["fm"].get("description") or "")[:240],
        "references": _split_csv(r["fm"].get("references") or ""),
        "problems": r["problems"],
    } for r in _records(root) if r["topic"] == topic]
    chain.sort(key=lambda h: (h["ended"], h["path"]))

    # Only the authoritative (newest) handoff's references are current: earlier
    # handoffs' references were either carried forward or deliberately dropped.
    # Capped so a runaway references line cannot flood a resuming session.
    refs = list(dict.fromkeys(chain[-1]["references"])) if chain else []
    must_also_read, missing, unresolved, total = [], [], [], 0
    for ref in refs:
        full = os.path.join(root, os.path.expanduser(ref))
        if len(must_also_read) >= MAX_REFS:
            unresolved.append({"ref": ref, "reason": "over the %d-reference cap" % MAX_REFS})
            continue
        if not os.path.exists(full):
            missing.append(ref)
        else:
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            if total + size > MAX_REF_BYTES:
                unresolved.append({"ref": ref, "reason": "over the %d-byte cap" % MAX_REF_BYTES})
                continue
            total += size
        must_also_read.append(ref)
    return {
        "topic": topic,
        "chain": chain,
        "authoritative": chain[-1]["path"] if chain else None,
        "must_also_read": must_also_read,
        "missing_references": missing,
        "unresolved_references": unresolved,
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


def session_note_path(root, session=None):
    """Where context_watch.py leaves this project's live session facts
    (session id, transcript, whether the trigger fired), so ledger commands
    run from the agent's shell can read them without hook stdin."""
    import hashlib
    import tempfile
    key = hashlib.sha1((os.path.realpath(project_root(root)) + "\0" + (session or "unidentified")).encode("utf-8", "replace")).hexdigest()[:20]
    return os.path.join(tempfile.gettempdir(), "context-watch-session-%s.json" % key)


def read_session_note(root, max_age_s=86400, session=None):
    if not session:
        return {}
    try:
        with open(session_note_path(root, session), encoding="utf-8") as f:
            note = json.load(f)
        if time.time() - float(note.get("ts") or 0) <= max_age_s:
            return note
    except Exception:
        pass
    return {}


def transcript_skills(path):
    """Skill names this Claude session loaded, in first-use order, read from
    the Skill tool calls in its transcript."""
    seen = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:  # ponytail: full scan; tail-only if transcripts get huge
                if '"Skill"' not in line:
                    continue
                try:
                    msg = json.loads(line).get("message") or {}
                except ValueError:
                    continue
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") == "Skill":
                        name = str((block.get("input") or {}).get("skill") or "").strip()
                        if name and name not in seen:
                            seen.append(name)
    except OSError:
        pass
    return seen


def supersede_candidates(root, git_position, topic):
    """Legacy API: automatic predecessor inference has been retired."""
    return []  # lineage is explicit; sharing a branch never authorizes replacement.


def read_template():
    """handoff-template.md from the skill folder these hooks live in, so the
    agent never has to read a file outside its working directory. "" when
    absent: the agent reads the file itself."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "handoff-template.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def new_path(topic, root):
    now = datetime.now()
    abs_root = project_root(root)
    directory = save_path(abs_root)
    note = read_session_note(abs_root)
    git_position = _git_position(abs_root)
    stem = "%s-%s" % (now.strftime("%Y%m%d-%H%M"), topic)
    filename, n = stem + ".md", 2
    while os.path.exists(os.path.join(directory, filename)):
        filename, n = "%s-%d.md" % (stem, n), n + 1  # topic: front matter keeps the topic
    return {
        "directory": directory,
        "filename": filename,
        "path": os.path.join(directory, filename),
        "created": now.strftime(STAMP),
        "project": abs_root,
        "git": git_position,
        "reason": "context-pressure" if note.get("fired") else "user-parked",
        "skills": "",  # execution dependencies are explicit, not all past Skill calls
        "supersedes": supersede_candidates(abs_root, git_position, topic),
    }


def _stat_key(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def _atomic_write(path, text, expect):
    """Write text via a temp file + fsync + rename, refusing (ConflictError)
    when the file's (mtime_ns, size) is no longer `expect`."""
    directory = os.path.dirname(os.path.abspath(path))
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if _stat_key(path) != expect:
            raise ConflictError("%s changed while it was being updated; retry" % path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _set_fields(path, fields, drop):
    """Rewrite front matter: remove lines whose key starts with any of drop,
    then append fields in order. Adds a header to files without one."""
    before = _stat_key(path)
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
    _atomic_write(path, new_text, before)


def _mark_unlocked(path, status, superseded_by=None):
    """Flip a handoff's status and stamp the time. Idempotent."""
    stamp = datetime.now().strftime(STAMP)
    fields = [("status", status), (status, stamp)]
    drop = ("status:", "resumed:", "superseded:", "abandoned:", "claimed:", "claim_owner:")
    if superseded_by is not None:
        drop += ("superseded_by:",)
        fields.append(("superseded_by", superseded_by))
    _set_fields(path, fields, drop)
    return stamp


def _mark(path, status, superseded_by=None, owner=None):
    with locked(path):
        _check_owner(path, owner)
        return _mark_unlocked(path, status, superseded_by)


def _check_owner(path, owner):
    """Raise ConflictError when a live claim is held by an owner other than
    `owner` (an owner-less caller counts as different). Unowned or expired
    claims never block."""
    fm = _read_fm(path)
    holder = fm.get("claim_owner")
    if holder and holder != owner and _claimed_recently(fm, datetime.now()):
        raise ConflictError("%s is claimed by %s since %s" % (path, holder, fm.get("claimed")))


def claim(path, owner=None):
    with locked(path):
        _check_owner(path, owner)
        stamp = datetime.now().strftime(STAMP)
        fields = [("claimed", stamp)] + ([("claim_owner", owner)] if owner else [])
        _set_fields(path, fields, ("claimed:", "claim_owner:"))
        return stamp


def claim_report(path):
    """Lines a resuming session needs after claiming: template problems, the
    handoff's `verify:` command, and how far the repository moved since `git:`."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    fm = parse_front_matter(text)
    out = ["problem: %s" % p for p in _problems(text, fm)]
    if fm.get("verify"):
        out.append("verify: %s" % fm["verify"])
    sha = fm.get("git", "").rpartition("@")[2]
    root = fm.get("project") or project_root(os.path.dirname(path))
    if sha and os.path.isdir(root):
        def git(*args):
            return subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                                  text=True, timeout=5).stdout
        try:
            out.append("commits_since: %s" % git("rev-list", "--count", "%s..HEAD" % sha).strip())
            out.append("dirty: %d" % len(git("status", "--short").splitlines()))
        except Exception:
            pass
    return out


def release(path, owner=None):
    with locked(path):
        _check_owner(path, owner)
        _set_fields(path, [], ("claimed:", "claim_owner:"))


def mark_resumed(path):
    return _mark(path, "resumed")


def mark_superseded(path):
    return _mark(path, "superseded")


def _cli(argv):
    if not argv or argv[0] in ('--help', '-h'):
        print('Preferred workflow: lookup | save | prepare | verify | acknowledge | history.\nRun a command with --help for arguments. Legacy interfaces below:')
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd in ("lookup", "save", "prepare", "verify", "acknowledge", "history"):
        from handoff_protocol import cli
        return cli(argv)
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
        if stats.get('errors'):
            print('lookup failed: some handoff locations are unreadable; run lookup for recovery details.', file=sys.stderr)
            return 1
        if not handoffs and not as_json:
            from handoff_protocol import lookup
            print(lookup(root, max_age)['action'])
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
        resolved['history_count'] = len(resolved['chain'])
        resolved['chain'] = resolved['chain'][-1:]
        if len(json.dumps(resolved).encode()) > 16000:
            print(json.dumps(dict(outcome='needs-context', authoritative=resolved['authoritative'],
                  action='Resolution metadata exceeds 16000 bytes. Inspect the named checkpoint metadata locally; reduce required references before preparation. History is available separately.')))
            return 1
        if as_json:
            print(json.dumps(resolved, indent=2))
        else:
            for h in resolved["chain"]:
                print("%s\t%s\t%s\t%s" % (h["ended"], h["status"], h["path"],
                                          h["description"]))
                for problem in h["problems"]:
                    print("  problem: %s" % problem)
            print("authoritative: %s" % resolved["authoritative"])
            if resolved["must_also_read"]:
                print("must_also_read: %s" % ", ".join(resolved["must_also_read"]))
            if resolved["missing_references"]:
                print("missing_references: %s" % ", ".join(resolved["missing_references"]))
            for u in resolved["unresolved_references"]:
                print("unresolved_reference: %s (%s)" % (u["ref"], u["reason"]))
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
            print(json.dumps(dict(result, template=read_template()), indent=2))
        else:
            for key in ("directory", "filename", "path", "created", "project", "git",
                        "reason", "skills"):
                print("%s: %s" % (key, result[key]))
            if result["supersedes"]:
                print("supersedes: %s" % ", ".join(result["supersedes"]))
        return 0
    if cmd in ("resume", "supersede", "abandon", "claim", "release"):
        owner = None
        if "--owner" in args:
            i = args.index("--owner")
            if i + 1 >= len(args):
                print("usage: handoff_ledger.py %s <path> [--owner X]" % cmd, file=sys.stderr)
                return 1
            owner, args = args[i + 1], args[:i] + args[i + 2:]
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
        for p in (path, superseded_by):
            if p is not None and not os.path.isfile(p):
                print("no such handoff: %s" % p, file=sys.stderr)
                return 1
        try:
            if cmd == "claim":
                print("claimed (%s): %s" % (claim(path, owner), path))
                for line in claim_report(path):
                    print(line)
                return 0
            if cmd == "release":
                release(path, owner)
                print("released: %s" % path)
                return 0
            if owner is not None:
                _check_owner(path, owner)
            status = {"resume": "resumed", "supersede": "superseded",
                      "abandon": "abandoned"}[cmd]
            stamp = _mark(path, status, superseded_by, owner)
        except ConflictError as e:
            print("refused: %s" % e, file=sys.stderr)
            return 1
        print("marked %s (%s): %s" % (status, stamp, path))
        return 0
    print("unknown subcommand: %s" % cmd, file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(_cli(sys.argv[1:]))
    except OSError as exc:
        print("blocked: %s. Keep the checkpoint; report the failed location and restore access before retrying. Do not adopt or clear the session." % exc, file=sys.stderr)
        sys.exit(1)
