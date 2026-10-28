"""Local-only report-back drafts through the public resume seam."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_protocol as protocol


class ReportBackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        env = patch.dict(os.environ, HOME=self.root, USERPROFILE=self.root,
                         HANDOFF_TERMINAL_ID='', AGENTSROOM_AGENT_ID='')
        env.start()
        self.addCleanup(env.stop)

    def save(self, **extra):
        return protocol.save(self.root, 'author', 'save-1', dict(
            topic='work', description='Continue work',
            body='## Requester\nLane D\n## Objective\nFix resume\n'
                 '## Current state\nTests pending\n## Next steps\nRun tests\n',
            **extra))['path']

    def test_resume_writes_addressed_draft_without_network_and_replay_preserves_edits(self):
        ticket = '68bb0fa0-e7e0-4241-b6b8-fb88c6afc9b5'
        path = self.save(ticket=ticket)
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            result = protocol.resume(self.root, 'successor', path)
        self.assertEqual(result['outcome'], 'resumed')
        info = result['report_back_draft']
        self.assertEqual(info['status'], 'draft')
        draft_path = Path(info['path'])
        draft = json.loads(draft_path.read_text())
        self.assertEqual(draft['requester'], 'Lane D')
        self.assertEqual(draft['ticket'], ticket)
        self.assertEqual(draft['checkpoint'], path)
        self.assertEqual(draft['status'], 'draft')
        self.assertFalse(draft['sent'])
        for phrase in ('Fix resume', 'Tests pending', 'Run tests', 'not sent'):
            self.assertIn(phrase, draft['message'])
        draft_path.write_text('Edited by successor')
        replay = protocol.resume(self.root, 'successor', path)
        self.assertEqual(replay['report_back_draft'], info)
        self.assertEqual(draft_path.read_text(), 'Edited by successor')

    def test_missing_addresses_stay_unspecified_and_projection_keeps_draft_link(self):
        from handoff_contract import project_output
        path = protocol.save(self.root, 'author', 'save-1', dict(
            topic='legacy', description='Legacy checkpoint',
            body='## Objective\nContinue\n## Current state\nReady\n## Next steps\nInspect\n'))['path']
        result = protocol.resume(self.root, 'successor', path)
        info = project_output(result, 'path,body')['report_back_draft']
        draft = json.loads(Path(info['path']).read_text())
        self.assertIsNone(draft['requester'])
        self.assertIsNone(draft['ticket'])
        self.assertIn('Requester: [unspecified]', draft['message'])
        self.assertIn('Ticket: [unspecified]', draft['message'])

    def test_retrieval_and_refused_resume_do_not_produce_drafts(self):
        path = self.save(verify='echo unsafe')
        self.assertEqual(protocol.prepare(path, self.root, 'reader')['outcome'], 'retrieved')
        # Remove local trust without altering the saved checkpoint content.
        key = Path(self.root) / '.context-watch/receipt.key'
        key.unlink()
        result = protocol.resume(self.root, 'successor', path)
        self.assertNotEqual(result['outcome'], 'resumed')
        self.assertNotIn('report_back_draft', result)
        self.assertEqual(list(Path(self.root).rglob('*.report-back.json')), [])

    def test_draft_keeps_full_saved_sections_without_rewriting_checkpoint_body(self):
        state = 'Saved evidence ' * 2000
        body = '## Objective\nContinue\n## Current state\n' + state + '\n## Next steps\nInspect\n'
        path = protocol.save(self.root, 'author', 'save-1', dict(
            topic='large', description='Large state', body=body))['path']
        before = Path(path).read_text().split('\n---\n', 1)[-1]
        result = protocol.resume(self.root, 'successor', path)
        draft = json.loads(Path(result['report_back_draft']['path']).read_text())
        self.assertIn(state.strip(), draft['message'])
        self.assertEqual(Path(path).read_text().split('\n---\n', 1)[-1], before)
        self.assertEqual(result['body'], before)

    def test_draft_write_failure_blocks_acknowledgment_and_retry_recovers(self):
        path = self.save()
        replace = os.replace

        def fail_draft(source, destination):
            if str(destination).endswith('.report-back.json'):
                raise OSError('disk full')
            return replace(source, destination)

        with patch('os.replace', side_effect=fail_draft):
            result = protocol.resume(self.root, 'successor', path)
        self.assertEqual((result['outcome'], result['stage']), ('blocked', 'report-back'))
        self.assertEqual(protocol.history(self.root)['items'][0]['status'], 'open')
        self.assertEqual(list(Path(self.root).rglob('*.report-back.json')), [])
        retry = protocol.resume(self.root, 'successor', path)
        self.assertEqual(retry['outcome'], 'resumed')
        self.assertTrue(Path(retry['report_back_draft']['path']).is_file())
