#!/usr/bin/env python3
"""Executed verification for the watcher fix lane.

Runs context_watch.py as a subprocess against synthetic transcripts and
asserts the fixed behaviors. Run from the repo root (worktree)."""
import ast
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
    env.pop("HANDOFF_AT", None)
    env.pop("AUTORESUME", None)
    env.pop("HANDOFF_AUTO", None)
    env.pop("HANDOFF_AUTO_RUNNER", None)
    env.pop("CONTEXT_WATCH_AUTORESUME", None)
    env.pop("CONTEXT_WATCH_TOKENS", None)
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
    check("non-autoresume-message-omits-clear", "/clear" not in msg,
          "message=%r" % msg[:400])
    check("analytics-logged", os.path.isfile(log1) and json.loads(open(log1).read().splitlines()[-1])["occupancy"] == 137000)

    # 3. HANDOFF_AT precedence: beats CONTEXT_WATCH_TOKENS
    transcript_ha = os.path.join(tmp, "claude-handoff-at.jsonl")
    with open(transcript_ha, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 30000,
                                                  "cache_creation_input_tokens": 5000,
                                                  "cache_read_input_tokens": 100000,
                                                  "output_tokens": 2000}}}) + "\n")
    log_ha = os.path.join(tmp, "events-handoff-at.jsonl")
    evt_ha = {"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
              "transcript_path": transcript_ha, "cwd": tmp}
    env_ha = {"CONTEXT_WATCH_TOKENS": "999999", "HANDOFF_AT": "100000",
              "CONTEXT_WATCH_LOG": log_ha, "CONTEXT_WATCH_PENDING": "0",
              "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir}
    p_ha = run_hook(evt_ha, env_ha)
    check("handoff-at-trigger-exit0", p_ha.returncode == 0,
          "rc=%d stderr=%s" % (p_ha.returncode, p_ha.stderr[:200]))
    try:
        rec_ha = json.loads(open(log_ha).read().splitlines()[-1])
    except Exception:
        rec_ha = {}
    check("handoff-at-threshold-source",
          rec_ha.get("threshold") == 100000 and rec_ha.get("threshold_source") == "HANDOFF_AT",
          "record=%r" % rec_ha)

    # 4. Autoresume trigger message: /clear instruction is present only when enabled
    transcript_ar = os.path.join(tmp, "claude-autoresume.jsonl")
    with open(transcript_ar, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 30000,
                                                  "cache_creation_input_tokens": 5000,
                                                  "cache_read_input_tokens": 100000,
                                                  "output_tokens": 2000}}}) + "\n")
    log_ar = os.path.join(tmp, "events-autoresume.jsonl")
    evt_ar = {"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
              "transcript_path": transcript_ar, "cwd": tmp}
    env_ar = {"HANDOFF_AT": "100000", "AUTORESUME": "1", "CONTEXT_WATCH_LOG": log_ar,
              "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude",
              "TMPDIR": latchdir}
    p_ar = run_hook(evt_ar, env_ar)
    try:
        out_ar = json.loads(p_ar.stdout)
        msg_ar = out_ar.get("hookSpecificOutput", {}).get("additionalContext", "")
    except Exception:
        msg_ar = ""
    check("autoresume-trigger-message-clear", p_ar.returncode == 0 and "/clear" in msg_ar,
          "rc=%d stdout=%r" % (p_ar.returncode, p_ar.stdout[:400]))

    # 5. Latch: the same session fires once; a second notice needs a model turn
    #    since the first AND occupancy 25% of the threshold past the first
    #    (and >= 125%); then never again until a compaction re-arms it.
    p2 = run_hook(evt, dict(envx, CONTEXT_WATCH_TOKENS="120000"))  # 137k < 150k
    check("latch-fires-once", p2.returncode == 0 and p2.stdout.strip() == "",
          "stdout=%r" % p2.stdout[:200])
    # Parallel tool results land before the model saw the first notice: the
    # transcript usage is unchanged, so no second notice even far past 125%.
    p2p = run_hook(dict(evt, hook_event_name="UserPromptSubmit", prompt="word " * 40000),
                   dict(envx, CONTEXT_WATCH_PENDING="1"))  # 137k + ~50k pending
    check("latch-no-second-before-model-turn", p2p.stdout.strip() == "",
          "stdout=%r" % p2p.stdout[:200])
    grown = os.path.join(tmp, "claude-grown.jsonl")
    with open(grown, "w") as f:
        f.write(open(transcript).read())
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 30000,
                                                  "cache_read_input_tokens": 125000,
                                                  "output_tokens": 2000}}}) + "\n")
    p2g = run_hook(dict(evt, transcript_path=grown), envx)  # 157k: turn taken, < 137k+25k
    check("latch-no-second-until-quarter-past-first", p2g.stdout.strip() == "",
          "stdout=%r" % p2g.stdout[:200])
    with open(grown, "a") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 30000,
                                                  "cache_read_input_tokens": 140000,
                                                  "output_tokens": 2000}}}) + "\n")
    p2b = run_hook(dict(evt, transcript_path=grown), envx)  # 172k >= 162k
    check("latch-second-notice", "SECOND NOTICE" in p2b.stdout, "stdout=%r" % p2b.stdout[:200])
    check("latch-second-notice-logged",
          json.loads(open(log1).read().splitlines()[-1]).get("notice") == 2)
    p2c = run_hook(evt, envx)
    check("latch-silent-after-second", p2c.stdout.strip() == "", "stdout=%r" % p2c.stdout[:200])
    small = os.path.join(tmp, "claude-compacted.jsonl")
    with open(transcript) as f_in, open(small, "w") as f:
        f.write(f_in.read())
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 20000,
                                                  "output_tokens": 1000}}}) + "\n")
    p2d = run_hook(dict(evt, transcript_path=small), envx)
    check("latch-rearm-after-compaction", p2d.stdout.strip() == ""
          and not os.path.exists(os.path.join(latchdir, "context-watch-%s.fired" % sid)),
          "stdout=%r" % p2d.stdout[:200])
    p2e = run_hook(evt, envx)
    check("latch-fires-again-after-rearm", "[context-watch]" in p2e.stdout
          and "SECOND NOTICE" not in p2e.stdout, "stdout=%r" % p2e.stdout[:200])

    # 5b. Compaction boundary: pre-compaction usage is not the window any more.
    p2f = run_hook({"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
                    "transcript_path": small, "cwd": tmp}, envx)
    check("compaction-resets-usage", p2f.stdout.strip() == "", "stdout=%r" % p2f.stdout[:200])

    # 5c. Pending estimate: capped, skips hook-only metadata, flat cost for base64.
    small_t = os.path.join(tmp, "claude-small.jsonl")
    with open(small_t, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 90000}}}) + "\n")
    def pending_for(response, key="tool_response", event="PostToolUse"):
        log_p = os.path.join(tmp, "events-pending-%s.jsonl" % uuid.uuid4().hex[:6])
        run_hook({"hook_event_name": event, "session_id": "verify-" + uuid.uuid4().hex[:8],
                  "transcript_path": small_t, "cwd": tmp, key: response},
                 {"CONTEXT_WATCH_TOKENS": "90000", "CONTEXT_WATCH_LOG": log_p,
                  "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir})
        return json.loads(open(log_p).read().splitlines()[-1])
    rec = pending_for({"stdout": "word " * 80000})
    check("pending-capped", rec["pending_estimate"] == 25000 and rec["pending_raw"] == 100000,
          "record=%r" % rec)
    rec = pending_for({"filePath": "a.py", "originalFile": "y" * 400000, "newString": "z" * 400})
    check("pending-skips-originalFile", rec["pending_estimate"] < 200, "record=%r" % rec)
    rec = pending_for({"type": "image", "data": "QUJD" * 50000})
    check("pending-base64-flat", 1600 <= rec["pending_estimate"] < 1700, "record=%r" % rec)
    # A long unbroken word is text, not media; real base64 is flat-priced.
    rec = pending_for("T" * 520000, key="prompt", event="UserPromptSubmit")
    check("pending-long-word-prompt-is-text",
          rec["pending_estimate"] == 130000 and rec["pending_raw"] == 130000, "record=%r" % rec)
    rec = pending_for({"stdout": "T" * 520000})
    check("pending-long-word-tool-is-text",
          rec["pending_estimate"] == 25000 and rec["pending_raw"] == 130000, "record=%r" % rec)
    rec = pending_for({"stdout": base64.b64encode(os.urandom(300000)).decode()})
    check("pending-real-base64-flat", rec["pending_raw"] == 1600, "record=%r" % rec)
    rec = pending_for({"stdout": "QUJD" * 50000})
    check("pending-plain-alnum-is-text", rec["pending_raw"] == 50000, "record=%r" % rec)
    # 5d. Sessions without an id get a per-transcript latch, not one shared latch.
    id_less = []
    for n in ("a", "b"):
        tp = os.path.join(tmp, "idless-%s.jsonl" % n)
        shutil.copyfile(transcript, tp)
        id_less.append(run_hook({"hook_event_name": "PostToolUse", "transcript_path": tp,
                                 "cwd": tmp}, envx))
    check("idless-sessions-each-fire", all("[context-watch]" in q.stdout for q in id_less),
          "stdout=%r" % [q.stdout[:100] for q in id_less])


    # 6. stats CLI honors CONTEXT_WATCH_LOG=0 as 'disabled', not a path
    env = dict(os.environ)
    env["CONTEXT_WATCH_LOG"] = "0"
    p3 = subprocess.run([PY, PLUGIN, "stats"], env=env, capture_output=True, text=True, timeout=30)
    check("stats-log0-disabled", p3.returncode == 0 and "disabl" in p3.stdout.lower(),
          "stdout=%r" % p3.stdout[:200])

    # 7. Codex: model comes from turn_context, NOT from other payloads' model field
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
    check("codex-trigger-stdout", p4.returncode == 0 and p4.stdout.startswith("context-watch:"),
          "rc=%d stdout=%r" % (p4.returncode, p4.stdout[:200]))
    model = ""
    if os.path.isfile(log2):
        model = json.loads(open(log2).read().splitlines()[-1]).get("model") or ""
    check("codex-model-from-turn-context", model == "gpt-right", "logged model=%r" % model)

    # 8. Announcer: skips continuation sources (resume, compact, fork); announces on startup/clear
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, ".handoffs"))
    with open(os.path.join(proj, ".handoffs", "demo-topic.md"), "w") as f:
        f.write("---\ntopic: demo-topic\ncreated: 2026-08-11T09:00\nstatus: open\n---\n# Session Handoff — demo-topic\n")
    for src, expect_announce in (("startup", True), ("clear", True), ("resume", False),
                                 ("compact", False), ("fork", False)):
        ev = {"hook_event_name": "SessionStart", "source": src, "cwd": proj,
              "session_id": "verify-ss"}
        p5 = run_hook(ev, {"TMPDIR": latchdir})
        announced = "demo-topic" in p5.stdout
        check("announcer-source-%s" % src, p5.returncode == 0 and announced == expect_announce,
              "rc=%d stdout=%r" % (p5.returncode, p5.stdout[:200]))
    p_single_ended = run_hook({"hook_event_name": "SessionStart", "source": "startup",
                               "cwd": proj, "session_id": "verify-ss-ended"},
                              {"TMPDIR": latchdir})
    check("announcer-ended-single",
          p_single_ended.returncode == 0 and "2026-08-11T09:00" in p_single_ended.stdout
          and "d old" in p_single_ended.stdout,
          "rc=%d stdout=%r" % (p_single_ended.returncode, p_single_ended.stdout[:500]))

    proj_multi = os.path.join(tmp, "proj-multi")
    os.makedirs(os.path.join(proj_multi, ".handoffs"))
    with open(os.path.join(proj_multi, ".handoffs", "first.md"), "w") as f:
        f.write("---\ntopic: first\ncreated: 2026-08-10T08:30\nstatus: open\n---\n# Session Handoff — first\n")
    with open(os.path.join(proj_multi, ".handoffs", "second.md"), "w") as f:
        f.write("---\ntopic: second\ncreated: 2026-08-11T14:45\nstatus: open\n---\n# Session Handoff — second\n")
    p_multi_ended = run_hook({"hook_event_name": "SessionStart", "source": "startup",
                              "cwd": proj_multi, "session_id": "verify-multi-ended"},
                             {"TMPDIR": latchdir})
    check("announcer-ended-multi",
          p_multi_ended.returncode == 0 and "2026-08-10T08:30" in p_multi_ended.stdout
          and "2026-08-11T14:45" in p_multi_ended.stdout and "d old" in p_multi_ended.stdout,
          "rc=%d stdout=%r" % (p_multi_ended.returncode, p_multi_ended.stdout[:700]))

    fallback_hook_dir = os.path.join(tmp, "fallback-hook")
    os.makedirs(fallback_hook_dir)
    fallback_hook = os.path.join(fallback_hook_dir, "context_watch.py")
    shutil.copyfile(PLUGIN, fallback_hook)
    fallback_proj = os.path.join(tmp, "fallback-proj")
    os.makedirs(fallback_proj)
    fallback_path = os.path.join(fallback_proj, "HANDOFF.md")
    with open(fallback_path, "w") as f:
        f.write("# Legacy handoff\n")
    old_mtime = time.time() - 13 * 86400.0
    os.utime(fallback_path, (old_mtime, old_mtime))
    expected_ended = time.strftime("%Y-%m-%dT%H:%M", time.localtime(old_mtime))
    p_fallback = run_hook({"hook_event_name": "SessionStart", "source": "startup",
                           "cwd": fallback_proj, "session_id": "verify-fallback"},
                          {"TMPDIR": latchdir}, script=fallback_hook)
    check("announcer-fallback-age-from-mtime",
          p_fallback.returncode == 0 and "default" in p_fallback.stdout
          and "13d old" in p_fallback.stdout and "0d old" not in p_fallback.stdout
          and expected_ended in p_fallback.stdout,
          "rc=%d expected_ended=%s stdout=%r" % (p_fallback.returncode, expected_ended,
                                                p_fallback.stdout[:700]))

    # 9. Announcer includes descriptions and autoresume language only when enabled
    proj_desc = os.path.join(tmp, "proj-desc")
    os.makedirs(os.path.join(proj_desc, ".handoffs"))
    with open(os.path.join(proj_desc, ".handoffs", "20260811-1200-descdemo.md"), "w") as f:
        f.write("---\ntopic: descdemo\nstatus: open\ndescription: unmistakable demo description\n---\n# Session Handoff — descdemo\n")
    ev_desc = {"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_desc,
               "session_id": "verify-desc"}
    p_desc_ar = run_hook(ev_desc, {"AUTORESUME": "true", "TMPDIR": latchdir})
    check("announcer-description-autoresume",
          p_desc_ar.returncode == 0 and "unmistakable demo description" in p_desc_ar.stdout
          and "without asking" in p_desc_ar.stdout,
          "rc=%d stdout=%r" % (p_desc_ar.returncode, p_desc_ar.stdout[:500]))
    p_desc = run_hook(ev_desc, {"TMPDIR": latchdir})
    check("announcer-description-no-autoresume",
          p_desc.returncode == 0 and "descdemo" in p_desc.stdout
          and "without asking" not in p_desc.stdout
          and "First load exactly these skills" not in p_desc.stdout,
          "rc=%d stdout=%r" % (p_desc.returncode, p_desc.stdout[:500]))

    # 10. Announcer surfaces skills instructions for a single handoff when provided.
    proj_skills = os.path.join(tmp, "proj-skills")
    os.makedirs(os.path.join(proj_skills, ".handoffs"))
    with open(os.path.join(proj_skills, ".handoffs", "20260811-1300-skillsdemo.md"), "w") as f:
        f.write("---\ntopic: skillsdemo\nstatus: open\nskills: tdd, dataviz\n---\n# Session Handoff — skillsdemo\n")
    ev_skills = {"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_skills,
                 "session_id": "verify-skills"}
    p_skills = run_hook(ev_skills, {"TMPDIR": latchdir})
    check("announcer-skills-single",
          p_skills.returncode == 0 and "First load exactly these skills" in p_skills.stdout
          and "tdd, dataviz" in p_skills.stdout,
          "rc=%d stdout=%r" % (p_skills.returncode, p_skills.stdout[:500]))

    # 11. Codex: PostToolUse notice says it replaced the tool result; SessionStart
    #     is JSON with the bracket-free label.
    evt_cx = {"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
              "transcript_path": rollout}
    p_cx = run_hook(evt_cx, envx2)
    check("codex-posttooluse-exit2-notes-replaced-result",
          p_cx.returncode == 2 and p_cx.stderr.startswith("context-watch:")
          and "replaced the output" in p_cx.stderr,
          "rc=%d stderr=%r" % (p_cx.returncode, p_cx.stderr[:300]))
    p_cx_ss = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_skills,
                        "session_id": "verify-cx-ss"},
                       {"TMPDIR": latchdir, "CONTEXT_WATCH_AGENT": "codex"})
    try:
        cx_note = json.loads(p_cx_ss.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        cx_note = ""
    check("codex-sessionstart-json-label", cx_note.startswith("context-watch:"),
          "stdout=%r" % p_cx_ss.stdout[:300])

    # 12. Announcer: cap at 5 listed, count the rest, mention hidden stale ones.
    proj_many = os.path.join(tmp, "proj-many")
    os.makedirs(os.path.join(proj_many, ".handoffs"))
    for i in range(8):
        with open(os.path.join(proj_many, ".handoffs", "2026081%d-0900-t%d.md" % (i, i)), "w") as f:
            f.write("---\ntopic: t%d\nstatus: open\n---\n# Session Handoff\n" % i)
    stale = os.path.join(proj_many, ".handoffs", "20250101-0900-ancient.md")
    with open(stale, "w") as f:
        f.write("---\ntopic: ancient\nstatus: open\n---\n# Session Handoff\n")
    os.utime(stale, (time.time() - 90 * 86400.0,) * 2)
    p_many = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_many,
                       "session_id": "verify-many"}, {"TMPDIR": latchdir})
    check("announcer-caps-listing", "8 open handoffs" in p_many.stdout and "5) t3" in p_many.stdout
          and "6)" not in p_many.stdout and "and 3 more" in p_many.stdout,
          "stdout=%r" % p_many.stdout[:900])
    check("announcer-mentions-hidden-stale", "1 open handoff(s) older than 14 days" in p_many.stdout,
          "stdout=%r" % p_many.stdout[-400:])

    # 13. Fully automatic mode: the newest handoff wins without asking; the trigger
    #     tells the agent to end its turn instead of asking the user to /clear.
    p_auto = run_hook({"hook_event_name": "SessionStart", "source": "clear", "cwd": proj_multi,
                       "session_id": "verify-auto"}, {"TMPDIR": latchdir, "HANDOFF_AUTO": "1"})
    check("auto-announcer-picks-newest",
          "'second'" in p_auto.stdout and "without asking" in p_auto.stdout
          and "newest of 2" in p_auto.stdout and "ask the user which" not in p_auto.stdout,
          "stdout=%r" % p_auto.stdout[:500])
    p_auto_t = run_hook(dict(evt_ar, session_id="verify-" + uuid.uuid4().hex[:8]),
                        dict(env_ar, HANDOFF_AUTO="1"))
    check("auto-trigger-ends-turn", "end your turn" in p_auto_t.stdout
          and "type /clear" not in p_auto_t.stdout, "stdout=%r" % p_auto_t.stdout[:500])

    # 14. Stop hook (auto mode, tmux): after the trigger fired and a handoff was
    #     written, types /clear then "resume" into the pane, exactly once.
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    keys = os.path.join(tmp, "tmux-keys.txt")
    with open(os.path.join(bindir, "tmux"), "w") as f:
        f.write("#!/bin/sh\necho \"$*\" >> %s\n" % keys)
    os.chmod(os.path.join(bindir, "tmux"), 0o755)
    proj_stop = os.path.join(tmp, "proj-stop")
    os.makedirs(os.path.join(proj_stop, ".handoffs"))
    sid_stop = "verify-" + uuid.uuid4().hex[:8]
    env_stop = {"TMPDIR": latchdir, "HANDOFF_AUTO": "1", "TMUX_PANE": "%9",
                "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
    stop_evt = {"hook_event_name": "Stop", "session_id": sid_stop, "cwd": proj_stop}
    run_hook(stop_evt, env_stop)  # not fired yet: nothing to do
    open(os.path.join(latchdir, "context-watch-%s.fired" % sid_stop), "w").close()
    run_hook(stop_evt, env_stop)  # fired but no handoff yet
    with open(os.path.join(proj_stop, ".handoffs", "20260927-0900-stopdemo.md"), "w") as f:
        f.write("---\ntopic: stopdemo\nstatus: open\n---\n# Session Handoff\n")
    run_hook(stop_evt, env_stop)
    run_hook(stop_evt, env_stop)  # second Stop must not type again
    counter = os.path.join(latchdir, [n for n in os.listdir(latchdir)
                                      if n.startswith("context-watch-auto-")][0])
    check("auto-stop-counts-clear", open(counter).read().strip() == "1")
    deadline = time.time() + 15
    while time.time() < deadline and (not os.path.exists(keys)
                                      or "resume" not in open(keys).read()):
        time.sleep(0.5)
    time.sleep(1)
    typed = open(keys).read() if os.path.exists(keys) else ""
    check("auto-stop-types-clear-then-resume",
          typed.count("/clear") == 1 and typed.count("resume") == 1
          and typed.index("/clear") < typed.index("resume") and "-t %9" in typed,
          "typed=%r" % typed)

    # 14b. Loop guard: after HANDOFF_AUTO_MAX clears in a row the Stop hook stops
    #      typing and tells the user; a turn ending without the trigger resets it.
    for i in range(3):
        sid_loop = "verify-" + uuid.uuid4().hex[:8]
        open(os.path.join(latchdir, "context-watch-%s.fired" % sid_loop), "w").close()
        p_loop = run_hook(dict(stop_evt, session_id=sid_loop), dict(env_stop, HANDOFF_AUTO_MAX="2"))
    check("auto-stop-loop-guard", "stopped after 2 automatic clears" in p_loop.stdout
          and open(counter).read().strip() == "2", "stdout=%r" % p_loop.stdout[:300])
    run_hook(dict(stop_evt, session_id="verify-" + uuid.uuid4().hex[:8]), env_stop)
    check("auto-stop-counter-resets", not os.path.exists(counter))

    # 15. Headless runner: loops while each run leaves a new open handoff.
    proj_run = os.path.join(tmp, "proj-run")
    os.makedirs(os.path.join(proj_run, ".handoffs"))
    fake = os.path.join(bindir, "fake-agent")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\necho \"$HANDOFF_AUTO $*\" >> calls.txt\n"
                "n=$(wc -l < calls.txt | tr -d ' ')\n"
                "if [ \"$n\" -lt 3 ]; then printf -- '---\\ntopic: run%s\\nstatus: open\\n---\\n' $n"
                " > .handoffs/2026092$n-0900-run$n.md; fi\n")
    os.chmod(fake, 0o755)
    p_run = subprocess.run([PY, PLUGIN, "auto", "--prompt", "build it", "--", fake],
                           cwd=proj_run, capture_output=True, text=True, timeout=60)
    calls = open(os.path.join(proj_run, "calls.txt")).read().splitlines()
    check("auto-runner-loops-until-no-handoff",
          calls == ["1 build it", "1 resume", "1 resume"] and p_run.returncode == 0,
          "calls=%r stderr=%r" % (calls, p_run.stderr[-300:]))
    p_run_max = subprocess.run([PY, PLUGIN, "auto", "--max", "1", "--", "true"],
                               cwd=proj_run, capture_output=True, text=True, timeout=60)
    check("auto-runner-usage", subprocess.run([PY, PLUGIN, "auto"], capture_output=True,
                                              text=True).returncode == 2
          and p_run_max.returncode == 0)

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL WATCHER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
