"""G36: a parent (or a fresh subagent it starts) continues a subagent's checkpoint.

A subagent saves its checkpoint at ~/.context-watch/subagents/<parent>/<agent_id>.md
(G33). `handoff_ledger.py subagent-resume <agent_id> --session <parent>` finds it
by the agent_id under the parent session or any session in its /clear lineage,
claims it once, and returns its Next steps. The subagent's own session need not
be live. Every test runs with HOME in a temporary folder.

Run: python3 -m pytest -q tests/test_subagent_parent_resume.py"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import context_watch  # noqa: E402
import handoff_protocol  # noqa: E402

BODY = '## Findings so far\nThe bug is in parse().\n## Work in progress\nHalf a fix.\n## Next steps\nFinish parse() and run its test.\n'


class SubagentParentResume(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.root = os.path.join(self.home, 'project')
        os.makedirs(self.root)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('HANDOFF_', 'CLAUDE_', 'CODEX_'))}
        env['HOME'] = self.home
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, agent_id, parent):
        path = context_watch.subagent_checkpoint_path(agent_id, parent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(BODY)
        return path

    def resume(self, agent_id, session, lineage=()):
        with mock.patch.object(handoff_protocol, 'session_lineage', return_value=list(lineage)):
            return handoff_protocol.subagent_resume(self.root, session, agent_id)

    def test_parent_claims_and_gets_next_steps(self):
        path = self.save('a-7', 'parent-1')
        result = self.resume('a-7', 'parent-1')
        self.assertEqual(result['outcome'], 'resumed', result)
        self.assertEqual(result['path'], path)
        self.assertEqual(result['next_step'], 'Finish parse() and run its test.')
        self.assertIn('Half a fix.', result['body'])

    def test_found_through_the_parents_clear_lineage(self):
        path = self.save('a-7', 'parent-before-clear')
        result = self.resume('a-7', 'parent-after-clear', lineage=['parent-before-clear'])
        self.assertEqual((result['outcome'], result['path']), ('resumed', path))

    def test_nearest_session_in_the_lineage_wins(self):
        self.save('a-7', 'oldest')
        near = self.save('a-7', 'nearer')
        self.assertEqual(self.resume('a-7', 'now', lineage=['nearer', 'oldest'])['path'], near)

    def test_claimed_once_a_second_session_is_refused_the_same_one_is_not(self):
        self.save('a-7', 'parent-1')
        self.assertEqual(self.resume('a-7', 'parent-1')['outcome'], 'resumed')
        self.assertEqual(self.resume('a-7', 'parent-1')['outcome'], 'resumed', 'the claimant may read it again')
        other = self.resume('a-7', 'stranger', lineage=['parent-1'])
        self.assertEqual(other['outcome'], 'claimed', other)
        self.assertEqual(other['claimed_by'], 'parent-1')

    def test_another_parents_subagent_is_not_found(self):
        self.save('a-7', 'someone-else')
        self.assertEqual(self.resume('a-7', 'parent-1')['outcome'], 'none')

    def test_no_checkpoint_and_bad_ids(self):
        self.assertEqual(self.resume('a-7', 'parent-1')['outcome'], 'none')
        for bad in ('', '../x', 'a/b'):
            with self.subTest(agent_id=bad):
                self.assertEqual(self.resume(bad, 'parent-1')['outcome'], 'invalid')

    def test_cli_works_with_the_subagent_session_gone(self):
        path = self.save('a-7', 'parent-1')
        p = subprocess.run([sys.executable, str(HOOKS / 'handoff_ledger.py'), 'subagent-resume', 'a-7',
                            '--session', 'parent-1', '--root', self.root],
                           capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        result = json.loads(p.stdout)
        self.assertEqual((result['outcome'], result['path']), ('resumed', path))


if __name__ == '__main__':
    unittest.main()
