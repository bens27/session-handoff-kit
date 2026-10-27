"""Mandatory gate at the installed hook stdin/stdout boundary, isolated HOME."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_runtime_activation as runtime


class MandatoryGate(unittest.TestCase):
    cli = runtime.RuntimeActivation.cli

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        shutil.copytree(runtime.SKILL, self.source, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.root = self.base / 'runtime'
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('HANDOFF_', 'CONTEXT_WATCH_'))
                    and k not in ('AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'TMUX_PANE', 'AUTORESUME')}
        self.env.update(HOME=str(self.base / 'home'), TMPDIR=str(self.base),
                        CLAUDE_CONFIG_DIR=str(self.base / 'claude'), CODEX_HOME=str(self.base / 'codex'),
                        CONTEXT_WATCH_LOG='0', CONTEXT_WATCH_RESUME_LOG='0', PYTHONDONTWRITEBYTECODE='1')
        Path(self.env['HOME']).mkdir()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.transcript = self.base / 'transcript.jsonl'
        self.transcript.write_text(json.dumps({'message': {'model': 'claude-test',
            'usage': {'input_tokens': 140000}}}) + '\n')
        self.env.update(HANDOFF_AUTO='0', CONTEXT_WATCH_PENDING='0', CONTEXT_WATCH_JEV='0',
                        CONTEXT_WATCH_AGENT='claude', HANDOFF_STATE_DIR=str(self.base / 'state'))
        Path(self.env['HANDOFF_STATE_DIR']).mkdir()

    def config(self, **values):
        data = dict(handoff_v3=True, mandatory_at_tokens=135000)
        data.update(values)
        (self.project / '.context-watch.json').write_text(json.dumps(data))

    def gate(self, event='PreToolUse', tool='Edit', **fields):
        if not hasattr(self, 'snapshot'):
            self.snapshot = Path(self.cli('create', 'gate')['snapshot'])
        evt = dict(hook_event_name=event, cwd=str(self.project), session_id='gate-test',
                   transcript_path=str(self.transcript), tool_name=tool,
                   tool_input={'file_path': 'work.py', 'old_string': 'a', 'new_string': 'b'})
        evt.update(fields)
        result = subprocess.run([sys.executable, '-I', '-B',
            str(self.snapshot / 'hooks/mandatory_gate.py')], cwd=self.project,
            env=self.env, input=json.dumps(evt), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def test_opted_in_project_denies_new_work_at_mandatory_point(self):
        self.config()
        result = self.gate()
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('135000', result['hookSpecificOutput']['permissionDecisionReason'])

    def test_reads_status_and_user_decisions_remain_available(self):
        self.config()
        for tool in ('Read', 'Glob', 'Grep', 'TaskGet', 'TaskList', 'AskUserQuestion'):
            with self.subTest(tool=tool):
                self.assertEqual(self.gate(tool=tool), {})
        for command in ('cat work.py', 'git status', 'python3 -c "print(1)"',
                        'echo safe; rm -rf work', 'true'):
            with self.subTest(command=command):
                self.assertEqual(self.gate(tool='Bash', tool_input={'command': command})
                    ['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_exact_save_command_accepts_literal_json_without_shell_expansion(self):
        self.config()
        reason = self.gate()['hookSpecificOutput']['permissionDecisionReason']
        command = reason.split('Save command: ', 1)[1].split('\n', 1)[0]
        body = json.dumps({'topic': 'gate', 'description': 'Continue',
            'body': '## Objective\nContinue.\n## Current state\nReady.\n## Next steps\nReview.\n'})
        command += " <<'HANDOFF_JSON'\n" + body + '\nHANDOFF_JSON'
        self.assertEqual(self.gate(tool='Bash', tool_input={'command': command}), {})
        for bad in (command + '\ntrue', command.replace("<<'HANDOFF_JSON'", '<<HANDOFF_JSON'),
                    command.replace(' save ', ' repair ')):
            with self.subTest(command=bad):
                self.assertEqual(self.gate(tool='Bash', tool_input={'command': bad})
                    ['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_global_timing_and_project_override_report_effective_source(self):
        global_file = Path(self.env['HOME']) / '.context-watch/thresholds.json'
        global_file.parent.mkdir()
        global_file.write_text(json.dumps({'mandatory_at_tokens': 135000}))
        (self.project / '.context-watch.json').write_text('{"handoff_v3":true}')
        reason = self.gate()['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn(str(global_file), reason)
        self.config(mandatory_at_tokens=150000)
        self.assertEqual(self.gate(session_id='project-override'), {})
        self.config(mandatory_at_tokens=None)
        self.assertEqual(self.gate(), {})

    def test_invalid_timing_reports_configuration_error_without_enforcing(self):
        for point in (True, 0, -1, '140000', 90000, 190000):
            with self.subTest(point=point):
                self.config(mandatory_at_tokens=point)
                result = self.gate(context_window=200000, session_id='invalid-' + str(point))
                self.assertIn('mandatory_at_tokens', result['systemMessage'])
                self.assertNotIn('hookSpecificOutput', result)
        self.config(warning_window_tokens=-1)
        self.assertIn('warning_window_tokens', self.gate(session_id='invalid-warning')['systemMessage'])

    def test_no_opt_in_or_null_point_is_quiet_without_telemetry(self):
        self.transcript.unlink()
        self.assertEqual(self.gate(), {})
        self.config(handoff_v3=False)
        self.assertEqual(self.gate(), {})
        self.config(mandatory_at_tokens=None)
        self.assertEqual(self.gate(), {})

    def test_stop_is_bounded_and_publication_releases_only_save_gate(self):
        self.config()
        self.assertEqual(self.gate(event='Stop')['decision'], 'block')
        # Native recursion flag must allow the agent to report a blocked save.
        result = self.gate(event='Stop', stop_hook_active=True)
        self.assertNotIn('decision', result)
        self.assertIn('blocker', result['systemMessage'])
        # Unsetting the native flag cannot produce an endless sequence either.
        self.gate(event='Stop')
        self.assertNotIn('decision', self.gate(event='Stop'))
        self.assertEqual(self.gate()['hookSpecificOutput']['permissionDecision'], 'deny')
        reason = self.gate()['hookSpecificOutput']['permissionDecisionReason']
        command = reason.split('Save command: ', 1)[1].split('\n', 1)[0]
        body = json.dumps({'topic': 'gate', 'description': 'Continue',
            'body': '## Objective\nContinue.\n## Current state\nReady.\n## Next steps\nReview.\n'})
        saved = subprocess.run(command, shell=True, cwd=self.project, env=self.env,
                               input=body, capture_output=True, text=True, timeout=30)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(json.loads(saved.stdout)['outcome'], 'saved')
        self.assertEqual(self.gate(), {})
        self.assertEqual(self.gate(event='Stop'), {})

    def test_missing_telemetry_reports_unavailable_without_inventing_usage(self):
        self.config()
        self.transcript.unlink()
        result = self.gate()
        self.assertIn('telemetry', result['systemMessage'])
        self.assertNotIn('hookSpecificOutput', result)

    def test_runtime_activation_registers_isolated_gate_only_for_claude(self):
        release = self.cli('create', 'registration')
        self.cli('activate', release['release'], 'claude')
        config = json.loads((self.base / 'claude/settings.json').read_text())
        for event in ('PreToolUse', 'Stop'):
            commands = [h['command'] for g in config['hooks'][event] for h in g['hooks']]
            self.assertTrue(any(' -I -B ' in c and 'mandatory_gate.py' in c for c in commands))
        self.cli('activate', release['release'], 'codex')
        self.assertNotIn('mandatory_gate.py', (self.base / 'codex/hooks.json').read_text())

    def test_invalid_configuration_diagnostic_is_once_per_session(self):
        self.config(mandatory_at_tokens=-1)
        self.assertIn('invalid mandatory configuration', self.gate()['systemMessage'])
        self.assertEqual(self.gate(), {})
        self.assertEqual(self.gate(event='Stop'), {})

    def test_subdirectory_uses_project_config_state_and_auto_authority(self):
        self.config(auto=True)
        (self.project / '.handoffs').mkdir()
        child = self.project / 'pkg'
        child.mkdir()
        self.env.pop('HANDOFF_AUTO')
        denied = self.gate(cwd=str(child))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.transcript.unlink()
        self.assertEqual(self.gate()['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.gate(cwd=str(child))['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_runtime_failure_denies_only_observed_obligations(self):
        self.config()
        self.gate(tool='Read')  # Create a release without creating an obligation.
        (self.snapshot / 'hooks/context_watch.py').chmod(0o600)
        result = self.gate()
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        for rows in (
            [{'message': {'usage': {'input_tokens': 10000}}}],
            [{'timestamp': '2000-01-01T00:00:00Z', 'message': {'usage': {'input_tokens': 140000}}}],
            [{'message': {'usage': {'input_tokens': 140000}}}, {'type': 'system', 'subtype': 'compact_boundary'}],
            [{'isSidechain': True, 'message': {'usage': {'input_tokens': 140000}}}],
        ):
            with self.subTest(rows=rows):
                self.transcript.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
                self.assertNotIn('hookSpecificOutput', self.gate())

    def test_runtime_failure_preserves_latched_obligation_below_point(self):
        self.config()
        self.assertEqual(self.gate()['hookSpecificOutput']['permissionDecision'], 'deny')
        self.transcript.write_text(json.dumps({'message': {'usage': {'input_tokens': 10000}}}) + '\n')
        (self.snapshot / 'hooks/context_watch.py').chmod(0o600)
        self.assertEqual(self.gate()['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_runtime_failure_preserves_transcript_identity_latch(self):
        self.config()
        self.assertEqual(self.gate(session_id='')['hookSpecificOutput']['permissionDecision'], 'deny')
        self.transcript.write_text(json.dumps({'message': {'usage': {'input_tokens': 10000}}}) + '\n')
        (self.snapshot / 'hooks/context_watch.py').chmod(0o600)
        self.assertEqual(self.gate(session_id='')['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_watcher_subdirectory_obeys_project_auto_disable(self):
        self.config(auto=False)
        (self.project / '.handoffs').mkdir()
        child = self.project / 'pkg'
        child.mkdir()
        self.env.pop('HANDOFF_AUTO')
        user = Path(self.env['HOME']) / '.context-watch/thresholds.json'
        user.parent.mkdir()
        user.write_text('{"auto":true}')
        rows = [{'message': {'model': 'claude-test', 'usage': {'input_tokens': n}}}
                for n in (1000, 140000)]
        self.transcript.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        result = subprocess.run([sys.executable, str(self.source / 'hooks/context_watch.py')],
            cwd=child, env=self.env, input=json.dumps(dict(hook_event_name='PostToolUse',
                session_id='child-watcher', cwd=str(child), transcript_path=str(self.transcript))),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Fully automatic mode is active', result.stdout)
        self.assertIn('mandatory-unenforced', result.stdout)

    def test_runtime_failure_before_obligation_is_diagnostic_only(self):
        self.config()
        self.transcript.write_text(json.dumps({'message': {'usage': {'input_tokens': 10000}}}) + '\n')
        self.assertEqual(self.gate(), {})
        (self.snapshot / 'hooks/context_watch.py').chmod(0o600)
        result = self.gate()
        self.assertNotIn('hookSpecificOutput', result)
        self.assertIn('mandatory-state-error', result['systemMessage'])

    def test_unsealed_runtime_fails_closed_but_reads_remain_available(self):
        self.config()
        self.gate()
        path = self.snapshot / 'hooks/context_watch.py'
        path.chmod(0o600)
        result = self.gate()
        self.assertIn('mandatory-state-error', result['hookSpecificOutput']['permissionDecisionReason'])
        self.assertEqual(self.gate(tool='Read'), {})

    def test_unsupported_host_reports_diagnostic_only(self):
        self.config()
        self.transcript.write_text(json.dumps({'type': 'event_msg', 'payload': {
            'type': 'token_count', 'info': {'model_context_window': 200000,
            'last_token_usage': {'input_tokens': 140000, 'total_tokens': 140000}}}}) + '\n')
        result = subprocess.run([sys.executable, str(self.source / 'hooks/context_watch.py')],
            cwd=self.project, env=dict(self.env, CONTEXT_WATCH_AGENT='codex'),
            input=json.dumps(dict(hook_event_name='PostToolUse', session_id='unsupported',
                cwd=str(self.project), transcript_path=str(self.transcript))),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mandatory-unenforced', result.stdout)
        self.assertNotIn('permissionDecision', result.stdout)

    def test_project_python_is_never_imported_and_state_error_allows_save(self):
        self.config()
        sentinel = self.base / 'imported-project-code'
        (self.project / 'context_watch.py').write_text('open(%r, "w").write("unsafe")' % str(sentinel))
        self.env['PYTHONPATH'] = str(self.project)
        reason = self.gate()['hookSpecificOutput']['permissionDecisionReason']
        self.assertFalse(sentinel.exists())
        command = reason.split('Save command: ', 1)[1].split('\n', 1)[0]
        command += " <<'HANDOFF_JSON'\n{}\nHANDOFF_JSON"
        state = next((Path(self.env['HOME']) / '.context-watch/mandatory').glob('*.json'))
        state.write_text('interrupted write')
        result = self.gate(tool='Bash', tool_input={'command': command})
        self.assertNotIn('hookSpecificOutput', result)
        self.assertEqual(self.gate()['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_stale_telemetry_does_not_invent_current_occupancy(self):
        self.config()
        row = json.loads(self.transcript.read_text())
        row['timestamp'] = '2000-01-01T00:00:00Z'
        self.transcript.write_text(json.dumps(row) + '\n')
        result = self.gate()
        self.assertNotIn('hookSpecificOutput', result)
        self.assertIn('telemetry', result['systemMessage'])

    def test_invalid_timing_disables_automatic_stop_transition(self):
        self.config()
        self.gate()
        self.config(mandatory_at_tokens=-1)
        result = subprocess.run([sys.executable, str(self.source / 'hooks/context_watch.py')],
            cwd=self.project, env=dict(self.env, HANDOFF_AUTO='1'),
            input=json.dumps(dict(hook_event_name='Stop', session_id='gate-test',
                cwd=str(self.project), transcript_path=str(self.transcript))),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['decision'], 'block')

    def test_project_v3_auto_cannot_grant_continuation(self):
        self.config(auto=True)
        self.env.pop('HANDOFF_AUTO')
        rows = [{'message': {'model': 'claude-test', 'usage': {'input_tokens': n}}}
                for n in (1000, 140000)]
        self.transcript.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')

        def notice(session):
            result = subprocess.run([sys.executable, str(self.source / 'hooks/context_watch.py')],
                cwd=self.project, env=self.env, input=json.dumps(dict(hook_event_name='PostToolUse',
                    session_id=session, cwd=str(self.project), transcript_path=str(self.transcript))),
                text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertNotIn('Fully automatic mode is active', notice('project-auto'))
        reason = self.gate()['hookSpecificOutput']['permissionDecisionReason']
        command = reason.split('Save command: ', 1)[1].split('\n', 1)[0]
        draft = {'topic': 'authority', 'description': 'Continue',
                 'body': '## Objective\nContinue.\n## Current state\nReady.\n## Next steps\nReview.\n'}
        saved = subprocess.run(command, shell=True, cwd=self.project, env=self.env,
            input=json.dumps(draft), text=True, capture_output=True, timeout=30)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(json.loads(saved.stdout)['transition']['kind'], 'new-session')
        global_file = Path(self.env['HOME']) / '.context-watch/thresholds.json'
        global_file.parent.mkdir(exist_ok=True)
        global_file.write_text('{"auto":true}')
        self.assertIn('Fully automatic mode is active', notice('user-auto'))
