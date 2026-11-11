"""Your own Dota profile: rank, recent matches, and how each game compares with other players of the hero.

- **Your account only:** it comes from Dota's game data (`player.accountid`), or you type it.
- **Data:** OpenDota's public API. Dota's "Expose Public Match Data" setting must be on, or OpenDota
  has nothing to show.
- **Performance score:** each game's GPM, XPM, last hits, kills, damage and healing per minute, placed
  on OpenDota's per-hero percentiles. The score is the average of those, 0-100. The percentiles are from
  normal games, so a Turbo game (gold and experience doubled) gets no score rather than a flattering one.
- **What you usually play:** the share of Turbo among those matches tells Doty which mode to expect
  (knowledge/mode.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import mode as game_mode
from .constants import OPENDOTA
from .stats import Stats

RANKS = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon", 5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
STEAM64_BASE = 76561197960265728
# recentMatches field -> benchmarks metric (per minute where the benchmark is)
METRICS = {"gold_per_min": "gold_per_min", "xp_per_min": "xp_per_min", "last_hits": "last_hits_per_min",
           "kills": "kills_per_min", "hero_damage": "hero_damage_per_min", "hero_healing": "hero_healing_per_min"}
PER_MINUTE = {"last_hits", "kills", "hero_damage", "hero_healing"}
FRESH_S = 300  # recent matches: refetch after five minutes


def account_from_gsi(player: dict) -> int:
    """Your account id from the GSI player block (accountid, or the 64-bit steamid)."""
    for key in ("accountid", "account_id"):
        try:
            if int(player.get(key) or 0):
                return int(player[key])
        except (TypeError, ValueError):
            pass
    try:
        steam = int(player.get("steamid") or 0)
    except (TypeError, ValueError):
        return 0
    return steam - STEAM64_BASE if steam > STEAM64_BASE else 0


def parse_account(text: str) -> int:
    """An account id, a 64-bit Steam id, or a Dotabuff/OpenDota/STRATZ profile link."""
    digits = "".join(ch if ch.isdigit() else " " for ch in text).split()
    for d in sorted(digits, key=len, reverse=True):
        n = int(d)
        if n > STEAM64_BASE:
            return n - STEAM64_BASE
        if 1000 <= n < 2 ** 32:
            return n
    return 0


def rank_name(rank_tier: int | None, leaderboard: int | None = None) -> str:
    from ..i18n import tr

    if not rank_tier:
        return tr("Unranked (or hidden)")
    medal, stars = divmod(int(rank_tier), 10)
    if medal == 8:
        return f"{tr('Immortal')} #{leaderboard}" if leaderboard else tr("Immortal")
    name = tr(RANKS.get(medal, "Unknown"))
    return f"{name} {stars}" if stars else name


@dataclass
class Game:
    match_id: int
    hero_id: int
    won: bool
    kills: int
    deaths: int
    assists: int
    gpm: int
    xpm: int
    last_hits: int
    duration_s: int
    start_time: int
    score: float | None  # 0-100 performance against players of the same hero (None: no benchmark, or Turbo)
    turbo: bool = False


def _percentile(value: float, table: list[dict]) -> float | None:
    """Where `value` falls in a benchmark table [{percentile, value}, ...] (sorted by percentile)."""
    if not table:
        return None
    below = [row["percentile"] for row in table if value >= row["value"]]
    return max(below) if below else 0.0


def performance(match: dict, bench: dict) -> float | None:
    result = (bench or {}).get("result") or {}
    minutes = max(1.0, match.get("duration", 0) / 60)
    scores = []
    for field, metric in METRICS.items():
        if field not in match or metric not in result:
            continue
        value = match[field] / minutes if field in PER_MINUTE else match[field]
        p = _percentile(value, result[metric])
        if p is not None:
            scores.append(p)
    return round(100 * sum(scores) / len(scores), 0) if scores else None


class Profile:
    def __init__(self, stats: Stats) -> None:
        self.stats = stats

    def player(self, account_id: int) -> dict:
        data = self.stats._cached(f"player_{account_id}.json", f"{OPENDOTA}/players/{account_id}", ttl=FRESH_S)
        return data if isinstance(data, dict) else {}

    def recent(self, account_id: int) -> list[Game]:
        rows = self.stats._cached(f"recent_{account_id}.json", f"{OPENDOTA}/players/{account_id}/recentMatches",
                                  ttl=FRESH_S)
        out = []
        for m in rows if isinstance(rows, list) else []:
            radiant = (m.get("player_slot", 0) or 0) < 128
            turbo = m.get("game_mode") == game_mode.OPENDOTA_TURBO
            bench = None if turbo else self.stats._cached(f"bench_{m['hero_id']}.json",
                                                          f"{OPENDOTA}/benchmarks?hero_id={m['hero_id']}")
            out.append(Game(m.get("match_id", 0), m.get("hero_id", 0), bool(m.get("radiant_win")) == radiant,
                            m.get("kills", 0), m.get("deaths", 0), m.get("assists", 0), m.get("gold_per_min", 0),
                            m.get("xp_per_min", 0), m.get("last_hits", 0), m.get("duration", 0),
                            m.get("start_time", 0), None if turbo else performance(m, bench), turbo))
        return out

    def usual_mode(self, account_id: int) -> str | None:
        """What this player usually plays, from their recent matches: "turbo", "normal", or None when
        there's nothing to go by (no public matches, or OpenDota can't be reached)."""
        rows = self.stats._cached(f"recent_{account_id}.json", f"{OPENDOTA}/players/{account_id}/recentMatches",
                                  ttl=FRESH_S)
        return game_mode.usual(rows)


def ago(ts: int, now: float | None = None) -> str:
    s = max(0, int((now or time.time()) - ts))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = s // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
