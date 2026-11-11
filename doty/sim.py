"""A scripted fake match that POSTs realistic GSI payloads, for testing without Dota.

The script exercises every rule: pre-game timers, an unspent skill point, a
losing fight (low HP -> death), a missing TP after respawn, items sitting in
the stash, a Roshan kill + Aegis, and gold piling up. Game time runs `speed`x
faster than real time; payloads go out at Dota's real cadence (10/s).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from .state import IN_PROGRESS, POST_GAME, PRE_GAME

HERO = ("npc_dota_hero_juggernaut", 8)
ABILITIES = ["juggernaut_blade_fury", "juggernaut_healing_ward", "juggernaut_blade_dance", "juggernaut_omni_slash"]
# Skill order by level (index into ABILITIES); level 10/15 talents are skipped for brevity.
SKILL_ORDER = [0, 2, 0, 2, 0, 3, 0, 2, 2, 1, 3, 1, 1, 1, 3, 3]
LANE_POS = (-1800, -6400)  # Radiant safe lane
FOUNTAIN = (-6900, -6400)


@dataclass
class SimState:
    clock: float = -90.0
    gold: float = 600.0
    level: int = 1
    skills: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    hp_pct: float = 1.0
    alive: bool = True
    respawn_at: float | None = None
    deaths: int = 0
    kills: int = 0
    last_hits: int = 0
    inventory: list[str] = field(default_factory=list)
    stash: list[str] = field(default_factory=list)
    has_tp: bool = True
    events: list[dict[str, Any]] = field(default_factory=list)
    pos: tuple[int, int] = FOUNTAIN


class ScriptedMatch:
    def __init__(self, match_id: str = "doty-sim-1", end_clock: int = 26 * 60 + 30) -> None:
        self.s = SimState()
        self.match_id = match_id
        self.end_clock = end_clock
        self._done: set[str] = set()

    def _once(self, key: str, when: bool) -> bool:
        if when and key not in self._done:
            self._done.add(key)
            return True
        return False

    def step(self, dt: float) -> None:
        s = self.s
        s.clock += dt
        c = s.clock

        if self._once("buy_start", c >= -85):
            s.inventory = ["item_tango", "item_quelling_blade", "item_branches", "item_branches", "item_slippers"]
            s.gold -= 520
        if c > -80 and c < 0:
            s.pos = LANE_POS

        if c >= 0 and s.alive:
            s.gold += dt * 7.5  # ~450 GPM
            s.last_hits = int(max(0, c) / 60 * 5)

        # Leveling: one level per ~75 s; skill points spent 2 s later, except
        # level 3 which is left unspent for a while to trigger the reminder.
        target_level = min(25, 1 + int(max(0.0, c + 60) / 75))
        if target_level > s.level:
            s.level = target_level
        spent = sum(s.skills)
        if spent < min(s.level, len(SKILL_ORDER)):
            hold = spent == 2 and c < 200
            if not hold and self._once(f"skill{spent}", True):
                s.skills[SKILL_ORDER[spent]] += 1

        # A losing fight at 4:30: HP drains, then death at 4:46, respawn at 5:10.
        if 270 <= c < 286:
            s.hp_pct = max(0.08, 0.9 - (c - 270) * 0.05)
        elif self._once("die", c >= 286):
            s.alive, s.hp_pct, s.deaths = False, 0.0, s.deaths + 1
            s.respawn_at = c + 24
            s.has_tp = False
            s.gold -= 200
        elif s.alive:
            s.hp_pct = min(1.0, s.hp_pct + dt * 0.01)
        if not s.alive and s.respawn_at is not None and c >= s.respawn_at:
            s.alive, s.hp_pct, s.respawn_at, s.pos = True, 1.0, None, FOUNTAIN
        if s.alive and s.pos == FOUNTAIN and c > 310:
            s.pos = LANE_POS

        if self._once("phase", c >= 400):
            s.inventory = ["item_phase_boots", "item_quelling_blade", "item_magic_wand", "item_wraith_band"]
            s.gold -= 1600
        if self._once("stash_in", c >= 480):
            s.stash = ["item_cornucopia"]
            s.gold -= 1200
        if self._once("stash_out", c >= 640):
            s.stash = []
            s.inventory = s.inventory + ["item_cornucopia"]
        if self._once("aegis_expire", c >= 1205):
            s.inventory = [i for i in s.inventory if i != "item_aegis"]
        if self._once("tp", c >= 700):
            s.has_tp = True

        if self._once("rosh", c >= 900):  # (the first Roshan at 15:00, as in a real public game)
            s.events.append({"game_time": int(c) + 90, "clock_time": int(c), "event_type": "roshan_killed",
                             "team": "radiant", "killed_by_player_id": 0})
        if self._once("aegis_get", c >= 905):
            s.events.append({"game_time": int(c) + 90, "clock_time": int(c), "event_type": "aegis_picked_up",
                             "player_id": 0, "snatched": False})
            s.inventory = s.inventory + ["item_aegis"]

    def payload(self, token: str) -> dict[str, Any]:
        s = self.s
        c = int(s.clock)
        if c >= self.end_clock:
            state = POST_GAME
        else:
            state = PRE_GAME if c < 0 else IN_PROGRESS
        max_hp = 620 + s.level * 45
        hero = {
            "xpos": s.pos[0], "ypos": s.pos[1], "id": HERO[1], "name": HERO[0], "level": s.level,
            "xp": 0, "alive": s.alive, "respawn_seconds": int(max(0, (s.respawn_at or c) - c)),
            "buyback_cost": 200 + s.level * 40, "buyback_cooldown": 0,
            "health": int(max_hp * s.hp_pct), "max_health": max_hp, "health_percent": int(s.hp_pct * 100),
            "mana": 300, "max_mana": 300, "mana_percent": 100,
            "silenced": False, "stunned": False, "disarmed": False, "magicimmune": False, "hexed": False,
            "muted": False, "break": False, "aghanims_scepter": False, "aghanims_shard": False,
            "smoked": False, "has_debuff": False,
            **{f"talent_{i}": False for i in range(1, 9)},
        }
        abilities = {
            f"ability{i}": {"name": name, "level": s.skills[i], "can_cast": s.skills[i] > 0,
                            "passive": name == "juggernaut_blade_dance", "ability_active": True,
                            "cooldown": 0, "ultimate": i == 3}
            for i, name in enumerate(ABILITIES)
        }
        items: dict[str, Any] = {}
        for i in range(9):
            name = s.inventory[i] if i < len(s.inventory) else "empty"
            items[f"slot{i}"] = {"name": name} if name == "empty" else {
                "name": name, "purchaser": 0, "can_cast": False, "cooldown": 0, "passive": True, "charges": 0}
        for i in range(6):
            name = s.stash[i] if i < len(s.stash) else "empty"
            items[f"stash{i}"] = {"name": name}
        items["teleport0"] = ({"name": "item_tpscroll", "charges": 1, "can_cast": True, "cooldown": 0}
                              if s.has_tp else {"name": "empty"})
        items["neutral0"] = {"name": "empty"}
        return {
            "provider": {"name": "Dota 2", "appid": 570, "version": 47, "timestamp": int(time.time())},
            "map": {
                "name": "start", "matchid": self.match_id, "game_time": c + 90, "clock_time": c,
                "daytime": c < 0 or (c // 300) % 2 == 0, "nightstalker_night": False,
                "radiant_score": s.kills, "dire_score": s.deaths, "game_state": state, "paused": False,
                "win_team": "radiant" if state == POST_GAME else "none", "customgamename": "", "ward_purchase_cooldown": 0,
            },
            "player": {
                "steamid": "0", "accountid": "0", "name": "Doty Sim", "activity": "playing",
                "kills": s.kills, "deaths": s.deaths, "assists": 0, "last_hits": s.last_hits, "denies": 0,
                "kill_streak": 0, "commands_issued": 0, "team_name": "radiant", "player_slot": 0,
                "gold": int(s.gold), "gold_reliable": 0, "gold_unreliable": int(s.gold),
                "gpm": 450 if c > 0 else 0, "xpm": 500 if c > 0 else 0,
            },
            "hero": hero,
            "abilities": abilities,
            "items": items,
            "events": list(s.events),
            "auth": {"token": token},
        }


async def run_sim(url: str, token: str, speed: float = 6.0, end_clock: int = 26 * 60 + 30,
                  hz: float = 10.0, verbose: bool = True) -> dict[str, float]:
    match = ScriptedMatch(end_clock=end_clock)
    interval = 1.0 / hz
    sent = 0
    rtts: list[float] = []
    async with aiohttp.ClientSession() as session:
        while match.s.clock < end_clock + 2:
            match.step(interval * speed)
            t0 = time.perf_counter()
            try:
                async with session.post(url, json=match.payload(token)) as resp:
                    await resp.read()
                    if resp.status == 401:
                        raise SystemExit("[sim] server rejected the auth token")
            except aiohttp.ClientConnectorError:
                raise SystemExit(f"[sim] nothing listening at {url} — start `doty run` first")
            rtts.append((time.perf_counter() - t0) * 1000.0)
            sent += 1
            await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))
    rtts.sort()
    stats = {"payloads": sent, "p50_ms": rtts[len(rtts) // 2], "p99_ms": rtts[int(len(rtts) * 0.99)],
             "max_ms": rtts[-1]}
    if verbose:
        print(f"[sim] sent {sent} payloads; server round-trip p50={stats['p50_ms']:.1f} ms "
              f"p99={stats['p99_ms']:.1f} ms max={stats['max_ms']:.1f} ms")
    return stats


async def replay_file(path: Path, url: str, speed: float = 1.0) -> None:
    """Replay a recording made with gsi.record_dir, preserving (scaled) timing."""
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return
    async with aiohttp.ClientSession() as session:
        start_wall, start_rec = time.perf_counter(), lines[0]["t"]
        for entry in lines:
            due = (entry["t"] - start_rec) / speed
            await asyncio.sleep(max(0.0, due - (time.perf_counter() - start_wall)))
            async with session.post(url, json=entry["payload"]) as resp:
                await resp.read()
