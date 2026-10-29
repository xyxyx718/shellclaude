import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import shellclaude as app


class BypassTests(unittest.TestCase):
    def test_approval_not_read_and_not_persisted(self):
        with patch.object(app, '_BYPASS_MODE', True), patch('builtins.input') as read:
            self.assertEqual(app.ask_permission('write file', 'fixture'), 'y')
            read.assert_not_called()

    def test_blocked_shell_expression_runs_only_in_bypass(self):
        with patch.object(app, '_exec_shell', return_value=('ok', 0)) as run, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(app, '_BYPASS_MODE', False):
                self.assertIn('BLOCKED', app.tool_run_command('ls | cat'))
                run.assert_not_called()
            with patch.object(app, '_BYPASS_MODE', True), patch('builtins.input') as read:
                self.assertEqual(app.tool_run_command('ls | cat'), 'ok')
                run.assert_called_once()
                read.assert_not_called()

    def test_safe_mode_dispatch_does_not_prompt_in_bypass(self):
        with patch.object(app, '_BYPASS_MODE', True), patch.object(app, '_SAFE_MODE', True), patch.object(app, 'TOOL_MAP', {'fixture': lambda args: 'ok'}), patch('builtins.input') as read:
            self.assertEqual(app.dispatch_tool('fixture', '{}'), 'ok')
            read.assert_not_called()

    def test_internal_configuration_stays_protected(self):
        with patch.object(app, '_BYPASS_MODE', True):
            self.assertTrue(app._is_sensitive_path(app.CFG_PATH)[0])
            self.assertIn('ERROR', app.tool_read_file(app.CFG_PATH))
            self.assertIn('ERROR', app.tool_write_file(app.CFG_PATH, 'changed'))
            self.assertIn('ERROR', app.tool_patch_file(app.CFG_PATH, 'a', 'b'))
            self.assertFalse(app._is_sensitive_path('.env')[0])

    def test_url_and_compiler_filters_bypassed(self):
        with patch.object(app, '_BYPASS_MODE', True):
            self.assertTrue(app._is_safe_url('http://127.0.0.1:8000')[0])
            self.assertFalse(app._is_safe_url('file:///etc/passwd')[0])
            self.assertEqual(app._sanitize_clang_flags('-include fixture.h'), ['-include', 'fixture.h'])
