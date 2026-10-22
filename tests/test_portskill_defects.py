"""ce8a995f: four automatic-handoff defects from the Portskill field report."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))
import handoff_protocol as protocol
import restart_preflight as restart

BODY = '## Objective\nFinish work.\n## Current state\nReady.\n## Next steps\nRun checks.\n'


class PortskillDefectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        # The subprocesses' temp base is a sibling of the project, so the project is not under it.
        self.tmpdir = self.base / 'tmp'
        self.tmpdir.mkdir()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.scratch = self.tmpdir / 'scratch'
        self.scratch.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(('CODEX_THREAD_ID', 'HANDOFF_', 'CONTEXT_WATCH_', 'AUTORESUME', 'AGENTSROOM_'))}
        self.env.update(HOME=str(self.base), TMPDIR=str(self.tmpdir), AGENTSROOM_AGENT_ID='terminal-a',
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_JEV='0', CONTEXT_WATCH_THINKING='0')

    def hook(self, tokens, session='s1', event='PostToolUse', cwd=None):
        trace = self.root / (session + '.jsonl')
        trace.write_text('\n'.join(json.dumps({'message': {'model': 'claude-fable-5-1',
                         'usage': {'input_tokens': n}}}) for n in (31000, tokens)))
        p = subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')],
                           input=json.dumps(dict(hook_event_name=event, session_id=session,
                                                 cwd=str(cwd or self.root), transcript_path=str(trace))),
                           cwd=cwd or self.root, env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def save_cli(self, doc, cwd, session='s1', *extra):
        p = subprocess.run([sys.executable, str(HOOKS / 'handoff_ledger.py'), 'save', '--session', session, *extra],
                           input=json.dumps(doc), cwd=cwd, env=self.env, capture_output=True, text=True, timeout=20)
        return p.returncode, json.loads(p.stdout)

    # 1. topic contract
    def test_contract_text_states_the_kebab_case_rule(self):
        self.assertIn('kebab-case', self.hook(140000))

    def test_spaced_topic_is_slugified_on_save(self):
        code, out = self.save_cli(dict(topic='AgentsRoom Skills Sync!', description='d', body=BODY), self.root)
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertIn('-agentsroom-skills-sync-', Path(out['path']).name)
        self.assertTrue(any('my topic' in w.lower() or 'slug' in w.lower() for w in out.get('warnings', [])), out)

    def test_topic_with_no_usable_characters_is_still_refused(self):
        code, out = self.save_cli(dict(topic='!!!', description='d', body=BODY), self.root)
        self.assertEqual(out['outcome'], 'invalid')
        self.assertIn('kebab-case', out['action'])

    # 2. root from recorded session metadata, never from a temp cwd
    def test_save_from_scratch_cwd_files_under_the_recorded_project(self):
        self.hook(140000)  # hook runs in the project and records it for the session
        code, out = self.save_cli(dict(topic='work', description='d', body=BODY), self.scratch)
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertEqual(os.path.realpath(out['project']), str(self.root))
        self.assertFalse(list(self.scratch.rglob('*.md')), 'nothing may be filed under the scratch cwd')
        self.assertTrue(any(str(self.root) in w for w in out.get('warnings', [])), out)

    def assert_refused(self, cwd, session='unknown'):
        code, out = self.save_cli(dict(topic='work', description='d', body=BODY), cwd, session)
        self.assertEqual((code, out['outcome']), (1, 'invalid'), out)
        self.assertIn('save <root>', out['action'])
        self.assertFalse(list(self.tmpdir.rglob('*.md')), 'nothing may be filed under the temp base')

    def test_save_from_a_temp_base_without_record_is_refused(self):
        self.assert_refused(self.tmpdir)

    def test_leftover_handoffs_in_a_temp_base_does_not_lift_the_refusal(self):
        # One stray save (or another local user) can create <tmp>/.handoffs; that is no consent.
        (self.tmpdir / '.handoffs').mkdir()
        self.assert_refused(self.tmpdir)

    def test_explicit_root_in_a_temp_base_is_honoured(self):
        code, out = self.save_cli(dict(topic='work', description='d', body=BODY), self.tmpdir, 'unknown', '.')
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertEqual(os.path.realpath(out['project']), str(self.tmpdir))

    def test_scratch_dir_inside_a_temp_base_without_record_is_refused(self):
        self.assert_refused(self.scratch)

    def plant_pointer(self, session, project, mode=0o600, link=False):
        with patch.object(tempfile, 'tempdir', str(self.tmpdir)):
            pointer = Path(protocol.ledger.project_pointer_path(session))
        target = self.base / 'planted.json' if link else pointer
        target.write_text(json.dumps(dict(session_id=session, project=str(project), ts=time.time())))
        os.chmod(target, mode)
        if link:
            os.symlink(target, pointer)

    def test_symlinked_pointer_is_ignored(self):
        self.plant_pointer('sP', self.root, link=True)
        self.assert_refused(self.scratch, 'sP')

    def test_group_or_other_accessible_pointer_is_ignored(self):
        self.plant_pointer('sP', self.root, mode=0o644)
        self.assert_refused(self.scratch, 'sP')

    def test_pointer_naming_a_project_under_a_temp_base_is_ignored(self):
        elsewhere = self.tmpdir / 'elsewhere'
        elsewhere.mkdir()
        self.plant_pointer('sP', elsewhere)
        self.assert_refused(self.scratch, 'sP')

    def test_private_pointer_naming_a_user_project_redirects(self):
        self.plant_pointer('sP', self.root)
        code, out = self.save_cli(dict(topic='work', description='d', body=BODY), self.scratch, 'sP')
        self.assertEqual((code, out['outcome']), (0, 'saved'), out)
        self.assertEqual(os.path.realpath(out['project']), str(self.root))

    # 3. worker inventory
    def preflight_with(self, workers):
        root = str(self.root)
        (self.root / '.handoffs').mkdir(exist_ok=True)
        with patch.dict(os.environ, {'HOME': str(self.base), 'HANDOFF_TERMINAL_ID': 'term', 'AGENTSROOM_AGENT_ID': ''}):
            saved = protocol.save(root, 'old', 'save-w', dict(topic='work', description='d', body=BODY))
            return restart.preflight(root=root, path=saved['path'], session='old', terminal='term',
                                     request_id='restart-w', workers=workers, host_capable=True)

    def test_explicit_empty_inventory_is_dispatchable(self):
        out = self.preflight_with([])
        self.assertEqual((out['outcome'], out['dispatch']), ('ready', True), out)

    def test_settled_worker_without_report_is_dispatchable(self):
        out = self.preflight_with([dict(id='mailbox-brief', settled=True, report=None)])
        self.assertEqual((out['outcome'], out['dispatch']), ('ready', True), out)

    def test_settled_worker_with_malformed_report_still_blocks(self):
        out = self.preflight_with([dict(id='mailbox-brief', settled=True, report=[])])
        self.assertEqual((out['outcome'], out.get('reason')), ('blocked', 'worker-report-not-durable'), out)

    def test_unsettled_worker_without_report_still_blocks(self):
        out = self.preflight_with([dict(id='mailbox-brief')])
        self.assertEqual(out['outcome'], 'blocked', out)

    def test_in_flight_mailbox_delegation_is_dispatchable(self):
        # Its reply is persisted in the AgentsRoom inbox and reaches the restarted agent.
        out = self.preflight_with([dict(id='lane-brief', state='in_flight', via='mailbox')])
        self.assertEqual((out['outcome'], out['dispatch']), ('ready', True), out)
        intent = restart.status(str(self.root), 'restart-w')
        self.assertEqual(intent['workers'], [dict(id='lane-brief', state='in_flight', via='mailbox')])

    def test_in_flight_worker_without_durable_channel_blocks(self):
        # A native subagent's report reaches only the spawning session; a restart would lose it.
        out = self.preflight_with([dict(id='subagent', state='in_flight')])
        self.assertEqual((out['outcome'], out['reason']), ('blocked', 'worker-in-flight'), out)

    def test_omitted_inventory_blocks_and_transition_text_says_so(self):
        self.assertEqual(self.preflight_with(None)['reason'], 'worker-inventory-unknown')
        with patch.dict(os.environ, {'AGENTSROOM_AGENT_ID': 'terminal-a', 'HANDOFF_AUTO': '1'}):
            action = protocol._transition(str(self.root))['action']
        self.assertNotIn('Omit --workers', action)
        self.assertIn("--workers '[]'", action)
        self.assertIn('worker-inventory-unknown', action)
        self.assertIn('"state": "in_flight", "via": "mailbox"', action)

    # 4. one hooks root per message
    def test_transition_names_the_installed_hooks_path_not_the_symlink_target(self):
        installed = self.base / '.agents/skills'
        installed.mkdir(parents=True)
        os.symlink(HOOKS.parent, installed / 'session-handoff')
        with patch.dict(os.environ, {'HOME': str(self.base), 'AGENTSROOM_AGENT_ID': 'terminal-a', 'HANDOFF_AUTO': '1'}):
            action = protocol._transition(str(self.root))['action']
        self.assertIn(str(installed / 'session-handoff/hooks/restart_preflight.py'), action)
        self.assertNotIn(str(HOOKS / 'restart_preflight.py'), action)


if __name__ == '__main__':
    unittest.main()
