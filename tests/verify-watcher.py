#!/usr/bin/env python3
"""Executed verification for the watcher fix lane.

Runs context_watch.py as a subprocess against synthetic transcripts and
asserts the fixed behaviors. Run from the repo root (worktree)."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import uuid

REPO = os.getcwd()
PLUGIN = os.path.join(REPO, "plugins/session-handoff/hooks/context_watch.py")
CODEX = os.path.join(REPO, "codex/hooks/context_watch.py")
PY = sys.executable

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run_hook(event, env_extra, script=PLUGIN):
    env = dict(os.environ)
    env.pop("CONTEXT_WATCH_TOKENS_MAP", None)
    env.pop("CONTEXT_WATCH_PERCENT", None)
    env.update(env_extra)
    p = subprocess.run([PY, script], input=json.dumps(event), env=env,
                       capture_output=True, text=True, timeout=30)
    return p


def main():
    # 1. Both copies parse and are byte-identical
    for path in (PLUGIN, CODEX):
        try:
            ast.parse(open(path).read())
            check("parse:" + os.path.relpath(path, REPO), True)
        except Exception as e:
            check("parse:" + os.path.relpath(path, REPO), False, str(e))
    check("copies-identical", open(PLUGIN, "rb").read() == open(CODEX, "rb").read())

    tmp = tempfile.mkdtemp(prefix="cw-verify-")
    latchdir = os.path.join(tmp, "latches")
    os.makedirs(latchdir)

    # 2. Claude trigger: over-threshold transcript emits additionalContext with a
    #    cache-read SHARE (percentage), and analytics logs the event.
    transcript = os.path.join(tmp, "claude.jsonl")
    with open(transcript, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 30000,
                                                  "cache_creation_input_tokens": 5000,
                                                  "cache_read_input_tokens": 100000,
                                                  "output_tokens": 2000}}}) + "\n")
    log1 = os.path.join(tmp, "events1.jsonl")
    sid = "verify-" + uuid.uuid4().hex[:8]
    evt = {"hook_event_name": "PostToolUse", "session_id": sid,
           "transcript_path": transcript, "cwd": tmp}
    envx = {"CONTEXT_WATCH_TOKENS": "100000", "CONTEXT_WATCH_LOG": log1,
            "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude",
            "TMPDIR": latchdir}
    p = run_hook(evt, envx)
    check("claude-trigger-exit0", p.returncode == 0, "rc=%d stderr=%s" % (p.returncode, p.stderr[:200]))
    try:
        out = json.loads(p.stdout)
        msg = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        msg = ""
    check("claude-trigger-message", "[context-watch]" in msg and "session-handoff" in msg,
          "stdout=%r" % p.stdout[:300])
    # SPEC 5.2: the message names the cache-read SHARE — a percentage, not only a count.
    # occupancy=137000, cache_read=100000 -> ~73%
    check("cache-read-share-is-percent", "%" in msg, "message=%r" % msg[:300])
    check("analytics-logged", os.path.isfile(log1) and json.loads(open(log1).read().splitlines()[-1])["occupancy"] == 137000)

    # 3. Latch: same session id fires once
    p2 = run_hook(evt, envx)
    check("latch-fires-once", p2.returncode == 0 and p2.stdout.strip() == "",
          "stdout=%r" % p2.stdout[:200])

    # 4. stats CLI honors CONTEXT_WATCH_LOG=0 as 'disabled', not a path
    env = dict(os.environ)
    env["CONTEXT_WATCH_LOG"] = "0"
    p3 = subprocess.run([PY, PLUGIN, "stats"], env=env, capture_output=True, text=True, timeout=30)
    check("stats-log0-disabled", p3.returncode == 0 and "disabl" in p3.stdout.lower(),
          "stdout=%r" % p3.stdout[:200])

    # 5. Codex: model comes from turn_context, NOT from other payloads' model field
    rollout = os.path.join(tmp, "codex.jsonl")
    with open(rollout, "w") as f:
        f.write(json.dumps({"type": "turn_context", "payload": {"model": "gpt-right"}}) + "\n")
        f.write(json.dumps({"payload": {"type": "token_count", "model": "gpt-wrong",
                                        "info": {"last_token_usage": {"total_tokens": 150000},
                                                 "model_context_window": 272000}}}) + "\n")
    log2 = os.path.join(tmp, "events2.jsonl")
    evt2 = {"hook_event_name": "UserPromptSubmit", "session_id": "verify-" + uuid.uuid4().hex[:8],
            "transcript_path": rollout}
    envx2 = {"CONTEXT_WATCH_TOKENS": "100000", "CONTEXT_WATCH_LOG": log2,
             "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "codex",
             "TMPDIR": latchdir}
    p4 = run_hook(evt2, envx2)
    check("codex-trigger-stdout", p4.returncode == 0 and "[context-watch]" in p4.stdout,
          "rc=%d stdout=%r" % (p4.returncode, p4.stdout[:200]))
    model = ""
    if os.path.isfile(log2):
        model = json.loads(open(log2).read().splitlines()[-1]).get("model") or ""
    check("codex-model-from-turn-context", model == "gpt-right", "logged model=%r" % model)

    # 6. Announcer: skips continuation sources (resume, compact, fork); announces on startup
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, ".handoffs"))
    with open(os.path.join(proj, ".handoffs", "demo-topic.md"), "w") as f:
        f.write("---\ntopic: demo-topic\ncreated: 2026-08-11T09:00\nstatus: open\n---\n# Session Handoff — demo-topic\n")
    for src, expect_announce in (("startup", True), ("resume", False), ("compact", False), ("fork", False)):
        ev = {"hook_event_name": "SessionStart", "source": src, "cwd": proj,
              "session_id": "verify-ss"}
        p5 = run_hook(ev, {"TMPDIR": latchdir})
        announced = "demo-topic" in p5.stdout
        check("announcer-source-%s" % src, p5.returncode == 0 and announced == expect_announce,
              "rc=%d stdout=%r" % (p5.returncode, p5.stdout[:200]))

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL WATCHER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
