"""Map-objective timings for the current patch.

All times are in-game clock seconds (GSI map.clock_time). Keep this file the
single source of truth: the timer rules and the coach's system prompt both
read it, so a patch update is a one-file change. Sources for each number are
in docs/RESEARCH.md (Valve patch-note JSON + Liquipedia, checked 2026-11-04).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

PATCH = "7.41f"


@dataclass(frozen=True)
class Spawn:
    key: str
    label: str  # spoken/displayed name, e.g. "Power rune"
    first: int  # first spawn (clock seconds)
    every: int | None  # repeat interval; None = one-off
    until: int | None = None  # last spawn (inclusive), None = forever
    lead_setting: str = "rune_lead_s"  # which RulesConfig field gives the warning lead
    toggle: str | None = None  # RulesConfig bool that must be True to announce
    priority: int = 3  # advice.Priority value

    def times_after(self, clock: int, horizon: int) -> list[int]:
        """Spawn times t with clock < t <= clock + horizon."""
        out = []
        if self.every is None:
            if clock < self.first <= clock + horizon:
                out.append(self.first)
            return out
        n = max(0, -(-(clock + 1 - self.first) // self.every))  # ceil((clock+1-first)/every)
        t = self.first + n * self.every
        while t <= clock + horizon:
            if self.until is None or t <= self.until:
                out.append(t)
            t += self.every
        return out


SPAWNS: list[Spawn] = [
    # 0:00 set at the river spots, then every 4:00 at the two jungle spots (they stack).
    Spawn("bounty", "Bounty runes", first=0, every=240, toggle="announce_bounty_runes"),
    Spawn("water", "Water runes", first=120, every=120, until=240),
    Spawn("power", "Power rune", first=360, every=120),
    # Wisdom runes became Shrines of Wisdom in 7.38: stand in range 3s for XP.
    Spawn("wisdom", "Wisdom shrines", first=420, every=420),
    # A lotus grows every 3:00 into a free slot (max 6), so missing one isn't fatal.
    Spawn("lotus", "Lotus pools", first=180, every=180, toggle="announce_bounty_runes", priority=4),
    Spawn("tormentor", "Tormentors", first=1200, every=None),
    # Madstone neutral-item tiers (T1 is available from 0:00).
    Spawn("neutral_t2", "Tier 2 neutral items", first=900, every=None, priority=4),
    Spawn("neutral_t3", "Tier 3 neutral items", first=1500, every=None, priority=4),
    Spawn("neutral_t4", "Tier 4 neutral items", first=2100, every=None, priority=4),
    Spawn("neutral_t5", "Tier 5 neutral items", first=3600, every=None, priority=4),
    # Camps respawn on the minute from 1:00; pull at about x:53-x:55 to stack.
    Spawn("stack", "Stack camps", first=60 + 53, every=60, lead_setting="stack_lead_s", toggle="announce_stacks"),
]

DAY_NIGHT_CYCLE_S = 600  # 5:00 day (from 0:00) then 5:00 night

ROSHAN_RESPAWN_MIN_S = 480
ROSHAN_RESPAWN_MAX_S = 660
ROSHAN_ROAMS_FROM = 900  # stays in its starting pit until 15:00
AEGIS_DURATION_S = 300
TORMENTOR_RESPAWN_S = 600
BUYBACK_COOLDOWN_S = 480
GLYPH_COOLDOWN_S = 300

# Turbo (knowledge/mode.py has the rules and where they come from): runes, lotus, shrines and stacks keep
# their times; neutral tiers arrive twice as fast; the Tormentor's Turbo time isn't in the source, so it
# isn't announced there rather than announced at the wrong minute.
TURBO_SPAWNS: list[Spawn] = [
    replace(sp, first=sp.first // 2) if sp.key.startswith("neutral_") else sp
    for sp in SPAWNS if sp.key != "tormentor"
]


def spawns(turbo: bool = False) -> list[Spawn]:
    return TURBO_SPAWNS if turbo else SPAWNS


def roshan_window(kill_clock: int, turbo: bool = False) -> tuple[int, int]:
    if turbo:
        from .mode import ROSHAN_RESPAWN_S

        return kill_clock + ROSHAN_RESPAWN_S[0], kill_clock + ROSHAN_RESPAWN_S[1]
    return kill_clock + ROSHAN_RESPAWN_MIN_S, kill_clock + ROSHAN_RESPAWN_MAX_S


def aegis_s(turbo: bool = False) -> int:
    if turbo:
        from .mode import AEGIS_S

        return AEGIS_S
    return AEGIS_DURATION_S


def is_day(clock: int) -> bool:
    """The *natural* cycle. Roshan and Tormentors follow this, not map.daytime,
    which Night Stalker's ultimate can flip."""
    return clock < 0 or (clock // (DAY_NIGHT_CYCLE_S // 2)) % 2 == 0


def roshan_pit(clock: int) -> str:
    """7.41: from 15:00 Roshan walks to the other pit at each day/night change."""
    if clock < ROSHAN_ROAMS_FROM:
        return "starting pit (top, Dire side)"
    return "top pit (Dire side)" if is_day(clock) else "bottom pit (Radiant side)"


def tormentor_side(clock: int) -> str:
    return "bottom-right (Radiant) corner" if is_day(clock) else "top-left (Dire) corner"


def prompt_summary() -> str:
    return "\n".join([
        f"(patch {PATCH})",
        "- Bounty runes: 0:00 in the river, then every 4:00 at the jungle spots (uncollected ones stack).",
        "- Water runes: 2:00 and 4:00. Power rune: from 6:00, every 2:00, one random river spot.",
        "- Shrines of Wisdom (XP): 7:00 then every 7:00, in the offlane-side jungles.",
        "- Lotus pools: a lotus every 3:00, up to 6 stored.",
        f"- Tormentors: first at 20:00, respawn {TORMENTOR_RESPAWN_S // 60}:00 after death; bottom-right corner by day, "
        "top-left by night. "
        "Kill reward is an Aghanim's Shard for a low-net-worth player.",
        f"- Roshan: respawns {ROSHAN_RESPAWN_MIN_S // 60}-{ROSHAN_RESPAWN_MAX_S // 60} min after death. "
        "From 15:00 he is in the top pit by day and the bottom pit by night. "
        "Drops Aegis (lasts 5:00); Banner from the 2nd kill; Cheese and Refresher Shard from the 3rd.",
        "- Neutral items: Madstone crafting; tiers unlock at 0:00, 15:00, 25:00, 35:00, 60:00.",
        "- Neutral camps respawn every minute from 1:00; pull at about x:53-x:55 to stack.",
        f"- Day 0:00-5:00, night 5:00-10:00, repeating. Buyback cooldown {BUYBACK_COOLDOWN_S // 60}:00. "
        f"Glyph cooldown {GLYPH_COOLDOWN_S // 60}:00.",
    ])
