#!/usr/bin/env python3
"""context-watch: deterministic context-occupancy watcher for Claude Code, Cowork, and Codex.

Purpose: QUALITY preservation, not cost containment. Reasoning quality degrades
as the context window fills, regardless of how cheaply those tokens were served
— cache reads are billed at a discount but occupy the window at full size, so
they count in full here. The watcher measures window occupancy and, past a
per-model absolute threshold, injects a one-time instruction to invoke the
session-handoff skill. Any cost savings are a logged byproduct (see analytics),
not the objective.

Hook events: PostToolUse + UserPromptSubmit (threshold watch), SessionStart
(open-handoff announcer, see handoff_ledger.py).

Occupancy measure (what the window holds heading into the NEXT call):
  Claude Code: last main-chain usage -> input_tokens + cache_creation_input_tokens
               + cache_read_input_tokens + output_tokens
  Codex:       last token_count event -> last_token_usage.total_tokens
               (input incl. cached + output + reasoning)
  Plus a pending estimate: the tool result (PostToolUse) or new prompt
  (UserPromptSubmit) already in this hook's stdin but not yet in any usage
  entry, estimated at ~4 chars/token. Biases the trigger early, never late.

Threshold resolution (first match wins; model id matched by longest substring):
  1. HANDOFF_AT                 global absolute, highest-precedence override
  2. CONTEXT_WATCH_TOKENS_MAP   e.g. "opus=120000,sonnet=140000,gpt-5.5=180000"
  3. ./.context-watch.json      per-model keys (project-local)
  4. ~/.context-watch/thresholds.json   per-model keys (user-global)
  5. CONTEXT_WATCH_TOKENS       global absolute
  6. "default" key in the config files
  7. CONTEXT_WATCH_PERCENT x window     only if PERCENT is explicitly set
  8. built-in default: 130,000 tokens

Config file format (flat; keys are case-insensitive substrings of model ids):
  {"claude-opus": 120000, "claude-sonnet": 140000, "gpt-5.5": 180000,
   "default": 130000}

Other environment variables:
  CONTEXT_WATCH_WINDOW        window size for the PERCENT path (default 200000;
                              Codex reports its own window and overrides this)
  CONTEXT_WATCH_SKILL         skill named in the instruction (default: session-handoff)
  CONTEXT_WATCH_MODE          warn | block (default: warn)
  CONTEXT_WATCH_AGENT         claude | codex (default: auto-detect)
  CONTEXT_WATCH_DISABLE       1 to no-op
  CONTEXT_WATCH_PENDING       0 to disable the pending-content estimate
  CONTEXT_WATCH_LOG           analytics path (default ~/.context-watch/events.jsonl;
                              0 to disable)
  CONTEXT_WATCH_MAX_AGE_DAYS  announcer: ignore open handoffs older than this (14)
  AUTORESUME                  announcer + trigger note: true resumes a single open
                              handoff without asking after /clear
  CONTEXT_WATCH_AUTORESUME    legacy alias for AUTORESUME

CLI: `context_watch.py stats` summarizes the analytics log.

Stdlib only. Fails open: any error exits 0 so the watcher can never break a
session. Fires once per session via a latch file in the temp directory.
"""

import json
import os
import sys
import tempfile
import time

TAIL_BYTES = 2_000_000   # only scan the tail of large transcripts
DEFAULT_TOKENS = 130_000  # built-in fallback; quality degradation commonly ~120-140k


def env(name, default=""):
    return os.environ.get(name, default)


def autoresume_on():
    return (env("AUTORESUME") or env("CONTEXT_WATCH_AUTORESUME")).strip().lower() in (
        "1", "true", "yes", "on")


# ---------------------------------------------------------------- transcript

def read_event():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def load_transcript_tail(path):
    entries = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(-TAIL_BYTES, os.SEEK_END)
                f.readline()  # discard the partial first line
            for raw in f:
                try:
                    entries.append(json.loads(raw))
                except Exception:
                    continue
    except Exception:
        pass
    return entries


def detect_agent(evt, entries):
    forced = env("CONTEXT_WATCH_AGENT").strip().lower()
    if forced in ("claude", "codex"):
        return forced
    path = evt.get("transcript_path") or ""
    if ".codex" in path:
        return "codex"
    if ".claude" in path:
        return "claude"
    for e in entries:
        payload = e.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return "codex"
        msg = e.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            return "claude"
    return "claude"


def claude_usage(entries):
    """(breakdown, model) from the last main-chain API call."""
    last, model = None, None
    for e in entries:
        if e.get("isSidechain"):
            continue
        msg = e.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict) and "input_tokens" in usage:
                last = usage
                model = msg.get("model") or model
    if not last:
        return None, model
    breakdown = {
        "input": last.get("input_tokens") or 0,
        "cache_creation": last.get("cache_creation_input_tokens") or 0,
        "cache_read": last.get("cache_read_input_tokens") or 0,
        "output": last.get("output_tokens") or 0,
    }
    breakdown["occupancy"] = (breakdown["input"] + breakdown["cache_creation"]
                              + breakdown["cache_read"] + breakdown["output"])
    return breakdown, model


def codex_usage(entries):
    """(breakdown, window, model) from the last token_count / turn_context events."""
    last, window, model = None, None, None
    for e in entries:
        payload = e.get("payload")
        if isinstance(payload, dict):
            if payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = info.get("last_token_usage")
                if isinstance(usage, dict):
                    last = usage
                w = info.get("model_context_window")
                if isinstance(w, int) and w > 0:
                    window = w
        if e.get("type") == "turn_context" and isinstance(payload, dict) and payload.get("model"):
            model = payload["model"]
    if not last:
        return None, window, model
    total = last.get("total_tokens") or (
        (last.get("input_tokens") or 0) + (last.get("output_tokens") or 0)
    )
    breakdown = {
        "input": last.get("input_tokens") or 0,
        "cache_read": last.get("cached_input_tokens") or 0,
        "output": last.get("output_tokens") or 0,
        "reasoning": last.get("reasoning_output_tokens") or 0,
        "occupancy": total,
    }
    return breakdown, window, model


def estimate_pending(evt):
    """Tokens already in this hook's stdin but not yet in any usage entry:
    the just-produced tool result (PostToolUse) or the new prompt
    (UserPromptSubmit). ~4 chars/token; a deliberate early-bias margin."""
    if env("CONTEXT_WATCH_PENDING", "1") == "0":
        return 0
    blob = None
    for key in ("tool_response", "tool_output", "tool_result", "prompt"):
        if key in evt and evt[key] is not None:
            blob = evt[key]
            break
    if blob is None:
        return 0
    try:
        text = blob if isinstance(blob, str) else json.dumps(blob)
        return len(text) // 4
    except Exception:
        return 0


# ---------------------------------------------------------------- thresholds

def _parse_map(raw):
    out = {}
    for part in raw.split(","):
        if "=" in part:
            key, _, val = part.partition("=")
            key, val = key.strip().lower(), val.strip()
            if key and val.isdigit():
                out[key] = int(val)
    return out


def _load_config_files(cwd):
    merged = {}
    for path in (
        os.path.join(os.path.expanduser("~"), ".context-watch", "thresholds.json"),
        os.path.join(cwd or os.getcwd(), ".context-watch.json"),
    ):  # user-global first so project-local overrides on key collision
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, int):
                        merged[str(k).strip().lower()] = v
        except Exception:
            continue
    return merged


def _best_model_match(table, model):
    model_l = (model or "").lower()
    best_key, best_len = None, 0
    for key in table:
        if key != "default" and key in model_l and len(key) > best_len:
            best_key, best_len = key, len(key)
    return best_key


def resolve_threshold(model, window, cwd):
    """Return (limit, source_description). First match wins."""
    handoff_at = env("HANDOFF_AT").strip()
    if handoff_at.isdigit():
        return int(handoff_at), "HANDOFF_AT"

    env_map = _parse_map(env("CONTEXT_WATCH_TOKENS_MAP"))
    key = _best_model_match(env_map, model)
    if key:
        return env_map[key], "TOKENS_MAP:%s" % key

    files = _load_config_files(cwd)
    key = _best_model_match(files, model)
    if key:
        return files[key], "config:%s" % key

    raw = env("CONTEXT_WATCH_TOKENS").strip()
    if raw.isdigit():
        return int(raw), "TOKENS"

    if "default" in files:
        return files["default"], "config:default"

    pct_raw = env("CONTEXT_WATCH_PERCENT").strip()
    if pct_raw:
        try:
            return int(window * float(pct_raw) / 100.0), "PERCENT"
        except ValueError:
            pass

    return DEFAULT_TOKENS, "builtin-default"


# ---------------------------------------------------------------- analytics

def log_event(record):
    raw = env("CONTEXT_WATCH_LOG")
    if raw == "0":
        return
    path = raw or os.path.join(os.path.expanduser("~"), ".context-watch", "events.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def stats_cli():
    raw = env("CONTEXT_WATCH_LOG")
    if raw == "0":
        print("analytics log disabled")
        return 0
    path = raw or os.path.join(
        os.path.expanduser("~"), ".context-watch", "events.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            events = [json.loads(l) for l in f if l.strip()]
    except Exception:
        print("no analytics log at %s" % path)
        return 0
    if not events:
        print("analytics log is empty")
        return 0
    print("%d handoff trigger(s) logged (%s)" % (len(events), path))
    by_model = {}
    for e in events:
        by_model.setdefault(e.get("model") or "unknown", []).append(e)
    for model, evs in sorted(by_model.items()):
        occ = [e.get("occupancy", 0) for e in evs]
        cache = [e.get("breakdown", {}).get("cache_read", 0) for e in evs]
        avg_occ = sum(occ) / len(occ)
        cache_share = (sum(cache) / sum(occ) * 100.0) if sum(occ) else 0.0
        print("  %-32s n=%-3d avg trigger %8.0f tok  cache-read share %4.1f%%"
              % (model, len(evs), avg_occ, cache_share))
    print("last event: %s" % json.dumps(events[-1]))
    return 0


# ---------------------------------------------------------------- announcer

def handle_session_start(evt, agent):
    """Announce open (untransferred) handoffs; enumerate and offer a choice when several exist."""
    if (evt.get("source") or "") in ("resume", "compact", "fork"):
        sys.exit(0)  # a resumed session already has its context
    cwd = evt.get("cwd") or os.getcwd()
    here = os.path.dirname(os.path.abspath(__file__))
    ledger = os.path.join(here, "handoff_ledger.py")
    try:
        max_age = int(env("CONTEXT_WATCH_MAX_AGE_DAYS", "14"))
    except ValueError:
        max_age = 14

    open_handoffs = []
    try:
        if here not in sys.path:
            sys.path.insert(0, here)
        import handoff_ledger
        open_handoffs = handoff_ledger.scan(cwd, max_age)
    except Exception:
        path = os.path.join(cwd, "HANDOFF.md")
        try:
            if os.path.isfile(path):
                mtime = os.path.getmtime(path)
                age_days = (time.time() - mtime) / 86400.0
                if age_days <= max_age:
                    open_handoffs = [{"path": path, "topic": "default",
                                      "ended": time.strftime("%Y-%m-%dT%H:%M", time.localtime(mtime)),
                                      "age_days": round(age_days, 1),
                                      "description": "", "skills": ""}]
        except Exception:
            pass

    if not open_handoffs:
        sys.exit(0)

    mark = ("After a handoff has been resumed, mark it transferred so future "
            "sessions stop announcing it: python3 %s resume <path>" % ledger)
    defer = ("If the user's opening request is an unrelated explicit task, mention the "
             "open handoff(s) in one sentence and proceed with their task instead.")

    if len(open_handoffs) == 1:
        h = open_handoffs[0]
        desc = h.get("description") or ""
        suffix = " — %s" % desc if desc else ""
        if autoresume_on():
            action = "Read it in full and resume it immediately without asking. "
        else:
            action = ("Read it in full before doing anything else, then continue the "
                      "work it describes. ")
        skills = h.get("skills") or ""
        skills_note = ""
        if skills:
            skills_note = ("First load exactly these skills via the Skill tool, in "
                           "order, before resuming: %s. " % skills)
        note = ("context-watch: One open handoff awaiting resume: '%s' (%.0fd old, ended %s) at %s%s. "
                % (h["topic"], h["age_days"], h.get("ended") or "unknown", h["path"], suffix)
                + action + skills_note + mark + " " + defer)
    else:
        listing = "; ".join(
            "%d) %s (%.0fd old, ended %s, %s)%s%s" % (
                i + 1, h["topic"], h["age_days"], h.get("ended") or "unknown", h["path"],
                " — %s" % h.get("description") if h.get("description") else "",
                " [skills: %s]" % h.get("skills") if h.get("skills") else "")
            for i, h in enumerate(open_handoffs)
        )
        note = ("context-watch: %d open handoffs awaiting resume: %s. Before any other "
                "work, present this list and ask the user which one to resume (use an "
                "interactive question tool if available), or none. %s %s"
                % (len(open_handoffs), listing, mark, defer))

    # Always emit SessionStart JSON for both Claude and Codex. Newer Codex treats
    # stdout that looks like JSON (leading "[" or "{") as JSON; a plain-text note
    # starting with "[" therefore fails parse. Empty stdout, non-JSON plain text,
    # and valid SessionStart JSON are all fine.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": note,
        }
    }))
    sys.exit(0)


# ---------------------------------------------------------------- emit

def build_message(occupancy, pending, breakdown, limit, source, model, skill):
    cache_read = breakdown.get("cache_read", 0)
    cache_share = (cache_read / occupancy * 100.0) if occupancy else 0.0
    detail = "cache-read %s of it (%.0f%%)" % (format(cache_read, ","), cache_share)
    if pending:
        detail += "; incl. ~%s pending" % format(pending, ",")
    message = (
        "context-watch: Context occupancy ~%s tokens, over the %s-token threshold "
        "for %s [%s] (%s). Finish only the action currently in progress, then "
        "immediately invoke the `%s` skill: write the handoff document and stop. "
        "Do not begin any new work."
        % (format(occupancy, ","), format(limit, ","), model or "unknown model",
           source, detail, skill)
    )
    if autoresume_on():
        message += (" Autoresume is active: after the handoff is written, tell the user "
                    "to type /clear — the cleared session will announce the open handoff "
                    "and resume it automatically.")
    return message


def emit(agent, event_name, message, mode):
    if agent == "claude":
        if mode == "block" and event_name == "PostToolUse":
            print(json.dumps({"decision": "block", "reason": message}))
        else:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": message,
                }
            }))
        sys.exit(0)
    if event_name == "UserPromptSubmit":
        print(message)  # Codex: stdout becomes extra context for the turn
        sys.exit(0)
    # Codex PostToolUse: exit 2 + stderr is the documented injection channel.
    # It replaces this one tool result — acceptable, the instruction is to stop.
    print(message, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- main

def main():
    if env("CONTEXT_WATCH_DISABLE") == "1":
        sys.exit(0)

    evt = read_event()
    event_name = evt.get("hook_event_name") or ""
    transcript_path = evt.get("transcript_path") or ""
    entries = load_transcript_tail(transcript_path) if transcript_path else []
    agent = detect_agent(evt, entries)

    if event_name == "SessionStart":
        handle_session_start(evt, agent)

    if event_name not in ("PostToolUse", "UserPromptSubmit"):
        sys.exit(0)

    session_id = str(evt.get("session_id") or "unknown")
    latch = os.path.join(
        tempfile.gettempdir(),
        "context-watch-%s.fired" % "".join(
            c if c.isalnum() or c in "-_" else "_" for c in session_id),
    )
    if os.path.exists(latch):
        sys.exit(0)

    try:
        window = int(env("CONTEXT_WATCH_WINDOW", "200000"))
    except ValueError:
        window = 200000

    model = evt.get("model")  # Codex includes it in hook input; Claude Code does not
    if agent == "claude":
        breakdown, tmodel = claude_usage(entries)
    else:
        breakdown, reported, tmodel = codex_usage(entries)
        if reported:
            window = reported
    model = model or tmodel

    if not breakdown or breakdown.get("occupancy", 0) <= 0:
        sys.exit(0)

    pending = estimate_pending(evt)
    occupancy = breakdown["occupancy"] + pending

    cwd = evt.get("cwd") or os.getcwd()
    limit, source = resolve_threshold(model, window, cwd)
    if occupancy < limit:
        sys.exit(0)

    try:
        fd = os.open(latch, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write("%d/%d\n" % (occupancy, limit))
    except FileExistsError:
        sys.exit(0)
    except Exception:
        pass

    log_event({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent,
        "model": model,
        "occupancy": occupancy,
        "pending_estimate": pending,
        "threshold": limit,
        "threshold_source": source,
        "breakdown": breakdown,
        "session_id": session_id,
        "cwd": cwd,
    })

    skill = env("CONTEXT_WATCH_SKILL", "session-handoff")
    mode = env("CONTEXT_WATCH_MODE", "warn").strip().lower()
    emit(agent, event_name,
         build_message(occupancy, pending, breakdown, limit, source, model, skill),
         mode)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        try:
            sys.exit(stats_cli())
        except BrokenPipeError:
            sys.exit(0)  # downstream pipe (grep -q, head) closed early — fine
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)  # fail open — never break the session
