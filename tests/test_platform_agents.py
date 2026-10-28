"""Bundled a-Shell guidance must survive project rules and custom prompts."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import shellclaude as app


class PlatformAgentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.platform = self.root / "shellclaude-data" / "AGENTS.md"
        for name, value in (("PLATFORM_AGENTS_PATH", str(self.platform)),
                            ("PLATFORM_AGENTS_RULES", app.DEFAULT_PLATFORM_AGENTS),
                            ("AGENTS_MD_PATH", None), ("SYSTEM", app.DEFAULT_SYSTEM),
                            ("SKILLS_REGISTRY", {})):
            p = patch.object(app, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_embedded_rules_match_reviewable_bundled_document(self):
        bundled = Path(app.__file__).parent / "defaults" / "AGENTS.md"
        self.assertEqual(bundled.read_text(), app.DEFAULT_PLATFORM_AGENTS)

    def test_creates_default_once_and_keeps_manual_edits(self):
        app.load_platform_agents(create=True)
        self.assertEqual(self.platform.read_text(), app.DEFAULT_PLATFORM_AGENTS)
        self.assertEqual(self.platform.stat().st_mode & 0o777, 0o600)
        self.platform.write_text("平台规则：自定义 Python 测试要求")
        app.load_platform_agents(create=True)
        self.assertEqual(self.platform.read_text(), "平台规则：自定义 Python 测试要求")
        self.assertIn("自定义 Python", app._sys_prompt(app.DEFAULT_CFG))

    def test_missing_empty_or_invalid_file_uses_embedded_rules(self):
        self.assertEqual(app.load_platform_agents(), app.DEFAULT_PLATFORM_AGENTS)
        self.platform.parent.mkdir()
        for content in (b"", b"\xff"):
            self.platform.write_bytes(content)
            self.assertEqual(app.load_platform_agents(), app.DEFAULT_PLATFORM_AGENTS)

    def test_creation_failure_does_not_disable_guidance(self):
        with patch.object(app.os, "open", side_effect=OSError("fixture")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.load_platform_agents(create=True), app.DEFAULT_PLATFORM_AGENTS)

    def test_project_rules_and_platform_rules_both_reach_prompt(self):
        project = self.root / "AGENTS.md"
        project.write_text("Project-specific style: fixture-style")
        with patch.object(app, "find_agents_md", return_value=str(project)), contextlib.redirect_stdout(io.StringIO()):
            app.load_agents_md(app.DEFAULT_CFG)
        prompt = app._sys_prompt(app.DEFAULT_CFG)
        self.assertIn("fixture-style", prompt)
        self.assertIn(app.DEFAULT_PLATFORM_AGENTS, prompt)
        self.assertEqual(prompt.count("[Platform AGENTS.md — a-Shell runtime guidance]"), 1)

    def test_custom_system_or_persona_does_not_remove_platform_guidance(self):
        with patch.object(app, "SYSTEM", "Custom persona fixture"):
            prompt = app._sys_prompt({**app.DEFAULT_CFG, "system": "Custom persona fixture"})
        self.assertIn("Custom persona fixture", prompt)
        self.assertIn(app.DEFAULT_PLATFORM_AGENTS, prompt)
        self.assertIn(app.DATA_DIR, prompt)
        self.assertIn(os.getcwd(), prompt)

    def test_reload_reads_edits_at_same_project_path(self):
        project = self.root / "AGENTS.md"
        project.write_text("old project rules")
        with patch.object(app, "find_agents_md", return_value=str(project)), contextlib.redirect_stdout(io.StringIO()):
            app.load_agents_md(app.DEFAULT_CFG)
            project.write_text("updated project rules")
            app.load_agents_md(app.DEFAULT_CFG, force=True)
        self.assertIn("updated project rules", app.SYSTEM)
        self.assertNotIn("old project rules", app.SYSTEM)

    def test_platform_file_is_not_mistaken_for_project_rules(self):
        app.load_platform_agents(create=True)
        parent_project = self.root / "AGENTS.md"
        parent_project.write_text("project fixture")
        self.assertEqual(app.find_agents_md(str(self.platform.parent)), str(parent_project))

    def test_oversized_local_platform_file_is_bounded(self):
        self.platform.parent.mkdir()
        self.platform.write_text("line\n" * 3000)
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.load_platform_agents()
        self.assertLessEqual(len(result), app._AGENTS_MD_MAX_CHARS)

    def test_all_providers_receive_platform_guidance(self):
        cfg = {**app.DEFAULT_CFG, "model": "fixture", "api_key": "fixture-key"}
        for build in (app._build_payload_openai, app._build_payload_anthropic):
            payload = build(cfg, [{"role": "user", "content": "fixture"}])
            instructions = payload.get("system") or payload["messages"][0]["content"]
            self.assertIn(app.DEFAULT_PLATFORM_AGENTS, instructions)
        provider = Mock()
        with patch.object(app, "_chatgpt_provider", return_value=provider):
            app._chatgpt_call(cfg, [])
        self.assertIn(app.DEFAULT_PLATFORM_AGENTS, provider.call.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
