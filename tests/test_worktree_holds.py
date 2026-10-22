"""Main-checkout holds must gate a linked worker's real lifecycle CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
BODY = '## Objective\nRespect shared holds.\n## Current state\nPRIVATE-HOLD-MARKER\n## Next steps\nReport only.\n'


class WorktreeHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        home, tmp = base / 'home', base / 'tmp'
        home.mkdir(); tmp.mkdir()
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
                    'USERPROFILE': str(home), 'TMPDIR': str(tmp), 'GIT_CONFIG_NOSYSTEM': '1',
                    'HANDOFF_TERMINAL_ID': 'fixture-worker', 'CODEX_THREAD_ID': 'fixture-old',
                    'HANDOFF_AUTO': '0', 'CONTEXT_WATCH_LOG': '0', 'CONTEXT_WATCH_RESUME_LOG': '0',
                    'CONTEXT_WATCH_JEV': '0', 'CONTEXT_WATCH_ORIGIN': 'test'}
        self.main = base / 'main'
        subprocess.run(['git', 'init', '-q', str(self.main)], env=self.env, check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.main), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'Fixture'],
                       env=self.env, check=True, capture_output=True)
        (self.main / '.handoffs').mkdir()
        self.worker = self.main / '.worktrees/worker'
        subprocess.run(['git', '-C', str(self.main), 'worktree', 'add', '--detach', str(self.worker)],
                       env=self.env, check=True, capture_output=True)
        _, self.saved = self.cli('handoff_ledger.py', 'save', '.', '--session', 'fixture-old',
                                 '--request-id', 'worker-save',
                                 input=json.dumps({'topic': 'held-work', 'description': 'Worker fixture', 'body': BODY}))
        self.assertEqual(self.saved['outcome'], 'saved')

    def cli(self, script, *args, input=None, session='fixture-old', environment=None):
        result = subprocess.run([sys.executable, '-B', '-S', str(HOOKS / script), *map(str, args)],
                                cwd=self.worker, env=dict(self.env, CODEX_THREAD_ID=session, **(environment or {})),
                                input=input, text=True, capture_output=True, timeout=30)
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        self.assertNotIn('Traceback (most recent call last)', result.stderr, result.stderr)
        return result.stdout, json.loads(result.stdout)

    def hold(self, root, reason='Chief requires a pause', scope='project'):
        return self.cli('handoff_ledger.py', 'hold', '--root', root, '--scope', scope, '--reason', reason)[1]

    def test_restart_cli_reports_hook_drift_as_warning_or_block(self):
        config = self.worker / '.claude/settings.local.json'
        config.parent.mkdir()
        config.write_text('{"hooks": {"Stop": []}}')
        (self.worker / '.context-watch.json').write_text('{"hook_drift": "block"}')
        _, blocked = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                              '--workers', '[]', '--host-capable')
        self.assertEqual((blocked['outcome'], blocked['reason']), ('blocked', 'hooks-changed'), blocked)
        self.assertEqual([c['path'] for c in blocked['changed']], [str(config)])
        self.assertIn('.claude/settings.local.json', blocked['detail'])
        self.assertNotIn('approval', blocked['detail'])
        (self.worker / '.context-watch.json').unlink()
        _, ready = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                            '--workers', '[]', '--host-capable')
        self.assertEqual(ready['outcome'], 'ready', ready)
        [warning] = ready['warnings']
        self.assertEqual(warning['reason'], 'hooks-changed')
        self.assertEqual([c['path'] for c in warning['changed']], [str(config)])

    def test_main_project_hold_blocks_worker_successor_adoption(self):
        self.assert_adoption_held('project')

    def test_main_terminal_hold_blocks_worker_successor_adoption(self):
        self.assert_adoption_held('terminal:fixture-worker')

    def assert_adoption_held(self, scope):
        _, ready = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                            '--workers', '[]', '--host-capable')
        self.assertEqual(ready['outcome'], 'ready', ready)
        held = self.hold(self.main, scope=scope)
        self.assertEqual(held['outcome'], 'held')
        raw, adopted = self.cli('restart_preflight.py', 'adopt', '--root', self.worker,
                                '--request-id', ready['request_id'], session='fixture-new')
        self.assertEqual((adopted['outcome'], adopted.get('stage')), ('held', 'hold'), adopted)
        self.assertNotIn('PRIVATE-HOLD-MARKER', raw)
        self.assertNotIn('resumed_by:', Path(self.saved['path']).read_text())

    def test_worker_local_release_does_not_remove_main_restart_hold(self):
        main = self.hold(self.main)
        before = Path(main['path']).read_bytes()
        local = self.hold(self.worker, reason='Additional worker pause')
        self.assertEqual(Path(local['path']).parent, self.worker / '.handoffs/holds')
        _, released = self.cli('handoff_ledger.py', 'release', '--scope', 'project', '--root', self.worker)
        self.assertEqual(released['outcome'], 'released')
        self.assertEqual(Path(main['path']).read_bytes(), before)
        _, restart = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                              '--workers', '[]', '--host-capable')
        self.assertFalse(restart['dispatch'], restart)
        self.assertEqual(restart['reason'], 'human-hold', restart)

    def test_identical_main_and_legacy_worker_hold_records_are_deduplicated(self):
        main = self.hold(self.main)
        local = self.worker / '.handoffs/holds' / Path(main['path']).name
        local.parent.mkdir()
        local.write_bytes(Path(main['path']).read_bytes())
        _, retrieved = self.cli('handoff_ledger.py', 'dispatch', '--root', self.worker, '--input', '-',
                                 input=json.dumps({'operation': 'retrieve', 'target': self.saved['path']}))
        self.assertEqual(retrieved['outcome'], 'proceed', retrieved)
        self.assertEqual(len(retrieved['holds']), 1, retrieved)
        self.assertEqual(retrieved['holds'][0]['root'], str(self.main))

    def test_main_hold_blocks_restart_even_without_git_on_path(self):
        self.hold(self.main)
        no_git = Path(self.temp.name) / 'no-git-bin'
        no_git.mkdir()
        _, restart = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                              '--workers', '[]', '--host-capable', environment={'PATH': str(no_git)})
        self.assertFalse(restart['dispatch'], restart)
        self.assertEqual(restart['reason'], 'human-hold', restart)

    def test_missing_shared_metadata_blocks_adoption_instead_of_losing_holds(self):
        _, ready = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                            '--workers', '[]', '--host-capable')
        self.assertEqual(ready['outcome'], 'ready', ready)
        self.hold(self.main)
        pointer = (self.worker / '.git').read_text().strip().removeprefix('gitdir: ')
        common = Path(pointer) / 'commondir'
        preserved = common.with_suffix('.preserved')
        common.rename(preserved)
        try:
            raw, adopted = self.cli('restart_preflight.py', 'adopt', '--root', self.worker,
                                    '--request-id', ready['request_id'], session='fixture-new')
            self.assertEqual((adopted['outcome'], adopted.get('stage')), ('blocked', 'hold'), adopted)
            self.assertNotIn('PRIVATE-HOLD-MARKER', raw)
        finally:
            preserved.rename(common)

    def test_superproject_hold_applies_inside_submodule(self):
        source = Path(self.temp.name) / 'submodule-source'
        subprocess.run(['git', 'clone', '-q', str(self.main), str(source)], env=self.env,
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.main), '-c', 'protocol.file.allow=always',
                        'submodule', 'add', '-q', str(source), 'mods/sub'], env=self.env,
                       check=True, capture_output=True)
        self.worker = self.main
        _, saved = self.cli('handoff_ledger.py', 'save', '.', '--session', 'fixture-old',
                            '--request-id', 'main-save', input=json.dumps({
                                'topic': 'held-main', 'description': 'Main fixture', 'body': BODY}))
        self.assertEqual(saved['outcome'], 'saved', saved)
        self.hold(self.main)
        self.worker = self.main / 'mods/sub'
        raw, result = self.cli('handoff_ledger.py', 'dispatch', '--input', '-',
                              input=json.dumps({'operation': 'resume', 'target': saved['path'],
                                                'authorization': {'source': 'user'}}))
        self.assertEqual(result['holds'][0]['root'], str(self.main), result)
        self.assertEqual((result['outcome'], result.get('code')), ('blocked', 'held'), result)
        self.assertNotIn('PRIVATE-HOLD-MARKER', raw)

    def test_missing_shared_metadata_blocks_restart_without_traceback(self):
        pointer = (self.worker / '.git').read_text().strip().removeprefix('gitdir: ')
        common = Path(pointer) / 'commondir'
        preserved = common.with_suffix('.preserved')
        common.rename(preserved)
        try:
            _, result = self.cli('restart_preflight.py', 'restart', '--root', self.worker,
                                 '--workers', '[]', '--host-capable')
            self.assertEqual((result['outcome'], result.get('stage')), ('blocked', 'hold'), result)
        finally:
            preserved.rename(common)

    def test_non_utf8_git_marker_preserves_save_and_blocks_retrieval_safely(self):
        (self.worker / '.git').write_bytes(b'gitdir: \xff\xfe/x\n')
        _, saved = self.cli('handoff_ledger.py', 'save', '.', '--session', 'fixture-old',
                            '--request-id', 'malformed-marker-save', input=json.dumps({
                                'topic': 'safe-save', 'description': 'Keep save local', 'body': BODY}))
        self.assertEqual(saved['outcome'], 'saved', saved)
        self.assertEqual(Path(saved['path']).parent, self.worker / '.handoffs')
        raw, result = self.cli('handoff_ledger.py', 'dispatch', '--input', '-',
                              input=json.dumps({'operation': 'retrieve', 'target': saved['path']}))
        self.assertEqual((result['outcome'], result.get('code')),
                         ('blocked', 'hold_state_unreadable'), result)
        self.assertNotIn('PRIVATE-HOLD-MARKER', raw)
