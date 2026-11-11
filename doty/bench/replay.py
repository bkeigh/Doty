"""Replay a recorded match through Doty offline, at full speed, and collect every line it says.

The same parts the app runs on every game update (app.Doty._on_payload): a real GameStateStore, the game mode,
the player's role and own build, the RuleEngine with the default rules, and optionally the AI coach. What's left
out is everything that needs the outside world: no voice, no overlay, no screenshots, no network.

Time is simulated. Rule cooldowns, the coach's look rhythm and prepared calls all read `time.monotonic()` and
`time.perf_counter()`; during a replay both return the recording's own time (`SimClock`), so a 30-minute match
replays in a second or two and the same input gives the same lines, every run.

The coach is optional:
  (a) rules only (the default);
  (b) `ScriptedBackend(answers)`: the coach runs for real (when to look, what it's told, how its answer is vetted,
      cut into sentences and voiced) but its answers come from a list, so a replay costs nothing and repeats
      exactly;
  (c) `ScriptedBackend(respond=fn)`: `fn(kind, clock, system, content)` returns the answer (or an awaitable of it),
      which is where a live model plugs in: build the request from `content` and call the model. Note the
      simulated clock stands still while an answer is awaited, so a look's lateness isn't measured; and a
      backend that sleeps (retries with back-off) would wait on the frozen clock, so a live hook should call its
      model without asyncio.sleep.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from ..advice import Advice, AdviceBus
from ..coach.backends import Backend
from ..config import Config
from ..knowledge import build, roles
from ..knowledge import mode as game_mode
from ..rules import RuleEngine
from ..rules.economy import spendable
from ..sim import ScriptedMatch
from ..state import IN_PROGRESS, POST_GAME, PRE_GAME, GameStateStore, Snapshot
from .model import Context, Line, Match

TRAVEL_BOOTS = ("item_travel_boots", "item_travel_boots_2")
SIM_START = 1000.0  # where the simulated clock starts (anything positive)


class SimClock:
    """While active, `time.monotonic()` and `time.perf_counter()` return `now`, which the replay moves forward
    with the recording. (Every part of Doty calls them as `time.x()`, so patching the module is enough.)"""

    def __init__(self, start: float = SIM_START) -> None:
        self.now = start
        self._saved: tuple | None = None

    def __enter__(self) -> "SimClock":
        self._saved = (time.monotonic, time.perf_counter)
        time.monotonic = time.perf_counter = lambda: self.now  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._saved is not None:
            time.monotonic, time.perf_counter = self._saved  # type: ignore[assignment]
            self._saved = None


# inputs -------------------------------------------------------------------------------------------------------------
def read_recording(path: Path) -> Iterator[tuple[float, dict]]:
    """(wall time, payload) from a capture folder (its gsi.jsonl) or a gsi.record_dir recording: both write one
    {"t": unix time, "payload": {...}} per line. A line that doesn't parse (a file cut off mid-write) is skipped."""
    path = Path(path)
    if path.is_dir():
        path = path / "gsi.jsonl"
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
                yield float(entry.get("t") or 0.0), entry["payload"]


def scripted_payloads(end_clock: int = 26 * 60 + 30, dt: float = 1.0,
                      match_id: str = "doty-sim-1") -> Iterator[tuple[float, dict]]:
    """The scripted match (sim.ScriptedMatch), one payload every `dt` game seconds; time = game clock."""
    match = ScriptedMatch(match_id=match_id, end_clock=end_clock)
    while match.s.clock < end_clock + 2:
        match.step(dt)
        yield match.s.clock + SIM_START, match.payload("bench")


# the coach's answers ------------------------------------------------------------------------------------------------
class ScriptedBackend(Backend):
    """An AI backend that answers from a list instead of a model. Each answer is {"kind", "clock", "reply"}:
    a look of that kind (watch, now, death, start, question...; "*" for any) takes the first unused answer whose
    clock is at or before the game clock and no more than `window_s` earlier. No answer: an empty reply (the
    coach then says nothing). `respond` replaces the list with a function (see the module docstring)."""

    label = "bench"
    model = "scripted"

    def __init__(self, answers: Iterable[dict] = (), respond: Callable | None = None, window_s: int = 90) -> None:
        self.answers = sorted((dict(a) for a in answers), key=lambda a: int(a.get("clock") or 0))
        self.used: set[int] = set()
        self.respond = respond
        self.window_s = window_s
        self.store: GameStateStore | None = None  # the replay sets it: answers are picked by the game clock
        self.usage: dict = {}
        self.requests: list[tuple[str, int]] = []  # (kind, clock) of every look the coach made

    async def check(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def stream(self, system, content, max_tokens, kind):
        snap = self.store.current if self.store is not None else None
        clock = snap.clock if snap is not None else 0
        self.requests.append((kind, clock))
        if self.respond is not None:
            text = self.respond(kind, clock, system, content)
            if asyncio.iscoroutine(text):
                text = await text
        else:
            text = self._pick(kind, clock)
        # In pieces, sentence by sentence, the way a model streams: the coach speaks each finished sentence.
        for piece in re.split(r"(?<=[.!?\n])", text or ""):
            if piece:
                yield piece

    def _pick(self, kind: str, clock: int) -> str:
        for i, a in enumerate(self.answers):
            at = int(a.get("clock") or 0)
            if at > clock:
                break
            if i in self.used or a.get("kind", "*") not in ("*", kind) or clock - at > self.window_s:
                continue
            self.used.add(i)
            return str(a.get("reply") or "")
        return ""


def load_answers(path: Path) -> list[dict]:
    """Answers for ScriptedBackend from a .json list or a .jsonl file of {"kind", "clock", "reply"}."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else data.get("answers", [])


# the run ----------------------------------------------------------------------------------------------------------
def context_of(snap: Snapshot | None, store: GameStateStore, kb=None, heroes: Iterable[str] | None = None) -> Context:
    """What the bench knows about the moment `snap` is the latest update."""
    if snap is None:
        return Context(clock=0)
    ctx = Context(clock=snap.clock, hero=snap.hero_name, snap=snap)
    if not snap.has_hero:
        return ctx
    ctx.alive = snap.alive
    ctx.fighting = bool(snap.alive and store.in_fight())
    if snap.has_items_section:
        ctx.owned = frozenset(snap.all_item_names())
        ctx.items = tuple(snap.item_list())
        ctx.has_tp = snap.teleport is not None or any(i.name in TRAVEL_BOOTS for i in snap.inventory)
    ctx.gold = spendable(snap, game_mode.of(store.memory))
    if kb is not None:
        ctx.skipped = frozenset(kb.skipped)
        if getattr(kb, "lineup_complete", False):
            ctx.enemies = tuple(kb.enemies)
    if heroes:
        ctx.heroes = frozenset(heroes)
    else:
        tracker = store.memory.get("lineup_votes")
        if tracker is not None and getattr(tracker, "complete", False):
            ctx.heroes = frozenset(tracker.confirmed("radiant")) | frozenset(tracker.confirmed("dire"))
    return ctx


class Replay:
    """Feeds payloads through Doty's parts and records every finished line shown, with the game at that moment.

    `rules=False` runs everything but the rules (to give log lines their game state, see `timeline`).
    `heroes`: the npc names in the match, when known from elsewhere (the game data doesn't list them)."""

    def __init__(self, kb=None, cfg: Config | None = None, backend: Backend | None = None, label: str = "replay",
                 rules: bool = True, heroes: Iterable[str] | None = None, level: str = "some") -> None:
        self.cfg = cfg or Config()
        self.kb = kb
        self.label = label
        self.heroes = list(heroes) if heroes else None
        self.store = GameStateStore()
        self.bus = AdviceBus()
        self.engine = RuleEngine(self.store, self.bus, self.cfg.rules, kb, rules=None if rules else [])
        self.engine.level_of = lambda: level
        self.engine.suggest = self.cfg.suggest
        self.engine.prefs = self.cfg.coach  # (as the app: "When I go my own way", never-suggest, hero builds)
        for rule in self.engine.rules:
            rule.evaluate = _tagged(rule.evaluate, rule.id)  # (which rule said each line)
        self.coach = None
        if backend is not None:
            from ..coach import Coach

            self.coach = Coach(self.cfg.coach, self.store, self.bus, kb)
            self.coach.backend, self.coach.enabled, self.coach.status = backend, True, "ready"
            self.coach.suggest = self.cfg.suggest
            if isinstance(backend, ScriptedBackend):
                backend.store = self.store
            self.bus.subscribe(self.coach.note_alert)
        self.bus.subscribe(self._record)
        self._matches: dict[int, Match] = {}
        self._seen: set[int] = set()
        self._ctx: tuple[int, Context] | None = None  # (id of the snapshot, its context): one per update
        # (wall time, match generation, context) at each new game second: what log lines are judged against.
        self.timeline: list[tuple[float, int, Context]] = []
        self._second: tuple | None = None
        self._t0: float | None = None  # the recording's first time stamp
        self._t = 0.0  # the simulated clock's latest reading

    # running --------------------------------------------------------------------------------------------------
    def run(self, payloads: Iterable[tuple[float, dict]], quiet: bool = True) -> list[Match]:
        """Replay everything and return the matches seen. `quiet`: Doty's own console notes ("[doty] coach
        watch via ...", "dropped a live tip ...") are kept off the screen."""
        sink = contextlib.redirect_stdout(io.StringIO()) if quiet else contextlib.nullcontext()
        with sink, SimClock() as clock:
            if self.coach is None:
                for t, payload in payloads:
                    clock.now = self._sim_time(t)
                    self._feed(payload, t)
            else:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._run_async(payloads, clock))
                    for task in asyncio.all_tasks(loop):  # (the coach's warm-up pings)
                        task.cancel()
                    loop.run_until_complete(asyncio.sleep(0))
                finally:
                    loop.close()
        return self.matches()

    def _sim_time(self, t: float) -> float:
        # The recording's own spacing between updates; a clock that never goes back, so cooldowns hold.
        if self._t0 is None:
            self._t0 = t
        self._t = max(self._t, SIM_START + (t - self._t0))
        return self._t

    async def _run_async(self, payloads, clock: SimClock) -> None:
        for t, payload in payloads:
            clock.now = self._sim_time(t)
            self._feed(payload, t)
            await self._drain()

    async def _drain(self) -> None:
        """Let the look the coach just started finish before the next update (an answer is instant here)."""
        for _ in range(4):
            inflight = self.coach._inflight
            if inflight is None or inflight[2].done():
                break
            try:
                await inflight[2]
            except (asyncio.CancelledError, Exception):
                pass
        await asyncio.sleep(0)

    def _feed(self, payload: dict, wall_t: float) -> None:
        store, kb = self.store, self.kb
        snap = store.ingest(payload, received_t=time.perf_counter())
        mode = game_mode.observe(snap, store, self.cfg.rules.game_mode, None)
        if kb is not None:
            kb.mode = mode
        roles.observe(snap, store, kb)
        if kb is not None:
            for item in build.observe(snap, store, kb):
                note = build.note(kb, item)
                note.bench_rule = "build"
                self.bus.publish(note)
        self.engine.on_snapshot(snap)
        if self.coach is not None:
            self.coach.on_snapshot(snap)
        if snap.has_hero and snap.game_state in (PRE_GAME, IN_PROGRESS, POST_GAME):
            m = self._match()
            if not m.lines and m.start_clock == m.end_clock == 0:
                m.start_clock = m.end_clock = snap.clock
            m.start_clock, m.end_clock = min(m.start_clock, snap.clock), max(m.end_clock, snap.clock)
            second = (store.generation, snap.clock)
            if second != self._second:
                self._second = second
                self.timeline.append((wall_t, store.generation, self._context(snap)))

    # recording --------------------------------------------------------------------------------------------------
    def _match(self) -> Match:
        gen = self.store.generation
        m = self._matches.get(gen)
        if m is None:
            m = self._matches[gen] = Match(label=f"{self.label}#{len(self._matches) + 1}", kind="replay", kb=self.kb)
            if self.coach is None:
                m.notes.append("rules only: the AI coach wasn't replayed")
            if not self.heroes:
                m.notes.append("lineup unknown: heroes not in the match can't be checked")
        return m

    def _context(self, snap: Snapshot | None) -> Context:
        key = id(snap)
        if self._ctx is None or self._ctx[0] != key:
            self._ctx = (key, context_of(snap, self.store, self.kb, self.heroes))
        return self._ctx[1]

    def _record(self, adv: Advice) -> None:
        if not adv.final or not adv.display or adv.id in self._seen:
            return
        text = adv.text.strip()
        if not text or text == "…":
            return
        self._seen.add(adv.id)
        snap = self.store.current
        voiced = self.cfg.voice.min_priority
        spoken = (adv.source == "coach" and adv.category == "coach" and adv.priority <= 2) or (
            adv.speak and adv.priority <= voiced)
        rule = getattr(adv, "bench_rule", "") or (f"coach:{adv.category}" if adv.source == "coach" else "")
        m = self._match()
        m.lines.append(Line(clock=snap.clock if snap else 0, text=text, source=adv.source, rule=rule,
                            priority=int(adv.priority), category=adv.category, spoken=bool(spoken),
                            ctx=self._context(snap), index=len(m.lines)))

    def matches(self) -> list[Match]:
        return [m for m in self._matches.values() if m.lines or m.end_clock > m.start_clock]


def _tagged(evaluate, rule_id: str):
    def run(ctx):
        for adv in evaluate(ctx):
            adv.bench_rule = rule_id
            yield adv
    return run


def attach_states(lines: list[Line], timeline: list[tuple[float, int, Context]], max_gap_s: float = 5.0) -> int:
    """Give log lines (what was really said) the game state recorded at that moment: the latest update at or
    before each line's wall time, if one came within `max_gap_s`. Returns how many lines got a state."""
    times = [t for t, _gen, _ctx in timeline]
    found = 0
    for line in lines:
        if line.t is None:
            continue
        i = bisect.bisect_right(times, line.t) - 1
        if i >= 0 and line.t - times[i] <= max_gap_s:
            line.ctx = timeline[i][2]
            found += 1
    return found
