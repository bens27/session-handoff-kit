"""One-shot, script-deduced resume CLI: every outcome names the next action."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'


class ResumeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        (self.root / '.handoffs').mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
                                 'HANDOFF_SKILL_ROOTS', 'CODEX_HOME', 'HANDOFF_AUTO')}
        self.env.update(HOME=self.tmp.name, TMPDIR=self.tmp.name, CONTEXT_WATCH_ORIGIN='test',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='')

    def run_cli(self, *args, input=None, terminal=None):
        env = dict(self.env)
        if terminal:
            env['HANDOFF_TERMINAL_ID'] = terminal
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=self.root, env=env,
                           input=input, capture_output=True, text=True, timeout=30)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def save(self, topic='work', terminal=None, session='old', **extra):
        doc = dict(topic=topic, description='About ' + topic, body=BODY, **extra)
        code, out = self.run_cli('save', '--session', session, '--request-id', 'r-' + topic,
                                 input=json.dumps(doc), terminal=terminal)
        self.assertEqual(code, 0, out)
        return out['path']

    def test_lookup_marks_and_selects_this_terminals_handoff(self):
        mine = self.save('alpha', terminal='term-a')
        self.save('beta', terminal='term-b')
        _, out = self.run_cli('lookup', terminal='term-a')
        self.assertEqual(out['items'][0]['path'], mine)
        self.assertEqual([i['mine'] for i in out['items']], [True, False])
        self.assertEqual(out['selected'], mine)
        _, out = self.run_cli('lookup')
        self.assertNotIn('selected', out)
        self.assertEqual([i['mine'] for i in out['items']], [False, False])

    def test_lookup_selects_the_only_handoff(self):
        only = self.save('alpha', terminal='term-a')
        _, out = self.run_cli('lookup', terminal='term-z')
        self.assertEqual(out['selected'], only)
        self.assertFalse(out['items'][0]['mine'])

    def skill(self, root, name, text):
        path = Path(root) / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_prepare_builds_skill_catalog_from_installed_roots(self):
        self.skill(Path(self.tmp.name) / '.agents/skills', 'tdd', 'agents tdd')
        self.skill(Path(self.tmp.name) / '.claude/skills', 'tdd', 'claude tdd')
        self.skill(Path(self.tmp.name) / '.codex/skills', 'lint', 'codex lint')
        path = self.save(skills=['tdd', 'lint'])
        code, out = self.run_cli('prepare', path, '--session', 'new', '--execute')
        self.assertEqual((code, out['outcome']), (0, 'prepared'), out)
        self.assertEqual({s['name']: s['text'] for s in out['skills']}, {'tdd': 'agents tdd', 'lint': 'codex lint'})

    def test_handoff_skill_roots_take_precedence_and_missing_skill_needs_context(self):
        override = Path(self.tmp.name) / 'override'
        self.skill(override, 'tdd', 'override tdd')
        self.skill(Path(self.tmp.name) / '.agents/skills', 'tdd', 'agents tdd')
        self.env['HANDOFF_SKILL_ROOTS'] = str(override)
        path = self.save(skills=['tdd'])
        _, out = self.run_cli('prepare', path, '--session', 'new', '--execute')
        self.assertEqual(out['skills'][0]['text'], 'override tdd', out)
        missing = self.save('other', skills=['absent'])
        code, out = self.run_cli('prepare', missing, '--session', 'new2', '--execute')
        self.assertEqual((code, out['outcome']), (1, 'needs-context'))
        self.assertIn('absent', out['action'])

    def pointer(self, terminal, session, age=0):
        key = os.path.realpath(self.root) + '\0' + terminal
        name = 'context-watch-terminal-%s.json' % hashlib.sha1(key.encode()).hexdigest()[:20]
        (Path(self.tmp.name) / name).write_text(json.dumps(dict(session_id=session, ts=time.time() - age)))

    def test_save_defaults_session_from_env_and_request_id_from_content(self):
        self.env['HANDOFF_SESSION_ID'] = 'env-session'
        doc = json.dumps(dict(topic='work', description='d', body=BODY))
        code, first = self.run_cli('save', input=doc)
        self.assertEqual((code, first['outcome']), (0, 'saved'), first)
        self.assertIn('session_id: env-session\n', Path(first['path']).read_text())
        _, retry = self.run_cli('save', input=doc)
        self.assertEqual((retry['outcome'], retry['path']), ('saved', first['path']))

    def test_prepare_defaults_session_from_terminal_pointer(self):
        path = self.save()
        self.pointer('term-a', 'hook-session')
        code, out = self.run_cli('prepare', path, '--execute', terminal='term-a')
        self.assertEqual((code, out['outcome']), (0, 'prepared'), out)
        self.assertIn('claim_owner: hook-session', Path(path).read_text())

    def test_missing_or_stale_session_blocks_with_flag_named(self):
        path = self.save()
        self.pointer('term-a', 'hook-session', age=90000)
        for terminal in ('term-a', None):
            code, out = self.run_cli('prepare', path, '--execute', terminal=terminal)
            self.assertEqual((code, out['outcome']), (1, 'blocked'), out)
            self.assertIn('--session', out['action'])

    def failing(self, *ids):
        return "printf '%s'; exit 1" % ''.join('FAILED %s - AssertionError\\n' % i for i in ids)

    def test_verify_accepts_failures_already_recorded_at_save_time(self):
        known = 'tests/test_x.py::Case::test_known'
        path = self.save(verify=self.failing(known), verify_baseline=[known])
        self.assertIn('verify_baseline: ' + known + '\n', Path(path).read_text())
        self.run_cli('prepare', path, '--session', 'new', '--execute')
        code, out = self.run_cli('verify', path, '--session', 'new')
        self.assertEqual((code, out['outcome'], out['baseline_failures']), (0, 'verified', [known]), out)
        self.assertIn('already recorded at save time', out['action'])
        code, out = self.run_cli('acknowledge', path, '--session', 'new')
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)

    def test_verify_reports_new_and_fixed_failures_against_baseline(self):
        old, gone, new = 'tests/a.py::t_old', 'tests/a.py::t_gone', 'tests/b.py::C::t_new'
        path = self.save(verify=self.failing(old, new), verify_baseline=[old, gone])
        self.run_cli('prepare', path, '--session', 'new', '--execute')
        code, out = self.run_cli('verify', path, '--session', 'new')
        self.assertEqual((code, out['outcome']), (1, 'verification-failed'), out)
        self.assertEqual((out['new_failures'], out['fixed']), ([new], [gone]))
        code, out = self.run_cli('acknowledge', path, '--session', 'new')
        self.assertEqual(out['outcome'], 'blocked')

    def test_resume_runs_prepare_verify_and_acknowledge_in_one_call(self):
        self.skill(Path(self.tmp.name) / '.agents/skills', 'tdd', 'red then green')
        (self.root / 'notes.txt').write_text('keep the API stable\n')
        path = self.save(skills=['tdd'], references=['notes.txt'], verify='true')
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome'], out['path']), (0, 'resumed', path), out)
        self.assertEqual(out['next_step'], 'Run the parser checks.')
        self.assertEqual(out['skills'], [dict(name='tdd', text='red then green')])
        self.assertEqual(out['references'][0]['text'], 'keep the API stable\n')
        self.assertEqual(out['verification']['outcome'], 'verified')
        self.assertIn('## Objective', out['body'])
        self.assertEqual(out['action'], 'Continue with next_step.')
        self.assertIn('status: resumed', Path(path).read_text())

    def test_oversized_resume_output_leaves_the_checkpoint_open(self):
        big = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\n' + 'x' * 19000 + '\n'
        code, out = self.run_cli('save', '--session', 'old', '--request-id', 'r-big',
                                 input=json.dumps(dict(topic='big', description='Big', body=big)))
        self.assertEqual(code, 0, out)
        path = out['path']
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome']), (1, 'needs-context'), out)
        self.assertNotIn('status: resumed', Path(path).read_text())
        self.assertNotIn('resumed_by', Path(path).read_text())

    def test_resume_asks_to_choose_among_several_and_accepts_a_topic(self):
        self.save('alpha')
        beta = self.save('beta')
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome']), (0, 'choose'), out)
        self.assertEqual(sorted(c['topic'] for c in out['candidates']), ['alpha', 'beta'])
        self.assertEqual(set(out['candidates'][0]), {'path', 'topic', 'description', 'mine'})
        self.assertEqual(out['action'], 'Rerun resume with one of these paths, or none.')
        code, out = self.run_cli('resume', 'beta', '--session', 'new')
        self.assertEqual((code, out['outcome'], out['path']), (0, 'resumed', beta), out)

    def test_resume_reports_none_and_stops_at_the_failing_stage(self):
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome']), (0, 'none'), out)
        path = self.save(verify='exit 3')
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome'], out['stage']), (1, 'verification-failed', 'verify'), out)
        self.assertNotIn('status: resumed', Path(path).read_text())
        self.save('other', skills=['absent'])
        code, out = self.run_cli('resume', 'other', '--session', 'new2')
        self.assertEqual((code, out['outcome'], out['stage']), (1, 'needs-context', 'prepare'), out)

    def test_fields_limits_output_to_named_keys_plus_outcome_and_action(self):
        path = self.save()
        _, out = self.run_cli('lookup', '--fields', 'selected,total')
        self.assertEqual(out, dict(outcome='available', action=out['action'], selected=path, total=1))
        _, out = self.run_cli('resume', '--session', 'new', '--fields', 'next_step')
        self.assertEqual(set(out), {'outcome', 'action', 'next_step'})
        _, out = self.run_cli('acknowledge', path, '--session', 'new', '--fields', 'path')
        self.assertEqual(set(out), {'outcome', 'action', 'path'})

    def test_wait_polls_until_saved_then_until_resumed(self):
        env = dict(self.env)
        waiter = subprocess.Popen([sys.executable, str(LEDGER), 'wait', 'work', '--status', 'saved',
                                   '--interval', '0.1', '--timeout', '20'], cwd=self.root, env=env,
                                  stdout=subprocess.PIPE, text=True)
        time.sleep(0.5)
        path = self.save()
        stdout, _ = waiter.communicate(timeout=25)
        out = json.loads(stdout)
        self.assertEqual((waiter.returncode, out['outcome'], out['path'], out['status']), (0, 'reached', path, 'saved'))
        self.assertIn('resume ' + path, out['action'])
        code, out = self.run_cli('wait', path, '--status', 'resumed', '--timeout', '0.3', '--interval', '0.1')
        self.assertEqual((code, out['outcome']), (1, 'timeout'), out)
        self.assertIn('--timeout', out['action'])
        self.run_cli('resume', '--session', 'new')
        code, out = self.run_cli('wait', 'work', '--status', 'resumed', '--timeout', '1')
        self.assertEqual((code, out['outcome'], out['path']), (0, 'reached', path), out)

    def test_protocol_script_resumes_a_bare_topic(self):
        # handoff_ledger.py `resume <path>` alone stays the legacy direct transfer;
        # handoff_protocol.py always runs the one-shot workflow.
        path = self.save()
        env = dict(self.env, HANDOFF_SESSION_ID='new')
        p = subprocess.run([sys.executable, str(LEDGER.with_name('handoff_protocol.py')), 'resume', 'work'],
                           cwd=self.root, env=env, capture_output=True, text=True, timeout=30)
        out = json.loads(p.stdout)
        self.assertEqual((p.returncode, out['outcome'], out['path']), (0, 'resumed', path), out)
        self.assertIn('resumed_by: new', Path(path).read_text())


if __name__ == '__main__':
    unittest.main()
