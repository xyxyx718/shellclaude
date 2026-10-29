"""Offline protocol/security regression tests. No personal tokens or live requests."""
import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import jwt
import rsa
import shellclaude as app
from shellclaude_lib import auth
from shellclaude_lib import responses

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
CALL = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "namespace": "shellclaude",
        "name": "read_file", "arguments": '{"path":"sample.txt"}', "status": "completed"}
MESSAGE = {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": "你好", "annotations": []}]}


def sse(*events):
    return io.BytesIO(b"".join(("event: " + e["type"] + "\r\ndata: " + json.dumps(e) + "\r\n\r\n").encode() for e in events))


def completed(output):
    return {"type": "response.completed", "response": {"status": "completed", "output": output,
            "usage": {"input_tokens": 12, "output_tokens": 4}}}


def token_response(**kwargs):
    return {"access_token": "fixture-access", "refresh_token": "fixture-refresh-new", "id_token": "fixture-id",
            "token_type": "Bearer", "expires_in": 3600, "scope": auth.SCOPES, **kwargs}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = auth.Store(self.temp.name)
        self.client = auth.ChatGPTAuth(self.store)

    def seed(self, expires=0, **kwargs):
        a = {"client_id": "oaiapp_fixture", "subject": "fixture-sub", "issuer": auth.ISSUER,
             "label": "account-1", "access_token": "fixture-access-old", "refresh_token": "fixture-refresh-old",
             "id_token": "fixture-id-old", "expires_at": expires, "scopes": auth.SCOPES.split(), **kwargs}
        self.store.save("credentials", {"active": a["client_id"], "accounts": {a["client_id"]: a}})
        return a

    def pending(self, account=None):
        return auth.authorization("http://127.0.0.1:1234/auth/callback", "urn:uuid:fixture", account)

    def callback(self, p, **kwargs):
        return "/auth/callback?" + urlencode({"state": p["state"], "code": "fixture-code", "client_id": "oaiapp_fixture", **kwargs})

    def test_pkce_url_and_randomness(self):
        url, p = self.pending()
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["client_id"], ["dynamic_agent_client"])
        self.assertEqual(q["agent_name_hint"], ["shellclaude"])
        self.assertEqual(q["resource"], [auth.RESOURCE])
        self.assertIn("chatgpt.tokens.use.direct", q["scope"][0].split())
        digest = base64.urlsafe_b64encode(auth.hashlib.sha256(p["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual(q["code_challenge"], [digest])
        self.assertTrue(43 <= len(p["verifier"]) <= 128)
        _, other = self.pending()
        for key in ("state", "nonce", "verifier"):
            self.assertNotEqual(p[key], other[key])
        self.assertNotIn(p["verifier"], url)

    def test_returning_url_no_credential_hint(self):
        url, p = self.pending(self.seed())
        q = parse_qs(urlsplit(url).query)
        self.assertEqual(q["client_id"], ["oaiapp_fixture"])
        self.assertNotIn("agent_name_hint", q)
        self.assertNotIn("id_token_hint", q)
        self.assertNotIn("fixture-id-old", url)

    def test_callback_success_and_replay(self):
        _, p = self.pending()
        self.assertEqual(auth.validate_callback(self.callback(p), p), ("fixture-code", "oaiapp_fixture"))
        with self.assertRaises(auth.AuthError):
            auth.validate_callback(self.callback(p), p)

    def test_callback_failure_paths(self):
        for change in ({"state": "wrong"}, {"client_id": "dynamic_agent_client"}, {"client_id": ""}, {"code": ""}, {"error": "access_denied"}):
            with self.subTest(change=change):
                _, p = self.pending()
                with self.assertRaises(auth.AuthError):
                    auth.validate_callback(self.callback(p, **change), p)
        for suffix in ("&state=duplicate", "#fragment"):
            _, p = self.pending()
            with self.assertRaises(auth.AuthError):
                auth.validate_callback(self.callback(p) + suffix, p)
        _, p = self.pending()
        p["created_at"] -= 301
        with self.assertRaises(auth.AuthError):
            auth.validate_callback(self.callback(p), p)
        _, p = self.pending({"client_id": "oaiapp_existing"})
        with self.assertRaises(auth.AuthError):
            auth.validate_callback(self.callback(p), p)

    def test_host_persisted_and_installation_unique(self):
        with self.store.locked():
            h = self.store.host()
            self.assertEqual(h, auth.Store(self.temp.name).host())
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(h, auth.Store(d).host())

    def test_atomic_permissions_and_corruption(self):
        self.store.save("credentials", {"fixture": True})
        self.assertEqual(os.stat(self.store.path("credentials")).st_mode & 0o777, 0o600)
        with patch.object(auth.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.store.save("credentials", {"replacement": True})
        self.assertEqual(self.store.load("credentials"), {"fixture": True})
        self.assertEqual(os.listdir(self.temp.name), ["credentials.json"])
        with open(self.store.path("credentials"), "w") as f:
            f.write("{broken")
        with self.assertRaises(auth.AuthError):
            self.store.load("credentials")

    def test_icloud_refused(self):
        with self.assertRaises(auth.AuthError):
            auth.Store(self.temp.name + "/Mobile Documents/auth")

    def test_lock_collision(self):
        with self.store.locked():
            with self.assertRaises(auth.AuthError):
                with auth.Store(self.temp.name).locked():
                    self.fail("concurrent lock granted")

    def test_refresh_rotation_and_no_repeat(self):
        self.seed()
        with patch.object(auth, "request", return_value=token_response(id_token=None)) as req:
            self.assertEqual(self.client.access_token(), "fixture-access")
            self.assertEqual(self.client.access_token(), "fixture-access")
        req.assert_called_once_with(auth.TOKEN, form={"grant_type": "refresh_token", "client_id": "oaiapp_fixture",
                                    "refresh_token": "fixture-refresh-old", "resource": auth.RESOURCE})
        self.assertEqual(self.client.active()["refresh_token"], "fixture-refresh-new")
        self.assertGreater(self.client.active()["expires_at"], time.time())

    def test_refresh_omitted_scope_retains_grant(self):
        self.seed()
        data = token_response(id_token=None)
        del data["scope"]
        with patch.object(auth, "request", return_value=data):
            self.client.access_token()
        self.assertIn("chatgpt.tokens.use.direct", self.client.active()["scopes"])

    def test_refresh_terminal_failure_clears_tokens_preserves_mapping(self):
        self.seed()
        with patch.object(auth, "request", side_effect=auth.RemoteError(400, {"error": "invalid_grant"})):
            with self.assertRaises(auth.AuthError):
                self.client.access_token()
        a = self.client.active()
        self.assertNotIn("refresh_token", a)
        self.assertEqual(a["client_id"], "oaiapp_fixture")
        self.assertIn("signed out", self.client.status())

    def test_transient_and_malformed_refresh_preserve_credentials(self):
        a = self.seed()
        for err in (auth.RemoteError(503, {"detail": "unavailable"}), auth.AuthError("offline")):
            with patch.object(auth, "request", side_effect=err):
                with self.assertRaises(auth.AuthError):
                    self.client.access_token()
            self.assertEqual(self.client.active(), a)
        for data in (token_response(expires_in=-1), token_response(expires_in="NaN"), token_response(refresh_token="")):
            with patch.object(auth, "request", return_value=data):
                with self.assertRaises(auth.AuthError):
                    self.client.access_token()
            self.assertEqual(self.client.active(), a)

    def test_missing_grant_prevents_inference(self):
        self.seed(expires=time.time()+3600, scopes=["openid"])
        with patch.object(auth, "request") as req:
            with self.assertRaises(auth.AuthError):
                self.client.access_token()
            req.assert_not_called()
        self.assertIn("disabled", self.client.status())

    def test_logout_revokes_and_clears_all_tokens(self):
        self.seed()
        with patch.object(auth, "metadata", return_value={"revocation_endpoint": auth.ISSUER + "/fixture/revoke"}), patch.object(auth, "request", return_value={}) as req:
            self.assertIn("Session revoked", self.client.logout())
        self.assertEqual(req.call_args.kwargs["form"]["token_type_hint"], "refresh_token")
        self.assertNotIn("access_token", self.client.active())
        self.assertNotIn("id_token", self.client.active())

    def test_logout_offline_clears_locally(self):
        self.seed()
        with patch.object(auth, "metadata", side_effect=auth.AuthError("offline")):
            self.assertIn("not confirmed", self.client.logout())
        self.assertNotIn("refresh_token", self.client.active())

    def test_login_issued_id_exchange_and_verified_activation(self):
        loop = Mock()
        loop.uri = "http://127.0.0.1:1234/auth/callback"
        loop.wait.return_value = ("fixture-code", "oaiapp_fixture")
        with patch.object(auth, "Loopback", return_value=loop), patch.object(auth, "request", return_value=token_response()) as req, patch.object(auth, "verify_id_token", return_value={"sub": "fixture-sub"}) as verify:
            self.client.login(lambda s: None)
        self.assertEqual(req.call_args.kwargs["form"]["client_id"], "oaiapp_fixture")
        self.assertEqual(req.call_args.kwargs["form"]["redirect_uri"], loop.uri)
        self.assertEqual(verify.call_args.args[2], loop.pending["nonce"])
        self.assertEqual(self.client.active()["subject"], "fixture-sub")

    def test_failed_identity_keeps_active_account(self):
        old = self.seed(expires=time.time()+1000)
        loop = Mock(uri="http://127.0.0.1:1234/auth/callback")
        loop.wait.return_value = ("fixture-code", "oaiapp_fixture")
        with patch.object(auth, "Loopback", return_value=loop), patch.object(auth, "request", return_value=token_response()), patch.object(auth, "verify_id_token", return_value={"sub": "wrong-account"}):
            with self.assertRaises(auth.AuthError):
                self.client.login(lambda s: None)
        self.assertEqual(self.client.active(), old)

    def test_redaction_and_errors(self):
        data = auth.redact({"refresh_token": "DO_NOT_PRINT", "nested": {"Authorization": "DO_NOT_PRINT"},
                            "url": auth.AUTHORIZE + "?id_token_hint=DO_NOT_PRINT"})
        self.assertNotIn("DO_NOT_PRINT", json.dumps(data))
        exc = auth.RemoteError(429, {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "DO_NOT_PRINT"}}, "fixture-request")
        self.assertIn("Settings", str(exc))
        self.assertNotIn("DO_NOT_PRINT", str(exc))
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.request_id, "fixture-request")


class JWTTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.public, cls.private = rsa.newkeys(2048)
        encint = lambda n: base64.urlsafe_b64encode(n.to_bytes((n.bit_length()+7)//8, "big")).decode().rstrip("=")
        cls.jwk = {"kid": "fixture-key", "kty": "RSA", "use": "sig", "alg": "RS256",
                   "n": encint(cls.public.n), "e": encint(cls.public.e)}

    def signed(self, changes=None, header=None):
        enc = lambda x: base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip("=")
        claims = {"iss": auth.ISSUER, "aud": ["oaiapp_fixture"], "sub": "fixture-sub",
                  "nonce": "fixture-nonce", "iat": time.time()-1, "exp": time.time()+300, **(changes or {})}
        body = enc(header or {"alg": "RS256", "kid": "fixture-key"}) + "." + enc(claims)
        sig = base64.urlsafe_b64encode(rsa.sign(body.encode(), self.private, "SHA-256")).decode().rstrip("=")
        return body + "." + sig

    def setUp(self):
        self.patch = patch.object(auth, "_JWKS", {"keys": [self.jwk]})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.at = patch.object(auth, "_JWKS_AT", time.time())
        self.at.start()
        self.addCleanup(self.at.stop)

    def test_real_rsa_signature_and_claims(self):
        c = auth.verify_id_token(self.signed(), "oaiapp_fixture", "fixture-nonce")
        self.assertEqual(c["sub"], "fixture-sub")

    def test_reject_claims(self):
        for changes in ({"iss": "https://evil.invalid"}, {"aud": ["other-client"]}, {"exp": time.time()-30},
                        {"nonce": "wrong"}, {"sub": ""}, {"iat": time.time()+60}, {"nbf": time.time()+60},
                        {"aud": ["oaiapp_fixture", "other"]}, {"azp": "other"}):
            with self.subTest(changes=changes):
                with self.assertRaises(auth.AuthError):
                    auth.verify_id_token(self.signed(changes), "oaiapp_fixture", "fixture-nonce")

    def test_reject_signature_and_alg_confusion(self):
        token = self.signed()
        a, b, c = token.split(".")
        c = ("A" if c[0] != "A" else "B") + c[1:]
        for bad in (a+"."+b+"."+c, "broken", self.signed(header={"alg": "none", "kid": "fixture-key"}),
                    self.signed(header={"alg": "HS256", "kid": "fixture-key"})):
            with self.assertRaises(auth.AuthError):
                auth.verify_id_token(bad, "oaiapp_fixture", "fixture-nonce")

    def test_unknown_kid_refreshes_official_jwks(self):
        with patch.object(auth, "_JWKS", {"keys": []}), patch.object(auth, "metadata", return_value={"jwks_uri": auth.ISSUER + "/.well-known/jwks.json"}), patch.object(auth, "request", return_value={"keys": [self.jwk]}) as req:
            auth.verify_id_token(self.signed(), "oaiapp_fixture", "fixture-nonce")
        req.assert_called_once_with(auth.ISSUER + "/.well-known/jwks.json")


class ResponsesTests(unittest.TestCase):
    def test_history_and_namespace(self):
        history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "read"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "call_fixture", "function": {"name": "read_file", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "call_fixture", "content": "file contents", "_db_id": 1}]
        p = responses.build_payload("fixture-model", history, "instructions", TOOLS)
        self.assertFalse(p["store"])
        self.assertTrue(p["stream"])
        self.assertEqual(p["input"][0]["role"], "developer")
        self.assertEqual(p["input"][-1]["type"], "function_call_output")
        self.assertEqual(p["tools"][0]["type"], "namespace")
        for key in ("temperature", "max_output_tokens", "previous_response_id", "metadata", "max_tokens"):
            self.assertNotIn(key, p)
        self.assertNotIn("_db_id", json.dumps(p))

    def test_stream_text_tools_and_reasoning_continuation(self):
        reason = {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "fixture-encrypted"}
        frames = sse({"type": "response.output_text.delta", "delta": "你好"},
                     {"type": "response.output_item.added", "output_index": 1, "item": {**CALL, "arguments": ""}},
                     {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":'},
                     {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"sample.txt"}'},
                     {"type": "response.function_call_arguments.done", "output_index": 1, "arguments": CALL["arguments"]},
                     completed([reason, MESSAGE, CALL]))
        chunks = []
        result = responses.parse_stream(frames, TOOLS, chunks.append)
        self.assertEqual(chunks, ["你好"])
        m = result["choices"][0]["message"]
        self.assertEqual(m["content"], "你好")
        self.assertEqual(m["tool_calls"][0]["id"], CALL["call_id"])
        p = responses.build_payload("fixture", [m, {"role": "tool", "tool_call_id": CALL["call_id"], "content": "result"}], "", TOOLS)
        self.assertEqual(p["input"][:3], [reason, MESSAGE, CALL])
        self.assertEqual(p["input"][-1]["output"], "result")

    def test_stream_failures_never_return_tools(self):
        for frames in (sse({"type": "response.output_text.delta", "delta": "partial"}),
                       sse({"type": "response.incomplete", "response": {}}),
                       sse({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}),
                       sse({"type": "error", "code": "subscription_sharing_usage_unavailable"}),
                       io.BytesIO(b'data: {bad}\n\n'), io.BytesIO(b'data: {}'),
                       sse(completed([{**CALL, "arguments": "{"}])),
                       sse(completed([{**CALL, "name": "unknown"}])),
                       sse(completed([CALL, CALL]))):
            with self.assertRaises(auth.AuthError):
                responses.parse_stream(frames, TOOLS)

    def test_cancellation(self):
        with self.assertRaises(auth.AuthError):
            responses.parse_stream(sse(completed([CALL])), TOOLS, cancelled=lambda: True)

    def test_sse_comments_and_multiline(self):
        raw = io.BytesIO(b':heartbeat\r\nevent: test\r\ndata: {"type":\r\ndata: "test"}\r\n\r\n')
        self.assertEqual(list(responses.sse_events(raw)), [{"type": "test"}])

    def test_model_catalog_account_format(self):
        provider = responses.ResponsesProvider(Mock(access_token=Mock(return_value="fixture-access")))
        with patch.object(responses, "request", return_value={"models": [
                {"slug": "b", "display_name": "B", "visibility": "list"},
                {"slug": "hidden", "display_name": "Hidden", "visibility": "hidden"},
                {"slug": "a", "display_name": "A", "visibility": "list"}]}) as req:
            self.assertEqual([m["slug"] for m in provider.models()], ["b", "a"])
        req.assert_called_once_with(auth.RESOURCE+"/models", token="fixture-access")
        with patch.object(responses, "request", return_value={"data": []}):
            with self.assertRaises(auth.AuthError):
                provider.models()

    def test_no_post_replay(self):
        provider = responses.ResponsesProvider(Mock(access_token=Mock(return_value="fixture")))
        with patch.object(responses, "request", side_effect=auth.RemoteError(401, {"detail": "denied"})) as req:
            with self.assertRaises(auth.AuthError):
                provider.call("m", [], "", TOOLS)
        self.assertEqual(req.call_count, 1)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(app, "DB_PATH", os.path.join(self.temp.name, "sessions.db"))
        p.start()
        self.addCleanup(p.stop)
        self.conn = app.db_init()
        self.addCleanup(self.conn.close)
        self.sid = app.db_new_session(self.conn)

    def cfg(self, **kwargs):
        return {**app.DEFAULT_CFG, "model": "fixture-model", "api_key": "fixture-key", "base_url": "https://fixture.invalid/v1", **kwargs}

    def test_legacy_provider_headers_and_payloads(self):
        cfg = self.cfg()
        self.assertEqual(app.auth_provider(cfg), "openai_api")
        self.assertEqual(app._api_headers(cfg)["Authorization"], "Bearer fixture-key")
        self.assertIn("messages", app._build_payload_openai(cfg, [{"role": "user", "content": "Hi"}]))
        cfg["endpoint_type"] = "anthropic"
        self.assertEqual(app.auth_provider(cfg), "anthropic")
        self.assertEqual(app._api_headers(cfg)["x-api-key"], "fixture-key")
        self.assertNotIn("Authorization", app._api_headers(cfg))
        self.assertIn("system", app._build_payload_anthropic(cfg, []))

    def test_existing_api_key_transports(self):
        for cfg, body in ((self.cfg(), {"choices": [{"message": {"content": "OK"}}]}),
                          (self.cfg(endpoint_type="anthropic"), {"content": [{"type": "text", "text": "OK"}], "usage": {}})):
            with patch.object(app, "urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
                result = app.api_call(cfg, [])
            self.assertEqual(result["choices"][0]["message"]["content"], "OK")

    def test_session_roundtrip_and_copy(self):
        output = [{"type": "reasoning", "encrypted_content": "fixture-encrypted"}, CALL]
        app.db_save_msg(self.conn, self.sid, "assistant", "", responses_output=output)
        app.db_save_msg(self.conn, self.sid, "tool", "result", tool_call_id="call_fixture")
        loaded = app.db_load_session(self.conn, self.sid)
        self.assertEqual(loaded[0]["_responses_output"], output)
        self.assertEqual(loaded[1]["tool_call_id"], "call_fixture")
        copied = app.db_import_messages(self.conn, self.sid, [0, 1], app.db_new_session(self.conn))
        self.assertEqual(copied[0]["_responses_output"], output)
        self.assertEqual(copied[1]["tool_call_id"], "call_fixture")
        self.assertNotIn("_responses_output", app._clean_messages(loaded)[0])

    def test_multistep_agent_mocked_http(self):
        cfg = self.cfg(auth_method="chatgpt", api_key="", stream=False)
        provider = responses.ResponsesProvider(Mock(access_token=Mock(return_value="fixture-access")))
        with patch.object(app, "_chatgpt_provider", return_value=provider), patch.object(responses, "request", side_effect=[sse(completed([CALL])), sse(completed([MESSAGE]))]) as req, patch.object(app, "get_tool_defs", return_value=TOOLS), patch.object(app, "dispatch_tool", return_value="file contents") as dispatch, contextlib.redirect_stdout(io.StringIO()):
            result = app.agent_loop(cfg, self.conn, self.sid, [], "Read sample.txt")
        dispatch.assert_called_once_with("read_file", CALL["arguments"])
        self.assertEqual(len(req.call_args_list), 2)
        continued = req.call_args_list[1].kwargs["payload"]["input"]
        self.assertEqual(continued[-1]["type"], "function_call_output")
        self.assertEqual(continued[-1]["call_id"], "call_fixture")
        self.assertEqual(result[-1]["content"], "你好")
        self.assertEqual(app._LAST_MSG_COST, 0)
        self.assertEqual(app.db_load_session(self.conn, self.sid)[1]["_responses_output"], [CALL])

    def test_failed_stream_does_not_dispatch(self):
        cfg = self.cfg(auth_method="chatgpt", stream=True)
        provider = responses.ResponsesProvider(Mock(access_token=Mock(return_value="fixture-access")))
        with patch.object(app, "_chatgpt_provider", return_value=provider), patch.object(responses, "request", return_value=sse({"type": "response.output_item.added", "output_index": 0, "item": CALL})), patch.object(app, "get_tool_defs", return_value=TOOLS), patch.object(app, "dispatch_tool") as dispatch, contextlib.redirect_stdout(io.StringIO()):
            app.agent_loop(cfg, self.conn, self.sid, [], "test")
        dispatch.assert_not_called()

    def test_auth_storage_protected(self):
        path = auth.AUTH_DIR + "/credentials.json"
        self.assertTrue(app._is_sensitive_path(path)[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(app.tool_write_file(path, "bad").startswith("ERROR"))


class TransportTests(unittest.TestCase):
    def test_form_and_bearer(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"ok":true}')
        with patch.object(auth, "build_opener", return_value=opener):
            auth.request(auth.TOKEN, form={"grant_type": "authorization_code", "code": "fixture-code"})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertEqual(parse_qs(req.data.decode())["code"], ["fixture-code"])
        self.assertEqual(req.full_url, auth.TOKEN)

    def test_http_error_shapes_and_no_secrets_in_exception(self):
        for body in (b'{"detail":"fixture-private"}', b'not-json', b'{"error":{"code":"subscription_sharing_usage_limit_exceeded"}}'):
            err = HTTPError(auth.RESOURCE, 429, "fixture-private", {"x-request-id": "fixture-request"}, io.BytesIO(body))
            opener = Mock()
            opener.open.side_effect = err
            with patch.object(auth, "build_opener", return_value=opener):
                with self.assertRaises(auth.RemoteError) as ctx:
                    auth.request(auth.RESOURCE+"/models", token="fixture-private")
            self.assertNotIn("fixture-private", str(ctx.exception))
            self.assertEqual(ctx.exception.request_id, "fixture-request")

    def test_redirect_refused(self):
        with self.assertRaises(auth.AuthError):
            auth._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")

    def test_loopback_real_socket_probe(self):
        listener = auth.Loopback(probe=True)
        errors = []
        def client():
            try:
                with urlopen(listener.uri, timeout=3) as r:
                    self.assertIn(b"Callback received", r.read())
            except Exception as exc:
                errors.append(exc)
        t = threading.Thread(target=client)
        t.start()
        self.assertTrue(listener.wait(timeout=5))
        t.join(5)
        self.assertEqual(errors, [])



class AdditionalSecurityTests(unittest.TestCase):
    setUp = OAuthTests.setUp
    seed = OAuthTests.seed
    pending = OAuthTests.pending
    # Inherit protocol tests against the same storage implementation, plus edge paths.
    def test_identity_only_login_retains_identity_without_enabling_plan(self):
        loop = Mock(uri="http://127.0.0.1:1234/auth/callback")
        loop.wait.return_value = ("fixture-code", "oaiapp_fixture")
        data = {"id_token": "fixture-id", "scope": "openid profile email"}
        with patch.object(auth, "Loopback", return_value=loop), patch.object(auth, "request", return_value=data), patch.object(auth, "verify_id_token", return_value={"sub": "fixture-sub"}):
            self.client.login(lambda s: None)
        self.assertIn("signed in; plan usage disabled", self.client.status())
        with self.assertRaises(auth.AuthError):
            self.client.access_token()

    def test_malformed_storage_record_is_actionable(self):
        self.store.save("credentials", {"accounts": []})
        with self.assertRaises(auth.AuthError):
            self.client.status()

    def test_account_selection_and_new_login_isolation(self):
        old = self.seed(expires=time.time()+3600)
        loop = Mock(uri="http://127.0.0.1:1234/auth/callback")
        loop.wait.return_value = ("fixture-code", "oaiapp_second")
        with patch.object(auth, "Loopback", return_value=loop), patch.object(auth, "request", return_value=token_response()), patch.object(auth, "verify_id_token", return_value={"sub": "fixture-second-sub"}):
            self.client.login(lambda s: None, new=True)
        self.assertEqual(len(self.client.accounts()), 2)
        self.client.select("account-1")
        self.assertEqual(self.client.active(), old)

    def test_refresh_revoked_scope_disables_inference(self):
        self.seed()
        with patch.object(auth, "request", return_value=token_response(id_token=None, scope="openid profile email")):
            with self.assertRaises(auth.AuthError):
                self.client.access_token()
        self.assertNotIn("chatgpt.tokens.use.direct", self.client.active()["scopes"])

    def test_refresh_identity_mismatch_rejected(self):
        old = self.seed()
        with patch.object(auth, "request", return_value=token_response()), patch.object(auth, "verify_id_token", return_value={"sub": "different"}):
            with self.assertRaises(auth.AuthError):
                self.client.access_token()
        self.assertEqual(self.client.active(), old)

    def test_returning_callback_may_omit_client_id(self):
        _, p = self.pending({"client_id": "oaiapp_fixture"})
        self.assertEqual(auth.validate_callback("/auth/callback?"+urlencode({"state": p["state"], "code": "fixture-code"}), p)[1], "oaiapp_fixture")

    def test_consent_is_explicit(self):
        url, _ = auth.authorization("http://127.0.0.1:1234/auth/callback", "fixture-host", consent=True)
        self.assertEqual(parse_qs(urlsplit(url).query)["prompt"], ["consent"])
        self.assertNotIn("prompt", parse_qs(urlsplit(self.pending()[0]).query))


class LegacyConfigTests(unittest.TestCase):
    def test_old_config_has_no_migration_and_env_not_persisted(self):
        with tempfile.TemporaryDirectory() as d, patch.object(app, "CFG_PATH", os.path.join(d, "cfg.json")), patch.dict(os.environ, {"OPENAI_API_KEY": "fixture-env"}, clear=True):
            with open(app.CFG_PATH, "w") as f:
                json.dump({"endpoint_type": "anthropic", "model": "fixture-model"}, f)
            cfg = app.load_cfg()
            self.assertEqual(app.auth_provider(cfg), "anthropic")
            self.assertEqual(cfg["api_key"], "fixture-env")
            app.save_cfg(cfg)
            with open(app.CFG_PATH) as f:
                self.assertNotIn("fixture-env", f.read())

    def test_safe_mode_approval_is_still_required(self):
        with patch.object(app, "_SAFE_MODE", True), patch.object(app, "ask_permission", return_value="n") as ask:
            result = app.dispatch_tool("read_file", '{"path":"fixture.txt"}')
        self.assertIn("DENIED", result)
        ask.assert_called_once()


class FinalRegressionTests(unittest.TestCase):
    def test_existing_provider_streams(self):
        openai = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'
        anthropic = b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'
        for endpoint, stream in (("openai", openai), ("anthropic", anthropic)):
            cfg = {**app.DEFAULT_CFG, "endpoint_type": endpoint, "api_key": "fixture-key", "base_url": "https://fixture.invalid", "model": "fixture-model"}
            app._CANCEL.clear()
            with patch.object(app, "urlopen", return_value=io.BytesIO(stream)), patch.object(app, "_RICH", False), contextlib.redirect_stdout(io.StringIO()):
                result = app.api_call_stream(cfg, [])
            self.assertEqual(result["choices"][0]["message"]["content"], "OK")

    def test_debug_token_redaction(self):
        out = io.StringIO()
        with patch.object(app, "_DEBUG", True), patch.object(app, "_RICH", False), contextlib.redirect_stdout(out):
            app._debug_print("fixture", {"access_token": "FIXTURE_SECRET", "nested": [{"refresh_token": "FIXTURE_SECRET"}]})
        self.assertNotIn("FIXTURE_SECRET", out.getvalue())

    def test_auth_commands_do_not_store_tokens_in_config(self):
        cfg = dict(app.DEFAULT_CFG)
        login = Mock()
        login.login.return_value = "signed in"
        with patch.object(app, "_chatgpt_auth", return_value=login), patch.object(app, "save_cfg") as save, contextlib.redirect_stdout(io.StringIO()):
            app.cmd_auth(cfg, "login", "")
            self.assertEqual(cfg["auth_method"], "chatgpt")
            app.cmd_auth(cfg, "auth", "method anthropic")
            self.assertEqual(cfg["endpoint_type"], "anthropic")
        self.assertNotIn("access_token", cfg)
        self.assertNotIn("refresh_token", cfg)
        self.assertEqual(save.call_count, 2)

    def test_incomplete_output_and_wrong_namespace(self):
        for call in ({**CALL, "status": "in_progress"}, {**CALL, "namespace": "other"}):
            with self.assertRaises(auth.AuthError):
                responses.parse_stream(sse(completed([call])), TOOLS)

    def test_non_object_and_invalid_utf8_sse(self):
        for stream in (io.BytesIO(b'data: []\n\n'), io.BytesIO(b'data: \xff\n\n')):
            with self.assertRaises(auth.AuthError):
                list(responses.sse_events(stream))

    def test_discovery_offsite_endpoint_is_rejected(self):
        for endpoint in ("https://evil.invalid/jwks", "http://auth.openai.com/jwks", "https://auth.openai.com@evil.invalid/jwks"):
            with self.assertRaises(auth.AuthError):
                auth._official_url(endpoint)

    def test_malformed_token_fields(self):
        for change in ({"token_type": []}, {"scope": []}, {"expires_in": None}, {"access_token": None}):
            with self.assertRaises(auth.AuthError):
                auth._token_record(token_response(**change))


class InterruptedTransportTests(unittest.TestCase):
    def test_truncated_http_body_stops_with_safe_error(self):
        from http.client import IncompleteRead
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.__iter__ = Mock(side_effect=IncompleteRead(b"fixture-partial-body"))
        provider = responses.ResponsesProvider(Mock(access_token=Mock(return_value="fixture")))
        with patch.object(responses, "request", return_value=stream):
            with self.assertRaises(auth.AuthError) as ctx:
                provider.call("fixture-model", [], "", TOOLS)
        self.assertNotIn("fixture-partial-body", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
