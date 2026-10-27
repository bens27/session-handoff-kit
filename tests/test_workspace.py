"""Workspace probe seam: one module answers every Git question about a workspace.

Callers are tested against the in-memory adapter. Exactly one test runs real
Git, and it pins the real adapter to the same answers.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_workspace as workspace
from handoff_workspace import MemoryWorkspace, GitWorkspace
import handoff_dispatch as dispatch
import handoff_ledger as ledger
import handoff_pointers as pointers
import handoff_protocol as protocol

SHA = 'a' * 40
OLD, NEW = 'b' * 40, 'c' * 40


class Callers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)

    def test_dispatch_workspace_state_payload(self):
        with workspace.use(MemoryWorkspace(heads={self.root: SHA}, status={self.root: ' M a.py\n'})):
            state = dispatch.workspace_state(self.root)
        self.assertEqual(state, dict(kind='workspace', git=True, head=SHA,
                                     status=hashlib.sha256(b' M a.py\n').hexdigest()[:16]))
        with workspace.use(MemoryWorkspace()):
            self.assertEqual(dispatch.workspace_state(self.root), dict(kind='workspace', git=False))

    def test_changes_hash_reads_listed_files_and_skips_control_dirs(self):
        Path(self.root, 'a.py').write_text('x')
        Path(self.root, 'link').symlink_to('a.py')
        names = ['a.py', 'gone.py', 'link', '.handoffs/c.md', '.agentsroom/s.json', 'dir/']
        with workspace.use(MemoryWorkspace(changed={self.root: names})):
            got = protocol.workspace_changes_hash(self.root)
        digest = hashlib.sha256()
        for raw, data in sorted([(b'a.py', b'x'), (b'gone.py', b'<deleted>'), (b'link', b'a.py')]):
            digest.update(raw + b'\0' + hashlib.sha256(data).digest())
        self.assertEqual(got, digest.hexdigest())
        with workspace.use(MemoryWorkspace()):
            self.assertEqual(protocol.workspace_changes_hash(self.root), '')

    def test_changes_hash_is_unknown_when_too_large(self):
        Path(self.root, 'big').write_bytes(b'x' * 2_000_001)
        with workspace.use(MemoryWorkspace(changed={self.root: ['big']})):
            self.assertEqual(protocol.workspace_changes_hash(self.root), '')

    def progress(self, ancestry):
        base = dict(topic='t', reason='context-pressure', content_hash='h1', continuity='[]')
        previous = dict(base, git='main@' + OLD[:7])
        current = dict(base, git='main@' + NEW[:7], content_hash='h2')
        with workspace.use(MemoryWorkspace(ancestry=ancestry)):
            return protocol.save_progress(self.root, current, [], previous)

    def test_save_progress_counts_forward_commits_only(self):
        self.assertTrue(self.progress({(OLD[:7], NEW[:7])})['new_commits'])
        self.assertFalse(self.progress(set())['new_commits'])

    def test_ledger_position_and_claim_report(self):
        fake = MemoryWorkspace(heads={self.root: SHA}, branches={self.root: 'main'},
                               status={self.root: ' M a\n?? b\n'}, counts={(self.root, 'abc1234'): 3})
        with workspace.use(fake):
            self.assertEqual(ledger._git_position(self.root), 'main@' + SHA[:7])
            path = Path(self.root, 'c.md')
            path.write_text('---\nproject: %s\ngit: main@abc1234\n---\n' % self.root)
            report = ledger.claim_report(str(path))
        self.assertIn('commits_since: 3', report)
        self.assertIn('dirty: 2', report)
        with workspace.use(MemoryWorkspace()):
            self.assertEqual(ledger._git_position(self.root), '')
            self.assertFalse([l for l in ledger.claim_report(str(path)) if l.startswith(('commits', 'dirty'))])

    def test_pointer_tracking_fails_closed(self):
        path = os.path.join(self.root, 'p.json')
        with workspace.use(MemoryWorkspace(tracked={path: True})):
            self.assertTrue(pointers.git_tracked(path))
        with workspace.use(MemoryWorkspace()):
            with self.assertRaises(RuntimeError):
                pointers.git_tracked(path)

    def test_hold_roots_follow_the_common_dir(self):
        worker = os.path.join(self.root, 'worker')
        os.makedirs(worker)
        # Identity reads the marker itself (no Git): a gitdir under worktrees/ is a linked worktree.
        Path(worker, '.git').write_text('gitdir: ../main/.git/worktrees/w\n')
        main_git = os.path.join(self.root, 'main', '.git')
        cases = {
            'linked': ((os.path.join(main_git, 'worktrees/w'), main_git), [os.path.join(self.root, 'main'), worker]),
            'separate': ((main_git, None), [worker]),
            'same': ((main_git, main_git), [worker]),
        }
        for name, (answer, expected) in cases.items():
            with self.subTest(name), workspace.use(MemoryWorkspace(common={worker: answer})):
                self.assertEqual(dispatch._hold_roots(worker), expected)
        with workspace.use(MemoryWorkspace(common={worker: 'Invalid common Git directory; shared holds cannot be checked'})):
            with self.assertRaisesRegex(OSError, 'Invalid common Git directory'):
                dispatch._hold_roots(worker)


class RealAdapterFailures(unittest.TestCase):
    """Real adapter error handling, with the Git process stubbed."""

    def test_tracked_fails_closed_on_timeout_and_unknown_stderr_in_c_locale(self):
        from unittest.mock import patch
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 5)):
            with self.assertRaisesRegex(RuntimeError, 'git-check-indeterminate'):
                GitWorkspace().tracked('/x/p.json')
        dubious = subprocess.CompletedProcess([], 128, b'', b'fatal: detected dubious ownership')
        with patch.object(subprocess, 'run', return_value=dubious) as run:
            with self.assertRaisesRegex(RuntimeError, 'git-check-indeterminate'):
                GitWorkspace().tracked('/x/p.json')
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_missing_git_is_unknown_everywhere(self):
        from unittest.mock import patch
        real = GitWorkspace()
        with patch.object(subprocess, 'run', side_effect=FileNotFoundError('git')):
            self.assertIsNone(real.snapshot('/r'))
            self.assertIsNone(real.changed_files('/r'))
            self.assertFalse(real.is_ancestor('/r', 'a', 'b'))
            self.assertEqual(real.position('/r'), '')
            self.assertEqual(real.movement('/r', 'abc'), (None, None))
            self.assertEqual(real.resolve_commit('/r', 'abcdef1'), '')


class RealGitContract(unittest.TestCase):
    """The one real-Git test: the real adapter gives the answers the memory adapter models."""

    def test_real_adapter_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
            def git(*args):
                return subprocess.run(['git', '-C', root] + list(args), env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()
            real = GitWorkspace()
            outside_dir = tempfile.TemporaryDirectory()
            self.addCleanup(outside_dir.cleanup)
            outside = os.path.realpath(outside_dir.name)
            git('init', '-q', '-b', 'main')
            Path(root, 'a.py').write_text('1')
            git('add', 'a.py')
            git('commit', '-q', '-m', 'one')
            old = git('rev-parse', 'HEAD')
            Path(root, 'a.py').write_text('2')
            git('commit', '-qam', 'two')
            new = git('rev-parse', 'HEAD')
            Path(root, 'a.py').write_text('3')
            Path(root, 'n.py').write_text('n')
            Path(root, '.handoffs').mkdir()
            Path(root, '.handoffs/c.md').write_text('c')

            head, porcelain = real.snapshot(root)
            self.assertEqual(head, new)
            self.assertEqual(porcelain, ' M a.py\n?? n.py\n')  # .handoffs excluded
            self.assertEqual(sorted(real.changed_files(root).split(b'\0')), [b'', b'.handoffs/c.md', b'a.py', b'n.py'])
            self.assertTrue(real.is_ancestor(root, old[:7], new[:7]))
            self.assertFalse(real.is_ancestor(root, new[:7], old[:7]))
            self.assertFalse(real.is_ancestor(root, 'f' * 7, new[:7]))
            self.assertEqual(real.position(root), 'main@' + new[:7])
            self.assertEqual(real.resolve_commit(root, old[:7]), old)
            self.assertEqual(real.resolve_commit(root, 'f' * 40), '')
            tree = git('rev-parse', 'HEAD^{tree}')
            self.assertEqual(real.resolve_commit(root, tree), '')  # an object, but not a commit
            memory = MemoryWorkspace(commits={root: [old, new]})
            self.assertEqual(memory.resolve_commit(root, old[:7]), old)
            self.assertEqual(memory.resolve_commit(root, ''), '')  # ambiguous: matches both
            self.assertEqual(memory.resolve_commit(outside, old[:7]), '')
            self.assertEqual(real.movement(root, old[:7]), ('1', 3))
            self.assertTrue(real.tracked(os.path.join(root, 'a.py')))
            self.assertFalse(real.tracked(os.path.join(root, 'n.py')))
            with self.assertRaisesRegex(RuntimeError, r'^git -C \S+ rev-parse nope failed: fatal: '):
                real.run(['-C', root, 'rev-parse', 'nope'])

            worker = os.path.join(root, 'worker')
            git('worktree', 'add', '-q', worker)
            gitdir, common = real.linked_common(worker)
            self.assertEqual(common, os.path.realpath(os.path.join(root, '.git')))
            self.assertEqual(os.path.dirname(gitdir), os.path.join(common, 'worktrees'))
            commondir = Path(gitdir, 'commondir')
            original = commondir.read_text()
            for content, text in (('\n', 'Invalid common Git directory; shared holds cannot be checked'),
                                  ('../../missing\n', 'Common Git directory unavailable; shared holds cannot be checked')):
                with self.subTest(commondir=content):
                    commondir.write_text(content)
                    with self.assertRaises(OSError) as raised:
                        real.linked_common(worker)
                    self.assertEqual(str(raised.exception), text)
            commondir.write_text(original)

            # Unknown answers keep each caller's failure contract.
            self.assertIsNone(real.snapshot(outside))
            self.assertIsNone(real.changed_files(outside))
            self.assertEqual(real.position(outside), '')
            self.assertEqual(real.resolve_commit(outside, old), '')
            self.assertFalse(real.tracked(os.path.join(outside, 'x')))


if __name__ == '__main__':
    unittest.main()
