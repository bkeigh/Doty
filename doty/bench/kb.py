"""Game knowledge for the bench, from Doty's cache folder only: no downloads, ever.

The app's Knowledge fetches from dotaconstants and OpenDota and refreshes old files in the background. A bench run
must not touch the network (and must give the same answer twice), so this reads whatever the cache holds, however
old, and treats a missing file as no data: the checks that need it then say "not applicable".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Iterable

from ..config import CACHE_DIR
from ..knowledge.constants import Knowledge


class CachedKnowledge(Knowledge):
    async def _cached_json(self, name: str, url: str, ttl: float = 0) -> Any:
        try:
            return json.loads((self.cache_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def load_kb(cache_dir: Path | None = None, hero_ids: Iterable[int] = ()) -> Knowledge:
    """Items, heroes and abilities from the cache, plus each hero's builds and timings when cached."""
    kb = CachedKnowledge(Path(cache_dir) if cache_dir else CACHE_DIR)

    async def go() -> None:
        await kb.load()
        for hero_id in sorted({int(h) for h in hero_ids if h}):
            await kb.ensure_hero(hero_id)
    asyncio.run(go())
    return kb


def hero_ids_in(payloads: Iterable[tuple[float, dict]]) -> set[int]:
    ids = set()
    for _t, payload in payloads:
        hero = payload.get("hero") if isinstance(payload, dict) else None
        if isinstance(hero, dict) and hero.get("id"):
            ids.add(int(hero["id"]))
    return ids
