"""The coach's other AI providers, against local fakes: OpenAI-compatible chat streaming (OpenAI, Gemini,
OpenRouter, Ollama), the Responses API on a ChatGPT plan, and the "Continue with ChatGPT" sign-in
(PKCE, a signed ID token, refresh). Nothing here talks to a real provider."""

import base64
import hashlib
import json
import random
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from doty.coach import backends, chatgpt_auth
from doty.coach.backends import PROVIDERS, BackendError, ChatGPTBackend, OpenAIChatBackend, pick_model

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}
CONTENT = [IMG, {"type": "text", "text": "Game state: {}"}]


class Fake:
    """A tiny HTTP server: `routes[(method, path)] = fn(body, headers) -> (status, content_type, bytes)`."""

    def __init__(self) -> None:
        self.routes, self.requests = {}, []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def _go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path = urllib.parse.urlsplit(self.path).path
                fake.requests.append((method, path, raw, dict(self.headers)))
                fn = fake.routes.get((method, path))
                status, ctype, body = fn(raw, self.headers) if fn else (404, "text/plain", b"nope")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def sse(events) -> bytes:
    return "".join(f"data: {json.dumps(e) if not isinstance(e, str) else e}\n\n" for e in events).encode()


async def collect(backend, kind="now"):
    return "".join([c async for c in backend.stream("SYSTEM", CONTENT, 300, kind)])


async def test_chat_completions_stream_with_images(fake):
    fake.routes[("GET", "/v1/models")] = lambda b, h: (200, "application/json", json.dumps(
        {"data": [{"id": "llama3"}, {"id": "gemma3:12b"}, {"id": "qwen2.5vl"}]}).encode())
    fake.routes[("POST", "/v1/chat/completions")] = lambda b, h: (200, "text/event-stream", sse(
        [{"choices": [{"delta": {"content": "Back off, "}}]}, {"choices": [{"delta": {"content": "three missing."}}]},
         {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 9}}, "[DONE]"]))
    b = OpenAIChatBackend(PROVIDERS["ollama"], "", base_url=fake.url + "/v1")
    await b.check()
    assert b.model == "qwen2.5vl"  # a vision model first
    assert await collect(b) == "Back off, three missing." and b.usage == {"in": 900, "out": 9}
    body = json.loads(fake.requests[-1][2])
    assert body["messages"][0] == {"role": "system", "content": "SYSTEM"} and body["stream"] is True
    assert body["messages"][1]["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}
    assert body["max_tokens"] == 300
    await b.aclose()


async def test_openai_key_errors_are_clear(fake):
    fake.routes[("GET", "/v1/models")] = lambda b, h: (401, "application/json", b'{"error": "bad"}')
    b = OpenAIChatBackend(PROVIDERS["openai"], "gpt-x", key="sk-wrong", base_url=fake.url + "/v1")
    with pytest.raises(BackendError) as e:
        await b.check()
    assert e.value.fatal and "didn't accept the key" in str(e.value)
    assert fake.requests[-1][3]["Authorization"] == "Bearer sk-wrong"
    fake.routes[("POST", "/v1/chat/completions")] = lambda b, h: (429, "application/json", b"{}")
    with pytest.raises(BackendError) as e:
        await collect(b)
    assert not e.value.fatal and "busy" in str(e.value)
    body = json.loads(fake.requests[-1][2])
    assert body["max_completion_tokens"] == 300  # OpenAI's newer name for it
    await b.aclose()


async def test_responses_api_on_a_chatgpt_plan(fake, monkeypatch):
    events = [{"type": "response.created"}, {"type": "response.output_text.delta", "delta": "Buy "},
              {"type": "response.output_text.delta", "delta": "BKB."},
              {"type": "response.completed", "response": {"usage": {"input_tokens": 50, "output_tokens": 3}}}]
    fake.routes[("POST", "/v1/responses")] = lambda b, h: (200, "text/event-stream", sse(events))
    b = ChatGPTBackend(PROVIDERS["chatgpt"], "gpt-6.1-mini", token_source=lambda: "tok-123")
    b.base = fake.url + "/v1"
    assert await collect(b) == "Buy BKB." and b.usage == {"in": 50, "out": 3}
    method, path, raw, headers = fake.requests[-1]
    body = json.loads(raw)
    assert headers["Authorization"] == "Bearer tok-123"
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "SYSTEM"
    assert body["input"][0]["content"][0] == {"type": "input_image", "image_url": "data:image/jpeg;base64,QUJD"}
    events[1:] = [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]
    with pytest.raises(BackendError) as e:
        await collect(b)
    assert "plan's limit" in str(e.value) and not e.value.fatal

    def not_signed_in():
        raise chatgpt_auth.SignInError("not signed in to ChatGPT")

    b2 = ChatGPTBackend(PROVIDERS["chatgpt"], "m", token_source=not_signed_in)
    with pytest.raises(BackendError) as e:
        await collect(b2)
    assert e.value.fatal
    await b.aclose()
    await b2.aclose()


def test_pick_model_and_missing_keys(monkeypatch):
    assert pick_model(["gemini-3.5-pro", "gemini-3.5-flash", "gemini-3.5-flash-lite"], ("flash", "gemini")) == "gemini-3.5-flash"
    assert pick_model(["a", "b"], ("zzz",)) == "a" and pick_model([], ("x",)) == ""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from doty.config import CoachConfig

    with pytest.raises(BackendError) as e:
        backends.make_backend(CoachConfig(provider="gemini"))
    assert e.value.fatal and "no Google Gemini API key" in str(e.value)


def test_the_ai_sdk_loads_after_the_window_not_before():
    """The Anthropic SDK takes ~2 s to import: nothing Doty loads before its window shows may pull it in."""
    import subprocess
    import sys

    code = ("import sys; import doty.app, doty.ui.window, doty.ui.controller, doty.ui.ai_settings; "
            "before = 'anthropic' in sys.modules or 'httpx2' in sys.modules; "
            "from doty.coach.backends import preload; preload(); "
            "print(before, 'anthropic' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["False", "True"]


# --------------------------------------------------------------------------- Continue with ChatGPT


def _rsa_key(bits=1024):
    rng = random.Random(7)

    def prime(n):
        while True:
            c = rng.getrandbits(n) | (1 << (n - 1)) | 1
            if all(pow(a, c - 1, c) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29)):
                return c

    e = 65537
    while True:
        p, q = prime(bits // 2), prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(claims: dict, n: int, d: int, kid="k1") -> str:
    head = _b64(json.dumps({"alg": "RS256", "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    k = (n.bit_length() + 7) // 8
    info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{head}.{body}".encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return f"{head}.{body}.{_b64(sig)}"


N, E, D = _rsa_key()
JWKS = {"keys": [{"kty": "RSA", "kid": "k1", "n": _b64(N.to_bytes((N.bit_length() + 7) // 8, "big")),
                  "e": _b64(E.to_bytes(3, "big"))}]}


def test_id_token_signature_and_claims():
    claims = {"iss": chatgpt_auth.ISSUER, "aud": "oaiapp_1", "exp": time.time() + 600, "nonce": "n1",
              "sub": "user-1", "email": "a@b.c"}
    tok = _sign(claims, N, D)
    assert chatgpt_auth.verify_id_token(tok, "oaiapp_1", "n1", JWKS)["sub"] == "user-1"
    for bad, why in ((tok[:-4] + "AAAA", "signature"), (_sign({**claims, "aud": "x"}, N, D), "issued for Doty"),
                     (_sign({**claims, "nonce": "z"}, N, D), "match this sign-in"),
                     (_sign({**claims, "exp": 1}, N, D), "expired")):
        with pytest.raises(chatgpt_auth.SignInError, match=why):
            chatgpt_auth.verify_id_token(bad, "oaiapp_1", "n1", JWKS)


def test_full_sign_in_then_refresh(fake, monkeypatch, tmp_path):
    seen = {}

    def token(raw, headers):
        form = dict(urllib.parse.parse_qsl(raw.decode()))
        seen.setdefault("grants", []).append(form)
        if form["grant_type"] == "authorization_code":
            ok = hashlib.sha256(form["code_verifier"].encode()).digest()
            assert _b64(ok) == seen["challenge"] and form["redirect_uri"] == seen["redirect"]
            assert form["client_id"] == "oaiapp_9" and form["resource"] == chatgpt_auth.RESOURCE
            idt = _sign({"iss": chatgpt_auth.ISSUER, "aud": "oaiapp_9", "exp": time.time() + 600,
                         "nonce": seen["nonce"], "sub": "u9", "email": "player@example.com"}, N, D)
            out = {"id_token": idt, "access_token": "acc-1", "refresh_token": "ref-1", "token_type": "Bearer",
                   "expires_in": 3600, "scope": chatgpt_auth.SCOPE}
        else:
            assert form["refresh_token"] == "ref-1"
            out = {"access_token": "acc-2", "refresh_token": "ref-2", "expires_in": 3600}
        return 200, "application/json", json.dumps(out).encode()

    fake.routes[("POST", "/token")] = token
    fake.routes[("GET", "/jwks")] = lambda b, h: (200, "application/json", json.dumps(JWKS).encode())
    monkeypatch.setattr(chatgpt_auth, "TOKEN_URL", fake.url + "/token")
    monkeypatch.setattr(chatgpt_auth, "JWKS_URL", fake.url + "/jwks")

    def browser(url):  # what OpenAI's page does after you sign in: redirect back with a code
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["client_id"] == "dynamic_agent_client" and q["code_challenge_method"] == "S256"
        assert chatgpt_auth.PLAN_SCOPE in q["scope"] and q["redirect_uri"].startswith("http://127.0.0.1:")
        assert q["agent_name_hint"] and q["ext_agent_host_id"]
        seen.update(challenge=q["code_challenge"], redirect=q["redirect_uri"], nonce=q["nonce"])
        back = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "c0de", "state": q["state"],
                                                                   "client_id": "oaiapp_9", "scope": q["scope"]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()

    path = tmp_path / "chatgpt.json"
    rec = chatgpt_auth.sign_in(open_browser=browser, path=path, timeout=10)
    assert rec["email"] == "player@example.com" and chatgpt_auth.signed_in(path) == "player@example.com"
    assert chatgpt_auth.access_token(path) == "acc-1"  # still fresh
    saved = json.loads(path.read_text())
    saved["expires_at"] = time.time() + 10
    path.write_text(json.dumps(saved))
    assert chatgpt_auth.access_token(path) == "acc-2"  # refreshed, rotating refresh token saved
    assert json.loads(path.read_text())["refresh_token"] == "ref-2"
    chatgpt_auth.sign_out(path)
    assert chatgpt_auth.signed_in(path) == ""


def test_cancelled_sign_in(monkeypatch, tmp_path):
    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        back = q["redirect_uri"] + "?" + urllib.parse.urlencode({"error": "access_denied", "state": q["state"]})
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=5).read(), daemon=True).start()

    with pytest.raises(chatgpt_auth.SignInError, match="cancelled"):
        chatgpt_auth.sign_in(open_browser=browser, path=tmp_path / "c.json", timeout=10)
