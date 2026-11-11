"""Continue with ChatGPT: let the coach run on your ChatGPT Plus or Pro plan instead of an API key.

This is OpenAI's official "Sign in with ChatGPT" token sharing for open-source apps
(developers.openai.com/siwc). The flow:
- **Sign-in in your browser:** Doty opens OpenAI's sign-in page there and listens on
  http://127.0.0.1:1455/auth/callback for the reply. You sign in to OpenAI, never to Doty; Doty
  never sees your password.
- **Registration:** the first sign-in registers Doty with OpenAI (`dynamic_agent_client`) and
  returns a client id that later sign-ins reuse.
- **Code exchange:** PKCE (S256); no client secret.
- **Verification:** the ID token's RS256 signature is checked against OpenAI's published keys
  (plain Python, no extra library), plus issuer, audience, expiry and nonce. The plan-usage scope
  must have been granted.
- **Storage:** tokens are saved in %LOCALAPPDATA%\\Doty\\auth\\chatgpt.json, never logged. They're
  refreshed before they expire.
- **Billing:** usage counts against your ChatGPT plan's limits. You can cap it per app in your
  ChatGPT settings.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
PORTS = (1455, 1456, 1457)  # only the port may change between sign-ins
APP_NAME = "Doty (Dota 2 coach)"
AUTH_FILE = CACHE_DIR.parent / "auth" / "chatgpt.json"
SIGN_IN_TIMEOUT_S = 300


class SignInError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                                          "User-Agent": "Doty"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read())
        except Exception:
            err = {}
        raise SignInError(err.get("error_description") or err.get("error") or f"HTTP {exc.code}") from exc
    except OSError as exc:
        raise SignInError(f"couldn't reach OpenAI ({exc.__class__.__name__})") from exc


# --------------------------------------------------------------------------- ID token checks


def _rs256_ok(signing_input: bytes, sig: bytes, jwk: dict) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, the standard JWT RS256 check, in plain Python."""
    n = int.from_bytes(_unb64url(jwk["n"]), "big")
    e = int.from_bytes(_unb64url(jwk["e"]), "big")
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    return secrets.compare_digest(em, expected)


def verify_id_token(token: str, client_id: str, nonce: str, jwks: dict | None = None, now: float | None = None) -> dict:
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header, claims = json.loads(_unb64url(head_b64)), json.loads(_unb64url(body_b64))
    except Exception as exc:
        raise SignInError("OpenAI sent an unreadable ID token") from exc
    if header.get("alg") != "RS256":
        raise SignInError("unexpected ID token algorithm")
    if jwks is None:
        with urllib.request.urlopen(urllib.request.Request(JWKS_URL, headers={"User-Agent": "Doty"}), timeout=20) as r:
            jwks = json.loads(r.read())
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None or not _rs256_ok(f"{head_b64}.{body_b64}".encode(), _unb64url(sig_b64), key):
        raise SignInError("the ID token's signature didn't check out")
    aud = claims.get("aud")
    if claims.get("iss") != ISSUER or (client_id not in aud if isinstance(aud, list) else aud != client_id):
        raise SignInError("the ID token wasn't issued for Doty")
    if claims.get("exp", 0) < (now or time.time()) - 60:
        raise SignInError("the ID token has expired")
    if nonce and claims.get("nonce") != nonce:
        raise SignInError("the ID token doesn't match this sign-in")
    return claims


# --------------------------------------------------------------------------- storage


def load(path: Path = AUTH_FILE) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save(record: dict, path: Path = AUTH_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner-only where the OS supports it (the folder is per-user on Windows)
    except OSError:
        pass
    tmp.replace(path)


def sign_out(path: Path = AUTH_FILE) -> None:
    path.unlink(missing_ok=True)


def signed_in(path: Path = AUTH_FILE) -> str:
    """The signed-in account's email, or ""."""
    rec = load(path)
    return (rec or {}).get("email", "") if rec and rec.get("refresh_token") else ""


# --------------------------------------------------------------------------- sign-in


class _Callback(http.server.BaseHTTPRequestHandler):
    result: dict | None = None
    done: threading.Event

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/auth/callback":
            self.send_error(404)
            return
        type(self).result = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        ok = "code" in type(self).result
        body = ("<html><body style='font-family:Segoe UI;background:#0b0d10;color:#ece6dc;padding:40px'>"
                f"<h2>{'Signed in. You can close this tab and go back to Doty.' if ok else 'Sign-in was cancelled.'}"
                "</h2></body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).done.set()


def _listen() -> tuple[http.server.HTTPServer, int, type]:
    handler = type("Callback", (_Callback,), {"result": None, "done": threading.Event()})
    for port in PORTS:
        try:
            return http.server.HTTPServer(("127.0.0.1", port), handler), port, handler
        except OSError:
            continue
    raise SignInError("ports 1455-1457 on this PC are busy; close the app using them and try again")


def sign_in(open_browser=webbrowser.open, path: Path = AUTH_FILE, timeout: float = SIGN_IN_TIMEOUT_S) -> dict:
    """Run the whole browser sign-in. Blocking (call from a worker thread). Returns the saved record."""
    prev = load(path) or {}
    client_id = prev.get("client_id") or "dynamic_agent_client"
    host_id = prev.get("ext_agent_host_id") or f"doty-{uuid.uuid4()}"
    verifier = _b64url(secrets.token_bytes(48))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    server, port, handler = _listen()  # listen before the browser opens
    redirect = f"http://127.0.0.1:{port}/auth/callback"
    query = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect, "scope": SCOPE,
        "resource": RESOURCE, "state": state, "nonce": nonce, "ext_agent_host_id": host_id,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    }
    if client_id == "dynamic_agent_client":
        query["agent_name_hint"] = APP_NAME
    elif prev.get("id_token"):
        query["id_token_hint"] = prev["id_token"]
    thread = threading.Thread(target=server.serve_forever, name="doty-chatgpt-callback", daemon=True)
    thread.start()
    try:
        open_browser(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(query)}")
        if not handler.done.wait(timeout):
            raise SignInError("the sign-in timed out; try again")
    finally:
        server.shutdown()
        server.server_close()
    cb = handler.result or {}
    if cb.get("state") != state:
        raise SignInError("the sign-in reply didn't match; try again")
    if cb.get("error"):
        raise SignInError("sign-in was cancelled" if cb["error"] == "access_denied" else cb["error"])
    if "code" not in cb:
        raise SignInError("OpenAI didn't send a sign-in code")
    client_id = cb.get("client_id") or (client_id if client_id != "dynamic_agent_client" else "")
    if not client_id:
        raise SignInError("OpenAI didn't register Doty; try again")
    tokens = _post_form(TOKEN_URL, {"grant_type": "authorization_code", "client_id": client_id, "code": cb["code"],
                                    "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
    claims = verify_id_token(tokens.get("id_token", ""), client_id, nonce)
    scopes = (tokens.get("scope") or cb.get("scope") or "").split()
    if PLAN_SCOPE not in scopes:
        raise SignInError("your ChatGPT account didn't allow plan usage for Doty (it needs Plus or Pro)")
    record = {"email": claims.get("email", ""), "issuer": ISSUER, "subject": claims.get("sub", ""),
              "client_id": client_id, "ext_agent_host_id": host_id, "id_token": tokens.get("id_token"),
              "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token", ""),
              "token_type": tokens.get("token_type", "Bearer"),
              "expires_at": time.time() + float(tokens.get("expires_in", 3600)), "scopes": scopes,
              "saved_at": time.time()}
    save(record, path)
    return record


def access_token(path: Path = AUTH_FILE, refresh_margin_s: float = 300) -> str:
    """A valid access token, refreshed (and the rotating refresh token saved) when close to expiry."""
    rec = load(path)
    if not rec or not rec.get("refresh_token"):
        raise SignInError("not signed in to ChatGPT")
    if rec.get("expires_at", 0) - time.time() > refresh_margin_s:
        return rec["access_token"]
    try:
        tokens = _post_form(TOKEN_URL, {"grant_type": "refresh_token", "client_id": rec["client_id"],
                                        "refresh_token": rec["refresh_token"], "resource": RESOURCE})
    except SignInError as exc:
        raise SignInError(f"ChatGPT sign-in expired ({exc}); sign in again in Settings") from exc
    rec.update(access_token=tokens["access_token"], refresh_token=tokens.get("refresh_token", rec["refresh_token"]),
               expires_at=time.time() + float(tokens.get("expires_in", 3600)), saved_at=time.time())
    if tokens.get("scope"):
        rec["scopes"] = tokens["scope"].split()
    if tokens.get("id_token"):
        rec["id_token"] = tokens["id_token"]
    save(rec, path)
    return rec["access_token"]


