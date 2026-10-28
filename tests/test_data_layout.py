"""Offline upgrades from both legacy layouts without losing SQLite WAL data."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import shellclaude as app


class DataMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.documents = Path(self.temp.name, "Documents")
        self.documents.mkdir()
        self.target = Path(self.temp.name, "app", "shellclaude-data")

    def migrate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.migrate_legacy_data(str(self.target), str(self.documents))

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_imports_root_config_without_removing_original(self):
        old = self.documents / "shellclaude.json"
        self.write(old, '{"model":"fixture","api_key":"fixture-key"}')
        self.migrate()
        self.assertEqual((self.target / old.name).read_text(), old.read_text())
        self.assertEqual((self.target / old.name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)

    def test_also_supports_older_nested_config(self):
        old = self.documents / "shellclaude" / "shellclaude.json"
        self.write(old, '{"model":"older-layout"}')
        self.migrate()
        self.assertEqual(json.loads((self.target / old.name).read_text())["model"], "older-layout")

    def test_current_legacy_layout_takes_precedence(self):
        self.write(self.documents / "shellclaude.json", '{"model":"current"}')
        self.write(self.documents / "shellclaude" / "shellclaude.json", '{"model":"older"}')
        self.migrate()
        self.assertEqual(json.loads((self.target / "shellclaude.json").read_text())["model"], "current")

    def test_existing_destination_is_never_overwritten(self):
        self.write(self.documents / "shellclaude.json", '{"model":"legacy"}')
        self.write(self.target / "shellclaude.json", '{"model":"new"}')
        self.migrate()
        self.assertEqual(json.loads((self.target / "shellclaude.json").read_text())["model"], "new")

    def test_import_runs_once_even_if_old_config_changes(self):
        old = self.documents / "shellclaude.json"
        self.write(old, '{"model":"first"}')
        self.migrate()
        self.write(old, '{"model":"later"}')
        self.migrate()
        self.assertEqual(json.loads((self.target / old.name).read_text())["model"], "first")
        # Intentional deletion after a completed migration must not resurrect old settings.
        (self.target / old.name).unlink()
        self.migrate()
        self.assertFalse((self.target / old.name).exists())

    def test_database_backup_includes_committed_uncheckpointed_wal(self):
        old = self.documents / "shellclaude.db"
        source = sqlite3.connect(old)
        self.addCleanup(source.close)
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE fixture (value TEXT)")
        source.execute("INSERT INTO fixture VALUES ('wal-history')")
        source.commit()
        self.assertTrue(Path(str(old) + "-wal").exists())
        self.migrate()
        with contextlib.closing(sqlite3.connect(self.target / old.name)) as copied:
            self.assertEqual(copied.execute("SELECT value FROM fixture").fetchone()[0], "wal-history")
        self.assertEqual(source.execute("SELECT value FROM fixture").fetchone()[0], "wal-history")

    def test_allowlist_and_customization_directories_are_preserved(self):
        old = self.documents / "shellclaude"
        self.write(old / "allowlist.txt", "pwd\n")
        for directory in ("plugins", "skills", "snippets", "personas"):
            self.write(old / directory / "fixture.txt", directory)
        self.migrate()
        self.assertEqual((self.target / "allowlist.txt").read_text(), "pwd\n")
        for directory in ("plugins", "skills", "snippets", "personas"):
            self.assertEqual((self.target / directory / "fixture.txt").read_text(), directory)
            self.assertTrue((old / directory / "fixture.txt").exists())

    def test_existing_customization_directory_is_not_merged_or_replaced(self):
        self.write(self.documents / "shellclaude" / "plugins" / "old.py", "old")
        self.write(self.target / "plugins" / "new.py", "new")
        self.migrate()
        self.assertFalse((self.target / "plugins" / "old.py").exists())
        self.assertEqual((self.target / "plugins" / "new.py").read_text(), "new")

    def test_failed_copy_does_not_publish_partial_config_and_can_retry(self):
        old = self.documents / "shellclaude.json"
        self.write(old, '{"model":"fixture"}')
        with patch.object(app.shutil, "copyfileobj", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertFalse((self.target / old.name).exists())
        self.assertFalse(list(self.target.glob(".migrating-*")))
        self.migrate()
        self.assertEqual((self.target / old.name).read_text(), old.read_text())

    def test_corrupt_legacy_config_is_not_silently_replaced_with_defaults(self):
        old = self.documents / "shellclaude.json"
        self.write(old, "{invalid")
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertFalse((self.target / old.name).exists())
        self.assertEqual(old.read_text(), "{invalid")

    def test_unrelated_current_project_files_are_not_imported(self):
        project = Path(self.temp.name, "project")
        self.write(project / "shellclaude.json", '{"model":"unrelated"}')
        before = os.getcwd()
        try:
            os.chdir(project)
            self.migrate()
        finally:
            os.chdir(before)
        self.assertFalse((self.target / "shellclaude.json").exists())

    def test_default_paths_are_anchored_to_script_not_working_directory(self):
        self.assertEqual(app.DATA_DIR, str(Path(app.__file__).resolve().parent / "shellclaude-data"))
        for name in ("CFG_PATH", "DB_PATH", "ALLOWLIST_PATH", "PLUGINS_DIR", "SKILLS_DIR", "SNIPPETS_DIR", "PERSONAS_DIR", "EXPORTS_DIR", "TOOL_TMP_DIR"):
            self.assertEqual(os.path.dirname(getattr(app, name)), app.DATA_DIR)

    def test_default_export_uses_data_subdirectory(self):
        export_dir = str(self.target / "exports")
        with patch.object(app, "EXPORTS_DIR", export_dir), contextlib.redirect_stdout(io.StringIO()):
            app.cmd_export([{"role": "user", "content": "fixture"}], 1, "example")
        self.assertTrue((Path(export_dir) / "example.md").exists())

    def test_legacy_credentials_remain_blocked_from_model_file_reads(self):
        for name in ("~/Documents/shellclaude.json", "~/Documents/shellclaude/shellclaude.json"):
            self.assertTrue(app._is_sensitive_path(name)[0])


if __name__ == "__main__":
    unittest.main()
