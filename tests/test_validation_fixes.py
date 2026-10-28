"""Regressions for findings in the user-supplied a-Shell hardware audit."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import shellclaude as app


class PlatformUpgradeTests(unittest.TestCase):
    def test_known_default_upgrades_but_custom_edits_survive(self):
        previous = "fixture old bundled rules\n"
        digest = hashlib.sha256(previous.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as d, patch.object(app, "PLATFORM_AGENTS_PATH", os.path.join(d, "AGENTS.md")), patch.object(app, "_PREVIOUS_PLATFORM_DEFAULT_HASHES", {digest}), patch.object(app, "PLATFORM_AGENTS_RULES", app.DEFAULT_PLATFORM_AGENTS), contextlib.redirect_stdout(io.StringIO()):
            path = Path(app.PLATFORM_AGENTS_PATH)
            path.write_text(previous)
            app.load_platform_agents(create=True)
            self.assertEqual(path.read_text(), app.DEFAULT_PLATFORM_AGENTS)
            customized = previous + "manually added requirement\n"
            path.write_text(customized)
            app.load_platform_agents(create=True)
            self.assertEqual(path.read_text(), customized)
            self.assertIn("manually added", app.PLATFORM_AGENTS_RULES)

    def test_failed_upgrade_keeps_previous_file_and_removes_staging(self):
        previous = b"fixture old rules"
        digest = hashlib.sha256(previous).hexdigest()
        with tempfile.TemporaryDirectory() as d, patch.object(app, "PLATFORM_AGENTS_PATH", os.path.join(d, "AGENTS.md")), patch.object(app, "_PREVIOUS_PLATFORM_DEFAULT_HASHES", {digest}):
            path = Path(app.PLATFORM_AGENTS_PATH)
            path.write_bytes(previous)
            with patch.object(app.os, "replace", side_effect=OSError("fixture")):
                with self.assertRaises(OSError):
                    app._upgrade_platform_default()
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(d).glob(".agents-*")), [])


class PatchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_protected_patch_is_rejected_before_read_diff_or_backup(self):
        path = self.root / "protected" / "config.json"
        path.parent.mkdir()
        path.write_text('{"fixture":"secret"}')
        with patch.object(app, "PROTECTED_PATHS", {str(path.parent.resolve())}), patch("builtins.open") as read, patch.object(app, "show_diff") as diff, patch.object(app, "ask_permission") as ask:
            result = app.tool_patch_file(str(path), "fixture", "changed")
        self.assertIn("blocked", result)
        read.assert_not_called()
        diff.assert_not_called()
        ask.assert_not_called()
        self.assertFalse(Path(str(path) + ".bak").exists())
        self.assertIn("fixture", path.read_text())

    def test_sensitive_file_patch_is_rejected_before_read(self):
        path = self.root / ".env"
        path.write_text("fixture=private")
        with patch("builtins.open") as read:
            result = app.tool_patch_file(str(path), "private", "changed")
        self.assertIn("blocked", result)
        read.assert_not_called()

    def test_symlink_to_protected_file_is_rejected(self):
        protected = self.root / "protected.txt"
        protected.write_text("original")
        link = self.root / "link.txt"
        link.symlink_to(protected)
        with patch.object(app, "PROTECTED_PATHS", {str(protected.resolve())}):
            self.assertIn("blocked", app.tool_patch_file(str(link), "original", "changed"))
        self.assertEqual(protected.read_text(), "original")

    def test_denied_patch_makes_no_changes_or_backup(self):
        path = self.root / "sample.txt"
        path.write_text("before")
        with patch.object(app, "ALLOWLIST", []), patch.object(app, "_DIFF_MODE", False), patch.object(app, "ask_permission", return_value="n") as ask, contextlib.redirect_stdout(io.StringIO()):
            result = app.tool_patch_file(str(path), "before", "after")
        self.assertIn("DENIED", result)
        ask.assert_called_once_with("patch file", str(path))
        self.assertEqual(path.read_text(), "before")
        self.assertFalse(Path(str(path) + ".bak").exists())

    def test_approved_patch_still_edits_and_preserves_backup(self):
        path = self.root / "sample.txt"
        path.write_text("before")
        with patch.object(app, "ALLOWLIST", []), patch.object(app, "_DIFF_MODE", False), patch.object(app, "ask_permission", return_value="y"), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.tool_patch_file(str(path), "before", "after").startswith("OK:"))
        self.assertEqual(path.read_text(), "after")
        self.assertEqual(Path(str(path) + ".bak").read_text(), "before")


class CompilerResultTests(unittest.TestCase):
    def test_sdk_notice_with_zero_exit_and_empty_target_does_not_run_or_chmod(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "TOOL_TMP_DIR", d), patch.object(app, "_exec_argv", return_value=("Install/update SDK first", 0)), patch.object(app.os, "chmod") as chmod, patch.object(app, "_exec_shell") as run:
            result = app.tool_run_c("int main(void) { return 0; }")
            self.assertEqual(os.listdir(d), [])
        self.assertIn("COMPILE ERROR", result)
        self.assertIn("SDK", result)
        chmod.assert_not_called()
        run.assert_not_called()

    def compile_fixture(self, argv, timeout):
        Path(argv[argv.index("-o") + 1]).write_bytes(b"fixture-target")
        return "", 0

    def test_runtime_error_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "TOOL_TMP_DIR", d), patch.object(app, "_exec_argv", side_effect=self.compile_fixture), patch.object(app.os, "chmod"), patch.object(app, "_exec_shell", return_value=("target unavailable", 127)):
            result = app.tool_run_c("int main(void) { return 0; }")
        self.assertIn("RUN ERROR (exit 127)", result)
        self.assertNotIn("exit 0", result)

    def test_successful_target_result_preserved(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "TOOL_TMP_DIR", d), patch.object(app, "_exec_argv", side_effect=self.compile_fixture), patch.object(app.os, "chmod"), patch.object(app, "_exec_shell", return_value=("fixture-success", 0)):
            self.assertEqual(app.tool_run_cpp("int main() { return 0; }"), "fixture-success")


if __name__ == "__main__":
    unittest.main()
