"""v3-2 project pointer index (SPEC 7.3d): provenance, ordering and dispatch marker."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_pointers as pointers
import handoff_workspace as workspace
from handoff_workspace import MemoryWorkspace
import handoff_protocol as protocol
import restart_preflight as restart


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = str(Path(self.tmp.name).resolve())
        (Path(self.root) / '.handoffs').mkdir()
        self.env = patch.dict(os.environ, {
            'HOME': self.root, 'USERPROFILE': self.root, 'HANDOFF_TERMINAL_ID': 'term',
            'AGENTSROOM_AGENT_ID': '', 'AUTORESUME': '', 'CONTEXT_WATCH_AUTORESUME': '',
            'HANDOFF_AUTO': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.saved = protocol.save(self.root, 'old', 'save-1', {
            'topic': 'work', 'description': 'Continue work',
            'body': '## Objective\nFix it\n## Current state\nSaved\n## Next steps\nTest it\n'})
        self.path = self.saved['path']

    def preflight(self, **kw):
        args = dict(root=self.root, path=self.path, session='old', terminal='term',
                    request_id='restart-1', workers=[], host_capable=True)
        args.update(kw)
        return restart.preflight(**args)

    def authorize(self):
        user = Path(self.root) / '.context-watch'
        user.mkdir(exist_ok=True)
        (user / 'thresholds.json').write_text(json.dumps({'autoresume': True}))

    def pointer_file(self, ready):
        return Path(pointers.pointer_path(self.root, ready['binding_sha256']))

    def intent_file(self):
        return Path(restart._location(self.root, 'restart-1'))

    # -- ordering and schema -------------------------------------------------
    def test_preflight_writes_private_pointer_then_intent_then_marker(self):
        ready = self.preflight()
        self.assertTrue(ready['dispatch'], ready)
        record = restart.status(self.root, 'restart-1')
        self.assertIsInstance(record.get('dispatch_started_at'), float)
        location = self.pointer_file(ready)
        self.assertEqual(location.stat().st_mode & 0o777, 0o600)
        data = json.loads(location.read_text())
        self.assertEqual(data['version'], 1)
        for key in ('checkpoint_id', 'request_id', 'root', 'session', 'terminal', 'binding_sha256'):
            self.assertEqual(data[key], record[key])
        self.assertEqual(data['checkpoint_path'], record['path'])
        self.assertEqual(data['handle'], 'h-' + record['binding_sha256'][:24])
        self.assertEqual(data['workspace'], self.root)
        self.assertNotIn('Fix it', location.read_text())

    def test_pointer_write_failure_leaves_no_pending_intent(self):
        with patch.object(pointers, 'write_pointer', side_effect=OSError('disk full')):
            result = self.preflight()
        self.assertEqual((result['outcome'], result['stage'], result['dispatch']),
                         ('blocked', 'pointer', False), result)
        self.assertIsNone(restart.status(self.root, 'restart-1'))
        self.assertTrue(Path(self.path).exists())

    def test_pointer_without_intent_is_inert_and_identical_retry_finishes(self):
        real = protocol.write_json
        def crash(path, value):
            if path == str(self.intent_file()):
                raise OSError('crash before intent')
            return real(path, value)
        with patch.object(protocol, 'write_json', side_effect=crash):
            with self.assertRaises(OSError):
                self.preflight()
        self.authorize()
        scan = pointers.scan(self.root)
        self.assertEqual(scan['candidates'], [])
        self.assertEqual([d['reason'] for d in scan['diagnostics']], ['incomplete-index'])
        ready = self.preflight()
        self.assertTrue(ready['dispatch'], ready)
        self.assertEqual(len(pointers.scan(self.root)['candidates']), 1)

    def test_missing_marker_permits_exactly_one_reservation(self):
        ready = self.preflight()
        record = restart.status(self.root, 'restart-1')
        del record['dispatch_started_at']
        protocol.write_json(str(self.intent_file()), record)  # crash before the marker
        again = self.preflight()
        self.assertTrue(again['dispatch'], again)
        self.assertEqual(again['restart_prompt'], ready['restart_prompt'])
        third = self.preflight()
        self.assertEqual((third['dispatch'], third['reason']), (False, 'dispatch-reserved'), third)

    def test_marker_retry_rechecks_holds(self):
        self.preflight()
        record = restart.status(self.root, 'restart-1')
        del record['dispatch_started_at']
        protocol.write_json(str(self.intent_file()), record)
        held = self.preflight(hold=True)
        self.assertEqual((held['dispatch'], held['reason']), (False, 'human-hold'))
        self.assertNotIn('dispatch_started_at', restart.status(self.root, 'restart-1'))

    def test_marker_present_never_redispatches(self):
        self.preflight()
        again = self.preflight()
        self.assertEqual((again['outcome'], again['dispatch'], again['reason']),
                         ('pending', False, 'dispatch-reserved'), again)

    def test_legacy_intent_without_pointer_keeps_reconcile_behavior(self):
        ready = self.preflight()
        record = restart.status(self.root, 'restart-1')
        del record['dispatch_started_at']
        protocol.write_json(str(self.intent_file()), record)
        self.pointer_file(ready).unlink()
        again = self.preflight()
        self.assertFalse(again['dispatch'], again)

    def test_conflicting_pointer_refuses_overwrite(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        data = json.loads(location.read_text())
        self.intent_file().unlink()
        protocol.write_json(str(location), dict(data, workspace='/elsewhere'))
        result = self.preflight()
        self.assertEqual((result['outcome'], result['stage'], result['reason']),
                         ('blocked', 'pointer', 'pointer-conflict'), result)
        self.assertIsNone(restart.status(self.root, 'restart-1'))

    # -- eligibility ---------------------------------------------------------
    def test_pending_trusted_pointer_with_user_authorization_is_candidate(self):
        ready = self.preflight()
        self.authorize()
        scan = pointers.scan(self.root)
        self.assertEqual([c['request_id'] for c in scan['candidates']], ['restart-1'], scan)
        self.assertEqual(scan['candidates'][0]['pointer'], str(self.pointer_file(ready)))

    def test_project_config_cannot_authorize_continuation(self):
        self.preflight()
        (Path(self.root) / '.context-watch.json').write_text(json.dumps({'autoresume': True, 'auto': True}))
        scan = pointers.scan(self.root)
        self.assertEqual(scan['candidates'], [])
        self.assertEqual(scan['diagnostics'][0]['reason'], 'continuation-not-authorized')

    def test_missing_or_rotated_receipt_key_is_ineligible(self):
        self.preflight()
        self.authorize()
        key = Path(self.root) / '.context-watch/receipt.key'
        key.write_bytes(os.urandom(32))  # rotated
        scan = pointers.scan(self.root)
        self.assertEqual(scan['diagnostics'][0]['reason'], 'publication-untrusted')
        self.assertIn('adopt', scan['diagnostics'][0]['action'])
        key.unlink()
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'publication-untrusted')

    def test_foreign_home_cannot_resolve_handle(self):
        self.preflight()
        other = tempfile.mkdtemp(dir=self.root)
        Path(other, '.context-watch').mkdir()
        Path(other, '.context-watch/thresholds.json').write_text(json.dumps({'autoresume': True}))
        with patch.dict(os.environ, {'HOME': other, 'USERPROFILE': other}):
            scan = pointers.scan(self.root)
        self.assertEqual(scan['candidates'], [])
        self.assertEqual(scan['diagnostics'][0]['reason'], 'locator-unresolved')

    def test_planted_pointer_without_locator_or_intent(self):
        self.authorize()
        directory = Path(pointers.pointer_dir(self.root))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        digest = 'a' * 64
        protocol.write_json(str(directory / (digest + '.json')), dict(
            version=1, checkpoint_id='b' * 64, checkpoint_path=self.path, root=self.root,
            workspace=self.root, session='old', terminal='term', request_id='forged',
            binding_sha256=digest, handle='h-' + digest[:24], created_at=time.time()))
        scan = pointers.scan(self.root)
        self.assertEqual(scan['candidates'], [])
        self.assertEqual(scan['diagnostics'][0]['reason'], 'incomplete-index')

    def test_git_tracked_pointer_intent_or_checkpoint_is_refused(self):
        ready = self.preflight()
        self.authorize()
        files = (self.pointer_file(ready), self.intent_file(), Path(self.path))
        untracked = {os.path.realpath(str(f)): False for f in files}
        with workspace.use(MemoryWorkspace(tracked=untracked)):
            self.assertEqual(len(pointers.scan(self.root)['candidates']), 1)
        for tracked in files:
            state = dict(untracked, **{os.path.realpath(str(tracked)): True})
            with workspace.use(MemoryWorkspace(tracked=state)):
                scan = pointers.scan(self.root)
            self.assertEqual(scan['candidates'], [], tracked)
            self.assertEqual(scan['diagnostics'][0]['reason'], 'git-tracked')

    def test_indeterminate_git_check_fails_closed(self):
        self.preflight()
        self.authorize()
        with workspace.use(MemoryWorkspace()):  # Git cannot decide
            scan = pointers.scan(self.root)
        self.assertEqual(scan['diagnostics'][0]['reason'], 'git-check-indeterminate')

    def test_symlink_and_fifo_pointers_are_not_followed(self):
        ready = self.preflight()
        self.authorize()
        location = self.pointer_file(ready)
        real = location.with_name('real.data')
        location.rename(real)
        location.symlink_to(real)
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'invalid-pointer')
        location.unlink()
        os.mkfifo(location)
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'invalid-pointer')

    def test_retrieved_replays_only_to_recorded_successor_and_settled_is_silent(self):
        self.preflight()
        self.authorize()
        receipt = protocol.prepare(self.path, self.root, 'new')['delivery_receipt']
        restart.confirm_successor(self.root, 'restart-1', 'new', 'term', receipt)
        self.assertEqual(pointers.scan(self.root, session='other'),
                         {'candidates': [], 'diagnostics': []})
        replay = pointers.scan(self.root, session='new')['candidates']
        self.assertEqual([(c['request_id'], c['mode']) for c in replay], [('restart-1', 'replay')])
        record = restart.status(self.root, 'restart-1')
        protocol.write_json(str(self.intent_file()), dict(record, state='refused', refusal='host'))
        self.assertEqual(pointers.scan(self.root, session='new'),
                         {'candidates': [], 'diagnostics': []})

    def test_expired_pending_is_ineligible(self):
        self.preflight()
        self.authorize()
        record = restart.status(self.root, 'restart-1')
        protocol.write_json(str(self.intent_file()), dict(record, intent_at=time.time() - 8 * 86400))
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'intent-expired')

    # -- review round 1 (Lane E B1/B2, N1/N2/N4) ------------------------------
    def test_planted_pointer_cannot_grant_reentry_to_legacy_intent(self):
        ready = self.preflight()
        record = restart.status(self.root, 'restart-1')
        for key in ('dispatch_started_at', 'pointer_version'):
            record.pop(key, None)
        protocol.write_json(str(self.intent_file()), record)  # legacy shape
        self.assertTrue(self.pointer_file(ready).exists())
        again = self.preflight()
        self.assertFalse(again['dispatch'], again)

    def test_new_intents_record_pointer_version(self):
        self.preflight()
        self.assertEqual(restart.status(self.root, 'restart-1')['pointer_version'], 1)

    def rewrite_pointer(self, ready, **fields):
        location = self.pointer_file(ready)
        data = json.loads(location.read_text())
        location.unlink()
        protocol.write_json(str(location), dict(data, **fields))
        location.chmod(0o600)

    def test_pointer_task_and_workspace_must_match_sealed_checkpoint(self):
        ready = self.preflight()
        self.authorize()
        self.rewrite_pointer(ready, task_id='attacker-task')
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'binding-mismatch')
        data = json.loads(self.pointer_file(ready).read_text())
        del data['task_id']
        self.pointer_file(ready).unlink()
        protocol.write_json(str(self.pointer_file(ready)), dict(data, workspace='/nonexistent/ws'))
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'binding-mismatch')

    def test_missing_workspace_is_ineligible(self):
        ready = self.preflight()
        self.authorize()
        with patch.object(pointers, '_workspace_exists', return_value=False):
            self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'workspace-missing')

    def test_world_readable_pointer_is_invalid(self):
        ready = self.preflight()
        self.authorize()
        self.pointer_file(ready).chmod(0o644)
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'invalid-pointer')

    def test_environment_authorizes_continuation(self):
        self.preflight()
        for name in ('AUTORESUME', 'CONTEXT_WATCH_AUTORESUME'):
            with self.subTest(name=name), patch.dict(os.environ, {name: '1'}):
                self.assertEqual(len(pointers.scan(self.root)['candidates']), 1)

    def test_unrecognised_git_stderr_fails_closed(self):
        self.preflight()
        self.authorize()
        # Stderr classification is the real adapter's (tests/test_workspace.py); here: any undecided answer fails closed.
        with workspace.use(MemoryWorkspace()):
            self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'git-check-indeterminate')

    def test_symlinked_pointer_directory_is_refused(self):
        ready = self.preflight()
        self.authorize()
        directory = Path(pointers.pointer_dir(self.root))
        moved = directory.with_name('elsewhere')
        directory.rename(moved)
        directory.symlink_to(moved)
        self.assertEqual(pointers.scan(self.root)['diagnostics'][0]['reason'], 'unsafe-pointer-dir')
        self.intent_file().unlink()
        result = self.preflight()
        self.assertEqual((result['stage'], result['reason']), ('pointer', 'pointer-write-failed'), result)

    def swap_after_directory_check(self, directory, replacement):
        """Inject a rename at the directory-validation/filesystem-use boundary."""
        checked = pointers._safe_dir
        moved = directory.with_name('pinned-pointers')
        swapped = False

        def check_then_swap(path_or_fd, private):
            nonlocal swapped
            checked(path_or_fd, private)
            if private and not swapped:
                swapped = True
                directory.rename(moved)
                directory.symlink_to(replacement, target_is_directory=True)

        return moved, patch.object(pointers, '_safe_dir', side_effect=check_then_swap)

    def test_scan_does_not_follow_directory_swap_after_validation(self):
        ready = self.preflight()
        self.authorize()
        directory = self.pointer_file(ready).parent
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        planted = replacement / self.pointer_file(ready).name
        planted.write_text('{}')
        planted.chmod(0o600)
        _, race = self.swap_after_directory_check(directory, replacement)
        with race:
            result = pointers.scan(self.root)
        self.assertEqual([c['request_id'] for c in result['candidates']], ['restart-1'], result)
        self.assertEqual(result['diagnostics'], [])

    def test_write_does_not_follow_directory_swap_after_validation(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        data = json.loads(location.read_text())
        location.unlink()
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        moved, race = self.swap_after_directory_check(location.parent, replacement)
        with race:
            pointers.write_pointer(self.root, data)
        self.assertEqual(list(replacement.iterdir()), [])
        self.assertEqual(json.loads((moved / location.name).read_text()), data)

    def test_prune_does_not_unlink_from_replacement_directory(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        planted = replacement / location.name
        planted.write_bytes(location.read_bytes())
        planted.chmod(0o600)
        moved, race = self.swap_after_directory_check(location.parent, replacement)
        with race:
            pointers.prune(self.root, {'restart-1'})
        self.assertTrue(planted.exists())
        self.assertFalse((moved / location.name).exists())

    def test_direct_read_pins_checked_directory(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        expected = json.loads(location.read_text())
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        _, race = self.swap_after_directory_check(location.parent, replacement)
        with race:
            self.assertEqual(pointers.read_pointer(str(location)), expected)

    def test_handoffs_swap_does_not_redirect_pointer_directory_open(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        expected = json.loads(location.read_text())
        handoffs = location.parent.parent
        moved = handoffs.with_name('pinned-handoffs')
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        checked = pointers._safe_dir
        swapped = False

        def check_then_swap(fd, private):
            nonlocal swapped
            checked(fd, private)
            if not private and not swapped:
                swapped = True
                handoffs.rename(moved)
                handoffs.symlink_to(replacement, target_is_directory=True)

        with patch.object(pointers, '_safe_dir', side_effect=check_then_swap):
            self.assertEqual(pointers.read_pointer(str(location)), expected)

    def test_symlink_lock_is_refused_without_touching_target(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        data = json.loads(location.read_text())
        lock = location.with_name(location.name + '.lock')
        lock.unlink()
        target = Path(self.root) / 'victim'
        target.write_text('untouched')
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            pointers.write_pointer(self.root, data)
        self.assertEqual(target.read_text(), 'untouched')

    def test_failed_replace_cleans_temporary_file_in_pinned_directory(self):
        ready = self.preflight()
        location = self.pointer_file(ready)
        data = json.loads(location.read_text())
        location.unlink()
        replacement = Path(self.root) / 'attacker'
        replacement.mkdir(mode=0o700)
        moved = location.parent.with_name('pinned-pointers')

        def fail_replace(*args, **kwargs):
            location.parent.rename(moved)
            location.parent.symlink_to(replacement, target_is_directory=True)
            raise OSError('replace failed')

        with patch.object(os, 'replace', side_effect=fail_replace):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                pointers.write_pointer(self.root, data)
        self.assertEqual(list(moved.glob('.state-*')), [])
        self.assertFalse((moved / location.name).exists())
        self.assertEqual(list(replacement.iterdir()), [])

    def test_group_writable_handoffs_under_umask_002_still_dispatches(self):
        import shutil
        shutil.rmtree(Path(self.root) / '.handoffs')
        old = os.umask(0o002)
        try:
            for present in (False, True):
                with self.subTest(handoffs_present=present), \
                        patch.dict(os.environ, {'HANDOFF_TERMINAL_ID': 'term-u%d' % present}):
                    if present:
                        (Path(self.root) / '.handoffs').chmod(0o775)
                    request = 'restart-umask-%d' % present
                    saved = protocol.save(self.root, 'old', 'save-u%d' % present, {
                        'topic': 'umask%d' % present, 'description': 'Continue work',
                        'body': '## Objective\nFix it\n## Current state\nSaved\n## Next steps\nTest it\n'})
                    result = self.preflight(path=saved['path'], request_id=request,
                                            terminal='term-u%d' % present)
                    self.assertTrue(result['dispatch'], result)
        finally:
            os.umask(old)

    def test_other_writable_handoffs_is_refused(self):
        (Path(self.root) / '.handoffs').chmod(0o777)
        result = self.preflight()
        self.assertEqual((result['stage'], result['reason']), ('pointer', 'pointer-write-failed'), result)


if __name__ == '__main__':
    unittest.main()
