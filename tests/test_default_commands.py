import contextlib
import io
import unittest
from unittest.mock import patch
import shellclaude as app


class DefaultCommandTests(unittest.TestCase):
    def test_common_inspection_commands(self):
        for command in ('ls', 'ls -lah .', 'pwd', 'du -sh .', 'wc -l README.md', 'cat README.md', 'head -n 20 README.md', 'tail -n 10 README.md', 'uname -a', 'which python', 'stat README.md'):
            with self.subTest(command=command):
                self.assertTrue(app.is_default_command_allowed(command))

    def test_shell_expressions_and_execution_still_need_permission(self):
        for command in ('ls > output', 'ls && rm x', 'ls; echo x', 'ls\npython x', 'ls $(python x)', 'ls `python x`', 'ls | cat', 'ls &', 'ls *.txt', 'python script.py', 'rm x', 'rg --pre python x', 'tail -f x', 'tail -nf 10 x', 'cat "unterminated'):
            with self.subTest(command=command):
                self.assertFalse(app.is_default_command_allowed(command))

    def test_credentials_are_not_default_allowed(self):
        for path in (app.CFG_PATH, app.DB_PATH, '.env', '~/.ssh/id_rsa'):
            self.assertFalse(app.is_default_command_allowed('cat ' + app.shlex.quote(path)))

    def test_dispatch_skips_permission_for_ls_only(self):
        with patch.object(app, '_SAFE_MODE', False), patch.object(app, '_exec_shell', return_value=('files', 0)) as execute, patch.object(app, 'ask_permission', return_value='n') as ask, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.tool_run_command('ls -la'), 'files')
            ask.assert_not_called()
            self.assertIn('DENIED', app.tool_run_command('python demo.py'))
            ask.assert_called_once()
            execute.assert_called_once()

    def test_safe_mode_still_confirms_default_commands(self):
        with patch.object(app, '_SAFE_MODE', True), patch.object(app, 'ask_permission', return_value='n') as ask:
            self.assertIn('DENIED', app.dispatch_tool('run_command', '{"cmd":"ls"}'))
            ask.assert_called_once()
