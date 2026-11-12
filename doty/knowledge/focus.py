"""One thing to practise per game: a measurable focus, graded after the match, with its trend.

Reminders don't make a player better; practising one thing, being graded on it and seeing the trend does
(docs/ACCURACY_PLAN.md, item 11). Each topic here is a habit that one match's review measures as a number, with a
target to beat:

| key              | measured from the review                                 | target (source)                           |
|------------------|----------------------------------------------------------|-------------------------------------------|
| lh10             | last hits at 10:00                                       | the hero card's pace, one rank up (else   |
|                  |                                                          | your rank's; else a fixed number a level) |
| deaths_no_tp     | deaths with no TP scroll in the TP slot                  | 0                                         |
| deaths_with_gold | deaths holding UNSPENT_GOLD or more unspent gold         | 0                                         |
| key_item         | how late the card's first key item came, in seconds      | 0 (by the card's timing target)           |
| skill_points     | minutes holding an unspent skill point (past a grace)    | 1 minute                                  |
| buyback          | deaths from 30:00 on without buyback ready (normal only) | 0                                         |
| farm_stalls      | minutes after the lanes with under half your usual       | 2                                         |
|                  | income, alive the whole minute                           |                                           |

Fixed targets and why: a death without a TP scroll, holding 2,000 gold, or without buyback late are each
avoidable every time, so the target is none. Skill points are spent in seconds; a minute in a whole game allows for
level-ups mid-fight. Two slow minutes after the lanes allow for a smoke or a long walk; more is time not farming.
Last hits without a card: 20, 30, 40, 50, 60 at 10:00 for the five levels, from about half of a perfect lane (a
10-minute lane has about 80 lane creeps for a solo core) for a new player to most of it at the top.

The history (`%LOCALAPPDATA%\\Doty\\focus.json`) keeps, per game: when (a unix time), the mode (normal or Turbo),
the role group (core, support or unknown), the player's level setting, the hero, which topic was the focus, and
each topic's number and target. No account, match id or name. Turbo and normal games are never mixed in a trend.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..config import LOG_DIR

if TYPE_CHECKING:
    from ..review import Review

HISTORY_FILE = LOG_DIR.parent / "focus.json"
HISTORY_KEPT = 200
FORMAT = 1
RECENT = 5  # the games a topic's trend and its gap are read from
MASTERED_AFTER = 3  # hit this many measured games in a row: mastered, it isn't chosen again until missed
ROTATE_AFTER = 3  # the focus this many games running: another one gets a turn (when another has a gap)
UNSPENT_GOLD = 2000  # (as review.UNSPENT_GOLD)
SKILL_POINT_GRACE_S = 10  # seconds after a level-up before a point counts as unspent
LANES_OVER_S = 600  # farm stalls count from 10:00 (a Turbo game's 5:00)
STALL_SHARE = 0.5  # a minute's income under this share of your usual minute is a stall
LH_BY_LEVEL = {"new": 20, "learning": 30, "some": 40, "experienced": 50, "pro": 60}
ALL_LEVELS = frozenset(LH_BY_LEVEL)


@dataclass(frozen=True)
class Target:
    value: float
    source: str  # "card_up" | "card_rank" | "fixed"
    item: str = ""  # key_item: the item (key) and its name
    item_name: str = ""
    at: float = 0.0  # key_item: the card's timing target (game seconds); the value is then 0 (seconds late)


@dataclass(frozen=True)
class Topic:
    key: str
    title: str  # short, for the review card ("Last hits at 10:00")
    levels: frozenset[str]
    roles: frozenset[str]  # "core", "support"; a player whose role isn't known gets any topic
    switch: str  # the Settings > Alerts subject it belongs to (config.SuggestConfig)
    better: str  # "high" or "low": which way a number is better
    scale: float  # one "unit" of gap, so gaps of different topics compare
    measure: Callable[["Review", Target], float | None]
    target: Callable[[dict[str, Any]], Target | None]

    def hit(self, value: float, target: float) -> bool:
        return value >= target if self.better == "high" else value <= target

    def gap(self, value: float, target: float) -> float:
        """How far short of the target, in units of `scale` (0 when hit)."""
        short = (target - value) if self.better == "high" else (value - target)
        return max(0.0, short / self.scale)


# measurements --------------------------------------------------------------------------------------------------
def _turbo(review: "Review") -> bool:
    return getattr(review, "mode", "normal") == "turbo"


def _lh10(review: "Review", _t: Target) -> float | None:
    if review.recorded_from > 60:  # (Doty joined late: the lane wasn't seen)
        return None
    return float(review.lh_10) if review.lh_10 is not None else None


def _deaths_no_tp(review: "Review", _t: Target) -> float | None:
    seen = [d for d in review.deaths if d.had_tp is not None]
    if review.duration < 600:
        return None
    return float(sum(1 for d in seen if d.had_tp is False))


def _deaths_with_gold(review: "Review", _t: Target) -> float | None:
    if review.duration < 600:
        return None
    return float(sum(1 for d in review.deaths if d.gold_before >= UNSPENT_GOLD))


def _key_item(review: "Review", target: Target) -> float | None:
    if not target.item:
        return None
    got = [c for item, c in (review.purchases or review.bought) if item == target.item]
    if got:
        return float(min(got) - target.at)
    if review.duration > target.at:
        return float(review.duration - target.at)  # (not bought at all: at least this late)
    return None


def _skill_points(review: "Review", _t: Target) -> float | None:
    seconds = getattr(review, "unspent_point_s", None)
    if seconds is None or review.duration < 600:
        return None
    return round(seconds / 60.0, 1)


def _buyback(review: "Review", _t: Target) -> float | None:
    if _turbo(review) or review.duration < 1800:
        return None
    late = [d for d in review.deaths if d.clock >= 1800 and getattr(d, "buyback_ready", None) is not None]
    return float(sum(1 for d in late if not d.buyback_ready))


def farm_stall_minutes(review: "Review") -> int | None:
    """Minutes after the lanes where income fell under half your usual minute, alive all minute. Income is read
    from the minute samples' GPM (GPM times minutes is the gold earned so far)."""
    start = LANES_OVER_S // (2 if _turbo(review) else 1)
    samples = sorted((m for m in review.minutes if m.get("clock", -1) >= 0), key=lambda m: m["clock"])
    income: list[tuple[int, float]] = []
    for a, b in zip(samples, samples[1:]):
        ma, mb = a["clock"] / 60.0, b["clock"] / 60.0
        if mb - ma < 0.5 or b["clock"] < start:
            continue
        earned = b.get("gpm", 0) * mb - a.get("gpm", 0) * ma
        income.append((b["clock"], earned / (mb - ma)))
    if len(income) < 3:
        return None
    usual = statistics.median(v for _c, v in income)
    deaths = [d.clock for d in review.deaths]
    return sum(1 for clock, v in income
               if v < STALL_SHARE * usual and not any(clock - 90 <= d <= clock for d in deaths))


def _farm_stalls(review: "Review", _t: Target) -> float | None:
    n = farm_stall_minutes(review)
    return None if n is None else float(n)


# targets -------------------------------------------------------------------------------------------------------
def _fixed(value: float) -> Callable[[dict[str, Any]], Target]:
    return lambda _ctx: Target(value, "fixed")


def _lh_target(ctx: dict[str, Any]) -> Target | None:
    pace = ctx.get("pace")
    if pace is None and ctx.get("hero_id"):
        from . import cards

        pace = cards.pace(ctx["hero_id"], ctx.get("role"), ctx.get("bracket", 0), ctx.get("mode", "normal"),
                          ctx.get("cache_dir"))
    if pace and pace.get("available"):
        up = ((pace.get("up") or {}).get(600) or {}).get("lh")
        ref = ((pace.get("ref") or {}).get(600) or {}).get("lh")
        if up:
            return Target(float(up), "card_up")
        if ref:
            return Target(float(ref), "card_rank")
    return Target(float(LH_BY_LEVEL.get(ctx.get("level", "some"), 40)), "fixed")


def _item_target(ctx: dict[str, Any]) -> Target | None:
    card = ctx.get("card")
    if card is None and ctx.get("hero_id"):
        from . import cards

        card = cards.card(ctx["hero_id"], ctx.get("role"), ctx.get("bracket", 0), ctx.get("mode", "normal"),
                          cache_dir=ctx.get("cache_dir"))
    timings = (card or {}).get("timings") or []
    if not timings or not timings[0].get("target_s"):
        return None
    first = timings[0]
    item = str(first["item"])
    item = item if item.startswith("item_") else f"item_{item}"
    return Target(0.0, "card", item, str(first.get("name") or ""), float(first["target_s"]))


BASICS = frozenset({"new", "learning"})
CLIMB = frozenset({"learning", "some", "experienced"})
UPPER = frozenset({"some", "experienced", "pro"})
CORE, SUPPORT = frozenset({"core"}), frozenset({"core", "support"})

TOPICS: dict[str, Topic] = {t.key: t for t in (
    Topic("lh10", "Last hits at 10:00", ALL_LEVELS - {"pro"}, CORE, "farm", "high", 10.0, _lh10, _lh_target),
    Topic("deaths_no_tp", "Deaths without a TP scroll", BASICS | {"some"}, SUPPORT, "gold", "low", 1.0,
          _deaths_no_tp, _fixed(0)),
    Topic("skill_points", "Minutes with a skill point unspent", BASICS, SUPPORT, "skills", "low", 1.0,
          _skill_points, _fixed(1)),
    Topic("deaths_with_gold", f"Deaths holding {UNSPENT_GOLD:,}+ gold", CLIMB, SUPPORT, "gold", "low", 1.0,
          _deaths_with_gold, _fixed(0)),
    Topic("farm_stalls", "Slow farming minutes after the lanes", UPPER, CORE, "farm", "low", 2.0, _farm_stalls,
          _fixed(2)),
    Topic("key_item", "Your first key item", UPPER, CORE, "items", "low", 120.0, _key_item, _item_target),
    Topic("buyback", "Late deaths without buyback", UPPER, SUPPORT, "gold", "low", 1.0, _buyback, _fixed(0)),
)}
# With no history: the first that fits the level, the role and the switches.
DEFAULTS = {"new": ("lh10", "deaths_no_tp", "skill_points"), "learning": ("lh10", "deaths_no_tp", "deaths_with_gold"),
            "some": ("lh10", "deaths_with_gold", "deaths_no_tp", "key_item"),
            "experienced": ("key_item", "farm_stalls", "deaths_with_gold", "buyback"),
            "pro": ("key_item", "farm_stalls", "buyback")}


# grading one game ------------------------------------------------------------------------------------------------
def role_group(role: Any) -> str:
    """"core", "support" or "" from a roles.Role (or what a review saved)."""
    if isinstance(role, str):
        return role if role in ("core", "support") else ""
    core = getattr(role, "core", None)
    return "core" if core is True else "support" if core is False else ""


def any_role(topic: Topic) -> bool:
    """A topic for every role (deaths without a TP, skill points...): fair to set before the role is known."""
    return {"core", "support"} <= set(topic.roles)


def settled(role: Any) -> bool:
    """The role is known well enough to set a role's topic by: the lanes have been seen (a position) or roles.py is
    sure. A first guess from the hero's tags isn't (as Knowledge._role_known: Spirit Breaker is tagged Carry and
    mostly played as a support)."""
    return role is not None and (bool(getattr(role, "position", 0)) or bool(getattr(role, "sure", False)))


HERO_ROLE_GAMES = 3  # the player's own games on this hero before their usual role on it counts...
HERO_ROLE_SHARE = 0.8  # ...and the share of them in one role group


def usual_role(history: list[dict[str, Any]], hero: str, mode: str = "normal") -> str:
    """The role group the player nearly always plays this hero in ("core"/"support"), from their own graded games
    of this mode; "" when there aren't enough, or they're mixed."""
    groups = [g.get("role", "") for g in history if hero and g.get("hero") == hero and g.get("mode", "normal") == mode
              and g.get("role") in ("core", "support")]
    if len(groups) < HERO_ROLE_GAMES:
        return ""
    for group in ("core", "support"):
        if groups.count(group) >= HERO_ROLE_SHARE * len(groups):
            return group
    return ""


def grade(review: "Review", ctx: dict[str, Any]) -> dict[str, list[float]]:
    """Every topic this game measures: {key: [number, target]}. `ctx` gives the targets their data: hero_id, role,
    bracket, mode, level, cache_dir (or "pace" and "card" directly). A role's own topic is graded only when the
    game's final role is that role (a support's last hits aren't a core's miss, and stay out of the core trend)."""
    out: dict[str, list[float]] = {}
    group = role_group(ctx.get("role"))
    for topic in TOPICS.values():
        if not any_role(topic) and group not in topic.roles:
            continue
        try:
            target = topic.target(ctx)
            value = topic.measure(review, target) if target is not None else None
        except Exception as exc:  # one topic's data being odd mustn't lose the rest
            print(f"[doty] focus {topic.key}: {exc!r}")
            continue
        if value is not None:
            out[topic.key] = [round(float(value), 1), round(float(target.value), 1)]
    return out


# the history -------------------------------------------------------------------------------------------------------
def load_history(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path or HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    games = data.get("games") if isinstance(data, dict) else None
    return [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []


def add_game(entry: dict[str, Any], path: Path | None = None) -> list[dict[str, Any]]:
    """Append one game (see the module docstring for what's in it) and keep the last HISTORY_KEPT."""
    path = Path(path or HISTORY_FILE)
    games = (load_history(path) + [entry])[-HISTORY_KEPT:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(json.dumps({"format": FORMAT, "games": games}, indent=1), encoding="utf-8")
        part.replace(path)
    except OSError as exc:
        print(f"[doty] couldn't save the focus history ({exc.__class__.__name__})")
    return games


def entry_for(review: "Review", results: dict[str, list[float]], focus: str, level: str, role: str) -> dict[str, Any]:
    return {"when": int(time.time()), "mode": "turbo" if _turbo(review) else "normal", "role": role_group(role),
            "level": level, "hero": review.hero, "focus": focus or "", "results": results}


def _games(history: list[dict[str, Any]], mode: str, role: str) -> list[dict[str, Any]]:
    """The games a trend is read from: this mode only (Turbo and normal never mix), this role group when known."""
    same = [g for g in history if g.get("mode", "normal") == mode]
    if role:
        same = [g for g in same if g.get("role", "") in (role, "")]
    return same


def _rows(history: list[dict[str, Any]], key: str, mode: str, role: str) -> list[list[float]]:
    """A topic's [number, target] over the games it was measured in, oldest first. A role's own topic only counts
    games played in that role (an older history may hold a support game graded on last hits)."""
    topic = TOPICS[key]
    return [g["results"][key] for g in _games(history, mode, role) if key in (g.get("results") or {})
            and (any_role(topic) or g.get("role", "") in topic.roles)]


def trend(history: list[dict[str, Any]], key: str, mode: str = "normal", role: str = "",
          n: int = RECENT) -> list[float]:
    """The topic's numbers over the last `n` games it was measured in (oldest first)."""
    return [v for v, _t in _rows(history, key, mode, role)][-n:]


def mastered(history: list[dict[str, Any]], key: str, mode: str = "normal", role: str = "") -> bool:
    topic = TOPICS[key]
    last = _rows(history, key, mode, role)[-MASTERED_AFTER:]
    return len(last) == MASTERED_AFTER and all(topic.hit(v, t) for v, t in last)


def fits(topic: Topic, level: str, role: str, topics_off=frozenset()) -> bool:
    return (level in topic.levels and (not role or role in topic.roles)
            and topic.switch not in topics_off)


@dataclass(frozen=True)
class Choice:
    key: str
    why: str  # "gap" | "default" | "rotation"
    recent: tuple[float, ...] = ()  # the topic's last numbers, oldest first


def choose(history: list[dict[str, Any]], level: str = "some", role: Any = None, mode: str = "normal",
           topics_off=frozenset(), any_role_only: bool = False) -> Choice | None:
    """This game's focus: among the topics that suit the level and role (and whose subject isn't switched off),
    the one with the worst average gap to its target over the last games, skipping what's mastered (hit in the
    last MASTERED_AFTER games) and, after ROTATE_AFTER games running, the current focus, so another gets a turn.
    With no history for any of them: the level's default. None if nothing fits. `any_role_only`: the role isn't
    known yet, so only topics that suit every role (no "40 last hits" to a support)."""
    role = role_group(role)
    level = level if level in ALL_LEVELS else "some"
    games = _games(history, mode, role)
    fit = [t for t in TOPICS.values() if fits(t, level, role, topics_off) and (any_role(t) or not any_role_only)]
    if not fit:
        return None
    recent_focus = [g.get("focus", "") for g in games[-ROTATE_AFTER:]]
    stuck = recent_focus[0] if len(recent_focus) == ROTATE_AFTER and len(set(recent_focus)) == 1 else ""
    scored = []
    for t in fit:
        rows = _rows(history, t.key, mode, role)[-RECENT:]
        if not rows or mastered(history, t.key, mode, role):
            continue
        gap = sum(t.gap(v, target) for v, target in rows) / len(rows)
        if gap > 0:
            scored.append((gap, t.key))
    scored.sort(key=lambda s: (-s[0], list(TOPICS).index(s[1])))
    if scored:
        best = scored[0][1]
        why = "gap"
        if best == stuck and len(scored) > 1:
            best, why = scored[1][1], "rotation"
        return Choice(best, why, tuple(trend(history, best, mode, role)))
    measured = {t.key for t in fit if _rows(history, t.key, mode, role)}
    order = list(DEFAULTS.get(level, ())) + [t.key for t in fit]
    for key in order:
        t = TOPICS.get(key)
        if t is not None and t in fit and key not in measured and key != stuck:
            return Choice(key, "default", ())
    for key in order:  # (everything measured is hit: keep the level's first one going)
        t = TOPICS.get(key)
        if t is not None and t in fit and not mastered(history, key, mode, role) and key != stuck:
            return Choice(key, "default", tuple(trend(history, key, mode, role)))
    first = next(t.key for t in fit)
    return Choice(first, "default", tuple(trend(history, first, mode, role)))


# what it says -----------------------------------------------------------------------------------------------------
def mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{'-' if s < 0 else ''}{abs(s) // 60}:{abs(s) % 60:02d}"


def result_text(review_practice: dict[str, Any]) -> str:
    """The review card's line: "Focus: Last hits at 10:00 — 38 (target 45). Last five: 29, 31, 30, 36, 38."
    (English: the UI translates it from its parts, see coach_page.)"""
    p = review_practice
    head = f"Focus: {p['title']} — {_num(p['value'])} (target {_num(p['target'])})"
    tail = f" Last {len(p['trend'])}: {', '.join(_num(v) for v in p['trend'])}." if len(p.get("trend") or []) > 1 else ""
    return head + "." + tail


def _num(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:.1f}"
