"""The AI behind the coach. The coach streams text from one of these; each speaks its own API.

Providers (Settings > AI coach):
- **claude:** the Anthropic API with a key. Claude Max and Ultra plans include API credits.
- **chatgpt:** your ChatGPT Plus or Pro plan, through OpenAI's official "Continue with ChatGPT"
  (chatgpt_auth.py) and the Responses API.
- **openai / gemini / openrouter:** an API key and their OpenAI-compatible endpoints. Gemini and
  OpenRouter have free tiers.
- **ollama:** a free model running on this PC (needs a vision model, e.g. `ollama pull gemma3`).

Anthropic's terms don't allow apps to sign in with a Claude Pro/Max subscription, so there's no
"Continue with Claude"; a Claude API key it is.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..config import CoachConfig

# US dollars per million tokens: (input, output, cache read, cache write), from Anthropic's price list (2026-11).
# For the app's "this match cost about..." line; other providers show tokens only.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
}


def cost_usd(model: str, usage: dict) -> float | None:
    """What one request cost, from its usage ({"in", "cached", "written", "out"} tokens); None if the model's
    price isn't known."""
    price = next((p for m, p in PRICES.items() if model.startswith(m)), None)
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    return (usage.get("in", 0) * p_in + usage.get("out", 0) * p_out + usage.get("cached", 0) * p_read
            + usage.get("written", 0) * p_write) / 1e6


# Models that accept server-side refusal fallbacks (Haiku does not).
FALLBACK_PREFIXES = ("claude-opus", "claude-fable", "claude-sonnet-5-5")
# Haiku 5.5 thinks before answering unless told not to, and live looks want the first word fast (a one-sentence
# tip and a few prepared calls don't need it). Turning it off is allowed up to effort "high".
NO_THINKING_MODELS = ("claude-haiku-5-5",)
NO_THINKING_EFFORTS = ("low", "medium", "high")
# Sonnet 5.5 thinks by default too, and refuses {"type": "disabled"} (a 400): {"type": "between_tools"} is how it
# answers without thinking first (accepted up to effort "high"). The death answer is read while the player waits to
# respawn: a real run on Sonnet 5.5 took 5.0 s to its first word and wrote 618 tokens (most of them thinking) for two
# sentences. The answer rests on Doty's own facts (how_you_died, death_cause_candidates), not on long reasoning.
BETWEEN_TOOLS_MODELS = ("claude-sonnet-5-5",)
FAST_KINDS = ("death",)


# The Anthropic SDK takes ~2 s to import (httpx2 ~0.15 s), so neither loads before Doty's window
# can show: they load on first use, or earlier in the background (preload, once the window is up).
def _anthropic():
    import anthropic

    return anthropic


def _httpx():
    import httpx2

    return httpx2


def preload() -> None:
    """Import the HTTP and AI libraries now (from a background thread) so the coach's first use doesn't wait."""
    _httpx()
    _anthropic()


@dataclass(frozen=True)
class Provider:
    key: str
    label: str  # as listed in Settings
    name: str = ""  # short, for messages ("Claude", "Google Gemini")
    base_url: str = ""
    key_env: str = ""  # API key environment variable ("" = no key)
    keys_url: str = ""  # where to get one
    prefer: tuple[str, ...] = ()  # model-name hints for picking a default (fast, sees images)


PROVIDERS = {p.key: p for p in (
    Provider("claude", "Claude · API key (Claude Console)", "Claude", key_env="ANTHROPIC_API_KEY",
             keys_url="https://console.anthropic.com/settings/keys"),
    Provider("chatgpt", "ChatGPT · your Plus or Pro plan (sign in)", "ChatGPT", base_url="https://api.openai.com/v1",
             prefer=("mini", "gpt-")),
    Provider("openai", "OpenAI · API key (OpenAI Platform)", "OpenAI", base_url="https://api.openai.com/v1",
             key_env="OPENAI_API_KEY", keys_url="https://platform.openai.com/api-keys", prefer=("mini", "gpt-")),
    Provider("gemini", "Google Gemini · API key (free tier)", "Google Gemini", key_env="GEMINI_API_KEY",
             base_url="https://generativelanguage.googleapis.com/v1beta/openai",
             keys_url="https://aistudio.google.com/apikey", prefer=("flash", "gemini")),
    Provider("openrouter", "OpenRouter · API key (free models)", "OpenRouter", base_url="https://openrouter.ai/api/v1",
             key_env="OPENROUTER_API_KEY", keys_url="https://openrouter.ai/settings/keys",
             prefer=("openrouter/auto",)),
    Provider("ollama", "Local model with Ollama (free, offline)", "Ollama", base_url="http://127.0.0.1:11434/v1",
             keys_url="https://ollama.com/download", prefer=("vl", "gemma", "llava", "llama3.2-vision")),
)}
# Listed in Settings but not selectable, with the reason: people look for it, and it shouldn't look forgotten.
UNAVAILABLE = [("Claude · Pro or Max plan (not allowed)",
                "Anthropic doesn't let other apps sign in with a Claude Pro or Max plan, so Claude needs an API "
                "key from the Claude Console. Max and Ultra plans include API credits you can use with one.")]


class BackendError(Exception):
    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal  # retrying won't help: bad key, not signed in, no model


def pick_model(models: list[str], prefer: tuple[str, ...]) -> str:
    """A sensible default from a provider's model list: the first match of each hint in turn."""
    for hint in prefer:
        hits = [m for m in models if hint in m.lower()]
        if hits:
            return sorted(hits, key=len)[0]
    return models[0] if models else ""


def _data_url(block: dict) -> str:
    src = block["source"]
    return f"data:{src.get('media_type', 'image/jpeg')};base64,{src['data']}"


class Backend:
    label = ""
    model = ""
    on_usage = None  # (model, usage) -> None after each request: the coach adds it to the match's tally

    async def check(self) -> None:
        """Cheap round trip at startup and every ~45 s: a bad key shows up now, and the connection stays warm."""

    def stream(self, system: str, content: list[dict], max_tokens: int, kind: str) -> AsyncIterator[str]:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass

    usage: dict = {}


# --------------------------------------------------------------------------- Claude


def claude_request(cfg: CoachConfig, system: str, content: list, kind: str, max_tokens: int = 0) -> dict:
    # Live looks run on the fast model (watch_model): a tip that lands sooner beats a slightly deeper one.
    model = cfg.watch_model if kind == "watch" and getattr(cfg, "watch_model", "") else cfg.model
    req = {
        "model": model,
        "max_tokens": max_tokens or (4000 if kind in ("plan", "start") else 2000),
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": {"effort": cfg.effort},
    }
    fast = kind == "watch" or kind in FAST_KINDS
    if fast and model.startswith(NO_THINKING_MODELS) and cfg.effort in NO_THINKING_EFFORTS:
        req["thinking"] = {"type": "disabled"}
    elif kind in FAST_KINDS and model.startswith(BETWEEN_TOOLS_MODELS) and cfg.effort in NO_THINKING_EFFORTS:
        # Exactly this and nothing else inside `thinking` (no display, no budget): only Sonnet 5.5 takes it, only at
        # effort "high" or below, and never with a per-message effort change (none is sent here).
        req["thinking"] = {"type": "between_tools"}
    if model.startswith(FALLBACK_PREFIXES):
        # The server-side refusal fallback stays on with between_tools too: under server-side-fallback-2026-07-01 a
        # between_tools request that falls back to Claude Sonnet 5 runs there with thinking disabled, handled by the
        # server. (Under -2026-06-01 the fallback entry would need its own `thinking`, or the request is rejected;
        # only a client-side retry or the SDK's fallback middleware has to drop between_tools itself.) Keep this
        # beta version if between_tools is sent.
        req["betas"] = ["server-side-fallback-2026-07-01"]
        req["fallbacks"] = "default"
    return req


class ClaudeBackend(Backend):
    label = "Claude"

    def __init__(self, cfg: CoachConfig) -> None:
        self.cfg, self.model = cfg, cfg.model
        self.usage = {}
        self.sdk = sdk = _anthropic()
        try:
            self.client = sdk.AsyncAnthropic(
                max_retries=1, timeout=sdk.Timeout(30.0, connect=5.0),
                # Keep idle connections around so the next ask skips the TCP + TLS handshake.
                http_client=sdk.DefaultAsyncHttpxClient(
                    limits=_httpx().Limits(max_keepalive_connections=4, keepalive_expiry=120.0)))
        except sdk.AnthropicError as exc:
            raise BackendError(str(exc), fatal=True) from exc

    async def check(self) -> None:
        sdk = self.sdk
        try:
            await self.client.models.retrieve(self.cfg.model)
        except TypeError as exc:  # the SDK raises this when no credentials resolve
            raise BackendError("no API key — set ANTHROPIC_API_KEY", fatal=True) from exc
        except sdk.AuthenticationError as exc:
            raise BackendError("bad API key", fatal=True) from exc
        except sdk.NotFoundError as exc:
            raise BackendError(f"unknown model {self.cfg.model}", fatal=True) from exc
        except sdk.AnthropicError as exc:
            raise BackendError(f"warm-up failed ({exc.__class__.__name__})") from exc

    async def prewarm(self, system: str, content: list, kind: str) -> None:
        """Write the prompt cache ahead of the first real request (a zero-token request: Claude reads the
        prompt and stops, billing only the cache write). Same model, thinking and effort as that request, or
        it wouldn't hit. Best effort: a failure here costs nothing but the head start."""
        req = claude_request(self.cfg, system, content, kind)
        req["max_tokens"] = 0
        try:
            reply = await self.client.beta.messages.create(**req)
        except Exception as exc:  # (never stops coaching)
            print(f"[doty] coach pre-warm skipped: {exc.__class__.__name__}")
            return
        u = getattr(reply, "usage", None)
        if u is not None and self.on_usage is not None:
            self.on_usage(req["model"], {"in": u.input_tokens or 0, "cached": u.cache_read_input_tokens or 0,
                                         "written": u.cache_creation_input_tokens or 0, "out": 0})

    async def stream(self, system, content, max_tokens, kind):
        sdk = self.sdk
        try:
            async with self.client.beta.messages.stream(**claude_request(self.cfg, system, content, kind,
                                                                         max_tokens)) as s:
                async for chunk in s.text_stream:
                    yield chunk
                final = await s.get_final_message()
        except (sdk.APIConnectionError, _httpx().HTTPError) as exc:
            # (the SDK passes a connection dropped mid-reply through as the HTTP library's own error)
            raise BackendError(f"Coach offline ({exc.__class__.__name__})") from exc
        except sdk.RateLimitError as exc:
            raise BackendError("Coach rate-limited; try again in a moment") from exc
        except sdk.APIStatusError as exc:
            raise BackendError(f"Coach error {exc.status_code}") from exc
        except TypeError as exc:
            raise BackendError("no API key — set ANTHROPIC_API_KEY", fatal=True) from exc
        if final.stop_reason == "refusal":
            raise BackendError("Coach declined that one")
        self.usage = {"in": final.usage.input_tokens, "cached": final.usage.cache_read_input_tokens or 0,
                      "written": final.usage.cache_creation_input_tokens or 0, "out": final.usage.output_tokens}
        if self.on_usage is not None:
            self.on_usage(final.model or "", self.usage)

    async def aclose(self) -> None:
        await self.client.close()


# --------------------------------------------------------------------------- OpenAI-compatible


class _HttpBackend(Backend):
    def __init__(self, provider: Provider, model: str, base_url: str = "") -> None:
        self.provider, self.label = provider, provider.name or provider.label
        self.base = (base_url or provider.base_url).rstrip("/")
        self.model = model
        self.usage = {}
        self.hx = hx = _httpx()
        self.http = hx.AsyncClient(timeout=hx.Timeout(60.0, connect=5.0),
                                   limits=hx.Limits(max_keepalive_connections=4, keepalive_expiry=120.0))

    async def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "User-Agent": "Doty"}

    async def list_models(self) -> list[str]:
        try:
            r = await self.http.get(f"{self.base}/models", headers=await self._headers())
        except self.hx.HTTPError as exc:
            raise BackendError(f"{self.label} isn't reachable ({exc.__class__.__name__})",
                               fatal=self.provider.key == "ollama") from exc
        self._raise_for(r.status_code, r.text)
        try:
            body = r.json()
        except ValueError as exc:  # (not an AI endpoint: a web page at that address)
            raise BackendError(f"{self.label} didn't answer like an AI service", fatal=True) from exc
        data = (body.get("data") or body.get("models") or []) if isinstance(body, dict) else []
        return [m.get("id") or m.get("name") for m in data
                if isinstance(m, dict) and m.get("visibility", "list") == "list"]

    async def check(self) -> None:
        if self.model and getattr(self, "_checked", False):
            return  # (checked once; looks every few seconds keep the connection warm, no need to list models again)
        models = await self.list_models()
        if not self.model:
            self.model = pick_model([m for m in models if m], self.provider.prefer)
        if not self.model:
            raise BackendError(f"no models available on {self.label}"
                               + (" (run: ollama pull gemma3)" if self.provider.key == "ollama" else ""), fatal=True)
        self._checked = True

    def _raise_for(self, status: int, body: str) -> None:
        if status < 400:
            return
        if status in (401, 403):
            raise BackendError(f"{self.label} didn't accept the key" if self.provider.key_env
                               else f"{self.label} sign-in expired; sign in again in Settings", fatal=True)
        if status == 404:
            raise BackendError(f"{self.label} has no model {self.model!r}", fatal=True)
        if status == 429:
            raise BackendError(f"{self.label} is busy or out of quota; try again in a moment")
        raise BackendError(f"{self.label} error {status}: {body[:120]}")

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _sse(self, url: str, body: dict) -> AsyncIterator[dict]:
        try:
            async with self.http.stream("POST", url, json=body, headers=await self._headers()) as r:
                if r.status_code >= 400:
                    self._raise_for(r.status_code, (await r.aread()).decode("utf-8", "replace"))
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
        except self.hx.HTTPError as exc:
            raise BackendError(f"Coach offline ({exc.__class__.__name__})") from exc


class OpenAIChatBackend(_HttpBackend):
    """Chat Completions with images as data URLs: OpenAI, Gemini, OpenRouter and Ollama all speak it."""

    def __init__(self, provider: Provider, model: str, key: str = "", base_url: str = "") -> None:
        super().__init__(provider, model, base_url)
        self.key = key

    async def _headers(self) -> dict[str, str]:
        h = await super()._headers()
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        if self.provider.key == "openrouter":
            h["HTTP-Referer"], h["X-Title"] = "https://github.com/doty-coach", "Doty"
        return h

    @staticmethod
    def convert(content: list[dict]) -> list[dict]:
        out = []
        for b in content:
            if b.get("type") == "image":
                out.append({"type": "image_url", "image_url": {"url": _data_url(b)}})
            elif b.get("type") == "text":
                out.append({"type": "text", "text": b["text"]})
        return out

    async def stream(self, system, content, max_tokens, kind):
        if not self.model:
            await self.check()
        body = {"model": self.model, "stream": True,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": self.convert(content)}]}
        body["max_completion_tokens" if self.provider.key == "openai" else "max_tokens"] = max_tokens
        async for event in self._sse(f"{self.base}/chat/completions", body):
            if event.get("error"):
                raise BackendError(f"{self.label}: {str(event['error'].get('message', event['error']))[:120]}")
            for choice in event.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    yield text
            if event.get("usage"):
                u = event["usage"]
                self.usage = {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}


class ChatGPTBackend(_HttpBackend):
    """The Responses API on your ChatGPT plan, with the token from "Continue with ChatGPT"."""

    def __init__(self, provider: Provider, model: str, token_source=None) -> None:
        super().__init__(provider, model)
        from . import chatgpt_auth

        self._token = token_source or chatgpt_auth.access_token

    async def _headers(self) -> dict[str, str]:
        from .chatgpt_auth import SignInError

        try:
            token = await asyncio.to_thread(self._token)
        except SignInError as exc:
            raise BackendError(str(exc), fatal=True) from exc
        return {**await super()._headers(), "Authorization": f"Bearer {token}"}

    @staticmethod
    def convert(content: list[dict]) -> list[dict]:
        out = []
        for b in content:
            if b.get("type") == "image":
                out.append({"type": "input_image", "image_url": _data_url(b)})
            elif b.get("type") == "text":
                out.append({"type": "input_text", "text": b["text"]})
        return out

    async def stream(self, system, content, max_tokens, kind):
        if not self.model:
            await self.check()
        body = {"model": self.model, "instructions": system, "stream": True, "store": False,
                "max_output_tokens": max_tokens,
                "input": [{"role": "user", "content": self.convert(content)}]}
        completed = False
        async for event in self._sse(f"{self.base}/responses", body):
            kind_ = event.get("type", "")
            if kind_ == "response.output_text.delta":
                yield event.get("delta", "")
            elif kind_ == "response.failed":
                code = ((event.get("response") or {}).get("error") or {}).get("code", "")
                if code == "subscription_sharing_usage_limit_exceeded":
                    raise BackendError("you've reached your ChatGPT plan's limit for Doty; it resets later")
                raise BackendError(f"ChatGPT couldn't answer ({code or 'failed'})",
                                   fatal=code == "subscription_sharing_usage_unavailable")
            elif kind_ == "response.incomplete":
                break  # hit the length cap: keep what arrived
            elif kind_ == "response.completed":
                completed = True
                u = (event.get("response") or {}).get("usage") or {}
                self.usage = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}
        if not completed:
            self.usage = {}


def make_backend(cfg: CoachConfig) -> Backend:
    provider = PROVIDERS.get(cfg.provider, PROVIDERS["claude"])
    if provider.key == "claude":
        return ClaudeBackend(cfg)
    if provider.key == "chatgpt":
        return ChatGPTBackend(provider, cfg.provider_model)
    key = os.environ.get(provider.key_env, "").strip() if provider.key_env else ""
    if provider.key_env and not key:
        raise BackendError(f"no {provider.name} API key yet (Settings > AI coach)", fatal=True)
    return OpenAIChatBackend(provider, cfg.provider_model, key, cfg.ollama_url if provider.key == "ollama" else "")
