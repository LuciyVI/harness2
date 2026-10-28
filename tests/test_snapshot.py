"""Git-aware workspace selection, limits and retained failure diagnostics."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.snapshot import SnapshotError, plan_snapshot, snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args], check=True, capture_output=True
        )

    @unittest.skipUnless(shutil.which("git"), "Git metadata test requires git")
    def test_git_selection_keeps_tracked_and_new_inputs_but_omits_ignored_artifacts(self):
        self.git("init", "-q")
        (self.project / ".gitignore").write_text("cache/\n*.beam\n.env\n")
        (self.project / "app.py").write_text("print(1)\n")
        (self.project / "keep.beam").write_text("tracked input")
        (self.project / ".env").write_text("DUMMY_TOKEN=do-not-copy")
        self.git("add", "-f", "app.py", "keep.beam", ".env")
        (self.project / "new-input.txt").write_text("new input")
        (self.project / "ignored.beam").write_text("generated")
        (self.project / "cache").mkdir()
        with (self.project / "cache/large.bin").open("wb") as stream:
            stream.truncate(100_000_001)
        copied = self.root / "copy"
        report = snapshot(self.project, copied)
        self.assertEqual(report["selection"], "git-tracked-and-untracked")
        self.assertEqual(
            {p.name for p in copied.iterdir()},
            {"app.py", "keep.beam", ".gitignore", "new-input.txt"},
        )
        self.assertEqual(report["exclusion_reasons"]["cache"], "git-ignore")
        self.assertIn(".env", report["excluded"])
        self.assertLess(report["bytes"], 1000)

    @unittest.skipUnless(shutil.which("git"), "Git metadata test requires git")
    def test_gitlinks_are_excluded_before_traversal_or_copy(self):
        self.git("init", "-q")
        (self.project / "app.txt").write_text("parent")
        private = self.project / "vendor"
        private.mkdir()
        (private / "must-not-read.txt").write_text("submodule content")
        self.git("update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",vendor")
        original = shutil.copy2

        def guarded_copy(source, target, **kwargs):
            self.assertFalse(Path(source).is_relative_to(private))
            return original(source, target, **kwargs)

        with patch("harness2.snapshot.shutil.copy2", side_effect=guarded_copy):
            report = snapshot(self.project, self.root / "copy")
        self.assertEqual(report["git_submodules"], ["vendor"])
        self.assertEqual(report["exclusion_reasons"]["vendor"], "git-submodule")
        self.assertFalse((self.root / "copy/vendor").exists())
        self.assertEqual((self.root / "copy/app.txt").read_text(), "parent")

    @unittest.skipUnless(shutil.which("git"), "Git metadata test requires git")
    def test_workspace_subdirectory_does_not_select_parent_repository_files(self):
        self.git("init", "-q")
        (self.project / "outside.txt").write_text("not selected")
        child = self.project / "child"
        child.mkdir()
        (child / "inside.txt").write_text("selected")
        report = snapshot(child, self.root / "copy")
        self.assertEqual(report["files"], 1)
        self.assertEqual((self.root / "copy/inside.txt").read_text(), "selected")
        self.assertFalse((self.root / "copy/outside.txt").exists())

    @unittest.skipUnless(shutil.which("git"), "Git metadata test requires git")
    def test_untracked_nested_repository_is_not_traversed(self):
        self.git("init", "-q")
        (self.project / "app").write_text("parent")
        nested = self.project / "reference"
        nested.mkdir()
        subprocess.run(["git", "-C", str(nested), "init", "-q"], check=True, capture_output=True)
        (nested / "large").write_bytes(b"0123456789")
        report = snapshot(self.project, self.root / "copy", max_bytes=6)
        self.assertEqual(report["exclusion_reasons"]["reference"], "nested-repository")
        self.assertFalse((self.root / "copy/reference").exists())

    def test_limits_report_the_individual_metric_and_offending_path(self):
        (self.project / "a").write_bytes(b"12345")
        (self.project / "b").write_bytes(b"6")
        for options, reason, value in (
            ({"max_bytes": 4}, "max-bytes", 5),
            ({"max_files": 1}, "max-files", 2),
        ):
            with self.subTest(reason=reason), self.assertRaises(SnapshotError) as captured:
                plan_snapshot(self.project, self.root / "copy", **options)
            report = captured.exception.manifest
            self.assertEqual(report["reason"], reason)
            self.assertFalse(report["complete"])
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["bytes"] if reason == "max-bytes" else report["files"], value)
            self.assertTrue(report["offending_path"])
            self.assertEqual(report["largest_files"][0], {"path": "a", "bytes": 5})
        self.assertFalse((self.root / "copy").exists())

    def test_explicit_exclusions_and_limit_overrides_work_without_git(self):
        (self.project / "assets").mkdir()
        (self.project / "assets/large").write_bytes(b"0123456789")
        (self.project / "app").write_bytes(b"ok")
        report = snapshot(self.project, self.root / "small", max_bytes=2, exclude=("assets/**",))
        self.assertEqual(report["bytes"], 2)
        self.assertEqual(report["exclusion_reasons"]["assets"], "configured-exclusion")
        expanded = snapshot(self.project, self.root / "expanded", max_bytes=12)
        self.assertEqual(expanded["bytes"], 12)
        self.assertEqual((self.root / "expanded/assets/large").read_bytes(), b"0123456789")

    def test_missing_git_cannot_fall_back_to_copying_an_existing_repository(self):
        (self.project / ".git").mkdir()
        with patch("harness2.snapshot.shutil.which", return_value=None):
            with self.assertRaises(SnapshotError) as captured:
                plan_snapshot(self.project, self.root / "copy")
        self.assertEqual(captured.exception.manifest["reason"], "git-metadata")

    def test_worker_retains_snapshot_report_when_planning_exceeds_a_limit(self):
        (self.project / "large.txt").write_text("too large")
        output = self.root / "failed"
        request = self.root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "workspace": str(self.project),
                    "output": str(output),
                    "prompt": "task",
                    "snapshot": {"max_bytes": 2},
                }
            )
        )
        result = subprocess.run(
            [sys.executable, "-m", "harness2.cli.dsh_task", "--request", str(request)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        status = json.loads((output / "result.json").read_text())
        report = json.loads((output / "snapshot.json").read_text())
        self.assertEqual(status["status"], "snapshot-failed")
        self.assertEqual(report["reason"], "max-bytes")
        self.assertEqual(report["offending_path"], "large.txt")
        self.assertIn(str(output / "snapshot.json"), status["error"])

    def test_invalid_limits_and_exclusions_fail_before_copy(self):
        for options in ({"max_bytes": 0}, {"max_files": True}, {"exclude": ["../private"]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                snapshot(self.project, self.root / "copy", **options)
        self.assertFalse((self.root / "copy").exists())


if __name__ == "__main__":
    unittest.main()
