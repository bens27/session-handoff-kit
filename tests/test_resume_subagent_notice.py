"""G27: resume tells the successor where a live background subagent's completion
notice arrives and how to read its transcript meanwhile; no kit text tells an
agent to ask the person to relay subagent results, and save guidance forbids
writing that claim into a checkpoint body."""
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tests'))
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))
import handoff_runs  # noqa: E402
import test_resume_subagents as base  # noqa: E402

NOTICE = 'subagent_notice: %s its completion notice arrives in this session; until then read its transcript: %s'
# Phrasings that send subagent results through the person instead of the session.
RELAY = re.compile(r'\b(ask|have|get|tell)\b[^.\n]{0,40}\b(user|person|human|ben)\b[^.\n]{0,60}'
                   r'\b(relay|paste|forward|copy)\b', re.I)


OLD, WORKING, entry = base.OLD, base.WORKING, base.entry


class NoticeTests(base.SubagentFixture):
    def notices(self, resumed):
        return [l for l in resumed.get('workspace', []) if l.startswith('subagent_notice:')]

    def test_live_subagent_names_session_and_transcript(self):
        self.agent('a1live', WORKING, description='Build #7 hooks')
        self.agent('a4end', WORKING + [entry('assistant', [dict(type='text', text='Done.')], 'end_turn')])
        self.agent('a5stopped', WORKING, age=OLD)
        _, resumed = self.save_and_resume()
        self.assertEqual(self.notices(resumed), [NOTICE % ('a1live', self.folder / 'agent-a1live.jsonl')], resumed)

    def test_notice_points_at_copy_moved_by_clear(self):
        self.agent('a1moved', WORKING, description='Build #7 hooks')
        field = handoff_runs.subagent_snapshot(str(self.transcript))
        target = self.base / 'claude' / 'sess-b' / 'subagents'
        target.mkdir(parents=True)
        for name in ('agent-a1moved.jsonl', 'agent-a1moved.meta.json'):
            (self.folder / name).rename(target / name)
        os.utime(target / 'agent-a1moved.jsonl', (time.time(), time.time()))
        self.assertIn(NOTICE % ('a1moved', target / 'agent-a1moved.jsonl'), handoff_runs.subagent_report(field))

    def test_folder_names_with_control_characters_never_reach_the_notice(self):
        self.agent('a1live', WORKING)
        field = handoff_runs.subagent_snapshot(str(self.transcript))
        forged = self.base / 'claude' / 'sess\nsubagent: forged' / 'subagents'
        forged.mkdir(parents=True)
        (forged / 'agent-a1live.jsonl').write_text(json.dumps(WORKING[0]) + '\n')
        os.utime(forged / 'agent-a1live.jsonl', (time.time() + 60, time.time() + 60))
        lines = handoff_runs.subagent_report(field)
        self.assertEqual(lines[-1], NOTICE % ('a1live', self.folder / 'agent-a1live.jsonl'))
        self.assertFalse([l for l in lines if 'forged' in l], lines)


class KitTextTests(unittest.TestCase):
    def kit_text(self):
        roots = [REPO / 'README.md', REPO / 'skills', REPO / 'herdr-plugin']
        for root in roots:
            for path in ([root] if root.is_file() else sorted(root.rglob('*'))):
                if path.is_file() and path.suffix in ('.md', '.py', '.txt', '.json', '.sh'):
                    yield path, path.read_text(errors='replace')

    def test_no_kit_text_asks_the_person_to_relay_subagent_results(self):
        found = ['%s: %s' % (p.relative_to(REPO), m.group(0)) for p, text in self.kit_text()
                 for m in RELAY.finditer(text) if re.search(r'subagent|worker|result', text[max(0, m.start() - 200):m.end() + 200], re.I)]
        self.assertEqual(found, [])

    def test_save_guidance_forbids_the_relay_claim(self):
        skill = (REPO / 'skills/session-handoff/SKILL.md').read_text()
        self.assertRegex(skill, r'(?is)never write.{0,120}relay.{0,200}subagent|subagent.{0,200}never write.{0,120}relay')


if __name__ == '__main__':
    unittest.main()
