"""Your build, as you're actually building it.

Doty points at one item at a time (`Knowledge.next_target`). When you go another way, it has to notice and
drop that item, or it nags for the rest of the game. A real Turbo match on Spirit Breaker: "hold until Black
King Bar is done", about twenty-five times over twenty-five minutes, to a player who was building Shadow
Blade, Aghanim's Scepter and Silver Edge.

The game data has no "I'm not buying that" field, so Doty reads it from your gold: once you've put twice the
item's price into other things without buying a single part of it (about two big items bought instead), you've
passed on it. Buying things in another order isn't that: one item bought first leaves the plan as it is. (The same
match: Eul's Scepter was next, the player finished Boots of Bearing and Silver Edge first, then bought Eul's.)
It's then left out of the build for this match (`Knowledge.skipped`), and the coach is told, so neither the rules nor the AI bring
it up again. Saying so ("skip Black King Bar"), the skip hotkey and the app's Buy Next panel do the same at
once; "bring back Black King Bar" undoes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..advice import Advice, Priority
from ..lineup import SIDES, other
from ..state import GameStateStore, Snapshot

if TYPE_CHECKING:
    from .constants import Knowledge

# Spending counts from this price up: wards, a TP scroll and branches aren't a choice of build.
COUNTS_FROM = 400
# What can be bought, carried and lost again without being part of a build.
NOT_BUILD = {"item_gem", "item_rapier", "item_aegis", "item_cheese", "item_refresher_shard", "item_bottle"}
# Passed on: this many times the item's price spent on other things, and for a cheap item (Magic Wand, Bracer)
# at least SKIP_MIN_SPENT.
SKIP_TIMES_COST = 2
SKIP_MIN_SPENT = 2500
CHECK_EVERY_S = 5  # with the same items, look again this often (the plan follows the game's phase)

BOUGHT_OTHER = "bought other items instead"
COULD_AFFORD = "could afford it and didn't buy it"
SAID_SO = "the player said so"


def worth(kb: "Knowledge", owned: list[str]) -> int:
    """Gold sitting in the items of a build (inventory, backpack and stash)."""
    return sum(cost for i in owned if i not in NOT_BUILD and (cost := kb.item_cost(i)) >= COUNTS_FROM)


def observe(snap: Snapshot, store: GameStateStore, kb: "Knowledge | None") -> list[str]:
    """Called on every game-data update, before the rules. Returns the items the player has just passed on
    (now in `kb.skipped`), usually none."""
    if kb is None:
        return []
    lineup(snap, store, kb)
    mem = store.memory.get("build")
    if mem is None:  # a new match (its memory is empty): last game's passed-on items aren't this game's
        mem = store.memory["build"] = {"marks": {}, "seen": None}
        kb.skipped.clear()
    if not kb.items or not (snap.has_hero and snap.in_game and snap.has_items_section):
        return []
    owned = snap.item_list()
    seen = (tuple(sorted(owned)), snap.clock // CHECK_EVERY_S)
    if seen == mem["seen"]:
        return []  # (ten updates a second: nothing to work out until an item or the phase changes)
    mem["seen"] = seen
    target = kb.next_target(snap.hero_name, snap.clock, owned)
    if target is None:
        return []
    cost = kb.item_cost(target)
    # (Plain Boots of Speed are a part of every pair of boots: holding them isn't a start on any one.)
    paid = cost - kb.cost_left(target, [i for i in owned if i != "item_boots"])
    total = worth(kb, owned)
    mark = mem["marks"].setdefault(target, total - paid)  # what the rest was worth when Doty first pointed at it
    if paid > 0 or cost <= 0:
        return []  # you hold a part of it: you're building it
    if total - mark >= max(SKIP_TIMES_COST * cost, SKIP_MIN_SPENT) and kb.skip(target, BOUGHT_OTHER):
        return [target]
    return []


def lineup(snap: Snapshot, store: GameStateStore, kb: "Knowledge") -> None:
    """Tell the knowledge who the other team is, as far as Doty has confirmed it off the top bar (the same
    votes rules/matchup.py reads), so the build can weigh counter items against them (`Knowledge.item_case`).
    A new match (empty memory) has no enemies yet."""
    tracker = store.memory.get("lineup_votes")
    team = (snap.team or "").lower()
    enemies = tracker.confirmed(other(team)) if tracker is not None and team in SIDES else []
    if tuple(enemies) != kb.enemies or (len(enemies) >= 5) != kb.lineup_complete:
        kb.set_enemies(enemies, complete=len(enemies) >= 5)
        mem = store.memory.get("build")
        if mem is not None:
            mem["seen"] = None  # (the target may change with the lineup: look again at once)


def note(kb: "Knowledge", item: str, why: str = BOUGHT_OTHER) -> Advice:
    """The line for the app's feed when an item leaves the build (shown, not said: it's not worth an
    interruption, but the player should be able to see why Doty stopped mentioning it)."""
    name = kb.item_name(item)
    reason = {BOUGHT_OTHER: "you bought other items instead", COULD_AFFORD: "you could afford it and didn't buy it",
              SAID_SO: "as you asked"}.get(why, why)
    return Advice(f'{name} is out of your build for this game: {reason}. Say "bring back {name}" to undo.',
                  Priority.INFO, "economy", key=f"passed:{item}", speak=False, overlay=False)


def passed_on(store: GameStateStore) -> None:
    """Forget what was worked out for the item being built (after a skip or its undo), so the next update
    looks again at once."""
    mem = store.memory.get("build")
    if mem is not None:
        mem["seen"] = None
