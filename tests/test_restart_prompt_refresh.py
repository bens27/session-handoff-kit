"""14752baa: the kit's own restart prompt refreshes the terminal pointer.

On Claude Code there is no host session id, so a successor is identified only by
the terminal pointer. The successor's first prompt is the one restart_preflight
builds ("Continue the handoff: run exactly <adopt command>"); it must record the
successor session even before its transcript exists (fixture: 9fe919eb,
transcript 8e5609e3 line 6).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HOOKS = Path(__file__).resolve().parents[1] / 'skills/session-handoff/hooks'
sys.path.insert(0, str(HOOKS))

import handoff_contract  # noqa: E402
import handoff_identity  # noqa: E402
import restart_preflight  # noqa: E402

TERMINAL = 'agent-chief-7f5928a0d1bbfe52'
PREDECESSOR = '6f0aceac-fdda-4789-a04f-4191b6965881'
SUCCESSOR = '8e5609e3-fa62-400d-bf85-16dd2e39c0f6'
CLEARED = ('HANDOFF_TERMINAL_ID', 'AGENTSROOM_AGENT_ID', 'CODEX_THREAD_ID', 'HANDOFF_SESSION_ID',
           'CONTEXT_WATCH_DISABLE')


class RestartPromptRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, 'state')
        os.mkdir(self.state)
        self.root = os.path.join(self.tmp.name, 'project')
        os.makedirs(os.path.join(self.root, '.handoffs'))
        self.env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        self.env.update(HANDOFF_STATE_DIR=self.state, AGENTSROOM_AGENT_ID=TERMINAL)
        self.pointer = handoff_identity.terminal_pointer_path(self.root, TERMINAL, self.state)
        with open(self.pointer, 'w', encoding='utf-8') as stream:
            json.dump({'session_id': PREDECESSOR, 'host_session_id': '', 'ts': time.time()}, stream)

    def restart_prompt(self):
        command = 'python3 %s adopt h-0fc3fb66de11a42dde0fc2fd' % (HOOKS / 'restart_preflight.py')
        return handoff_contract.RESTART_PROMPT_PREFIX + command

    def submit(self, prompt):
        evt = dict(hook_event_name='UserPromptSubmit', session_id=SUCCESSOR, cwd=self.root, prompt=prompt,
                   transcript_path=os.path.join(self.tmp.name, 'not-yet-written.jsonl'))
        subprocess.run([sys.executable, str(HOOKS / 'context_watch.py')], input=json.dumps(evt).encode(),
                       env=self.env, cwd=self.root, capture_output=True, timeout=60, check=False)
        with open(self.pointer, encoding='utf-8') as stream:
            return json.load(stream)['session_id']

    def test_restart_prompt_records_successor_before_transcript_exists(self):
        self.assertEqual(self.submit(self.restart_prompt()), SUCCESSOR)

    def test_prefix_is_case_and_leading_space_insensitive(self):
        self.assertEqual(self.submit('  continue THE handoff: run exactly python3 x adopt h-1'), SUCCESSOR)

    def test_prompt_merely_quoting_the_prefix_does_not_refresh(self):
        self.assertEqual(self.submit('Why did ' + self.restart_prompt() + ' fail?'), PREDECESSOR)

    def test_restart_prompt_is_not_a_lean_resume_prompt(self):
        import context_watch
        self.assertTrue(context_watch.restart_prompt(self.restart_prompt()))
        self.assertFalse(context_watch.lean_resume_prompt(self.restart_prompt()))

    def test_restart_preflight_builds_its_prompt_from_the_shared_prefix(self):
        source = (HOOKS / 'restart_preflight.py').read_text(encoding='utf-8')
        self.assertNotIn("'Continue the handoff: run exactly '", source)
        self.assertIs(restart_preflight.RESTART_PROMPT_PREFIX, handoff_contract.RESTART_PROMPT_PREFIX)


if __name__ == '__main__':
    unittest.main()
