"""Who is in this game, and on which side: votes from the coach's reads of the top bar and the draft.

A single read of ten small portraits is easy to get wrong (a hero on the wrong side, or one that isn't
in the game at all), and one bad read used to stick for the whole match. So every read is a vote:

- The player's own hero comes from the game data (GSI), and it anchors their side. A read that puts it
  on the other team has the sides swapped and is flipped; a read with a full side that leaves it out is
  dropped.
- A hero counts as confirmed on a side once two reads agree, and more reads put it there than on the
  other side. Heroes read only once stay unconfirmed and are never shown to the coach as fact.

Lists are of npc names (npc_dota_hero_x); the coach maps them to display names.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

SIDES = ("radiant", "dire")
CONFIRM_VOTES = 2
OWN_VOTES = 100  # the player's own hero, straight from the game data


def other(side: str) -> str:
    return "dire" if side == "radiant" else "radiant"


@dataclass
class Lineup:
    votes: dict[str, Counter] = field(default_factory=lambda: {s: Counter() for s in SIDES})
    reads: int = 0
    rejected: int = 0
    # The coach adds votes on the event loop while the screen reader (its own thread) reads them.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def add(self, radiant: list[str], dire: list[str], own: str = "", team: str = "", weight: int = 1) -> bool:
        """Count one read. Returns False if it was dropped as inconsistent with the game data."""
        with self._lock:
            return self._add(radiant, dire, own, team, weight)

    def _add(self, radiant: list[str], dire: list[str], own: str, team: str, weight: int) -> bool:
        sides = {"radiant": _unique(radiant)[:5], "dire": _unique(dire)[:5]}
        both = set(sides["radiant"]) & set(sides["dire"])
        if both:  # the same hero on both teams: that part of the read is noise
            sides = {s: [h for h in names if h not in both] for s, names in sides.items()}
        team = (team or "").lower()
        if own and team in SIDES:
            if own in sides[other(team)] and own not in sides[team]:
                sides = {team: sides[other(team)], other(team): sides[team]}  # read with the sides swapped
            if own not in sides[team]:
                if len(sides[team]) >= 5:
                    self.rejected += 1
                    return False  # a full side without the player's own hero: a misread
                sides[team].append(own)
            self.votes[team][own] = max(self.votes[team][own], OWN_VOTES)
        if sum(len(v) for v in sides.values()) < 3:
            self.rejected += 1
            return False
        for side, names in sides.items():
            for npc in names:
                self.votes[side][npc] += weight
        self.reads += 1
        return True

    def confirmed(self, side: str) -> list[str]:
        """Heroes two reads agree are on `side` (at most five, most-voted first)."""
        with self._lock:
            mine, theirs = self.votes[side], self.votes[other(side)]
            names = [h for h, n in mine.items() if n >= CONFIRM_VOTES and n > theirs.get(h, 0)]
            return sorted(names, key=lambda h: -mine[h])[:5]

    def as_dict(self) -> dict[str, list[str]]:
        return {s: self.confirmed(s) for s in SIDES}

    @property
    def complete(self) -> bool:
        return all(len(self.confirmed(s)) == 5 for s in SIDES)

    def side_of(self, npc: str) -> str | None:
        for s in SIDES:
            if npc in self.confirmed(s):
                return s
        return None


def _unique(names: list[str]) -> list[str]:
    seen: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen
