#!/usr/bin/env python3
"""Executed verification for the ledger fix lane. Run from repo root (worktree)."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = os.getcwd()
PLUGIN = os.path.join(REPO, "skills/session-handoff/hooks/handoff_ledger.py")
PY = sys.executable

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(args, cwd, env=None):
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update(env)
    return subprocess.run([PY, PLUGIN] + args, cwd=cwd, capture_output=True, text=True,
                          timeout=30, env=child_env)


def main():
    for path in (PLUGIN,):
        try:
            ast.parse(open(path).read())
            check("parse:" + os.path.relpath(path, REPO), True)
        except Exception as e:
            check("parse:" + os.path.relpath(path, REPO), False, str(e))

    tmp = tempfile.mkdtemp(prefix="ledger-verify-")
    hd = os.path.join(tmp, ".handoffs")
    os.makedirs(hd)

    # 1. Well-formed lifecycle: open -> listed; resume -> not listed; idempotent
    good = os.path.join(hd, "good-topic.md")
    with open(good, "w") as f:
        f.write("---\ntopic: good-topic\ncreated: 2026-08-11T09:00\nstatus: open\n---\n# Session Handoff — good-topic\n")
    p = run(["list", tmp, "--json"], tmp)
    listed = [h.get("topic") for h in json.loads(p.stdout or "[]")]
    check("open-listed", "good-topic" in listed, "stdout=%r" % p.stdout[:200])
    p = run(["resume", good], tmp)
    check("resume-exit0", p.returncode == 0, "rc=%d stderr=%r" % (p.returncode, p.stderr[:200]))
    text1 = open(good).read()
    check("resume-stamps", "status: resumed" in text1 and "resumed:" in text1, "content=%r" % text1[:200])
    p = run(["list", tmp, "--json"], tmp)
    check("resumed-not-listed", "good-topic" not in [h.get("topic") for h in json.loads(p.stdout or "[]")])
    run(["resume", good], tmp)
    text2 = open(good).read()
    check("resume-idempotent", text2.count("status:") == 1 and text2.count("resumed:") == 1,
          "content=%r" % text2[:300])

    # 2. Malformed front matter: opening fence, NO closing fence.
    #    resume must never claim success while leaving the file unchanged.
    bad = os.path.join(hd, "bad-topic.md")
    bad_body = "---\ntopic: bad-topic\nstatus: open\n# Body starts here, no closing fence\nstatus: resumed\n"
    with open(bad, "w") as f:
        f.write(bad_body)
    p = run(["resume", bad], tmp)
    after = open(bad).read()
    silently_lied = (p.returncode == 0 and after == bad_body)
    check("resume-no-silent-noop", not silently_lied,
          "rc=0 and file unchanged: CLI claimed success without modifying the file")
    if p.returncode == 0:
        check("resume-malformed-actually-marked", "resumed" in after and after != bad_body,
              "rc=0 but no resumed stamp; content=%r" % after[:200])

    # 3. Body text must not populate front matter: an unfenced file whose BODY
    #    contains 'status: resumed' must still scan as open (or at minimum not
    #    be silently treated as resumed).
    tmp2 = tempfile.mkdtemp(prefix="ledger-verify2-")
    hd2 = os.path.join(tmp2, ".handoffs")
    os.makedirs(hd2)
    tricky = os.path.join(hd2, "tricky-topic.md")
    with open(tricky, "w") as f:
        f.write("---\ntopic: tricky-topic\nstatus: open\n# Notes\nHow to mark done, quoted from the docs:\nstatus: resumed\n")
    p = run(["list", tmp2, "--json"], tmp2)
    topics = [h.get("topic") for h in json.loads(p.stdout or "[]")]
    check("body-not-front-matter", "tricky-topic" in topics,
          "unfenced body absorbed into front matter suppressed an open handoff; listed=%r" % topics)

    # 4. Dated handoffs sort newest-first, surface descriptions, derive missing
    #    topic from filename, and supersede is idempotent.
    tmp3 = tempfile.mkdtemp(prefix="ledger-verify3-")
    hd3 = os.path.join(tmp3, ".handoffs")
    os.makedirs(hd3)
    alpha = os.path.join(hd3, "20260810-0900-alpha.md")
    beta = os.path.join(hd3, "20260811-1500-beta.md")
    with open(alpha, "w") as f:
        f.write("---\nstatus: open\ndescription: older alpha work\nskills: alpha-skill, beta-skill\n---\n# Session Handoff — alpha\n")
    with open(beta, "w") as f:
        f.write("---\ntopic: beta\nstatus: open\ndescription: newer beta work\n---\n# Session Handoff — beta\n")
    p = run(["list", tmp3, "--json"], tmp3)
    try:
        handoffs = json.loads(p.stdout or "[]")
    except Exception:
        handoffs = []
    check("dated-order-newest-first",
          [h.get("topic") for h in handoffs] == ["beta", "alpha"],
          "stdout=%r" % p.stdout[:500])
    by_topic = dict((h.get("topic"), h) for h in handoffs)
    check("dated-alpha-topic-derived", by_topic.get("alpha", {}).get("topic") == "alpha",
          "handoffs=%r" % handoffs)
    check("dated-ended-from-filename",
          by_topic.get("alpha", {}).get("ended") == "2026-08-10T09:00"
          and by_topic.get("beta", {}).get("ended") == "2026-08-11T15:00",
          "handoffs=%r" % handoffs)
    check("dated-descriptions-surfaced",
          by_topic.get("alpha", {}).get("description") == "older alpha work"
          and by_topic.get("beta", {}).get("description") == "newer beta work",
          "handoffs=%r" % handoffs)
    check("dated-skills-surfaced",
          by_topic.get("alpha", {}).get("skills") == "alpha-skill, beta-skill"
          and by_topic.get("beta", {}).get("skills") == "",
          "handoffs=%r" % handoffs)

    p = run(["supersede", alpha], tmp3)
    check("supersede-exit0", p.returncode == 0,
          "rc=%d stderr=%r" % (p.returncode, p.stderr[:200]))
    alpha_text = open(alpha).read()
    check("supersede-stamps", "status: superseded" in alpha_text and "superseded:" in alpha_text,
          "content=%r" % alpha_text[:300])
    p = run(["list", tmp3, "--json"], tmp3)
    remaining = [h.get("topic") for h in json.loads(p.stdout or "[]")]
    check("superseded-not-listed", "alpha" not in remaining and "beta" in remaining,
          "listed=%r" % remaining)
    run(["supersede", alpha], tmp3)
    alpha_text2 = open(alpha).read()
    status_lines = [l for l in alpha_text2.splitlines()
                    if l.strip().lower().startswith("status:")]
    check("supersede-idempotent", len(status_lines) == 1,
          "content=%r" % alpha_text2[:300])

    # 5. resolve: reproduces the pdf-mcp-mvp-build retrospective's exact
    #    scenario — a NEWER handoff declares `references:` pointing at an
    #    OLDER, already-superseded handoff that holds the real specs.
    #    resolve must surface both, oldest-first, and name the newer one
    #    authoritative.
    tmp4 = tempfile.mkdtemp(prefix="ledger-verify4-")
    hd4 = os.path.join(tmp4, ".handoffs")
    os.makedirs(hd4)
    older = os.path.join(hd4, "20260812-1530-pdf-mcp-mvp-build.md")
    newer = os.path.join(hd4, "20260813-0002-pdf-mcp-mvp-build.md")
    with open(older, "w") as f:
        f.write("---\ntopic: pdf-mcp-mvp-build\nstatus: superseded\n"
                 "description: original wave 2-5 specs\n---\n# older\n")
    with open(newer, "w") as f:
        f.write("---\ntopic: pdf-mcp-mvp-build\nstatus: open\n"
                 "description: current handoff\n"
                 "references: .handoffs/20260812-1530-pdf-mcp-mvp-build.md\n---\n# newer\n")
    p = run(["resolve", "pdf-mcp-mvp-build", tmp4, "--json"], tmp4)
    check("resolve-exit0", p.returncode == 0, "rc=%d stderr=%r" % (p.returncode, p.stderr[:300]))
    try:
        result = json.loads(p.stdout)
    except Exception as e:
        result = {}
        check("resolve-json-parses", False, "%s; stdout=%r" % (e, p.stdout[:300]))
    else:
        check("resolve-json-parses", True)
    chain_paths = [os.path.basename(h.get("path", "")) for h in result.get("chain", [])]
    check("resolve-default-authoritative-only",
          chain_paths == ["20260813-0002-pdf-mcp-mvp-build.md"] and result.get("history_count") == 2,
          "chain=%r" % chain_paths)
    check("resolve-authoritative-is-newest",
          os.path.basename(result.get("authoritative", "")) == "20260813-0002-pdf-mcp-mvp-build.md",
          "authoritative=%r" % result.get("authoritative"))
    must_read = [os.path.basename(p2) for p2 in result.get("must_also_read", [])]
    check("resolve-surfaces-references",
          "20260812-1530-pdf-mcp-mvp-build.md" in must_read,
          "must_also_read=%r" % must_read)
    p = run(["resolve", "no-such-topic", tmp4], tmp4)
    check("resolve-unknown-topic-fails", p.returncode != 0,
          "rc=%d (expected nonzero)" % p.returncode)

    # 6. save-path: deterministic write-location lookup — an existing
    #    .handoffs/ (even empty) wins; otherwise fall back to the generic
    #    ~/.claude/handoffs/<project-name>/ path.
    tmp5 = tempfile.mkdtemp(prefix="ledger-verify5-with-")
    os.makedirs(os.path.join(tmp5, ".handoffs"))
    with open(os.path.join(tmp5, ".handoffs", "20260101-0000-x.md"), "w") as f:
        f.write("---\nstatus: open\n---\n# x\n")
    p = run(["save-path", tmp5], tmp5)
    check("save-path-with-convention-returns-local",
          p.returncode == 0 and p.stdout.strip() == os.path.join(tmp5, ".handoffs"),
          "rc=%d stdout=%r" % (p.returncode, p.stdout.strip()))

    tmp6 = tempfile.mkdtemp(prefix="ledger-verify6-without-myproj-")
    p = run(["save-path", tmp6], tmp6)
    expected_fallback = os.path.expanduser(
        os.path.join("~/.claude/handoffs", os.path.basename(tmp6)))
    check("save-path-without-convention-falls-back",
          p.returncode == 0 and p.stdout.strip() == expected_fallback,
          "rc=%d stdout=%r want=%r" % (p.returncode, p.stdout.strip(), expected_fallback))

    tmp7 = tempfile.mkdtemp(prefix="ledger-verify7-empty-")
    os.makedirs(os.path.join(tmp7, ".handoffs"))
    p = run(["save-path", tmp7], tmp7)
    check("save-path-empty-handoffs-dir-still-wins",
          p.stdout.strip() == os.path.join(tmp7, ".handoffs"),
          "stdout=%r" % p.stdout.strip())

    # 7. new-path: deterministic path metadata for a new handoff, using one
    #    timestamp for both filename and created front matter.
    tmp9 = tempfile.mkdtemp(prefix="ledger-verify9-new-path-")
    os.makedirs(os.path.join(tmp9, ".handoffs"))
    p = run(["new-path", "fresh-topic", tmp9], tmp9)
    p_save = run(["save-path", tmp9], tmp9)
    fields = {}
    lines = p.stdout.strip().splitlines()
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            fields[key] = value.strip()
    expected_directory = p_save.stdout.strip()
    expected_filename = fields.get("filename", "")
    expected_path = os.path.join(expected_directory, expected_filename)
    check("new-path-default-exit0", p.returncode == 0,
          "rc=%d stderr=%r" % (p.returncode, p.stderr[:200]))
    check("new-path-default-fields-present",
          set(fields.keys()) == set(["directory", "filename", "path", "created", "project", "git",
                                     "reason", "skills"]),
          "stdout=%r" % p.stdout[:500])
    check("new-path-default-line-order",
          [line.partition(":")[0] for line in lines] == ["directory", "filename", "path", "created",
                                                     "project", "git", "reason", "skills"],
          "stdout=%r" % p.stdout[:500])
    check("new-path-directory-matches-save-path",
          fields.get("directory") == expected_directory,
          "directory=%r want=%r" % (fields.get("directory"), expected_directory))
    check("new-path-path-joins-directory-and-filename",
          fields.get("path") == expected_path,
          "path=%r want=%r" % (fields.get("path"), expected_path))
    check("new-path-topic-used-verbatim",
          expected_filename.endswith("-fresh-topic.md"),
          "filename=%r" % expected_filename)
    filename_minute = expected_filename[:13]
    created_minute = (fields.get("created", "")[:4] + fields.get("created", "")[5:7]
                      + fields.get("created", "")[8:10] + "-"
                      + fields.get("created", "")[11:13]
                      + fields.get("created", "")[14:16])
    check("new-path-filename-and-created-share-minute",
          filename_minute == created_minute,
          "filename=%r created=%r" % (expected_filename, fields.get("created")))

    p = run(["new-path", "json-topic", tmp9, "--json"], tmp9)
    try:
        generated = json.loads(p.stdout)
    except Exception as e:
        generated = {}
        check("new-path-json-parses", False, "%s; stdout=%r" % (e, p.stdout[:300]))
    else:
        check("new-path-json-parses", True)
    check("new-path-json-exact-keys",
          set(generated.keys()) == set(["directory", "filename", "path", "created", "project", "git",
                                        "reason", "skills", "supersedes", "template"]),
          "keys=%r" % sorted(generated.keys()))
    with open(os.path.join(REPO, "skills", "session-handoff", "handoff-template.md"), encoding="utf-8") as f:
        check("new-path-json-template-is-skill-file", generated.get("template") == f.read(),
              "template=%r" % (generated.get("template") or "")[:80])
    check("new-path-json-directory-matches-save-path",
          generated.get("directory") == expected_directory,
          "directory=%r want=%r" % (generated.get("directory"), expected_directory))

    # 7b. new-path reads the session note context_watch.py leaves in TMPDIR:
    #     reason from whether the trigger fired, skills from the transcript's
    #     Skill tool calls, and supersede candidates from the same git branch.
    check("new-path-reason-default-user-parked", fields.get("reason") == "user-parked"
          and fields.get("skills") == "", "fields=%r" % fields)
    tmp7b = tempfile.mkdtemp(prefix="ledger-verify7b-")
    os.makedirs(os.path.join(tmp7b, ".handoffs"))
    subprocess.run(["git", "-C", tmp7b, "init", "-q", "-b", "feat-x"], check=True)
    subprocess.run(["git", "-C", tmp7b, "-c", "user.email=v@v", "-c", "user.name=v",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    with open(os.path.join(tmp7b, ".handoffs", "20260927-0800-same-branch.md"), "w") as f:
        f.write("---\ntopic: same-branch\nstatus: open\ngit: feat-x@abc1234\n---\n# Session Handoff\n")
    with open(os.path.join(tmp7b, ".handoffs", "20260927-0801-other-branch.md"), "w") as f:
        f.write("---\ntopic: other-branch\nstatus: open\ngit: main@abc1234\n---\n# Session Handoff\n")
    tr = os.path.join(tmp7b, "transcript.jsonl")
    with open(tr, "w") as f:
        f.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill",
                                                     "input": {"skill": "ponytail"}}]}}) + "\n")
        f.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Read",
                                                     "input": {"file_path": "x"}}]}}) + "\n")
        f.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill",
                                                     "input": {"skill": "tdd"}}]}}) + "\n")
        f.write(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Skill",
                                                     "input": {"skill": "ponytail"}}]}}) + "\n")
    sys.path.insert(0, os.path.dirname(PLUGIN))
    import handoff_ledger as hl
    notedir = tempfile.mkdtemp(prefix="ledger-verify7b-tmp-")
    env7b = {"TMPDIR": notedir}
    with open(os.path.join(notedir, os.path.basename(hl.session_note_path(tmp7b))), "w") as f:
        json.dump({"session_id": "s1", "cwd": tmp7b, "transcript_path": tr, "fired": True,
                   "ts": time.time()}, f)
    p7b = run(["new-path", "new-thread", tmp7b, "--json"], tmp7b, env7b)
    j7b = json.loads(p7b.stdout or "{}")
    check("new-path-unidentified-session-does-not-borrow-pressure", j7b.get("reason") == "user-parked", "out=%r" % j7b)
    check("new-path-no-eager-skill-inheritance", j7b.get("skills") == "", "out=%r" % j7b)
    check("new-path-does-not-infer-lineage",
          [os.path.basename(x) for x in j7b.get("supersedes", [])] == [],
          "out=%r" % j7b)
    p7b = run(["new-path", "new-thread", tmp7b], tmp7b, env7b)
    check("new-path-no-branch-supersede-instruction", "supersedes: " not in p7b.stdout, "out=%r" % p7b.stdout)

    # 8. supersede --by: bidirectional link, backward compatible with plain
    #    supersede (already covered above).
    tmp8 = tempfile.mkdtemp(prefix="ledger-verify8-supersede-by-")
    hd8 = os.path.join(tmp8, ".handoffs")
    os.makedirs(hd8)
    old2 = os.path.join(hd8, "20260810-0900-topic.md")
    new2 = os.path.join(hd8, "20260811-0900-topic.md")
    with open(old2, "w") as f:
        f.write("---\ntopic: topic\nstatus: open\n---\n# old\n")
    with open(new2, "w") as f:
        f.write("---\ntopic: topic\nstatus: open\n---\n# new\n")
    p = run(["supersede", old2, "--by", new2], tmp8)
    check("supersede-by-exit0", p.returncode == 0, "stderr=%r" % p.stderr[:300])
    old2_text = open(old2).read()
    check("supersede-by-writes-link", ("superseded_by: " + new2) in old2_text,
          "content=%r" % old2_text[:400])
    check("supersede-by-still-marks-status", "status: superseded" in old2_text)
    old3 = os.path.join(hd8, "20260809-0900-topic.md")
    with open(old3, "w") as f:
        f.write("---\ntopic: topic\nstatus: open\n---\n# older\n")
    p = run(["supersede", old3, "--by", os.path.join(hd8, "missing.md")], tmp8)
    check("supersede-by-refuses-missing-target",
          p.returncode == 1 and "status: open" in open(old3).read(),
          "rc=%d stderr=%r" % (p.returncode, p.stderr[:200]))

    # 9. Reads cover the fallback write location. A project with no local
    #    .handoffs/ has every handoff written to
    #    ~/.claude/handoffs/<project>/ — so list and resolve must read there
    #    too, or that project's whole history is invisible to the announcer.
    tmp10 = tempfile.mkdtemp(prefix="ledger-verify10-fallback-")
    home = os.path.join(tmp10, "home")
    proj = os.path.join(tmp10, "myproj")
    fallback = os.path.join(home, ".claude", "handoffs", "myproj")
    os.makedirs(proj)
    os.makedirs(fallback)
    with open(os.path.join(fallback, "20260813-2219-parked-thread.md"), "w") as f:
        f.write("---\ntopic: parked-thread\ncreated: 2026-08-13T22:19\nstatus: open\n"
                "description: parked in the fallback location\n---\n# parked\n")
    home_env = {"HOME": home, "USERPROFILE": home}

    p = run(["save-path", proj], proj, home_env)
    check("fallback-save-path-points-into-home", p.stdout.strip() == fallback,
          "stdout=%r want=%r" % (p.stdout.strip(), fallback))

    p = run(["list", proj, "--json", "--max-age-days", "3650"], proj, home_env)
    try:
        listed = json.loads(p.stdout or "[]")
    except ValueError:
        listed = []
    check("fallback-handoff-is-listed",
          any(entry.get("topic") == "parked-thread" for entry in listed),
          "rc=%d stdout=%r" % (p.returncode, p.stdout[:300]))
    check("fallback-handoff-carries-description",
          any(entry.get("description") == "parked in the fallback location"
              for entry in listed),
          "listed=%r" % listed)

    p = run(["resolve", "parked-thread", proj], proj, home_env)
    check("fallback-handoff-resolves",
          p.returncode == 0 and "20260813-2219-parked-thread.md" in p.stdout,
          "rc=%d stdout=%r stderr=%r" % (p.returncode, p.stdout[:300], p.stderr[:300]))

    # A local .handoffs/ still wins for writes, and both locations are read.
    os.makedirs(os.path.join(proj, ".handoffs"))
    with open(os.path.join(proj, ".handoffs", "20260814-0900-local-thread.md"), "w") as f:
        f.write("---\ntopic: local-thread\ncreated: 2026-08-14T09:00\nstatus: open\n---\n# local\n")
    p = run(["list", proj, "--json", "--max-age-days", "3650"], proj, home_env)
    try:
        both = json.loads(p.stdout or "[]")
    except ValueError:
        both = []
    topics = {entry.get("topic") for entry in both}
    check("both-locations-read-together",
          {"parked-thread", "local-thread"} <= topics, "topics=%r" % topics)

    # 10. Accuracy fixes: per-topic collapse, claim, abandon, stale count,
    #     HANDOFF.md heuristic, missing references, subfolder root, project filter.
    sys.path.insert(0, os.path.dirname(PLUGIN))
    import handoff_ledger as hl
    t11 = tempfile.mkdtemp(prefix="ledger-verify11-")
    h11 = os.path.join(t11, ".handoffs")
    os.makedirs(h11)
    def put(name, body):
        path = os.path.join(h11, name)
        with open(path, "w") as f:
            f.write(body)
        return path
    put("20260901-0900-dup.md", "---\ntopic: dup\nstatus: open\n---\n")
    newest = put("20260902-0900-dup.md", "---\ntopic: dup\nstatus: open\n"
                 "references: exists.md, gone.md\n---\n")
    put("20260801-0900-old.md", "---\ntopic: old\nstatus: open\nreferences: stale-ref.md\n---\n")
    newest_old = put("20260802-0900-old.md", "---\ntopic: old\nstatus: superseded\n---\n")
    open(os.path.join(t11, "exists.md"), "w").close()
    stats = {}
    listed = hl.scan(t11, 14, stats)
    check("scan-one-per-topic", [h["path"] for h in listed if h["topic"] == "dup"] == [newest]
          and stats["duplicates"] == 1, "listed=%r stats=%r" % (listed, stats))
    r = hl.resolve("dup", t11)
    check("resolve-missing-references", r["must_also_read"] == ["exists.md", "gone.md"]
          and r["missing_references"] == ["gone.md"], "resolved=%r" % r)
    r = hl.resolve("old", t11)
    check("resolve-refs-from-authoritative-only",
          r["authoritative"] == newest_old and r["must_also_read"] == [], "resolved=%r" % r)

    p = run(["claim", newest], t11)
    check("claim-hides-from-scan", p.returncode == 0 and "claimed:" in open(newest).read()
          and not any(h["topic"] == "dup" for h in hl.scan(t11)), "stderr=%r" % p.stderr)
    txt = open(newest).read().replace("claimed: ", "claimed: 2000-01-01T00:00 #")
    open(newest, "w").write(txt.replace("#" + txt.split("#")[1].split("\n")[0], ""))
    check("expired-claim-listed-again", any(h["path"] == newest for h in hl.scan(t11)))
    run(["resume", newest], t11)
    check("resume-drops-claim", "claimed:" not in open(newest).read())

    ab = put("20260903-0900-drop.md", "---\ntopic: drop\nstatus: open\n---\n")
    p = run(["abandon", ab], t11)
    check("abandon-closes", p.returncode == 0 and "status: abandoned" in open(ab).read()
          and not any(h["topic"] == "drop" for h in hl.scan(t11)))

    aged = put("20250101-0900-aged.md", "---\ntopic: aged\nstatus: open\n---\n")
    os.utime(aged, (time.time() - 60 * 86400.0,) * 2)
    p = run(["list", t11], t11)
    check("list-reports-hidden-stale", "1 open handoff(s) older than 14 days hidden" in p.stderr,
          "stderr=%r" % p.stderr)

    t12 = tempfile.mkdtemp(prefix="ledger-verify12-")
    with open(os.path.join(t12, "HANDOFF.md"), "w") as f:
        f.write("# Release notes\nnothing to see\n")
    check("bare-handoff-md-needs-marker", hl.scan(t12) == [])
    with open(os.path.join(t12, "HANDOFF.md"), "w") as f:
        f.write("# Session handoff\nnext: ship\n")
    check("bare-handoff-md-with-marker", [h["topic"] for h in hl.scan(t12)] == ["default"])

    put("remaining.md", "---\ntopic: remaining\nstatus: open\n---\n")
    sub = os.path.join(t11, "src", "deep")
    os.makedirs(sub)
    p = run(["list", sub, "--json"], sub)
    check("subfolder-finds-project-handoffs",
          any(h["topic"] == "remaining" for h in json.loads(p.stdout or "[]"))
          and run(["save-path", sub], sub).stdout.strip() == h11, "stdout=%r" % p.stdout[:300])

    t13 = tempfile.mkdtemp(prefix="ledger-verify13-")
    home13 = os.path.join(t13, "home")
    a_proj = os.path.join(t13, "a", "verdict")
    b_proj = os.path.join(t13, "b", "verdict")
    fb = os.path.join(home13, ".claude", "handoffs", "verdict")
    for d in (a_proj, b_proj, fb):
        os.makedirs(d)
    env13 = {"HOME": home13, "USERPROFILE": home13}
    p = run(["new-path", "mine", a_proj, "--json"], a_proj, env13)
    np_ = json.loads(p.stdout)
    check("new-path-records-project", np_["project"] == os.path.realpath(a_proj)
          or np_["project"] == a_proj, "new-path=%r" % np_)
    with open(np_["path"], "w") as f:
        f.write("---\ntopic: mine\nstatus: open\nproject: %s\n---\n" % np_["project"])
    with open(os.path.join(fb, "20260101-0900-legacy.md"), "w") as f:
        f.write("---\ntopic: legacy\nstatus: open\n---\n")
    def topics(proj):
        p = run(["list", proj, "--json", "--max-age-days", "3650"], proj, env13)
        return {h["topic"] for h in json.loads(p.stdout or "[]")}
    check("fallback-project-filter", topics(a_proj) == {"mine", "legacy"}
          and topics(b_proj) == {"legacy"}, "a=%r b=%r" % (topics(a_proj), topics(b_proj)))
    a_sub = os.path.join(a_proj, "pkg")
    os.makedirs(a_sub)
    check("subfolder-finds-fallback-project", "mine" in topics(a_sub), "topics=%r" % topics(a_sub))
    p = run(["new-path", "g", REPO, "--json"], REPO)
    check("new-path-git-position", "@" in json.loads(p.stdout).get("git", ""),
          "stdout=%r" % p.stdout[:300])

    # 11. Hardening: atomic conflict check, unique new-path, problems, ref caps.
    t12 = tempfile.mkdtemp(prefix="ledger-verify12-")
    h12 = os.path.join(t12, ".handoffs")
    os.makedirs(h12)
    def put12(name, body):
        path = os.path.join(h12, name)
        with open(path, "w") as f:
            f.write(body)
        return path
    f12 = put12("20260910-0900-atom.md", "---\ntopic: atom\nstatus: open\n---\nbody\n")
    try:
        hl._atomic_write(f12, "clobbered", (0, 0))
        conflict = False
    except hl.ConflictError:
        conflict = True
    check("atomic-write-refuses-changed-file", conflict and "clobbered" not in open(f12).read()
          and not [n for n in os.listdir(h12) if n.startswith(".handoff-")])
    os.chmod(f12, 0o640)
    run(["resume", f12], t12)
    check("atomic-write-keeps-mode", os.stat(f12).st_mode & 0o777 == 0o640
          and "status: resumed" in open(f12).read())

    first = json.loads(run(["new-path", "same", t12, "--json"], t12).stdout)
    open(first["path"], "w").write("---\ntopic: same\nstatus: open\n---\n")
    second = json.loads(run(["new-path", "same", t12, "--json"], t12).stdout)
    check("new-path-unique-suffix", second["path"] != first["path"]
          and second["filename"].endswith("-same-2.md"), "second=%r" % second)
    open(second["path"], "w").write("---\ntopic: same\nstatus: open\n---\n")
    check("suffixed-file-keeps-topic",
          len(hl.resolve("same", t12)["chain"]) == 2, "chain=%r" % hl.resolve("same", t12)["chain"])

    put12("20260911-0900-weird.md", "---\ntopic: weird\nstatus: resuming\ncreated: yesterday\n---\n")
    put12("20260911-0901-nofence.md", "---\ntopic: nofence\nstatus: open\n")
    by_topic = {h["topic"]: h for h in hl.scan(t12)}
    check("unknown-status-listed-as-open", "weird" in by_topic, "topics=%r" % list(by_topic))
    probs = " ".join(by_topic.get("weird", {}).get("problems", []))
    check("problems-unknown-status-and-created", "unknown status" in probs and "not ISO" in probs,
          "problems=%r" % probs)
    check("problems-no-closing-fence",
          any("closing" in x for x in by_topic.get("20260911-0901-nofence", by_topic.get("nofence", {}))
              .get("problems", [])), "scan=%r" % by_topic)
    p = run(["resolve", "weird", t12], t12)
    check("resolve-prints-problems", "problem: unknown status" in p.stdout, "stdout=%r" % p.stdout)

    # 11b. Template lint + claim report: problems, verify:, commits_since/dirty.
    t11b = tempfile.mkdtemp(prefix="ledger-verify11b-")
    os.makedirs(os.path.join(t11b, ".handoffs"))
    subprocess.run(["git", "-C", t11b, "init", "-q", "-b", "main"], check=True)
    def commit11b(msg):
        subprocess.run(["git", "-C", t11b, "-c", "user.email=v@v", "-c", "user.name=v",
                        "commit", "-q", "--allow-empty", "-m", msg], check=True)
    commit11b("base")
    sha11b = subprocess.run(["git", "-C", t11b, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    commit11b("moved on")
    with open(os.path.join(t11b, "untracked.txt"), "w") as f:
        f.write("x")
    f11b = os.path.join(t11b, ".handoffs", "20260927-0900-lint.md")
    with open(f11b, "w") as f:
        f.write("---\ntopic: lint\nstatus: open\nproject: %s\ngit: main@%s\nverify: python3 -c 1\n---\n"
                "## Objective\n%s\n## Next steps\n1. x\n" % (t11b, sha11b, "word " * 1600))
    p = run(["claim", f11b], t11b)
    check("claim-prints-missing-section", "problem: missing section '## Current state'" in p.stdout,
          "stdout=%r" % p.stdout)
    check("claim-prints-word-limit", "problem: body is 1" in p.stdout and "(limit 1500)" in p.stdout,
          "stdout=%r" % p.stdout)
    check("claim-prints-verify", "verify: python3 -c 1\n" in p.stdout, "stdout=%r" % p.stdout)
    check("claim-prints-commits-since", "commits_since: 1\n" in p.stdout, "stdout=%r" % p.stdout)
    check("claim-prints-dirty", "dirty: 2\n" in p.stdout, "stdout=%r" % p.stdout)
    with open(f11b, "w") as f:
        f.write("---\ntopic: lint\nstatus: open\n---\n## Objective\nx\n## Current state\nx\n## Next steps\nx\n")
    check("complete-body-has-no-problems", hl.claim_report(f11b) == [], "%r" % hl.claim_report(f11b))

    refs = ["r%d.md" % i for i in range(10)]
    for ref in refs[:9]:
        with open(os.path.join(t12, ref), "w") as f:
            f.write("x" * (200_000 if ref == "r1.md" else 10))
    put12("20260912-0900-refs.md", "---\ntopic: refs\nstatus: open\nreferences: %s, r9.md\n---\n"
          % ", ".join(refs[:9]))
    r = hl.resolve("refs", t12)
    check("resolve-ref-caps", len(r["must_also_read"]) == 8 and "r9.md" not in r["must_also_read"]
          and {u["ref"] for u in r["unresolved_references"]} == {"r8.md", "r9.md"},
          "resolved=%r" % r)
    big = put12("20260912-0901-bigrefs.md", "---\ntopic: bigrefs\nstatus: open\n"
                "references: r1.md, r1.md, big2.md, r0.md\n---\n")
    with open(os.path.join(t12, "big2.md"), "w") as f:
        f.write("y" * 100_000)
    r = hl.resolve("bigrefs", t12)
    check("resolve-byte-cap", r["must_also_read"] == ["r1.md", "r0.md"]
          and r["unresolved_references"][0]["ref"] == "big2.md", "resolved=%r" % r)
    p = run(["resolve", "bigrefs", t12], t12)
    check("resolve-prints-unresolved", "unresolved_reference: big2.md" in p.stdout, "stdout=%r" % p.stdout)

    # 12. Owner-aware claims.
    oc = put12("20260913-0900-owned.md", "---\ntopic: owned\nstatus: open\n---\n")
    p = run(["claim", oc, "--owner", "sess-a"], t12)
    check("owner-claim-writes-owner", p.returncode == 0 and "claim_owner: sess-a" in open(oc).read())
    p = run(["claim", oc, "--owner", "sess-a"], t12)
    check("owner-claim-idempotent", p.returncode == 0 and open(oc).read().count("claim_owner:") == 1)
    p = run(["claim", oc, "--owner", "sess-b"], t12)
    check("owner-claim-refuses-other", p.returncode == 1 and "refused" in p.stderr
          and "claim_owner: sess-a" in open(oc).read(), "rc=%d stderr=%r" % (p.returncode, p.stderr))
    p = run(["claim", oc], t12)
    check("bare-claim-refuses-owned", p.returncode == 1, "rc=%d" % p.returncode)
    p = run(["resume", oc, "--owner", "sess-b"], t12)
    check("owner-resume-refuses-other", p.returncode == 1 and "status: open" in open(oc).read())
    p = run(["release", oc, "--owner", "sess-b"], t12)
    check("release-refuses-other", p.returncode == 1)
    p = run(["release", oc, "--owner", "sess-a"], t12)
    check("release-drops-claim", p.returncode == 0 and "claimed:" not in open(oc).read()
          and "claim_owner:" not in open(oc).read())
    run(["claim", oc, "--owner", "sess-a"], t12)
    p = run(["resume", oc, "--owner", "sess-a"], t12)
    txt = open(oc).read()
    check("owner-resume-drops-owner", p.returncode == 0 and "status: resumed" in txt
          and "claim_owner:" not in txt, "content=%r" % txt)
    bc = put12("20260913-0901-bare.md", "---\ntopic: bare\nstatus: open\n---\n")
    check("bare-claim-still-works", run(["claim", bc], t12).returncode == 0
          and run(["claim", bc, "--owner", "x"], t12).returncode == 0)
    expired = put12("20260913-0902-expired.md", "---\ntopic: expired\nstatus: open\n"
                    "claimed: 2000-01-01T00:00\nclaim_owner: gone\n---\n")
    check("expired-owner-claim-yields", run(["claim", expired, "--owner", "new"], t12).returncode == 0
          and "claim_owner: new" in open(expired).read())

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL LEDGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
