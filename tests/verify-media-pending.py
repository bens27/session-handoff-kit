#!/usr/bin/env python3
"""Regression tests for context-watch pending token estimation.

Pass hook paths on argv to test other copies. With no args, tests the three
sibling staged hooks: installed.py, codex.py, and plugin.py.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOOKS = [
    os.path.join(HERE, "installed.py"),
    os.path.join(HERE, "codex.py"),
    os.path.join(HERE, "plugin.py"),
]
MEDIA_TOKENS = 4096
SCREENSHOT_B64_LEN = 1_556_792


def data_url(kind="image", size=12000):
    return "data:%s/png;base64,%s" % (kind, "A" * size)


def load_hook(path, idx):
    name = "context_watch_test_%d_%s" % (idx, os.path.basename(path).replace(".", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PendingEstimatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths = sys.argv[1:] or DEFAULT_HOOKS
        cls.hook_paths = [os.path.abspath(path) for path in paths]
        cls.modules = [load_hook(path, idx) for idx, path in enumerate(cls.hook_paths)]

    def estimate(self, module, payload, key="tool_response"):
        return module.estimate_pending({key: payload})

    def test_ordinary_text_and_json_keep_character_estimate(self):
        for module in self.modules:
            with self.subTest(module=module.__file__):
                text = "abcd" * 25
                obj = {"alpha": "x" * 40, "n": 3, "items": [1, "two"]}
                self.assertEqual(self.estimate(module, text), len(text) // 4)
                self.assertEqual(self.estimate(module, obj), len(json.dumps(obj)) // 4)
                encoded = json.dumps(obj)
                self.assertEqual(self.estimate(module, encoded), len(encoded) // 4)

    def test_disabled_pending_missing_payload_and_errors(self):
        for module in self.modules:
            with self.subTest(module=module.__file__):
                with mock.patch.dict(os.environ, {"CONTEXT_WATCH_PENDING": "0"}):
                    self.assertEqual(module.estimate_pending({"tool_response": "x" * 1000}), 0)
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("CONTEXT_WATCH_PENDING", None)
                    self.assertEqual(module.estimate_pending({}), 0)
                    self.assertEqual(module.estimate_pending({"tool_response": object()}), 0)

    def test_large_real_text_still_crosses_130k(self):
        huge = "T" * (130_000 * 4)
        for module in self.modules:
            with self.subTest(module=module.__file__):
                self.assertGreaterEqual(self.estimate(module, huge), 130_000)

    def test_screenshot_sized_image_url_stays_under_threshold_at_58039(self):
        payload = {"image_url": data_url(size=SCREENSHOT_B64_LEN)}
        old_estimate = len(json.dumps(payload)) // 4
        for module in self.modules:
            with self.subTest(module=module.__file__):
                pending = self.estimate(module, payload)
                self.assertGreaterEqual(old_estimate, 389_000)
                self.assertLess(pending, 130_000 - 58_039)
                self.assertGreaterEqual(pending, MEDIA_TOKENS)

    def test_media_envelope_variants_are_bounded(self):
        variants = [
            {"image_url": {"url": data_url(size=50000)}},
            {"type": "image", "mimeType": "image/png", "data": "A" * 50000},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": "A" * 50000}},
            {"audio_url": data_url("audio", 50000).replace("/png;", "/mpeg;")},
            {"type": "audio", "mimeType": "audio/mpeg", "data": "A" * 50000},
            {"type": "audio", "source": {
                "type": "base64", "media_type": "audio/mpeg", "data": "A" * 50000}},
            {"input_audio": {"format": "wav", "data": "A" * 50000}},
        ]
        for module in self.modules:
            for payload in variants:
                with self.subTest(module=module.__file__, payload=payload):
                    pending = self.estimate(module, payload)
                    self.assertGreaterEqual(pending, MEDIA_TOKENS)
                    self.assertLess(pending, len(json.dumps(payload)) // 4)
                    self.assertLess(pending, 8000)

    def test_mixed_text_and_media_preserves_text_count(self):
        payload = {
            "content": [
                {"type": "text", "text": "x" * 400},
                {"type": "image_url", "image_url": {"url": data_url(size=40000)}},
                "tail text" * 20,
            ]
        }
        for module in self.modules:
            with self.subTest(module=module.__file__):
                pending = self.estimate(module, payload)
                self.assertGreater(pending, MEDIA_TOKENS + 120)
                self.assertLess(pending, 6000)

    def test_json_encoded_structured_media_and_data_urls(self):
        payloads = [
            json.dumps({"tool": "view_image", "image_url": data_url(size=40000)}),
            json.dumps({"result": "before %s after" % data_url(size=40000)}),
        ]
        for module in self.modules:
            for payload in payloads:
                with self.subTest(module=module.__file__, payload=payload[:30]):
                    pending = self.estimate(module, payload)
                    self.assertGreaterEqual(pending, MEDIA_TOKENS)
                    self.assertLess(pending, 6000)

    def test_serialized_json_without_media_keeps_original_length(self):
        payloads = [
            "{" + " " * 600000 + "\"x\":1}",
            json.dumps({"message": "snowman \u2603", "items": ["alpha", "beta"]}, indent=2, ensure_ascii=False),
        ]
        for module in self.modules:
            for payload in payloads:
                with self.subTest(module=module.__file__, payload=payload[:30]):
                    self.assertEqual(self.estimate(module, payload), len(payload) // 4)

    def test_serialized_media_preserves_large_formatting_overhead(self):
        payload = "{" + " " * 600000 + "\"image_url\":\"%s\"}" % data_url(size=40000)
        for module in self.modules:
            with self.subTest(module=module.__file__):
                pending = self.estimate(module, payload)
                self.assertGreaterEqual(pending, 130000)
                self.assertLess(pending, len(payload) // 4)

    def test_unrecognized_long_plain_strings_are_still_counted(self):
        plain = "A" * 200_000
        base64_like = "QUJD" * 50_000
        almost_media = {"data": "A" * 160_000, "mimeType": "image/png"}
        for module in self.modules:
            with self.subTest(module=module.__file__):
                self.assertEqual(self.estimate(module, plain), len(plain) // 4)
                self.assertEqual(self.estimate(module, base64_like), len(base64_like) // 4)
                self.assertEqual(
                    self.estimate(module, almost_media),
                    len(json.dumps(almost_media)) // 4,
                )

    def test_codex_last_usage_not_cumulative_or_double_cached(self):
        entries = [
            {"payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 125000, "input_tokens": 100000,
                                     "cached_input_tokens": 75000, "output_tokens": 25000}}}},
            {"payload": {"type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 58039, "input_tokens": 50000,
                                     "cached_input_tokens": 30000, "output_tokens": 8039}}}},
        ]
        for module in self.modules:
            with self.subTest(module=module.__file__):
                breakdown, _window, _model = module.codex_usage(entries)
                self.assertEqual(breakdown["occupancy"], 58039)
                self.assertEqual(breakdown["cache_read"], 30000)

    def write_transcript(self, directory, total=58039):
        path = os.path.join(directory, "transcript.jsonl")
        entries = [
            {"payload": {"type": "token_count", "info": {
                "model_context_window": 200000,
                "last_token_usage": {
                    "total_tokens": total,
                    "input_tokens": 50000,
                    "cached_input_tokens": 30000,
                    "output_tokens": total - 50000,
                }}}},
            {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
        ]
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        return path

    def run_hook(self, hook_path, payload, session_id):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as tmp:
            transcript = self.write_transcript(tmp)
            event = {
                "hook_event_name": "PostToolUse",
                "transcript_path": transcript,
                "session_id": session_id,
                "cwd": tmp,
                "model": "gpt-5-codex",
                "tool_response": payload,
            }
            env = os.environ.copy()
            env.update({
                "HOME": home,
                "TMPDIR": tmp,
                "CONTEXT_WATCH_AGENT": "codex",
                "CONTEXT_WATCH_LOG": "0",
                "HANDOFF_AT": "130000",
            })
            env.pop("CONTEXT_WATCH_PENDING", None)
            return subprocess.run(
                [sys.executable, hook_path],
                input=json.dumps(event),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=tmp,
                check=False,
            )

    def test_subprocess_screenshot_does_not_trigger_but_huge_text_does(self):
        screenshot = {"image_url": data_url(size=SCREENSHOT_B64_LEN)}
        huge_text = "Z" * ((130_000 - 58_039 + 100) * 4)
        for idx, hook_path in enumerate(self.hook_paths):
            with self.subTest(hook=hook_path, case="screenshot"):
                result = self.run_hook(hook_path, screenshot, "shot-%d" % idx)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
            with self.subTest(hook=hook_path, case="huge_text"):
                result = self.run_hook(hook_path, huge_text, "text-%d" % idx)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("context-watch", result.stderr)
                self.assertIn("130,000", result.stderr)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]])
