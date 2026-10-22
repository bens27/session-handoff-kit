#!/usr/bin/env python3
"""handoff-workspace: every question the kit asks Git about a workspace.

One seam, two adapters. GitWorkspace runs `git -C root ...` and owns all Git
error handling; MemoryWorkspace answers from plain dicts so tests describe a
workspace instead of building a repository. Callers use the module functions,
which delegate to the active adapter (`use()` swaps it for a block).

Each question keeps the failure contract its callers already had:
  snapshot(root)        (head, porcelain) or None when status is unknown
  changed_files(root)   raw `ls-files -z` bytes, or None
  is_ancestor(root,a,b) bool; unknown is False
  position(root)        'branch@shortsha', or ''
  movement(root, sha)   (commits_since or None, dirty_count or None)
  tracked(path)         bool; RuntimeError('git-check-indeterminate') if unknown
  linked_common(wt)     (gitdir, common or None); OSError when metadata is bad
  run(args, check)      strict command in the cwd (lane); RuntimeError on failure

Stdlib only.
"""

from contextlib import contextmanager
import os
import subprocess


class GitWorkspace:
    """The real adapter."""

    def _git(self, root, args, timeout, text=True, env=None):
        return subprocess.run(['git', '-C', root] + list(args), capture_output=True,
                              text=text, timeout=timeout, env=env)

    def snapshot(self, root):
        try:
            head = self._git(root, ['rev-parse', '-q', '--verify', 'HEAD'], 10)
            status = self._git(root, ['status', '--porcelain=v1', '-uall', '--', '.', ':(exclude).handoffs'], 10)
        except (OSError, subprocess.SubprocessError):
            return None
        if status.returncode:
            return None
        return head.stdout.strip(), status.stdout

    def changed_files(self, root):
        try:
            listed = self._git(root, ['ls-files', '--modified', '--deleted', '--others',
                                      '--exclude-standard', '-z'], 5, text=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return None if listed.returncode else listed.stdout

    def is_ancestor(self, root, old, new):
        try:
            return self._git(root, ['merge-base', '--is-ancestor', old, new], 5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def position(self, root):
        try:
            sha = self._git(root, ['rev-parse', '--short', 'HEAD'], 5).stdout.strip()
            if not sha:
                return ''
            return '%s@%s' % (self._git(root, ['rev-parse', '--abbrev-ref', 'HEAD'], 5).stdout.strip(), sha)
        except Exception:
            return ''

    def movement(self, root, sha):
        commits = dirty = None
        try:
            commits = self._git(root, ['rev-list', '--count', '%s..HEAD' % sha], 5).stdout.strip()
            dirty = len(self._git(root, ['status', '--short'], 5).stdout.splitlines())
        except Exception:
            pass
        return commits, dirty

    def tracked(self, path):
        directory = os.path.dirname(os.path.realpath(path))
        try:
            result = self._git(directory, ['ls-files', '--error-unmatch', '--', os.path.realpath(path)], 5,
                               text=False, env=dict(os.environ, LC_ALL='C', LANG='C'))
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('git-check-indeterminate') from exc
        if result.returncode == 0:
            return True
        err = result.stderr.decode('utf-8', 'replace').lower()
        if 'not a git repository' in err or 'did not match' in err:
            return False
        raise RuntimeError('git-check-indeterminate')

    def linked_common(self, worktree):
        try:
            with open(os.path.join(worktree, '.git'), encoding='utf-8') as stream:
                pointer = stream.read(8192).strip()
        except ValueError as exc:
            raise OSError('Invalid Git worktree metadata; shared holds cannot be checked') from exc
        if not pointer.startswith('gitdir: ') or not pointer[8:].strip():
            raise OSError('Invalid Git worktree metadata; shared holds cannot be checked')
        gitdir = os.path.realpath(os.path.join(worktree, pointer[8:].strip()))
        if not os.path.isdir(gitdir):
            raise OSError('Git worktree metadata unavailable; shared holds cannot be checked')
        try:
            with open(os.path.join(gitdir, 'commondir'), encoding='utf-8') as stream:
                relative = stream.read(8192).strip()
        except FileNotFoundError:
            # A submodule/separate-git-dir marker has no linked-worktree control plane.
            if os.path.isfile(os.path.join(gitdir, 'gitdir')) or os.path.basename(os.path.dirname(gitdir)) == 'worktrees':
                raise OSError('Linked-worktree common metadata missing; shared holds cannot be checked')
            return gitdir, None
        if not relative:
            raise OSError('Invalid common Git directory; shared holds cannot be checked')
        common = os.path.realpath(os.path.join(gitdir, relative))
        if not os.path.isdir(common):
            raise OSError('Common Git directory unavailable; shared holds cannot be checked')
        return gitdir, common

    def run(self, args, check=True):
        p = subprocess.run(['git'] + list(args), capture_output=True, text=True)
        if check and p.returncode:
            raise RuntimeError('git %s failed: %s' % (' '.join(args), p.stderr.strip()))
        return p.stdout.rstrip('\n') if check else p


class MemoryWorkspace:
    """The in-memory adapter: describe a workspace with dicts. A root missing
    from a dict answers like a directory Git cannot read."""

    def __init__(self, heads=None, branches=None, status=None, changed=None, ancestry=(),
                 counts=None, tracked=None, common=None):
        self.heads = dict(heads or {})        # root -> full sha
        self.branches = dict(branches or {})  # root -> branch name
        self.status = dict(status or {})      # root -> porcelain text
        self.changed = dict(changed or {})    # root -> [relative names]
        self.ancestry = set(ancestry)         # {(old, new)}
        self.counts = dict(counts or {})      # (root, sha) -> commits since
        self.tracked_paths = dict(tracked or {})  # realpath -> bool
        self.common = dict(common or {})      # worktree -> (gitdir, common) or OSError message

    def snapshot(self, root):
        if root not in self.status:
            return None
        return self.heads.get(root, ''), self.status[root]

    def changed_files(self, root):
        if root not in self.changed:
            return None
        return b''.join(os.fsencode(name) + b'\0' for name in self.changed[root])

    def is_ancestor(self, root, old, new):
        return (old, new) in self.ancestry

    def position(self, root):
        sha = self.heads.get(root, '')[:7]
        return '%s@%s' % (self.branches.get(root, 'HEAD'), sha) if sha else ''

    def movement(self, root, sha):
        if root not in self.status:
            return None, None
        return str(self.counts.get((root, sha), 0)), len(self.status[root].splitlines())

    def tracked(self, path):
        key = os.path.realpath(path)
        if key not in self.tracked_paths:
            raise RuntimeError('git-check-indeterminate')
        return self.tracked_paths[key]

    def linked_common(self, worktree):
        value = self.common.get(worktree)
        if isinstance(value, str):
            raise OSError(value)
        if value is None:
            raise OSError('Git worktree metadata unavailable; shared holds cannot be checked')
        return value

    def run(self, args, check=True):
        raise RuntimeError('git %s failed: in-memory workspace runs no commands' % ' '.join(args))


_active = GitWorkspace()


def adapter():
    return _active


@contextmanager
def use(workspace):
    """Answer every probe from `workspace` inside the block."""
    global _active
    previous, _active = _active, workspace
    try:
        yield workspace
    finally:
        _active = previous


def snapshot(root):
    return _active.snapshot(root)


def changed_files(root):
    return _active.changed_files(root)


def is_ancestor(root, old, new):
    return _active.is_ancestor(root, old, new)


def position(root):
    return _active.position(root)


def movement(root, sha):
    return _active.movement(root, sha)


def tracked(path):
    return _active.tracked(path)


def linked_common(worktree):
    return _active.linked_common(worktree)


def run(args, check=True):
    return _active.run(args, check)
