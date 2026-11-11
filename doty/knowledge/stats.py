"""Public Dota statistics for drafts and builds (blocking calls: use from a worker thread).

- **OpenDota aggregates:** hero win and pick rates per rank, hero-vs-hero results and item
  popularity by game phase, from millions of public matches.
- **Ability data:** from dotaconstants (cooldowns, BKB-piercing, dispellable).
- **Cached** on disk for a day, in the same cache as `Knowledge`, so the app and the engine share
  downloads.
- **Hero-level only:** nothing here identifies or looks up the players in your lobby. Valve hides
  who's in your game until picks end, and Doty respects that.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR
from .constants import CONSTANTS_BASE, HERO_ALIASES, OPENDOTA, _norm

CACHE_TTL_S = 24 * 3600
BRACKETS = {0: "All ranks", 1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon", 5: "Legend", 6: "Ancient",
            7: "Divine", 8: "Immortal"}
ATTR_NAMES = {"str": "Strength", "agi": "Agility", "int": "Intelligence", "all": "Universal"}


class Stats:
    def __init__(self, cache_dir: Path = CACHE_DIR, fetch=None) -> None:
        self.cache_dir = cache_dir
        self._fetch = fetch or _http_json
        self.heroes: dict[int, dict[str, Any]] = {}  # id -> heroStats row (names, roles, per-rank picks/wins)
        self.abilities: dict[str, dict[str, Any]] = {}
        self.hero_abilities: dict[str, dict[str, Any]] = {}  # npc name -> {"abilities": [...], "talents": [...]}
        self.items: dict[str, dict[str, Any]] = {}  # dotaconstants key (no item_ prefix) -> item
        self._matchups: dict[int, list[dict[str, Any]]] = {}
        self._items_pop: dict[int, dict[str, Any]] = {}
        self._index: dict[str, int] = {}
        self.loaded = False
        self.error = ""

    # loading ---------------------------------------------------------------------
    def load(self) -> bool:
        rows = self._cached("heroStats.json", f"{OPENDOTA}/heroStats")
        if isinstance(rows, list):
            self.heroes = {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}
        self.abilities = self._cached("abilities.json", f"{CONSTANTS_BASE}/abilities.json") or {}
        self.hero_abilities = self._cached("hero_abilities.json", f"{CONSTANTS_BASE}/hero_abilities.json") or {}
        self.items = self._cached("items.json", f"{CONSTANTS_BASE}/items.json") or {}
        self._index = {}
        for hid, h in self.heroes.items():
            for key in (h.get("localized_name", ""), h.get("name", "").removeprefix("npc_dota_hero_")):
                self._index[_norm(key)] = hid
        by_npc = {h.get("name"): hid for hid, h in self.heroes.items()}
        for alias, npc in HERO_ALIASES.items():
            if npc in by_npc:
                self._index.setdefault(_norm(alias), by_npc[npc])
        self.loaded = bool(self.heroes)
        self.error = "" if self.loaded else "couldn't load hero statistics (offline?)"
        return self.loaded

    def has_matchups(self, hero_id: int) -> bool:
        """True once `matchups(hero_id)` answers without going to the network."""
        return hero_id in self._matchups

    def matchups(self, hero_id: int) -> list[dict[str, Any]]:
        """Every hero's results against `hero_id`'s team, from `hero_id`'s side."""
        if hero_id not in self._matchups:
            data = self._cached(f"matchups_{hero_id}.json", f"{OPENDOTA}/heroes/{hero_id}/matchups")
            self._matchups[hero_id] = data if isinstance(data, list) else []
        return self._matchups[hero_id]

    def item_popularity(self, hero_id: int) -> dict[str, Any]:
        if hero_id not in self._items_pop:
            data = self._cached(f"itempop_{hero_id}.json", f"{OPENDOTA}/heroes/{hero_id}/itemPopularity")
            self._items_pop[hero_id] = data if isinstance(data, dict) else {}
        return self._items_pop[hero_id]

    def _cached(self, name: str, url: str, ttl: float = CACHE_TTL_S) -> Any:
        path = self.cache_dir / name
        if path.is_file() and time.time() - path.stat().st_mtime < ttl:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        try:
            data = self._fetch(url)
            try:  # through a temporary file, like Knowledge: the two share these files
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                part = path.with_name(path.name + ".stats-part")
                part.write_text(json.dumps(data), encoding="utf-8")
                part.replace(path)
            except OSError as exc:
                print(f"[doty] couldn't save {name} to the cache ({exc.__class__.__name__})")
            return data
        except Exception as exc:  # offline, rate-limited: stale beats nothing
            print(f"[doty] couldn't fetch {url} ({exc.__class__.__name__}); using cached data if any")
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
            return None

    # lookups ---------------------------------------------------------------------
    def find(self, name: str) -> int | None:
        return self._index.get(_norm(name)) if name else None

    def name(self, hero_id: int) -> str:
        return self.heroes.get(hero_id, {}).get("localized_name", f"Hero {hero_id}")

    def hero_names(self) -> list[str]:
        return sorted(h.get("localized_name", "") for h in self.heroes.values())

    def win_rate(self, hero_id: int, bracket: int = 0) -> tuple[float, int]:
        """(win rate, games) at this rank (1 Herald .. 8 Immortal), or across all public ranks."""
        h = self.heroes.get(hero_id, {})
        if bracket:  # ("or 0": OpenDota sometimes sends null for a count)
            games, wins = h.get(f"{bracket}_pick") or 0, h.get(f"{bracket}_win") or 0
        else:
            games, wins = h.get("pub_pick") or 0, h.get("pub_win") or 0
        return (wins / games if games else 0.5), games

    def pick_rate(self, hero_id: int, bracket: int = 0) -> float:
        key = f"{bracket}_pick" if bracket else "pub_pick"
        total = sum(h.get(key) or 0 for h in self.heroes.values())
        return 10 * (self.heroes.get(hero_id, {}).get(key) or 0) / total if total else 0.0  # 10 heroes a game

    def is_core(self, hero_id: int) -> bool:
        roles = self.heroes.get(hero_id, {}).get("roles", [])
        return "Carry" in roles or ("Support" not in roles)

    def is_support(self, hero_id: int) -> bool:
        return "Support" in self.heroes.get(hero_id, {}).get("roles", [])

    def item_name(self, key: str) -> str:
        key = key.removeprefix("item_")
        return self.items.get(key, {}).get("dname") or key.replace("_", " ").title()

    def item_cost(self, key: str) -> int:
        return int(self.items.get(key.removeprefix("item_"), {}).get("cost") or 0)


RETRY_DELAYS_S = (1.0, 3.0)  # OpenDota is asked again after these when it's busy (HTTP 5xx, 429, a timeout)


def _http_json(url: str) -> Any:
    last: Exception | None = None
    for delay in (0.0, *RETRY_DELAYS_S):
        if delay:
            time.sleep(delay)  # (this runs off the UI thread)
        req = urllib.request.Request(url, headers={"User-Agent": "Doty (open-source Dota 2 coach)"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise  # (a 4xx: asking again won't change it)
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
    raise last if last else RuntimeError("no answer")
