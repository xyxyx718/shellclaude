"""Compact presentation must leave full tool results intact."""
import contextlib
import io
import unittest
from unittest.mock import patch
import shellclaude as app


class InteractionTests(unittest.TestCase):
    def setUp(self):
        app._TOOL_DETAILS.clear()

    def test_summary_handles_invalid_and_nonobject_json(self):
        for arguments in ('{', '[]', 'null'):
            self.assertEqual(app.tool_summary('read_file', arguments), '读取文件')
        self.assertEqual(app.tool_summary('run_command', '{"cmd":"ls\\n-a"}'), '执行命令：ls -a')

    def test_long_output_can_be_retrieved_without_truncation(self):
        result = '\n'.join(str(i) for i in range(200))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            app.show_tool_result('read_file', '{"path":"a.txt"}', result)
            compact = output.getvalue()
            self.assertNotIn('199', compact)
            app.show_tool_details('1')
        self.assertIn(result, output.getvalue())

    def test_details_bounds_and_retention(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            for i in range(35):
                app.show_tool_result('read_file', '{}', str(i))
            app.show_tool_details('-1')
        self.assertEqual(len(app._TOOL_DETAILS), 30)
        self.assertEqual(app._TOOL_DETAILS[0][2], '5')
        self.assertIn('请输入', output.getvalue())

    def test_multiline_keeps_blank_lines(self):
        with patch('builtins.input', side_effect=['first', '', 'last', '/send']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.read_multiline(), 'first\n\nlast')

    def test_multiline_cancel_and_eof_discard_input(self):
        for ending in ('/cancel', EOFError()):
            with patch('builtins.input', side_effect=['partial', ending]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.read_multiline(), '')

    def test_numeric_approval_is_single_read(self):
        for answer, expected in [('1', 'y'), ('2', 'a'), ('3', 'n'), ('', 'n')]:
            with patch('builtins.input', return_value=answer) as read, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(app.ask_permission('run command', 'pwd'), expected)
                read.assert_called_once()
