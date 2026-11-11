"""Local HTTP endpoint Dota 2 POSTs Game State Integration payloads to.

Who may talk to it: Dota, on this PC. The listener is on 127.0.0.1, but a web page open in the player's
browser can also reach 127.0.0.1, and the token in Dota's config file is no secret from it (the default is
in this source). So two more checks, neither of which Dota's own client trips:

- **No browsers.** A browser marks every cross-site request and every POST with an `Origin` header (and
  modern ones add `Sec-Fetch-*`). Dota's HTTP client sends neither. A request carrying them is refused.
- **Addressed to this PC.** With the listener on a loopback address, the `Host` header must name loopback.
  That stops "DNS rebinding", where a hostile site's own name is pointed at 127.0.0.1.

Without these a page could feed Doty a made-up match: wrong calls on screen, and AI looks billed to the
player's own key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

PayloadHandler = Callable[[dict[str, Any], float], None]
LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "::1")


def from_a_browser(headers) -> bool:
    """True for a request a web browser made (see the module docstring)."""
    return "Origin" in headers or any(name.lower().startswith("sec-fetch-") for name in headers)


def host_name(header: str) -> str:
    """The host part of a Host header: "127.0.0.1:53000" -> "127.0.0.1", "[::1]:53000" -> "[::1]"."""
    header = (header or "").strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if ":" in header else header


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

    def foreign(self, request: web.Request) -> bool:
        """A request that can't be Dota on this PC: from a browser, or addressed to some other name."""
        if from_a_browser(request.headers):
            return True
        host = host_name(request.headers.get("Host", ""))
        return bool(host) and self.host.lower() in LOOPBACK and host not in LOOPBACK

    def build_app(self) -> web.Application:
        @web.middleware
        async def local_only(request: web.Request, handler):
            if self.foreign(request):
                if not self.rejected:  # said once, so a surprise here can be traced: header names only
                    print("[doty] refused a request to the game-data port that didn't come from Dota on this PC "
                          f"(Host {request.headers.get('Host', '')!r}; headers: {sorted(request.headers)})")
                self.rejected += 1
                return web.Response(status=403)
            return await handler(request)

        app = web.Application(client_max_size=4 * 1024 * 1024, middlewares=[local_only])
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
