#!/usr/bin/env python3
"""watch-decision: when the context watcher speaks, as a pure function.

context_watch.main is the host adapter: it reads the hook event, the transcript
tail, the environment and the latch files, then performs what this module
decides. Nothing here touches the filesystem, the environment or a subprocess,
so the latch and re-arm rules are table-tested in tests/test_watch_decision.py.

  WatchConfig.from_environ(environ)   CONTEXT_WATCH_* settings, parsed once
  claude_observation / codex_observation
                                      what each host's transcript says about
                                      the window, the model and pending tokens
  cap_threshold(...)                  a host-reported window caps the threshold
  Latch                               the latch files, as read by the adapter
  decide(...)                         rearm | quiet | notice | floor-note, plus
                                      the floor latch to record and the limit

Stdlib only; imports no other hook.
"""

from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_WINDOW = 200_000
DEFAULT_RESERVE = 20_000     # room kept free for writing the handoff (reported windows only)
REARM_FACTOR = 0.5           # occupancy below 50% (a compaction) re-arms the latch
SECOND_NOTICE_FACTOR = 1.25  # re-fire once at 125% of the threshold, and 25% past the first
SECOND_NOTICE_MARGIN = 5_000  # the second notice is capped this far under the window
FLOOR_MARGIN = 10_000        # a threshold within this (or 10%) of startup context is useless

REARM, QUIET, NOTICE, FLOOR_NOTE = 'rearm', 'quiet', 'notice', 'floor-note'


def _int(raw, default):
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class WatchConfig:
    """The CONTEXT_WATCH_* settings the pressure path reads (see context_watch's docstring)."""
    window: int = DEFAULT_WINDOW       # CONTEXT_WATCH_WINDOW, assumed when the host reports none
    reserve: int = DEFAULT_RESERVE     # CONTEXT_WATCH_RESERVE
    pending: bool = True               # CONTEXT_WATCH_PENDING != "0"
    thinking: Optional[int] = None     # CONTEXT_WATCH_THINKING override, None when unset/invalid
    skill: str = 'session-handoff'     # CONTEXT_WATCH_SKILL
    mode: str = 'warn'                 # CONTEXT_WATCH_MODE, stripped and lower-cased

    @classmethod
    def from_environ(cls, environ):
        get = environ.get
        thinking = None
        if get('CONTEXT_WATCH_THINKING', '').strip():
            thinking = _int(get('CONTEXT_WATCH_THINKING'), None)
        return cls(window=_int(get('CONTEXT_WATCH_WINDOW', str(DEFAULT_WINDOW)), DEFAULT_WINDOW),
                   reserve=_int(get('CONTEXT_WATCH_RESERVE', str(DEFAULT_RESERVE)), DEFAULT_RESERVE),
                   pending=get('CONTEXT_WATCH_PENDING', '1') != '0',
                   thinking=thinking,
                   skill=get('CONTEXT_WATCH_SKILL', 'session-handoff'),
                   mode=get('CONTEXT_WATCH_MODE', 'warn').strip().lower())


# ------------------------------------------------------------------ observations

@dataclass(frozen=True)
class Observation:
    """One host's reading of the window: usage breakdown and pending tokens."""
    breakdown: dict
    model: Optional[str]
    window: int
    window_source: str     # 'assumed' | 'reported'
    pending: int           # estimate + unrecorded results + thinking allowance
    pending_raw: int
    unrecorded: int
    thinking: int

    @property
    def base(self):
        """Occupancy the transcript's last usage entry recorded."""
        return self.breakdown.get('occupancy', 0) if self.breakdown else 0

    @property
    def occupancy(self):
        return self.breakdown['occupancy'] + self.pending


def _observe(cfg, breakdown, model, window, window_source, estimate, unrecorded, thinking):
    pending, pending_raw = estimate
    if cfg.thinking is not None:
        thinking = cfg.thinking
    return Observation(breakdown=breakdown, model=model, window=window, window_source=window_source,
                       pending=pending + unrecorded + thinking, pending_raw=pending_raw,
                       unrecorded=unrecorded, thinking=thinking)


def claude_observation(cfg, usage, event_model, estimate, unrecorded, thinking_cap):
    """Claude Code: the window is assumed; the response that issued this call
    (thinking included) is written only after the hook runs, so assume it is as
    large as the session's largest so far, up to thinking_cap (the adapter's
    PENDING_CAP). `unrecorded` is only counted while CONTEXT_WATCH_PENDING is on."""
    breakdown, tmodel = usage
    thinking = min((breakdown or {}).get('max_output', 0), thinking_cap) if cfg.pending else 0
    return _observe(cfg, breakdown, event_model or tmodel, cfg.window, 'assumed', estimate,
                    unrecorded if cfg.pending else 0, thinking)


def codex_observation(cfg, usage, event_model, estimate):
    """Codex: a reported window wins over the assumed one; no unrecorded-result
    or thinking allowance (rollout write order around hooks is unverified)."""
    breakdown, reported, tmodel = usage
    window, source = (reported, 'reported') if reported else (cfg.window, 'assumed')
    return _observe(cfg, breakdown, event_model or tmodel, window, source, estimate, 0, 0)


def cap_threshold(limit, source, window, window_source, reserve):
    """Only a host-reported window caps the threshold: an assumed 200k would
    clamp every 1M-context model's threshold to 180k."""
    if window_source == 'reported':
        cap = window - reserve
        if 0 < cap < limit:
            source = '%s, capped from %s to window %s - reserve %s' % (
                source, format(limit, ','), format(window, ','), format(reserve, ','))
            limit = cap
    return limit, source


# ------------------------------------------------------------------------ latch

@dataclass(frozen=True)
class Latch:
    """The session's latch files, as the adapter read them.

    fired / second: the first / second notice latch exists.
    floored: the floor latch exists; floor_startup is the startup context it
    recorded (0 when unknown). first_occupancy / first_base: what the first
    notice recorded (occupancy with pending, transcript usage), read when fired.
    """
    fired: bool = False
    second: bool = False
    floored: bool = False
    floor_startup: int = 0
    first_occupancy: int = 0
    first_base: Optional[int] = None


@dataclass(frozen=True)
class Decision:
    action: str                       # REARM | QUIET | NOTICE | FLOOR_NOTE
    limit: int                        # threshold the notice reports (floor-raised when below_floor)
    source: str
    configured_limit: int             # threshold before any floor raise
    floor: Optional[int] = None
    floor_limit: Optional[int] = None
    below_floor: bool = False         # the threshold sits under the startup context
    record_floor: bool = False        # write the floor latch ("<floor or 0>/0/0")
    notice: Optional[int] = None      # 1 or 2 for NOTICE

    @property
    def log_notice(self):
        return 'floor' if self.action == FLOOR_NOTE else self.notice


def decide(occupancy, base, limit, source, window, reserve, latch,
           floor_probe: Callable[[], Optional[int]], handoff_written: Callable[[], bool]):
    """What the watcher does for one PostToolUse/UserPromptSubmit observation.

    occupancy includes pending tokens; base is the transcript's own usage.
    floor_probe() returns the startup context when it can be measured (only
    asked for an unfired, unfloored session at or over its threshold);
    handoff_written() is asked only before a second notice. Any side effect a
    QUIET decision still carries (record_floor) is the adapter's to perform.
    """
    fired, floored = latch.fired, latch.floored
    # A floored session is watched against window - reserve; compaction cannot
    # drop it under the configured (startup-floor) threshold. A high startup
    # floor can never compact under half of window - reserve either: back
    # within FLOOR_MARGIN of the startup context counts as compacted.
    rearm_limit = max(window - reserve, limit) if floored else limit
    near_floor = floored and bool(latch.floor_startup) and occupancy < latch.floor_startup + FLOOR_MARGIN
    if (fired or floored) and (occupancy < rearm_limit * REARM_FACTOR or near_floor):
        return Decision(REARM, limit, source, limit)

    floor, below_floor, floor_limit = None, False, None
    if floored or (not fired and occupancy >= limit):
        # A threshold at or under the startup context would fire at once and
        # free nothing. Say so once, then watch the window-reserve limit
        # instead: pressure protection stays, the handoff loop cannot start.
        floor = (latch.floor_startup or None) if floored else floor_probe()
        if floored or (floor is not None and limit < floor + max(FLOOR_MARGIN, limit * 0.1)):
            floor_limit = max(window - reserve, limit)
            below_floor = True
            source = '%s, raised to window %s - reserve %s (startup context ~%s)' % (
                source, format(window, ','), format(reserve, ','), format(floor or 0, ','))
    configured_limit = limit
    if below_floor:
        limit = floor_limit
    common = dict(limit=limit, source=source, configured_limit=configured_limit, floor=floor,
                  floor_limit=floor_limit, below_floor=below_floor,
                  record_floor=below_floor and not floored)

    if latch.second:
        return Decision(QUIET, **common)  # both notices given; only a compaction re-arms
    need = limit
    if fired:
        if base == latch.first_base:
            # No model turn since the first notice (e.g. parallel tool results
            # landing together): it has not been seen yet, so it was not ignored.
            return Decision(QUIET, **common)
        if handoff_written():
            return Decision(QUIET, **common)  # the handoff was written; the notice was acted on
        need = max(limit * SECOND_NOTICE_FACTOR,
                   latch.first_occupancy + limit * (SECOND_NOTICE_FACTOR - 1))
        # Occupancy cannot pass the window: keep the second notice reachable.
        need = max(limit, min(need, window - SECOND_NOTICE_MARGIN))
    if below_floor and not floored and occupancy < need:
        return Decision(FLOOR_NOTE, **common)
    if occupancy < need:
        return Decision(QUIET, **common)
    return Decision(NOTICE, notice=2 if fired else 1, **common)


# Advisory window (23079574): an early, non-publishing warning a lead of
# warning_window_tokens before the resolved threshold. It has its own
# once-per-window latch, rearmed by compaction, and never touches the notice latch.
DEFAULT_WARNING_LEAD = 30_000
ADVISE, ADVISORY_REARM = 'advise', 'advisory-rearm'


def warning_lead(candidates):
    """Resolve warning_window_tokens from [(source, raw)], highest precedence first.

    raw None means absent. A source whose label starts with 'env' carries a
    string that must be all digits; file values must be a non-negative int
    (not bool). Any present malformed value refuses the whole setting:
    returns (None, None, error). Otherwise (lead, source, None).
    """
    chosen = None
    for source, raw in candidates:
        if raw is None:
            continue
        if source.startswith('env') and isinstance(raw, str):
            value = int(raw) if raw.isdigit() else None
        elif isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            value = raw
        else:
            value = None
        if value is None:
            return None, None, 'warning_window_tokens from %s must be a non-negative integer, got %r' % (source, raw)
        if chosen is None:
            chosen = (value, source, None)
    return chosen or (DEFAULT_WARNING_LEAD, 'builtin-default', None)


def advise(occupancy, lead, decision, latch, advised):
    """ADVISE once per window between limit - lead and limit; ADVISORY_REARM on compaction."""
    if lead is None or lead <= 0:
        return None
    point = max(0, decision.limit - lead)
    if advised and occupancy < point * REARM_FACTOR:
        return ADVISORY_REARM
    if advised or latch.fired or latch.floored or decision.below_floor or decision.action != QUIET:
        return None
    if point <= occupancy < decision.limit:
        return ADVISE
    return None
