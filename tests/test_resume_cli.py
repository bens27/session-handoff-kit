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
                    if k not in ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID',
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
        code, out = self.run_cli('save', '.', '--session', session, '--request-id', 'r-' + topic,
                                 input=json.dumps(doc), terminal=terminal)
        self.assertEqual(code, 0, out)
        return out['path']

    def test_resume_extracts_next_step_under_relaxed_headings(self):
        for n, heading in enumerate(('## Next steps (ordered)', '## Next steps ', '## Next steps')):
            topic = 'relaxed-%d' % n
            body = BODY.replace('## Next steps', heading, 1).replace('Run the parser checks.', '1. FIRST\n2. second')
            code, saved = self.run_cli('save', '.', '--session', 'old', '--request-id', 'r-' + topic,
                                       input=json.dumps(dict(topic=topic, description='d', body=body)))
            self.assertEqual(code, 0, saved)
            _, out = self.run_cli('resume', saved['path'], '--session', 'new-%d' % n)
            self.assertEqual(out['outcome'], 'resumed', out)
            self.assertEqual(out['next_step'], '1. FIRST\n2. second', heading)

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

    def test_lookup_requires_selection_for_foreign_terminal_but_preserves_portable_recovery(self):
        only = self.save('alpha', terminal='term-a')
        _, out = self.run_cli('lookup', terminal='term-z')
        self.assertNotIn('selected', out)
        self.assertEqual(out['items'][0]['path'], only)
        self.assertFalse(out['items'][0]['mine'])
        _, own = self.run_cli('lookup', terminal='term-a')
        self.assertEqual(own['selected'], only)
        _, portable = self.run_cli('lookup')
        self.assertEqual(portable['selected'], only)

    def test_lookup_and_resume_share_explicit_untagged_session_context(self):
        session_a = 'session-' + 'a' * 200 + '-owner'
        session_b = 'session-' + 'a' * 200 + '-other'
        own = self.save('untagged', session=session_a)
        _, out = self.run_cli('lookup', '--session', session_a, terminal='term-a')
        self.assertEqual(out['selected'], own)
        self.assertTrue(out['items'][0]['mine'])
        _, other = self.run_cli('lookup', '--session', session_b, terminal='term-a')
        self.assertNotIn('selected', other)
        self.assertFalse(other['items'][0]['mine'])
        code, resumed = self.run_cli('resume', '--session', session_a, terminal='term-a')
        self.assertEqual((code, resumed['outcome']), (0, 'resumed'))

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
        code, first = self.run_cli('save', '.', input=doc)
        self.assertEqual((code, first['outcome']), (0, 'saved'), first)
        self.assertIn('session_id: env-session\n', Path(first['path']).read_text())
        _, retry = self.run_cli('save', '.', input=doc)
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
        summary = '%d failed in 0.10s\\n' % len(ids)
        return "printf '%s'; exit 1" % (''.join('FAILED %s - AssertionError\\n' % i for i in ids) + summary)

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

    def test_verify_baseline_match_does_not_hide_errors_or_a_missing_summary(self):
        known = 'tests/test_x.py::Case::test_known'
        failed = 'FAILED %s - AssertionError\\n' % known
        for name, output in (('error', failed + 'ERROR tests/t2.py - ImportError\\n1 failed, 1 error in 0.10s\\n'),
                             ('crash', failed)):
            path = self.save(topic=name, verify="printf '%s'; exit 1" % output, verify_baseline=[known])
            self.run_cli('prepare', path, '--session', 'new', '--execute')
            code, out = self.run_cli('verify', path, '--session', 'new')
            self.assertEqual((code, out['outcome']), (1, 'verification-failed'), (name, out))
            code, out = self.run_cli('acknowledge', path, '--session', 'new')
            self.assertEqual(out['outcome'], 'blocked', name)

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
        self.assertEqual(out['next_step_section'], 'Next steps')
        self.assertEqual(out['skills'], [dict(name='tdd', text='red then green')])
        self.assertEqual(out['references'][0]['text'], 'keep the API stable\n')
        self.assertEqual(out['verification']['outcome'], 'verified')
        self.assertIn('## Objective', out['body'])
        self.assertEqual(out['action'], 'Continue with ## Next steps in the delivered body.')
        self.assertIn('status: resumed', Path(path).read_text())

    def test_oversized_resume_output_is_delivered_and_acknowledged(self):
        big = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\n' + 'x' * 19000 + '\n'
        code, out = self.run_cli('save', '.', '--session', 'old', '--request-id', 'r-big',
                                 input=json.dumps(dict(topic='big', description='Big', body=big)))
        self.assertEqual(code, 0, out)
        path = out['path']
        code, out = self.run_cli('resume', '--session', 'new')
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)
        self.assertEqual(out['body'], big)
        # H3: a bounded lead copy (<= 3000 encoded bytes) is the price of clip safety
        self.assertLessEqual(len(json.dumps(out['next_step'], ensure_ascii=False).encode()), 3000)
        self.assertTrue(out['next_step'].startswith('xxx'), out['next_step'][:40])
        self.assertIn('continues in the body', out['next_step'])
        self.assertIn('status: resumed', Path(path).read_text())

    def test_next_step_lead_survives_a_4000_byte_clip(self):
        """H3: next_step follows action and its marker lands in the first 4000 encoded
        bytes whether the bulk is a reference, the steps, or escape-heavy text."""
        (self.root / 'ref.md').write_text('reference ' * 1500)
        cases = dict(
            big_ref=('## Objective\nX\n## Current state\nS\n## Next steps\nMARK-NEXT\n', ['ref.md']),
            big_steps=('## Objective\nX\n## Current state\n' + 'y ' * 3000 + '\n## Next steps\nMARK-NEXT '
                       + 'z ' * 2100 + '\n', None),
            escaped=('## Objective\nX\n## Current state\n' + '\u00e9\n' * 2500 + '\n## Next steps\nMARK-NEXT\n'
                     + 'say "\u00fc"\n' * 300, None))
        for name, (body, refs) in cases.items():
            draft = dict(topic=name.replace('_', '-'), description=name, body=body)
            if refs:
                draft['references'] = refs
            code, saved = self.run_cli('save', '.', '--session', 'old-' + name, '--request-id', 'r-' + name,
                                       input=json.dumps(draft))
            self.assertEqual(code, 0, saved)
            p = subprocess.run([sys.executable, str(LEDGER), 'resume', saved['path'], '--session', 'new-' + name,
                                '--fields', 'path,body'], cwd=self.root, env=self.env, capture_output=True,
                               text=True, timeout=30)
            out = json.loads(p.stdout)
            self.assertEqual(out['outcome'], 'resumed', (name, out))
            keys = list(out)
            self.assertEqual(keys.index('next_step'), keys.index('action') + 1, (name, keys))
            lead = p.stdout.encode()[:4000].decode('utf-8', 'ignore')
            self.assertIn('MARK-NEXT', lead, name)
            if name != 'big_ref':  # cut by encoded size, so escaping cannot push the marker past 4000
                self.assertIn('continues in the body', lead)
            self.assertEqual(out['body'], body, name)

    def test_large_dependencies_and_small_budget_are_advisory(self):
        content = 'required material ' * 20000
        refs = []
        for i in range(9):
            name = 'notes-%d.txt' % i
            (self.root / name).write_text(content)
            refs.append(name + '#L1-L1')
        self.skill(Path(self.tmp.name) / '.agents/skills', 'large-skill', content)
        skills = ['large-skill']
        for i in range(16):
            name = 'required-skill-%d' % i
            self.skill(Path(self.tmp.name) / '.agents/skills', name, 'Required instruction.')
            skills.append(name)
        path = self.save(references=refs, skills=skills)
        code, out = self.run_cli('resume', path, '--session', 'new', '--budget-bytes', '1024')
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)
        self.assertEqual(len(out['references']), 9)
        self.assertTrue(all(r['text'] == content for r in out['references']))
        self.assertEqual(out['skills'][0]['text'], content)
        self.assertEqual(len(out['skills']), 17)
        self.assertTrue(out['warnings'])
        self.assertEqual(self.run_cli('lookup')[1]['outcome'], 'none')

    def test_resume_within_budget_before_project_field_is_still_delivered(self):
        # resume() sizes its output without the CLI's added "project" key; the CLI must not then withhold it.
        body = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\n' + 'x' * 15000 + '\n'
        doc = json.dumps(dict(topic='edge', description='Edge', body=body))
        probe = Path(self.tmp.name) / 'projecp'  # same name length as 'project' so sizes match
        (probe / '.handoffs').mkdir(parents=True)
        real = self.root
        self.root = probe
        self.run_cli('save', '.', '--session', 'old', '--request-id', 'r', input=doc)
        _, full = self.run_cli('resume', '--session', 'new', '--budget-bytes', '60000')
        self.root = real
        inner = len(json.dumps({k: v for k, v in full.items() if k != 'project'}, ensure_ascii=False).encode())
        self.run_cli('save', '.', '--session', 'old', '--request-id', 'r', input=doc)
        code, out = self.run_cli('resume', '--session', 'new', '--budget-bytes', str(inner + 10))
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)
        self.assertEqual(self.run_cli('lookup')[1]['outcome'], 'none', 'acknowledged work must have been delivered')

    def test_stale_session_cannot_save_over_a_handoff_another_session_resumed(self):
        path = self.save('work', session='a')
        code, out = self.run_cli('resume', '--session', 'b')
        self.assertEqual((code, out['outcome']), (0, 'resumed'), out)
        doc = dict(topic='work', description='Again', body=BODY)
        code, out = self.run_cli('save', '.', '--session', 'a', '--request-id', 'r-again', input=json.dumps(doc))
        self.assertEqual((code, out['outcome']), (1, 'conflict'), out)
        _, out = self.run_cli('lookup')
        self.assertEqual(out['outcome'], 'none', out)
        self.assertIn('status: resumed', Path(path).read_text())

    def test_resumer_can_continue_the_lineage_and_keeps_the_transfer_record(self):
        path = self.save('work', session='a')
        self.run_cli('resume', '--session', 'b')
        doc = dict(topic='work', description='Next', body=BODY, predecessor=path)
        code, out = self.run_cli('save', '.', '--session', 'b', '--request-id', 'r-next', input=json.dumps(doc))
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        _, out = self.run_cli('history', '--topic', 'work')
        statuses = {Path(i['path']).name: i.get('status') for i in out['items']}
        self.assertEqual(statuses[Path(path).name], 'resumed', out)

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

    def test_next_steps_lead_a_large_resume_so_a_bounded_read_still_has_them(self):
        """M2, fixture aa95d3ec line 47: a 6000-byte clip of a large resume lost Next
        steps at the end of the body. They now precede the body in key order."""
        steps = '1. Run the parser checks.\n2. Reply on the ticket.'
        big = ('## Objective\nFix it.\n## Current state\n' + 'evidence line\n' * 5000
               + '## Next steps\n' + steps + '\n## Gotchas\n- None.\n')
        self.assertGreater(len(big), 65000)
        for fields in ((), ('--fields', 'path,body')):
            topic = 'big%d' % len(fields)
            code, out = self.run_cli('save', '.', '--session', 'old', '--request-id', 'r-' + topic,
                                     input=json.dumps(dict(topic=topic, description='Big', body=big)))
            self.assertEqual(code, 0, out)
            p = subprocess.run([sys.executable, str(LEDGER), 'resume', out['path'], '--session', 'new-' + topic, *fields],
                               cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30)
            out = json.loads(p.stdout)
            self.assertEqual(out['outcome'], 'resumed', out)
            self.assertEqual(out['body'], big)
            keys = list(out)
            self.assertLess(keys.index('next_step_section'), keys.index('body'))
            self.assertLess(keys.index('next_step'), keys.index('body'))
            self.assertIn(json.dumps(steps)[1:-1], p.stdout[:4000])

    def test_fields_trims_optional_output_but_preserves_context_and_diagnostics(self):
        path = self.save()
        _, out = self.run_cli('lookup', '--fields', 'selected,total')
        self.assertEqual(out, dict(outcome='available', action=out['action'], selected=path, total=1, errors=[]))
        _, out = self.run_cli('resume', '--session', 'new', '--fields', 'next_step')
        self.assertEqual(out['outcome'], 'resumed')
        self.assertEqual(out['body'], BODY)
        self.assertEqual(out['next_step'], 'Run the parser checks.')
        self.assertEqual(out['path'], path)
        self.assertNotIn('project', out)
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
