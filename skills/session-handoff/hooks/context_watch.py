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
(open-handoff announcer, see handoff_ledger.py), Stop (every mode: blocks a turn
once when a context-watch notice, advisory included, fired and this session has
no published checkpoint (#180); fully-automatic mode only: clears a tmux-hosted
session once its handoff is written).

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
                                (else the "at" config key, see below)
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
Optional auto-mode keys in the same files (never model thresholds):
  "auto" (bool), "at" (int), "auto_max" (int) = HANDOFF_AUTO, HANDOFF_AT,
  HANDOFF_AUTO_MAX; a non-empty environment variable wins, then the project
  file, then the user file.

Other environment variables:
  CONTEXT_WATCH_WINDOW        window size for the PERCENT path (default 200000;
                              Codex reports its own window and overrides this)
  CONTEXT_WATCH_SKILL         skill named in the instruction (default: session-handoff)
  CONTEXT_WATCH_MODE          warn | block (default: warn)
  CONTEXT_WATCH_AGENT         claude | codex (default: auto-detect)
  CONTEXT_WATCH_DISABLE       1 to no-op
  CONTEXT_WATCH_WARNING_WINDOW  advisory lead in tokens before the threshold
                              (default 30000; 0 disables; also the
                              "warning_window_tokens" config key, project then user)
  CONTEXT_WATCH_PENDING       0 to disable the pending-content estimate
  CONTEXT_WATCH_THINKING      tokens assumed for the not-yet-written current
                              response (default: session's largest response,
                              capped at 25000; Claude only)
  CONTEXT_WATCH_JEV           1 to ask Jev (TypeSafe System One, needs
                              TYPESAFE_API_KEY) whether a session's opening
                              prompt continues an open handoff (default off)
  CONTEXT_WATCH_LOG           analytics path (default ~/.context-watch/events.jsonl;
                              0 to disable)
  CONTEXT_WATCH_MAX_AGE_DAYS  announcer: ignore open handoffs older than this (14)
  AUTORESUME                  announcer + trigger note: true resumes a single open
                              handoff without asking after /clear
  CONTEXT_WATCH_AUTORESUME    legacy alias for AUTORESUME
  HANDOFF_AUTO                fully automatic (default on in AgentsRoom, where the
                              tab restarts itself; 0 or "auto": false turns it
                              off): implies AUTORESUME, the newest open
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

import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
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


def canonical_ledger():
    """One printed ledger path whichever registration ran the hook: the installed
    path when it resolves to this hook's sibling, else the sibling's realpath."""
    sibling = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "handoff_ledger.py"))
    installed = os.path.join(os.path.expanduser("~"), ".agents", "skills", "session-handoff",
                             "hooks", "handoff_ledger.py")
    return installed if os.path.exists(installed) and os.path.realpath(installed) == sibling else sibling


LEDGER = canonical_ledger()
# handoff_protocol prints paths from its own __file__: import it from the same directory.
if os.path.dirname(LEDGER) not in sys.path:
    sys.path.insert(0, os.path.dirname(LEDGER))
try:
    from handoff_contract import emit_output, render_output, python_command, command_template, RESTART_PROMPT_RE
except ImportError:
    if __name__ != '__main__':
        raise
    RESTART_PROMPT_RE = None
    # A lone copied watcher must still report an incomplete installation. The
    # unavailable contract cannot encode its own missing-install diagnostic.
    render_output = json.dumps

    def emit_output(value):
        print(render_output(value))

try:
    import handoff_identity  # noqa: E402
    from handoff_identity import (  # noqa: E402,F401
        auto_count_path, latch_path, session_key, terminal_pointer_path, truthy as _truthy)
    _MISSING_INSTALL = None
except ImportError as _exc:
    # A hook copied without its siblings is a missing installation. Stand-ins
    # re-raise the ImportError where the hook first needs identity, so main()
    # reaches fail_open at the same point it did before this module existed.
    if __name__ != "__main__":
        raise
    _MISSING_INSTALL = _exc

    def _missing(*_args, **_kwargs):
        raise _MISSING_INSTALL

    class _MissingModule:
        def __getattr__(self, _name):
            return _missing

    handoff_identity = _MissingModule()
    auto_count_path = latch_path = session_key = terminal_pointer_path = _missing

    def _truthy(value):  # module-free, as before the identity module
        return value.strip().lower() in ("1", "true", "yes", "on")
try:
    # Turns between reminders while a context-pressure restart stays blocked (9452e1fe).
    RESTART_REMIND_TURNS = max(1, int(os.environ.get("CONTEXT_WATCH_REMIND_TURNS") or 5))
except ValueError:
    RESTART_REMIND_TURNS = 5
STALE_USAGE_S = 600          # a usage entry older than this may not describe the window
LABEL = {"claude": "[context-watch]",
         # Newer Codex parses stdout that starts with "[" or "{" as JSON, so the
         # Codex label must not start with a bracket (see handle_session_start).
         "codex": "context-watch:"}


def env(name, default=""):
    return os.environ.get(name, default)


def config_setting(name, key, cwd=None):
    """Auto-mode setting: environment when set and non-empty, then the project
    .context-watch.json, then ~/.context-watch/thresholds.json; None if unset."""
    value = env(name).strip()
    if value:
        return value
    value = _load_config_files(cwd or (_EVT or {}).get("cwd") or os.getcwd(), settings=True).get(key)
    return None if value is None else str(value).lower()


_V3_INPUTS = None  # (model, window) mandatory_status judged this event with


def auto_mode_on():
    """Unset means on inside AgentsRoom (the tab restarts itself), off elsewhere;
    an explicit HANDOFF_AUTO=0 or "auto": false always wins."""
    if globals().get('_V3_INVALID', False):
        return False
    import mandatory_gate
    cwd = mandatory_gate.project_root((_EVT or {}).get('cwd') or os.getcwd())
    try:
        with open(os.path.join(cwd, '.context-watch.json'), encoding='utf-8') as stream:
            project = json.load(stream)
    except FileNotFoundError:
        project = {}
    except (OSError, ValueError):
        project = None
    if not isinstance(project, dict):
        project = {}
    model = window = cw = None
    if project.get('handoff_v3') is True:  # only v3 timing needs this module
        cw = sys.modules[__name__]
        if _V3_INPUTS is not None:  # what mandatory_status judged, also in the session note
            model, window = _V3_INPUTS
        else:  # same v3_inputs rule as mandatory_status; an event window is host-reported
            model = claude_usage(load_transcript_tail((_EVT or {}).get('transcript_path') or ''))[1]
            window = (_EVT or {}).get('context_window')
            model, window = mandatory_gate.v3_inputs(model, window, 'reported' if window is not None else None)
    # One rule with the save transition: invalid v3 timing is off, HANDOFF_AUTO
    # wins, a project may disable auto but never enable it.
    value = mandatory_gate.auto_setting(project, cwd, cw, model, window)
    if value is None:
        return bool(env("AGENTSROOM_AGENT_ID"))
    return value


def auto_max(cwd=None):
    try:
        return int(config_setting("HANDOFF_AUTO_MAX", "auto_max", cwd) or 10)
    except ValueError:
        return 10


def autoresume_on():
    return auto_mode_on() or _truthy(env("AUTORESUME") or env("CONTEXT_WATCH_AUTORESUME"))


def _ledger():
    import handoff_ledger
    return handoff_ledger


def open_private(path, temp=False):
    """Text-mode write handle that never follows a symlink planted at `path` in a
    shared temp dir. A temp file (always `<final>.<pid>`) is created exclusively,
    clearing a stale one first (unlink does not follow links); anything else is
    created or truncated in place."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if temp else os.O_TRUNC)
    if temp:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8")


def write_session_note(cwd, evt, fired, context=None):
    """Leave this session's facts where handoff_ledger.py new-path (run from
    the agent's shell, no hook stdin) can read them: reason and skills."""
    try:
        sid = session_key(evt)
        if not sid:
            return
        note = {"session_id": sid, "cwd": cwd,
                "transcript_path": evt.get("transcript_path") or "", "ts": time.time()}
        path = handoff_identity.session_note_path(cwd, sid)
        previous = _ledger().read_session_note(cwd, session=sid)
        note.update({k: v for k, v in previous.items() if k.startswith('resume')})
        note.update({k: previous[k] for k in ('observed_occupancy_tokens', 'threshold', 'threshold_source',
                                            'window', 'window_source', 'model') if k in previous})
        note.update(context or {})
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
        # Read the latch last: a parallel hook may have created it after this
        # hook's caller looked, and its trigger must not be blanked.
        try:
            note["trigger_id"] = str(os.stat(latch_path(sid)).st_mtime_ns)
        except OSError:
            note["trigger_id"] = ''
        note["fired"] = bool(fired) or bool(note["trigger_id"])
        tmp = "%s.%d" % (path, os.getpid())
        with open_private(tmp, temp=True) as f:
            json.dump(note, f)
        os.replace(tmp, path)
        write_terminal_pointer(cwd, sid)
        write_project_pointer(cwd, sid)
    except Exception:
        pass


def write_project_pointer(cwd, sid):
    """Record this session's project for saves run from a temp cwd. A temp project is
    never recorded (save ignores one), and an unchanged pointer under an hour old is
    left alone instead of being rewritten on every event."""
    project = handoff_identity.canonical_root(cwd)
    if (handoff_identity.under_temp_base(project)
            or handoff_identity.read_project_pointer(sid, max_age_s=3600) == project):
        return
    pointer = handoff_identity.project_pointer_path(sid)
    ptmp = "%s.%d" % (pointer, os.getpid())
    try:
        with open_private(ptmp, temp=True) as f:
            json.dump({"session_id": sid, "project": project, "ts": time.time()}, f)
        os.replace(ptmp, pointer)
    except OSError as exc:
        # A foreign file in a sticky temp dir cannot be replaced (EPERM); save then
        # refuses rather than trusting it, but say why the record is missing.
        print("context-watch: cannot record the session project pointer %s: %s" % (pointer, exc), file=sys.stderr)
        try:
            os.unlink(ptmp)
        except OSError:
            pass


def write_terminal_pointer(cwd, sid):
    """This terminal's current session, so a shell command can find it without hook stdin."""
    try:
        terminal = handoff_identity.terminal_id()
        if not terminal:
            return
        path = terminal_pointer_path(cwd, terminal)
        tmp = "%s.%d" % (path, os.getpid())
        with open_private(tmp, temp=True) as f:
            json.dump({"session_id": sid, "host_session_id": os.environ.get("CODEX_THREAD_ID", ""), "ts": time.time()}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def read_first_latch(path):
    """(occupancy, transcript usage) recorded by the first notice; (0, None) if unknown."""
    try:
        with open(path) as f:
            occ, _limit, base = (int(x) for x in f.read().strip().split("/"))
        return occ, base
    except Exception:
        return 0, None


# ---------------------------------------------------------------- transcript

def in_subagent(evt):
    """True when the event fired inside an Agent-tool subagent. Claude Code runs
    these hooks there with the parent's session_id, so a notice would reach a
    worker that cannot save the parent's handoff. `agent_id` is the signal: a
    captured subagent PostToolUse (2026-10-02) also carried the parent's
    transcript_path. A transcript under .../subagents/ stays as a fallback."""
    if evt.get("agent_id"):
        return True
    return "subagents" in (evt.get("transcript_path") or "").replace("\\", "/").split("/")


def invalid_hook_input():
    print(json.dumps({'systemMessage': 'handoff-status: Invalid UTF-8 hook input; encode JSON as UTF-8 '
                      'and retry. No hook event was processed.'}))
    sys.exit(0)


def read_event():
    try:
        try:
            from handoff_protocol import read_cli_input
        except ImportError:
            # A lone hook still needs the event to report its missing installation.
            raw = sys.stdin.buffer.read().decode('utf-8')
        else:
            raw = read_cli_input('-')
        evt = json.loads(raw)
    except UnicodeDecodeError:
        invalid_hook_input()
    except Exception:
        return {}
    return evt if isinstance(evt, dict) else {}


TAIL_STATUS = None  # why the last load_transcript_tail produced no telemetry: missing | unreadable | malformed


def load_transcript_tail(path):
    """Transcript entries (dicts only). A legitimately empty or usage-free
    transcript is not an error; a missing/unreadable file, or a non-empty one
    with no parsable entry, sets TAIL_STATUS so main() can say so once."""
    global TAIL_STATUS
    TAIL_STATUS = None
    entries, bad = [], 0
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(-TAIL_BYTES, os.SEEK_END)
                f.readline()  # discard the partial first line
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    bad += 1
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
                else:
                    bad += 1
    except FileNotFoundError:
        TAIL_STATUS = "missing"
    except OSError:
        TAIL_STATUS = "unreadable"
    else:
        if bad and not entries:
            TAIL_STATUS = "malformed"
    return entries


_DIAG = None  # one-time telemetry diagnostic, delivered by emit() or at exit
_OPENING_NOTE = ''  # explicit resume guidance, delivered alongside pressure if needed


def claim_once(evt, suffix):
    """True the first time per session (latch file); True too when the
    session is unknowable, so a failure is never swallowed."""
    key = session_key(evt)
    if not key:
        return True
    try:
        os.close(os.open(handoff_identity.latch_claim_path(key, suffix), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def diag_envelope(agent, event_name, message):
    if agent != "claude" and event_name == "UserPromptSubmit":
        return message  # Codex UserPromptSubmit: plain stdout becomes context
    return render_output({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message}})


def flush_diag(agent):
    """Print a still-undelivered diagnostic (the hook had nothing else to say)."""
    global _DIAG, _OPENING_NOTE
    if _OPENING_NOTE:
        note, _OPENING_NOTE = _OPENING_NOTE, ''
        if _DIAG:
            _DIAG = (_DIAG[0], note + ' ' + _DIAG[1])
        else:
            _DIAG = ('UserPromptSubmit', note)
    if _DIAG:
        event_name, message = _DIAG
        _DIAG = None
        print(diag_envelope(agent, event_name, message))


def _claim_lone_notice(evt):
    """Once-per-session claim for a hook installed without handoff_identity, so
    its own temp-state paths are unavailable: a 0600 marker made with
    O_CREAT|O_EXCL|O_NOFOLLOW in the system temp dir. True means "say it";
    a marker that cannot be made (other than already existing) says it again."""
    import tempfile  # only here: every other temp path comes from handoff_identity
    key = str((evt or {}).get("session_id") or (evt or {}).get("transcript_path") or "")
    if not key:
        return True
    path = os.path.join(tempfile.gettempdir(), "context-watch-lone-%s.error"
                        % hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:20])
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600))
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def fail_open(exc, evt, once=True):
    """Report an unexpected failure under the event that triggered it, once per
    session (via a module-free marker when `once` is false: a missing install),
    with the exception type only.
    Always exits 0."""
    event_name = (evt or {}).get("hook_event_name") or "SessionStart"
    if event_name == "SessionStart":
        message = ('handoff-status: Handoff service unavailable (%s). Continue the current task; explicit retrieval '
                   'is blocked. Restore the installed skill files/access; do not infer that no handoff exists.'
                   % type(exc).__name__)
        emit_output({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': message}})
    elif claim_once(evt, ".error") if once else _claim_lone_notice(evt):
        message = ("handoff-status: Context-watch failed (%s); context telemetry is unavailable for this hook. "
                   "Continue the current task." % type(exc).__name__)
        if event_name in ("PostToolUse", "UserPromptSubmit"):
            print(diag_envelope(detect_agent(evt, []), event_name, message))
        elif event_name == "Stop":
            emit_output({"systemMessage": message})
    sys.exit(0)  # report failure without breaking the host session


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
    seen_total = stale_total = None  # running total of the newest accepted / pre-compaction count
    for e in entries:
        payload = e.get("payload")
        if e.get("type") == "compacted" or (isinstance(payload, dict)
                                            and payload.get("type") == "context_compacted"):
            # Pre-compaction usage no longer describes the window. Codex also
            # replays the old token_count right after the record: ignore it
            # until a count with a different running total arrives.
            last, stale_total = None, seen_total
            continue
        if isinstance(payload, dict):
            if payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = info.get("last_token_usage")
                total = info.get("total_token_usage")
                if stale_total is not None and total == stale_total:
                    continue  # replay of the pre-compaction measurement
                if isinstance(usage, dict):
                    last = usage
                    seen_total, stale_total = total, None
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


def handoff_written_since(cwd, session=None, require_open=False):
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
            for r in ledger._records(handoff_identity.project_root(cwd)))
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
    tool call, whose result estimate_pending already counts.
    api-request-blob rows mirror canonical rows (the serialized request), so
    they are skipped, and each tool_use_id counts once per reset window.
    Streamed rows of one response repeat its usage and message.id, so only a
    usage row with a new message.id resets the window; an id-less usage row
    always resets (it cannot be proven to be the same response)."""
    total, seen, usage_id = 0, set(), None
    for e in entries:
        if e.get("isSidechain") or e.get("type") == "api-request-blob":
            continue
        if (e.get("type") == "system" and e.get("subtype") == "compact_boundary") \
                or e.get("isCompactSummary"):
            total, seen, usage_id = 0, set(), None
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        if isinstance(msg.get("usage"), dict) and "input_tokens" in msg["usage"]:
            mid = msg.get("id")
            if mid is None or mid != usage_id:
                total, seen = 0, set()
            usage_id = mid
            continue
        content = msg.get("content")
        for block in content if isinstance(content, list) else ():
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            tid = block.get("tool_use_id")
            if tid == skip_id or (tid is not None and tid in seen):
                continue
            if tid is not None:
                seen.add(tid)
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


SETTING_KEYS = {"auto": bool, "at": int, "auto_max": int}  # not model thresholds
PROJECT_DISABLE_ONLY_KEYS = ("auto",)  # a project may disable, never enable (mandatory_gate.auto_setting)
WARNING_WINDOW_KEY = "warning_window_tokens"  # advisory lead (23079574), never a model threshold


def warning_window_candidates(cwd):
    """[(source, raw)] for warning_window_tokens, highest precedence first; raw None if absent."""
    raw = env("CONTEXT_WATCH_WARNING_WINDOW").strip()
    out = [("env CONTEXT_WATCH_WARNING_WINDOW", raw or None)]
    for path in (os.path.join(cwd or os.getcwd(), ".context-watch.json"),
                 os.path.join(os.path.expanduser("~"), ".context-watch", "thresholds.json")):
        value = None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if str(k).strip().lower() == WARNING_WINDOW_KEY:
                        value = v
        except Exception:
            pass
        out.append((path, value))
    return out


def _load_config_files(cwd, settings=False):
    """Model thresholds (int keys), or with settings=True the auto-mode settings."""
    merged = {}
    user_path = os.path.join(os.path.expanduser("~"), ".context-watch", "thresholds.json")
    for path in (
        user_path,
        os.path.join(cwd or os.getcwd(), ".context-watch.json"),
    ):  # user-global first so project-local overrides on key collision
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    k = str(k).strip().lower()
                    if k in {WARNING_WINDOW_KEY, 'handoff_v3', 'mandatory_at_tokens',
                             'resume_pointer_policy', 'activate_suggested_skills'}:
                        continue
                    if k in SETTING_KEYS:
                        if k in PROJECT_DISABLE_ONLY_KEYS:
                            continue  # skipped in every file: mandatory_gate.auto_setting decides
                        if settings and type(v) is SETTING_KEYS[k]:
                            merged[k] = v
                    elif not settings and isinstance(v, int) and not isinstance(v, bool):
                        merged[k] = v
        except Exception:
            continue
    return merged


def project_enables_disable_only_key(cwd):
    """Disable-only keys this project's .context-watch.json sets to true (ignored)."""
    import mandatory_gate
    try:
        with open(os.path.join(mandatory_gate.project_root(cwd or os.getcwd()), ".context-watch.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    lowered = {str(key).strip().lower(): value for key, value in data.items()}
    return [k for k in PROJECT_DISABLE_ONLY_KEYS if lowered.get(k) is True]


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
    at = None if handoff_at else _load_config_files(cwd, settings=True).get("at")
    if at is not None and at > 0:
        return at, "config:at"

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
            logged = [json.loads(l) for l in f if l.strip()]
    except Exception:
        print("no analytics log at %s" % path)
        return 0
    events = [e for e in logged if e.get("notice") != "advisory"]  # early warnings are not triggers
    if not events:
        print("no handoff triggers logged (%s)" % path if logged else "analytics log is empty")
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
    print("last event: %s" % render_output(events[-1]))
    return 0


# ---------------------------------------------------------------- announcer

ANNOUNCE_CAP = 5  # handoffs listed by name; the rest are counted


def lean_resume_prompt(prompt):
    return bool(re.fullmatch(r'\s*(?:continue|resume) (?:the|your) handoff[.!]?\s*',
                             str(prompt or ''), re.I))


def restart_prompt(prompt):
    """The successor prompt restart_preflight builds (shared prefix, matched at the
    start only). Identity-only: unlike a lean prompt it changes no routing."""
    return bool(RESTART_PROMPT_RE and RESTART_PROMPT_RE.match(str(prompt or '')))


def bounded_opening_note(evt):
    """Isolate filesystem inspection so a broken/slow store cannot block a prompt."""
    try:
        result = subprocess.run([sys.executable, os.path.abspath(__file__), 'opening-note'],
                                input=json.dumps(evt), capture_output=True, text=True, encoding='utf-8',
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'), timeout=1)
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


STARTUP_TIMEOUT = 8  # below the 10 s SessionStart hook timeout (install.py, hooks.json)


def startup_adoption(cwd, sid):
    """v3-3: adopt one eligible restart pointer, isolated so a broken store cannot block startup.

    Empty when there is nothing to adopt or announce; the opening note then runs as before."""
    if not sid:
        return ''
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'restart_preflight.py')
    try:
        root = handoff_identity.project_root(cwd)
        if not os.path.isfile(script) or not os.path.isdir(os.path.join(root, '.handoffs', 'pointers')):
            return ''  # Fast no-op: no pointer index, or no installed service to read it.
        result = subprocess.run([sys.executable, script, 'startup', '--cwd', cwd, '--session', sid],
                                capture_output=True, text=True, encoding='utf-8', timeout=STARTUP_TIMEOUT,
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    except Exception:
        return ''
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return ''
    try:
        data = json.loads(out)
    except ValueError:
        return ''
    if isinstance(data, dict) and data.get('quiet'):
        log_event({'ts': time.time(), 'event': 'startup-adoption', 'outcome': data.get('outcome'),
                   'session': sid})
        return ''
    return 'startup-adoption: ' + out


def mark_startup_delivered(text):
    """After the delivery is flushed: record it so a later exact prompt does not repeat the body."""
    try:
        data = json.loads(text[len('startup-adoption: '):])
        info = data.get('startup') or {}
        if data.get('outcome') != 'resumed' or not info.get('request_id'):
            return
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'restart_preflight.py')
        subprocess.run([sys.executable, script, 'startup', '--root', info['root'],
                        '--request-id', info['request_id'], '--session', info['session']],
                       capture_output=True, timeout=1)
    except Exception:
        pass  # No marker: a later exact prompt may deliver the body again, never lose it.


def reserve_handoff_status(evt):
    """Share one neutral opening notice across hook events in this session."""
    sid = session_key(evt)
    if not sid:
        return True  # No reliable identity: avoid silencing another session.
    cwd = evt.get('cwd') or os.getcwd()
    path = handoff_identity.session_status_path(cwd, sid)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    except OSError:
        return True  # Storage failure must not hide useful guidance.
    return True


def handle_session_start(evt, agent):
    """Keep startup quiet except for owned publication repair or compaction recovery."""
    source = evt.get("source") or ""
    if source == "compact":
        sid = session_key(evt)
        if sid:
            try:
                os.unlink(handoff_identity.delivery_receipt_path(handoff_identity.project_root(evt.get('cwd') or os.getcwd()), sid))
            except FileNotFoundError:
                pass
        note = ("handoff-status: Context was just compacted in this "
                "session. The summary above is evidence, not the live state: re-verify the "
                "workspace, revision and test results before relying on it. Compaction is "
                "automatic pressure, not the user parking the work: if the user had "
                "authorized a task that was in progress, continue it.")
        emit_output({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": note}})
        sys.exit(0)
    if source in ("resume", "fork"):
        sys.exit(0)  # a resumed session already has its context
    cwd = evt.get("cwd") or os.getcwd()
    write_session_note(cwd, evt, False)  # fresh native identity first, then the pointer index
    adopted = startup_adoption(cwd, session_key(evt)) if _MISSING_INSTALL is None else ''
    if adopted:
        emit_output({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': adopted}})
        sys.stdout.flush()
        mark_startup_delivered(adopted)
        sys.exit(0)
    note = bounded_opening_note(evt)
    if note:
        emit_output({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': note}})
    sys.exit(0)


# ---------------------------------------------------------------- stop nudge

def session_checkpoint_published(cwd, session, since):
    """A validated checkpoint this session published at or after `since` (the
    advisory latch's mtime): an older save does not cover the notice (#180)."""
    if not session:
        return False
    try:
        return any(r['fm'].get('session_id') == session and r['fm'].get('checkpoint_id') and not r['problems']
                   and r['fm'].get('status') != 'unpublished' and r['mtime'] >= since
                   for r in _ledger()._records(handoff_identity.project_root(cwd)))
    except OSError:
        return False


NUDGE_BAND_TOKENS = 10000


def nudge_band(cwd, key, notice, current=None):
    """Which +10k-token band past the notice occupancy this session is in:
    `current` (else the session note's last observed occupancy) against the
    latch's first field."""
    try:
        with open(notice, encoding="utf-8") as f:
            start = int(f.read().split("/", 1)[0])
    except (OSError, ValueError):
        return 0
    if current is None:
        current = _ledger().read_session_note(cwd, session=key).get("observed_occupancy_tokens")
    if type(current) is not int:
        return 0
    return max(0, (current - start) // NUDGE_BAND_TOKENS)


def clear_nudges(notice):
    """Re-arm every spent reminder and Stop warning of a notice (all bands)."""
    for stale in glob.glob(glob.escape(notice) + ".nudged*") + glob.glob(glob.escape(notice) + ".reminded*"):
        try:
            os.remove(stale)
        except OSError:
            pass


def unsaved_notice(cwd, key):
    """The latch of this session's context-watch notice (threshold, else the
    advisory) when no checkpoint was published since it, else None."""
    latch = latch_path(key) if key else None
    if not latch:
        return None
    if os.path.exists(latch):
        return None if handoff_written_since(cwd, key) else latch
    advised = latch + ".advised"
    try:
        since = os.path.getmtime(advised)
    except OSError:
        return None
    return None if session_checkpoint_published(cwd, key, since) else advised


def claim_band(notice, kind, band):
    """One shot per band: O_EXCL, so the same band never repeats."""
    try:
        os.close(os.open("%s.%s.%d" % (notice, kind, band), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except OSError:
        return False  # spent already, or unwritable temp dir


def remind_unwritten_handoff(agent, evt, event_name, cwd, key, occupancy):
    """#186: while nothing was saved since this session's notice, instruct the
    agent once per further +10k-token band past it (Claude and Codex). The block
    lives in nudge_unwritten_handoff; this never blocks and reads no message text."""
    notice = unsaved_notice(cwd, key)
    if notice and notice.endswith(".advised"):
        return  # #208: the advisory arms the user warning only, never a save reminder
    band = nudge_band(cwd, key, notice, occupancy) if notice else 0
    # Band 0 is the notice itself: it already carries the instruction.
    if band < 1 or not claim_band(notice, "reminded", band):
        return
    emit(agent, event_name, (
        "[context-watch] This session has a context-watch notice and no checkpoint saved since it "
        "(about %dk tokens now). Save a checkpoint before any clearing, so a cleared session keeps "
        "this work's context. Never tell the user the work continues after a handoff, in a next session "
        "or with a successor unless you can name the saved checkpoint path. "
        "For the JSON draft format run %s. Publish with %s; stop after outcome saved."
        % (occupancy // 1000, python_command(LEDGER, 'save', '--template'),
           command_template(LEDGER, ('save', '--session', key), '--input <draft.json>'))), "warn")


def nudge_unwritten_handoff(evt):
    """Every mode (#180, #186): a context-watch notice fired this session, the
    turn is ending, and no checkpoint was published since. Every such stop
    warns the user. Once per +10k-token band past the notice, the same output
    also blocks the stop so the agent really saves or states plainly that
    nothing was saved and why (the reply is never parsed; only a save silences
    the guard). A stop already continued by a block (stop_hook_active) only
    warns and claims nothing, so the guard never loops and the band's block
    stays for the next ordinary stop. Claude only (Codex has no Stop hook)."""
    if detect_agent(evt, []) != "claude":
        return
    key = session_key(evt)
    cwd = evt.get("cwd") or os.getcwd()
    notice = unsaved_notice(cwd, key)
    if not notice:
        return
    out = {"systemMessage": (
        "[context-watch] This session got a context-watch notice and has published no checkpoint "
        "since. Do not clear it yet: clearing now would lose this work's context. Ask the agent to "
        "save a checkpoint first; any handoff it mentions without a saved path does not exist. "
        "Searched project: %s." % handoff_identity.project_root(cwd))}
    advisory_only = notice.endswith(".advised")  # #208: warn only, no block, below the threshold
    if not advisory_only and not evt.get("stop_hook_active") and claim_band(notice, "nudged", nudge_band(cwd, key, notice)):
        out.update(decision="block", reason=(
            "[context-watch] No checkpoint has been published since this session's context-watch notice. "
            "Before ending this turn, do one of two things: really save a checkpoint now, or state plainly "
            "to the user that nothing was saved and why. Never say the work continues after a handoff, in "
            "a next session or with a successor without a saved checkpoint path. Your statement does not "
            "lift this guard: only a saved checkpoint does, and every further +10k tokens blocks again. "
            "For the JSON draft format run %s. Preserve the draft and publish with %s; stop after outcome "
            "saved. If the save fails, report the failed location and keep this session; do not clear."
            % (python_command(LEDGER, 'save', '--template'),
               command_template(LEDGER, ('save', '--session', key), '--input <draft.json>'))))
    emit_output(out)
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
    from handoff_protocol import lookup, opening_action, generic_intent, terminal_id
    prompt = str(evt.get('prompt') or '')
    import re
    direct = None if generic_intent(prompt) else re.fullmatch(r'(resume|retrieve) ([a-z0-9-]+|/[^\n]+)', prompt.strip(), re.I)
    result = lookup(cwd, topic=direct[2] if direct else None,
                    terminal=terminal_id() if auto_mode_on() and not direct else None)
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
            os.close(os.open(handoff_identity.session_routed_path(cwd, sid),
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
    if label == 'handoff-status:' and not reserve_handoff_status(evt):
        return
    emit(agent, 'UserPromptSubmit', label + ' ' + policy, 'warn')


# ---------------------------------------------------------------- auto mode

def announce_manual_clear(evt):
    """No tmux pane to type into (e.g. an AgentsRoom console): once this session's
    trigger produced a published handoff, tell the user to clear it themselves
    instead of leaving them waiting on a clear that can never happen."""
    key = session_key(evt)
    latch = latch_path(key) if key else None
    if not latch or not os.path.exists(latch):
        return
    cwd = evt.get("cwd") or os.getcwd()
    if not handoff_written_since(cwd, key, require_open=True):
        return
    try:
        os.close(os.open(latch + ".announced", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return  # told once already, or unwritable temp dir
    emit_output({"systemMessage": (
        "[context-watch] Handoff saved. If the agent did not restart itself, type "
        "/clear, then resume (no tmux pane to clear automatically). Hook session id: %s" % key)})


def handle_stop(evt):
    """Fully automatic mode, interactive session inside tmux: once this session's
    trigger has fired and a handoff was written after it, type /clear and then
    "resume" into the pane, so the cleared session picks the handoff up."""
    # An AgentsRoom tab restarts itself; a TMUX_PANE it inherited (the app was
    # launched from inside tmux) belongs to another terminal, so never type there.
    pane = "" if env("AGENTSROOM_AGENT_ID") else env("TMUX_PANE")
    cwd = evt.get('cwd') or os.getcwd()
    key = session_key(evt)
    if auto_mode_on():
        paused = [r for r in _ledger()._records(cwd) if r['record'].session_id == key
                  and r['record'].open_strict and r['record'].paused
                  and r['record'].trusted()]
        if paused:
            if claim_once(evt, '.automatic-paused'):
                emit_output({'systemMessage': '[context-watch] Automatic handoff paused. Checkpoint remains open at %s. Report the diagnostics; a fresh session can explicitly continue the handoff after addressing re-entry overhead or the restart budget.' % paused[-1]['path']})
            sys.exit(0)
    nudge_unwritten_handoff(evt)  # every mode (#180)
    if not pane and not env("HANDOFF_AUTO_RUNNER") and (auto_mode_on() or env("AGENTSROOM_AGENT_ID")):
        announce_manual_clear(evt)
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
    if not handoff_written_since(cwd, key, require_open=True):
        sys.exit(0)  # still writing it; the next Stop will check again
    try:
        os.close(os.open(latch + ".cleared", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        sys.exit(0)
    # Loop guard: a session that starts close to the threshold hands off again at
    # once, so without a cap it would clear and resume forever.
    max_clears = auto_max(cwd)
    try:
        with open(counter) as f:
            count = int(f.read().strip() or 0)
    except Exception:
        count = 0
    if count >= max_clears:
        emit_output({"systemMessage": (
            "[context-watch] Fully automatic mode stopped after %d automatic clears in a "
            "row in this project, so it cannot loop forever. The newest handoff is still "
            "open. A fresh session may already start near the threshold: raise HANDOFF_AT "
            "(or HANDOFF_AUTO_MAX), then type /clear and resume." % count)})
        sys.exit(0)
    try:
        with open_private(counter) as f:
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
    # Each child starts a new host session in this runner's own terminal: no
    # inherited session ids, and no AgentsRoom id that would route its save to
    # restarting the caller's tab. The terminal stays constant across runs.
    child_env = handoff_identity.runner_environment(dict(os.environ, HANDOFF_AUTO="1", HANDOFF_AUTO_RUNNER="1"))
    import uuid
    rc = 0
    for run in range(1, max_runs + 1):
        run_id = uuid.uuid4().hex
        child_env['HANDOFF_RUN_ID'] = run_id
        print("[auto] run %d: %s" % (run, " ".join(cmd + [prompt])), file=sys.stderr)
        rc = subprocess.run(cmd + [prompt], env=child_env).returncode
        if rc != 0:
            print("[auto] child failed (exit %d); stopped without retry. Preserve this session's checkpoint and inspect the failure." % rc, file=sys.stderr)
            return rc if rc > 0 else 128 - rc
        fresh = [h for h in _ledger().scan(os.getcwd(), 1)
                 for checkpoint in [_ledger().Record.load(h['path'])]
                 if checkpoint.runner_id == run_id and checkpoint.checkpoint_id]
        if not fresh:
            print("[auto] run %d left no new open handoff; done (exit %d)" % (run, rc),
                  file=sys.stderr)
            return rc
        if _ledger().Record.load(fresh[0]['path']).paused:
            print('[auto] automatic handoff paused; checkpoint remains open at %s. Start a fresh session and explicitly continue the handoff after addressing re-entry overhead.' % fresh[0]['path'], file=sys.stderr)
            return 75
        print("[auto] handoff %s written; clearing and resuming" % fresh[0]["path"],
              file=sys.stderr)
        prompt = "resume"
    print("[auto] stopped after --max %d runs; the newest handoff is still open" % max_runs,
          file=sys.stderr)
    return 75  # run budget exhausted; an open checkpoint is not completed work


# ---------------------------------------------------------------- emit

def occupancy_note(occupancy, breakdown):
    """What the occupancy figure counts; cache-read usually dominates it."""
    cache_read = breakdown.get("cache_read", 0)
    share = (cache_read / occupancy * 100.0) if occupancy else 0.0
    return ("Occupancy counts the last model call's input + cache-creation + cache-read + output "
            "tokens; cache-read is %s of it (%.0f%%)." % (format(cache_read, ","), share))


def _read_restart_block(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _save_restart_block(path, value):
    tmp = "%s.%d" % (path, os.getpid())
    try:
        with open_private(tmp, temp=True) as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError:
        pass


def restart_block_notice(cwd, session_id, event_name, occupancy, limit, breakdown):
    """Re-offer a context-pressure restart whose human hold has cleared, or remind
    every RESTART_REMIND_TURNS prompts while it stays blocked (9452e1fe).

    Hold cleared: a caller hold (the ask_user card) counts as cleared at the next
    UserPromptSubmit after the block, which the notice hedges because the hook cannot
    see the card; a durable hold when none applies to the
    checkpoint any more; a checkpoint hold when Record.held is false."""
    path = latch_path(session_id) + ".restart-blocked"
    marker = _read_restart_block(path)
    if marker is None:
        return ""

    def drop():
        try:
            os.remove(path)
        except OSError:
            pass
        return ""
    if not isinstance(marker.get("path"), str) or not isinstance(marker.get("turns", 0), int):
        return drop()  # malformed: treat like a missing marker
    try:
        from handoff_record import Record
        record = Record.load(marker.get("path") or "")
    except Exception:
        return drop()
    if (marker.get("session") != session_id or not record.open_strict
            or record.session_id != session_id or record.fm.get("reason") != "context-pressure"):
        return drop()
    root = marker.get("root") if isinstance(marker.get("root"), str) and marker.get("root") else cwd
    command = ("%s --root %s --path %s with your complete --workers inventory and --host-capable"
               % (python_command(os.path.join(os.path.dirname(LEDGER), "restart_preflight.py"), "restart"),
                  shlex.quote(root), shlex.quote(marker["path"])))
    label = LABEL.get(_AGENT, LABEL["claude"])
    note = occupancy_note(occupancy, breakdown)
    message = ""
    if marker.get("reason") == "human-hold" and not marker.get("reoffered"):
        cleared = not marker.get("caller_hold") or event_name == "UserPromptSubmit"
        if cleared:
            try:
                import handoff_holds
                cleared = not handoff_holds.applicable_holds(
                    handoff_holds.read_holds(marker.get("root") or cwd), record.topic)
            except Exception:
                cleared = False  # unreadable holds fail closed: stay quiet
        if not cleared or record.held:
            return ""
        marker.update(reoffered=True, turns=0)
        if marker.get("caller_hold"):
            # The hook cannot see an ask_user card: a mailbox notification is a prompt too.
            status = ("a new prompt arrived since the hold. If a question to the user is still "
                      "unanswered, keep waiting (or pass --hold). Otherwise, finish")
        else:
            status = "the hold has cleared. Finish"
        message = ("%s This session's context-pressure restart was blocked by a human hold; %s "
                   "the action in progress, refresh the checkpoint %s if the state moved, then retry: "
                   "%s. Context occupancy is ~%s tokens (threshold %s). %s"
                   % (label, status, marker.get("path"), command, format(occupancy, ","),
                      format(limit, ","), note))
    elif event_name == "UserPromptSubmit":
        marker["turns"] = marker.get("turns", 0) + 1
        if marker["turns"] >= RESTART_REMIND_TURNS and occupancy >= limit:
            marker["turns"] = 0
            message = ("%s The context-pressure restart is still blocked (%s) and occupancy is ~%s "
                       "tokens, over the %s-token threshold. The checkpoint %s stays open: resolve the "
                       "cause, refresh it, then retry: %s. %s"
                       % (label, marker.get("reason") or "unknown", format(occupancy, ","),
                          format(limit, ","), marker.get("path"), command, note))
    else:
        return ""
    _save_restart_block(path, marker)
    return message


def build_message(occupancy, pending, breakdown, limit, source, model, skill,
                  agent="claude", second=False, usage_age=None):
    cache_read = breakdown.get("cache_read", 0)
    cache_share = (cache_read / occupancy * 100.0) if occupancy else 0.0
    detail = "cache-read %s of it (%.0f%%)" % (format(cache_read, ","), cache_share)
    counted = " " + occupancy_note(occupancy, breakdown)
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
    ) + counted
    if auto_mode_on():
        message += (" Fully automatic mode is active: after publication succeeds, follow "
                    "the save result's transition.action, then stop. A paused transition "
                    "preserves the checkpoint for explicit continuation.")
    elif autoresume_on():
        message += (" Autoresume is active: after the handoff is written, tell the user "
                    "to type /clear — the cleared session will announce the open handoff "
                    "and resume it automatically.")
    return message


def build_floor_message(floor, threshold, floor_limit, model, agent):
    # Smallest threshold that clears the floor by max(10k, 10% of itself).
    suggest = -(-max(floor + 10000, floor / 0.9) // 1000) * 1000
    return (
        "%s The %s-token handoff threshold for %s is below this session's startup "
        "context (~%s tokens at the first model call), so an early handoff would free "
        "nothing. No handoff notice is sent until context pressure nears the window "
        "limit (~%s tokens). Tell the user: set HANDOFF_AT (or CONTEXT_WATCH_TOKENS) "
        "to at least ~%s."
        % ("handoff-status:", format(threshold, ","),
           model or "unknown model", format(floor, ","), format(floor_limit, ","),
           format(int(suggest), ","))
    )


def advisory(agent, evt, event_name, cwd, session_id, path, occupancy, decision, latch, wd, floor_probe):
    """Early warning a lead before the threshold (23079574): own latch, never publishes or restarts."""
    global _DIAG
    lead, lead_source, error = wd.warning_lead(warning_window_candidates(cwd))
    if error:
        if claim_once(evt, ".warning-config"):
            _DIAG = (event_name, "handoff-status: %s; the context-watch early warning is off until it is "
                     "fixed. Continue the current task." % error)
        return
    advised = os.path.exists(path)
    action = wd.advise(occupancy, lead, decision, latch, advised)
    if action == wd.ADVISE:
        # Measured only when the advisory would fire (lazy): startup context on the
        # occupancy basis (the notice's floor measure); the session note's
        # input-only figure is the fallback when the trace is too long.
        startup = floor_probe()
        if startup is None:
            noted = _ledger().read_session_note(cwd, session=session_id).get('startup_input_tokens')
            startup = noted if type(noted) is int else None
        action = wd.advise(occupancy, lead, decision, latch, advised, startup)
    if action == wd.ADVISORY_REARM:
        try:
            os.remove(path)
        except OSError:
            pass
        clear_nudges(path)  # the spent Stop blocks re-arm with the advisory (#180)
        return
    if action != wd.ADVISE:
        return
    point = max(0, decision.limit - lead)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write("%d/%d/%d\n" % (occupancy, point, decision.limit))
    except FileExistsError:
        sys.exit(0)
    except OSError:
        return  # fail closed: without a latch it would repeat on every callback
    log_event({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agent": agent, "occupancy": occupancy,
               "threshold": decision.limit, "threshold_source": decision.source, "advisory_point": point,
               "warning_lead": lead, "warning_lead_source": lead_source, "notice": "advisory",
               "session_id": session_id, "cwd": cwd})
    emit(agent, event_name,
         "[context-watch] Early warning: context ~%s tokens, past the advisory point %s (lead %s [%s] before "
         "the %s-token handoff threshold [%s]). Keep working on the current task; this is not "
         "a pause and not a cue to hand the turn back to the user. As you go, track progress in your own working "
         "notes. Take no handoff step now: the notice at the threshold says when; do not save yet."
         % (format(occupancy, ','), format(point, ','), format(lead, ','),
                                          lead_source, format(decision.limit, ','), decision.source), "warn")


def mandatory_status(evt, agent, cwd, model, window, window_source):
    """Diagnostics only; supported Claude enforcement lives in the sealed gate."""
    global _DIAG, _V3_INVALID, _V3_INPUTS
    import mandatory_gate
    cwd = mandatory_gate.project_root(cwd)
    try:
        with open(os.path.join(cwd, '.context-watch.json'), encoding='utf-8') as stream:
            project = json.load(stream)
    except (OSError, ValueError):
        return
    if not isinstance(project, dict) or project.get('handoff_v3') is not True:
        return
    _V3_INPUTS = mandatory_gate.v3_inputs(model, window, window_source)
    try:
        point, source, _, _ = mandatory_gate.timing(project, cwd, sys.modules[__name__], *_V3_INPUTS)
    except ValueError as exc:
        _V3_INVALID = True
        message = 'invalid mandatory configuration: %s; automatic v3 transitions disabled.' % exc
    else:
        if point is None:
            return
        if agent == 'claude' and os.path.isfile(os.path.join(os.path.dirname(__file__), '..', 'runtime-manifest.json')):
            return
        message = ('mandatory-unenforced: %s has no installed supported mandatory gate; '
                   'effective mandatory_at_tokens=%s [%s]. Advisory only.' % (agent, point, source))
    if claim_once(evt, '.mandatory-config'):
        _DIAG = (evt['hook_event_name'], 'handoff-status: ' + message)


def emit(agent, event_name, message, mode):
    global _DIAG, _OPENING_NOTE
    if _OPENING_NOTE:
        message, _OPENING_NOTE = _OPENING_NOTE + ' ' + message, ''
    if _DIAG and _DIAG[0] == event_name:
        message, _DIAG = _DIAG[1] + " " + message, None
    if agent == "claude" or event_name == "PostToolUse":
        # Codex PostToolUse takes the same JSON: additionalContext is added as
        # developer context and the tool result is kept; block replaces it.
        if mode == "block" and event_name == "PostToolUse":
            emit_output({"decision": "block", "reason": message})
        else:
            emit_output({
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": message,
                }
            })
        sys.exit(0)
    print(message)  # Codex UserPromptSubmit: stdout becomes extra context for the turn
    sys.exit(0)


# ---------------------------------------------------------------- main

_AGENT, _EVT = "claude", {}


def main():
    if env("CONTEXT_WATCH_DISABLE") == "1":
        sys.exit(0)

    global _DIAG, _AGENT, _EVT, _OPENING_NOTE
    evt = _EVT = read_event()
    if in_subagent(evt):
        sys.exit(0)
    event_name = evt.get("hook_event_name") or ""
    if event_name == "Stop":
        handle_stop(evt)
    transcript_path = evt.get("transcript_path") or ""
    entries = load_transcript_tail(transcript_path) if transcript_path else []
    agent = _AGENT = detect_agent(evt, entries)
    lean = event_name == 'UserPromptSubmit' and lean_resume_prompt(evt.get('prompt'))
    if lean:
        # This prompt is a host identity observation even before its transcript
        # exists. Keep the command's session and the terminal pointer aligned.
        write_terminal_pointer(evt.get('cwd') or os.getcwd(), session_key(evt))
        _OPENING_NOTE = bounded_opening_note(evt)
    elif event_name == 'UserPromptSubmit' and restart_prompt(evt.get('prompt')):
        # The kit's own restart prompt is the successor's first identity
        # observation: record it before adopt reads the pointer (14752baa).
        write_terminal_pointer(evt.get('cwd') or os.getcwd(), session_key(evt))
    # A first prompt may precede the transcript file; PostToolUse never does.
    if (TAIL_STATUS and event_name in ("PostToolUse", "UserPromptSubmit")
            and not (TAIL_STATUS == "missing" and event_name == "UserPromptSubmit")
            and claim_once(evt, ".telemetry")):
        _DIAG = (event_name, "handoff-status: Context telemetry unavailable (transcript %s); context-pressure "
                 "warnings are off for this session until it is readable. Continue the current task." % TAIL_STATUS)

    if event_name in ("PostToolUse", "UserPromptSubmit") and not _DIAG:
        ignored = project_enables_disable_only_key(evt.get("cwd") or os.getcwd())
        if ignored and claim_once(evt, ".user-only-config"):
            _DIAG = (event_name, "handoff-status: %s: true in this project's .context-watch.json is ignored: "
                     "a project may disable auto, never enable it (enable it in "
                     "~/.context-watch/thresholds.json or HANDOFF_AUTO). "
                     "Continue the current task." % ", ".join('"%s"' % k for k in ignored))

    if event_name == "SessionStart":
        handle_session_start(evt, agent)

    if event_name not in ("PostToolUse", "UserPromptSubmit"):
        sys.exit(0)

    session_id = session_key(evt)
    if not session_id:
        sys.exit(0)
    first, second = latch_path(session_id), latch_path(session_id, 2)
    advised_latch = first + ".advised"

    import watch_decision as wd
    cfg = wd.WatchConfig.from_environ(os.environ)
    model = evt.get("model")  # Codex includes it in hook input; Claude Code does not
    usage = claude_usage(entries) if agent == "claude" else codex_usage(entries)
    if not usage[0] or usage[0].get("occupancy", 0) <= 0:
        if event_name == "UserPromptSubmit" and not lean:
            route_opening_prompt(evt, agent)  # no API call yet: the opening prompt
        sys.exit(0)
    estimate = estimate_pending(evt)
    if agent == "claude":
        # ponytail: Claude only; Codex rollouts' write order around hooks is unverified.
        unrecorded = unrecorded_results(entries, evt.get("tool_use_id")) if cfg.pending else 0
        seen = wd.claude_observation(cfg, usage, model, estimate, unrecorded, PENDING_CAP)
    else:
        seen = wd.codex_observation(cfg, usage, model, estimate)
    breakdown, model, window, window_source = seen.breakdown, seen.model, seen.window, seen.window_source
    pending, pending_raw, unrecorded, thinking = seen.pending, seen.pending_raw, seen.unrecorded, seen.thinking
    occupancy = seen.occupancy

    cwd = evt.get("cwd") or os.getcwd()
    limit, source = wd.cap_threshold(*resolve_threshold(model, window, cwd), window, window_source, cfg.reserve)
    mandatory_status(evt, agent, cwd, model, window, window_source)
    write_session_note(cwd, evt, os.path.exists(first), dict(observed_occupancy_tokens=occupancy,
                       threshold=limit, threshold_source=source, window=window,
                       window_source=window_source, model=model))
    fired = os.path.exists(first)
    floor_latch = handoff_identity.latch_floor_path(session_id)
    floored = os.path.exists(floor_latch)
    first_occupancy, first_base = read_first_latch(first) if fired else (0, None)
    latch = wd.Latch(fired=fired, second=os.path.exists(second), floored=floored,
                     floor_startup=read_first_latch(floor_latch)[0] if floored else 0,
                     first_occupancy=first_occupancy, first_base=first_base)

    def floor_probe():
        try:
            if os.path.getsize(transcript_path) <= TAIL_BYTES:
                return floor_occupancy(entries, agent)
        except OSError:
            pass
        return None

    decision = wd.decide(occupancy, breakdown["occupancy"], limit, source, window, cfg.reserve, latch,
                         floor_probe, lambda: handoff_written_since(cwd, session_id))
    if decision.action == wd.REARM:
        for path in (first, second, floor_latch, first + ".cleared",
                     first + ".announced", first + ".restart-blocked", advised_latch):
            try:
                os.remove(path)  # the window was compacted: re-arm
            except OSError:
                pass
        clear_nudges(first)
        clear_nudges(advised_latch)
        sys.exit(0)
    limit, source, floor = decision.limit, decision.source, decision.floor
    if decision.below_floor:
        if decision.record_floor:
            try:
                with open_private(floor_latch) as f:
                    f.write("%d/0/0\n" % (floor or 0))
            except OSError:
                pass
        write_session_note(cwd, evt, os.path.exists(first), dict(threshold=limit, threshold_source=source))
    if decision.action == wd.QUIET:
        notice = restart_block_notice(cwd, session_id, event_name, occupancy, limit, breakdown)
        if notice:
            emit(agent, event_name, notice, "warn")
        advisory(agent, evt, event_name, cwd, session_id, advised_latch, occupancy, decision, latch, wd, floor_probe)
        remind_unwritten_handoff(agent, evt, event_name, cwd, session_id, occupancy)
        sys.exit(0)
    note_only = decision.action == wd.FLOOR_NOTE

    if not note_only:
        try:
            fd = os.open(second if fired else first, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write("%d/%d/%d\n" % (occupancy, limit, breakdown["occupancy"]))
        except FileExistsError:
            remind_unwritten_handoff(agent, evt, event_name, cwd, session_id, occupancy)
            sys.exit(0)
        except Exception:
            pass
        write_session_note(cwd, evt, True)

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
        "notice": decision.log_notice,
        "floor": floor,
        "breakdown": breakdown,
        "session_id": session_id,
        "cwd": cwd,
    })

    skill, mode = cfg.skill, cfg.mode
    if note_only:
        emit(agent, event_name, build_floor_message(floor, decision.configured_limit, decision.floor_limit, model, agent), "warn")
    from handoff_protocol import restart_guard
    guard = restart_guard(cwd, session_id) if auto_mode_on() else None
    message = build_message(occupancy, pending, breakdown, limit, source, model, skill,
                            agent, second=fired, usage_age=usage_age)
    message += " Session identity: %s. Publish with %s." % (
        session_id, command_template(LEDGER, ('save', '--session', session_id), '--input <draft.json>'))
    note = _ledger().read_session_note(cwd, session=session_id)
    startup = note.get('startup_input_tokens')
    message += (" Context window ~%s [%s]; startup ~%s; working room at startup ~%s tokens. "
                "Configure HANDOFF_AT or per-model ~/.context-watch/thresholds.json."
                % (format(window, ','), window_source, format(startup, ',') if type(startup) is int else 'unknown',
                   format(limit - startup, ',') if type(startup) is int else 'unknown'))
    if guard and guard['pause_reasons']:
        message = ("[context-watch] Context pressure; automatic handoff paused (%s). Finish the atomic action, "
                   "publish the checkpoint with %s, "
                   "then follow transition.action and stop. The checkpoint stays open for explicit continuation. "
                   "Growth diagnostics: %s." % (', '.join(guard['pause_reasons']),
                                              command_template(LEDGER, ('save', '--session', session_id), '--input <draft.json>'),
                                              json.dumps(guard)))
    elif auto_mode_on() and env("AGENTSROOM_AGENT_ID"):
        # The AgentsRoom agent restarts its own tab, even with an inherited TMUX_PANE.
        command = python_command(os.path.join(os.path.dirname(LEDGER), 'restart_preflight.py'))
        message += (" Fully automatic mode in AgentsRoom: after outcome saved and kind agents_restart, run "
                    "%s restart --root <project> with your complete --workers inventory and --host-capable, as "
                    "transition.action states. Only outcome ready with dispatch true permits agents_restart, as "
                    "the final tool action with the returned restart_prompt; then end the turn. Blocked means "
                    "manual fresh-session continuation; pending needs reconciliation, not redispatch."
                    % command)
    if guard and guard['reasons'] and not guard['pause_reasons']:
        message += ' Thrash warning: %s; save checks predecessor progress before deciding to pause.' % ', '.join(guard['reasons'])
    message += (" Minimal save contract: JSON topic (short kebab-case, e.g. fix-login-bug), description, body with ## Objective, ## Current state, "
                "## Next steps; preserve authorization, unresolved decisions and effects already performed. "
                "Include decisions and code anchors for planned edits. Require only excerpts needed for the "
                "immediate action; defer other docs/skills until needed. Save supplies metadata and lineage. "
                "This contract suffices under pressure; load the template only for extra drafting guidance.")
    emit(agent, event_name, message, mode)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == 'opening-note':
        try:
            from handoff_protocol import hook_opening_note, read_cli_input
            try:
                raw = read_cli_input('-')
            except UnicodeDecodeError:
                invalid_hook_input()
            note = hook_opening_note(json.loads(raw))
            if note:
                print(note)
        except Exception:
            pass
        sys.exit(0)
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
        flush_diag(_AGENT)
        raise
    except Exception as exc:
        fail_open(exc, _EVT, once=_MISSING_INSTALL is None)
