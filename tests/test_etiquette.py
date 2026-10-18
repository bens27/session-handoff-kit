"""Exercise etiquette drift detection and safe synchronization through the CLI."""
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-etiquette"
SCHEMA = runpy.run_path(str(SCRIPT))


class EtiquetteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "_etiquette.md"
        self.directory = self.root / "etiquette"
        self.directory.mkdir()
        self.manifest = self.root / "deviations.json"
        self.manifest.write_text("{}")
        self.source.write_text("\n\n".join([
            "[TOP PRIORITY DIRECTIVE: read before anything else]\nQA rule.",
            "AgentsRoom integration test.\nThese tools wire the desktop.",
            "Shared git workspace safety:\n- Preserve colleagues' work.",
            "AgentsRoom-owned files inside the project:\n- Preserve runtime files.",
            "GOLDEN RULE: interpret intent, then act.\n- Requested actions are authorized.",
            "Per-tool specifics (the GOLDEN RULE above governs all of them):\n" + "\n".join(
                "- " + prefix + " fixture rule." for _, prefix in SCHEMA["TOOLS"].values()),
            "Guiding principle: act on the request.",
            "VISIBLE ANSWER RULE\n\nState the findings.",
        ]) + "\n")
        names = list(SCHEMA["TOOLS"]) + list(SCHEMA["BLOCKS"])
        self.index = self.directory / "INDEX.md"
        self.index.write_text(
            "# Etiquette\n\nAgentsRoom integration test.\nThese tools wire the desktop.\n"
            "Load sections just in time. Preserve this custom rule.\n\n"
            "| Section | Read when |\n|---|---|\n" + "\n".join(
                f"| [{name}]({name}.md) | When needed |" for name in names)
            + "\n\nGuiding principle: act on the request.\n")
        result = self.cli("--sync")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--source", str(self.source),
             "--directory", str(self.directory), "--deviations", str(self.manifest), *args],
            text=True, capture_output=True, check=False)

    def pin_database(self, status="pending_review"):
        path = self.directory / "databases.md"
        source_rule = path.read_text().split("\n\n", 1)[1]
        split_rule = "- Databases: writes are always refused.\n"
        path.write_text("# Databases\n\n" + split_rule)
        def fingerprint(text):
            return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
        self.manifest.write_text(json.dumps({"databases.md": {
            "status": status, "reason": "Stricter write policy requires Ben's review.",
            "source_sha256": fingerprint(source_rule), "split_sha256": fingerprint(split_rule),
        }}))
        return path

    def test_check_is_read_only_and_sync_preserves_index_and_heading(self):
        before = self.index.read_bytes()
        path = self.directory / "backlog.md"
        path.write_text("# Custom title\n\nStale policy.\n")
        stale = path.read_bytes()
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("DRIFT backlog.md", result.stdout)
        self.assertEqual(path.read_bytes(), stale)
        self.assertEqual(self.cli("--sync").returncode, 0)
        self.assertTrue(path.read_text().startswith("# Custom title\n"))
        self.assertEqual(self.index.read_bytes(), before)
        self.assertEqual(self.cli().returncode, 0)

    def test_source_update_is_detected(self):
        self.source.write_text(self.source.read_text().replace("- Backlog: fixture rule.",
                                                               "- Backlog: new progress policy."))
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn("DRIFT backlog.md", result.stdout)

    def test_pending_deviation_is_visible_and_never_overwritten(self):
        path = self.pin_database()
        before = path.read_bytes()
        result = self.cli("--sync")
        self.assertEqual(result.returncode, 1)
        self.assertIn("REVIEW databases.md", result.stdout)
        self.assertEqual(path.read_bytes(), before)

    def test_approved_deviation_passes_only_while_both_fingerprints_match(self):
        path = self.pin_database("approved")
        original = path.read_bytes()
        self.assertEqual(self.cli().returncode, 0)
        path.write_text(path.read_text().replace("always refused", "permitted"))
        self.assertIn("DRIFT databases.md", self.cli().stdout)
        path.write_bytes(original)
        self.source.write_text(self.source.read_text().replace("- Databases fixture rule.",
                                                               "- Databases new rule."))
        result = self.cli("--sync")
        self.assertEqual(result.returncode, 1)
        self.assertIn("fingerprints changed", result.stdout)

    def test_unmapped_new_source_rule_fails_before_any_writes(self):
        path = self.directory / "backlog.md"
        path.write_text("# Backlog\n\nStale.\n")
        before = path.read_bytes()
        self.source.write_text(self.source.read_text().replace(
            "- Backlog:", "- New tool: new policy.\n- Backlog:"))
        result = self.cli("--sync")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unmapped source content", result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_section_trigger_and_unmapped_section_are_reported(self):
        (self.directory / "backlog.md").unlink()
        self.index.write_text(self.index.read_text().replace("(backlog.md)", "(wrong.md)"))
        (self.directory / "obsolete.md").write_text("# Old\n\nOld rule.\n")
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        for finding in ("DRIFT backlog.md: missing", "INDEX backlog.md", "UNMAPPED obsolete.md"):
            self.assertIn(finding, result.stdout)

    def test_duplicate_anchor_and_invalid_manifest_are_errors(self):
        original = self.source.read_text()
        self.source.write_text(original.replace("- Backlog:", "- Backlog: duplicate.\n- Backlog:"))
        self.assertEqual(self.cli("--sync").returncode, 2)
        self.source.write_text(original)
        self.manifest.write_text('{"databases.md": {"status": "approved"}}')
        self.assertEqual(self.cli("--sync").returncode, 2)

    def test_wrapping_does_not_create_drift(self):
        path = self.directory / "backlog.md"
        path.write_text(path.read_text().replace("fixture rule.", "fixture\n  rule."))
        self.assertEqual(self.cli().returncode, 0)


if __name__ == "__main__":
    unittest.main()
