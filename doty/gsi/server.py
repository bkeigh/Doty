"""Local HTTP endpoint Dota 2 POSTs Game State Integration payloads to."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

PayloadHandler = Callable[[dict[str, Any], float], None]


class GsiServer:
    def __init__(self, host: str, port: int, auth_token: str, on_payload: PayloadHandler,
                 record_dir: str = "") -> None:
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self.on_payload = on_payload
        self.rejected = 0
        self.received = 0
        self.last_payload_t: float | None = None
        self._record_fh = None
        if record_dir:
            path = Path(record_dir)
            path.mkdir(parents=True, exist_ok=True)
            self._record_fh = (path / time.strftime("gsi-%Y%m%d-%H%M%S.jsonl")).open("a", encoding="utf-8")
        self._runner: web.AppRunner | None = None

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_post("/", self._handle)
        app.router.add_get("/health", self._health)
        return app

    async def _handle(self, request: web.Request) -> web.Response:
        received_t = time.perf_counter()
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400)
        token = (payload.get("auth") or {}).get("token")
        if self.auth_token and token != self.auth_token:
            self.rejected += 1
            return web.Response(status=401)
        self.received += 1
        self.last_payload_t = received_t
        if self._record_fh:
            self._record_fh.write(json.dumps({"t": time.time(), "payload": payload}) + "\n")
            self._record_fh.flush()
        # Handlers run inline: the rules engine is a few ms, and Dota waits for
        # our 200 before sending the next update anyway.
        self.on_payload(payload, received_t)
        return web.Response(status=200)

    async def _health(self, _request: web.Request) -> web.Response:
        age = None if self.last_payload_t is None else round(time.perf_counter() - self.last_payload_t, 2)
        return web.json_response({"received": self.received, "rejected": self.rejected,
                                  "seconds_since_last": age})

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._record_fh:
            self._record_fh.close()
