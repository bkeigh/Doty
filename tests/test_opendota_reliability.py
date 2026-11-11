"""OpenDota now and then answers HTTP 500 or 429 (seen in a real log). Doty asks again after a short wait, uses
the saved copy at once when it has one (a fresh one downloads in the background), and stays under the free
limit of 60 requests a minute."""

import asyncio
import json
import os
import time

from doty.knowledge import constants
from doty.knowledge.constants import Knowledge


class FakeSession:
    """aiohttp.ClientSession's get(), answering from a list of (status, body) per call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def get(self, url):
        status, body = self.answers.pop(0) if self.answers else (200, {"ok": True})
        self.calls += 1
        session = self

        class Resp:
            def __init__(self):
                self.status = status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                if status >= 400:
                    import aiohttp

                    raise aiohttp.ClientResponseError(None, (), status=status)

            async def json(self, content_type=None):
                return body

        session.last_url = url
        return Resp()


def _kb(tmp_path, answers, monkeypatch):
    monkeypatch.setattr(constants, "RETRY_DELAYS_S", (0.0, 0.0))
    kb = Knowledge()
    kb.cache_dir = tmp_path
    kb._session = FakeSession(answers)
    return kb


def test_a_busy_opendota_is_asked_again(tmp_path, monkeypatch):
    kb = _kb(tmp_path, [(500, None), (429, None), (200, {"a": 1})], monkeypatch)
    assert asyncio.run(kb._cached_json("x.json", "https://api/x")) == {"a": 1}
    assert kb._session.calls == 3 and json.loads((tmp_path / "x.json").read_text()) == {"a": 1}


def test_a_4xx_isnt_asked_again_and_nothing_is_kept(tmp_path, monkeypatch):
    kb = _kb(tmp_path, [(404, None), (200, {"a": 1})], monkeypatch)
    assert asyncio.run(kb._cached_json("x.json", "https://api/x")) is None
    assert kb._session.calls == 1 and not (tmp_path / "x.json").exists() and kb.fetch_failures == 1


def test_an_old_saved_copy_is_used_at_once_and_refreshed_behind(tmp_path, monkeypatch):
    kb = _kb(tmp_path, [(200, {"v": 2})], monkeypatch)
    old = tmp_path / "x.json"
    old.write_text(json.dumps({"v": 1}))
    os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))  # two days old

    async def run():
        first = await kb._cached_json("x.json", "https://api/x")  # no waiting on the network
        for _ in range(50):
            if not kb._refreshing:
                break
            await asyncio.sleep(0.01)
        return first

    assert asyncio.run(run()) == {"v": 1}
    assert json.loads(old.read_text()) == {"v": 2}  # the fresh copy is in for next time


def test_requests_are_paced_under_the_free_limit(tmp_path, monkeypatch):
    kb = _kb(tmp_path, [], monkeypatch)
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(constants, "REQUESTS_PER_MIN", 3)
    monkeypatch.setattr(constants.asyncio, "sleep", fake_sleep)

    async def run():
        for _ in range(4):
            await kb._pace()

    asyncio.run(run())
    assert len(waits) == 1 and 55 < waits[0] <= 60  # the 4th in a minute waits its turn
