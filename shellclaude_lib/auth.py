"""Official Sign in with ChatGPT public-client flow. No native dependencies.

OAuth secrets never enter shellclaude configuration, history, or debug hooks.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
AUTH_DIR = os.path.expanduser("~/Library/Application Support/shellclaude/auth")
_TOKEN_KEYS = {"access_token", "refresh_token", "id_token", "code", "code_verifier",
               "authorization", "api_key", "id_token_hint", "client_secret"}


class AuthError(RuntimeError):
    pass


def redact(value):
    """Defense in depth for diagnostics; raw OAuth responses are never logged."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in _TOKEN_KEYS else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'https?://[^\s<>"\']*(?:authorize|callback)[^\s<>"\']*',
                       '[REDACTED URL]', value, flags=re.I)
        value = re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
        value = re.sub(r'(?i)((?:access_token|refresh_token|id_token|code_verifier|api_key|code)\s*[=:]\s*)[^\s&,"\']+',
                       r'\1[REDACTED]', value)
        value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED JWT]', value)
    return value


class RemoteError(AuthError):
    def __init__(self, status, body, request_id=None):
        # Preserve shape/code/param, never raw token-bearing response text.
        self.status = status
        self.body = redact(body)
        err = body.get("error", {}) if isinstance(body, dict) else {}
        self.code = err.get("code") if isinstance(err, dict) else err
        self.param = err.get("param") if isinstance(err, dict) else None
        self.request_id = redact(request_id)
        hints = {
            "subscription_sharing_usage_limit_exceeded": "Pause requests; check ChatGPT Settings → Usage: https://chatgpt.com/#settings/Usage . App limits may apply.",
            "subscription_sharing_user_not_eligible": "This user/workspace is not eligible. Check plan and workspace policy; do not repeat login in a loop.",
            "subscription_sharing_usage_unavailable": "Usage is temporarily unavailable. Retry later; credentials were preserved.",
            "subscription_sharing_user_unavailable": "Account information is temporarily unavailable. Retry later.",
            "subscription_sharing_unsupported_capability": "Remove the unsupported capability; check error.param.",
            "subscription_sharing_invalid_user": "Check account and permission. Sign in again after confirmed revocation or terminal refresh failure.",
        }
        hint = hints.get(self.code, {401: "Check account/scopes or sign in again; no automatic inference replay.",
                                    403: "Check account, region and workspace policy.",
                                    503: "Routing temporarily unavailable; retry later."}.get(status, ""))
        # OAuth server descriptions can echo credentials. Only allow structured codes.
        shape = "object:" + ",".join(sorted(body)) if isinstance(body, dict) else type(body).__name__
        safe_code = self.code if isinstance(self.code, str) and re.fullmatch(r'[a-z_]{1,100}', self.code) else "unknown"
        safe_param = self.param if isinstance(self.param, str) and re.fullmatch(r'[\w.\[\]-]{1,100}', self.param) else None
        super().__init__(f"HTTP {status}; code={safe_code}; param={safe_param}; request_id={self.request_id}; body={shape}. {hint}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthError("Unexpected HTTP redirect refused; credentials were not forwarded.")


def request(url, form=None, payload=None, token=None, empty=False):
    """Bounded transport with no redirect or retry of credential-bearing requests."""
    headers = {"Accept": "application/json"}
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        response = build_opener(_NoRedirect()).open(Request(url, data=data, headers=headers), timeout=60)
        if payload is not None:
            return response
        with response:
            if empty:
                response.read(1024)
                return {}
            result = json.loads(response.read(2_000_000))
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except HTTPError as exc:
        try:
            body = json.loads(exc.read(65536))
        except (ValueError, OSError):
            body = "non-JSON body (withheld)"
        raise RemoteError(exc.code, body, exc.headers.get("x-request-id") or exc.headers.get("openai-request-id")) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise AuthError("Network request failed. Check connectivity and retry; no request was replayed.") from None
    except (ValueError, TypeError):
        raise AuthError("Malformed JSON from OpenAI; response withheld.") from None


def _official_url(url):
    p = urlsplit(url or "")
    if p.scheme != "https" or p.netloc != "auth.openai.com" or p.fragment or p.query:
        raise AuthError("Invalid OpenAI discovery endpoint.")
    return url


def metadata():
    doc = request(DISCOVERY)
    if doc.get("issuer") != ISSUER:
        raise AuthError("Unexpected OpenAI issuer.")
    return doc


_JWKS = None
_JWKS_AT = 0


def _login_dependencies():
    try:
        import jwt
        import rsa
        return jwt, rsa
    except ImportError:
        raise AuthError("Install login dependencies: python3 -m pip install 'PyJWT>=2.10,<3' 'rsa>=4.9.1,<5'") from None


def verify_id_token(token, client_id, nonce=None):
    """RS256 via maintained pure-Python rsa; JWT claim validation via PyJWT.

    No private keys, handwritten RSA, native crypto, or algorithm negotiation.
    """
    jwt, rsa = _login_dependencies()
    global _JWKS, _JWKS_AT
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or header.get("crit"):
            raise ValueError()
        if _JWKS is None or time.time() - _JWKS_AT > 3600:
            _JWKS = request(_official_url(metadata()["jwks_uri"]))
            _JWKS_AT = time.time()
        keys = [k for k in _JWKS["keys"] if k.get("kid") == header["kid"]]
        if not keys:
            _JWKS = request(_official_url(metadata()["jwks_uri"]))
            _JWKS_AT = time.time()
            keys = [k for k in _JWKS["keys"] if k.get("kid") == header["kid"]]
        if len(keys) != 1:
            raise ValueError()
        k = keys[0]
        if k.get("kty") != "RSA" or k.get("use", "sig") != "sig" or k.get("alg", "RS256") != "RS256":
            raise ValueError()
        if "key_ops" in k and "verify" not in k["key_ops"]:
            raise ValueError()
        if any(not isinstance(k.get(x), str) or len(k[x]) > 1500 for x in ("n", "e")):
            raise ValueError()
        dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        pub = rsa.PublicKey(int.from_bytes(dec(k["n"]), "big"), int.from_bytes(dec(k["e"]), "big"))
        if not 2048 <= pub.n.bit_length() <= 8192:
            raise ValueError()
        signed, signature = token.rsplit(".", 1)
        if rsa.verify(signed.encode("ascii"), dec(signature), pub) != "SHA-256":
            raise ValueError()
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": True,
                            "verify_iat": True, "verify_nbf": True, "verify_aud": True,
                            "verify_iss": True, "require": ["iss", "aud", "sub", "exp", "iat"]},
                            audience=client_id, issuer=ISSUER, leeway=5)
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError()
        for claim in ("iat", "exp", "nbf"):
            if claim in claims and (type(claims[claim]) not in (int, float) or not math.isfinite(claims[claim])):
                raise ValueError()
        aud = claims["aud"]
        if (isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client_id) or claims.get("azp", client_id) != client_id:
            raise ValueError()
        if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError()
        return claims
    except AuthError:
        raise
    except Exception:
        raise AuthError("ID token signature or claims could not be verified; credentials were not activated.") from None


class Store:
    def __init__(self, directory=AUTH_DIR):
        self.directory = os.path.realpath(directory)
        if any(x in self.directory for x in ("iCloud", "Mobile Documents", "CloudDocs")):
            raise AuthError("OAuth storage must be local, outside iCloud Drive.")

    def path(self, name):
        return os.path.join(self.directory, name + ".json")

    def load(self, name):
        try:
            with open(self.path(name), encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                raise ValueError()
            return doc
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            raise AuthError("Credential storage is unreadable or corrupted; restore a local backup or remove it and sign in again.") from None

    def save(self, name, doc):
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.directory, 0o700)
        fd, temp = tempfile.mkstemp(prefix=".credentials-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                with contextlib.suppress(OSError):
                    os.fchmod(f.fileno(), 0o600)
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path(name))
            # Directory fsync is best-effort on iOS filesystems.
            with contextlib.suppress(OSError):
                dfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)

    @contextlib.contextmanager
    def locked(self):
        """Nonblocking OS lock, automatically released after a crash. Fail closed."""
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        try:
            import fcntl
            fd = os.open(os.path.join(self.directory, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        except (ImportError, OSError):
            raise AuthError("Credential locking unavailable on this platform; login/refresh disabled.") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AuthError("Another shellclaude process is using credentials. Retry after it finishes.") from None
            yield
        finally:
            os.close(fd)

    def host(self):
        doc = self.load("host")
        if not doc:
            doc = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
            self.save("host", doc)
        host = doc.get("ext_agent_host_id")
        if not isinstance(host, str) or not re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", host):
            raise AuthError("Malformed installation host ID; restore host.json or create a new installation.")
        return host


def authorization(redirect_uri, host_id, account=None, consent=False):
    account = account or {}
    verifier = secrets.token_urlsafe(48)
    pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
               "verifier": verifier, "redirect_uri": redirect_uri,
               "client_id": account.get("client_id", "dynamic_agent_client"),
               "created_at": time.monotonic(), "used": False}
    params = {"client_id": pending["client_id"], "ext_agent_host_id": host_id,
              "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
              "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
              "code_challenge_method": "S256",
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
    if pending["client_id"] == "dynamic_agent_client":
        params["agent_name_hint"] = "shellclaude"
    # Deliberately omit id_token_hint: no credential-bearing authorization URL in terminal.
    if consent:
        params["prompt"] = "consent"
    return AUTHORIZE + "?" + urlencode(params), pending


def validate_callback(path, pending):
    if pending["used"] or time.monotonic() - pending["created_at"] > 300:
        raise AuthError("Login attempt expired or already used; run /login again.")
    pending["used"] = True
    p = urlsplit(path)
    if p.path != "/auth/callback" or p.fragment or p.scheme or p.netloc:
        raise AuthError("Invalid callback path.")
    try:
        q = parse_qs(p.query, keep_blank_values=True, max_num_fields=20)
        if any(len(v) != 1 for v in q.values()):
            raise ValueError()
        if not hmac.compare_digest(q.get("state", [""])[0], pending["state"]):
            raise ValueError()
    except (ValueError, TypeError):
        raise AuthError("Callback state validation failed; no code exchanged.") from None
    if "error" in q:
        raise AuthError("Authorization was declined or failed; run /login when ready.")
    client = q.get("client_id", [pending["client_id"]])[0]
    if not client or client == "dynamic_agent_client" or (pending["client_id"] != "dynamic_agent_client" and client != pending["client_id"]):
        raise AuthError("Missing or mismatched issued client ID.")
    code = q.get("code", [""])[0]
    if not code:
        raise AuthError("Missing authorization code.")
    return code, client


class Loopback:
    """One foreground Python process; no fork, background shell or subprocess."""
    def __init__(self, probe=False):
        self.result = None
        self.error = None
        self.probe = probe
        self.pending = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Base implementation logs the credential-bearing callback URI.

            def do_GET(self):
                self.connection.settimeout(3)
                if self.headers.get("Host") != f"127.0.0.1:{outer.port}":
                    self.send_error(400)
                    return
                if urlsplit(self.path).path != "/auth/callback":
                    self.send_error(404)
                    return
                try:
                    outer.result = True if outer.probe else validate_callback(self.path, outer.pending)
                    text = b"Callback received. Return to a-Shell to finish verification."
                    status = 200
                except AuthError as exc:
                    outer.error = exc
                    text, status = b"Sign-in failed. Return to a-Shell and start a new login.", 400
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(text)))
                self.end_headers()
                self.wfile.write(text)

            def handle(self):
                self.connection.settimeout(3)
                super().handle()

        try:
            self.server = HTTPServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise AuthError("Cannot listen on 127.0.0.1. Use API-key providers and report your a-Shell/iPadOS versions.") from None
        self.port = self.server.server_port
        self.uri = f"http://127.0.0.1:{self.port}/auth/callback"
        self.server.timeout = 0.5

    def wait(self, timeout=300):
        deadline = time.monotonic() + timeout
        try:
            while self.result is None and self.error is None and time.monotonic() < deadline:
                self.server.handle_request()
            if self.error:
                raise self.error
            if self.result is None:
                raise AuthError("Callback timed out. Keep Safari and a-Shell visible together on iPad, then run /login again.")
            return self.result
        finally:
            self.server.server_close()


def _token_record(data, old=None, identity_only=False):
    old = old or {}
    try:
        if identity_only and isinstance(data.get("scope"), str) and "chatgpt.tokens.use.direct" not in data["scope"].split():
            record = {"scopes": data["scope"].split(), "expires_at": 0, "saved_at": time.time()}
            for key in ("access_token", "refresh_token"):
                if key in data:
                    if not isinstance(data[key], str):
                        raise ValueError()
                    record[key] = data[key]
            return record
        if not isinstance(data.get("access_token"), str) or not data["access_token"] or data.get("token_type", "").lower() != "bearer":
            raise ValueError()
        lifetime = float(data["expires_in"])
        if not math.isfinite(lifetime) or lifetime <= 0:
            raise ValueError()
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ValueError()
        scope = data.get("scope")
        if scope is None and old:
            scopes = old["scopes"]  # RFC 6749: unchanged grant when omitted on refresh.
        elif isinstance(scope, str):
            scopes = scope.split()
        else:
            raise ValueError()
        return {**old, "access_token": data["access_token"], "refresh_token": refresh,
                "token_type": "Bearer", "scopes": scopes, "expires_at": time.time() + lifetime,
                "saved_at": time.time()}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AuthError("Incomplete token response; credentials were not replaced.") from None


class ChatGPTAuth:
    def __init__(self, store=None):
        self.store = store or Store()
        self._mutex = threading.RLock()

    def _document(self):
        doc = self.store.load("credentials")
        try:
            accounts = doc.get("accounts", {})
            if not isinstance(accounts, dict) or (doc.get("active") is not None and doc["active"] not in accounts):
                raise ValueError()
            for key, a in accounts.items():
                if not isinstance(key, str) or not key or not isinstance(a, dict) or a.get("client_id") != key or key == "dynamic_agent_client":
                    raise ValueError()
                if any(not isinstance(a.get(k), str) or not a[k] for k in ("subject", "label", "issuer")) or a["issuer"] != ISSUER:
                    raise ValueError()
                for k in ("access_token", "refresh_token", "id_token"):
                    if k in a and not isinstance(a[k], str):
                        raise ValueError()
                if "scopes" in a and (not isinstance(a["scopes"], list) or any(not isinstance(x, str) for x in a["scopes"])):
                    raise ValueError()
                if "expires_at" in a and (not isinstance(a["expires_at"], (int, float)) or not math.isfinite(a["expires_at"])):
                    raise ValueError()
            return doc
        except (TypeError, KeyError, ValueError):
            raise AuthError("Credential record is malformed; restore a local backup or remove credentials.json and sign in again.") from None

    def accounts(self):
        return self._document().get("accounts", {})

    def active(self):
        doc = self._document()
        return doc.get("accounts", {}).get(doc.get("active"), {})

    def status(self):
        a = self.active()
        if not a:
            return "ChatGPT: signed out. /login — Continue with ChatGPT"
        if not a.get("access_token"):
            if a.get("id_token"):
                return f"ChatGPT account {a['label']}: signed in; plan usage disabled — /login consent"
            return "ChatGPT: signed out; registration retained. /login"
        enabled = "chatgpt.tokens.use.direct" in a.get("scopes", [])
        return f"ChatGPT account {a['label']}: signed in; plan usage {'enabled' if enabled else 'disabled — /login consent'}; token {'expired/refresh needed' if a.get('expires_at', 0) <= time.time()+60 else 'current'}."

    def login(self, display, new=False, consent=False):
        _login_dependencies()  # Fail before the browser flow when optional packages are missing.
        with self._mutex, self.store.locked():
            doc = self._document()
            old = {} if new else self.active()
            listener = Loopback()
            try:
                url, pending = authorization(listener.uri, self.store.host(), old, consent)
                listener.pending = pending
                display("Continue with ChatGPT: open this authorization link in Safari. Keep both apps visible on iPad. Do not share the link.")
                display(url)  # No tokens/id_token_hint/code in this link; never sent to logs/debug.
                code, client = listener.wait()
                data = request(TOKEN, form={"grant_type": "authorization_code", "client_id": client,
                               "code": code, "code_verifier": pending["verifier"],
                               "redirect_uri": pending["redirect_uri"], "resource": RESOURCE})
                claims = verify_id_token(data.get("id_token", ""), client, pending["nonce"])
                if old and claims["sub"] != old["subject"]:
                    raise AuthError("Returning account identity mismatch; active credentials unchanged. Use /login new.")
                record = _token_record(data, identity_only=True)
                record.update({"client_id": client, "subject": claims["sub"], "issuer": ISSUER,
                               "id_token": data["id_token"], "label": old.get("label") or f"account-{len(doc.get('accounts', {}))+1}"})
                # Optional recognition hint from verified identity, never used as the account key.
                if isinstance(claims.get("email"), str):
                    record["email"] = claims["email"]
                doc.setdefault("accounts", {})[client] = record
                doc["active"] = client
                self.store.save("credentials", doc)
            finally:
                listener.server.server_close()
        return self.status()

    def select(self, label):
        with self._mutex, self.store.locked():
            doc = self._document()
            matches = [k for k,v in doc.get("accounts", {}).items() if v["label"] == label]
            if len(matches) != 1:
                raise AuthError("Unknown account label. Use /auth accounts.")
            doc["active"] = matches[0]
            self.store.save("credentials", doc)

    def access_token(self):
        with self._mutex, self.store.locked():
            doc = self._document()
            client = doc.get("active")
            a = doc.get("accounts", {}).get(client, {})
            if not a.get("access_token"):
                raise AuthError("Sign in first: /login")
            if "chatgpt.tokens.use.direct" not in a.get("scopes", []):
                raise AuthError("Signed in, but ChatGPT plan usage is disabled. /login consent or /auth method openai_api")
            if a.get("expires_at", 0) <= time.time() + 60:
                try:
                    data = request(TOKEN, form={"grant_type": "refresh_token", "client_id": client,
                                   "refresh_token": a["refresh_token"], "resource": RESOURCE})
                except RemoteError as exc:
                    if exc.code in {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}:
                        doc["accounts"][client] = self._signed_out(a)
                        self.store.save("credentials", doc)
                    raise
                updated = _token_record(data, a)
                if data.get("id_token"):
                    claims = verify_id_token(data["id_token"], client)
                    if claims["sub"] != a["subject"]:
                        raise AuthError("Refresh identity mismatch; sign in again.")
                    updated["id_token"] = data["id_token"]
                doc["accounts"][client] = updated
                self.store.save("credentials", doc)
                a = updated
            if "chatgpt.tokens.use.direct" not in a["scopes"]:
                raise AuthError("Plan usage permission removed. Run /login consent.")
            return a["access_token"]

    @staticmethod
    def _signed_out(a):
        return {k: a[k] for k in ("client_id", "subject", "issuer", "label", "email") if k in a}

    def logout(self):
        with self._mutex, self.store.locked():
            doc = self._document()
            client = doc.get("active")
            a = doc.get("accounts", {}).get(client, {})
            confirmed = False
            try:
                if a.get("refresh_token"):
                    endpoint = _official_url(metadata()["revocation_endpoint"])
                    for attempt in range(2):
                        try:
                            request(endpoint, form={"token": a["refresh_token"], "token_type_hint": "refresh_token", "client_id": client}, empty=True)
                            confirmed = True
                            break
                        except AuthError as exc:
                            if isinstance(exc, RemoteError) and exc.status < 500:
                                break
                            if attempt == 0:
                                time.sleep(1)
            except (AuthError, KeyError):
                pass
            finally:
                if client in doc.get("accounts", {}):
                    doc["accounts"][client] = self._signed_out(a)
                    self.store.save("credentials", doc)
            return "Signed out; tokens removed. " + ("Session revoked." if confirmed else "Remote revocation not confirmed; disconnect shellclaude in ChatGPT Settings if needed.")
