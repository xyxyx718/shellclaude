"""Installer tests use local fixtures and mocked HTTPS; never download live code."""
import contextlib
import io
import json
import hashlib
import os
from pathlib import Path
import tempfile
import subprocess
import sys
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
        prefix = f"https://raw.githubusercontent.com/{installer.REPOSITORY}/{installer.DEFAULT_REF}/"
        self.assertTrue(url.startswith(prefix), url)
        return self.bodies[url[len(prefix):]]

    def run_install(self):
        with patch.object(installer, "download", side_effect=self.fetch):
            return installer.install(self.target)

    def test_installs_all_files_from_one_pinned_commit(self):
        self.assertEqual(self.run_install(), installer.DEFAULT_REF)
        for name, body in self.bodies.items():
            self.assertEqual((self.target / name).read_bytes(), body)
        self.assertEqual(len(self.urls), len(self.bodies))
        self.assertIn("raw.githubusercontent.com", self.urls[0])
        self.assertFalse(any("api.github.com" in url for url in self.urls))

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
            if url.endswith("shellclaude_lib/responses.py"):
                raise RuntimeError("fixture network failure")
            return self.fetch(url)
        with patch.object(installer, "download", side_effect=failing):
            with self.assertRaises(RuntimeError):
                installer.install(self.target)
        self.assertEqual(old.read_text(), "old program")
        self.assertFalse((self.target / "shellclaude_lib/auth.py").exists())

    def test_syntax_error_is_detected_before_any_replacement(self):
        old = self.target / "shellclaude.py"
        old.write_text("old program")
        self.bodies["shellclaude_lib/auth.py"] = b"def broken("
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
        self.assertFalse((self.target / "shellclaude_lib/auth.py").exists())
        self.assertFalse(list(self.target.glob(".shellclaude-update-*")))

    def test_external_symlink_target_is_rejected(self):
        external = Path(self.temp.name) / "external.py"
        external.write_text("keep")
        (self.target / "shellclaude.py").symlink_to(external)
        with self.assertRaises(RuntimeError):
            self.run_install()
        self.assertEqual(external.read_text(), "keep")

    def test_fixed_file_list_never_targets_user_data(self):
        for path in installer.FILES:
            self.assertNotIn("..", Path(path).parts)
            self.assertFalse(path.startswith("/"))
            self.assertFalse(path.startswith("shellclaude-data/"))

    def test_invalid_history_is_rejected(self):
        self.bodies["defaults/agents-history.json"] = b'{"wrong":"shape"}'
        with self.assertRaises(ValueError):
            self.run_install()
        self.assertFalse((self.target / "shellclaude.py").exists())

    def test_stock_legacy_modules_removed_and_edits_kept(self):
        auth_body = self.bodies["shellclaude_lib/auth.py"]
        (self.target / "shellclaude_auth.py").write_bytes(auth_body)
        (self.target / "shellclaude_responses.py").write_text("# custom legacy module")
        self.run_install()
        self.assertFalse((self.target / "shellclaude_auth.py").exists())
        self.assertEqual((self.target / "shellclaude_responses.py").read_text(), "# custom legacy module")

    def test_shell_bootstrap_downloads_and_runs_installer(self):
        root = Path(installer.__file__).resolve().parent
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text("#!" + sys.executable + "\n" +
            "import sys\nfrom pathlib import Path\n" +
            "assert 'api.github.com' not in ' '.join(sys.argv)\n" +
            "assert sys.argv[-2:] == ['-o', 'install.py']\n" +
            "Path('install.py').write_text(\"from pathlib import Path\\nPath('executed').write_text('ok')\\n\")\n")
        curl.chmod(0o700)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], PYTHON=sys.executable)
        result = subprocess.run(['sh', str(root / 'install.sh')], cwd=self.target,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.target / 'executed').read_text(), 'ok')
        # Failed bootstrap download must not launch Python.
        (self.target / 'executed').unlink()
        curl.write_text('#!/bin/sh\nexit 22\n')
        result = subprocess.run(['sh', str(root / 'install.sh')], cwd=self.target,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.target / 'executed').exists())

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
