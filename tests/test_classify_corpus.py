"""G12: real save and resume request shapes keep their classification.

The fixture holds prompts that were misrouted before G12 (clause-form saves,
typos, `continue <path>`) beside prose that must stay inert.
"""
from pathlib import Path
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'skills/session-handoff/hooks'))
import handoff_dispatch  # noqa: E402

INERT = ('none', 'maintenance', 'neutral')


def corpus():
    rows = (REPO / 'tests/fixtures/classify-corpus.tsv').read_text(encoding='utf-8').splitlines()
    return [tuple(r.split('\t', 1)) for r in rows if r.strip() and not r.startswith('#')]


class ClassifyCorpusTests(unittest.TestCase):
    def test_every_corpus_prompt_keeps_its_label(self):
        for label, prompt in corpus():
            with self.subTest(prompt=prompt):
                got = handoff_dispatch.classify(prompt)
                if label == 'none':
                    self.assertIn(got, INERT)
                else:
                    self.assertEqual(got, label)

    def test_quoted_clause_stays_a_mention(self):
        self.assertEqual(handoff_dispatch.classify('what does "session handoff to yourself" do'), 'none')

    def test_pressure_notice_keeps_precedence(self):
        self.assertEqual(handoff_dispatch.classify(
            '[context-watch] write a handoff now, then session handoff to yourself'), 'pressure')


if __name__ == '__main__':
    unittest.main()
