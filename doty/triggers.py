"""Instant calls the AI coach prepares ahead of time, which Doty fires itself the moment they come true.

The AI takes a few seconds to look at the screen and answer, and a fight is over in about as long, so
asking it more often can't make it live. Instead, at calm moments it foresees what's likely in the next
few minutes and writes lines like

    TRIGGER: hp < 35 and enemy_side => Back off now, you're too low to stay.
    TRIGGER: gold >= 4050 and no black_king_bar => Buy Black King Bar now.

Doty checks each condition against the game data on every update (ten times a second) and says the
line the moment it turns true, in milliseconds, mid-fight if need be.

Conditions use a small fixed vocabulary, parsed here; nothing the AI writes is ever run as code, and a
line with a word Doty doesn't know is dropped:
- numbers: hp, mana (percent of max), gold, level, clock (m:ss or seconds), respawn_s, hp_lost (percent of
  max HP lost in the last two seconds); compared with <, <=, >, >=, =, !=
- has <item> / no <item> (internal names, with or without "item_"); ready <item> (in your inventory, off
  cooldown and usable right now); afford <item> (your gold covers what's left of it, counting the parts of it
  you already hold: an Ogre Axe and a Mithril Hammer leave 1,450 of Black King Bar, not 4,050)
- flags: alive, dead, in_fight, enemy_side (you're on the enemy's half of the map), tp (TP scroll ready),
  ult_ready (your ultimate is learned, off cooldown and castable), day, night, buyback (you're dead and can buy
  back right now), and what's on your hero right now: silenced, stunned, hexed, disarmed, muted, broken, magic_immune,
  smoked
- read off the screen by Doty itself (the top bar's greyed portraits), when it can: enemies_dead and
  allies_dead as numbers. A call that uses one waits while Doty can't read it.
Clauses join with "and". A call fires once, when its condition goes from false to true, within the next
six game minutes. The same words aren't said twice within a few minutes, however often the AI arms them again
("Buy Power Treads now" three times in three minutes, in a real demo run).
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .advice import Advice, Priority
from .state import DISABLES, GameStateStore, Snapshot

TRIGGER_LINE = re.compile(r"^\s*TRIGGER:\s*(.+?)\s*=>\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
NUMBERS = ("hp", "mana", "gold", "level", "clock", "respawn_s", "hp_lost", "enemies_dead", "allies_dead")
# What GSI says is on your hero right now (its own field names on the right).
STATUS_FLAGS = {**DISABLES, "magic_immune": "magicimmune", "smoked": "smoked"}
FLAGS = ("alive", "dead", "in_fight", "enemy_side", "tp", "ult_ready", "day", "night", "buyback",
         *STATUS_FLAGS)
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "=": lambda a, b: a == b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
_CLAUSE = re.compile(r"^(?P<var>[a-z_]+)\s*(?P<op><=|>=|==|!=|<|>|=)\s*(?P<val>-?\d+(?::\d{1,2})?)$")
MAX_ACTIVE = 6
VALID_S = 360  # game seconds a prepared call stays armed
MIN_GAP_S = 4.0  # wall seconds between two fired calls, so they never pile up
MAX_WORDS = 16
STALE_S = 3.0  # a fired call that can't be heard within this long is dropped
SAME_WORDS_GAP_S = 150  # game seconds before a call with the same words may be said again
Afford = Callable[[str], "bool | None"]  # item -> your gold covers what's left of it (None: no item data)


@dataclass(frozen=True)
class Clause:
    kind: str  # "num" | "flag" | "has" | "no" | "ready" | "afford"
    var: str
    op: str = ""
    value: float = 0.0

    def holds(self, facts: dict[str, float | bool], items: set[str],
              ready: frozenset[str] | set[str] = frozenset(), afford: Afford | None = None) -> bool | None:
        """True/False, or None when the fact isn't known (then the whole call waits)."""
        if self.kind == "has":
            return self.var in items
        if self.kind == "no":
            return self.var not in items
        if self.kind == "ready":
            return self.var in ready
        if self.kind == "afford":
            return afford(self.var) if afford is not None else None
        if self.var not in facts:
            return None
        v = facts[self.var]
        if self.kind == "flag":
            return bool(v)
        return OPS[self.op](float(v), self.value)


@dataclass
class Trigger:
    condition: str
    say: str
    clauses: tuple[Clause, ...]
    armed_clock: int = 0
    was_true: bool | None = None  # unknown until the first check

    @property
    def while_dead(self) -> bool:
        """A call about being dead ("dead and respawn_s < 10"); every other call waits while you're dead
        (a dead hero has 0 HP, which would set off "hp < 35" on the death screen)."""
        return any(c.var in ("dead", "respawn_s", "buyback") for c in self.clauses)

    def state(self, facts: dict[str, float | bool], items: set[str],
              ready: frozenset[str] | set[str] = frozenset(), afford: Afford | None = None) -> bool | None:
        results = [c.holds(facts, items, ready, afford) for c in self.clauses]
        if any(r is False for r in results):
            return False  # (one false part settles it, even while another isn't known: "dead and respawn_s < 10")
        if any(r is None for r in results):
            return None
        return True


def parse_condition(text: str) -> tuple[Clause, ...] | None:
    """'hp < 35 and no blink' -> clauses, or None if any part isn't in the vocabulary."""
    clauses = []
    for part in re.split(r"\s+and\s+", text.strip().lower()):
        part = part.strip().strip(".")
        if not part:
            return None
        words = part.split()
        if len(words) == 2 and words[0] in ("has", "no", "ready", "afford"):
            item = words[1] if words[1].startswith("item_") else f"item_{words[1]}"
            if not re.fullmatch(r"item_[a-z0-9_]+", item):
                return None
            clauses.append(Clause(words[0], item))
            continue
        if part in FLAGS:
            clauses.append(Clause("flag", part))
            continue
        m = _CLAUSE.match(part.replace(" ", ""))
        if not m or m.group("var") not in NUMBERS:
            return None
        raw = m.group("val")
        value = (int(raw.split(":")[0]) * 60 + int(raw.split(":")[1])) if ":" in raw else float(raw)
        if m.group("var") == "clock" and raw.startswith("-") and ":" in raw:
            value = -(abs(int(raw.split(":")[0])) * 60 + int(raw.split(":")[1]))
        clauses.append(Clause("num", m.group("var"), m.group("op"), float(value)))
    return tuple(clauses) or None


def parse(text: str, clock: int = 0) -> list[Trigger]:
    """TRIGGER lines in the coach's answer -> armed calls (lines Doty can't check are dropped)."""
    out = []
    for m in TRIGGER_LINE.finditer(text):
        condition, say = m.group(1), m.group(2).strip().strip('"')
        clauses = parse_condition(condition)
        if clauses is None or not say:
            continue
        words = say.split()
        if len(words) > MAX_WORDS:
            say = " ".join(words[:MAX_WORDS]).rstrip(",;") + "."
        out.append(Trigger(condition.strip().lower(), say, clauses, armed_clock=clock))
    return out


def facts_for(snap: Snapshot, store: GameStateStore | None = None,
              extra: dict[str, float] | None = None) -> dict[str, float | bool]:
    """The numbers and flags a condition can use, from one game-data update."""
    # Dead means a real death: an Aegis or Reincarnation revive flips "alive" for a few seconds too.
    dead = not snap.alive and (store is None or bool(store.memory.get("death_open")))
    facts: dict[str, float | bool] = {
        "hp": round(snap.hp_pct * 100, 1), "mana": round(snap.mana_pct * 100, 1), "gold": snap.gold,
        "level": snap.level, "clock": snap.clock, "alive": snap.alive, "dead": dead,
    }
    if dead:  # (only then: it's 0 while alive, which would set off "respawn_s <= 5")
        facts["respawn_s"] = int(snap.hero("respawn_seconds", 0) or 0)
    tp = snap.teleport
    facts["tp"] = tp is not None and float(tp.cooldown or 0) <= 0
    facts["day"], facts["night"] = snap.daytime, not snap.daytime
    ults = [a for a in snap.abilities if a.ultimate]
    if ults:
        facts["ult_ready"] = any(a.level > 0 and a.cooldown <= 0 and a.can_cast for a in ults)
    for flag, field_name in STATUS_FLAGS.items():
        facts[flag] = bool(snap.hero(field_name, False))
    cost, cooldown = snap.hero("buyback_cost"), snap.hero("buyback_cooldown")
    if cost is not None:  # (only while dead: alive, there's nothing to buy back)
        facts["buyback"] = dead and snap.gold >= int(cost or 0) and float(cooldown or 0) <= 0
    if store is not None:
        facts["in_fight"] = store.in_fight()
        facts["hp_lost"] = round(max(0.0, -store.hp_trend(2.0)) * 100, 1)
    pos = snap.position
    team = (snap.team or "").lower()
    if pos is not None and team in ("radiant", "dire"):
        x, y = pos
        facts["enemy_side"] = (x + y > 1000) if team == "radiant" else (x + y < -1000)  # past the river
    if extra:
        facts.update(extra)
    return facts


@dataclass
class TriggerBook:
    """The calls armed right now."""

    active: list[Trigger] = field(default_factory=list)
    _last_fired: float = -1e9
    fired: int = 0
    _said: deque = field(default_factory=lambda: deque(maxlen=12))  # (game clock, the words) of fired calls

    def arm(self, triggers: list[Trigger]) -> None:
        for t in triggers:
            self.active = [a for a in self.active if a.condition != t.condition]
            self.active.append(t)
        self.active = self.active[-MAX_ACTIVE:]

    def clear(self) -> None:
        self.active.clear()
        self._said.clear()

    def _said_lately(self, say: str, clock: int) -> bool:
        words = _words(say)
        return any(0 <= clock - when < SAME_WORDS_GAP_S and words == heard for when, heard in self._said)

    def describe(self) -> list[str]:
        """For the coach's next look, so it doesn't arm the same call twice."""
        return [f"{t.condition} => {t.say}" for t in self.active]

    def check(self, snap: Snapshot, store: GameStateStore | None = None,
              extra: dict[str, float] | None = None, afford: Afford | None = None) -> list[Advice]:
        """Advice for every call whose condition just turned true (at most one per few seconds). `afford`
        answers the "afford <item>" clauses (the coach passes Knowledge.cost_left against your gold)."""
        if not self.active or not snap.has_hero or snap.paused:
            return []
        self.active = [t for t in self.active if snap.clock - t.armed_clock <= VALID_S]
        facts = facts_for(snap, store, extra)
        items = snap.all_item_names()
        ready = snap.ready_items()
        out: list[Advice] = []
        for t in list(self.active):
            if not snap.alive and not t.while_dead:
                continue
            now_true = t.state(facts, items, ready, afford)
            if now_true is None:
                continue
            if t.was_true is False and now_true:
                if self._said_lately(t.say, snap.clock):
                    self.active.remove(t)  # said moments ago and armed again: once was enough
                    continue
                last = max(self._last_fired, store.memory.get("save_call_t", -1e9) if store is not None else -1e9)
                if time.monotonic() - last < MIN_GAP_S:
                    # Another call just went (or Doty's own save call, "Glimmer yourself"): this one waits its
                    # turn instead of losing its moment.
                    continue
                self._last_fired = time.monotonic()
                self.fired += 1
                self._said.append((snap.clock, _words(t.say)))
                self.active.remove(t)
                # A prepared call is about this very moment: it cuts in over whatever is being said, and
                # is dropped rather than said late.
                out.append(Advice(t.say, Priority.HIGH, "trigger", source="coach", key=f"trigger:{t.condition}",
                                  trigger_t=snap.received_t, cut_in=True, stale_s=STALE_S))
                continue
            t.was_true = now_true
        return out


def _words(text: str) -> frozenset[str]:
    """A call's words, for telling "the same call again" apart from a new one."""
    return frozenset(re.findall(r"[a-z0-9']+", text.lower()))
