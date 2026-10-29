"""Official DeepSeek transport and thinking/tool-history integration, offline."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import shellclaude as app


def cfg(**extra):
    return {**app.DEFAULT_CFG, "auth_method": "deepseek", "model": "deepseek-flash",
            "deepseek_api_key": "fixture-deepseek", "api_key": "fixture-openai",
            "base_url": "https://unrelated.invalid/v1", **extra}


def response(text="done", reasoning="fixture reasoning", call=False):
    message = {"role": "assistant", "content": text, "reasoning_content": reasoning}
    if call:
        message["tool_calls"] = [{"id": "call_fixture", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"sample.txt"}'}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if call else "stop"}]}


def stream(call=False, done=True):
    events = [
        {"choices": [{"delta": {"reasoning_content": "fixture "}}]},
        {"choices": [{"delta": {"reasoning_content": "reasoning"}}]},
        {"choices": [{"delta": {"content": "done"}}]},
    ]
    if call:
        events.append({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fixture", "function": {"name": "read_file", "arguments": '{"path":"sample.txt"}'}}]}}]})
    if done:
        events.append({"choices": [{"delta": {}, "finish_reason": "tool_calls" if call else "stop"}]})
    data = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    if done:
        data += "data: [DONE]\n\n"
    return io.BytesIO(data.encode())


class DeepSeekTests(unittest.TestCase):
    def test_setup_preserves_legacy_key_and_url(self):
        config = cfg(auth_method="openai_api")
        with patch.object(app, "save_cfg"), contextlib.redirect_stdout(io.StringIO()):
            app.cmd_auth(config, "auth", "method deepseek")
        self.assertEqual(app.auth_provider(config), "deepseek")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["api_key"], "fixture-openai")
        self.assertEqual(config["base_url"], "https://unrelated.invalid/v1")

    def test_separate_key_and_fixed_official_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(app._api_headers(cfg())["Authorization"], "Bearer fixture-deepseek")
            self.assertEqual(app._api_base_url(cfg()), "https://api.deepseek.com")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-env"}):
            self.assertEqual(app._provider_api_key(cfg()), "fixture-env")

    def test_env_deepseek_key_is_not_saved_or_used_by_other_providers(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "CFG_PATH", os.path.join(d, "config.json")), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture-env-secret"}):
            app.save_cfg(cfg(deepseek_api_key=""))
            with open(app.CFG_PATH) as f:
                self.assertNotIn("fixture-env-secret", f.read())
            self.assertEqual(app._provider_api_key(cfg(auth_method="openai_api")), "fixture-openai")

    def test_config_masks_new_key_in_setter_and_display(self):
        config = cfg()
        output = io.StringIO()
        with patch.object(app, "save_cfg"), patch.object(app, "_RICH", False), contextlib.redirect_stdout(output):
            app.cmd_config(config, "deepseek_api_key=FIXTURE_PRIVATE")
            app.cmd_config(config, "")
        self.assertNotIn("FIXTURE_PRIVATE", output.getvalue())

    def test_payload_thinking_and_complete_reasoning_history(self):
        history = [{"role": "assistant", "content": "prior", "thinking": "persisted reasoning"}]
        payload = app._build_payload_openai(cfg(), history)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["messages"][-1]["reasoning_content"], "persisted reasoning")
        self.assertNotIn("reasoning_content", history[0])
        self.assertNotIn("reasoning_content", app._build_payload_openai(cfg(auth_method="openai_api"), history)["messages"][-1])

    def test_non_thinking_mode_and_invalid_configuration(self):
        p = app._build_payload_openai(cfg(deepseek_thinking="disabled"), [])
        self.assertEqual(p["thinking"]["type"], "disabled")
        self.assertIn("temperature", p)
        self.assertNotIn("reasoning_effort", p)
        with self.assertRaises(RuntimeError):
            app._build_payload_openai(cfg(deepseek_reasoning_effort="invalid"), [])

    def test_account_model_catalog_and_context(self):
        data = {"data": [{"id": "deepseek-flash", "name": "Flash", "context_window": 1048576}]}
        with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())) as request:
            models = app._fetch_model_list(cfg())
        self.assertEqual(models[0], {"slug": "deepseek-flash", "display_name": "Flash"})
        self.assertEqual(request.call_args.args[0].full_url, "https://api.deepseek.com/models")
        self.assertEqual(app.get_context_window("deepseek-flash"), 1048576)

    def test_nonstream_transport_and_reasoning(self):
        with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps(response()).encode())) as request:
            result = app.api_call(cfg(), [])
        self.assertEqual(request.call_args.args[0].full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(result["choices"][0]["message"]["reasoning_content"], "fixture reasoning")

    def test_stream_accumulates_reasoning_and_tools(self):
        app._CANCEL.clear()
        with patch.object(app, "urlopen", return_value=stream(call=True)), patch.object(app, "_RICH", False), contextlib.redirect_stdout(io.StringIO()):
            result = app.api_call_stream(cfg(), [])
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "fixture reasoning")
        self.assertEqual(message["tool_calls"][0]["id"], "call_fixture")

    def test_interrupted_stream_never_returns_partial_calls_or_retries(self):
        app._CANCEL.clear()
        with patch.object(app, "urlopen", return_value=stream(call=True, done=False)) as request, patch.object(app, "_RICH", False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                app.api_call_stream(cfg(), [])
        self.assertEqual(request.call_count, 1)

    def test_agent_tool_continuation_and_session_reload_keep_reasoning(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "DB_PATH", os.path.join(d, "session.db")):
            conn = app.db_init()
            try:
                sid = app.db_new_session(conn)
                with patch.object(app, "urlopen", side_effect=[io.BytesIO(json.dumps(response(text="", call=True)).encode()), io.BytesIO(json.dumps(response()).encode())]) as request, patch.object(app, "dispatch_tool", return_value="fixture file") as tool, contextlib.redirect_stdout(io.StringIO()):
                    app.agent_loop(cfg(stream=False), conn, sid, [], "Read sample.txt")
                tool.assert_called_once()
                continuation = json.loads(request.call_args_list[1].args[0].data)["messages"]
                self.assertEqual(continuation[-2]["reasoning_content"], "fixture reasoning")
                self.assertEqual(continuation[-1]["tool_call_id"], "call_fixture")
                loaded = app.db_load_session(conn, sid)
                restored = app._build_payload_openai(cfg(), loaded)
                self.assertEqual(restored["messages"][-1]["reasoning_content"], "fixture reasoning")
            finally:
                conn.close()


class DeepSeekFailureTests(unittest.TestCase):
    def test_balance_error_is_actionable_without_echoing_body(self):
        from urllib.error import HTTPError
        error = HTTPError("https://api.deepseek.com/chat/completions", 402, "fixture", {}, io.BytesIO(b"FIXTURE_PRIVATE"))
        with patch.object(app, "urlopen", side_effect=error) as request:
            with self.assertRaises(RuntimeError) as ctx:
                app.api_call(cfg(), [])
        self.assertIn("余额不足", str(ctx.exception))
        self.assertNotIn("FIXTURE_PRIVATE", str(ctx.exception))
        self.assertEqual(request.call_count, 1)

    def test_truncated_nonstream_output_does_not_enable_tool_execution(self):
        data = response(call=True)
        data["choices"][0]["finish_reason"] = "length"
        with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            with self.assertRaises(RuntimeError):
                app.api_call(cfg(), [])


if __name__ == "__main__":
    unittest.main()
