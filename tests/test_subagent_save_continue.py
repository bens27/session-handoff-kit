#!/usr/bin/env python3
"""G33: an opted-in subagent notice is save-and-continue, never stop.

The notice tells the subagent to write its findings so far and its work in
progress to its own subagent checkpoint, plus a `git diff` patch beside that
checkpoint when its brief forbids commits, and then to keep working. No kit
text aimed at a subagent says it hit a context limit or should end its turn,
and the parent's checkpoint is never named as somewhere to write.

Run: python3 -m pytest -q tests/test_subagent_save_continue.py"""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_context_watch_subagent_notice as sub  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills/session-handoff/hooks"))
import context_watch  # noqa: E402

BANNED = ("stop", "end your turn", "end the turn", "context limit", "hand over", "wrap up", "finish now")


class SaveAndContinue(sub.SubagentCase):
    def notice(self):
        os.makedirs(os.path.join(self.tmp, ".context-watch"), exist_ok=True)
        with open(os.path.join(self.tmp, ".context-watch", "thresholds.json"), "w") as f:
            f.write('{"subagent_at": 60000}')
        self.write_sub([20000, 60000])
        return self.context(self.run_hook())

    def test_notice_names_checkpoint_patch_and_keep_working(self):
        text = self.notice()
        low = text.lower()
        self.assertIn("findings so far", low)
        self.assertIn("work in progress", low)
        self.assertIn(os.path.join(".context-watch", "subagents"), text)
        self.assertIn("git diff", low)
        self.assertIn("forbids commits", low)
        self.assertIn(".patch", text)
        self.assertIn("keep working", low)
        for banned in BANNED:
            self.assertNotIn(banned, low, banned)

    def test_patch_sits_beside_the_subagent_checkpoint(self):
        checkpoint = context_watch.subagent_checkpoint_path("a1", "parent")
        message = context_watch.subagent_message(60000, 60000, "a1", "parent")
        self.assertIn(checkpoint, message)
        self.assertIn(os.path.splitext(checkpoint)[0] + ".patch", message)
        self.assertNotIn(".handoffs", message)  # never the parent's ledger

    def test_no_stop_wording_in_any_kit_subagent_text(self):
        source = (REPO / "skills/session-handoff/hooks/context_watch.py").read_text()
        start = source.index("def subagent_message")
        body = source[start:source.index("\ndef ", start + 1)].lower()
        for banned in BANNED:
            self.assertNotIn(banned, body.split('"""')[-1], banned)


if __name__ == "__main__":
    unittest.main()
