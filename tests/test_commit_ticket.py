"""Ticket attribution through the commit-msg CLI and project installer."""
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / 'skills/session-handoff/hooks/commit_ticket.py'
INSTALL = REPO / 'skills/session-handoff/install.py'
TICKET = '222a49df-730b-437d-99e3-05c3acda0a29'


def run_hook(tmp_path, message, ticket=TICKET):
    path = tmp_path / 'COMMIT_EDITMSG'
    path.write_text(message)
    env = dict(os.environ)
    env.pop('HANDOFF_TICKET_ID', None)
    if ticket is not None:
        env['HANDOFF_TICKET_ID'] = ticket
    result = subprocess.run([sys.executable, str(HOOK), str(path)], env=env,
                            capture_output=True, text=True, timeout=5)
    return result, path.read_text()


def test_adds_explicit_ticket(tmp_path):
    result, message = run_hook(tmp_path, 'Fix resume selection\n')
    assert result.returncode == 0, result.stderr
    assert message == f'Fix resume selection\n\nTicket: {TICKET}\n'


def test_existing_ticket_wins_and_repeated_hook_is_idempotent(tmp_path):
    original = 'Fix resume\n\nSigned-off-by: Ben <ben@example.test>\nticket: OTHER-95\n'
    result, message = run_hook(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert message == original


def test_rejects_malformed_existing_trailer_without_mutating_message(tmp_path):
    original = 'Fix resume\n\nTicket: bad ticket\n'
    result, message = run_hook(tmp_path, original, ticket=None)
    assert result.returncode == 1
    assert 'invalid Ticket trailer' in result.stderr
    assert message == original


def test_invalid_or_missing_environment_id_never_blocks_or_changes_message(tmp_path):
    original = 'Fix resume\n'
    for ticket in (None, '', 'bad ticket', 'a\nTicket: injected', 'é', 'x' * 129):
        result, message = run_hook(tmp_path, original, ticket=ticket)
        assert (result.returncode, message) == (0, original), result.stderr


def test_only_final_trailers_count_and_new_ticket_precedes_git_comments(tmp_path):
    original = ('Fix resume\n\nTicket: this is body text\n\nMore explanation.\n\n'
                'Signed-off-by: Ben <ben@example.test>\n\n# Comment\n'
                '# ------------------------ >8 ------------------------\n'
                'Ticket: diff content\n')
    result, message = run_hook(tmp_path, original)
    assert result.returncode == 0, result.stderr
    assert message == original.replace('\n\n# Comment', f'\nTicket: {TICKET}\n\n# Comment')
    result, repeated = run_hook(tmp_path, message)
    assert result.returncode == 0 and repeated == message


def git(project, *args, env=None):
    return subprocess.run(['git', '-C', str(project), *args], env=env,
                          capture_output=True, text=True, timeout=10)


def install(project, *args):
    return subprocess.run([sys.executable, str(INSTALL), 'git-hook', '--project',
                           str(project), *args], capture_output=True, text=True, timeout=10)


def test_opt_in_installer_runs_on_real_commits_and_uninstalls(tmp_path):
    project = tmp_path / 'project with spaces'
    project.mkdir()
    assert git(project, 'init', '-q').returncode == 0
    hook = project / '.git/hooks/commit-msg'
    unrelated = project / '.git/hooks/pre-commit'
    unrelated.write_text('#!/bin/sh\nexit 0\n')
    before = (project / '.git/config').read_bytes()
    result = install(project)
    assert result.returncode == 0, result.stderr
    first = hook.read_bytes()
    assert install(project).returncode == 0
    assert hook.read_bytes() == first
    env = dict(os.environ, HANDOFF_TICKET_ID=TICKET,
               GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.test',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.test')
    result = git(project, 'commit', '--allow-empty', '-m', 'Test attribution', env=env)
    assert result.returncode == 0, result.stderr
    assert git(project, 'log', '-1', '--format=%B').stdout == f'Test attribution\n\nTicket: {TICKET}\n\n'
    result = git(project, 'commit', '--allow-empty', '-m', 'Bad\n\nTicket: bad ticket', env=env)
    assert result.returncode != 0 and 'invalid Ticket trailer' in result.stderr
    assert install(project, '--uninstall').returncode == 0
    assert not hook.exists()
    assert install(project, '--uninstall').returncode == 0
    assert (project / '.git/config').read_bytes() == before
    assert unrelated.read_text() == '#!/bin/sh\nexit 0\n'


def test_foreign_trailer_continuation_does_not_extend_ticket_id(tmp_path):
    original = 'Fix\n\nTicket: OTHER-95\nReviewed-by: A\n B\n'
    result, message = run_hook(tmp_path, original)
    assert result.returncode == 0 and message == original, result.stderr


def test_comment_character_follows_project_git_configuration(tmp_path):
    assert git(tmp_path, 'init', '-q').returncode == 0
    assert git(tmp_path, 'config', 'core.commentChar', ';').returncode == 0
    message = tmp_path / 'COMMIT_EDITMSG'
    message.write_text('Fix\n\n; Git comment\n')
    result = subprocess.run([sys.executable, str(HOOK), str(message)], cwd=tmp_path,
                            env=dict(os.environ, HANDOFF_TICKET_ID=TICKET),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert message.read_text() == f'Fix\n\nTicket: {TICKET}\n\n; Git comment\n'


def test_hook_rejects_ambiguous_ids_and_preserves_unattributed_messages(tmp_path):
    for block in ('Ticket: A\nTicket: B\n', 'Ticket:\n', 'Ticket: A\n continuation\n',
                  'Ticket: é\n', 'Ticket: ' + 'x' * 129 + '\n'):
        original = 'Fix\n\n' + block
        result, message = run_hook(tmp_path, original, ticket=None)
        assert result.returncode == 1 and message == original
    for original in ('Fix\n\nA body.\n', '\n# Only comments\n', '',
                     'Ticket: this is the subject\n'):
        result, message = run_hook(tmp_path, original, ticket=None)
        assert result.returncode == 0 and message == original
    result, message = run_hook(tmp_path, '# Only comments\n')
    assert result.returncode == 0 and message == '# Only comments\n'


def test_install_refuses_foreign_hooks_and_custom_hook_directories(tmp_path):
    assert git(tmp_path, 'init', '-q').returncode == 0
    hook = tmp_path / '.git/hooks/commit-msg'
    foreign = b'#!/bin/sh\nexit 42\n'
    hook.write_bytes(foreign)
    for args in ((), ('--uninstall',)):
        result = install(tmp_path, *args)
        assert result.returncode != 0 and 'existing commit-msg hook left intact' in result.stderr
        assert hook.read_bytes() == foreign
    hook.unlink()
    shared = tmp_path / 'shared'
    assert git(tmp_path, 'config', 'core.hooksPath', str(shared)).returncode == 0
    result = install(tmp_path)
    assert result.returncode != 0 and 'core.hooksPath is configured' in result.stderr
    assert not shared.exists() and not hook.exists()


def test_hook_io_failure_is_nonblocking(tmp_path):
    result = subprocess.run([sys.executable, str(HOOK), str(tmp_path / 'missing')],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and 'skipped' in result.stderr


def test_message_survives_a_failed_write(tmp_path):
    import resource
    import signal
    message = tmp_path / 'COMMIT_EDITMSG'
    original = 'Fix resume selection without losing the original message\n'
    message.write_text(original)

    def small_file_limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (16, 16))
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)

    result = subprocess.run([sys.executable, str(HOOK), str(message)],
                            env=dict(os.environ, HANDOFF_TICKET_ID=TICKET),
                            preexec_fn=small_file_limit, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and 'skipped' in result.stderr
    assert message.read_text() == original
    assert list(tmp_path.iterdir()) == [message]


def test_auto_comment_character_leaves_ambiguous_message_intact(tmp_path):
    assert git(tmp_path, 'init', '-q').returncode == 0
    assert git(tmp_path, 'config', 'core.commentChar', 'auto').returncode == 0
    path = tmp_path / 'COMMIT_EDITMSG'
    original = '# Real subject\n\nA body with a hash heading.\n\n; Git comment\n'
    path.write_text(original)
    result = subprocess.run([sys.executable, str(HOOK), str(path)], cwd=tmp_path,
                            env=dict(os.environ, HANDOFF_TICKET_ID=TICKET),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == original
    path.write_text('Plain message\n')
    result = subprocess.run([sys.executable, str(HOOK), str(path)], cwd=tmp_path,
                            env=dict(os.environ, HANDOFF_TICKET_ID=TICKET),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert path.read_text() == f'Plain message\n\nTicket: {TICKET}\n'


def test_install_marker_allows_relocation_after_previous_release_is_removed(tmp_path):
    import shutil
    project = tmp_path / 'project'
    project.mkdir()
    assert git(project, 'init', '-q').returncode == 0
    first = tmp_path / 'release-one'
    second = tmp_path / 'release-two'
    for release in (first, second):
        release.mkdir()
        shutil.copy2(INSTALL, release / 'install.py')
        (release / 'hooks').mkdir()
        for name in ('commit_ticket.py', 'handoff_contract.py'):
            shutil.copy2(HOOK.parent / name, release / 'hooks' / name)
    def from_release(release, *args):
        return subprocess.run([sys.executable, str(release / 'install.py'), 'git-hook',
                               '--project', str(project), *args],
                              capture_output=True, text=True, timeout=10)
    assert from_release(first).returncode == 0
    hook = project / '.git/hooks/commit-msg'
    assert str(first).encode() in hook.read_bytes()
    shutil.rmtree(first)
    result = from_release(second)
    assert result.returncode == 0, result.stderr
    assert str(second).encode() in hook.read_bytes()
    message = project / 'MESSAGE'
    message.write_text('Fix relocated hook\n')
    result = subprocess.run([str(hook), str(message)], cwd=project,
                            env=dict(os.environ, HANDOFF_TICKET_ID=TICKET),
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert message.read_text() == f'Fix relocated hook\n\nTicket: {TICKET}\n'
    assert from_release(second, '--uninstall').returncode == 0
    assert not hook.exists()
    # Uninstall must also recognize the first path without needing a reinstall.
    first.mkdir()
    shutil.copy2(INSTALL, first / 'install.py')
    assert from_release(first).returncode == 0
    result = from_release(second, '--uninstall')
    assert result.returncode == 0 and not hook.exists(), result.stderr


def test_install_refuses_symlinked_parent_git_directory(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    assert git(project, 'init', '-q').returncode == 0
    actual = tmp_path / 'actual-git'
    (project / '.git').rename(actual)
    (project / '.git').symlink_to(actual, target_is_directory=True)
    hook = actual / 'hooks/commit-msg'
    for args in ((), ('--uninstall',)):
        result = install(project, *args)
        assert result.returncode != 0, result.stdout
        assert 'symlink' in result.stderr
        assert not hook.exists()
    # A linked worktree's .git is a regular indirection file; it remains supported.
    (project / '.git').unlink()
    actual.rename(project / '.git')
    linked = tmp_path / 'linked'
    assert git(project, 'worktree', 'add', '--orphan', '-b', 'linked', str(linked)).returncode == 0
    assert (linked / '.git').is_file()
    result = install(linked)
    assert result.returncode == 0, result.stderr
    assert (project / '.git/hooks/commit-msg').exists()


def test_environment_uses_shared_ticket_format_but_existing_trailers_remain_broad(tmp_path):
    original = 'Fix shared attribution\n'
    for invalid in ('PROJ-123', 'abc1234', 'abc123456', TICKET.upper(), 'a' * 128):
        result, message = run_hook(tmp_path, original, ticket=invalid)
        assert (result.returncode, message) == (0, original), result.stderr
    for valid in ('39661343', '39661343-1d16-4c04-bf21-33c24edff2e0'):
        result, message = run_hook(tmp_path, original, ticket=valid)
        assert result.returncode == 0, result.stderr
        assert message == f'Fix shared attribution\n\nTicket: {valid}\n'
    existing = 'Fix\n\nTicket: PROJ-123\n'
    result, message = run_hook(tmp_path, existing, ticket='invalid env')
    assert result.returncode == 0 and message == existing, result.stderr
