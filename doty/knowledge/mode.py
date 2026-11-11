"""Which kind of game this is: a normal match or Turbo.

Dota's game data doesn't say which mode a match is, so Doty works it out, most trusted first:

1. **Your setting** (Settings > Coaching > Game mode), when you picked Normal or Turbo yourself.
2. **This match:** experience comes in twice as fast in Turbo, so a few minutes in, experience per minute
   far above what a normal lane can give says Turbo, and far below what a Turbo lane gives says normal.
   In between (a normal game's solo lane, a Turbo game's support) the match says nothing, and Doty goes by
   3. That's why the app shows which mode it's coaching and lets you set it.
3. **What you usually play:** the share of Turbo among your recent matches (OpenDota, your own account).

The scripted demo is always a normal game.

Turbo's rules, from the Dota 2 wiki (Game modes > Turbo Mode, read 2026-11-09): all experience and creep
gold doubled; no gold lost on death; respawn 25% faster; weaker towers; every player has a fast flying
courier that delivers by itself; Secret Shop items sold at every shop; Town Portal Scroll cooldown halved;
Roshan's respawn halved; Aegis lasts 4 minutes; neutral item tiers arrive twice as fast. The wiki doesn't
give the Tormentor's Turbo time, so Doty doesn't announce it there. `PACE` (items and levels arriving in
about half the time) follows from the doubled gold and experience; it is an estimate, not a rule, and the
first thing to check against a recorded Turbo match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OPENDOTA_TURBO = 23  # OpenDota's game_mode number for Turbo
PACE = 2.0  # how much faster a Turbo game runs than a normal one
USUAL_SHARE = 0.5  # this share of your recent matches being Turbo makes Turbo what you usually play
MIN_HISTORY = 5  # with fewer recent matches than this, they say nothing

# Experience per minute, a few minutes in. A normal lane gives one hero about 500 a minute at the very best
# (alone, every creep); a Turbo lane twice that. So far above a normal lane is Turbo, and far below what even
# a shared Turbo lane gives is a normal game. In between says nothing.
EVIDENCE_FROM = 150  # the first minutes swing too much (one early kill)
EVIDENCE_UNTIL = 600
XPM_SURELY_TURBO = 850
XPM_SURELY_NORMAL = 280
NORMAL_CHECK_FROM = 300  # (a slow first few minutes of a Turbo game can dip under it)
# After the lanes (Doty was started mid-match, or restarted), everyone's rate has climbed, so the bars are
# higher: a normal game's best core rarely holds this much, and a Turbo support rarely this little.
LATE_XPM_SURELY_TURBO = 1100
LATE_XPM_SURELY_NORMAL = 400

# Added to the game plan the coach is given (which lineup the long game favours, from normal games).
PLAN_NOTE = (" (From normal games: in Turbo both lineups get there about twice as fast, so the early window is "
             "short.)")
# Added to what the AI is given to review a Turbo match.
REVIEW_NOTE = ("This was a TURBO game: gold and experience come about twice as fast as in a normal game, so GPM, XPM "
               "and item times are not comparable with normal-game numbers, and the usual item times below are "
               "estimates (half a normal game's).")
ROSHAN_RESPAWN_S = (240, 330)  # half of the normal 8:00-11:00
AEGIS_S = 240
RESPAWN_FACTOR = 0.75


@dataclass(frozen=True)
class Mode:
    key: str = "normal"  # "normal" | "turbo"
    source: str = "default"  # "setting" | "match" | "history" | "demo" | "default": why Doty thinks so

    @property
    def turbo(self) -> bool:
        return self.key == "turbo"

    @property
    def pace(self) -> float:
        return PACE if self.turbo else 1.0

    def at(self, normal_s: float) -> int:
        """A time from normal games (an item's usual timing, "keep buyback from 30:00") on this game's clock."""
        return int(round(normal_s / self.pace))

    def as_normal(self, clock: float) -> int:
        """This game's clock as the moment of a normal game it matches (for data keyed by normal-game time)."""
        return int(round(clock * self.pace)) if clock > 0 else int(clock)

    @property
    def label(self) -> str:
        return "Turbo" if self.turbo else "Normal"


NORMAL = Mode()


def of(memory: dict[str, Any] | None) -> Mode:
    """The mode Doty has settled on for this match (match memory), a normal game until it has."""
    found = (memory or {}).get("mode")
    return found if isinstance(found, Mode) else NORMAL


def usual(rows: Any) -> str | None:
    """What you usually play, from your recent matches (OpenDota's recentMatches rows): "turbo", "normal",
    or None without enough of them."""
    modes = [r.get("game_mode") for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if len(modes) < MIN_HISTORY:
        return None
    return "turbo" if sum(1 for m in modes if m == OPENDOTA_TURBO) / len(modes) >= USUAL_SHARE else "normal"


def evidence(snap) -> str | None:
    """What this match itself shows right now: "turbo", "normal", or None when it can't tell."""
    if not (snap.has_hero and snap.in_game) or snap.clock < EVIDENCE_FROM:
        return None
    try:
        xpm = float(snap.player("xpm", 0) or 0)
    except (TypeError, ValueError):
        return None
    if snap.clock > EVIDENCE_UNTIL:
        return "turbo" if xpm >= LATE_XPM_SURELY_TURBO else "normal" if 0 < xpm <= LATE_XPM_SURELY_NORMAL else None
    if xpm >= XPM_SURELY_TURBO:
        return "turbo"
    if 0 < xpm <= XPM_SURELY_NORMAL and snap.clock >= NORMAL_CHECK_FROM:
        return "normal"
    return None


def observe(snap, store, setting: str = "auto", usually: str | None = None) -> Mode:
    """Decide this match's mode from the latest update and keep it in match memory ("mode")."""
    mem = store.memory
    if snap.is_demo or snap.custom_game:
        mode = Mode("normal", "demo" if snap.is_demo else "default")
    elif setting in ("normal", "turbo"):
        mode = Mode(setting, "setting")
    else:
        seen = mem.get("mode_seen")
        if seen is None:
            seen = evidence(snap)
            if seen:
                mem["mode_seen"] = seen  # (what a match showed once, it is: the rate only evens out later)
        if seen:
            mode = Mode(seen, "match")
        elif usually in ("normal", "turbo"):
            mode = Mode(usually, "history")
        else:
            mode = NORMAL
    if mem.get("mode") != mode:
        mem["mode"] = mode
    return mode


def prompt_note(mode: Mode) -> str:
    """What the coach must know about this game's mode ('' for a normal game)."""
    if not mode.turbo:
        return ""
    lo, hi = ROSHAN_RESPAWN_S
    return ("TURBO game, not a normal one. All experience and creep gold are doubled, so items and levels come "
            "about twice as fast and the game is decided around 15:00-25:00: treat this clock as a normal game's "
            "clock doubled. No gold is lost on death, and respawns are 25% shorter. Towers are much weaker: push "
            "after every won fight. Each player has a fast courier that delivers by itself, and Secret Shop "
            "items are sold at the base shop: never send the player to the Secret Shop or tell them to use the "
            f"courier. TP scroll cooldown is halved. Roshan respawns {lo // 60}:00-{hi // 60}:{hi % 60:02d} after "
            f"dying and the Aegis lasts {AEGIS_S // 60}:00. Item timings in this state are already on the Turbo "
            "clock. Neutral item tiers unlock twice as fast as the objective timings above say, and the Tormentor "
            "time there is for normal games: don't give a Tormentor time.")
