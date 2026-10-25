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
                # written=never: once both latches exist the handoff probe must be skipped.
                got = decide(occ, latch=latch, floor=lambda: None, written=never)
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



def advise(occupancy, limit=130_000, lead=30_000, latch=Latch(), advised=False, floor=lambda: None,
           startup=None):
    """The advisory rule on top of decide(), both on one occupancy basis (23079574)."""
    decision = decide(occupancy, limit=limit, latch=latch, floor=floor, written=lambda: False)
    return decision, wd.advise(occupancy, lead, decision, latch, advised, startup)


class Advisory(unittest.TestCase):
    """23079574: warning_window_tokens advisory before the publication notice."""

    def test_default_lead_and_point(self):
        self.assertEqual(wd.DEFAULT_WARNING_LEAD, 30_000)
        self.assertIsNone(advise(99_999)[1])
        self.assertEqual(advise(100_000)[1], wd.ADVISE)
        self.assertEqual(advise(129_999)[1], wd.ADVISE)

    def test_advisory_before_publication_and_never_instead_of_it(self):
        decision, got = advise(100_000)
        self.assertEqual((decision.action, got), (wd.QUIET, wd.ADVISE))
        # One callback jumping across both points: the notice fires, no advisory.
        decision, got = advise(140_000)
        self.assertEqual((decision.action, decision.notice, got), (wd.NOTICE, 1, None))
        # The advisory latch never suppresses the notice.
        decision, got = advise(130_000, advised=True)
        self.assertEqual((decision.action, got), (wd.NOTICE, None))

    def test_once_per_window_and_rearm(self):
        self.assertIsNone(advise(110_000, advised=True)[1], 'duplicate callback')
        self.assertIsNone(advise(65_001, advised=True)[1])
        self.assertEqual(advise(64_999, advised=True)[1], wd.ADVISORY_REARM, 'compaction under half the limit')
        self.assertEqual(advise(55_000, advised=True)[1], wd.ADVISORY_REARM, 'between point/2 and limit/2')
        self.assertIsNone(advise(49_999)[1], 'nothing to re-arm without the latch')
        # Point under limit/2 (lead 80k, point 50k): re-arm below the point, never at or above it,
        # so a window sitting past the point cannot re-arm and re-fire on alternate callbacks.
        self.assertEqual(advise(28_000, lead=80_000, advised=True)[1], wd.ADVISORY_REARM)
        self.assertIsNone(advise(55_000, lead=80_000, advised=True)[1])

    def test_no_advisory_when_the_point_is_at_the_startup_floor(self):
        # HANDOFF_AT=50000 with a 27k startup: the point (20k) is under startup + margin.
        self.assertIsNone(advise(27_000, limit=50_000, startup=27_000)[1])
        self.assertIsNone(advise(105_000, startup=95_000)[1], 'point 100k within 10k of startup')
        self.assertEqual(advise(105_000, startup=89_000)[1], wd.ADVISE)
        self.assertEqual(advise(105_000, startup=None)[1], wd.ADVISE, 'unknown startup: no guard')

    def test_no_advisory_once_the_notice_fired_or_floored(self):
        fired = Latch(fired=True, first_occupancy=131_000, first_base=1)
        self.assertIsNone(advise(120_000, latch=fired)[1])
        self.assertIsNone(advise(150_000, latch=Latch(floored=True, floor_startup=128_000))[1])
        decision, got = advise(135_000, floor=lambda: 128_000)  # floor note instead
        self.assertEqual((decision.action, got), (wd.FLOOR_NOTE, None))

    def test_same_occupancy_basis_with_a_large_response_allowance(self):
        # e9b68a08: recorded 93.6k + 6,495 session-max allowance = 100,095 estimated.
        cfg = WatchConfig(pending=True)
        seen = wd.claude_observation(cfg, ({'occupancy': 93_600, 'max_output': 6_495}, 'opus'),
                                     None, (0, 0), 0, 25_000)
        self.assertEqual(seen.occupancy, 100_095)
        self.assertEqual(advise(seen.occupancy)[1], wd.ADVISE)
        seen = wd.claude_observation(cfg, ({'occupancy': 123_600, 'max_output': 6_495}, 'opus'),
                                     None, (0, 0), 0, 25_000)
        self.assertEqual(advise(seen.occupancy)[0].action, wd.NOTICE, 'notice at ~123.5k measured')

    def test_point_follows_the_resolved_threshold(self):
        self.assertIsNone(advise(100_000, limit=140_000)[1], 'model or config change moves the point')
        self.assertEqual(advise(110_000, limit=140_000)[1], wd.ADVISE)
        self.assertEqual(advise(1, limit=20_000)[1], wd.ADVISE, 'lead over the threshold: max(0, ...)')
        self.assertIsNone(advise(129_000, lead=0)[1], 'zero lead disables the advisory')
        self.assertIsNone(advise(129_000, lead=None)[1], 'invalid config disables the advisory')

    def test_warning_lead_validation_and_precedence(self):
        lead = wd.warning_lead
        self.assertEqual(lead([]), (30_000, 'builtin-default', None))
        self.assertEqual(lead([('env', None), ('project', 20_000), ('user', 10_000)]),
                         (20_000, 'project', None))
        self.assertEqual(lead([('env', '5000'), ('project', 20_000)]), (5_000, 'env', None))
        self.assertEqual(lead([('project', 0)]), (0, 'project', None))
        for bad in (-1, True, 1.5, '30000', None.__class__, [1]):
            with self.subTest(bad=bad):
                got = lead([('user', bad)])
                self.assertIsNone(got[0])
                self.assertIn('user', got[2])
        self.assertIsNone(lead([('env', 'abc')])[0])
        self.assertIsNone(lead([('env', '\u00b2')])[0], 'superscript digit: isdigit() but not int()')
        self.assertIsNone(lead([('env', '\u0663')])[0], 'non-ASCII digit refused too')
        # A malformed lower-precedence value still refuses: strict validation.
        self.assertIsNone(lead([('project', 20_000), ('user', -5)])[0])

if __name__ == '__main__':
    unittest.main()
