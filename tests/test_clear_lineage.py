#!/usr/bin/env python3
"""A /clear successor inherits its predecessor's handoff lineage (G30).

Incident 2026-11-22: a coordinator session resumed a checkpoint, worked six
hours, was /cleared without saving, and both successor sessions got
foreign_only. Claude Code writes {"type": "derived-from", "sessions": [{"id",
"via": "clear"}]} near the top of the new transcript. The kit reads it at
SessionStart clear (and in dispatch), walks the chain, and treats the
predecessors' checkpoints as this session's own. When the lineage saved
nothing after its last resume, resume says so plainly.

Run: python3 -m pytest -q tests/test_clear_lineage.py"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_identity  # noqa: E402

WRAPPERS = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'HERDR_ENV', 'HERDR_PANE_ID', 'HANDOFF_PROCESS_ANCHOR',
            'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID', 'HANDOFF_STATE_DIR')
BODY = '## Objective\nx\n## Current state\ny\n## Next steps\nz\n'


def transcript(folder, session, predecessor=None, via='clear'):
    head = [{'type': 'mode', 'mode': 'normal', 'sessionId': session}]
    if predecessor:
        head.append({'type': 'derived-from', 'v': 1, 'sessionId': session,
                     'sessions': [{'type': 'claude_code_session', 'id': predecessor, 'via': via}]})
    path = folder / (session + '.jsonl')
    path.write_text(''.join(json.dumps(r) + '\n' for r in head))
    return path


class LineageWalk(unittest.TestCase):
    def test_walks_clear_records_nearest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            transcript(d, 'aaa')
            transcript(d, 'bbb', 'aaa')
            path = transcript(d, 'ccc', 'bbb')
            self.assertEqual(handoff_identity.clear_lineage(str(path)), ['bbb', 'aaa'])

    def test_only_clear_counts_and_cycles_and_odd_ids_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertEqual(handoff_identity.clear_lineage(str(transcript(d, 'f1', 'f0', via='fork'))), [])
            transcript(d, 'c1', 'c2')
            self.assertEqual(handoff_identity.clear_lineage(str(transcript(d, 'c2', 'c1'))), ['c1'])
            self.assertEqual(handoff_identity.clear_lineage(str(transcript(d, 'x', '../etc/passwd'))), [])
            self.assertEqual(handoff_identity.clear_lineage(str(d / 'missing.jsonl')), [])
            self.assertEqual(handoff_identity.clear_lineage(''), [])


class ClearSuccessor(unittest.TestCase):
    """End to end: hook SessionStart clear, then dispatch, each in a new terminal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        self.proj = self.base / 'proj'
        (self.proj / '.handoffs').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.proj)], check=True)
        self.transcripts = self.base / 'claude-projects' / 'p'
        self.transcripts.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, terminal):
        env = {k: v for k, v in os.environ.items() if k not in WRAPPERS}
        env.update(HOME=str(self.base), TMPDIR=str(self.base), CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0',
                   CONTEXT_WATCH_JEV='0', HANDOFF_PROCESS_ANCHOR='0', HANDOFF_TERMINAL_ID=terminal)
        return env

    def call(self, args, stdin, terminal):
        p = subprocess.run([sys.executable, '-B'] + args, input=stdin, env=self.env(terminal), cwd=str(self.proj),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, (p.stderr + p.stdout)[-2000:])
        return p.stdout

    def save(self, topic, session, terminal):
        out = self.call([str(HOOKS / 'handoff_ledger.py'), 'save', '.', '--session', session, '--request-id', 'r-' + topic],
                       json.dumps(dict(topic=topic, description=topic + ' work', body=BODY)), terminal)
        return json.loads(out)['path']

    def mark_resumed(self, path, session, when):
        text = Path(path).read_text()
        text = text.replace('status: open', 'status: resumed\nresumed: %s\nresumed_by: %s' % (when, session), 1)
        Path(path).write_text(text)

    def start_after_clear(self, session, predecessor, terminal):
        path = transcript(self.transcripts, session, predecessor)
        evt = dict(hook_event_name='SessionStart', source='clear', session_id=session,
                   transcript_path=str(path), cwd=str(self.proj))
        self.call([str(HOOKS / 'context_watch.py')], json.dumps(evt), terminal)

    def dispatch(self, session, terminal):
        out = self.call([str(HOOKS / 'handoff_ledger.py'), 'dispatch', '--input', '-'],
                       json.dumps(dict(prompt='continue the handoff', session=session)), terminal)
        return json.loads(out)

    def test_successor_owns_what_its_predecessor_saved_in_another_terminal(self):
        self.save('other', 'someone-else', 'tab-other')
        mine = self.save('coordinator', 'pred', 'tab-before')
        transcript(self.transcripts, 'pred')
        self.start_after_clear('succ', 'pred', 'tab-after')
        r = self.dispatch('succ', 'tab-after')
        self.assertEqual((r['outcome'], r['code']), ('proceed', 'ok'), json.dumps(r)[:600])
        self.assertEqual(os.path.realpath(r['target']['path']), os.path.realpath(mine))

    def test_unsaved_predecessor_is_named_not_foreign_only(self):
        self.save('other', 'someone-else', 'tab-other')
        resumed = self.save('coordinator', 'older', 'tab-older')
        when = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 6 * 3600))
        self.mark_resumed(resumed, 'pred', when)
        transcript(self.transcripts, 'pred')
        for session, predecessor in (('succ1', 'pred'), ('succ2', 'succ1')):  # the incident's two successors
            self.start_after_clear(session, predecessor, 'tab-after')
            with self.subTest(session=session):
                r = self.dispatch(session, 'tab-after')
                self.assertNotEqual(r['code'], 'foreign_only', json.dumps(r)[:600])
                self.assertEqual(r['code'], 'lineage_unsaved', json.dumps(r)[:600])
                text = r['next_action']['text']
                self.assertIn('before /clear', text)
                self.assertIn('without a checkpoint since %s' % when, text)
                self.assertIn(os.path.basename(resumed), text)
                self.assertIn('only in git and the board', text)
                self.assertEqual(r['target']['lineage']['resumed_by'], 'pred')

    def test_lineage_without_clear_record_stays_foreign_only(self):
        self.save('other', 'someone-else', 'tab-other')
        resumed = self.save('coordinator', 'older', 'tab-older')
        self.mark_resumed(resumed, 'pred', time.strftime('%Y-%m-%dT%H:%M:%S'))
        path = transcript(self.transcripts, 'fresh')
        evt = dict(hook_event_name='SessionStart', source='startup', session_id='fresh',
                   transcript_path=str(path), cwd=str(self.proj))
        self.call([str(HOOKS / 'context_watch.py')], json.dumps(evt), 'tab-after')
        r = self.dispatch('fresh', 'tab-after')
        self.assertEqual((r['outcome'], r['code']), ('ask', 'foreign_only'), json.dumps(r)[:600])


if __name__ == '__main__':
    unittest.main()
