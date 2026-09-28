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
(open-handoff announcer, see handoff_ledger.py), Stop (fully-automatic mode only:
clears a tmux-hosted session once its handoff is written).

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
  CONTEXT_WATCH_THINKING      tokens assumed for the not-yet-written current
                              response (default: session's largest response,
                              capped at 25000; Claude only)
  CONTEXT_WATCH_JEV           0 to skip asking Jev (TypeSafe System One,
                              TYPESAFE_API_KEY) whether a session's opening
                              prompt continues an open handoff
  CONTEXT_WATCH_LOG           analytics path (default ~/.context-watch/events.jsonl;
                              0 to disable)
  CONTEXT_WATCH_MAX_AGE_DAYS  announcer: ignore open handoffs older than this (14)
  AUTORESUME                  announcer + trigger note: true resumes a single open
                              handoff without asking after /clear
  CONTEXT_WATCH_AUTORESUME    legacy alias for AUTORESUME
  HANDOFF_AUTO                fully automatic: implies AUTORESUME, the newest open
                              handoff wins without asking, the agent ends its turn
                              after writing the handoff, and the Stop hook types
                              /clear + "resume" into the session's tmux pane
                              (or use the `auto` runner below for headless runs)
  HANDOFF_AUTO_MAX            automatic clears in a row per project before the
                              Stop hook stops and tells the user (default 10);
                              a turn that ends without the trigger resets it

CLI: `context_watch.py stats` summarizes the analytics log.
     `context_watch.py auto [--max N] [--prompt TEXT] -- <agent command...>`
     runs a headless agent (e.g. `claude -p`, `codex exec`) in a loop with
     HANDOFF_AUTO=1: each run that leaves a new open handoff is followed by a
     fresh run given the prompt "resume"; the loop ends when a run leaves none.

Stdlib only. Fails open: any error exits 0 so the watcher can never break a
session. Fires at the threshold, and once more when occupancy has grown by a
further 25% of it (and to at least 125% of it) AND the model has taken a turn
since the first notice (latch files in the temp directory); a drop below 50%
of it (compaction) re-arms both.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

TAIL_BYTES = 2_000_000   # only scan the tail of large transcripts
DEFAULT_TOKENS = 130_000  # built-in fallback; quality degradation commonly ~120-140k
# Pending-estimate guards. Claude Code caps a tool result at ~25k tokens before
# it enters context and Codex truncates harder, so a larger "pending" figure is
# hook-payload metadata (full original files, base64 images), not context.
PENDING_CAP = 25_000
CHARS_PER_TOKEN = 3       # measured ~3.2 on line-numbered code; 4 undershot by ~30%
IMAGE_TOKENS = 1_600      # an image/audio item costs ~1.6k tokens, not len(base64)/4
DATA_MEDIA_URL = re.compile(r"data:(?:image|audio)/[\w.+-]+(?:;[\w.+-]+)*;base64,[\w+/=-]+",
                            re.IGNORECASE)
MEDIA_URL_KEYS = ("image_url", "audio_url")
BASE64_MIN = 1_000
NOT_IN_CONTEXT_KEYS = ("originalFile", "structuredPatch", "originalContent")
SECOND_NOTICE_FACTOR = 1.25  # re-fire once at 125% of the threshold, and 25% past the first
FLOOR_MARGIN = 10_000        # a threshold within this (or 10%) of startup context is useless
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handoff_ledger.py")
REARM_FACTOR = 0.5           # occupancy below 50% (a compaction) re-arms the latch
DEFAULT_RESERVE = 20_000     # room kept free for writing the handoff (reported windows only)
STALE_USAGE_S = 600          # a usage entry older than this may not describe the window
LABEL = {"claude": "[context-watch]",
         # Newer Codex parses stdout that starts with "[" or "{" as JSON, so the
         # Codex label must not start with a bracket (see handle_session_start).
         "codex": "context-watch:"}


def env(name, default=""):
    return os.environ.get(name, default)


def _truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


def auto_mode_on():
    return _truthy(env("HANDOFF_AUTO"))


def autoresume_on():
    return auto_mode_on() or _truthy(env("AUTORESUME") or env("CONTEXT_WATCH_AUTORESUME"))


def _ledger():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import handoff_ledger
    return handoff_ledger


def session_key(evt):
    """The session id, else a hash of the transcript path, so sessions whose
    host omits the id never share one latch; None when neither is known."""
    sid = str(evt.get("session_id") or "").strip()
    if sid:
        return sid
    tp = evt.get("transcript_path") or ""
    return "t-" + hashlib.sha1(tp.encode("utf-8", "replace")).hexdigest()[:16] if tp else None


def latch_path(session_id, stage=1):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session_id))
    return os.path.join(tempfile.gettempdir(),
                        "context-watch-%s.fired%s" % (safe, "" if stage == 1 else stage))


def write_session_note(cwd, evt, fired):
    """Leave this session's facts where handoff_ledger.py new-path (run from
    the agent's shell, no hook stdin) can read them: reason and skills."""
    try:
        sid = session_key(evt)
        if not sid:
            return
        trigger = str(os.stat(latch_path(sid)).st_mtime_ns) if fired and os.path.exists(latch_path(sid)) else ''
        note = {"session_id": sid, "trigger_id": trigger, "cwd": cwd,
                "transcript_path": evt.get("transcript_path") or "",
                "fired": bool(fired), "ts": time.time()}
        path = _ledger().session_note_path(cwd, sid)
        previous = _ledger().read_session_note(cwd, session=sid)
        entries = load_transcript_tail(evt.get('transcript_path') or '') if evt.get('transcript_path') else []
        agent = detect_agent(evt, entries)
        usage = claude_usage(entries)[0] if agent == 'claude' else codex_usage(entries)[0]
        note['startup_input_tokens'] = previous.get('startup_input_tokens')
        def input_size(breakdown):
            if not breakdown:
                return None
            return (sum(breakdown.get(k, 0) for k in ('input', 'cache_creation', 'cache_read'))
                    if agent == 'claude' else breakdown.get('input'))
        # A tail is not the startup baseline. Only label the first input when the whole trace fits.
        transcript = evt.get('transcript_path')
        if note['startup_input_tokens'] is None and entries and transcript and os.path.getsize(transcript) <= TAIL_BYTES:
            usage_fn = claude_usage if agent == 'claude' else codex_usage
            note['startup_input_tokens'] = next((input_size(b) for e in entries
                if (b := usage_fn([e])[0]) is not None), None)
        note['observed_input_tokens'] = input_size(usage)
        tmp = "%s.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(note, f)
        os.replace(tmp, path)
    except Exception:
        pass


def read_first_latch(path):
    """(occupancy, transcript usage) recorded by the first notice; (0, None) if unknown."""
    try:
        occ, _limit, base = (int(x) for x in open(path).read().strip().split("/"))
        return occ, base
    except Exception:
        return 0, None


def auto_count_path(cwd):
    try:
        root = _ledger().project_root(cwd)
    except Exception:
        root = cwd
    safe = "".join(c if c.isalnum() else "_" for c in os.path.realpath(root))[-80:]
    return os.path.join(tempfile.gettempdir(), "context-watch-auto-%s.count" % safe)


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
    last, model, max_output = None, None, 0
    for e in entries:
        if e.get("isSidechain"):
            continue
        if (e.get("type") == "system" and e.get("subtype") == "compact_boundary") \
                or e.get("isCompactSummary"):
            last = None  # pre-compaction usage no longer describes the window
            continue
        msg = e.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict) and "input_tokens" in usage:
                last = usage
                model = msg.get("model") or model
                max_output = max(max_output, usage.get("output_tokens") or 0)
    if not last:
        return None, model
    breakdown = {
        "input": last.get("input_tokens") or 0,
        "cache_creation": last.get("cache_creation_input_tokens") or 0,
        "cache_read": last.get("cache_read_input_tokens") or 0,
        "output": last.get("output_tokens") or 0,
        "max_output": max_output,  # largest single response so far: thinking allowance
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


def floor_occupancy(entries, agent):
    """Occupancy at the session's first model call: the startup context a
    handoff cannot free. Callers pass entries only when the whole transcript
    was read, so the first usage entry really is the first."""
    usage = claude_usage if agent == "claude" else codex_usage
    for e in entries:
        breakdown = usage([e])[0]
        if breakdown and breakdown.get("occupancy", 0) > 0:
            return breakdown["occupancy"]
    return None


def usage_age_seconds(entries):
    """Seconds since the newest usage-bearing entry's timestamp, or None."""
    from datetime import datetime, timezone
    for e in reversed(entries):
        payload = e.get("payload")
        msg = e.get("message")
        if (isinstance(msg, dict) and isinstance(msg.get("usage"), dict)
                and not e.get("isSidechain")) or (
                isinstance(payload, dict) and payload.get("type") == "token_count"):
            try:
                dt = datetime.fromisoformat(str(e.get("timestamp")).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, int(time.time() - dt.timestamp()))
            except (TypeError, ValueError):
                return None
    return None


def handoff_written_since(cwd, since, session=None, require_open=False):
    """Only a validated publication for this session's trigger satisfies it."""
    if not session:
        return False
    try:
        ledger = _ledger()
        note = ledger.read_session_note(cwd, session=session)
        trigger = note.get('trigger_id')
        return bool(trigger) and any(
            r['fm'].get('session_id') == session and r['fm'].get('trigger_id') == trigger
            and r['fm'].get('checkpoint_id') and not r['problems']
            and (not require_open or r['fm'].get('status') == 'open')
            for r in ledger._records(ledger.project_root(cwd)))
    except OSError:
        return False


def _looks_base64(s):
    """Real base64 (standard or URL-safe) of binary data, not merely a long
    unbroken word: it mixes upper, lower and digits and uses the non-alnum
    alphabet ('+/' or '-_') somewhere in the first kilobyte."""
    if len(s) < BASE64_MIN:
        return False
    head = s[:BASE64_MIN].replace("\n", "").replace("\r", "")
    if not all(c.isalnum() or c in "+/=-_" for c in head):
        return False
    return (any(c in "+/-_" for c in head) and any(c.isupper() for c in head)
            and any(c.islower() for c in head) and any(c.isdigit() for c in head))


def _is_media_block(d):
    """A recognized media envelope: MCP/Claude {type: image|audio, data | source:
    {type: base64, data}} with a matching mime, or OpenAI {input_audio: {data}}."""
    kind = str(d.get("type") or "").lower()
    if kind in ("image", "audio"):
        mime = d.get("mimeType") or d.get("mime_type") or d.get("media_type") or ""
        if isinstance(d.get("data"), str) and str(mime).split("/")[0].lower() in (kind, ""):
            return True
        src = d.get("source")
        if isinstance(src, dict) and src.get("type") == "base64" and isinstance(src.get("data"), str):
            return True
    audio = d.get("input_audio")
    return isinstance(audio, dict) and isinstance(audio.get("data"), str)


def _context_tokens(obj):
    """~tokens the model will actually see from a hook payload value: string
    content at ~3 chars/token, media (base64 blobs, data: URLs, image/audio
    envelopes) at a flat cost each, and keys that are hook metadata (an Edit's
    full original file) skipped. A JSON-encoded string is decoded first."""
    if isinstance(obj, str):
        if _looks_base64(obj):
            return IMAGE_TOKENS
        if len(obj) > BASE64_MIN and obj.lstrip()[:1] in ("{", "["):
            try:
                decoded = json.loads(obj)
                if not isinstance(decoded, str):
                    return _context_tokens(decoded)
            except ValueError:
                pass
        media = len(DATA_MEDIA_URL.findall(obj))
        if media:
            return media * IMAGE_TOKENS + len(DATA_MEDIA_URL.sub("", obj)) // CHARS_PER_TOKEN
        return len(obj) // CHARS_PER_TOKEN
    if isinstance(obj, dict):
        if _is_media_block(obj):
            return IMAGE_TOKENS
        return sum(IMAGE_TOKENS if k in MEDIA_URL_KEYS and isinstance(v, (str, dict))
                   else _context_tokens(v)
                   for k, v in obj.items() if k not in NOT_IN_CONTEXT_KEYS)
    if isinstance(obj, list):
        return sum(_context_tokens(v) for v in obj)
    return 0


def unrecorded_results(entries, skip_id=None):
    """~tokens of tool results already in a Claude transcript but after its
    last usage entry. Claude Code writes an assistant message (and its usage)
    only after that tool call's PostToolUse hook has run, so earlier results
    since the last API call are otherwise invisible. skip_id is the current
    tool call, whose result estimate_pending already counts."""
    total = 0
    for e in entries:
        if e.get("isSidechain"):
            continue
        if (e.get("type") == "system" and e.get("subtype") == "compact_boundary") \
                or e.get("isCompactSummary"):
            total = 0
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        if isinstance(msg.get("usage"), dict) and "input_tokens" in msg["usage"]:
            total = 0
            continue
        content = msg.get("content")
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_result" \
                    and block.get("tool_use_id") != skip_id:
                total += min(_context_tokens(block.get("content")), PENDING_CAP)
    return total


def estimate_pending(evt):
    """(capped, raw) tokens already in this hook's stdin but not yet in any
    usage entry: the just-produced tool result (PostToolUse) or the new prompt
    (UserPromptSubmit). Biased early, but capped: a harness never admits more
    than ~PENDING_CAP tokens of one tool result into context."""
    if env("CONTEXT_WATCH_PENDING", "1") == "0":
        return 0, 0
    for key in ("tool_response", "tool_output", "tool_result", "prompt"):
        if evt.get(key) is not None:
            try:
                raw = _context_tokens(evt[key])
            except Exception:
                return 0, 0
            if key == "prompt":
                return raw, raw  # a pasted prompt enters context whole
            return min(raw, PENDING_CAP), raw
    return 0, 0


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
    untuned = []
    for model, evs in sorted(by_model.items()):
        occ = [e.get("occupancy", 0) for e in evs]
        cache = [e.get("breakdown", {}).get("cache_read", 0) for e in evs]
        avg_occ = sum(occ) / len(occ)
        cache_share = (sum(cache) / sum(occ) * 100.0) if sum(occ) else 0.0
        # triggers that only crossed the threshold because of the pending estimate
        by_pending = sum(1 for e in evs if e.get("occupancy", 0) - e.get("pending_estimate", 0)
                         < e.get("threshold", 0))
        print("  %-32s n=%-3d avg trigger %8.0f tok  cache-read share %4.1f%%  pending-driven %d"
              % (model, len(evs), avg_occ, cache_share, by_pending))
        if all(e.get("threshold_source") == "builtin-default" for e in evs):
            untuned.append(model)
    if untuned:
        print("untuned: %s used only the built-in %s-token default. Set per-model values in "
              "~/.context-watch/thresholds.json (see hooks/thresholds.example.json) where each "
              "model's quality actually drops." % (", ".join(untuned), format(DEFAULT_TOKENS, ",")))
    print("last event: %s" % json.dumps(events[-1]))
    return 0


# ---------------------------------------------------------------- announcer

ANNOUNCE_CAP = 5  # handoffs listed by name; the rest are counted


def handle_session_start(evt, agent):
    """Announce open (untransferred) handoffs; enumerate and offer a choice when several exist."""
    source = evt.get("source") or ""
    if source == "compact":
        from handoff_protocol import receipt_path
        sid = session_key(evt)
        if sid:
            try:
                os.unlink(receipt_path(_ledger().project_root(evt.get('cwd') or os.getcwd()), sid) + '.delivery')
            except FileNotFoundError:
                pass
        note = ("handoff-status: Context was just compacted in this "
                "session. The summary above is evidence, not the live state: re-verify the "
                "workspace, revision and test results before relying on it. Compaction is "
                "automatic pressure, not the user parking the work: if the user had "
                "authorized a task that was in progress, continue it.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": note}}))
        sys.exit(0)
    if source in ("resume", "fork"):
        sys.exit(0)  # a resumed session already has its context
    cwd = evt.get("cwd") or os.getcwd()
    write_session_note(cwd, evt, False)
    from handoff_protocol import lookup, opening_action
    result = lookup(cwd)
    policy = opening_action('', result, session_key(evt), auto=autoresume_on(), newest=auto_mode_on())
    label = LABEL.get(agent, LABEL['claude']) if result['outcome'] == 'available' and autoresume_on() else 'handoff-status:'
    note = label + ' ' + policy + (' This status requires no skill loading.' if label == 'handoff-status:' else '')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': note}}))
    sys.exit(0)


# ---------------------------------------------------------------- stop nudge

def nudge_unwritten_handoff(evt):
    """Warn mode: the trigger fired this session, the turn is ending, and no
    handoff was written since. Block the stop once so the agent writes it now
    instead of the notice being lost with the turn. Claude only (Codex Stop
    hooks cannot block)."""
    if evt.get("stop_hook_active") or detect_agent(evt, []) != "claude":
        return
    key = session_key(evt)
    latch = latch_path(key) if key else None
    if not latch or not os.path.exists(latch):
        return
    second = latch_path(key, 2)
    if os.path.exists(second) and os.path.getsize(second) == 0:
        return  # floor note: no handoff was asked for
    cwd = evt.get("cwd") or os.getcwd()
    if handoff_written_since(cwd, os.path.getmtime(latch) - 60, key):
        return
    try:
        os.close(os.open(latch + ".nudged", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return  # nudged once already, or unwritable temp dir
    import shlex
    print(json.dumps({"decision": "block", "reason": (
        "[context-watch] No published checkpoint matches this session's trigger. "
        "For the JSON draft format run python3 %s save --template. Preserve the draft and publish with python3 %s save --session %s "
        "--request-id <stable-checkpoint-id> --input <draft.json>; stop after outcome saved. "
        "If blocked, report the failed location and keep this session; do not clear."
        % (shlex.quote(LEDGER), shlex.quote(LEDGER), shlex.quote(key)))}))
    sys.exit(0)


# ---------------------------------------------------------------- opening-prompt routing

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MIN = 0.8  # confidence below which the router stays silent (skill default applies)


def jev_handoff_route(prompt, handoffs, ask=None):
    """Ask Jev whether the session's opening prompt continues one of the open
    handoffs or is unrelated work. Returns (topic | "unrelated", confidence)
    or None. ask(body) -> response dict is injectable for tests; the default
    posts to TypeSafe with a 3 s timeout and fails open."""
    key = env("TYPESAFE_API_KEY")
    if not _truthy(env("CONTEXT_WATCH_JEV", "0")) or not prompt.strip() or not (ask or key):
        return None
    criteria = {h["topic"]: (h.get("description") or h["topic"])[:200] for h in handoffs}
    criteria["unrelated"] = "The prompt is a different, explicit task; it continues none of these."
    body = {"model": "jev-latest", "state": {"user_prompt": prompt[:4000]},
            "questions": {"route": {"type": "choice", "criteria": criteria, "instructions":
                          "Which parked handoff does the user's opening prompt clearly continue "
                          "or ask to resume? Choose unrelated unless one clearly matches."}}}
    try:
        if ask:
            resp = ask(body)
        else:
            import urllib.request
            req = urllib.request.Request(JEV_URL, json.dumps(body).encode(),
                                         {"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json",
                                          "User-Agent": "context-watch/1"})
            with urllib.request.urlopen(req, timeout=3) as r:
                resp = json.load(r)
        answer = resp["answers"]["route"]
        choice, conf = answer["choice"], float(answer["confidence"])
        if conf < JEV_MIN or choice not in criteria:
            return None
        return choice, conf
    except Exception:  # ponytail: no network, 401, timeout, odd shape -> silent
        return None


def route_opening_prompt(evt, agent):
    """First prompt of a session with open handoffs: tell the agent whether to
    claim one or leave them alone, so it neither resumes unrelated work nor
    asks which of one."""
    cwd = evt.get("cwd") or os.getcwd()
    from handoff_protocol import lookup, opening_action, generic_intent
    prompt = str(evt.get('prompt') or '')
    import re
    direct = None if generic_intent(prompt) else re.fullmatch(r'(resume|retrieve) ([a-z0-9-]+|/[^\n]+)', prompt.strip(), re.I)
    result = lookup(cwd, topic=direct[2] if direct else None)
    policy = opening_action(prompt, result, session_key(evt), auto=autoresume_on(), newest=auto_mode_on())
    actionable = result['outcome'] == 'available'
    if policy is None and direct:
        policy = result['action']
    if policy is None:
        # Optional inference is attempted once per project/session, including failures.
        # A missing transcript is not evidence that every prompt is a new session.
        sid = session_key(evt)
        if (result['outcome'] != 'available' or not sid or not prompt.strip()
                or not _truthy(env('CONTEXT_WATCH_JEV', '0')) or not env('TYPESAFE_API_KEY')):
            return
        try:
            os.close(os.open(_ledger().session_note_path(cwd, sid) + '.routed',
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except OSError:
            return  # Cannot reserve a unique attempt: stay local, without retries.
        hit = jev_handoff_route(prompt, result['items'])
        if not hit:
            return
        choice, conf = hit
        if choice == 'unrelated':
            actionable = False
            policy = 'The opening request is unrelated to the open handoff(s); continue the user task and leave them open.'
        else:
            # A classifier selects context, not authorization. Retrieval stays read-only.
            selected = dict(result, items=[h for h in result['items'] if h['topic'] == choice], total=1)
            policy = opening_action('retrieve', selected, session_key(evt))
    label = LABEL.get(agent, LABEL['claude']) if actionable else 'handoff-status:'
    emit(agent, 'UserPromptSubmit', label + ' ' + policy, 'warn')


# ---------------------------------------------------------------- auto mode

def handle_stop(evt):
    """Fully automatic mode, interactive session inside tmux: once this session's
    trigger has fired and a handoff was written after it, type /clear and then
    "resume" into the pane, so the cleared session picks the handoff up."""
    pane = env("TMUX_PANE")
    if not auto_mode_on():
        nudge_unwritten_handoff(evt)
    if not auto_mode_on() or not pane or env("HANDOFF_AUTO_RUNNER"):
        sys.exit(0)  # the headless runner starts fresh sessions itself
    cwd = evt.get("cwd") or os.getcwd()
    counter = auto_count_path(cwd)
    key = session_key(evt)
    latch = latch_path(key) if key else None
    if not latch or not os.path.exists(latch):
        try:
            os.remove(counter)  # a turn ended without the trigger: real progress
        except OSError:
            pass
        sys.exit(0)
    fired_at = os.path.getmtime(latch)
    handoffs = _ledger().scan(cwd, 1)
    if not handoff_written_since(cwd, fired_at, key, require_open=True):
        sys.exit(0)  # still writing it; the next Stop will check again
    try:
        os.close(os.open(latch + ".cleared", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        sys.exit(0)
    # Loop guard: a session that starts close to the threshold hands off again at
    # once, so without a cap it would clear and resume forever.
    try:
        max_clears = int(env("HANDOFF_AUTO_MAX", "10"))
    except ValueError:
        max_clears = 10
    try:
        count = int(open(counter).read().strip() or 0)
    except Exception:
        count = 0
    if count >= max_clears:
        print(json.dumps({"systemMessage": (
            "[context-watch] Fully automatic mode stopped after %d automatic clears in a "
            "row in this project, so it cannot loop forever. The newest handoff is still "
            "open. A fresh session may already start near the threshold: raise HANDOFF_AT "
            "(or HANDOFF_AUTO_MAX), then type /clear and resume." % count)}))
        sys.exit(0)
    try:
        with open(counter, "w") as f:
            f.write("%d\n" % (count + 1))
    except OSError:
        pass
    # Detached, so this hook returns and the TUI goes idle before the keys arrive.
    script = ("sleep 2; tmux send-keys -t \"$P\" -l /clear; tmux send-keys -t \"$P\" Enter; "
              "sleep 4; tmux send-keys -t \"$P\" -l resume; tmux send-keys -t \"$P\" Enter")
    subprocess.Popen(["sh", "-c", script], env=dict(os.environ, P=pane),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    sys.exit(0)


def auto_cli(argv):
    """context_watch.py auto [--max N] [--prompt TEXT] -- <agent command...>"""
    if "--" not in argv or argv.index("--") == len(argv) - 1:
        print(auto_cli.__doc__, file=sys.stderr)
        return 2
    opts, cmd = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    max_runs, prompt = 10, "resume"
    try:
        if "--max" in opts:
            max_runs = int(opts[opts.index("--max") + 1])
        if "--prompt" in opts:
            prompt = opts[opts.index("--prompt") + 1]
    except (IndexError, ValueError):
        print(auto_cli.__doc__, file=sys.stderr)
        return 2
    if max_runs < 1:
        print("[auto] --max must be a positive run limit", file=sys.stderr)
        return 2
    child_env = dict(os.environ, HANDOFF_AUTO="1", HANDOFF_AUTO_RUNNER="1")
    rc = 0
    for run in range(1, max_runs + 1):
        import uuid
        run_id = uuid.uuid4().hex
        child_env['HANDOFF_RUN_ID'] = run_id
        print("[auto] run %d: %s" % (run, " ".join(cmd + [prompt])), file=sys.stderr)
        rc = subprocess.run(cmd + [prompt], env=child_env).returncode
        if rc != 0:
            print("[auto] child failed (exit %d); stopped without retry. Preserve this session's checkpoint and inspect the failure." % rc, file=sys.stderr)
            return rc if rc > 0 else 128 - rc
        fresh = [h for h in _ledger().scan(os.getcwd(), 1)
                 if _ledger()._read_fm(h['path']).get('runner_id') == run_id
                 and _ledger()._read_fm(h['path']).get('checkpoint_id')]
        if not fresh:
            print("[auto] run %d left no new open handoff; done (exit %d)" % (run, rc),
                  file=sys.stderr)
            return rc
        print("[auto] handoff %s written; clearing and resuming" % fresh[0]["path"],
              file=sys.stderr)
        prompt = "resume"
    print("[auto] stopped after --max %d runs; the newest handoff is still open" % max_runs,
          file=sys.stderr)
    return 75  # run budget exhausted; an open checkpoint is not completed work


# ---------------------------------------------------------------- emit

def build_message(occupancy, pending, breakdown, limit, source, model, skill,
                  agent="claude", second=False, usage_age=None):
    cache_read = breakdown.get("cache_read", 0)
    cache_share = (cache_read / occupancy * 100.0) if occupancy else 0.0
    detail = "cache-read %s of it (%.0f%%)" % (format(cache_read, ","), cache_share)
    if pending:
        detail += "; incl. ~%s pending" % format(pending, ",")
    if usage_age is not None and usage_age > STALE_USAGE_S:
        detail += "; usage entry is %d min old, telemetry may be stale" % (usage_age // 60)
    message = (
        "%s %sContext occupancy is ~%s tokens, over the %s-token handoff threshold "
        "for %s [%s] (%s). The `%s` skill applies here: finish only the action "
        "in progress, write the handoff (reason: context-pressure), then stop; "
        "the remaining work stays authorized for the resuming session. "
        "Ledger: python3 %s"
        % (LABEL.get(agent, LABEL["claude"]),
           "SECOND NOTICE, the first was not acted on: " if second else "",
           format(occupancy, ","), format(limit, ","), model or "unknown model",
           source, detail, skill, LEDGER)
    )
    if auto_mode_on():
        message += (" Fully automatic mode is active: after the handoff is written and "
                    "verified, end your turn at once without asking the user anything — "
                    "the session is cleared and the newest handoff resumed automatically.")
    elif autoresume_on():
        message += (" Autoresume is active: after the handoff is written, tell the user "
                    "to type /clear — the cleared session will announce the open handoff "
                    "and resume it automatically.")
    return message


def build_floor_message(floor, limit, model, agent):
    # Smallest threshold that clears the floor by max(10k, 10% of itself).
    suggest = -(-max(floor + 10000, floor / 0.9) // 1000) * 1000
    return (
        "%s The %s-token handoff threshold for %s is below this session's startup "
        "context (~%s tokens at the first model call), so a handoff would free "
        "nothing and no handoff notice will be sent this session. Tell the user: "
        "set HANDOFF_AT (or CONTEXT_WATCH_TOKENS) to at least ~%s."
        % ("handoff-status:", format(limit, ","),
           model or "unknown model", format(floor, ","), format(int(suggest), ","))
    )


def emit(agent, event_name, message, mode):
    if agent == "claude" or event_name == "PostToolUse":
        # Codex PostToolUse takes the same JSON: additionalContext is added as
        # developer context and the tool result is kept; block replaces it.
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
    print(message)  # Codex UserPromptSubmit: stdout becomes extra context for the turn
    sys.exit(0)


# ---------------------------------------------------------------- main

def main():
    if env("CONTEXT_WATCH_DISABLE") == "1":
        sys.exit(0)

    evt = read_event()
    event_name = evt.get("hook_event_name") or ""
    if event_name == "Stop":
        handle_stop(evt)
    transcript_path = evt.get("transcript_path") or ""
    entries = load_transcript_tail(transcript_path) if transcript_path else []
    agent = detect_agent(evt, entries)

    if event_name == "SessionStart":
        handle_session_start(evt, agent)

    if event_name not in ("PostToolUse", "UserPromptSubmit"):
        sys.exit(0)

    session_id = session_key(evt)
    if not session_id:
        sys.exit(0)
    first, second = latch_path(session_id), latch_path(session_id, 2)

    try:
        window = int(env("CONTEXT_WATCH_WINDOW", "200000"))
    except ValueError:
        window = 200000
    window_source = "assumed"

    model = evt.get("model")  # Codex includes it in hook input; Claude Code does not
    if agent == "claude":
        breakdown, tmodel = claude_usage(entries)
    else:
        breakdown, reported, tmodel = codex_usage(entries)
        if reported:
            window, window_source = reported, "reported"
    model = model or tmodel

    if not breakdown or breakdown.get("occupancy", 0) <= 0:
        if event_name == "UserPromptSubmit":
            route_opening_prompt(evt, agent)  # no API call yet: the opening prompt
        sys.exit(0)

    pending, pending_raw = estimate_pending(evt)
    # ponytail: Claude only; Codex rollouts' write order around hooks is unverified.
    unrecorded = unrecorded_results(entries, evt.get("tool_use_id")) \
        if agent == "claude" and env("CONTEXT_WATCH_PENDING", "1") != "0" else 0
    # The response that issued this call (thinking included) is written only
    # after the hook runs; assume it is as large as the session's largest so far.
    thinking = min(breakdown.get("max_output", 0), PENDING_CAP) \
        if agent == "claude" and env("CONTEXT_WATCH_PENDING", "1") != "0" else 0
    if env("CONTEXT_WATCH_THINKING") is not None:
        try:
            thinking = int(env("CONTEXT_WATCH_THINKING"))
        except ValueError:
            pass
    pending += unrecorded + thinking
    occupancy = breakdown["occupancy"] + pending

    cwd = evt.get("cwd") or os.getcwd()
    write_session_note(cwd, evt, os.path.exists(first))
    limit, source = resolve_threshold(model, window, cwd)
    if window_source == "reported":
        # Only a host-reported window is trusted for this cap: an assumed 200k
        # would clamp every 1M-context model's threshold to 180k.
        try:
            reserve = int(env("CONTEXT_WATCH_RESERVE", str(DEFAULT_RESERVE)))
        except ValueError:
            reserve = DEFAULT_RESERVE
        cap = window - reserve
        if 0 < cap < limit:
            source = "%s, capped from %s to window %s - reserve %s" % (
                source, format(limit, ","), format(window, ","), format(reserve, ","))
            limit = cap
    fired = os.path.exists(first)
    if fired and occupancy < limit * REARM_FACTOR:
        for path in (first, second, first + ".cleared"):
            try:
                os.remove(path)  # the window was compacted: re-arm
            except OSError:
                pass
        sys.exit(0)
    if os.path.exists(second):
        sys.exit(0)  # both notices given; only a compaction re-arms
    need = limit
    if fired:
        first_occ, first_base = read_first_latch(first)
        if breakdown["occupancy"] == first_base:
            # No model turn since the first notice (e.g. parallel tool results
            # landing together): it has not been seen yet, so it was not ignored.
            sys.exit(0)
        if handoff_written_since(cwd, os.path.getmtime(first), session_id):
            sys.exit(0)  # the handoff was written; the notice was acted on
        need = max(limit * SECOND_NOTICE_FACTOR,
                   first_occ + limit * (SECOND_NOTICE_FACTOR - 1))
    if occupancy < need:
        sys.exit(0)

    floor = None
    if not fired:
        try:
            if os.path.getsize(transcript_path) <= TAIL_BYTES:
                floor = floor_occupancy(entries, agent)
        except OSError:
            pass
    below_floor = floor is not None and limit < floor + max(FLOOR_MARGIN, limit * 0.1)

    try:
        fd = os.open(second if fired else first, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write("%d/%d/%d\n" % (occupancy, limit, breakdown["occupancy"]))
    except FileExistsError:
        sys.exit(0)
    except Exception:
        pass
    write_session_note(cwd, evt, not below_floor)
    if below_floor:
        # One note per session: occupy the second-notice latch too, so the
        # handoff never fires (and HANDOFF_AUTO cannot loop) until a compaction.
        try:
            open(second, "w").close()
        except OSError:
            pass

    usage_age = usage_age_seconds(entries)
    log_event({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent,
        "model": model,
        "occupancy": occupancy,
        "pending_estimate": pending,
        "pending_raw": pending_raw,
        "unrecorded_results": unrecorded,
        "thinking_allowance": thinking,
        "threshold": limit,
        "threshold_source": source,
        "window": window,
        "window_source": window_source,
        "usage_age_s": usage_age,
        "notice": "floor" if below_floor else (2 if fired else 1),
        "floor": floor,
        "breakdown": breakdown,
        "session_id": session_id,
        "cwd": cwd,
    })

    skill = env("CONTEXT_WATCH_SKILL", "session-handoff")
    mode = env("CONTEXT_WATCH_MODE", "warn").strip().lower()
    if below_floor:
        emit(agent, event_name, build_floor_message(floor, limit, model, agent), "warn")
    import shlex
    message = build_message(occupancy, pending, breakdown, limit, source, model, skill,
                            agent, second=fired, usage_age=usage_age)
    message += " Session identity: %s. Publish with python3 %s save --session %s --request-id <stable-checkpoint-id> --input <draft.json>." % (session_id, shlex.quote(LEDGER), shlex.quote(session_id))
    emit(agent, event_name, message, mode)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auto":
        sys.exit(auto_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        try:
            sys.exit(stats_cli())
        except BrokenPipeError:
            sys.exit(0)  # downstream pipe (grep -q, head) closed early — fine
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext':
            'handoff-status: Handoff service unavailable (%s). Continue the current task; explicit retrieval is blocked. Restore the installed skill files/access; do not infer that no handoff exists.' % type(exc).__name__}}))
        sys.exit(0)  # report failure without breaking the host session
