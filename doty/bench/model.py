"""What the bench scores: a line Doty said, the game as it stood when it was said, and the match it belongs to.

A line's context holds only what Doty itself could see at that moment (fair play: the bench never uses anything
the player's own game client didn't send). Every field is optional, because the inputs differ in what they know:
a replayed recording knows the whole game state; a past session log knows only the line and the clock, plus the
purchase times and deaths from that match's review. A checker that needs a field the input doesn't have says
"not applicable" instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..knowledge.constants import Knowledge
    from ..state import Snapshot


@dataclass
class Context:
    clock: int  # the game clock when the line was said
    hero: str = ""  # the player's hero (npc name), when known
    owned: frozenset[str] | None = None  # every item held (keys), consumed Shard/Scepter included
    items: tuple[str, ...] | None = None  # the same with repeats: what counts towards building the next item
    gold: int | None = None  # gold free to spend (buyback gold kept back late in the game, like the rules do)
    has_tp: bool | None = None  # a Town Portal Scroll in the TP slot (or Boots of Travel)
    alive: bool | None = None
    fighting: bool | None = None  # GameStateStore.in_fight() at that moment
    skipped: frozenset[str] | None = None  # items the player passed on this game (Knowledge.skipped)
    heroes: frozenset[str] | None = None  # the heroes in this match (npc names), once the lineup is confirmed
    # The enemy heroes (npc names) once all five are confirmed (Knowledge.enemies with lineup_complete): what a
    # counter item's case (Knowledge.item_case) is judged against. None while not all are known.
    enemies: tuple[str, ...] | None = None
    snap: "Snapshot | None" = None  # the raw snapshot, when the input has one (replays)


@dataclass
class Line:
    clock: int
    text: str
    source: str = "rules"  # rules | coach | player
    rule: str = ""  # the rule's id (rules), "coach:<category>" for the coach, "" when the input doesn't say
    priority: int = 3
    category: str = "general"
    # Heard out loud: True/False when the input knows (a replay), None when it doesn't (a session log only has
    # what was shown; lines at NORMAL priority or above are then counted as said).
    spoken: bool | None = None
    ctx: Context | None = None
    index: int = 0  # position in its match
    t: float | None = None  # wall time it was said (session logs and recordings share this clock)

    @property
    def said(self) -> bool:
        """Counted as said: heard, or (when the input doesn't say) important enough to have been voiced."""
        if self.spoken is not None:
            return self.spoken
        return self.priority <= 3 and self.category != "watching"


@dataclass
class Match:
    label: str  # stable across runs of the same input (file name and match number), so two runs can be diffed
    lines: list[Line] = field(default_factory=list)
    kind: str = "replay"  # replay | log | fixture
    kb: "Knowledge | None" = None
    start_clock: int = 0  # the span of game time the input covers, for lines per minute
    end_clock: int = 0
    notes: list[str] = field(default_factory=list)  # what this input couldn't judge, said once in the scorecard
    memo: dict[str, Any] = field(default_factory=dict)  # checkers keep per-match work here (counts, indexes)
    # "How much Doty talks" the match was played (replayed) with (speech.TALK): the rate and coach-gap checks
    # follow it. None: the "normal" budget (past logs, fixtures).
    talk: str | None = None

    @property
    def minutes(self) -> float:
        return max(0.0, (self.end_clock - max(self.start_clock, 0)) / 60.0)

    def renumber(self) -> None:
        for i, line in enumerate(self.lines):
            line.index = i
