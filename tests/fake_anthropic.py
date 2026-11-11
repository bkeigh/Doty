"""A tiny stand-in for the Messages API's streaming endpoint (SSE), for tests."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web


class FakeAnthropic:
    def __init__(self, reply: str, ttft_s: float = 0.05, chunk_s: float = 0.005) -> None:
        self.reply = reply
        self.ttft_s = ttft_s
        self.chunk_s = chunk_s
        self.requests: list[dict] = []
        self.headers: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/v1/models/{model}", self._model)
        app.router.add_post("/v1/messages", self._messages)
        return app

    async def _model(self, request: web.Request) -> web.Response:
        return web.json_response({"id": request.match_info["model"], "type": "model",
                                  "display_name": "fake", "created_at": "2026-01-01T00:00:00Z"})

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if not body.get("stream"):  # a plain request (the cache pre-warm: max_tokens 0, nothing written)
            return web.json_response({
                "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                "stop_reason": "max_tokens", "stop_sequence": None,
                "usage": {"input_tokens": 1200, "output_tokens": 0, "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 1200}})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(event: str, data: dict) -> None:
            await resp.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())

        await send("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"], "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 900,
                      "cache_creation_input_tokens": 0}}})
        await send("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}})
        await asyncio.sleep(self.ttft_s)
        words = self.reply.split(" ")
        for i, word in enumerate(words):
            piece = word if i == len(words) - 1 else word + " "
            await send("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": piece}})
            await asyncio.sleep(self.chunk_s)
        await send("content_block_stop", {"type": "content_block_stop", "index": 0})
        await send("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                     "usage": {"output_tokens": len(words)}})
        await send("message_stop", {"type": "message_stop"})
        await resp.write_eof()
        return resp
