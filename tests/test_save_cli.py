"""Script-deduced save: the CLI infers project, lineage, verify and transition."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / 'skills/session-handoff/hooks/handoff_ledger.py'
BODY = '## Objective\nFix it.\n## Current state\nUnchanged.\n## Next steps\nRun the parser checks.\n'
CLEARED = ('CODEX_THREAD_ID', 'AGENTSROOM_AGENT_ID', 'HANDOFF_TERMINAL_ID', 'HANDOFF_SESSION_ID', 'HANDOFF_AUTO', 'HANDOFF_RUN_ID',
           'TMUX_PANE', 'AUTORESUME', 'CONTEXT_WATCH_AUTORESUME')


class SaveCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / 'tmp').mkdir()
        self.root = Path(self.tmp.name) / 'project'
        (self.root / '.handoffs').mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        self.env.update(HOME=self.tmp.name, TMPDIR=str(Path(self.tmp.name) / "tmp"), CONTEXT_WATCH_ORIGIN='test',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', TYPESAFE_API_KEY='')

    def run_cli(self, *args, input=None, cwd=None, **env):
        p = subprocess.run([sys.executable, str(LEDGER), *map(str, args)], cwd=cwd or self.root,
                           env={**self.env, **env}, input=input, capture_output=True, text=True, timeout=30)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def save(self, *args, session='s1', cwd=None, env=None, **doc):
        doc = dict(dict(topic='work', description='About work', body=BODY), **doc)
        return self.run_cli('save', '--session', session, *args, input=json.dumps(doc), cwd=cwd, **(env or {}))

    def frontmatter(self, path):
        head = Path(path).read_text().split('\n---\n', 1)[0]
        return dict(line.split(': ', 1) for line in head.splitlines()[1:] if ': ' in line)

    def test_explicit_stdin_matches_file_save(self):
        draft = dict(topic='work', description='About work', body=BODY)
        path = self.root / 'draft.json'
        path.write_text(json.dumps(draft))
        code, saved = self.run_cli('save', '--session', 's1', '--input', '-',
                                   input=json.dumps(draft))
        self.assertEqual((code, saved['outcome']), (0, 'saved'), saved)
        code, retried = self.run_cli('save', '--session', 's1', '--input', path)
        self.assertEqual((code, retried['outcome']), (0, 'saved'), retried)
        self.assertEqual(retried['path'], saved['path'])
        self.assertEqual(self.frontmatter(saved['path'])['topic'], 'work')
        self.assertTrue(Path(saved['path']).read_text().endswith(BODY))

    def run_cli_bytes(self, *args, input, **env):
        result = subprocess.run([sys.executable, str(LEDGER), *map(str, args)],
                                cwd=self.root, env={**self.env, **env}, input=input,
                                capture_output=True, timeout=30)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_stdin_utf8_ignores_process_encoding(self):
        draft = dict(topic='work', description='Café naïve', body=BODY)
        raw = json.dumps(draft, ensure_ascii=False).encode('utf-8')
        code, saved = self.run_cli_bytes('save', '--session', 's1', '--input', '-',
                                         input=raw, PYTHONIOENCODING='latin-1')
        self.assertEqual((code, saved['outcome']), (0, 'saved'), saved)
        self.assertEqual(self.frontmatter(saved['path'])['description'], 'Café naïve')
        path = self.root / 'draft.json'
        path.write_bytes(raw)
        code, retried = self.run_cli('save', '--session', 's1', '--input', path)
        self.assertEqual((code, retried['outcome']), (0, 'saved'), retried)
        self.assertEqual(retried['path'], saved['path'])

    def test_stdin_non_utf8_bytes_are_invalid(self):
        raw = json.dumps(dict(topic='work', description='Café naïve', body=BODY),
                         ensure_ascii=False).encode('latin-1')
        code, out = self.run_cli_bytes('save', '--session', 's1', '--input', '-',
                                       input=raw, PYTHONIOENCODING='latin-1')
        self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
        self.assertIn('utf-8', out['action'])
        self.assertIn('correct the input and retry', out['action'])
        self.assertEqual(list((self.root / '.handoffs').glob('*.md')), [])

    def test_explicit_stdin_invalid_json_is_recoverable(self):
        for raw in ('', '{broken', '[]'):
            with self.subTest(raw=raw):
                code, out = self.run_cli('save', '--session', 's1', '--input', '-', input=raw)
                self.assertEqual((code, out['outcome']), (1, 'invalid'))
                self.assertIn('correct the input and retry', out['action'])
                self.assertNotIn("No such file or directory", out['action'])
        self.assertEqual(list((self.root / '.handoffs').glob('*.md')), [])

    def test_explicit_stdin_read_failure_is_recoverable(self):
        result = subprocess.run(
            [sys.executable, '-c',
             "import runpy, sys; sys.stdin.close(); sys.argv = sys.argv[1:]; "
             "runpy.run_path(sys.argv[0], run_name='__main__')",
             str(LEDGER), 'save', '--session', 's1', '--input', '-'],
            cwd=self.root, env={**self.env, 'PYTHONPATH': str(LEDGER.parent)},
            capture_output=True, text=True, timeout=30)
        out = json.loads(result.stdout)
        self.assertEqual((result.returncode, out['outcome']), (1, 'invalid'))
        self.assertIn('closed file', out['action'])
        self.assertIn('correct the input and retry', out['action'])
        self.assertEqual(list((self.root / '.handoffs').glob('*.md')), [])

    # 12: project comes from the predecessor, not an unrelated cwd
    def test_save_follows_predecessor_project_from_other_cwd(self):
        _, first = self.save()
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        code, out = self.save(cwd=elsewhere, predecessor=first['path'], body=BODY + 'More.\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(out['project'], os.path.realpath(self.root))
        self.assertEqual(self.frontmatter(out['path'])['predecessor'], os.path.realpath(first['path']))

    def test_explicit_root_disagreeing_with_predecessor_names_both(self):
        _, first = self.save()
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        code, out = self.save(str(other), predecessor=first['path'], body=BODY + 'More.\n')
        self.assertEqual((code, out['outcome']), (1, 'conflict'))
        self.assertIn(os.path.realpath(self.root), out['action'])
        self.assertIn(os.path.realpath(other), out['action'])

    # 13: own latest open handoff on the topic is the default predecessor
    def test_same_session_resave_defaults_predecessor(self):
        _, first = self.save()
        code, out = self.save(body=BODY + 'Later.\n')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.frontmatter(out['path'])['predecessor'], os.path.realpath(first['path']))

    def test_other_session_still_needs_explicit_predecessor(self):
        self.save()
        code, out = self.save(session='s2', body=BODY + 'Later.\n')
        self.assertEqual((code, out['outcome']), (1, 'conflict'))

    # 14: draft from flags and a Markdown file
    def test_save_from_flags_and_body_file(self):
        body = self.root / 'body.md'
        body.write_text(BODY)
        code, out = self.run_cli('save', '--session', 's1', '--topic', 'flagged', '--description', 'From flags',
                                 '--body', body)
        self.assertEqual(code, 0, out)
        fm = self.frontmatter(out['path'])
        self.assertEqual((fm['topic'], fm['description']), ('flagged', 'From flags'))

    # 15: attachments become required references
    def test_attach_adds_reference(self):
        notes = self.root / 'notes.txt'
        notes.write_text('evidence\n')
        code, out = self.save('--attach', notes)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.frontmatter(out['path'])['references'], str(notes.resolve()))

    def test_attach_keeps_line_range(self):
        notes = self.root / 'notes.txt'
        notes.write_text('a\nb\nc\n')
        code, out = self.save('--attach', '%s#L2-L3' % notes)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.frontmatter(out['path'])['references'], '%s#L2-L3' % notes.resolve())

    def test_attach_missing_file_is_invalid(self):
        code, out = self.save('--attach', self.root / 'missing.txt')
        self.assertEqual((code, out['outcome']), (1, 'invalid'))

    # 16: verify defaults to user config (never the repo's), then predecessor
    def test_verify_defaults_from_user_config_then_predecessor(self):
        config = Path(self.tmp.name) / '.context-watch'
        config.mkdir(exist_ok=True)
        (config / 'thresholds.json').write_text(json.dumps({'verify': 'make check'}))
        _, first = self.save()
        self.assertEqual(self.frontmatter(first['path'])['verify'], 'make check')
        (config / 'thresholds.json').unlink()
        _, second = self.save(body=BODY + 'Later.\n')
        self.assertEqual(self.frontmatter(second['path'])['verify'], 'make check')

    # 17: saved output names the transition
    def test_transition_reflects_environment(self):
        cases = [({}, 'new-session'), ({'AUTORESUME': '1'}, 'clear'),
                 ({'HANDOFF_AUTO': '1', 'TMUX_PANE': '%1'}, 'tmux'),
                 ({'HANDOFF_AUTO': '1', 'AGENTSROOM_AGENT_ID': 'a1'}, 'agents_restart'),
                 # AgentsRoom defaults to automatic handoff; only an explicit off disables it.
                 ({'AGENTSROOM_AGENT_ID': 'a1'}, 'agents_restart'),
                 ({'HANDOFF_AUTO': '0', 'AGENTSROOM_AGENT_ID': 'a1'}, 'new-session')]
        for i, (env, expected) in enumerate(cases):
            _, out = self.save(topic='t%d' % i, env=env)
            self.assertEqual(out['transition']['kind'], expected, env)
            self.assertTrue(out['transition']['action'])
        # With auto off, the action says so instead of silently handing the restart to the user.
        self.assertIn('Automatic handoff is off', out['transition']['action'])
        self.assertIn('"auto": true', out['transition']['action'])

    def test_agentsroom_user_config_can_turn_auto_off(self):
        config = Path(self.tmp.name) / '.context-watch'
        config.mkdir(exist_ok=True)
        (config / 'thresholds.json').write_text(json.dumps({'auto': False}))
        _, out = self.save(env={'AGENTSROOM_AGENT_ID': 'a1'})
        self.assertEqual(out['transition']['kind'], 'new-session')

    def test_agentsroom_restart_uses_prompt_safe_for_native_cli(self):
        code, out = self.save(env={'AGENTSROOM_AGENT_ID': 'a1'})
        self.assertEqual(code, 0, out)
        self.assertEqual(out['transition']['kind'], 'agents_restart')
        self.assertIn('restart_preflight.py restart --root ', out['transition']['action'])
        self.assertNotIn('reference.md', out['transition']['action'])
        self.assertIn('restart_prompt', out['transition']['action'])
        self.assertNotIn('with prompt "continue the handoff"', out['transition']['action'])
        self.assertNotIn('with prompt "resume"', out['transition']['action'])

    def test_large_checkpoint_is_saved_without_truncation(self):
        body = BODY + 'essential context ' * 10000
        code, out = self.save(body=body)
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertTrue(Path(out['path']).read_text().endswith(body))
        code, resumed = self.run_cli('resume', out['path'], '--session', 'next')
        self.assertEqual((code, resumed['outcome']), (0, 'resumed'), resumed)
        self.assertEqual(resumed['body'], body)


if __name__ == '__main__':
    unittest.main()
