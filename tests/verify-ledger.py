#!/usr/bin/env python3
"""Executed verification for the ledger fix lane. Run from repo root (worktree)."""
import ast
import json
import os
import subprocess
import sys
import tempfile

REPO = os.getcwd()
PLUGIN = os.path.join(REPO, "plugins/session-handoff/hooks/handoff_ledger.py")
CODEX = os.path.join(REPO, "codex/hooks/handoff_ledger.py")
PY = sys.executable

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(args, cwd):
    return subprocess.run([PY, PLUGIN] + args, cwd=cwd, capture_output=True, text=True, timeout=30)


def main():
    for path in (PLUGIN, CODEX):
        try:
            ast.parse(open(path).read())
            check("parse:" + os.path.relpath(path, REPO), True)
        except Exception as e:
            check("parse:" + os.path.relpath(path, REPO), False, str(e))
    check("copies-identical", open(PLUGIN, "rb").read() == open(CODEX, "rb").read())

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

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL LEDGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
