"""Installed ledger calls keep Git worktree checkpoints out of the live tree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


LEDGER = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks/handoff_ledger.py'


class WorktreeRootTests(unittest.TestCase):
    def test_save_from_nested_worktree_creates_its_own_local_ledger(self):
        self.check_worktree_save('inside')

    def test_external_linked_worktree_also_keeps_its_own_ledger(self):
        self.check_worktree_save('outside')

    def check_worktree_save(self, layout):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            home, temp = base / 'home', base / 'tmp'
            home.mkdir(); temp.mkdir()
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
                   'USERPROFILE': str(home), 'TMPDIR': str(temp), 'GIT_CONFIG_NOSYSTEM': '1',
                   'CONTEXT_WATCH_LOG': '0', 'CONTEXT_WATCH_RESUME_LOG': '0',
                   'CONTEXT_WATCH_JEV': '0', 'CONTEXT_WATCH_ORIGIN': 'test', 'HANDOFF_AUTO': '0',
                   'PYTHONDONTWRITEBYTECODE': '1'}
            live = base / 'checkout'
            subprocess.run(['git', 'init', '-q', str(live)], env=env, check=True, capture_output=True)
            subprocess.run(['git', '-C', str(live), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Fixture',
                            '--allow-empty'], env=env, check=True, capture_output=True)
            (live / '.handoffs').mkdir()
            sentinel = live / '.handoffs/live.md'
            sentinel.write_text('---\ntopic: live-sentinel\nstatus: open\n---\n'
                                '## Objective\nPreserve live work.\n## Current state\nUntouched.\n'
                                '## Next steps\nStay in the live checkout.\n')
            before = {p.name: p.read_bytes() for p in (live / '.handoffs').iterdir()}
            worker = live / '.worktrees/worker' if layout == 'inside' else base / 'external-worker'
            subprocess.run(['git', '-C', str(live), 'worktree', 'add', '--detach', str(worker)],
                           env=env, check=True, capture_output=True)
            nested = worker / 'src/deep'
            nested.mkdir(parents=True)
            draft = {'topic': 'worktree-save', 'description': 'Stay in the caller worktree.',
                     'body': '## Objective\nKeep work isolated.\n## Current state\nWorker only.\n## Next steps\nReport the checkpoint path.\n',
                     'references': []}
            saved = subprocess.run([sys.executable, '-B', '-S', str(LEDGER), 'save', '.',
                                    '--session', 'fixture-worker', '--request-id', 'worktree-save'],
                                   cwd=nested, env=env, input=json.dumps(draft), text=True,
                                   capture_output=True, timeout=20)
            self.assertEqual(saved.returncode, 0, saved.stderr + saved.stdout)
            result = json.loads(saved.stdout)
            self.assertEqual(result['outcome'], 'saved')
            self.assertEqual(Path(result['path']).parent.resolve(), worker / '.handoffs')
            self.assertEqual(Path(result['project']), worker)
            # A second lane must not discover either the first lane or live work.
            other = base / 'other-worker'
            subprocess.run(['git', '-C', str(live), 'worktree', 'add', '--detach', str(other)],
                           env=env, check=True, capture_output=True)
            lookup = subprocess.run([sys.executable, '-B', '-S', str(LEDGER), 'lookup'],
                                    cwd=other, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(lookup.returncode, 0, lookup.stdout + lookup.stderr)
            self.assertEqual(json.loads(lookup.stdout)['outcome'], 'none')
            # A recorded home convention within the worktree remains authoritative.
            app = worker / 'app'
            child = app / 'src'
            child.mkdir(parents=True)
            fallback = home / '.claude/handoffs/app'
            fallback.mkdir(parents=True)
            (fallback / 'legacy.md').write_text(
                '---\ntopic: existing-app\nstatus: open\nproject: ' + str(app) + '\n---\n' + draft['body'])
            app_save = subprocess.run([sys.executable, '-B', '-S', str(LEDGER), 'save', '.',
                                       '--session', 'fixture-app', '--request-id', 'app-save'],
                                      cwd=child, env=env, text=True, capture_output=True, timeout=20,
                                      input=json.dumps(dict(draft, topic='app-save')))
            self.assertEqual(app_save.returncode, 0, app_save.stdout + app_save.stderr)
            app_result = json.loads(app_save.stdout)
            self.assertEqual(Path(app_result['path']).parent, fallback)
            self.assertEqual(Path(app_result['project']), app)
            offline = live / '.worktrees/offline'
            subprocess.run(['git', '-C', str(live), 'worktree', 'add', '--detach', str(offline)],
                           env=env, check=True, capture_output=True)
            offline_child = offline / 'pkg'
            offline_child.mkdir()
            no_git = base / 'no-git-bin'
            no_git.mkdir()
            offline_save = subprocess.run([sys.executable, '-B', '-S', str(LEDGER), 'save', '.',
                                           '--session', 'fixture-offline', '--request-id', 'offline-save'],
                                          cwd=offline_child, env=dict(env, PATH=str(no_git)),
                                          input=json.dumps(dict(draft, topic='offline-save')),
                                          text=True, capture_output=True, timeout=20)
            self.assertEqual(offline_save.returncode, 0, offline_save.stdout + offline_save.stderr)
            self.assertEqual(Path(json.loads(offline_save.stdout)['path']).parent, offline / '.handoffs')
            self.assertEqual({p.name: p.read_bytes() for p in (live / '.handoffs').iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
