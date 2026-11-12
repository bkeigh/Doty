"""Post-game review: what went wrong in the match you just played, and three things to work on.

Everything comes from data Doty already sees while it coaches: the game data Dota sends ~10 times a
second, and the lines Doty itself said. `MatchRecorder` keeps a small record of each match (a sample a
minute, when your big items arrived, and a close look at every death), and when the match ends it turns
that into a `Review`, saved as one JSON file per match so the app can show it later.

The death causes are simple heuristics, not a replay analysis: Doty can't see the enemies, only you. So
they are worded as "likely" and only fire on clear signs (HP under a third for seconds before dying, no
TP scroll, deep on the enemy's side, a warning shortly before, a big pile of unspent gold).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from .advice import Advice, Priority
from .config import LOG_DIR
from .knowledge.constants import NOT_BUILD_TARGETS, Knowledge
from .rules.base import mmss
from .knowledge import focus as focus_lib
from .knowledge import mode as game_mode
from .state import DEMO_MATCH, POST_GAME, GameStateStore, HistoryPoint, Snapshot
from .rules.survival import TRAVEL_BOOTS

REVIEW_DIR = LOG_DIR.parent / "reviews"

LOW_HP = 0.35  # "low HP" for the death check: about a third of your health
LOW_HP_S = 5.0  # ...for at least this long right before dying
HP_WINDOW_S = 10.0  # how much HP history each death keeps
SAID_WINDOW_S = 20  # game seconds of Doty's lines kept with each death
UNSPENT_GOLD = 2000
# The river runs roughly along x + y = 0 (Radiant bottom-left, Dire top-right, world coords ~±8000).
# Past this, you're beyond the enemy's first towers or in their jungle.
DEEP_ENEMY_SIDE = 4000
SAME_DEATH_S = 3  # the alive flag and the death counter can arrive a payload apart
JOINED_LATE_S = 60  # first seen after this: items already owned have no known timing
FARM_BEHIND = 0.75  # last-hit pace under this share of the hero's benchmark is worth a focus point
RESUME_WITHIN_S = 3 * 3600  # a match's unfinished review this recent is picked back up after a restart
LATE_ITEM_S = 180  # a core item this much later than half the hero's players have it is worth a focus point
ODDS_GAP = 0.05  # win-rate points between your timing and the usual one worth mentioning
MANY_DEATHS = 8

# Without item data (first run offline) Doty can't look up costs; skip what's surely not a big buy.
SMALL_ITEMS = NOT_BUILD_TARGETS | {
    "magic_wand", "wraith_band", "null_talisman", "bracer", "aegis", "cheese", "refresher_shard",
    "cornucopia", "ring_of_protection", "ring_of_regen", "sobi_mask", "gloves", "blades_of_attack",
    "belt_of_strength", "boots_of_elves", "robe", "wind_lace", "fluffy_hat", "orb_of_venom",
    "blight_stone", "lifesteal", "chainmail", "ring_of_health", "void_stone", "infused_raindrop",
}
WARNING_CATEGORIES = {"survival", "danger"}

CAUSE_TEXT = {
    "low_hp_fight": "likely stayed in a fight on low HP",
    "no_tp": "had no TP scroll",
    "enemy_side": "was deep on the enemy's side of the map",
    "after_warning": "died within 20 s of a warning from Doty",
    "unspent_gold": f"died holding {UNSPENT_GOLD:,}+ unspent gold",
}
# How much each cause counts per death when ranking focus points. Ignoring a warning and staying in a
# lost fight are the most fixable habits, so they edge ahead on ties.
CAUSE_WEIGHT = {"after_warning": 1.3, "low_hp_fight": 1.2, "no_tp": 1.0, "enemy_side": 1.0, "unspent_gold": 0.8}


@dataclass
class Death:
    clock: int
    level: int = 0
    position: tuple[int, int] | None = None
    hp_trace: list[tuple[float, float]] = field(default_factory=list)  # (seconds before death, HP fraction)
    low_hp_s: float = 0.0  # how long HP had been under LOW_HP right before dying
    had_tp: bool | None = None  # None: Doty couldn't see your items
    gold_before: int = 0
    gold_lost: int = 0
    items: list[str] = field(default_factory=list)
    said: list[dict[str, Any]] = field(default_factory=list)  # Doty's lines in the 20 s before
    causes: list[str] = field(default_factory=list)
    unspent_points: int | None = None  # skill points waiting to be spent when you died (None: not known)
    buyback_ready: bool | None = None  # buyback off cooldown and the gold for it, as you died (None: not known)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Death:
        d = _known(cls, d)
        if d.get("position") is not None:
            d["position"] = tuple(d["position"])
        d["hp_trace"] = [tuple(p) for p in d.get("hp_trace") or []]
        return cls(**d)


@dataclass
class Review:
    match_id: str = ""
    hero: str = ""  # npc name, e.g. npc_dota_hero_spirit_breaker
    hero_name: str = ""  # display name, e.g. Spirit Breaker
    hero_id: int = 0
    team: str = ""  # radiant | dire
    won: bool | None = None  # None: the result wasn't seen (Doty stopped first)
    duration: int = 0  # game clock at the end, seconds
    recorded_from: int = 0  # clock when Doty started watching (later than 0:00 if it joined mid-game)
    kda: tuple[int, int, int] = (0, 0, 0)
    last_hits: int = 0
    denies: int = 0
    lh_10: int | None = None  # last hits and denies at 10:00
    dn_10: int | None = None
    gpm: int = 0
    xpm: int = 0
    items: list[tuple[str, int]] = field(default_factory=list)  # (item name, clock it arrived)
    # Your core items next to the hero's players: (item name, clock it arrived, clock by which half of
    # them have it). Only items OpenDota has timings for (the hero's four most-bought).
    item_pace: list[tuple[str, int, int]] = field(default_factory=list)
    # For the same items: (item name, win rate of the hero's players who finish it when you did, when they
    # usually do). Public statistics; only shown when the gap is real.
    item_odds: list[tuple[str, float, float]] = field(default_factory=list)
    bought: list[tuple[str, int]] = field(default_factory=list)  # (item key, clock): to pick a match back up
    # Every item that arrived, parts and consumables included, once per copy: (item key, clock). What the bench
    # needs to know what you held when a line was said (`bought` has only the big items).
    purchases: list[tuple[str, int]] = field(default_factory=list)
    # Seconds you held a skill point past a short grace after each level-up (None: not watched).
    unspent_point_s: int | None = None
    role: str = ""  # "core" | "support" | "" (knowledge/roles.py, as it stood at the end)
    # This game's focus (knowledge/focus.py) and how it went: {"key", "title", "value", "target", "hit", "trend",
    # "source", "item_name"}; {} when there was none.
    practice: dict[str, Any] = field(default_factory=dict)
    deaths: list[Death] = field(default_factory=list)
    lh_per_min_10: float | None = None
    benchmark_lh_per_min: float | None = None  # the hero's median over whole games (OpenDota)
    typical_items: list[str] = field(default_factory=list)
    minutes: list[dict[str, int]] = field(default_factory=list)  # one sample per game minute
    focus: list[str] = field(default_factory=list)  # up to three points to work on, most important first
    saved_at: float = 0.0
    mode: str = "normal"  # "normal" | "turbo" (knowledge/mode.py): the timings above are on that mode's clock
    demo: bool = False  # Doty's scripted demo, not a game you played: nothing learns about you from it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Review:
        d = _known(cls, d)
        d["kda"] = tuple(d.get("kda") or (0, 0, 0))
        d["items"] = [tuple(i) for i in d.get("items") or []]
        d["item_pace"] = [tuple(i) for i in d.get("item_pace") or []]
        d["item_odds"] = [tuple(i) for i in d.get("item_odds") or []]
        d["bought"] = [tuple(i) for i in d.get("bought") or []]
        d["purchases"] = [tuple(i) for i in d.get("purchases") or []]
        d["deaths"] = [Death.from_dict(x) for x in d.get("deaths") or []]
        # (reviews saved before `demo` existed: the scripted match's id says so)
        d["demo"] = bool(d.get("demo")) or str(d.get("match_id") or "").startswith(DEMO_MATCH)
        return cls(**d)


def _known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """Only the fields this version knows, so older or newer files still load."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# death causes and focus points ------------------------------------------------------------------
def _enemy_depth(pos: tuple[int, int] | None, team: str) -> int | None:
    """How far past the river toward the enemy base (world units along x + y); None if unknown."""
    if pos is None or team not in ("radiant", "dire"):
        return None
    toward_dire = pos[0] + pos[1]
    return toward_dire if team == "radiant" else -toward_dire


def _is_warning(line: dict[str, Any]) -> bool:
    if line.get("category") in WARNING_CATEGORIES:
        return True
    # The coach's real advice, not its quiet observations or "coach is off" notices.
    return line.get("category") == "coach" and _int(line.get("priority")) <= Priority.HIGH


def death_causes(death: Death, team: str) -> list[str]:
    """Tags for what likely went wrong; see CAUSE_TEXT."""
    causes = []
    if death.low_hp_s >= LOW_HP_S:
        causes.append("low_hp_fight")
    if death.had_tp is False:
        causes.append("no_tp")
    depth = _enemy_depth(death.position, team)
    if depth is not None and depth >= DEEP_ENEMY_SIDE:
        causes.append("enemy_side")
    if any(_is_warning(line) for line in death.said):
        causes.append("after_warning")
    if death.gold_before >= UNSPENT_GOLD:
        causes.append("unspent_gold")
    return causes


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _of_deaths(n: int, total: int) -> str:
    if total == 1:
        return "Your death"
    return "One of your deaths" if n == 1 else f"{n} of your deaths"


def _cause_point(cause: str, n: int, total: int) -> str:
    deaths = _of_deaths(n, total)
    return {
        "no_tp": f"You died {_times(n)} without a TP scroll: always carry one.",
        "after_warning": f"{deaths} came within 20 s of a warning: back off when Doty says so.",
        "low_hp_fight": f"{deaths} likely came from staying in a fight on low HP: walk away once you drop "
                        f"under a third of your health.",
        "enemy_side": f"{deaths} happened deep on the enemy's side of the map: only go there with your team "
                      f"or vision.",
        "unspent_gold": f"You died {_times(n)} holding {UNSPENT_GOLD:,}+ gold: buy items before you fight.",
    }[cause]


def _farm_point(review: Review) -> tuple[float, str] | None:
    rate, median = review.lh_per_min_10, review.benchmark_lh_per_min
    if rate is None or not median or rate >= median * FARM_BEHIND:
        return None
    hero = review.hero_name or "this hero's"
    text = (f"{review.lh_10} last hits at 10:00; {hero} players average {median:.1f} a minute "
            f"({median * 10:.0f} in ten minutes): last-hit every wave and farm between fights.")
    return (1 - rate / median) * 10, text


def _item_point(review: Review) -> tuple[float, str] | None:
    """The core item that came latest against the hero's players, if it was LATE_ITEM_S or more behind."""
    late = [(clock - usual, name, clock, usual) for name, clock, usual in review.item_pace if usual]
    if not late:
        return None
    behind, name, clock, usual = max(late)
    if behind < LATE_ITEM_S:
        return None
    hero = review.hero_name or "this hero's"
    text = (f"Your {name} came at {mmss(clock)}; {hero} players usually have it by {mmss(usual)}. Farm "
            f"between fights, and skip buys that don't get you there sooner.")
    odds = next(((y, t) for n, y, t in review.item_odds if n == name), None)
    if odds and odds[1] - odds[0] >= ODDS_GAP:
        text += (f" Players who finish it as late as you did win {odds[0]:.0%} of their games; by "
                 f"{mmss(usual)}, {odds[1]:.0%}.")
    return min(1.5, 0.5 + behind / 600), text


def focus_points(review: Review) -> list[str]:
    """Up to three plain-English things to work on, the biggest first.

    Each death with a cause counts about one point (weighted by CAUSE_WEIGHT); a last-hit pace 10%
    under the benchmark also counts about one point, and a core item 3+ minutes behind the hero's
    players 0.8 to 1.5 (more the later it was)."""
    total = len(review.deaths)
    counts = Counter(c for d in review.deaths for c in d.causes)
    scored = [(n * CAUSE_WEIGHT[cause], _cause_point(cause, n, total))
              for cause, n in counts.items() if cause in CAUSE_WEIGHT]
    # (A Turbo game has no benchmark of its own yet: its farm and item pace aren't judged by a normal game's.)
    for extra in (() if review.mode == "turbo" else (_farm_point(review), _item_point(review))):
        if extra:
            scored.append(extra)
    if total >= MANY_DEATHS and max(counts.values(), default=0) < total / 2:  # no single cause explains them
        scored.append((total * 0.4, f"You died {total} times: before you walk forward, check the minimap "
                                    f"for missing enemies and where your team is."))
    scored.sort(key=lambda s: -s[0])
    return [text for _, text in scored[:3]]


# recording --------------------------------------------------------------------------------------
def _low_hp_seconds(points: list[HistoryPoint]) -> float:
    """How long HP had been under LOW_HP, counting back from the last moment alive."""
    if not points or points[-1].hp_pct >= LOW_HP:
        return 0.0
    start = points[-1].t
    for p in reversed(points):
        if p.hp_pct >= LOW_HP:
            break
        start = p.t
    return round(points[-1].t - start, 1)


def _hp_trace(points: list[HistoryPoint], death_t: float) -> list[tuple[float, float]]:
    """About one HP reading a second, enough to draw the last 10 s without bloating the file."""
    out: list[tuple[float, float]] = []
    last_t = None
    for p in reversed(points):
        if last_t is None or last_t - p.t >= 1.0:
            out.append((round(p.t - death_t, 1), round(p.hp_pct, 2)))
            last_t = p.t
    return out[::-1]


def _had_tp(snap: Snapshot) -> bool | None:
    if not snap.has_items_section:
        return None
    return snap.teleport is not None or bool(snap.all_item_names() & TRAVEL_BOOTS)


class MatchRecorder:
    """Watches one match at a time and writes its review when it ends.

    Feed it every snapshot (`on_snapshot`, before the rule engine so a death is recorded before the
    rules react to it) and every advice line (`on_advice`, subscribed to the bus). It finishes on its own
    at post-game or when a new match starts; call `finish()` when Doty stops."""

    def __init__(self, kb: Knowledge | None, out_dir: Path | None = None) -> None:
        self.kb = kb
        self.out_dir = out_dir if out_dir is not None else REVIEW_DIR
        self.last_review: Review | None = None
        self.last_path: Path | None = None
        self._lock = threading.RLock()  # advice arrives from other threads (hotkeys, voice input)
        self._failed = False
        self._generation: int | None = None  # the store's match count, to tell same-id games apart
        self._reset("")

    def _reset(self, match_id: str, fresh: bool = False) -> None:
        self._match_id = match_id
        self._fresh = fresh  # a new game in this session: never resume an older save under the same id
        self._ended = False  # post-game reached and the review written
        self._mode = "normal"  # the match's mode as of its latest update (knowledge/mode.py)
        self._saw_game = False
        self._clock = 0
        self._recorded_from = 0
        self._duration = 0
        self._hero = ""
        self._hero_id = 0
        self._team = ""
        self._win_team = ""
        self._last_player: dict[str, Any] = {}
        self._was_alive: bool | None = None
        self._last_alive: Snapshot | None = None
        self._deaths_count: int | None = None
        self._history: deque[HistoryPoint] = deque(maxlen=200)
        self._said: deque[dict[str, Any]] = deque(maxlen=200)
        self._said_ids: deque[int] = deque(maxlen=32)  # a card published again (drawings added) counts once
        self._seen_items: set[str] = set()
        self._items: list[tuple[str, int]] = []
        self._bought: list[tuple[str, int]] = []  # (item key, clock) for the same items, for timings
        self._purchases: list[tuple[str, int]] = []  # (item key, clock) for every copy of every item that arrived
        self._held: Counter[str] | None = None  # what was held at the last update (None: not seen yet)
        self._unspent_s = 0  # seconds holding a skill point past the grace
        self._point_since: int | None = None  # clock a skill point started waiting
        self._point_clock: int | None = None  # the last clock counted
        self._role: Any = None
        self._focus: dict[str, Any] = {}  # store.memory["focus_plan"]: this game's focus, set at the horn
        self._minutes: list[dict[str, int]] = []
        self._deaths: list[Death] = []

    # inputs ------------------------------------------------------------------------------------
    def on_snapshot(self, snap: Snapshot, store: GameStateStore | None = None) -> None:
        """Every GSI payload. `store` (already holding this snapshot) supplies the HP history."""
        with self._lock:
            try:
                self._on_snapshot(snap, store)
            except Exception as exc:  # the review must never break coaching
                if not self._failed:
                    print(f"[doty] post-game review skipped a game update: {exc!r}")
                    self._failed = True

    def on_advice(self, adv: Advice) -> None:
        """Every advice line; keeps what was actually shown, stamped with the game clock."""
        if not adv.final or not adv.display or adv.source == "player":
            return
        with self._lock:
            if adv.id in self._said_ids:
                return
            self._said_ids.append(adv.id)
            self._said.append({"clock": self._clock, "source": adv.source, "category": adv.category,
                               "priority": int(adv.priority), "text": adv.text})

    def finish(self) -> Review | None:
        """Write the review for the match so far. None if there's nothing to review or it's already
        written for a finished match. Safe to call more than once."""
        with self._lock:
            return self._finish(ended=False)

    # per snapshot ------------------------------------------------------------------------------
    def _on_snapshot(self, snap: Snapshot, store: GameStateStore | None) -> None:
        mid = snap.match_id
        gen = store.generation if store is not None else None
        new_game = gen is not None and self._generation is not None and gen != self._generation
        if (mid and mid != self._match_id) or new_game:
            # (a new game under the same id: bot and local games can all say "0")
            self._finish(ended=True)
            self._reset(mid or self._match_id, fresh=new_game)
        if gen is not None:
            self._generation = gen
        if self._ended:
            return
        game_map = snap.raw.get("map")
        if not isinstance(game_map, dict) or "clock_time" not in game_map:
            return  # no clock, nothing to stamp; the next full payload catches up
        self._clock = snap.clock
        if self.kb is not None:
            self._mode = self.kb.mode.key
        if snap.game_state == POST_GAME:
            self._note_end(snap)
            self._finish(ended=True)
            return
        if not snap.has_hero or snap.in_draft:
            return
        if not self._saw_game:
            self._start(snap)
        if snap.hero("health") is not None:
            self._history.append(HistoryPoint(snap.received_t, snap.clock, snap.hp_pct, snap.gold, snap.alive))
        self._check_death(snap, store)
        self._check_items(snap)
        self._sample_minute(snap)
        self._watch_points(snap)
        if store is not None:
            self._role = store.memory.get("role", self._role)
            plan = store.memory.get("focus_plan")
            if isinstance(plan, dict):
                self._focus = plan
        self._note_end(snap)

    def _unspent(self, snap: Snapshot) -> int | None:
        if self.kb is not None and self.kb.hero_abilities:
            try:
                return self.kb.unspent_points(snap)
            except Exception:
                return None
        return snap.unspent_skill_points

    def _watch_points(self, snap: Snapshot) -> None:
        """Time spent holding a skill point, once a level-up's few seconds of grace are over (alive only)."""
        points = self._unspent(snap) if snap.alive and snap.clock >= 0 else None
        if not points:
            self._point_since = None
        elif self._point_since is None:
            self._point_since = snap.clock
        elif snap.clock - self._point_since > focus_lib.SKILL_POINT_GRACE_S and self._point_clock is not None:
            self._unspent_s += max(0, min(5, snap.clock - self._point_clock))  # (a gap in the feed isn't held time)
        self._point_clock = snap.clock

    def _start(self, snap: Snapshot) -> None:
        self._saw_game = True
        self._recorded_from = snap.clock
        if snap.clock > JOINED_LATE_S:  # we don't know when these were bought
            self._seen_items |= {it.name for it in snap.inventory + snap.backpack + snap.stash}
        self._resume(snap)

    def _resume(self, snap: Snapshot | None = None) -> None:
        """Doty restarted mid-match (saving settings restarts its engine, and stopping saved what it had):
        carry on from that save instead of starting over and overwriting it at the end."""
        if not self._match_id or self._fresh:
            return
        try:
            old = load_review(self.out_dir / _file_name(self._match_id))
        except (OSError, ValueError, TypeError, KeyError):
            return
        if old.won is not None or time.time() - old.saved_at > RESUME_WITHIN_S:
            return  # that match was over, or it's an old file
        if snap is not None and ((old.hero and snap.has_hero and old.hero != snap.hero_name)
                                 or snap.clock < old.duration - 60):
            return  # another game with the same id (another hero, or a clock that started over)
        self._recorded_from = min(self._recorded_from, old.recorded_from)
        self._deaths = list(old.deaths)
        self._items = list(old.items)
        self._bought = list(old.bought)
        self._purchases = list(old.purchases)
        self._seen_items |= {key for key, _clock in old.bought}
        self._minutes = list(old.minutes)
        self._duration = max(self._duration, old.duration)

    def _note_end(self, snap: Snapshot) -> None:
        """Keep what the review's header needs, from the latest payload that has it."""
        self._duration = max(self._duration, snap.clock)
        if snap.has_hero:
            self._hero, self._hero_id = snap.hero_name, snap.hero_id
        if snap.team in ("radiant", "dire"):
            self._team = snap.team
        player = snap.raw.get("player")
        if isinstance(player, dict) and player:
            self._last_player = player
        win = snap.raw["map"].get("win_team")  # "none" until the ancient falls
        if win in ("radiant", "dire"):
            self._win_team = win

    def _check_death(self, snap: Snapshot, store: GameStateStore | None) -> None:
        alive = snap.hero("alive")
        alive = None if alive is None else bool(alive)
        count = snap.player("deaths", None)
        count = None if count is None else _int(count)
        # The death counter is the real thing; "alive" going false alone may be an Aegis or Reincarnation
        # revive, which the store tells apart (just_died) when it's there.
        went_down = self._was_alive is True and alive is False and (store is None or store.just_died)
        counted = count is not None and self._deaths_count is not None and count > self._deaths_count
        same_death = bool(self._deaths) and snap.clock - self._deaths[-1].clock < SAME_DEATH_S
        if (went_down or counted) and not same_death:
            self._record_death(snap, store)
        if alive is not None:
            self._was_alive = alive
            if alive:
                self._last_alive = snap
        if count is not None:
            self._deaths_count = count

    def _record_death(self, snap: Snapshot, store: GameStateStore | None) -> None:
        before = self._last_alive or snap  # the last moment we saw you alive
        history: Iterable[HistoryPoint] = store.history if store is not None else self._history
        points = [p for p in history if p.alive and snap.received_t - HP_WINDOW_S <= p.t <= snap.received_t]
        gold_before, gold_now = before.player("gold", None), snap.player("gold", None)
        gold_lost = 0
        if gold_before is not None and gold_now is not None:
            gold_lost = max(0, _int(gold_before) - _int(gold_now))
        death = Death(
            clock=snap.clock,
            level=before.level,
            position=before.position,
            hp_trace=_hp_trace(points, snap.received_t),
            low_hp_s=_low_hp_seconds(points),
            had_tp=_had_tp(before),
            gold_before=_int(gold_before),
            gold_lost=gold_lost,
            items=[self._item_label(it.name) for it in before.inventory + before.backpack],
            said=[dict(line) for line in self._said if snap.clock - SAID_WINDOW_S <= line["clock"] <= snap.clock],
        )
        death.causes = death_causes(death, self._team or snap.team)
        death.unspent_points = self._unspent(before)
        cost, cooldown = snap.hero("buyback_cost", None), snap.hero("buyback_cooldown", None)
        if cost is not None and gold_now is not None:
            death.buyback_ready = _int(cooldown) <= 0 and _int(gold_now) >= _int(cost)
        self._deaths.append(death)

    def _check_items(self, snap: Snapshot) -> None:
        if snap.has_items_section:
            # Every copy that arrived since the last update (a second Ogre Axe too). Doty joining a game late, or
            # picking a saved match back up, doesn't know when the items already held were bought: those aren't
            # new purchases.
            held = Counter(snap.item_list() + ([snap.teleport.name] if snap.teleport else []))
            if self._held is None and snap.clock <= JOINED_LATE_S and not self._purchases:
                self._held = Counter()
            if self._held is not None:
                for item, n in sorted(held.items()):
                    self._purchases += [(item, snap.clock)] * max(0, n - self._held[item])
            self._held = held
        for it in snap.inventory + snap.backpack + snap.stash:
            if it.name in self._seen_items:
                continue
            self._seen_items.add(it.name)
            if self._is_big(it.name):
                self._items.append((self._item_label(it.name), snap.clock))
                self._bought.append((it.name, snap.clock))

    def _sample_minute(self, snap: Snapshot) -> None:
        if snap.clock < 0 or (self._minutes and self._minutes[-1]["clock"] // 60 >= snap.clock // 60):
            return
        sample = {"clock": snap.clock, "gold": snap.gold, "level": snap.level}
        for key in ("gpm", "xpm", "last_hits", "denies", "kills", "deaths", "assists"):
            sample[key] = _int(snap.player(key, 0))
        self._minutes.append(sample)

    # names -------------------------------------------------------------------------------------
    def _is_big(self, item: str) -> bool:
        if self.kb is not None and self.kb.items:
            return self.kb._is_build_target(item)
        short = item.removeprefix("item_")
        return not (short in SMALL_ITEMS or short.startswith("recipe_"))

    def _item_label(self, item: str) -> str:
        if self.kb is not None:
            return self.kb.item_name(item)
        return item.removeprefix("item_").replace("_", " ").title()

    def _hero_label(self, npc: str) -> str:
        if self.kb is not None:
            return self.kb.hero_name(npc)
        return npc.removeprefix("npc_dota_hero_").replace("_", " ").title()

    # the review ----------------------------------------------------------------------------------
    def _finish(self, ended: bool) -> Review | None:
        if self._ended or not self._saw_game:
            self._ended = self._ended or ended
            return None
        self._ended = ended
        review = self._build()
        if ended and not review.demo:
            self._grade_focus(review)  # (a match cut short isn't graded: its numbers aren't the game's)
        self.last_review = review
        self.last_path = self._save(review)
        return review

    focus_history: Path | None = None  # (tests point it elsewhere; None = focus.HISTORY_FILE)

    def _grade_focus(self, review: Review) -> None:
        """Every focus topic's number for this game into the history (knowledge/focus.py), and this game's focus,
        its target, hit or missed and its trend into the review."""
        focus = focus_lib
        plan = self._focus or {}
        ctx = {"hero_id": review.hero_id, "role": self._role, "mode": review.mode,
               "bracket": getattr(self.kb, "bracket", 0) if self.kb is not None else 0,
               "level": plan.get("level", "some"),
               "cache_dir": getattr(self.kb, "cache_dir", None) if self.kb is not None else None}
        try:
            results = focus.grade(review, ctx)
            key = plan.get("key", "")
            history = focus.add_game(focus.entry_for(review, results, key, ctx["level"], self._role),
                                     self.focus_history)
        except Exception as exc:  # the review must never fail on its focus
            print(f"[doty] couldn't grade this game's focus: {exc!r}")
            return
        topic = focus.TOPICS.get(key)
        if topic is None or key not in results:
            return
        value, target = results[key]
        role = focus.role_group(self._role)
        review.practice = {"key": key, "title": topic.title, "value": value, "target": target,
                           "hit": topic.hit(value, target), "trend": focus.trend(history, key, review.mode, role),
                           "source": plan.get("source", ""), "item_name": plan.get("item_name", "")}

    def _build(self) -> Review:
        p = self._last_player
        at_10 = next((m for m in self._minutes if 600 <= m["clock"] < 630), None)
        review = Review(
            match_id=self._match_id,
            hero=self._hero,
            hero_name=self._hero_label(self._hero) if self._hero else "",
            hero_id=self._hero_id,
            team=self._team,
            won=(self._win_team == self._team) if self._win_team and self._team else None,
            duration=self._duration,
            recorded_from=self._recorded_from,
            kda=(_int(p.get("kills")), _int(p.get("deaths")), _int(p.get("assists"))),
            last_hits=_int(p.get("last_hits")),
            denies=_int(p.get("denies")),
            lh_10=at_10["last_hits"] if at_10 else None,
            dn_10=at_10["denies"] if at_10 else None,
            gpm=_int(p.get("gpm")),
            xpm=_int(p.get("xpm")),
            items=list(self._items),
            bought=list(self._bought),
            purchases=list(self._purchases),
            unspent_point_s=self._unspent_s,
            role=focus_lib.role_group(self._role),
            deaths=list(self._deaths),
            minutes=list(self._minutes),
            saved_at=time.time(),
            mode=self._mode,
            demo=self._match_id.startswith(DEMO_MATCH),
        )
        if at_10:
            review.lh_per_min_10 = round(at_10["last_hits"] / 10, 2)
        if self.kb is not None and self._hero_id:
            median = self.kb.benchmark_median(self._hero_id, "last_hits_per_min")
            review.benchmark_lh_per_min = round(float(median), 2) if median else None
            review.typical_items = [self.kb.item_name(i) for i in self.kb.typical_items(self._hero_id, 6)]
            for item, clock in self._bought:
                usual = self.kb.typical_time(self._hero_id, item)
                if usual:
                    review.item_pace.append((self._item_label(item), clock, usual))
                    yours, theirs = (self.kb.win_rate_by(self._hero_id, item, clock),
                                     self.kb.win_rate_by(self._hero_id, item, usual))
                    if yours is not None and theirs is not None:
                        review.item_odds.append((self._item_label(item), round(yours, 3), round(theirs, 3)))
        review.focus = focus_points(review)
        return review

    def _save(self, review: Review) -> Path | None:
        path = self.out_dir / _file_name(review.match_id)
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(review.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"[doty] couldn't save the post-game review: {exc!r}")
            return None
        return path


# saved reviews ----------------------------------------------------------------------------------
def _file_name(match_id: str) -> str:
    if match_id:
        return "match-" + re.sub(r"[^A-Za-z0-9_-]", "_", match_id) + ".json"
    return time.strftime("match-%Y%m%d-%H%M%S.json")


def list_reviews(dir: Path | None = None) -> list[Path]:
    """Saved review files, newest first."""
    folder = Path(dir) if dir is not None else REVIEW_DIR
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            found.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    return [path for *_, path in sorted(found, reverse=True)]


def load_review(path: Path) -> Review:
    return Review.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def latest_review(dir: Path | None = None, demos: bool = False) -> Review | None:
    """The newest review that still reads; None if there are none. The scripted demo's review only counts
    with `demos` (the recap right after it): it isn't a game you played."""
    for path in list_reviews(dir):
        try:
            review = load_review(path)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if demos or not review.demo:
            return review
    return None


def _death_line(d: Death) -> str:
    bits = [f"{mmss(d.clock)}, level {d.level}"]
    if d.causes:
        bits.append("; ".join(CAUSE_TEXT[c] for c in d.causes if c in CAUSE_TEXT))
    if d.low_hp_s and "low_hp_fight" not in d.causes:
        bits.append(f"HP under a third for {d.low_hp_s:.0f} s")
    if d.gold_before:
        bits.append(f"{d.gold_before:,} gold, lost {d.gold_lost:,}")
    warned = [line for line in d.said if _is_warning(line)][-2:]
    bits += [f'Doty said "{str(line.get("text", ""))[:90]}" at {mmss(_int(line.get("clock")))}' for line in warned]
    return " | ".join(bits)


def death_prompt(review: Review, index: int) -> str:
    """A question about one death in the review, for the coach: what happened and what would have saved it."""
    d = review.deaths[index]
    items = ", ".join(d.items) if d.items else "unknown"
    return (f"About my death at {mmss(d.clock)} as {review.hero_name or 'my hero'} (this match): "
            f"{_death_line(d)}. My items: {items}. In two or three short sentences: what most likely killed me, "
            "and the one thing I should have done differently (a position, an item to press, a TP, or not being "
            "there).")


def review_prompt(review: Review) -> str:
    """Compact text an AI can turn into three coaching points (the caller sends it)."""
    r = review
    result = {True: "won", False: "lost"}.get(r.won, "result not seen")
    k, d, a = r.kda
    lines = [
        "A Dota 2 player just finished a match. This is what Doty, their live coach, recorded "
        "(times are m:ss of game time).",
        f"Hero: {r.hero_name or 'unknown'} ({r.team or 'team unknown'}), {result}, game length {mmss(r.duration)}.",
        *([game_mode.REVIEW_NOTE] if r.mode == "turbo" else []),
        f"KDA {k}/{d}/{a}; GPM {r.gpm}, XPM {r.xpm}; last hits {r.last_hits}, denies {r.denies}.",
    ]
    if r.recorded_from > JOINED_LATE_S:
        lines.append(f"Doty only saw the game from {mmss(r.recorded_from)} on.")
    if r.lh_10 is not None:
        farm = f"Last hits at 10:00: {r.lh_10} (denies {r.dn_10})"
        if r.benchmark_lh_per_min:
            farm += f"; typical {r.hero_name} games average {r.benchmark_lh_per_min:.1f} a minute over the whole game"
        lines.append(farm + ".")
    if r.items:
        usual = {name: t for name, _clock, t in r.item_pace}
        lines.append("Item timings: " + ", ".join(
            f"{name} {mmss(clock)}" + (f" (half of {r.hero_name or 'this hero'}'s players have it by "
                                        f"{mmss(usual[name])})" if name in usual else "")
            for name, clock in r.items) + ".")
    if r.typical_items:
        lines.append("Items this hero's players usually buy: " + ", ".join(r.typical_items) + ".")
    lines.append(f"Deaths ({len(r.deaths)}):" if r.deaths else "Deaths: none seen.")
    lines += ["- " + _death_line(death) for death in r.deaths[:15]]
    if r.focus:
        lines.append("Doty's own reading, from simple rules: " + " ".join(r.focus))
    lines.append("Write three short coaching points for this player, most important first: one plain sentence "
                 "each, with something concrete to practise next game. Use only this data, and say \"likely\" "
                 "where a cause is a guess.")
    return "\n".join(lines)
