"""Typed view over raw GSI payloads, plus per-match memory the rules can use."""

from __future__ import annotations

import re

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

IN_PROGRESS = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
DEMO_MATCH = "doty-sim"  # the scripted demo's match ids start with this
PRE_GAME = "DOTA_GAMERULES_STATE_PRE_GAME"
STRATEGY = "DOTA_GAMERULES_STATE_STRATEGY_TIME"
HERO_SELECTION = "DOTA_GAMERULES_STATE_HERO_SELECTION"
POST_GAME = "DOTA_GAMERULES_STATE_POST_GAME"

EMPTY = "empty"


def _get(d: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


@dataclass(frozen=True)
class Item:
    name: str
    slot: str
    charges: int = 0
    cooldown: float = 0.0
    can_cast: bool = False
    passive: bool = False

    @property
    def short(self) -> str:
        return self.name.removeprefix("item_")


@dataclass(frozen=True)
class Ability:
    name: str
    level: int
    cooldown: float
    ultimate: bool
    can_cast: bool
    passive: bool

    @property
    def is_talent_or_attr(self) -> bool:
        return self.name.startswith("special_bonus") or self.name.startswith("plus_")


class Snapshot:
    """One GSI payload. Every accessor tolerates missing sections (spectating,
    menus, older clients) and returns a neutral default."""

    def __init__(self, raw: dict[str, Any], received_t: float | None = None) -> None:
        self.raw = raw
        self.received_t = received_t if received_t is not None else time.perf_counter()

    # map -----------------------------------------------------------------
    @property
    def match_id(self) -> str:
        return str(_get(self.raw, "map", "matchid", default="") or "")

    @property
    def is_demo(self) -> bool:
        """Doty's own scripted demo match (sim.py), not a game you played."""
        return self.match_id.startswith(DEMO_MATCH)

    @property
    def clock(self) -> int:
        """In-game clock in seconds (negative before the horn)."""
        return int(_get(self.raw, "map", "clock_time", default=0) or 0)

    @property
    def game_time(self) -> int:
        """Separate engine counter; events are stamped with it, not the clock."""
        return int(_get(self.raw, "map", "game_time", default=0) or 0)

    def event_clock(self, ev: dict[str, Any]) -> int:
        """Convert an event's game_time stamp to the in-game clock."""
        if "game_time" not in ev:
            return self.clock
        return int(ev["game_time"]) - (self.game_time - self.clock)

    @property
    def game_state(self) -> str:
        return _get(self.raw, "map", "game_state", default="") or ""

    @property
    def in_game(self) -> bool:
        return self.game_state in (PRE_GAME, IN_PROGRESS)

    @property
    def in_draft(self) -> bool:
        """Hero selection or strategy time (before your hero is in the game)."""
        return self.game_state in (HERO_SELECTION, STRATEGY)

    @property
    def custom_game(self) -> bool:
        """An arcade (custom) game: none of Dota's own timings or rules apply."""
        name = str(_get(self.raw, "map", "customgamename", default="") or "").strip().lower()
        return bool(name) and "demo" not in name  # (Demo Hero mode may run as one: it's still Dota)

    @property
    def paused(self) -> bool:
        return bool(_get(self.raw, "map", "paused", default=False))

    @property
    def daytime(self) -> bool:
        return bool(_get(self.raw, "map", "daytime", default=True))

    @property
    def events(self) -> list[dict[str, Any]]:
        ev = _get(self.raw, "events", default=None)
        if ev is None:
            ev = _get(self.raw, "map", "events", default=None)
        return ev if isinstance(ev, list) else []

    # player --------------------------------------------------------------
    def player(self, key: str, default: Any = 0) -> Any:
        return _get(self.raw, "player", key, default=default)

    @property
    def team(self) -> str:
        return str(self.player("team_name", "") or "")

    @property
    def gold(self) -> int:
        return int(self.player("gold", 0) or 0)

    @property
    def deaths(self) -> int:
        return int(self.player("deaths", 0) or 0)

    # hero ----------------------------------------------------------------
    def hero(self, key: str, default: Any = None) -> Any:
        return _get(self.raw, "hero", key, default=default)

    @property
    def hero_name(self) -> str:
        return str(self.hero("name", "") or "")

    @property
    def hero_id(self) -> int:
        return int(self.hero("id", 0) or 0)

    @property
    def has_hero(self) -> bool:
        return self.hero_name.startswith("npc_dota_hero_")

    @property
    def level(self) -> int:
        return int(self.hero("level", 0) or 0)

    @property
    def alive(self) -> bool:
        return bool(self.hero("alive", True))

    @property
    def hp(self) -> int:
        return int(self.hero("health", 0) or 0)

    @property
    def max_hp(self) -> int:
        return int(self.hero("max_health", 0) or 0)

    @property
    def hp_pct(self) -> float:
        return self.hp / self.max_hp if self.max_hp else 1.0

    @property
    def mana_pct(self) -> float:
        mx = int(self.hero("max_mana", 0) or 0)
        return int(self.hero("mana", 0) or 0) / mx if mx else 1.0

    @property
    def position(self) -> tuple[int, int] | None:
        x, y = self.hero("xpos"), self.hero("ypos")
        if x is None or y is None:
            return None
        return int(x), int(y)

    @property
    def in_own_base(self) -> bool:
        """Rough fountain-area test from world coordinates (map is ~±8000)."""
        pos = self.position
        if pos is None:
            return False
        x, y = pos
        if self.team == "radiant":
            return x < -5200 and y < -4700
        if self.team == "dire":
            return x > 5000 and y > 4500
        return False

    @property
    def talents_taken(self) -> int:
        return sum(1 for i in range(1, 9) if self.hero(f"talent_{i}", False))

    # items ---------------------------------------------------------------
    def _items(self, prefix: str, count: int) -> list[Item]:
        out: list[Item] = []
        for i in range(count):
            slot = f"{prefix}{i}"
            data = _get(self.raw, "items", slot, default=None)
            if not isinstance(data, dict):
                continue
            name = data.get("name", EMPTY)
            if not name or name == EMPTY:
                continue
            out.append(Item(
                name=name,
                slot=slot,
                charges=int(data.get("charges", 0) or 0),
                cooldown=float(data.get("cooldown", 0) or 0),
                can_cast=bool(data.get("can_cast", False)),
                passive=bool(data.get("passive", False)),
            ))
        return out

    @property
    def inventory(self) -> list[Item]:
        return self._items("slot", 6)

    @property
    def backpack(self) -> list[Item]:
        return [it for it in self._items("slot", 9) if it.slot in ("slot6", "slot7", "slot8")]

    @property
    def stash(self) -> list[Item]:
        return self._items("stash", 6)

    @property
    def teleport(self) -> Item | None:
        found = self._items("teleport", 1)
        return found[0] if found else None

    @property
    def neutral(self) -> Item | None:
        found = self._items("neutral", 2)
        return found[0] if found else None

    def _consumed(self) -> list[str]:
        """Aghanim's Shard and a Scepter that was consumed (Roshan's blessing, Alchemist's gift): owned, though
        they're in no slot. Without this the Shard stays "the next item to buy" for the rest of the game."""
        out = []
        if self.hero("aghanims_shard", False):
            out.append("item_aghanims_shard")
        if self.hero("aghanims_scepter", False):
            out.append("item_ultimate_scepter")
            # With no Scepter in a slot, its effect comes from a consumed Blessing: owning that too keeps
            # "finish Aghanim's Blessing" from being the next item for the rest of the game.
            if not any(it.name == "item_ultimate_scepter" for it in self.inventory + self.backpack + self.stash):
                out.append("item_ultimate_scepter_2")
        return out

    def all_item_names(self) -> set[str]:
        names = {it.name for it in self.inventory + self.backpack + self.stash}
        for extra in (self.teleport, self.neutral):
            if extra:
                names.add(extra.name)
        names.update(self._consumed())
        return names

    def item_list(self) -> list[str]:
        """Every item you hold, repeats included (inventory, backpack and stash, plus a consumed Shard or
        Scepter): what counts towards building the next item (Knowledge.cost_left)."""
        held = [it.name for it in self.inventory + self.backpack + self.stash]
        return held + [name for name in self._consumed() if name not in held]

    def ready_items(self) -> frozenset[str]:
        """Items you could use right now: in your inventory (or the TP and neutral slots), off cooldown
        and castable. Backpack and stash items can't be used."""
        slots = self.inventory + [it for it in (self.teleport, self.neutral) if it]
        return frozenset(it.name for it in slots if it.can_cast and it.cooldown <= 0)

    def has_item(self, name: str) -> bool:
        return name in self.all_item_names()

    @property
    def has_items_section(self) -> bool:
        return isinstance(self.raw.get("items"), dict)

    # abilities -----------------------------------------------------------
    @property
    def abilities(self) -> list[Ability]:
        out: list[Ability] = []
        section = self.raw.get("abilities")
        if not isinstance(section, dict):
            return out
        slots = [k for k in section if re.fullmatch(r"ability\d+", str(k))]  # (skip any other key Dota adds)
        for key in sorted(slots, key=lambda k: int(k.removeprefix("ability"))):
            a = section[key]
            if not isinstance(a, dict):
                continue
            out.append(Ability(
                name=a.get("name", ""),
                level=int(a.get("level", 0) or 0),
                cooldown=float(a.get("cooldown", 0) or 0),
                ultimate=bool(a.get("ultimate", False)),
                can_cast=bool(a.get("can_cast", False)),
                passive=bool(a.get("passive", False)),
            ))
        return out

    @property
    def unspent_skill_points(self) -> int | None:
        """Hero level minus points spent (without hero data: Knowledge.unspent_points is the careful count).
        None if abilities aren't reported, or past level 16, where some levels give no point."""
        abilities = self.abilities
        if not abilities or not self.level or self.level > 16:
            return None
        spent = sum(a.level for a in abilities if not a.is_talent_or_attr)
        spent += self.talents_taken
        spent += int(self.hero("attributes_level", 0) or 0)
        return max(0, self.level - spent)


@dataclass
class HistoryPoint:
    t: float  # perf_counter
    clock: int
    hp_pct: float
    gold: int
    alive: bool
    flags: frozenset[str] = frozenset()  # disables on your hero (stunned, hexed, silenced...)


# What GSI says is on your hero, as the death summary names it (GSI's field on the right).
DISABLES = {"stunned": "stunned", "hexed": "hexed", "silenced": "silenced", "disarmed": "disarmed",
            "muted": "muted", "broken": "break"}
DEATH_WINDOW_S = 10.0  # the death summary looks this far back
# Items that get you out when you're jumped (escape, immunity, a dispel, a shield, a heal, or disabling the
# one killing you). Only these count as "ready but never pressed": a toggle (Armlet, Radiance) or a farming
# item (Midas, Soul Ring) being off cooldown says nothing about the death.
SAVES = {f"item_{n}" for n in (
    "black_king_bar", "manta", "lotus_orb", "glimmer_cape", "force_staff", "hurricane_pike", "cyclone",
    "wind_waker", "ghost", "ethereal_blade", "blink", "overwhelming_blink", "swift_blink", "arcane_blink",
    "invis_sword", "silver_edge", "shadow_amulet", "satanic", "blade_mail", "crimson_guard", "pipe", "mekansm",
    "guardian_greaves", "solar_crest", "pavise", "holy_locket", "sheepstick", "abyssal_blade", "orchid",
    "bloodthorn", "rod_of_atos", "gungir", "heavens_halberd", "disperser")}
WAND_CHARGES = 5  # a Magic Wand or Stick this full is a real heal you didn't use


def disables(snap: Snapshot) -> frozenset[str]:
    return frozenset(name for name, field_name in DISABLES.items() if snap.hero(field_name, False))


def death_summary(history: "list[HistoryPoint]", last_alive: Snapshot | None, died_t: float) -> dict[str, Any]:
    """What happened in your last seconds, from your own game data: how fast the HP went, what was on you,
    and the active items you had ready and never pressed. For the death look and the review."""
    points = [p for p in history if p.alive and died_t - DEATH_WINDOW_S <= p.t <= died_t]
    out: dict[str, Any] = {}
    if points:
        peak = max(points, key=lambda p: (p.hp_pct, p.t))  # the latest moment at the highest HP
        out["hp_before"] = f"{peak.hp_pct:.0%} HP {died_t - peak.t:.1f} s before dying"
        held: dict[str, float] = {}
        for a, b in zip(points, points[1:] + [None]):
            span = (b.t if b is not None else died_t) - a.t
            for flag in a.flags:
                held[flag] = held.get(flag, 0.0) + min(span, 1.0)  # (a gap in the feed isn't a 5 s stun)
        held = {k: round(v, 1) for k, v in held.items() if v >= 0.3}
        if held:
            out["disabled_s"] = held
    if last_alive is not None:
        unused = sorted(it.name for it in last_alive.inventory
                        if it.can_cast and it.cooldown <= 0
                        and (it.name in SAVES or (it.name in ("item_magic_wand", "item_magic_stick")
                                                  and it.charges >= WAND_CHARGES)))
        if unused:
            out["ready_but_unused"] = [n.removeprefix("item_") for n in unused]
        pos, team = last_alive.position, (last_alive.team or "").lower()
        if pos is not None and team in ("radiant", "dire"):
            depth = (pos[0] + pos[1]) if team == "radiant" else -(pos[0] + pos[1])
            out["where"] = "deep on the enemy's side" if depth > 1000 else (
                "near the river" if depth > -1000 else "on your own side")
        tp = last_alive.teleport
        out["tp_ready"] = tp is not None and tp.cooldown <= 0
    return out


class GameStateStore:
    """Holds the latest snapshot, a short history, and memory scoped to one match."""

    HISTORY = 100  # ~10 s at 10 Hz

    def __init__(self) -> None:
        self.current: Snapshot | None = None
        self.previous: Snapshot | None = None
        self.history: deque[HistoryPoint] = deque(maxlen=self.HISTORY)
        self.memory: dict[str, Any] = {}
        self.seen_events: set[tuple[Any, ...]] = set()
        self.match_id: str = ""
        self.just_died = False  # this update is the one where you died (a real death, see _note_death)
        self.just_respawned = False  # this update is the one where you came back from a real death
        self._last_alive: Snapshot | None = None
        self.generation = 0  # goes up with every new match (reset), even when Dota repeats the match id
        self._played = False  # this match got past hero selection (so a new draft means a new match)

    def ingest(self, raw: dict[str, Any], received_t: float | None = None) -> Snapshot:
        snap = Snapshot(raw, received_t)
        if snap.match_id and snap.match_id != self.match_id:
            self.reset(snap.match_id)
        elif self._new_game(snap):  # bot and local games may all say "0", or nothing
            self.reset(snap.match_id or self.match_id)
        if snap.game_state in (PRE_GAME, IN_PROGRESS, POST_GAME):
            self._played = True
        self.previous, self.current = self.current, snap
        self.just_died = self.just_respawned = False
        if snap.has_hero:
            self.history.append(HistoryPoint(snap.received_t, snap.clock, snap.hp_pct, snap.gold, snap.alive,
                                             disables(snap)))
            if self.previous is not None and self.previous.has_hero and self._ult_cast(self.previous, snap):
                self.memory["fight_t"] = snap.received_t  # an ultimate just went off: that's a fight, even at full HP
            self._note_death(snap)
        return snap

    COUNT_LAG_S = 2.0  # the death counter and the alive flag may arrive an update apart

    def _note_death(self, snap: Snapshot) -> None:
        """A real death: your death count went up while you're dead. Aegis and Wraith King's
        Reincarnation flip "alive" for a few seconds without one, so they don't count. Without a count in
        the feed, alive going to dead is a death."""
        mem = self.memory
        count = snap.player("deaths", None)
        count = None if count is None else int(count or 0)
        base = mem.get("deaths_alive")
        if snap.alive:
            if count is not None and base is not None and count > base:
                mem["count_ahead_t"] = snap.received_t  # the counter came first: "alive" should follow
            if count is not None:
                mem["deaths_alive"] = count
            self.just_respawned = bool(mem.get("death_open"))
            mem["death_open"] = False
            self._last_alive = snap
            return
        if mem.get("death_open"):
            return
        prev_alive = self.previous is not None and self.previous.has_hero and self.previous.alive
        if count is None:
            died = prev_alive
        else:
            died = (base is not None and count > base) or (
                snap.received_t - mem.get("count_ahead_t", -1e9) <= self.COUNT_LAG_S)
        if died:
            mem["death_open"] = True
            mem.pop("count_ahead_t", None)
            self.just_died = True
            mem["last_death"] = {"clock": snap.clock,
                                 **death_summary(list(self.history), self._last_alive, snap.received_t)}

    # An ultimate cast counts as a fight only if it's a fighting one: a long cooldown (Invoke and Rearm
    # come back in seconds), not cast from your base, and not one of these (farming, or cast across the map).
    FIGHT_ULT_MIN_CD = 40.0
    NOT_FIGHT_ULTS = frozenset({
        "alchemist_chemical_rage", "zuus_thundergods_wrath", "furion_wrath_of_nature",
        "ancient_apparition_ice_blast", "silencer_global_silence", "spectre_haunt", "invoker_invoke",
        "tinker_rearm", "keeper_of_the_light_spirit_form", "lone_druid_true_form", "terrorblade_metamorphosis",
        "troll_warlord_battle_trance", "naga_siren_song_of_the_siren", "wisp_relocate", "furion_teleportation",
        "monkey_king_wukongs_command", "shredder_chakram", "life_stealer_infest",
    })

    @classmethod
    def _ult_cast(cls, before: Snapshot, now: Snapshot) -> bool:
        if now.in_own_base:
            return False
        ready = {a.name for a in before.abilities if a.ultimate and a.level > 0 and a.cooldown <= 0}
        return any(a.name in ready and a.cooldown >= cls.FIGHT_ULT_MIN_CD and a.name not in cls.NOT_FIGHT_ULTS
                   for a in now.abilities if a.ultimate)

    BACKWARDS_S = 60  # the clock going back this far in a match means a new game (or a restarted demo)

    def _new_game(self, snap: Snapshot) -> bool:
        """A new game under the same (or no) match id: hero selection again after a game was played, or
        the clock jumping far back."""
        if self._played and snap.in_draft:
            return True
        last = self.current
        return (last is not None and last.in_game and snap.in_game and last.has_hero and snap.has_hero
                and snap.clock < last.clock - self.BACKWARDS_S)

    def reset(self, match_id: str = "") -> None:
        self.generation += 1
        self._played = False
        self.previous = self.current = None
        self._last_alive = None
        self.just_died = self.just_respawned = False
        self.history.clear()
        self.memory.clear()
        self.seen_events.clear()
        self.match_id = match_id

    def new_events(self, snap: Snapshot) -> list[dict[str, Any]]:
        """Events not seen before in this match (GSI resends a rolling list)."""
        fresh = []
        for ev in snap.events:
            key = (ev.get("game_time"), ev.get("event_type"), ev.get("player_id"), ev.get("team"))
            if key not in self.seen_events:
                self.seen_events.add(key)
                fresh.append(ev)
        return fresh

    FIGHT_DROP = 0.10  # HP lost (fraction of max) within FIGHT_WINDOW_S that means "you're in a fight"
    FIGHT_WINDOW_S = 3.0
    FIGHT_LINGER_S = 5.0  # and for this long after the last such drop

    def in_fight(self) -> bool:
        """Taking real damage right now, or did in the last few seconds. Advice that took seconds to
        think up (the AI coach) is usually stale by then, so it waits until the fight is over."""
        if not self.history:
            return False
        latest = self.history[-1]
        if not latest.alive:
            return False
        peak = max((p.hp_pct for p in self.history if latest.t - p.t <= self.FIGHT_WINDOW_S and p.alive),
                   default=latest.hp_pct)
        if peak - latest.hp_pct >= self.FIGHT_DROP:
            self.memory["fight_t"] = latest.t
            return True
        return latest.t - self.memory.get("fight_t", -1e9) < self.FIGHT_LINGER_S

    def hp_trend(self, seconds: float = 2.0) -> float:
        """Change in HP fraction over the last `seconds` of wall time (negative = losing HP)."""
        if len(self.history) < 2:
            return 0.0
        latest = self.history[-1]
        for point in reversed(self.history):
            if latest.t - point.t >= seconds:
                return latest.hp_pct - point.hp_pct
        return latest.hp_pct - self.history[0].hp_pct
