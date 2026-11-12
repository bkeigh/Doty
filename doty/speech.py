"""What Doty says out loud: one gate for every spoken line, the rules' and the coach's alike.

Everything still reaches the app's feed and the overlay; this decides only what is heard. Real games ran 3.7 to
7.0 spoken lines a minute, and the bench's largest failure counts were about talking too much, not about facts.
The player's words: help "without being too annoying, but making sure that it's delivering the best and most
relevant information".

The policy, in order:
  1. A danger call (act now to stay alive) and an answer the player asked for are always said.
  2. Nothing else during a fight, or in the few seconds after one (AFTER_FIGHT_S). Dead is a good moment: the
     shop is open and nothing else is happening.
  3. A budget of lines a minute and a gap between lines, and a longer gap between two coach lines, set by "How
     much Doty talks" (CoachConfig.talk: less / normal / more; by default "less" for experienced and top
     players, else "normal"). See TALK.
  4. One item is named in buy lines at most ITEM_CAP times, whoever says it (rules or coach), until a part of it
     is bought.
  5. A line that can't be said now waits for a slot (HOLD_S, or the gap and two seconds when that's longer; never
     past the line's own staleness), then is dropped: never said late. When a slot opens and several lines wait,
     the one that most changes what the player does in the next 30 seconds is said (SCORES; while dead,
     DEAD_BONUS favours buying and planning over calls to go somewhere now).

`SpeechGate.admit` is called by the advice bus on every line before anyone sees it (AdviceBus.gate) and turns
`speak` off for a line that isn't said now; `SpeechGate.release` gives back held lines whose slot has come, which
the bus sends again (AdviceBus.tick, on every game update). Time is `time.monotonic()`.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .advice import Advice, Priority


@dataclass(frozen=True)
class Talk:
    per_min: int  # non-danger lines heard in any 60 s
    gap_s: float  # between two non-danger lines
    coach_gap_s: float  # between two of the coach's own lines


# "How much Doty talks" (CoachConfig.talk). "normal" is what the bench measures real logs against (4 a minute, a
# coach line at most every 30 s).
TALK = {
    "less": Talk(per_min=2, gap_s=20.0, coach_gap_s=60.0),
    "normal": Talk(per_min=4, gap_s=10.0, coach_gap_s=30.0),
    "more": Talk(per_min=6, gap_s=5.0, coach_gap_s=20.0),
}
QUIET_LEVELS = frozenset({"experienced", "pro"})  # their default is "less"
WINDOW_S = 60.0
AFTER_FIGHT_S = 5.0
HOLD_S = 8.0
ITEM_CAP = 2  # buy lines naming one item, rules and coach together, until a part of it is bought
ITEM_CAP_REMIND = 3  # ("When I go my own way" on "remind": the old three pushes)

# When several lines want the same moment, the one that most changes what the player should do in the next 30
# seconds wins. Plain numbers, easy to tune: higher is said first.
SCORES = {
    "asked": 100,  # the player asked (a question, the ask key, "why?"): always said
    "danger": 90,  # act now to stay alive: always said
    "act_now": 70,  # a call prepared for exactly this moment (the coach's TRIGGER lines)
    "timer_now": 60,  # a timer that needs action now (a rune, a stack, Roshan) and won't wait
    "buy_now": 50,  # a buying moment with the gold for it
    "focus": 40,  # about the game's focus topic (store.memory["focus"])
    "coach": 30,  # the coach's judgement for the next minute
    "timer": 20,  # a heads-up for later
    "general": 10,  # anything else
}
# While dead: buying and planning lines are at their best moment (the shop is open, nothing else is happening);
# a call to go and do something on the map now can't be acted on.
DEAD_BONUS = {"buy_now": 25, "coach": 30, "focus": 20, "timer_now": -40, "act_now": -40}


def score_of(kind: str, dead: bool = False) -> int:
    """SCORES, with the dead adjustment."""
    return SCORES[kind] + (DEAD_BONUS.get(kind, 0) if dead else 0)

# Rules that belong to a focus topic, when the topic's name isn't in the rule's own id.
FOCUS_RULES = {
    "last_hits": frozenset({"farm_check", "laning"}),
    "farm": frozenset({"farm_check", "laning"}),
    "items": frozenset({"affordable_item", "item_pace", "own_way"}),
    "deaths": frozenset({"death_watch"}),
    "map": frozenset({"gank_risk", "pick_off"}),
    "skills": frozenset({"skill_point", "power_spike"}),
}
TIMER_CATEGORIES = frozenset({"timer", "runes", "objectives"})
DANGER_CATEGORIES = frozenset({"survival", "danger"})
# A call to get out: never held back (the coach's own word for it, coach/llm.RETREAT).
RETREAT = re.compile(r"\b(back\s+off|walk\s+(back|away)|get\s+out|retreat|leave\b|run\b|teleport|tp\s+out|"
                     r"step\s+back|pull\s+back|head\s+back)", re.IGNORECASE)


def talk_of(cfg: Any) -> str:
    """The "How much Doty talks" setting of a CoachConfig; "" (automatic) or nonsense: by the player's level."""
    value = getattr(cfg, "talk", "") if cfg is not None else ""
    if value in TALK:
        return value
    return "less" if getattr(cfg, "level", "") in QUIET_LEVELS else "normal"


def danger(priority: int, category: str, text: str) -> bool:
    """A call about staying alive right now: never held, never counted against the budget. (The bench's checks
    use the same test.)"""
    # ("No TP scroll — buy one" is a survival rule's line too, but not an act-now one: only HIGH and above count.)
    return priority <= Priority.CRITICAL or (priority <= Priority.HIGH and (
        category in DANGER_CATEGORIES or bool(RETREAT.search(text))))


def kind_of(adv: Advice, focus: str = "") -> str:
    """Which row of SCORES a line is."""
    if getattr(adv, "asked", False):
        return "asked"
    if danger(adv.priority, adv.category, adv.text):
        return "danger"
    rule = getattr(adv, "rule_id", "") or (adv.key or "").split(":")[0]
    if focus and (focus in rule or focus == adv.category or rule in FOCUS_RULES.get(focus, ())):
        return "focus"
    if adv.category == "trigger":
        return "act_now"
    if adv.category in TIMER_CATEGORIES:
        return "timer_now" if adv.priority <= Priority.NORMAL else "timer"
    if (adv.key or "").startswith(("afford:", "own_way:")):
        return "buy_now"
    if adv.source == "coach":
        return "coach"
    return "general"


def rule_reason(engine: Any, rule_id: str) -> str:
    """The rule's own reason, from its plain-language docstring (its first two sentences), for "why?"."""
    for rule in getattr(engine, "rules", ()):
        if getattr(rule, "id", "") == rule_id:
            doc = " ".join((type(rule).__doc__ or "").split())
            return " ".join(re.split(r"(?<=[.!?])\s+", doc)[:2])
    return ""


def gate_for(cfg: Any, voice_max: int, store: Any, kb: Any = None) -> "SpeechGate":
    """The gate as the app (and the bench's replay) runs it: the match's state from the store, items from the
    knowledge."""

    def state() -> tuple[Any, bool]:
        snap = store.current
        return snap, bool(snap is not None and snap.has_hero and snap.alive and store.in_fight())

    def buys(text: str, snap: Any) -> set[str]:
        return kb.buy_mentions(text, snap.hero_name) if kb is not None and kb.items else set()

    def cost_left(item: str, snap: Any) -> int:
        return kb.cost_left(item, snap.item_list())

    return SpeechGate(cfg, voice_max, state=state, buys=buys if kb is not None else None,
                      cost_left=cost_left if kb is not None else None,
                      focus=lambda: store.memory.get("focus") or "")


@dataclass
class _Held:
    score: int
    until: float
    lines: list[Advice] = field(default_factory=list)


class SpeechGate:
    """See the module docstring. `cfg`: the CoachConfig (talk, level, own_way: read live). `voice_max`: the
    voice's own priority cut (VoiceConfig.min_priority): lines it wouldn't say anyway aren't counted. `state`:
    () -> (snapshot or None, fighting now). `buys`: (text, snapshot) -> the items a line says to buy, and
    `cost_left`: (item, snapshot) -> gold still to pay on it (both from Knowledge; without them, no item cap)."""

    def __init__(self, cfg: Any = None, voice_max: int = Priority.NORMAL,
                 state: Callable[[], tuple[Any, bool]] | None = None,
                 buys: Callable[[str, Any], set[str]] | None = None,
                 cost_left: Callable[[str, Any], int] | None = None,
                 focus: Callable[[], str] | None = None) -> None:
        self.cfg = cfg
        self.voice_max = int(voice_max)
        self.state = state or (lambda: (None, False))
        self.buys = buys
        self.cost_left = cost_left
        self.focus = focus or (lambda: "")
        self._said: list[tuple[float, bool]] = []  # (when, a coach line) of non-danger lines heard
        self._last_any = -1e9
        self._last_coach = -1e9
        self._fight_end = -1e9
        self._held: list[_Held] = []
        self._groups: dict[int, object] = {}  # coach answer card id -> True (said), False (dropped) or its _Held
        self._items: dict[str, list[int]] = {}  # item -> [buy lines naming it, gold left on it then]
        self.last: Advice | None = None  # the last non-danger line heard (what "why?" explains)
        self.held_dropped = 0  # lines that waited and never got a slot
        self._lock = threading.RLock()  # (lines come from the game-data loop, hotkeys and the coach)

    # the settings ------------------------------------------------------------------------------------------------
    @property
    def talk(self) -> Talk:
        return TALK[talk_of(self.cfg)]

    def _item_cap(self) -> int:
        return ITEM_CAP_REMIND if getattr(self.cfg, "own_way", "") == "remind" else ITEM_CAP

    # deciding ---------------------------------------------------------------------------------------------------------
    def admit(self, adv: Advice, now: float | None = None) -> None:
        """Called on every line before it's shown or said: turns `speak` off when it isn't said now (holding it
        for a slot if it's worth one). Thread-safe."""
        with self._lock:
            self._admit(adv, now)

    def _admit(self, adv: Advice, now: float | None) -> None:
        if not adv.final or not adv.speak or adv.priority > self.voice_max or getattr(adv, "_gated", False):
            return
        now = time.monotonic() if now is None else now
        adv._gated = True
        group = getattr(adv, "group", 0)
        if group and group in self._groups:  # the rest of a coach answer follows its first sentence
            decided = self._groups[group]
            if isinstance(decided, _Held):
                adv.speak = False
                decided.lines.append(adv)
            elif not decided:
                adv.speak = False
            return
        snap, fighting = self._state(now)
        kind = kind_of(adv, self._focus())
        if kind in ("asked", "danger"):
            self._heard(adv, now, kind, count=False)
            self._decide(group, True)
            return
        if self._over_cap(adv, snap):
            adv.speak = False
            self._decide(group, False)
            return
        score = score_of(kind, snap is not None and getattr(snap, "has_hero", False) and not snap.alive)
        best_held = max((h.score for h in self._held if h.until >= now), default=-1)
        if self._open(adv, now, snap, fighting) and score >= best_held:
            self._heard(adv, now, kind)
            self._decide(group, True)
            return
        adv.speak = False
        # (long enough to outlast the gap, so a line that came just after another can still be said; never past
        # the line's own staleness: a tip about this moment isn't said late)
        wait = max(HOLD_S, self.talk.gap_s + 2)
        wait = wait if adv.stale_s is None else min(wait, adv.stale_s)
        held = _Held(score, now + wait, [adv])
        self._held.append(held)
        self._decide(group, held)

    def release(self, now: float | None = None) -> list[Advice]:
        """Held lines whose moment has come (to be sent again, now said), best first; lines that waited too long
        are dropped. Thread-safe."""
        with self._lock:
            return self._release(now)

    def _release(self, now: float | None) -> list[Advice]:
        now = time.monotonic() if now is None else now
        snap, fighting = self._state(now)
        kept = [h for h in self._held if h.until >= now]
        self.held_dropped += len(self._held) - len(kept)
        for h in self._held:
            if h.until < now:
                self._settle(h, False)
        self._held = kept
        out: list[Advice] = []
        while self._held:
            best = max(self._held, key=lambda h: h.score)
            first = best.lines[0]
            if not self._open(first, now, snap, fighting):
                break
            self._held.remove(best)
            self._heard(first, now, kind_of(first, self._focus()))
            self._settle(best, True)
            for adv in best.lines:
                adv.speak = True
                adv.stale_s = max(0.5, best.until - now)  # (the voice's own deadline: never said late)
                out.append(adv)
        return out

    # the parts ---------------------------------------------------------------------------------------------------------
    def _state(self, now: float) -> tuple[Any, bool]:
        try:
            snap, fighting = self.state()
        except Exception:
            snap, fighting = None, False
        if fighting:
            self._fight_end = now
        return snap, bool(fighting)

    def _focus(self) -> str:
        try:
            return str(self.focus() or "")
        except Exception:
            return ""

    def _open(self, adv: Advice, now: float, snap: Any, fighting: bool) -> bool:
        """A slot for this line now: no fight going on or just over (unless dead), under the minute's budget, past
        the gap since the last line, and for the coach, past the gap since its last line."""
        dead = snap is not None and getattr(snap, "has_hero", False) and not snap.alive
        if not dead and (fighting or now - self._fight_end < AFTER_FIGHT_S):
            return False
        talk = self.talk
        recent = [t for t, _coach in self._said if now - t < WINDOW_S]
        if len(recent) >= talk.per_min or now - self._last_any < talk.gap_s:
            return False
        return not (adv.source == "coach" and now - self._last_coach < talk.coach_gap_s)

    def _heard(self, adv: Advice, now: float, kind: str, count: bool = True) -> None:
        if count:
            self._said = [(t, c) for t, c in self._said if now - t < WINDOW_S] + [(now, adv.source == "coach")]
            self._last_any = now
            if adv.source == "coach":
                self._last_coach = now
        if kind != "danger":
            self.last = adv
        self._count_items(adv)

    def _decide(self, group: int, verdict: object) -> None:
        if group:
            self._groups[group] = verdict
            if len(self._groups) > 64:
                for old in list(self._groups)[:-32]:
                    del self._groups[old]

    def _settle(self, held: _Held, said: bool) -> None:
        for gid, v in list(self._groups.items()):
            if v is held:
                self._groups[gid] = said

    def _items_in(self, adv: Advice, snap: Any) -> set[str]:
        items: set[str] = set()
        key = adv.key or ""
        if key.startswith("afford:") and key.count(":") == 1:
            items.add(key.split(":", 1)[1])
        if self.buys is not None and snap is not None:
            try:
                items |= set(self.buys(adv.text, snap))
            except Exception:
                pass
        return items

    def _over_cap(self, adv: Advice, snap: Any) -> bool:
        if self.cost_left is None or snap is None:
            return False
        for item in self._items_in(adv, snap):
            n, was = self._items.get(item, (0, None))
            try:
                left = self.cost_left(item, snap)
            except Exception:
                continue
            if n >= self._item_cap() and (was is None or left >= was):
                return True
        return False

    def _count_items(self, adv: Advice) -> None:
        if self.cost_left is None:
            return
        snap, _f = self.state() if self.state else (None, False)
        if snap is None:
            return
        for item in self._items_in(adv, snap):
            try:
                left = self.cost_left(item, snap)
            except Exception:
                continue
            n, was = self._items.get(item, (0, left))
            self._items[item] = [1 if left < was else n + 1, left]
