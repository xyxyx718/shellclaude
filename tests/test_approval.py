"""Chinese approval prompts and explicit numbered allowlist choices."""
import contextlib
import io
import unittest
from unittest.mock import patch

import shellclaude as app


class ApprovalMenuTests(unittest.TestCase):
    def run_command(self, answers):
        output = io.StringIO()
        with patch.object(app, "_SAFE_MODE", False), patch.object(app, "ALLOWLIST", []), patch("builtins.input", side_effect=answers) as read, patch.object(app, "save_allowlist") as save, patch.object(app, "_exec_shell", return_value=("fixture", 0)) as execute, contextlib.redirect_stdout(output):
            result = app.dispatch_tool("run_command", '{"cmd":"python3 sample.py --version"}')
        return result, read, save, execute, output.getvalue()

    def test_first_prompt_is_chinese_and_allow_once_is_not_saved(self):
        result, read, save, execute, output = self.run_command(["y"])
        self.assertEqual(result, "fixture")
        self.assertIn("是否允许", read.call_args.args[0])
        self.assertIn("执行命令", output)
        read.assert_called_once()
        save.assert_not_called()
        execute.assert_called_once()

    def test_numbered_exact_option_preserves_second_choice(self):
        result, read, save, execute, output = self.run_command(["a", "1"])
        self.assertEqual(result, "fixture")
        self.assertEqual(read.call_count, 2)
        save.assert_called_once_with(["python3 sample.py --version"])
        self.assertIn("1. 仅此完整命令", output)
        self.assertIn("2. 命令前缀", output)
        self.assertIn("3. 取消本次执行", output)
        execute.assert_called_once()

    def test_enter_defaults_to_exact_not_prefix(self):
        result, read, save, execute, output = self.run_command(["a", ""])
        self.assertEqual(result, "fixture")
        save.assert_called_once_with(["python3 sample.py --version"])
        self.assertFalse(app.is_allowed("python3 different.py", save.call_args.args[0]))

    def test_prefix_requires_additional_explicit_confirmation(self):
        result, read, save, execute, output = self.run_command(["a", "2", "y"])
        self.assertEqual(result, "fixture")
        self.assertEqual(read.call_count, 3)
        self.assertIn("确认添加此规则", read.call_args.args[0])
        save.assert_called_once_with(["python3 *"])
        execute.assert_called_once()

    def test_cancel_or_invalid_selection_never_executes(self):
        for choice in ("3", "9", "x", "unknown"):
            with self.subTest(choice=choice):
                result, read, save, execute, output = self.run_command(["a", choice])
                self.assertIn("DENIED", result)
                save.assert_not_called()
                execute.assert_not_called()

    def test_prefix_confirmation_defaults_to_denial(self):
        result, read, save, execute, output = self.run_command(["a", "2", ""])
        self.assertIn("DENIED", result)
        save.assert_not_called()
        execute.assert_not_called()

    def test_interrupt_or_eof_at_menu_denies(self):
        for error in (KeyboardInterrupt(), EOFError()):
            result, read, save, execute, output = self.run_command(["a", error])
            self.assertIn("DENIED", result)
            save.assert_not_called()
            execute.assert_not_called()

    def test_interrupt_or_eof_at_prefix_confirmation_denies(self):
        for error in (KeyboardInterrupt(), EOFError()):
            result, read, save, execute, output = self.run_command(["a", "2", error])
            self.assertIn("DENIED", result)
            save.assert_not_called()
            execute.assert_not_called()

    def test_legacy_letters_still_work(self):
        for answers, rule in ((["a", "e"], "python3 sample.py --version"), (["a", "p", "y"], "python3 *")):
            result, read, save, execute, output = self.run_command(answers)
            self.assertEqual(result, "fixture")
            save.assert_called_once_with([rule])

    def test_empty_or_denied_initial_answer_never_executes(self):
        for choice in ("", "n"):
            result, read, save, execute, output = self.run_command([choice])
            self.assertIn("DENIED", result)
            save.assert_not_called()
            execute.assert_not_called()

    def test_safe_mode_action_is_chinese(self):
        output = io.StringIO()
        with patch("builtins.input", return_value="n"), contextlib.redirect_stdout(output):
            self.assertEqual(app.ask_permission("[safe] read_file", "sample.txt"), "n")
        self.assertIn("安全模式确认：读取文件", output.getvalue())


if __name__ == "__main__":
    unittest.main()
