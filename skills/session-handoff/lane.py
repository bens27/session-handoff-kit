#!/usr/bin/env python3
"""Keep parallel worker lanes inside the files and doc sections they own.

The coordinator writes a manifest (default `<git-common-dir>/lanes.json`, shared
by every worktree and never committed):

    {"base": "<sha>", "target": "main", "order": ["D", "E"],
     "checks": ["python3 -m pytest -q"],
     "lanes": {"D": {"branch": "batch3/lane-d",
                     "paths": ["skills/session-handoff/hooks/context_watch.py"],
                     "sections": ["SPEC.md#5", "README.md#Parallel workers"]}}}

`paths` are exact files, directory prefixes or fnmatch globs. A section entry
`FILE#PREFIX` owns every heading whose title equals PREFIX or starts with PREFIX
followed by a non-alphanumeric character, and everything nested under it.

Commands (worker): start LANE, check LANE, split LANE, sync LANE, report LANE.
Command (coordinator): integrate.
Output: one JSON line. Exit 0 ok, 1 action needed, 2 usage error.
"""
import argparse
import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile


class Usage(Exception):
    pass


def emit(code, outcome, **fields):
    fields = dict(outcome=outcome, **fields)
    print(json.dumps(fields, sort_keys=True))
    return code


def git(*args, check=True):
    p = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.rstrip("\n") if check else p


def is_ancestor(a, b):
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def current_branch():
    return git("rev-parse", "--abbrev-ref", "HEAD")


def dirty():
    return bool(git("status", "--porcelain", "--untracked-files=no"))


# -- manifest ----------------------------------------------------------------
def load_manifest(path):
    if not path:
        path = os.path.join(git("rev-parse", "--path-format=absolute", "--git-common-dir"),
                            "lanes.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise Usage("cannot read manifest %s: %s" % (path, exc))
    if not isinstance(data.get("lanes"), dict) or not data.get("base"):
        raise Usage("manifest %s needs 'base' and 'lanes'" % path)
    data.setdefault("target", "main")
    data.setdefault("order", sorted(data["lanes"]))
    data.setdefault("checks", [])
    for spec in data["lanes"].values():
        spec.setdefault("paths", [])
        spec.setdefault("sections", [])
    return data


def get_lane(manifest, name):
    if name not in manifest["lanes"]:
        raise Usage("unknown lane %r (manifest has %s)" % (name, ", ".join(sorted(manifest["lanes"]))))
    return manifest["lanes"][name]


def require_on_branch(spec):
    branch = current_branch()
    if branch != spec["branch"]:
        return emit(1, "wrong-branch", branch=branch, expected=spec["branch"],
                    next="Run this in the lane's worktree, on branch %s." % spec["branch"])
    return None


# -- ownership -----------------------------------------------------------------
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def section_stacks(lines):
    """stacks[i] = heading titles enclosing line i (outermost first), counting
    line i itself when it is a heading. Fenced code blocks are skipped."""
    stack, stacks, fenced = [], [], False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        m = None if fenced else HEADING.match(line.rstrip("\n"))
        if m:
            level = len(m.group(1))
            stack = [(lvl, t) for lvl, t in stack if lvl < level] + [(level, m.group(2))]
        stacks.append(tuple(t for _, t in stack))
    return stacks


def prefix_matches(title, prefix):
    return title == prefix or (title.startswith(prefix) and not title[len(prefix)].isalnum())


def path_owner(manifest, path):
    for name in manifest["order"] + sorted(manifest["lanes"]):
        for pattern in manifest["lanes"][name]["paths"]:
            if (path == pattern or fnmatch.fnmatch(path, pattern)
                    or path.startswith(pattern.rstrip("/") + "/")):
                return name
    return None


def section_owner(manifest, path, stack):
    for name in manifest["order"] + sorted(manifest["lanes"]):
        for entry in manifest["lanes"][name]["sections"]:
            file_, _, prefix = entry.partition("#")
            if file_ == path and any(prefix_matches(t, prefix) for t in stack):
                return name
    return None


def merge_base(manifest):
    return git("merge-base", "HEAD", manifest["target"])


def read_blob(rev, path):
    p = git("show", "%s:%s" % (rev, path), check=False)
    return p.stdout.splitlines(True) if p.returncode == 0 else []


def read_disk(path):
    root = git("rev-parse", "--show-toplevel")
    try:
        with open(os.path.join(root, path), errors="replace") as f:
            return f.read().splitlines(True)
    except OSError:
        return []


def changed_files(since):
    out = git("diff", "--no-renames", "--name-only", since)
    return [line for line in out.splitlines() if line]


def hunks(manifest, since, path, rev=None):
    """Changes to one file since `since` (in `rev`, else the working tree), each
    tagged with the lane that owns it. Line ranges are 0-based half-open:
    base [i1, i2) became [j1, j2)."""
    old = read_blob(since, path)
    new = read_blob(rev, path) if rev else read_disk(path)
    owner = path_owner(manifest, path)
    if owner or not path.endswith(".md") or not old:
        return [dict(file=path, section=None, owner=owner, spans=False, whole=True)]
    stacks = section_stacks(old)
    result = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        # An insertion belongs to the section of the line before it.
        lines = range(i1, i2) if i2 > i1 else [i1 - 1] if i1 > 0 else []
        owned = [(section_owner(manifest, path, stacks[i]), stacks[i]) for i in lines] or [(None, ())]
        owners = []
        for o, _ in owned:
            if o not in owners:
                owners.append(o)
        titles = []
        for _, s in owned:
            if s and s[-1] not in titles:
                titles.append(s[-1])
        result.append(dict(file=path, section=" | ".join(titles) or None, owner=owners[0],
                           owners=owners, spans=len(owners) > 1, whole=False,
                           lines=[i1 + 1, i2], range=[i1, i2, j1, j2]))
    return result


def classify(manifest, name):
    since = merge_base(manifest)
    owned, not_owned = [], []
    for path in changed_files(since):
        for h in hunks(manifest, since, path):
            mine = h["owners"] == [name] if not h["whole"] else h["owner"] == name
            if not mine and h["spans"]:
                h["owner"] = next(o for o in h["owners"] if o != name)
            (owned if mine else not_owned).append(h)
    return since, owned, not_owned


def public(h):
    return {k: v for k, v in h.items() if k in ("file", "section", "owner", "spans", "lines")}


# -- commands ------------------------------------------------------------------
def cmd_start(manifest, name):
    spec = get_lane(manifest, name)
    refused = require_on_branch(spec)
    if refused is not None:
        return refused
    base = git("rev-parse", manifest["base"])
    head = git("rev-parse", "HEAD")
    if head == base:
        return emit(0, "at-base", base=base)
    if is_ancestor(base, head):
        return emit(0, "started", base=base, head=head)
    if is_ancestor(head, base):
        if dirty():
            return emit(1, "dirty", base=base, head=head,
                        next="Commit or stash local edits, then run start again.")
        git("reset", "-q", "--hard", base)
        return emit(0, "reset", base=base, previous=head)
    return emit(1, "diverged", base=base, head=head,
                next="This branch has commits that are not on base. Report it to the "
                     "coordinator instead of rebasing.")


def cmd_check(manifest, name):
    spec = get_lane(manifest, name)
    refused = require_on_branch(spec)
    if refused is not None:
        return refused
    since, owned, not_owned = classify(manifest, name)
    fields = dict(since=since, owned=[public(h) for h in owned],
                  not_owned=[public(h) for h in not_owned])
    if not not_owned:
        return emit(0, "clean", **fields)
    return emit(1, "not-owned", next="Commit, then run `lane.py split %s` to move these changes "
                "to side branches; owner null means no lane owns it." % name, **fields)


def slug(path):
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")


def exists_at(rev, path):
    return git("cat-file", "-e", "%s:%s" % (rev, path), check=False).returncode == 0


def write_or_delete(root, path, lines, keep):
    full = os.path.join(root, path)
    if not keep:
        if os.path.exists(full):
            os.remove(full)
        return
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w") as f:
        f.write("".join(lines))


def cmd_split(manifest, name):
    spec = get_lane(manifest, name)
    refused = require_on_branch(spec)
    if refused is not None:
        return refused
    if dirty():
        return emit(1, "dirty", next="Commit your work first; split only moves committed changes.")
    since, _, not_owned = classify(manifest, name)
    if not not_owned:
        return emit(0, "nothing-to-split", since=since)
    spanning = [public(h) for h in not_owned if h["spans"]]
    if spanning:
        return emit(1, "spans-sections", hunks=spanning,
                    next="Each listed change crosses a section boundary. Split it by hand into "
                         "per-section edits, or report it to the coordinator.")
    by_file = {}
    for h in not_owned:
        by_file.setdefault(h["file"], []).append(h)
    sides = {path: "%s-shared-%s" % (spec["branch"], slug(path)) for path in by_file}
    taken = [b for b in sides.values() if git("rev-parse", "--verify", "-q", "refs/heads/" + b,
                                              check=False).returncode == 0]
    if taken:
        return emit(1, "side-exists", branches=taken,
                    next="Ask the coordinator to integrate or delete these side branches first.")
    root = git("rev-parse", "--show-toplevel")
    created = []
    for path, hs in sorted(by_file.items()):
        old, new = read_blob(since, path), read_disk(path)
        in_base, on_disk = exists_at(since, path), os.path.exists(os.path.join(root, path))
        if hs[0]["whole"]:
            side_lines, side_keep, lane_lines, lane_keep = new, on_disk, old, in_base
        else:
            side_lines, lane_lines = list(old), list(new)
            for h in sorted(hs, key=lambda h: h["range"][0], reverse=True):
                i1, i2, j1, j2 = h["range"]
                side_lines[i1:i2] = new[j1:j2]
                lane_lines[j1:j2] = old[i1:i2]
            side_keep = lane_keep = True
        tmp = tempfile.mkdtemp(prefix="lane-split-")
        os.rmdir(tmp)
        git("worktree", "add", "-q", "-b", sides[path], tmp, since)
        try:
            write_or_delete(tmp, path, side_lines, side_keep)
            git("-C", tmp, "add", "-A", "--", path)
            git("-C", tmp, "commit", "-q", "-m", "lane %s: shared change to %s" % (name, path))
            created.append(dict(branch=sides[path], file=path, sha=git("-C", tmp, "rev-parse", "HEAD"),
                                sections=sorted({h["section"] for h in hs if h["section"]})))
        finally:
            git("worktree", "remove", "--force", tmp, check=False)
        write_or_delete(root, path, lane_lines, lane_keep)
        git("add", "-A", "--", path)
    git("commit", "-q", "-m", "lane %s: move shared changes to side branches\n\n%s"
        % (name, "\n".join("- %s -> %s" % (c["file"], c["branch"]) for c in created)))
    return emit(0, "split", since=since, head=git("rev-parse", "HEAD"), side_branches=created)


def conflicts(manifest):
    """Conflict blocks of the merge in progress, each with its section and owning lane."""
    found = []
    for path in git("diff", "--name-only", "--diff-filter=U").splitlines():
        owner = path_owner(manifest, path)
        if owner or not path.endswith(".md"):
            found.append(dict(file=path, section=None, owner=owner, owners=[owner]))
            continue
        lines = read_disk(path)
        stacks = section_stacks(lines)
        start = None
        for i, line in enumerate(lines):
            if line.startswith("<<<<<<<"):
                start = i
            elif line.startswith(">>>>>>>") and start is not None:
                span = range(max(start - 1, 0), i + 1)
                owners, titles = [], []
                for k in span:
                    o = section_owner(manifest, path, stacks[k])
                    if o not in owners:
                        owners.append(o)
                    if stacks[k] and stacks[k][-1] not in titles:
                        titles.append(stacks[k][-1])
                found.append(dict(file=path, section=" | ".join(titles) or None,
                                  owner=owners[0], owners=owners, lines=[start + 1, i + 1]))
                start = None
    return found


def cmd_sync(manifest, name):
    spec = get_lane(manifest, name)
    refused = require_on_branch(spec)
    if refused is not None:
        return refused
    if dirty():
        return emit(1, "dirty", next="Commit your work first, then sync.")
    target = manifest["target"]
    before = git("rev-parse", "HEAD")
    if git("merge", "--no-edit", "-q", target, check=False).returncode == 0:
        head = git("rev-parse", "HEAD")
        return emit(0, "merged" if head != before else "up-to-date", target=target, head=head)
    found = conflicts(manifest)
    if not found:
        git("merge", "--abort", check=False)
        return emit(1, "merge-failed", target=target, next="git merge failed without conflicts; "
                    "report it to the coordinator.")
    shown = [{k: c[k] for k in ("file", "section", "owner")} for c in found]
    foreign = [c for c in found if c["owners"] != [name]]
    if foreign:
        git("merge", "--abort")
        return emit(1, "aborted", target=target, conflicts=shown, head=git("rev-parse", "HEAD"),
                    next="Merging %s conflicts outside this lane; the merge was aborted. Keep working "
                         "from your starting point and list these in `lane.py report`." % target)
    return emit(1, "conflicts", target=target, conflicts=shown,
                next="Every conflict is in a section this lane owns: resolve them, then commit.")


def side_branches(branch):
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/heads/%s-shared-*" % branch)
    return [r for r in refs.splitlines() if r]


def describe_side(manifest, side):
    since = git("merge-base", side, manifest["base"])
    changes = []
    for path in git("diff", "--no-renames", "--name-only", since, side).splitlines():
        sections = [h["section"] for h in hunks(manifest, since, path, rev=side) if h["section"]]
        for item in (["%s#%s" % (path, s) for s in dict.fromkeys(sections)] or [path]):
            changes.append(item)
    return changes


def cmd_report(manifest, name):
    spec = get_lane(manifest, name)
    base = git("rev-parse", manifest["base"])
    branch = spec["branch"]
    start = git("merge-base", branch, base)
    sides = [dict(name=s, sha=git("rev-parse", s), changes=describe_side(manifest, s))
             for s in side_branches(branch)]
    shared = ", ".join("%s: %s" % (s["name"], ", ".join(s["changes"])) for s in sides) or "none"
    line = "Starting point: %s (expected %s); shared-file branches: %s" % (start, base, shared)
    fields = dict(line=line, branch=dict(name=branch, sha=git("rev-parse", branch)),
                  side_branches=sides)
    if start != base:
        return emit(1, "wrong-start", next="Paste `line` into your report; the coordinator must "
                    "rebase or re-check this lane.", **fields)
    return emit(0, "report", next="Paste `line` into your completion report.", **fields)


def cmd_integrate(manifest):
    target = manifest["target"]
    if current_branch() != target:
        return emit(1, "wrong-branch", branch=current_branch(), expected=target,
                    next="Run integrate from the %s checkout." % target)
    if dirty():
        return emit(1, "dirty", next="Commit or stash local edits before integrating.")
    lanes = [manifest["lanes"][n]["branch"] for n in manifest["order"]]
    queue = lanes + [s for b in lanes for s in side_branches(b)]
    merged, skipped = [], []
    for branch in queue:
        if is_ancestor(branch, "HEAD"):
            skipped.append(branch)
            continue
        if git("merge", "--no-edit", "-q", branch, check=False).returncode:
            shown = [{k: c[k] for k in ("file", "section", "owner")} for c in conflicts(manifest)]
            return emit(1, "conflict", branch=branch, merged=merged, skipped=skipped,
                        conflicts=shown,
                        next="Resolve these sections, commit the merge, then run integrate again; "
                             "merged branches are skipped.")
        merged.append(branch)
    root = git("rev-parse", "--show-toplevel")
    results = []
    for cmd in manifest["checks"]:
        p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True)
        if p.returncode:
            results.append(dict(cmd=cmd, ok=False, exit=p.returncode))
            return emit(1, "check-failed", merged=merged, skipped=skipped, checks=results,
                        output=(p.stdout + p.stderr)[-2000:],
                        next="Fix the failure on %s, then run integrate again." % target)
        results.append(dict(cmd=cmd, ok=True))
    return emit(0, "integrated", merged=merged, skipped=skipped, checks=results,
                head=git("rev-parse", "HEAD"))


COMMANDS = {"start": cmd_start, "check": cmd_check, "split": cmd_split, "sync": cmd_sync,
            "report": cmd_report}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def main(argv=None):
    parser = Parser(prog="lane.py", description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", help="manifest path (default <git-common-dir>/lanes.json)")
    parser.add_argument("command", choices=sorted(COMMANDS) + ["integrate"])
    parser.add_argument("lane", nargs="?")
    try:
        args = parser.parse_args(argv)
        manifest = load_manifest(args.manifest)
        if args.command == "integrate":
            return cmd_integrate(manifest)
        if not args.lane:
            raise Usage("%s needs a lane name" % args.command)
        return COMMANDS[args.command](manifest, args.lane)
    except Usage as exc:
        return emit(2, "usage", error=str(exc))
    except RuntimeError as exc:
        return emit(1, "git-error", error=str(exc))


if __name__ == "__main__":
    sys.exit(main())
