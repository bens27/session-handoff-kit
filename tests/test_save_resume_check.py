"""G10: a save says whether a fresh session will find it, and how to resume it.

The reported failure: a just-saved checkpoint was not what the next session
selected, and nothing said so until the successor went looking.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dispatch import BODY, Base  # noqa: E402


class SaveResumeCheckTests(Base):
    def save_draft(self, topic, session='sess-a', terminal='term-a'):
        self.record_hook_session(session, terminal=terminal)
        draft = dict(topic=topic, description=topic.title(), body=BODY, suggested_skills=[])
        r = self.dispatch(operation='save', draft=draft, terminal=terminal)
        self.assertEqual((r['outcome'], r['code']), ('proceed', 'saved'), r)
        return r

    def test_sole_checkpoint_is_selected_and_names_its_resume_prompt(self):
        r = self.save_draft('parser')
        self.assertEqual(r['resume_check'], dict(selected=True, selection='owned'))
        self.assertEqual(r['resume_prompt'], 'resume ' + r['path'])
        self.assertNotIn('will not pick', r['next_action']['text'])
        self.record_hook_session('sess-b')  # the successor after /clear
        resumed = self.dispatch(prompt=r['resume_prompt'])
        self.assertEqual((resumed['operation'], resumed['target']['path']), ('resume', r['path']))

    def test_second_fresh_park_warns_that_lookup_will_ask(self):
        self.save_draft('parser')
        r = self.save_draft('lexer', session='sess-b')
        self.assertEqual(r['resume_check']['selected'], False)
        self.assertEqual(r['resume_check']['code'], 'target_ambiguous')
        self.assertIn('will not pick this checkpoint', r['next_action']['text'])
        self.assertIn(r['resume_prompt'], r['next_action']['text'])
        self.assertTrue(any('resume_check' in w for w in r['warnings']), r['warnings'])
