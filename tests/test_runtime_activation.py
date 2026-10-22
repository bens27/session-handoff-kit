"""Runtime lifecycle through the installer CLI, using isolated host configs."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1] / "skills/session-handoff"


class RuntimeActivation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        shutil.copytree(SKILL, self.source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.root = self.base / "runtime"
        self.env = dict(os.environ, HOME=str(self.base / "home"), USERPROFILE=str(self.base / "home"), TMPDIR=str(self.base),
                        CLAUDE_CONFIG_DIR=str(self.base / "claude"),
                        CODEX_HOME=str(self.base / "codex"), CONTEXT_WATCH_LOG="0",
                        CONTEXT_WATCH_RESUME_LOG="0", PYTHONDONTWRITEBYTECODE="1")
        for key in ("CODEX_THREAD_ID", "HANDOFF_SESSION_ID"):
            self.env.pop(key, None)
        Path(self.env["HOME"]).mkdir()

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(self.source / "install.py"),
                                 "runtime", "--root", str(self.root), *args],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout) if ok else result

    def test_create_isolated_validated_snapshot_without_activation(self):
        result = self.cli("create", "v1")
        snapshot = Path(result["snapshot"])
        self.assertEqual((snapshot / 'hooks/restart_preflight.py').read_bytes(),
                         (self.source / 'hooks/restart_preflight.py').read_bytes())
        probe = subprocess.run([sys.executable, '-B', str(snapshot / 'hooks/restart_preflight.py'), '--help'],
                               env=self.env, text=True, capture_output=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual((snapshot / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        self.assertEqual((snapshot / "hooks/handoff_contract.py").read_bytes(),
                         (self.source / "hooks/handoff_contract.py").read_bytes())
        (self.source / "SKILL.md").write_text("development edit")
        self.assertNotEqual((snapshot / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        self.assertTrue(self.cli("validate", snapshot.name)["valid"])
        self.assertFalse((self.base / "claude/settings.json").exists())
        self.assertFalse((self.base / "codex/hooks.json").exists())

    def test_unknown_secret_is_rejected_before_publication(self):
        (self.source / ".env").write_text("TOKEN=private-secret-value")
        result = self.cli("create", "secret", ok=False)
        self.assertIn("undeclared runtime input", result.stderr)
        self.assertNotIn("private-secret-value", result.stderr)
        self.assertFalse(list(self.root.glob("releases/*")))

    def test_storage_is_private_and_declared_package_extras_are_preserved(self):
        result = self.cli("create", "private")
        snapshot = Path(result["snapshot"])
        for directory in (self.root, self.root / "releases"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in snapshot.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        self.assertEqual((snapshot / "lane.py").read_bytes(), (self.source / "lane.py").read_bytes())
        self.assertEqual((snapshot / "hooks/thresholds.example.json").read_bytes(),
                         (self.source / "hooks/thresholds.example.json").read_bytes())

    def test_default_root_is_inside_fixture_home(self):
        result = subprocess.run([sys.executable, str(self.source / "install.py"), "runtime", "create", "default"],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = Path(json.loads(result.stdout)["snapshot"])
        self.assertTrue(snapshot.is_relative_to(Path(self.env["HOME"]).resolve()))

    def test_snapshot_can_transfer_checkpoint_at_permitted_path_without_runtime_writes(self):
        release = self.cli("create", "v1")
        project = self.base / "project"
        (project / ".handoffs").mkdir(parents=True)
        protected = project / ".agentsroom"
        protected.mkdir()
        (protected / "sentinel").write_text("host-owned")
        env = {k: v for k, v in self.env.items() if k not in
               ("AGENTSROOM_AGENT_ID", "HANDOFF_AUTO", "HANDOFF_TERMINAL_ID", "TMUX_PANE",
                "AUTORESUME", "CONTEXT_WATCH_AUTORESUME", "HANDOFF_RUN_ID", "HANDOFF_AT")}
        env.update(TMPDIR=str(self.base), CONTEXT_WATCH_ORIGIN="test", CONTEXT_WATCH_JEV="0")
        ledger = Path(release["snapshot"]) / "hooks/handoff_ledger.py"
        body = "## Objective\nKeep the summary.\n## Current state\nReview pending.\n## Next steps\nRequest independent review.\n"
        saved = subprocess.run([sys.executable, "-B", str(ledger), "save", ".", "--session", "sender",
                                "--request-id", "summary"], cwd=project, env=env, text=True,
                               input=json.dumps({"topic": "summary", "description": "Review pending", "body": body}),
                               capture_output=True, timeout=30)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        checkpoint = Path(json.loads(saved.stdout)["path"])
        self.assertEqual(checkpoint.parent.resolve(), (project / ".handoffs").resolve())
        prepared = subprocess.run([sys.executable, "-B", str(ledger), "prepare", str(checkpoint),
                                   "--session", "successor"], cwd=project, env=env, text=True,
                                  capture_output=True, timeout=30)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        self.assertIn("Request independent review.", prepared.stdout)
        self.assertEqual([p.name for p in protected.iterdir()], ["sentinel"])
        self.assertEqual((protected / "sentinel").read_text(), "host-owned")

    def test_activation_and_rollback_preserve_foreign_hooks(self):
        config = self.base / "codex/hooks.json"
        config.parent.mkdir()
        before = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo foreign"}]}]}}
        config.write_text(json.dumps(before))
        first = self.cli("create", "v1")
        self.cli("activate", first["release"], "codex")
        active = config.read_bytes()
        self.assertIn(first["snapshot"], active.decode())
        self.assertIn("echo foreign", active.decode())
        (self.source / "reference.md").write_text("release two")
        second = self.cli("create", "v2")
        self.cli("activate", second["release"], "codex")
        self.assertIn(second["snapshot"], config.read_text())
        self.cli("rollback", "codex")
        self.assertEqual(config.read_bytes(), active)
        self.cli("rollback", "codex")
        self.assertEqual(json.loads(config.read_text()), before)

    def test_tampered_snapshot_cannot_change_active_configuration(self):
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "claude")
        config = self.base / "claude/settings.json"
        active = config.read_bytes()
        file = Path(release["snapshot"]) / "hooks/context_watch.py"
        file.chmod(0o644)
        file.write_text("raise RuntimeError('tampered')")
        self.cli("validate", release["release"], ok=False)
        self.cli("activate", release["release"], "claude", ok=False)
        self.assertEqual(config.read_bytes(), active)

    def test_rollback_refuses_a_corrupt_previous_snapshot(self):
        first = self.cli("create", "v1")
        self.cli("activate", first["release"], "claude")
        second = self.cli("create", "v2")
        self.cli("activate", second["release"], "claude")
        config = self.base / "claude/settings.json"
        active = config.read_bytes()
        file = Path(first["snapshot"]) / "SKILL.md"
        file.chmod(0o644)
        file.write_text("corrupt")
        self.cli("rollback", "claude", ok=False)
        self.assertEqual(config.read_bytes(), active)

    def test_creation_rejects_symlinks_and_manifest_cannot_be_rewritten(self):
        (self.source / "external").symlink_to(self.base)
        self.cli("create", "unsafe", ok=False)
        (self.source / "external").unlink()
        result = self.cli("create", "v1")
        manifest = Path(result["snapshot"]) / "runtime-manifest.json"
        manifest.chmod(0o644)
        content = json.loads(manifest.read_text())
        content["version"] = "other"
        manifest.write_text(json.dumps(content))
        self.cli("validate", result["release"], ok=False)

    def test_failed_creation_publishes_no_snapshot(self):
        (self.source / "hooks/context_watch.py").write_text("invalid python !!!")
        self.cli("create", "broken", ok=False)
        self.assertEqual(list((self.root / "releases").iterdir()), [])
        self.assertFalse((self.base / "codex/hooks.json").exists())

    def test_rollback_refuses_later_configuration_edits(self):
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "codex")
        config = self.base / "codex/hooks.json"
        edited = json.loads(config.read_text())
        edited["foreign"] = True
        config.write_text(json.dumps(edited))
        before = config.read_bytes()
        self.cli("rollback", "codex", ok=False)
        self.assertEqual(config.read_bytes(), before)

    def test_unparsable_foreign_hook_does_not_block_activation_or_rollback(self):
        config = self.base / "codex/hooks.json"
        config.parent.mkdir()
        foreign = {"hooks": [{"type": "command", "command": "echo 'unbalanced foreign hook"}]}
        config.write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}))
        before = config.read_bytes()
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "codex")
        self.assertIn(foreign, json.loads(config.read_text())["hooks"]["SessionStart"])
        self.cli("rollback", "codex")
        self.assertEqual(config.read_bytes(), before)

    def test_malformed_receipts_are_skipped_without_blocking_valid_rollback(self):
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "codex")
        receipts = self.root / "transactions"
        valid = json.loads(next(receipts.glob("*.json")).read_text())
        invalid = ["{broken json", "{}", "[]", json.dumps(dict(valid, format=999)),
                   json.dumps(dict(valid, before="not base64!")),
                   json.dumps(dict(valid, before_mode="invalid"))]
        for i, content in enumerate(invalid):
            (receipts / ("zz-invalid-%d.json" % i)).write_text(content)
        result = self.cli("rollback", "codex")
        self.assertEqual(result["skipped_receipts"], len(invalid))
        self.assertFalse((self.base / "codex/hooks.json").exists())
        failure = self.cli("rollback", "codex", ok=False)
        self.assertNotIn("Traceback", failure.stderr)

    def assert_deep_receipt_skipped(self, nested_before):
        config = self.base / "codex/hooks.json"
        config.parent.mkdir()
        original = b'{"foreign":"preserve exact bytes"}\n'
        config.write_bytes(original)
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "codex")
        receipts = self.root / "transactions"
        valid = json.loads(next(receipts.glob("*.json")).read_text())
        deeply_nested = "[" * 100000 + "]" * 100000
        content = json.dumps(dict(valid, before=base64.b64encode(deeply_nested.encode()).decode())) if nested_before else deeply_nested
        (receipts / "zz-deeply-nested.json").write_text(content)
        result = self.cli("rollback", "codex")
        self.assertEqual(result["skipped_receipts"], 1)
        self.assertEqual(config.read_bytes(), original)

    def test_deeply_nested_receipt_body_does_not_block_older_valid_receipt(self):
        self.assert_deep_receipt_skipped(nested_before=False)

    def test_deeply_nested_encoded_before_does_not_block_older_valid_receipt(self):
        self.assert_deep_receipt_skipped(nested_before=True)

    def test_two_hosts_share_one_root_and_rollback_independently(self):
        first = self.cli("create", "v1")
        second = self.cli("create", "v2")
        self.cli("activate", first["release"], "codex")
        codex = self.base / "codex/hooks.json"
        codex_before = codex.read_bytes()
        self.cli("activate", second["release"], "claude")
        claude = self.base / "claude/settings.json"
        claude_before = claude.read_bytes()
        self.assertIn(first["snapshot"], codex_before.decode())
        self.assertIn(second["snapshot"], claude_before.decode())
        self.assertEqual(codex.read_bytes(), codex_before)
        self.cli("rollback", "codex")
        self.assertFalse(codex.exists())
        self.assertEqual(claude.read_bytes(), claude_before)
        self.cli("rollback", "claude")
        self.assertFalse(claude.exists())

    def test_repeated_create_and_activation_are_idempotent_and_new_config_rolls_back(self):
        first = self.cli("create", "v1")
        self.assertEqual(self.cli("create", "v1")["release"], first["release"])
        self.cli("activate", first["release"], "codex")
        self.assertTrue(self.cli("activate", first["release"], "codex")["unchanged"])
        self.cli("rollback", "codex")
        self.assertFalse((self.base / "codex/hooks.json").exists())

    def test_create_recovers_only_a_valid_snapshot_with_an_interrupted_top_seal(self):
        first = self.cli("create", "v1")
        snapshot = Path(first["snapshot"])
        snapshot.chmod(0o700)  # state left by rename before the final seal
        self.cli("validate", first["release"], ok=False)
        self.assertEqual(self.cli("create", "v1")["release"], first["release"])
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o500)
        self.assertTrue(self.cli("validate", first["release"])["valid"])
        snapshot.chmod(0o700)
        file = snapshot / "SKILL.md"
        file.chmod(0o600)
        file.write_text("corrupt")
        failure = self.cli("create", "v1", ok=False)
        self.assertIn("remove the invalid release", failure.stderr)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)

    def test_symlinked_host_config_preserves_target_mode_and_exact_rollback(self):
        target = self.base / "dotfile.json"
        original = b'{"model":"foreign"}\n'
        target.write_bytes(original)
        target.chmod(0o640)
        config = self.base / "claude/settings.json"
        config.parent.mkdir()
        config.symlink_to(target)
        release = self.cli("create", "v1")
        self.cli("activate", release["release"], "claude")
        self.assertTrue(config.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.cli("rollback", "claude")
        self.assertEqual(target.read_bytes(), original)

    def test_process_killed_at_config_replace_keeps_previous_release_and_retry_works(self):
        first = self.cli("create", "v1")
        self.cli("activate", first["release"], "codex")
        config = self.base / "codex/hooks.json"
        active = config.read_bytes()
        (self.source / "reference.md").write_text("release two")
        second = self.cli("create", "v2")
        # Inject process death at the OS publication boundary, not inside the
        # installer. The receipt must already be durable and config stay intact.
        program = """import os, runpy, sys
original = os.replace
def interrupted(source, destination):
    if os.path.realpath(destination) == os.path.realpath(os.environ['FAULT_CONFIG']):
        os._exit(73)
    return original(source, destination)
os.replace = interrupted
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.source / "install.py"),
                                 "runtime", "--root", str(self.root), "activate", second["release"], "codex"],
                                env=dict(self.env, FAULT_CONFIG=str(config)), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(config.read_bytes(), active)
        self.cli("activate", second["release"], "codex")
        self.cli("rollback", "codex")
        self.assertEqual(config.read_bytes(), active)

    def test_process_killed_after_config_replace_can_roll_back(self):
        release = self.cli("create", "v1")
        config = self.base / "codex/hooks.json"
        program = """import os, runpy, sys
original = os.replace
def interrupted(source, destination):
    result = original(source, destination)
    if os.path.realpath(destination) == os.path.realpath(os.environ['FAULT_CONFIG']):
        os._exit(73)
    return result
os.replace = interrupted
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        result = subprocess.run([sys.executable, "-c", program, str(self.source / "install.py"),
                                 "runtime", "--root", str(self.root), "activate", release["release"], "codex"],
                                env=dict(self.env, FAULT_CONFIG=str(config)), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertIn(release["snapshot"], config.read_text())
        self.cli("rollback", "codex")
        self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()
