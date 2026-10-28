"""Resume staleness verdict: fresh / progressed / superseded / conflicting (ticket #99)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))
import handoff_staleness as staleness  # noqa: E402

TICKET = '6cf33871-1ff9-4d27-a175-a869d775ee84'
OTHER = '222a49df-730b-437d-99e3-05c3acda0a29'


def backlog(state):
    """A ticket lookup that answers STATE for every ticket."""
    return lambda ticket: state


def offline(ticket):
    raise staleness.BacklogUnavailable('AgentsRoom is not reachable')


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t',
                        GIT_COMMITTER_EMAIL='t@example.com', GIT_CONFIG_NOSYSTEM='1', HOME=self.root)
        self.git('init', '-q', '-b', 'main')
        self.base = self.commit('init')

    def git(self, *args):
        return subprocess.run(['git', '-C', self.root, *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message, ticket=None):
        if ticket:
            message += '\n\nTicket: ' + ticket
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def publish(self):
        """Point origin/main at HEAD, as a fetch would."""
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def verdict(self, lookup=None, **fm):
        return staleness.classify(self.root, fm, lookup or offline)


class VerdictTests(GitFixture):
    def test_fresh_when_main_moved_without_this_ticket(self):
        self.commit('unrelated', ticket=OTHER)
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET, base=self.base)
        self.assertEqual((v['verdict'], v['source']), ('fresh', 'git+backlog'), v)
        self.assertIn('1 commit', v['reason'])

    def test_progressed_when_main_carries_this_tickets_trailer(self):
        landed = self.commit('part one', ticket=TICKET)
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'progressed', v)
        self.assertIn(landed[:7], v['reason'])

    def test_short_ticket_id_matches_full_trailer(self):
        self.commit('part one', ticket=TICKET.upper())
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET[:8], base=self.base)
        self.assertEqual(v['verdict'], 'progressed', v)

    # -- Opus r1 S1: only contract-shaped trailers count ----------------------
    def test_malformed_trailers_never_match(self):
        for value in ('f', TICKET[:8] + '-other', TICKET[:7], TICKET + 'x'):
            with self.subTest(value=value):
                self.assertFalse(staleness.ticket_matches(TICKET, value))
                self.assertFalse(staleness.ticket_matches(TICKET[:8], value))
        self.commit('one letter', ticket='f')
        self.commit('suffixed', ticket=TICKET[:8] + '-other')
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'fresh', v)

    def test_ticket_ids_compare_by_short_id_or_whole_uuid(self):
        twin = TICKET[:8] + '-0000-4000-8000-000000000000'  # same short id, other ticket
        self.assertTrue(staleness.ticket_matches(TICKET, TICKET[:8]))
        self.assertTrue(staleness.ticket_matches(TICKET[:8], TICKET.upper()))
        self.assertTrue(staleness.ticket_matches(TICKET, ' %s ' % TICKET))
        self.assertFalse(staleness.ticket_matches(TICKET, twin))
        self.assertFalse(staleness.ticket_matches(TICKET, OTHER[:8]))

    def test_superseded_when_ticket_is_closed(self):
        self.commit('landed', ticket=TICKET)
        self.publish()
        v = self.verdict(backlog('closed'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'superseded', v)

    def test_conflicting_when_base_is_not_on_origin_main(self):
        self.git('checkout', '-q', '-b', 'side')
        side = self.commit('side work')
        self.git('checkout', '-q', 'main')
        self.commit('main work')
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET, base=side)
        self.assertEqual(v['verdict'], 'conflicting', v)
        self.assertIn('origin/main', v['reason'])

    # -- Opus r1 S3: origin/main behind base is not a conflict ----------------
    def test_origin_main_behind_base_is_unclassified(self):
        self.publish()  # origin/main at the initial commit
        landed_locally = self.commit('newer than the fetched origin/main')
        v = self.verdict(backlog('open'), ticket=TICKET, base=landed_locally)
        self.assertEqual(v['verdict'], 'unclassified', v)
        self.assertIn('origin/main is behind base', v['reason'])
        self.assertIn('fetch', v['reason'])

    def test_origin_behind_base_still_reports_closed_or_missing_ticket(self):
        # Opus r2 N1: "behind" must not hide what the backlog says.
        self.publish()
        landed_locally = self.commit('newer than the fetched origin/main')
        closed = self.verdict(backlog('closed'), ticket=TICKET, base=landed_locally)
        self.assertEqual(closed['verdict'], 'superseded', closed)
        missing = self.verdict(backlog('missing'), ticket=TICKET, base=landed_locally)
        self.assertEqual(missing['verdict'], 'conflicting', missing)
        unknown = self.verdict(offline, ticket=TICKET, base=landed_locally)
        self.assertEqual((unknown['verdict'], unknown['source']), ('unclassified', 'git-only'), unknown)
        self.assertIn('origin/main is behind base', unknown['reason'])

    def test_conflicting_when_base_commit_is_gone(self):
        self.publish()
        v = self.verdict(backlog('open'), ticket=TICKET, base='a' * 40)
        self.assertEqual(v['verdict'], 'conflicting', v)

    def test_conflicting_beats_superseded(self):
        self.publish()
        v = self.verdict(backlog('closed'), ticket=TICKET, base='a' * 40)
        self.assertEqual(v['verdict'], 'conflicting', v)

    def test_conflicting_when_ticket_is_missing_from_backlog(self):
        self.publish()
        v = self.verdict(backlog('missing'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'conflicting', v)
        self.assertIn('backlog', v['reason'])

    def test_offline_falls_back_to_git_only(self):
        self.commit('part one', ticket=TICKET)
        self.publish()
        v = self.verdict(offline, ticket=TICKET, base=self.base)
        self.assertEqual((v['verdict'], v['source']), ('progressed', 'git-only'), v)
        self.assertIn('not reachable', v['backlog'])

    def test_offline_with_no_movement_is_fresh_git_only(self):
        self.publish()
        v = self.verdict(offline, ticket=TICKET, base=self.base)
        self.assertEqual((v['verdict'], v['source']), ('fresh', 'git-only'), v)

    def test_lookup_crash_never_escapes(self):
        def broken(ticket):
            raise RuntimeError('boom')
        self.publish()
        v = self.verdict(broken, ticket=TICKET, base=self.base)
        self.assertEqual(v['source'], 'git-only', v)

    def test_no_metadata_is_unclassified_without_lookup(self):
        def never(ticket):
            raise AssertionError('no ticket, no lookup')
        v = self.verdict(never)
        self.assertEqual(v['verdict'], 'unclassified', v)

    def test_malformed_metadata_is_ignored(self):
        v = self.verdict(backlog('closed'), ticket='#95', base='HEAD')
        self.assertEqual(v['verdict'], 'unclassified', v)

    def test_missing_origin_main_uses_ticket_state_only(self):
        v = self.verdict(backlog('closed'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'superseded', v)
        v = self.verdict(backlog('open'), ticket=TICKET, base=self.base)
        self.assertEqual(v['verdict'], 'unclassified', v)
        self.assertIn('origin/main', v['reason'])

    def test_ticket_without_base_uses_ticket_state(self):
        self.publish()
        self.assertEqual(self.verdict(backlog('closed'), ticket=TICKET)['verdict'], 'superseded')
        self.assertEqual(self.verdict(backlog('open'), ticket=TICKET)['verdict'], 'fresh')

    def test_base_without_ticket_is_git_only(self):
        self.commit('later')
        self.publish()
        v = self.verdict(backlog('closed'), base=self.base)
        self.assertEqual((v['verdict'], v['source']), ('fresh', 'git-only'), v)


if __name__ == '__main__':
    unittest.main()
