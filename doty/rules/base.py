"""Rule interface. A rule looks at one snapshot (plus memory) and yields advice.

Rules must be pure-Python and fast: the whole rule set runs on every GSI
update (~10/s) and the budget for the deterministic path is a few ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from ..advice import Advice
from ..config import RulesConfig
from ..state import GameStateStore, Snapshot

if TYPE_CHECKING:
    from ..knowledge.constants import Knowledge


@dataclass
class RuleContext:
    snap: Snapshot
    prev: Snapshot | None
    store: GameStateStore
    cfg: RulesConfig
    kb: "Knowledge | None"
    new_events: list[dict[str, Any]]
    level: str = "some"  # the player's level (CoachConfig.level)
    topics_off: frozenset = frozenset()  # subjects turned off in Settings (config.SuggestConfig field names)

    @property
    def mem(self) -> dict[str, Any]:
        return self.store.memory


class Rule:
    id: str = "rule"
    # Wall-clock seconds before the same advice key may fire again.
    # 0 means once per match per key (used by timers, whose keys include the spawn time).
    cooldown_s: float = 0.0
    # Rules that only make sense once the horn has sounded / while a hero exists.
    needs_hero: bool = True

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        raise NotImplementedError


def mmss(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(int(seconds))
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"
