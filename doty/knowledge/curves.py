"""Power curves: which lineup wins the short games and which the long ones.

From OpenDota's public win rate by game length for each hero (`/heroes/{id}/durations`: games and wins
in 5-minute bins). A hero whose players win more of the long games scales; one whose players win more of
the short games wants to end early. Averaged over a team, that says who the clock favours, which is the
game plan: force fights before their heroes come online, or play safe until yours do.

Fair play: public statistics about the heroes in the game, not anything about this match's players.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

EARLY_END = 25 * 60  # games shorter than this are "short"
LATE_START = 40 * 60  # and longer than this "long"
MIN_GAMES = 150  # a hero needs this many games in a span to count there
MIN_HEROES = 4  # per team, with data for both spans
LEAN = 0.03  # how much more one side gains from a long game before it's a plan (3 points of win rate)


@dataclass(frozen=True)
class Curve:
    early: float  # win rate in short games
    late: float  # win rate in long games


def curve(rows: Any) -> Curve | None:
    """A hero's win rate in short and long games, or None without enough of either."""
    if not isinstance(rows, list):
        return None
    spans = {"early": [0, 0], "late": [0, 0]}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            start, games, wins = int(r.get("duration_bin") or 0), int(r.get("games_played") or 0), int(r.get("wins") or 0)
        except (TypeError, ValueError):
            continue
        span = "early" if start < EARLY_END else "late" if start >= LATE_START else None
        if span:
            spans[span][0] += games
            spans[span][1] += wins
    if any(g < MIN_GAMES for g, _ in spans.values()):
        return None
    return Curve(early=spans["early"][1] / spans["early"][0], late=spans["late"][1] / spans["late"][0])


@dataclass(frozen=True)
class Plan:
    lean: float  # > 0: the long game favours your team; < 0: theirs
    ours: Curve  # team averages
    theirs: Curve

    @property
    def side(self) -> str:
        """'you' (you outscale them), 'them' (they outscale you) or '' (no clear lean)."""
        return "you" if self.lean >= LEAN else "them" if self.lean <= -LEAN else ""

    def card(self) -> str:
        """The plan in a sentence, with the numbers behind it."""
        nums = (f"(heroes like yours win {self.ours.early:.0%} of short games and {self.ours.late:.0%} of long "
                f"ones; theirs {self.theirs.early:.0%} and {self.theirs.late:.0%})")
        if self.side == "them":
            return f"Game plan: their lineup gets stronger the longer the game goes {nums}. Fight, push and take objectives early."
        if self.side == "you":
            return f"Game plan: your lineup gets stronger the longer the game goes {nums}. Farm safely and take even fights later."
        return f"Neither lineup is clearly stronger early or late {nums}."

    def brief(self) -> str:
        """One line for the coach's match brief."""
        if self.side == "them":
            return ("Timing: their heroes win more of the long games than yours (public win rate by game length): "
                    "the player's team should force fights and objectives early and avoid a slow game.")
        if self.side == "you":
            return ("Timing: the player's heroes win more of the long games than theirs (public win rate by game "
                    "length): avoid risky early fights, farm and scale, fight once core items are done.")
        return "Timing: neither lineup clearly favours a short or a long game (public win rate by game length)."


def _mean(curves: list[Curve]) -> Curve:
    return Curve(early=sum(c.early for c in curves) / len(curves), late=sum(c.late for c in curves) / len(curves))


def plan(ours: Iterable[Curve | None], theirs: Iterable[Curve | None]) -> Plan | None:
    """Who the clock favours, from each team's hero curves (None until both teams have enough data)."""
    a = [c for c in ours if c is not None]
    b = [c for c in theirs if c is not None]
    if len(a) < MIN_HEROES or len(b) < MIN_HEROES:
        return None
    mine, yours = _mean(a), _mean(b)
    lean = (mine.late - mine.early) - (yours.late - yours.early)
    return Plan(lean=lean, ours=mine, theirs=yours)
