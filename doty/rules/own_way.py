"""When the player goes their own way: how Doty disagrees with their build, and the build preferences they gave it.

A real match: "Black King Bar", fifty-seven times, to a player who had judged it unnecessary against that lineup.
The player's rule, in their words: when the coach disagrees with my build it says so "once, with a reason", and
that's a setting (CoachConfig.own_way):

  "once"    The first time Doty points at an item is advice, not an argument. After that, while the player
            hasn't started it (or has passed on it), Doty may argue for it at most once per item per match, at a
            buying moment (dead, in base, or the gold covers a real part of it) or when asked, and only with a
            reason from this match. With no reason it says nothing. Then it follows the player.
  "follow"  Doty never argues: one suggestion, then it follows.
  "remind"  The old way: the coach up to three times (coach/llm.py MAX_ITEM_PUSHES), "you can afford it" twice
            (rules/economy.py FINISH_CALLS).

An argument reopens only when the facts behind its reason change (`Reason.facts`): a new enemy hero or spell
makes it a different case, the same case said again is nagging.

The rules (this file's OwnWay, economy.AffordableBuildItem, progression.ItemPace) and the AI coach's vetting
(coach/llm.py) both ask here, and share one record of what was argued (match memory), so the player never hears
the same argument from both.

The player's standing preferences (CoachConfig.never_suggest, hero_builds) are applied to the build here too
(`honour`): an item never to suggest leaves the build at the start of every match, like an item passed on, and a
build the player always goes on a hero leads that hero's plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from ..advice import Advice, Priority
from ..knowledge import build, judgement
from ..knowledge import mode as game_mode
from .base import Rule, RuleContext

if TYPE_CHECKING:
    from ..knowledge.constants import Knowledge
    from ..state import GameStateStore, Snapshot

ONCE, FOLLOW, REMIND = "once", "follow", "remind"
OWN_WAYS = (ONCE, FOLLOW, REMIND)
DEFAULT = ONCE
# Why an item left the build when it's one the player never wants suggested (Knowledge.skipped's value).
NEVER = "never"
# kb.item_case(...).score from which a case is a reason to argue: judgement.STRONG. The one argument has to rest on
# something clear about this lineup; a middling case (between judgement.WEAK and STRONG) is a thing for the coach
# to weigh when asked, not a reason to tell a player they're wrong. A weak one (< WEAK) vetoes the item (coach/llm).
MIN_CASE_SCORE = judgement.STRONG
POPULAR_N = 10  # "the hero's usual items": the first this many of Knowledge.typical_items


def mode_of(prefs: Any) -> str:
    """The "own way" setting of a CoachConfig (or anything with `own_way`); the default for None or nonsense."""
    value = getattr(prefs, "own_way", DEFAULT) if prefs is not None else DEFAULT
    return value if value in OWN_WAYS else DEFAULT


def free_pushes(prefs: Any, remind_count: int) -> int:
    """How many times the coach may point the player at one item before a further push is an argument:
    `remind_count` in "remind" (the old limit), else once (the first time is advice)."""
    return remind_count if mode_of(prefs) == REMIND else 1


def afford_calls(prefs: Any, remind_count: int) -> int:
    """How many "you can afford X" calls an item gets before it leaves the build."""
    return remind_count if mode_of(prefs) == REMIND else 1


def never_items(prefs: Any) -> frozenset[str]:
    """Items the player never wants suggested (CoachConfig.never_suggest), as item keys."""
    items = getattr(prefs, "never_suggest", None) or ()
    return frozenset(i if i.startswith("item_") else f"item_{i}" for i in items if isinstance(i, str) and i)


def hero_build(prefs: Any, hero: str) -> list[str]:
    """The player's own build for this hero (CoachConfig.hero_builds), as item keys."""
    builds = getattr(prefs, "hero_builds", None)
    items = builds.get(hero) if isinstance(builds, dict) and hero else None
    if not isinstance(items, (list, tuple)):
        return []
    return list(dict.fromkeys(i if i.startswith("item_") else f"item_{i}" for i in items if isinstance(i, str) and i))


# the reason ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Reason:
    """Why an item is worth arguing for in this match. `text`: a short clause ("" for a popular item argued for
    without one). `facts`: what the reason rests on; an argument made reopens only when these change."""

    text: str
    facts: frozenset


def item_reason(kb: Knowledge | None, item: str, hero: str, enemies: list[str]) -> Reason | None:
    """THE place the reason for arguing comes from: `Knowledge.item_case` (knowledge/judgement.py).

    The case is judged against the enemies Doty has confirmed this match (`kb.case_here`, npc names kept by
    knowledge/build.lineup); before those are known, against `enemies`, the names the coach's lineup holds.
    Only a strong case (score >= MIN_CASE_SCORE) is a reason: its `.reason` is what's said, and its `.answers`, the
    (enemy hero, ability) pairs it rests on, are the facts (the argument reopens only when those change, not when
    the wording does). A weaker case is no reason: nothing is said. No case at all (no ability data, no enemies
    known, or not a counter item): an item from the hero's usual build may be argued for once without a reason
    clause; anything else is not argued for."""
    if kb is None or not item:
        return None
    case = None
    try:
        if getattr(kb, "enemies", ()):
            case = kb.case_here(item)
        elif enemies:
            case = kb.item_case(item, list(enemies))
    except Exception as exc:  # (a broken case is no reason, never a crash in the rules)
        print(f"[doty] item_case({item}) failed: {exc!r}")
        case = None
    if case is not None:
        try:
            score = float(getattr(case, "score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        text = str(getattr(case, "reason", "") or "").strip().rstrip(".")
        if score < MIN_CASE_SCORE or not text:
            return None
        try:
            facts = frozenset(tuple(a) for a in (getattr(case, "answers", None) or ()))
        except TypeError:
            facts = frozenset()
        return Reason(text, facts or frozenset({text}))
    info = kb.heroes.get(hero) if hero else None
    usual = set(kb.typical_items(int(info["id"]), POPULAR_N)) if info else set()
    return Reason("", frozenset({"usual_build"})) if item in usual else None


def enemies_of(store: GameStateStore, snap: Snapshot) -> list[str]:
    """The enemy heroes known so far (the lineup read off the top bar), by name."""
    lineup = store.memory.get("lineup") or {}
    team = (snap.team or "").lower() if snap is not None else ""
    if not isinstance(lineup, dict) or team not in ("radiant", "dire"):
        return []
    return [n for n in lineup.get("dire" if team == "radiant" else "radiant", []) if n]


# when it may be said --------------------------------------------------------------------------------------------
def buying_moment(snap: Snapshot, kb: Knowledge, item: str, store: GameStateStore) -> bool:
    """A moment the player can act on an item: dead (the shop is open), in base, or the gold covers a real part
    of it right now."""
    if snap is None:
        return False
    if not snap.alive or snap.in_own_base:
        return True
    from .economy import spendable

    gold = spendable(snap, game_mode.of(store.memory))
    return kb.next_buy(item, snap.item_list(), gold) is not None


def _argued(store: GameStateStore) -> dict[str, frozenset]:
    return store.memory.setdefault("own_way_argued", {})


def may_argue(store: GameStateStore, kb: Knowledge | None, item: str, snap: Snapshot, prefs: Any,
              asked: bool = False) -> Reason | None:
    """The reason Doty may give now for an item the player isn't building, or None to stay quiet. Only in
    "once": never for an item the player never wants or said they won't buy, only with a reason, only once per
    item (until the reason's facts change), and only at a buying moment or when the player asked."""
    if mode_of(prefs) != ONCE or kb is None or snap is None or not item:
        return None
    if item in never_items(prefs) or kb.skipped.get(item) in (build.SAID_SO, NEVER):
        return None  # (they've told Doty: that's settled)
    reason = item_reason(kb, item, snap.hero_name, enemies_of(store, snap))
    if reason is None:
        return None
    if _argued(store).get(item) == reason.facts:
        return None  # said once already, and nothing has changed since
    if not asked and not buying_moment(snap, kb, item, store):
        return None
    return reason


def argued(store: GameStateStore, item: str, reason: Reason) -> int:
    """Note that Doty argued for `item` with this reason. Returns how many times it has now (for the alert key)."""
    seen = _argued(store)
    seen[item] = reason.facts
    counts = store.memory.setdefault("own_way_count", {})
    counts[item] = counts.get(item, 0) + 1
    return counts[item]


def was_argued(store: GameStateStore, item: str) -> bool:
    return item in store.memory.get("own_way_argued", {})


def argument(kb: Knowledge, item: str, reason: Reason) -> tuple[str, str]:
    """(card, spoken) for the one argument about an item the player went past."""
    name = kb.item_name(item)
    if reason.text:
        return (f"Your call, but {name} is worth a thought: {reason.text}. Doty follows your build either way",
                f"Your call, but think about {name}: {reason.text}")
    return (f"Your call: most players on this hero get {name} here. Doty follows your build from now on",
            f"Most players get {name} here, but it's your call")


# the player's standing preferences ------------------------------------------------------------------------------
def honour(kb: Knowledge | None, prefs: Any, snap: Snapshot | None, store: GameStateStore) -> None:
    """Apply the player's standing preferences to this match's build, cheaply, on every update: the items they
    never want suggested leave the build once a match (so "bring back" still works for one game), and their
    own build for this hero leads its plan (again after the coach's BUILD line or a new match cleared it)."""
    if kb is None or prefs is None:
        return
    never = never_items(prefs)
    if never:
        done = store.memory.setdefault("own_way_never", set())
        for item in never - done:
            done.add(item)
            if item in kb.items or not kb.items:
                kb.skip(item, NEVER)
    hero = snap.hero_name if snap is not None and snap.has_hero else ""
    mine = [i for i in hero_build(prefs, hero) if i not in never and (i in kb.items or not kb.items)]
    if mine:
        plan = kb.custom_plan.get(hero) or []
        if plan[:len(mine)] != mine:
            kb.custom_plan[hero] = mine + [i for i in plan if i not in mine]


# the rule ----------------------------------------------------------------------------------------------------------
class OwnWay(Rule):
    """In "once": the one argument for an item the player went past (bought other things instead, or could
    afford it and didn't), at a buying moment and with a reason from this match. The item stays out of the
    build either way: this is a word, not a push."""

    id = "own_way"
    cooldown_s = 0.0  # (each argument has its own key)

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        kb, s = ctx.kb, ctx.snap
        prefs = getattr(ctx, "prefs", None)
        if kb is None or not kb.items or mode_of(prefs) != ONCE or "items" in ctx.topics_off:
            return
        if not s.has_items_section or s.clock < 0 or ctx.store.in_fight():
            return
        for item, why in list(kb.skipped.items()):
            if why not in (build.BOUGHT_OTHER, build.COULD_AFFORD) or item in s.all_item_names():
                continue
            reason = may_argue(ctx.store, kb, item, s, prefs)
            if reason is None:
                continue
            n = argued(ctx.store, item, reason)
            text, speech = argument(kb, item, reason)
            yield Advice(text, Priority.NORMAL, "economy", key=f"own_way:{item}:{n}", speech=speech)
            return  # one at a time
