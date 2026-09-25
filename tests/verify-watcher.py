#!/usr/bin/env python3
"""Executed verification for the watcher fix lane.

Runs context_watch.py as a subprocess against synthetic transcripts and
asserts the fixed behaviors. Run from the repo root (worktree)."""
import ast
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

    # 5. Latch: same session id fires once
    p2 = run_hook(evt, envx)
    check("latch-fires-once", p2.returncode == 0 and p2.stdout.strip() == "",
          "stdout=%r" % p2.stdout[:200])

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
    check("codex-trigger-stdout", p4.returncode == 0 and "[context-watch]" in p4.stdout,
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

    # 11. Codex PostToolUse: verified 0.157 contract — JSON additionalContext
    #     on exit 0 keeps the tool result; the exit-2/stderr channel discards it.
    rollout_pt = os.path.join(tmp, "codex-post.jsonl")
    with open(rollout_pt, "w") as f:
        f.write(json.dumps({"payload": {"type": "token_count",
                                        "info": {"last_token_usage": {"total_tokens": 150000},
                                                 "model_context_window": 272000}}}) + "\n")
    p_pt = run_hook({"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
                     "transcript_path": rollout_pt, "tool_response": "ok"},
                    {"CONTEXT_WATCH_TOKENS": "100000", "CONTEXT_WATCH_LOG": "0",
                     "CONTEXT_WATCH_AGENT": "codex", "TMPDIR": latchdir})
    try:
        out_pt = json.loads(p_pt.stdout)
    except Exception:
        out_pt = {}
    check("codex-posttooluse-json-exit0-preserves-result",
          p_pt.returncode == 0 and p_pt.stderr == "" and "decision" not in out_pt
          and out_pt.get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUse"
          and "[context-watch]" in out_pt.get("hookSpecificOutput", {}).get("additionalContext", ""),
          "rc=%d stdout=%r stderr=%r" % (p_pt.returncode, p_pt.stdout[:200], p_pt.stderr[:200]))

    # 12. block mode is the only path that emits decision: block (discards the result).
    p_blk = run_hook({"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
                      "transcript_path": transcript, "cwd": tmp},
                     {"CONTEXT_WATCH_TOKENS": "100000", "CONTEXT_WATCH_LOG": "0", "CONTEXT_WATCH_MODE": "block",
                      "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir})
    try:
        out_blk = json.loads(p_blk.stdout)
    except Exception:
        out_blk = {}
    check("block-mode-opt-in-only", out_blk.get("decision") == "block" and "reason" in out_blk
          and "decision" not in json.loads(p.stdout), "stdout=%r" % p_blk.stdout[:200])

    # 13. Threshold is capped at window - reserve, and the source says so.
    log_cap = os.path.join(tmp, "events-cap.jsonl")
    p_cap = run_hook({"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
                      "transcript_path": transcript, "cwd": tmp},
                     {"CONTEXT_WATCH_TOKENS": "999999", "CONTEXT_WATCH_WINDOW": "150000",
                      "CONTEXT_WATCH_RESERVE": "20000", "CONTEXT_WATCH_LOG": log_cap,
                      "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir})
    try:
        rec_cap = json.loads(open(log_cap).read().splitlines()[-1])
    except Exception:
        rec_cap = {}
    check("threshold-capped-to-window-minus-reserve",
          rec_cap.get("threshold") == 130000 and "capped" in rec_cap.get("threshold_source", "")
          and rec_cap.get("threshold_source", "").startswith("TOKENS")
          and rec_cap.get("window_source") == "assumed"
          and "window 150,000 assumed" in json.loads(p_cap.stdout)["hookSpecificOutput"]["additionalContext"],
          "record=%r stdout=%r" % (rec_cap, p_cap.stdout[:300]))
    p_pct = run_hook({"hook_event_name": "PostToolUse", "session_id": "verify-" + uuid.uuid4().hex[:8],
                      "transcript_path": transcript, "cwd": tmp},
                     {"CONTEXT_WATCH_PERCENT": "250", "CONTEXT_WATCH_LOG": log_cap, "CONTEXT_WATCH_WINDOW": "200000",
                      "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir})
    rec_pct = json.loads(open(log_cap).read().splitlines()[-1])
    check("percent-out-of-range-falls-through", rec_pct.get("threshold_source") == "builtin-default"
          and rec_pct.get("threshold") == 130000, "record=%r" % rec_pct)
    rec_codex = json.loads(open(log2).read().splitlines()[-1])
    check("codex-window-reported-in-record", rec_codex.get("window") == 272000
          and rec_codex.get("window_source") == "reported", "record=%r" % rec_codex)

    # 14. Latch: unknown session ids do not share one latch; compaction re-arms;
    #     a notice that produced no handoff repeats once occupancy grows; a
    #     written handoff keeps it quiet.
    t_a = os.path.join(tmp, "unknown-a.jsonl")
    t_b = os.path.join(tmp, "unknown-b.jsonl")
    for t in (t_a, t_b):
        shutil.copyfile(transcript, t)
    env_unk = {"CONTEXT_WATCH_TOKENS": "100000", "CONTEXT_WATCH_LOG": "0",
               "CONTEXT_WATCH_PENDING": "0", "CONTEXT_WATCH_AGENT": "claude", "TMPDIR": latchdir}
    p_a = run_hook({"hook_event_name": "PostToolUse", "transcript_path": t_a, "cwd": tmp}, env_unk)
    p_b = run_hook({"hook_event_name": "PostToolUse", "transcript_path": t_b, "cwd": tmp}, env_unk)
    check("latch-unknown-session-not-shared", "[context-watch]" in p_a.stdout and "[context-watch]" in p_b.stdout,
          "a=%r b=%r" % (p_a.stdout[:100], p_b.stdout[:100]))

    sid_c = "verify-compact-" + uuid.uuid4().hex[:8]
    proj_c = os.path.join(tmp, "proj-compact")
    os.makedirs(os.path.join(proj_c, ".handoffs"))
    ev_c = {"hook_event_name": "PostToolUse", "session_id": sid_c, "transcript_path": transcript, "cwd": proj_c}
    p1 = run_hook(ev_c, env_unk)
    p2 = run_hook(ev_c, env_unk)
    p_compact = run_hook({"hook_event_name": "SessionStart", "source": "compact", "session_id": sid_c,
                          "cwd": proj_c}, env_unk)
    p3 = run_hook(ev_c, env_unk)
    check("compaction-rearms-latch",
          "[context-watch]" in p1.stdout and p2.stdout.strip() == "" and "[context-watch]" in p3.stdout,
          "p1=%r p2=%r p3=%r" % (p1.stdout[:80], p2.stdout[:80], p3.stdout[:80]))
    check("compaction-note-keeps-authorized-task",
          "authorized" in p_compact.stdout and "evidence" in p_compact.stdout
          and "demo-topic" not in p_compact.stdout, "stdout=%r" % p_compact.stdout[:300])

    grown = os.path.join(tmp, "claude-grown.jsonl")
    with open(grown, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 60000, "cache_creation_input_tokens": 5000,
                                                  "cache_read_input_tokens": 100000, "output_tokens": 2000}}}) + "\n")
    p4 = run_hook(dict(ev_c, transcript_path=grown), env_unk)
    check("repeat-notice-when-no-handoff-written", "REPEAT NOTICE" in p4.stdout, "stdout=%r" % p4.stdout[:200])
    p5 = run_hook(dict(ev_c, transcript_path=grown), env_unk)
    check("repeat-notice-latches-again", p5.stdout.strip() == "", "stdout=%r" % p5.stdout[:200])
    time.sleep(0.05)
    with open(os.path.join(proj_c, ".handoffs", "20260811-1200-written.md"), "w") as f:
        f.write("---\ntopic: written\nstatus: open\n---\n")
    grown2 = os.path.join(tmp, "claude-grown2.jsonl")
    with open(grown2, "w") as f:
        f.write(json.dumps({"message": {"model": "claude-opus-4",
                                        "usage": {"input_tokens": 90000, "cache_creation_input_tokens": 5000,
                                                  "cache_read_input_tokens": 100000, "output_tokens": 2000}}}) + "\n")
    p6 = run_hook(dict(ev_c, transcript_path=grown2), env_unk)
    check("written-handoff-suppresses-repeat", p6.stdout.strip() == "", "stdout=%r" % p6.stdout[:200])

    # 15. Announcer surfaces stale, quarantined and claimed handoffs instead
    #     of hiding them, and never autoresumes a flagged one.
    proj_s = os.path.join(tmp, "proj-stale")
    os.makedirs(os.path.join(proj_s, ".handoffs"))
    stale_p = os.path.join(proj_s, ".handoffs", "20260101-0900-stale-topic.md")
    with open(stale_p, "w") as f:
        f.write("---\ntopic: stale-topic\nstatus: open\n---\n")
    old_t = time.time() - 40 * 86400
    os.utime(stale_p, (old_t, old_t))
    p_stale = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_s,
                        "session_id": "sess-S"}, {"AUTORESUME": "1", "TMPDIR": latchdir})
    check("announcer-stale-shown-not-hidden", "stale-topic" in p_stale.stdout and "stale: older" in p_stale.stdout
          and "without asking" not in p_stale.stdout and "confirm with the user" in p_stale.stdout,
          "stdout=%r" % p_stale.stdout[:500])
    check("announcer-claim-commands-carry-session",
          "claim" in p_stale.stdout and "--owner sess-S" in p_stale.stdout and "resume" in p_stale.stdout
          and "installed skill inventory" in p_stale.stdout, "stdout=%r" % p_stale.stdout[:800])

    proj_q = os.path.join(tmp, "proj-q")
    home_q = os.path.join(tmp, "home-q")
    os.makedirs(proj_q)
    os.makedirs(os.path.join(home_q, ".claude", "handoffs", "proj-q"))
    with open(os.path.join(home_q, ".claude", "handoffs", "proj-q", "20260811-0900-ghost.md"), "w") as f:
        f.write("---\ntopic: ghost\nstatus: open\n---\n")
    p_q = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_q, "session_id": "s"},
                   {"TMPDIR": latchdir, "HOME": home_q, "USERPROFILE": home_q})
    check("announcer-quarantined-not-offered", "quarantined" in p_q.stdout and "recover-legacy" in p_q.stdout
          and "awaiting resume" not in p_q.stdout, "stdout=%r" % p_q.stdout[:500])

    proj_cl = os.path.join(tmp, "proj-claim")
    os.makedirs(os.path.join(proj_cl, ".handoffs"))
    with open(os.path.join(proj_cl, ".handoffs", "20260811-0900-taken.md"), "w") as f:
        f.write("---\ntopic: taken\nstatus: resuming\nclaim_owner: other\nclaim_session: other\nclaim_at: %s\n---\n"
                % time.strftime("%Y-%m-%dT%H:%M:%S"))
    p_cl = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_cl, "session_id": "s"},
                    {"TMPDIR": latchdir})
    check("announcer-live-claim-not-offered", "another live session" in p_cl.stdout and "awaiting resume" not in p_cl.stdout,
          "stdout=%r" % p_cl.stdout[:400])
    p_cl2 = run_hook({"hook_event_name": "SessionStart", "source": "startup", "cwd": proj_cl, "session_id": "s"},
                     {"TMPDIR": latchdir, "CONTEXT_WATCH_CLAIM_TTL_MIN": "0"})
    check("announcer-stale-claim-offered-recoverable", "awaiting resume" in p_cl2.stdout and "STALE claim by other" in p_cl2.stdout,
          "stdout=%r" % p_cl2.stdout[:400])

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL WATCHER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
