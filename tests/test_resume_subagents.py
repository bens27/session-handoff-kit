"""#7: a checkpoint lists the Agent-tool subagents its session launched, and
resume reports each as live, finished or stopped, so a successor does not
relaunch builders that are still running."""
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
LEDGER = HOOKS / 'handoff_ledger.py'
sys.path.insert(0, str(HOOKS))
import handoff_identity  # noqa: E402
import handoff_runs  # noqa: E402

BODY = '## Objective\nFix it.\n## Current state\nTwo builders launched.\n## Next steps\nWait for them.\n'
OLD = 600


def entry(kind, content, stop_reason=None):
    message = dict(role=kind, content=content)
    if stop_reason:
        message['stop_reason'] = stop_reason
    return dict(type=kind, message=message)


WORKING = [entry('user', 'Build #7.'),
           entry('assistant', [dict(type='tool_use', name='Bash', input={})], 'tool_use')]


class SubagentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = self.base / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        (self.base / 'skills').mkdir()
        (self.base / 'state').mkdir()
        self.transcript = self.base / 'claude' / 'sess-a.jsonl'
        self.transcript.parent.mkdir()
        self.transcript.write_text('')
        self.folder = self.base / 'claude' / 'sess-a' / 'subagents'
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
                                 'HANDOFF_SKILL_ROOTS', 'CODEX_HOME', 'HANDOFF_AUTO', 'HANDOFF_RUN_ID')}
        self.env.update(HOME=str(self.base), TMPDIR=str(self.base), HANDOFF_STATE_DIR=str(self.base / 'state'),
                        CONTEXT_WATCH_ORIGIN='test', CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0',
                        CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='', HANDOFF_SKILL_ROOTS=str(self.base / 'skills'))

    def agent(self, agent_id, entries, description='Builder', age=0, meta=None):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / ('agent-%s.jsonl' % agent_id)
        path.write_text(''.join(json.dumps(e) + '\n' for e in entries))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        (self.folder / ('agent-%s.meta.json' % agent_id)).write_text(
            json.dumps(dict(dict(agentType='general-purpose', description=description), **(meta or {}))))

    def states(self):
        field = handoff_runs.subagent_snapshot(str(self.transcript))
        return {line.split()[1]: line.split()[2] for line in handoff_runs.subagent_report(field)}

    def cli(self, *args, input=None, terminal='term-a'):
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root,
                           env=dict(self.env, HANDOFF_TERMINAL_ID=terminal),
                           input=input, capture_output=True, text=True, timeout=60)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return json.loads(p.stdout)

    def save_and_resume(self):
        note = handoff_identity.session_note_path(str(self.root), 'sess-a', str(self.base / 'state'))
        Path(note).write_text(json.dumps(dict(session_id='sess-a', ts=time.time(), transcript_path=str(self.transcript))))
        out = self.cli('save', '.', '--session', 'sess-a', '--request-id', 'req-a',
                       input=json.dumps(dict(topic='builders', description='Builders', body=BODY)))
        self.assertEqual(out['outcome'], 'saved', out)
        resumed = self.cli('resume', 'builders', '--session', 'sess-b', terminal='term-b')
        self.assertEqual(resumed['outcome'], 'resumed', resumed)
        return Path(out['path']).read_text(), resumed

    def test_live_subagent(self):
        self.agent('a1live', WORKING)
        self.assertEqual(self.states(), {'a1live': 'live'})

    def test_finished_by_meta_handback_or_end_turn(self):
        self.agent('a2meta', WORKING, age=OLD, meta=dict(finished=True))
        self.agent('a3handback', WORKING + [entry('assistant', [dict(type='tool_use', name='SubagentHandback',
                                                                         input=dict(message='done'))], 'tool_use')])
        self.agent('a4end', WORKING + [entry('assistant', [dict(type='text', text='Done.')], 'end_turn')])
        self.assertEqual(self.states(), {'a2meta': 'finished', 'a3handback': 'finished', 'a4end': 'finished'})

    def test_stopped_subagent(self):
        self.agent('a5stopped', WORKING, age=OLD)
        self.assertEqual(self.states(), {'a5stopped': 'stopped'})

    def test_no_subagents(self):
        self.assertEqual(handoff_runs.subagent_snapshot(str(self.transcript)), '')
        self.assertEqual(handoff_runs.subagent_report(''), [])
        text, resumed = self.save_and_resume()
        self.assertNotIn('subagents:', text)
        self.assertFalse([l for l in resumed.get('workspace', []) if l.startswith('subagent:')], resumed)

    def test_checkpoint_lists_and_resume_reports_each_subagent(self):
        self.agent('a1live', WORKING, description='Build #7 hooks')
        self.agent('a4end', WORKING + [entry('assistant', [dict(type='text', text='Done.')], 'end_turn')],
                   description='Verify #3')
        self.agent('a5stopped', WORKING, description='Build #6', age=OLD)
        text, resumed = self.save_and_resume()
        field = next(l for l in text.splitlines() if l.startswith('subagents: '))
        recorded = json.loads(field[len('subagents: '):])
        self.assertEqual([(a['id'], a['description']) for a in recorded['agents']],
                         [('a1live', 'Build #7 hooks'), ('a4end', 'Verify #3'), ('a5stopped', 'Build #6')])
        lines = [l for l in resumed.get('workspace', []) if l.startswith('subagent:')]
        self.assertEqual(lines, ['subagent: a1live live Build #7 hooks', 'subagent: a4end finished Verify #3',
                                 'subagent: a5stopped stopped Build #6'], resumed)

    def test_tampered_ids_and_folders_build_no_path(self):
        field = json.dumps(dict(dir=str(self.folder), agents=[dict(id='../../etc/passwd'), dict(id='ok')]))
        self.assertEqual(handoff_runs.subagent_report(field), ['subagent: ok stopped'])
        self.assertEqual(handoff_runs.subagent_report(json.dumps(dict(dir='/etc', agents=[dict(id='ok')]))), [])


if __name__ == '__main__':
    unittest.main()
