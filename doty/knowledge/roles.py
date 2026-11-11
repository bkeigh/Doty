"""Which role you're playing this game, worked out from your own game data.

Advice depends on the role more than on anything else: "farm the jungle until Black King Bar" is right for a
carry and wrong for a hard support, and "your last hits are behind" said to a support is noise. The game data
doesn't name the role, but it shows it:

- the lane you spend the first minutes in (your hero's position: the safe lane, the middle or the offlane);
- your last hits a minute there (a core farms; a support leaves the creeps to the core);
- what you bought before the horn (Sentry Wards and Blood Grenades are support purchases);
- what the hero usually plays (OpenDota's role tags: a first guess before the lanes are settled).

Positions follow the usual numbering: 1 safe-lane carry, 2 mid, 3 offlaner, 4 soft support, 5 hard support.
Only the player's own data is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import mapinfo

if TYPE_CHECKING:
    from ..state import GameStateStore, Snapshot
    from .constants import Knowledge

WATCH_FROM, WATCH_UNTIL = 30, 360  # game seconds over which the lane is watched (the laning stage)
SURE_FROM = 270  # from 4:30, with enough of the lane seen, the role is settled
MIN_SEEN_S = 45  # seconds in the lanes before the lane counts at all
SURE_SEEN_S = 120
CORE_LH_PER_MIN = 2.0  # a core has at least this many last hits a minute in lane...
CARRY_HERO_LH = 1.2  # ...less is enough on a hero that's nearly always a core,
SUPPORT_HERO_LH = 3.0  # and it takes more on a hero that's nearly always a support
SUPPORT_SURE_LH = 1.5  # a support hero under this is surely supporting (above it, it may be an offlane Sand King)
SUPPORT_ITEMS = frozenset({"item_ward_sentry", "item_blood_grenade", "item_ward_dispenser"})
NAMES = {1: "safe-lane carry", 2: "mid", 3: "offlaner", 4: "soft support", 5: "hard support"}


@dataclass(frozen=True)
class Role:
    position: int  # 1-5; 0 = not known yet
    lane: str  # safe | mid | off | roam | "" (not seen yet)
    core: bool | None  # farms (1-3) or supports (4-5); None = not known
    sure: bool  # settled from the lane and the farm, not just a first guess from the hero

    @property
    def support(self) -> bool:
        """Surely a support: the farm reminders don't apply."""
        return self.sure and self.core is False

    @property
    def label(self) -> str:
        """For the coach: "safe-lane carry (position 1)", "support (a first guess from the hero)"."""
        if self.position:
            text = f"{NAMES[self.position]} (position {self.position})"
        elif self.core is None:
            return ""
        else:
            text = "a core" if self.core else "a support"
        return text if self.sure else f"{text}, a first guess"


def _hero_lean(kb: "Knowledge | None", hero_npc: str) -> str:
    """'core' or 'support' when the hero's role tags say only one of the two (Juggernaut: Carry; Lion: Support),
    '' when they say both or neither (Mirana, Pudge)."""
    tags = set(((kb.heroes.get(hero_npc) if kb else None) or {}).get("roles") or [])
    if "Carry" in tags and "Support" not in tags:
        return "core"
    if "Support" in tags and "Carry" not in tags:
        return "support"
    return ""


def observe(snap: "Snapshot", store: "GameStateStore", kb: "Knowledge | None" = None) -> Role | None:
    """Call on every game-data update: watches the lane and keeps `store.memory["role"]` current. Cheap (a few
    comparisons; the role is worked out once per game second while it can still change)."""
    mem = store.memory
    if not snap.has_hero or not snap.in_game or snap.custom_game:
        return mem.get("role")
    track: dict[str, Any] = mem.get("role_track") or {}
    if not track:
        track = mem["role_track"] = {"seen": {"top": 0, "mid": 0, "bottom": 0, "jungle": 0}, "clock": None,
                                     "support_items": False, "done": False}
    if track["done"] or snap.clock == track["clock"]:
        return mem.get("role")
    track["clock"] = snap.clock
    if snap.clock < 90 and not track["support_items"]:
        track["support_items"] = bool(snap.all_item_names() & SUPPORT_ITEMS)
    pos = snap.position
    if (WATCH_FROM <= snap.clock <= WATCH_UNTIL and snap.alive and pos is not None and not snap.in_own_base
            and not snap.paused):
        track["seen"][mapinfo.band(pos)] += 1
    role = decide(snap, track, kb)
    if role is not None:
        mem["role"] = role
    if snap.clock > WATCH_UNTIL:
        track["done"] = True  # the laning stage is over: the role stands for the rest of the game
    return mem.get("role")


def decide(snap: "Snapshot", track: dict[str, Any], kb: "Knowledge | None" = None) -> Role | None:
    """The role as far as it can be told now (None: nothing to go on yet)."""
    lean = _hero_lean(kb, snap.hero_name)
    seen: dict[str, int] = track["seen"]
    total = sum(seen.values())
    if total < MIN_SEEN_S:  # before the lanes: the hero and the starting items
        if track["support_items"] or lean == "support":
            return Role(0, "", False, False)
        if lean == "core":
            return Role(0, "", True, False)
        return None
    where = max(seen, key=lambda k: seen[k])
    lane = mapinfo.lane_role(where, snap.team) or "roam"
    minutes = max(1.0, snap.clock / 60.0)
    rate = int(snap.player("last_hits", 0) or 0) / minutes
    need = CARRY_HERO_LH if lean == "core" else SUPPORT_HERO_LH if lean == "support" else CORE_LH_PER_MIN
    if track["support_items"]:
        need += 1.0
    core = rate >= need
    if lane == "mid":
        core = core or not track["support_items"]  # the middle lane is a core's, even on a bad start
    if lean == "core" and not track["support_items"]:
        core = True  # a carry hero having a rough lane is still the carry: the one who most needs the farm tips
    position = {("safe", True): 1, ("mid", True): 2, ("off", True): 3, ("roam", True): 3,
                ("safe", False): 5, ("mid", False): 4, ("off", False): 4, ("roam", False): 4}[(lane, core)]
    sure = snap.clock >= SURE_FROM and total >= SURE_SEEN_S
    if not core:
        # Few last hits alone don't make a support (a core who's losing the lane has few too). It takes a
        # sign of supporting: support purchases, or a support hero hardly touching the creeps. Without one it
        # stays a guess, and a guess silences nothing.
        sure = sure and (track["support_items"] or (lean == "support" and rate < SUPPORT_SURE_LH))
    return Role(position, lane, core, sure)
