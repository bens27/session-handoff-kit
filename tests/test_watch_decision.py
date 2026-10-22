#!/usr/bin/env python3
"""Table tests for the pure watch-decision core (no files, no environment).

The subprocess tests in test_context_watch*.py stay the per-host smoke tests;
these pin the latch, re-arm, floor and second-notice rules directly.
"""

from pathlib import Path
import sys
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import watch_decision as wd
from watch_decision import Latch, WatchConfig

W, R = 200_000, 20_000  # window, reserve


def never():
    raise AssertionError('probe must not be asked here')


def decide(occupancy, limit=130_000, latch=Latch(), base=None, floor=never, written=never, window=W):
    return wd.decide(occupancy, occupancy if base is None else base, limit, 'default', window, R,
                     latch, floor, written)


class Config(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(WatchConfig.from_environ({}), WatchConfig())
        self.assertEqual(WatchConfig(), WatchConfig(200_000, 20_000, True, None, 'session-handoff', 'warn'))

    def test_parsing_matches_the_old_inline_reads(self):
        rows = [
            (dict(CONTEXT_WATCH_WINDOW='1000000'), 'window', 1_000_000),
            (dict(CONTEXT_WATCH_WINDOW='big'), 'window', 200_000),
            (dict(CONTEXT_WATCH_WINDOW=''), 'window', 200_000),
            (dict(CONTEXT_WATCH_RESERVE=' 5000 '), 'reserve', 5_000),
            (dict(CONTEXT_WATCH_RESERVE='x'), 'reserve', 20_000),
            (dict(CONTEXT_WATCH_PENDING='0'), 'pending', False),
            (dict(CONTEXT_WATCH_PENDING='false'), 'pending', True),  # only "0" disables
            (dict(CONTEXT_WATCH_THINKING='0'), 'thinking', 0),
            (dict(CONTEXT_WATCH_THINKING=' 7000\n'), 'thinking', 7_000),
            (dict(CONTEXT_WATCH_THINKING='  '), 'thinking', None),
            (dict(CONTEXT_WATCH_THINKING='lots'), 'thinking', None),
            (dict(CONTEXT_WATCH_SKILL='other'), 'skill', 'other'),
            (dict(CONTEXT_WATCH_MODE=' Block '), 'mode', 'block'),
        ]
        for environ, field, want in rows:
            with self.subTest(environ=environ):
                self.assertEqual(getattr(WatchConfig.from_environ(environ), field), want)


class Observations(unittest.TestCase):
    def test_claude_adds_unrecorded_and_capped_thinking(self):
        seen = wd.claude_observation(WatchConfig(), (dict(occupancy=100, max_output=40_000), 'm-t'), None,
                                     (10, 99), 5, 25_000)
        self.assertEqual((seen.pending, seen.pending_raw, seen.unrecorded, seen.thinking), (25_015, 99, 5, 25_000))
        self.assertEqual((seen.occupancy, seen.base, seen.model), (25_115, 100, 'm-t'))
        self.assertEqual((seen.window, seen.window_source), (200_000, 'assumed'))

    def test_claude_pending_off_drops_allowances_but_thinking_override_wins(self):
        cfg = WatchConfig(pending=False)
        seen = wd.claude_observation(cfg, (dict(occupancy=100, max_output=9), None), 'evt', (0, 0), 5, 25_000)
        self.assertEqual((seen.pending, seen.model), (0, 'evt'))
        cfg = WatchConfig(pending=False, thinking=300)
        self.assertEqual(wd.claude_observation(cfg, (dict(occupancy=1), None), None, (0, 0), 5, 25_000).pending, 300)

    def test_codex_reported_window_and_no_allowances(self):
        seen = wd.codex_observation(WatchConfig(), (dict(occupancy=50, max_output=9_000), 258_400, 'gpt'), None, (7, 7))
        self.assertEqual((seen.window, seen.window_source, seen.pending, seen.model), (258_400, 'reported', 7, 'gpt'))
        seen = wd.codex_observation(WatchConfig(window=1_000, thinking=4), (dict(occupancy=50), None, None), 'evt', (0, 0))
        self.assertEqual((seen.window, seen.window_source, seen.pending, seen.model), (1_000, 'assumed', 4, 'evt'))

    def test_cap_threshold_only_for_reported_windows(self):
        rows = [
            ((300_000, 's', 258_400, 'reported'), (238_400, 's, capped from 300,000 to window 258,400 - reserve 20,000')),
            ((300_000, 's', 200_000, 'assumed'), (300_000, 's')),
            ((100_000, 's', 258_400, 'reported'), (100_000, 's')),   # already under the cap
            ((100_000, 's', 10_000, 'reported'), (100_000, 's')),    # non-positive cap ignored
        ]
        for args, want in rows:
            with self.subTest(args=args):
                self.assertEqual(wd.cap_threshold(*args, R), want)


class Decide(unittest.TestCase):
    def test_first_notice_and_quiet_rows(self):
        rows = [
            ('under threshold', 129_999, Latch(), wd.QUIET, None),
            ('at threshold', 130_000, Latch(), wd.NOTICE, 1),
            ('both notices given', 199_000, Latch(fired=True, second=True, first_base=1), wd.QUIET, None),
        ]
        for name, occ, latch, action, notice in rows:
            with self.subTest(name):
                got = decide(occ, latch=latch, floor=lambda: None, written=lambda: False)
                self.assertEqual((got.action, got.notice), (action, notice))

    def test_rearm_below_half_threshold(self):
        for latch in (Latch(fired=True), Latch(fired=True, second=True), Latch(floored=True)):
            with self.subTest(latch=latch):
                limit = 130_000 if not latch.floored else 180_000  # floored: window - reserve
                self.assertEqual(decide(limit // 2 - 1, latch=latch).action, wd.REARM)
        self.assertEqual(decide(64_999).action, wd.QUIET, 'an unfired session has nothing to re-arm')

    def test_floored_rearms_near_startup_context(self):
        latch = Latch(floored=True, floor_startup=135_000)
        self.assertEqual(decide(144_999, latch=latch).action, wd.REARM)
        self.assertEqual(decide(145_000, latch=latch).action, wd.QUIET)
        self.assertEqual(decide(144_999, latch=Latch(floored=True)).action, wd.QUIET, 'unknown startup')

    def test_second_notice_rows(self):
        first = Latch(fired=True, first_occupancy=140_000, first_base=120_000)
        # need = max(130k * 1.25, 140k + 130k * .25) = 172.5k, under window - 5k
        self.assertEqual(decide(172_499, latch=first, written=lambda: False).action, wd.QUIET)
        got = decide(172_500, latch=first, written=lambda: False)
        self.assertEqual((got.action, got.notice), (wd.NOTICE, 2))
        self.assertEqual(decide(199_000, latch=first, base=120_000).action, wd.QUIET, 'no model turn yet')
        self.assertEqual(decide(199_000, latch=first, written=lambda: True).action, wd.QUIET, 'handoff written')

    def test_second_notice_stays_reachable_near_the_window(self):
        first = Latch(fired=True, first_occupancy=185_000, first_base=1)
        # 185k + 180k * .25 = 230k would be unreachable; capped at window - 5k = 195k
        self.assertEqual(decide(194_999, limit=180_000, latch=first, written=lambda: False).action, wd.QUIET)
        self.assertEqual(decide(195_000, limit=180_000, latch=first, written=lambda: False).notice, 2)

    def test_floor_detection_raises_threshold_and_notes_once(self):
        got = decide(135_000, floor=lambda: 128_000)  # 130k < 128k + 13k
        self.assertEqual(got.action, wd.FLOOR_NOTE)
        self.assertEqual((got.limit, got.configured_limit, got.floor, got.floor_limit), (180_000, 130_000, 128_000, 180_000))
        self.assertTrue(got.below_floor and got.record_floor)
        self.assertEqual(got.log_notice, 'floor')
        self.assertEqual(got.source, 'default, raised to window 200,000 - reserve 20,000 (startup context ~128,000)')
        self.assertEqual(decide(135_000, floor=lambda: 110_000).action, wd.NOTICE, 'floor far enough under')
        self.assertEqual(decide(135_000, floor=lambda: None).action, wd.NOTICE, 'floor unmeasurable')

    def test_floored_session_watches_window_minus_reserve(self):
        latch = Latch(floored=True, floor_startup=128_000)
        quiet = decide(179_999, latch=latch)
        self.assertEqual((quiet.action, quiet.limit, quiet.below_floor, quiet.record_floor), (wd.QUIET, 180_000, True, False))
        got = decide(180_000, latch=latch)
        self.assertEqual((got.action, got.notice, got.limit), (wd.NOTICE, 1, 180_000))

    def test_quiet_after_floor_raise_still_records_the_floor(self):
        got = decide(135_000, latch=Latch(second=True), floor=lambda: 128_000)
        self.assertEqual((got.action, got.record_floor), (wd.QUIET, True))

    def test_probes_are_lazy(self):
        decide(100_000)                                            # under threshold: no floor probe
        decide(199_000, latch=Latch(fired=True, first_base=1), written=lambda: True)  # fired: no floor probe
        decide(100_000, latch=Latch(fired=True, first_base=1), written=lambda: False)


if __name__ == '__main__':
    unittest.main()
