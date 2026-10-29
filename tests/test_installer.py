"""Installer tests use local fixtures and mocked HTTPS; never download live code."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install as installer
import shellclaude as app

COMMIT = "a" * 40


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "app"
        self.target.mkdir()
        root = Path(installer.__file__).resolve().parent
        self.bodies = {name: (root / name).read_bytes() for name in installer.FILES}
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if url.startswith("https://api.github.com/"):
            return json.dumps({"sha": COMMIT}).encode()
        prefix = f"https://raw.githubusercontent.com/{installer.REPOSITORY}/{COMMIT}/"
        self.assertTrue(url.startswith(prefix), url)
        return self.bodies[url[len(prefix):]]

    def run_install(self):
        with patch.object(installer, "download", side_effect=self.fetch):
            return installer.install(self.target)

    def test_installs_all_files_from_one_pinned_commit(self):
        self.assertEqual(self.run_install(), COMMIT)
        for name, body in self.bodies.items():
            self.assertEqual((self.target / name).read_bytes(), body)
        self.assertEqual(len(self.urls), len(installer.FILES) + 1)
        self.assertIn("feature%2Fchatgpt-sign-in", self.urls[0])

    def test_update_preserves_settings_sessions_and_custom_agents(self):
        data = self.target / "shellclaude-data"
        data.mkdir()
        protected = {"shellclaude.json": b'{"model":"fixture"}', "shellclaude.db": b"fixture-db",
                     "AGENTS.md": b"user customized guidance", "allowlist.txt": b"pwd\n"}
        for name, body in protected.items():
            (data / name).write_bytes(body)
        self.run_install()
        for name, body in protected.items():
            self.assertEqual((data / name).read_bytes(), body)

    def test_download_failure_does_not_replace_old_program(self):
        old = self.target / "shellclaude.py"
        old.write_text("old program")
        def failing(url):
            if url.endswith("shellclaude_responses.py"):
                raise RuntimeError("fixture network failure")
            return self.fetch(url)
        with patch.object(installer, "download", side_effect=failing):
            with self.assertRaises(RuntimeError):
                installer.install(self.target)
        self.assertEqual(old.read_text(), "old program")
        self.assertFalse((self.target / "shellclaude_auth.py").exists())

    def test_syntax_error_is_detected_before_any_replacement(self):
        old = self.target / "shellclaude.py"
        old.write_text("old program")
        self.bodies["shellclaude_auth.py"] = b"def broken("
        with self.assertRaises(SyntaxError):
            self.run_install()
        self.assertEqual(old.read_text(), "old program")

    def test_replacement_failure_rolls_back_existing_and_new_files(self):
        old = self.target / "shellclaude.py"
        old.write_text("old program")
        old.chmod(0o700)
        original = installer.publish
        calls = 0
        def fail_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("fixture disk failure")
            return original(source, target)
        with patch.object(installer, "publish", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.run_install()
        self.assertEqual(old.read_text(), "old program")
        self.assertEqual(old.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.target / "shellclaude_auth.py").exists())
        self.assertFalse(list(self.target.glob(".shellclaude-update-*")))

    def test_external_symlink_target_is_rejected(self):
        external = Path(self.temp.name) / "external.py"
        external.write_text("keep")
        (self.target / "shellclaude.py").symlink_to(external)
        with self.assertRaises(RuntimeError):
            self.run_install()
        self.assertEqual(external.read_text(), "keep")

    def test_invalid_commit_is_rejected(self):
        with patch.object(installer, "download", return_value=b'{"sha":"../invalid"}'):
            with self.assertRaises(RuntimeError):
                installer.resolve_commit("main")

    def test_invalid_history_is_rejected(self):
        self.bodies["defaults/agents-history.json"] = b'{"wrong":"shape"}'
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse((self.target / "shellclaude.py").exists())

    def test_rules_are_not_literal_text_in_main_source(self):
        source = Path(app.__file__).read_text()
        self.assertNotIn(app.DEFAULT_PLATFORM_AGENTS, source)
        self.assertEqual(app.DEFAULT_PLATFORM_AGENTS, Path(app.BUNDLED_PLATFORM_AGENTS_PATH).read_text())

    def test_identical_default_is_not_rewritten_on_every_start(self):
        path = self.target / "AGENTS.md"
        path.write_text(app.DEFAULT_PLATFORM_AGENTS)
        with patch.object(app, "PLATFORM_AGENTS_PATH", str(path)), patch.object(app.os, "replace") as replace:
            app._upgrade_platform_default()
        replace.assert_not_called()

    def test_missing_distribution_and_custom_rules_give_install_instruction(self):
        with patch.object(app, "DEFAULT_PLATFORM_AGENTS", ""), patch.object(app, "PLATFORM_AGENTS_PATH", str(self.target / "missing.md")):
            with self.assertRaisesRegex(RuntimeError, "install.py"):
                app.load_platform_agents(create=True)


if __name__ == "__main__":
    unittest.main()
