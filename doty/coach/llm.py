"""The AI coach: the slower, smarter tier behind the rule engine (Claude by default; see backends.py).

Latency tricks (see docs/RESEARCH.md for where each came from):
  * Speculative answers: a background "what now?" is refreshed every
    `speculative_interval_s` of game time, so the ask hotkey answers instantly
    from cache while a fresh answer streams in behind it.
  * Sentence streaming: each finished sentence goes to TTS immediately instead
    of waiting for the whole reply.
  * One request in flight: a new trigger cancels a stale background request.
  * Warm connection: a free models.retrieve() ping keeps TLS + the pool hot.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .. import marks as marklib
from ..advice import Advice, AdviceBus, Priority
from ..config import CoachConfig
from .. import triggers as triggerlib
from ..knowledge import mode as game_mode
from ..lineup import Lineup, other
from ..rules.economy import spendable
from ..state import GameStateStore, Snapshot
from . import prompts
from .backends import Backend, BackendError, ClaudeBackend, claude_request, make_backend

if TYPE_CHECKING:
    from pathlib import Path

    from ..knowledge.constants import Knowledge

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A tip that says there's a skill point or talent to spend ("Level Bulldoze now, you have an unspent point").
SKILL_CLAIM = re.compile(
    r"\b(unspent|unused|spare|free|unlearned|waiting|leftover)\s+(skill\s+|ability\s+|talent\s+)?(points?|talents?)\b"
    r"|\bskill\s+points?\b|\bability\s+points?\b|\bunlearned\b|\blevel\s+up\s+(a|an|your)\s+(skill|ability)\b"
    r"|\b(pick|take|learn|choose|grab)\s+(the\s+|your\s+|a\s+)?(level[\s-]+\d+\s+)?talent\b"
    r"|\b(spend|use)\s+(your|the|that)\s+(\w+\s+)?point\b"
    r"|\bpoints?\s+(is\s+|are\s+)?(unspent|unused|left|waiting|free)\b", re.IGNORECASE)
BUILD_LINE = re.compile(r"^\s*BUILD:\s*(.+)$", re.MULTILINE)
LINEUP_LINE = re.compile(r"^\s*LINEUP:\s*radiant\s*=\s*(.+?)\s*\|\s*dire\s*=\s*(.+?)\s*$",
                         re.MULTILINE | re.IGNORECASE)
DRAFT_LINE = re.compile(r"^\s*DRAFT:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
TAG_LINE = re.compile(r"^\s*(BUILD|LINEUP|DRAW|DRAFT|TRIGGER):.*$", re.MULTILINE | re.IGNORECASE)
TAGS = ("BUILD:", "LINEUP:", "DRAW:", "DRAFT:", "TRIGGER:")
WARM_EVERY_S = 45.0
FRESH_FOR_GAME_S = 30  # a cached answer older than this (game time) is stale
MIN_SPEC_WALL_S = 8.0  # floor on background looks in real time (sim speed-ups, reconnects)
MIN_DRAFT_WALL_S = 20.0  # draft looks: hero selection is short; a few looks are plenty
# Live tips are dropped rather than shown late: the moment they were about has usually passed.
WATCH_STALE_S = 9.0
# Drawings sit on the screenshot they came from. Once the camera has moved (the hero walked off) or a
# few seconds have passed, a circle on the screen points at the wrong thing, so it's dropped.
SCREEN_MARKS_MAX_S = 4.5
MINIMAP_MARKS_MAX_S = 12.0
MOVED_FAR = 700  # world units (the map is about 16000 across): the camera has moved on
WATCH_WIDTH = 1024  # live looks: a smaller screenshot than asks and plans (1280), so answers come faster
# Live tips are one short sentence: a 35-word tip takes ten seconds to say, and the part to act on comes last.
# 14 words is about five seconds of speech.
WATCH_WORDS = 14
REPEAT_S = 120.0  # a live tip that says again what was said this recently goes to the feed, not your ears
REPEAT_OVERLAP = 0.6  # share of the shorter line's words the two lines have in common
# Wait this long into a new match for the lineup (read off the top bar) before the opening look, so the
# laning plan knows who you're up against.
START_LOOK_BY = -30
WATCH_STALE_SPEECH_S = 6.0  # a live tip that can't start playing within this many seconds is dropped
WATCH_MAX_TOKENS = 600
# A look this many seconds before you respawn, so its tip (about 1.5 s to think up) is ready as you're back,
# instead of starting when you respawn.
PRE_RESPAWN_S = 3
# A calm moment (respawned, a fight just ended, level 6...) lasts one update; if a look can't start right then
# (one just ran, or the game is paused), it's kept this long, so the look still comes.
MOMENT_KEEP_S = 10.0
# A routine look (no calm moment) is skipped when nothing that matters has changed since a look that had nothing
# to say: the same answer would come back. At most this many in a row, so a look still comes every ~90 s.
MAX_SKIPS = 2


@dataclass
class Cached:
    text: str
    clock: int
    alive: bool
    created: float


class Coach:
    def __init__(self, cfg: CoachConfig, store: GameStateStore, bus: AdviceBus,
                 kb: "Knowledge | None" = None,
                 screenshot: Callable[[], tuple[str, float]] | None = None, stats=None) -> None:
        self.cfg = cfg
        self.stats = stats  # knowledge.stats.Stats: public hero statistics for draft looks
        self.store = store
        self.bus = bus
        self.kb = kb
        self.screenshot = screenshot
        self.recent_alerts: deque[str] = deque(maxlen=10)
        # Last few spoken questions and answers, so "what about the other one?" works.
        self.history: deque[tuple[str, str]] = deque(maxlen=4)
        # What live coaching said out loud recently, so it doesn't repeat itself.
        self.said: deque[str] = deque(maxlen=4)
        self.cached: Cached | None = None
        self.enabled = cfg.enabled
        self.status = "off"
        self.last_ttft_ms: float | None = None
        self.backend: Backend | None = None
        self._inflight: tuple[str, bool, asyncio.Task] | None = None  # (kind, visible, task)
        self._last_spec_clock: int | None = None
        self._last_spec_wall = 0.0
        self._last_warm = 0.0
        self._last_draft_wall = 0.0
        self._started_match: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Instant calls the coach prepared (TRIGGER lines), fired by Doty itself the moment they come true.
        self.triggers = triggerlib.TriggerBook()
        self._trigger_match = ""  # _match_key of the match all the per-match state belongs to
        self._was_fighting = False
        # What the coach saw for its last few lines (advice id -> state, screenshots, reply), for "Wrong tip?".
        self.evidence: dict[int, dict] = {}
        self.signals: Callable[[], dict[str, float]] | None = None  # screen readings (vision), when available
        # Everything said out loud lately (coach, prepared calls, rule alerts): (monotonic time, its words).
        self._heard: deque[tuple[float, frozenset[str]]] = deque(maxlen=24)
        self.on_armed: Callable[[list[str]], None] | None = None  # prepared calls' lines, to voice ahead of time
        self._dead_seen: tuple[float | None, float | None] = (None, None)  # (enemies, allies) dead, last seen
        self._pre_respawn_done = False  # this death's look before respawning has been started
        self.spoken_max = Priority.NORMAL  # the voice says alerts up to this priority (VoiceConfig.min_priority)
        self.language = "en"  # what the coach writes and says in (AppConfig.language)
        # What the AI cost this match: looks, tokens and dollars (Claude's prices; other providers: tokens only).
        self.spend: dict[str, float] = {"looks": 0, "in": 0, "out": 0, "usd": 0.0, "skipped": 0}
        self._quiet_sig: tuple | None = None  # the situation when the last look had nothing to say
        self._skips = 0
        self._moment: tuple[str, float] | None = None  # a calm moment still waiting for its look (until when)

    # lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self.enabled:
            return
        try:
            self.backend = make_backend(self.cfg)
        except BackendError as exc:
            self._disable(str(exc))
            return
        self.status = "ready"
        self.backend.on_usage = self._count
        # Awaited once so a missing/bad key shows up at startup, not on the first ask.
        await self._warm()

    async def close(self) -> None:
        if self.backend:
            await self.backend.aclose()

    # inputs ------------------------------------------------------------------
    def note_alert(self, adv: Advice) -> None:
        if adv.source == "rules" and adv.final:
            self.recent_alerts.append(adv.text)
            if adv.speak and adv.display and adv.priority <= self.spoken_max:  # (shown-only isn't heard)
                self._heard.append((time.monotonic(), _content_words(adv.text + " " + adv.spoken)))

    def _repeats(self, text: str) -> bool:
        """The line says again what was said out loud in the last REPEAT_S ("Buy a TP scroll" for the
        twentieth time): it's noted in the feed instead of spoken."""
        words = _content_words(text)
        if len(words) < 2:
            return False
        now = time.monotonic()
        for when, heard in self._heard:
            # Measured against the new line: a short alert ("Power rune in 15") shares two words with many
            # new tips ("Take the power rune, then gank bottom with Lion") that say much more.
            common = len(words & heard)
            if now - when <= REPEAT_S and common >= 2 and common >= REPEAT_OVERLAP * len(words):
                return True
        return False

    def on_snapshot(self, snap: Snapshot) -> None:
        """Called on the event loop for every GSI update; schedules coach work."""
        if snap.custom_game:  # (an arcade game: Dota's own coaching doesn't apply)
            return
        self._same_match(snap)  # (first, so a new game's draft doesn't carry the last game's talk)
        if snap.has_hero and snap.in_game and not snap.in_draft:
            # Before the on/off check: prepared calls need no AI, so they keep firing if the coach goes off.
            for adv in self.triggers.check(snap, self.store, self.signals() if self.signals else None,
                                           self._afford(snap)):
                if self._already_have(adv.text, snap):
                    continue  # armed minutes ago, and since then the player bought it (or other boots)
                self.said.append(adv.text)
                self._heard.append((time.monotonic(), _content_words(adv.text)))
                self.bus.publish(adv)
        if not self.enabled:
            return
        if snap.in_draft:
            self._maybe_draft_look()
            return
        if not snap.has_hero or not snap.in_game:
            return
        prev = self.store.previous
        if prev is not None and not (prev.has_hero and prev.in_game):
            prev = None  # (back from a disconnect: a menu update isn't "before" — it reads as level 0, no items)
        asking = self._inflight is not None and self._inflight[1] and not self._inflight[2].done()
        if (self._started_match != self._match_key(snap) and snap.clock < 120 and not asking
                and (snap.clock >= START_LOOK_BY or self.store.memory.get("lineup_complete"))):
            # (never while you're reading or hearing an answer: it would cut that answer off)
            self._started_match = self._match_key(snap)
            self._launch("start", visible=True)
            self._prewarm_watch(snap)
            return
        if self.store.just_died:  # (a real death: Aegis and Reincarnation don't start a death look)
            self._launch("death", visible=True)
            return
        if snap.alive:
            self._pre_respawn_done = False
        elif self._pre_respawn_look(snap):
            return
        if time.monotonic() - self._last_warm > WARM_EVERY_S:
            asyncio.create_task(self._warm())
        fighting = self.store.in_fight()
        moment = self._calm_moment(snap, prev, fighting)
        now = time.monotonic()
        if moment:
            self._moment = (moment, now + MOMENT_KEEP_S)
        elif self._moment is not None and (now > self._moment[1] or fighting or not snap.alive):
            self._moment = None  # (over: a new fight or a death is a different moment)
        moment = self._moment[0] if self._moment is not None else ""
        if (self.cfg.speculative_interval_s > 0 and not snap.paused and snap.clock > 0
                and not fighting  # mid-fight, a tip that takes seconds to think up lands late
                and now - self._last_spec_wall >= MIN_SPEC_WALL_S
                and (moment or self._last_spec_clock is None
                     or snap.clock - self._last_spec_clock >= self.cfg.speculative_interval_s)):
            # Live coaching: a background look at the screen + state that may speak up
            # (see speak_up). Either way its answer is cached, so Ask stays instant. Besides the
            # regular rhythm, it looks at calm moments when advice is easiest to act on.
            if not moment and self._unchanged(snap):
                self._last_spec_clock = snap.clock  # (the next routine look comes a rhythm later)
                self._skips += 1
                self.spend["skipped"] += 1
            elif self._launch("watch" if snap.alive else "now", visible=False, moment=moment):
                self._skips = 0
                self._last_spec_clock = snap.clock
                self._last_spec_wall = now
                self._moment = None
        self._was_fighting = fighting

    def _count(self, model: str, usage: dict) -> None:
        """One request's tokens and price, added to this match's tally (shown in the app)."""
        from .backends import cost_usd

        self.spend["looks"] += 1 if usage.get("out") else 0
        self.spend["in"] += usage.get("in", 0) + usage.get("cached", 0) + usage.get("written", 0)
        self.spend["out"] += usage.get("out", 0)
        usd = cost_usd(model, usage)
        if usd is not None:
            self.spend["usd"] += usd

    @staticmethod
    def situation(snap: Snapshot, signals: dict | None = None) -> tuple:
        """What a routine look's advice depends on, coarsely: alive, level, items, gold (per 1000), HP (quarters),
        where on the map (a 3x3 grid), who's dead. The same situation gets the same answer."""
        pos = snap.position
        zone = (int((pos[0] + 8200) // 5500), int((pos[1] + 8200) // 5500)) if pos is not None else None
        sig = signals or {}
        return (snap.alive, snap.level, frozenset(i.name for i in snap.inventory), snap.gold // 1000,
                round(snap.hp_pct * 4), zone, sig.get("enemies_dead"), sig.get("allies_dead"))

    def _unchanged(self, snap: Snapshot) -> bool:
        """Skip this routine look: the last one had nothing to say and nothing that matters has changed."""
        if self._quiet_sig is None or self._skips >= MAX_SKIPS:
            return False
        return self.situation(snap, self.signals() if self.signals else None) == self._quiet_sig

    def _prewarm_watch(self, snap: Snapshot) -> None:
        """The match's first live look would write the prompt cache (the system prompt and this match's brief)
        from cold, which slows its answer. A zero-token request now, while the opening look runs, writes it
        ahead. Claude only; it costs one cache write on the fast model (a fraction of a cent)."""
        if not isinstance(self.backend, ClaudeBackend) or self.cfg.speculative_interval_s <= 0:
            return
        brief = prompts.match_brief(self.store.memory.get("lineup"), snap.team, snap.hero_name, self.kb,
                                    self.store.memory.get("lineup_complete", False),
                                    timing=self._plan_brief())
        content = ([{"type": "text", "text": brief, "cache_control": {"type": "ephemeral"}}] if brief else [])
        content.append({"type": "text", "text": "warmup"})
        asyncio.create_task(self.backend.prewarm(prompts.SYSTEM_PROMPT, content, "watch"))

    def _pre_respawn_look(self, snap: Snapshot) -> bool:
        """While dead: start a live look a few seconds before respawning, so the tip for when you're back
        lands as you respawn rather than a couple of seconds after. True if one was started."""
        left = int(snap.hero("respawn_seconds", 0) or 0)
        if (self._pre_respawn_done or not 0 < left <= PRE_RESPAWN_S or snap.paused
                or self.cfg.speculative_interval_s <= 0 or not self.store.memory.get("death_open")):
            return False  # (an Aegis or Reincarnation revive isn't a respawn: you're still mid-fight)
        if self._inflight is not None and self._inflight[1] and not self._inflight[2].done():
            return False  # the death answer is still coming: never cut it off (tried again next update)
        self._pre_respawn_done = True
        self._last_spec_clock = snap.clock
        self._last_spec_wall = time.monotonic()  # (so the respawn itself doesn't start a second look)
        self._launch("watch", visible=False,
                     moment=f"the player respawns in {left} seconds: say what to do first when they're back")
        return True

    def _calm_moment(self, snap: Snapshot, prev: Snapshot | None, fighting: bool) -> str:
        """A good moment for a look right now ('' if none): just respawned, a fight just ended, level 6,
        a big item just finished. These are when the player can act on a plan."""
        if prev is None or not snap.alive or fighting:
            return ""  # (dead counts aren't updated mid-fight: a pick-off seen in a fight counts once it's over)
        sig = self.signals() if self.signals else {}
        seen_e, seen_a = self._dead_seen
        dead_e, dead_a = sig.get("enemies_dead"), sig.get("allies_dead")
        self._dead_seen = (seen_e if dead_e is None else dead_e, seen_a if dead_a is None else dead_a)
        if dead_e is not None and seen_e is not None and dead_e >= 2 > seen_e:
            return f"{int(dead_e)} enemies are dead right now: a window for an objective"
        if dead_a is not None and seen_a is not None and dead_a >= 2 > seen_a:
            return f"{int(dead_a)} of the player's allies are dead right now"
        if self.store.just_respawned:  # (back from a real death, not an Aegis revive mid-fight)
            return "the player just respawned"
        if not prev.alive:
            return ""
        if self._was_fighting:
            return "a fight just ended and the player survived"
        if prev.level < 6 <= snap.level:
            return "the player just reached level 6"
        if self.kb is not None:
            new = snap.all_item_names() - prev.all_item_names()
            done = [i for i in new if self.kb.item_cost(i) >= 2000]
            if done:
                return f"the player just finished {self.kb.item_name(done[0])}"
        return ""

    def _maybe_draft_look(self) -> None:
        if self.cfg.speculative_interval_s <= 0 or self.backend is None:
            return
        now = time.monotonic()
        if now - self._last_draft_wall >= max(self.cfg.speculative_interval_s, MIN_DRAFT_WALL_S):
            self._last_draft_wall = now
            self._launch("draft", visible=False)

    def look_screen_only(self) -> None:
        """Thread-safe: a background look from screenshots alone (no game-data feed yet)."""
        if self._loop is not None and self.backend is not None:
            self._loop.call_soon_threadsafe(lambda: self._launch("watch", visible=False, screen_only=True))

    def ask(self, kind: str = "now") -> None:
        """Thread-safe entry point for hotkeys."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._ask_on_loop, kind)

    def _ask_on_loop(self, kind: str) -> None:
        snap = self.store.current
        if not self.enabled:
            self.bus.publish(Advice(f"Coach is off: {self.status}", Priority.INFO, "coach", source="coach"))
            return
        if snap is not None and snap.in_draft:
            self._launch("draft", visible=True)
            return
        if snap is None or not snap.has_hero:
            if self.cfg.use_screenshot and self.screenshot is not None:
                self._launch(kind, visible=True, screen_only=True)  # judge from the screen alone
            else:
                self.bus.publish(Advice("Waiting for a game — start a match or demo hero.", Priority.INFO,
                                        "coach", source="coach"))
            return
        c = self.cached
        if (kind == "now" and c and c.alive == snap.alive
                and 0 <= snap.clock - c.clock <= FRESH_FOR_GAME_S):
            # Instant answer from the speculative cache; refresh in the background.
            adv = Advice(c.text, Priority.HIGH, "coach", source="coach", trigger_t=time.perf_counter(),
                         speak=False)
            self.bus.publish(adv)
            self._speak(c.text)
            self._launch("now", visible=False)
            return
        self._launch(kind, visible=True)

    def ask_question(self, question: str, screenshot_b64: str | None = None) -> None:
        """Thread-safe: a spoken question from push-to-talk. Works outside matches too."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._question_on_loop, question, screenshot_b64)

    def _question_on_loop(self, question: str, shot: str | None) -> None:
        if not self.enabled:
            self.bus.publish(Advice(f"Coach is off: {self.status}", Priority.INFO, "coach", source="coach",
                                    speak=False))
            return
        self._launch("question", visible=True, question=question, shot=shot)

    # request handling ----------------------------------------------------------
    def _launch(self, kind: str, visible: bool, question: str | None = None, shot=None,
                screen_only: bool = False, moment: str = "") -> bool:
        """Start a look (cancelling a background one still running). False if it didn't start."""
        if self._inflight:
            ikind, ivisible, task = self._inflight
            if not task.done():
                if ivisible and not visible:
                    return False  # never cancel something the player is reading for a background refresh
                task.cancel()
        task = asyncio.create_task(self._run(kind, visible, question, shot, screen_only, moment))
        self._inflight = (kind, visible, task)
        return True

    def _grab(self, kind: str):
        """A screenshot; live looks use a smaller one (fewer image tokens, a faster answer)."""
        if kind == "watch":
            try:
                return self.screenshot(WATCH_WIDTH)
            except TypeError:  # a screenshot function that takes no size
                pass
        return self.screenshot()

    async def _run(self, kind: str, visible: bool, question: str | None = None, shot=None,
                   screen_only: bool = False, moment: str = "") -> None:
        snap = self.store.current
        if snap is not None and snap.has_hero and not self._trigger_match:
            self._same_match(snap)  # (a look before Doty's first game update: it belongs to this match)
        match_key = self._trigger_match  # the match this look belongs to
        draft = kind == "draft"
        in_match = not screen_only and not draft and snap is not None and snap.has_hero
        if self.backend is None or (not in_match and not (screen_only or draft or kind == "question")):
            return
        t0 = time.perf_counter()
        if shot is None and self.cfg.use_screenshot and self.screenshot is not None:
            try:
                shot, _ms = await asyncio.to_thread(self._grab, kind)
            except Exception as exc:
                print(f"[doty] screenshot failed: {exc!r}")
        blind = ""
        if isinstance(shot, dict) and shot.get("unavailable"):
            blind, shot = shot["unavailable"], None  # a black capture: say so rather than send it
            if not in_match and not visible:
                return  # a background look from the screen alone has nothing to look at
        if isinstance(shot, dict) and self.store.memory.get("lineup_complete"):
            shot = {k: v for k, v in shot.items() if k != "topbar"}  # all ten known: skip the portraits
        if in_match:
            summary = prompts.state_summary(snap, self.store, self.kb, list(self.recent_alerts))
        elif draft:
            summary = {"status": "hero selection (draft)", "team": (snap.team if snap else "") or "unknown"}
            picks = self.store.memory.get("draft")
            if picks and self.stats is not None and self.stats.loaded:
                from ..knowledge.draft import draft_summary

                summary.update(await asyncio.to_thread(draft_summary, self.stats, picks, self.cfg.bracket))
        elif screen_only:
            summary = {"status": "no game-data feed (Dota's launch option isn't set yet): judge everything "
                                 "from the screenshots",
                       **prompts.lineup_summary(self.store.memory.get("lineup"), "", 0, self.kb,
                                                self.store.memory.get("lineup_complete", False))}
        else:
            summary = {"status": "not in a match right now (menu, queue, or hero selection)"}
        if self.history:
            summary["recent_questions"] = [{"q": q, "a": a} for q, a in self.history]
        if kind in ("watch", "draft") and self.said:
            summary["coach_said_recently"] = list(self.said)
        if in_match and self.triggers.active:
            summary["armed_calls"] = self.triggers.describe()
        if moment:
            summary["moment"] = moment  # why Doty is looking now: a good time to set a plan
        if blind:
            summary["screenshot"] = blind
        max_words = {"question": max(self.cfg.max_words, 40), "draft": 45,
                     "watch": min(self.cfg.max_words, WATCH_WORDS)}.get(kind, self.cfg.max_words)
        brief = prompts.match_brief(self.store.memory.get("lineup"), snap.team, snap.hero_name, self.kb,
                                    self.store.memory.get("lineup_complete", False),
                                    timing=self._plan_brief()) if in_match else ""
        content = prompts.user_content(kind, summary, max_words, shot, question, self.cfg.level, brief=brief,
                                       language=self.language)

        adv = Advice("…", Priority.HIGH, "coach", source="coach", trigger_t=t0, final=False, speak=False)
        if visible:
            self.bus.publish(adv)
        text = ""
        spoken_upto = 0
        first_token_t = None
        early: Advice | None = None  # a live tip said as soon as its sentence was done (see _tip_ready)
        early_done = False
        # A live reply is a sentence plus a few short lines (~250 tokens): a cap stops a rambling one early.
        # Claude only: other providers' reasoning models spend their budget thinking before the first word.
        max_tokens = {"plan": 4000, "start": 4000}.get(kind, 2000)
        if kind == "watch" and isinstance(self.backend, ClaudeBackend):
            max_tokens = WATCH_MAX_TOKENS
        try:
            async for chunk in self.backend.stream(prompts.SYSTEM_PROMPT, content, max_tokens, kind):
                if first_token_t is None:
                    first_token_t = time.perf_counter()
                    self.last_ttft_ms = (first_token_t - t0) * 1000.0
                text += chunk
                if kind == "watch" and not visible and in_match and not early_done:
                    tip = _tip_ready(text)
                    if tip is not None:
                        # The tip comes first and the prepared calls and drawings after it: say it now
                        # instead of after the whole reply (about a second sooner).
                        early_done = True
                        early = self._early_tip(tip, snap, t0)
                if visible:
                    shown = _strip_tags(text)
                    if kind in ("watch", "draft"):
                        shown = WATCH_TAG.sub("", shown, count=1)  # "SAY:" is for Doty, not for you
                    adv.text, adv.created_t = shown, time.perf_counter()
                    self.bus.publish(adv)
                    # Speak each completed sentence as soon as it exists.
                    parts = SENTENCE_END.split(shown)
                    done = parts[:-1]
                    for sentence in done[spoken_upto:]:
                        self._speak(sentence)
                    spoken_upto = max(spoken_upto, len(done))
        except asyncio.CancelledError:
            raise
        except BackendError as exc:
            if exc.fatal:
                self._disable(str(exc))
                return self._fail(adv, visible, f"Coach is off: {exc}")
            return self._fail(adv, visible, str(exc))
        except Exception as exc:  # anything else (a dropped connection mid-reply): this look ends, coaching goes on
            return self._fail(adv, visible, f"Coach error ({exc.__class__.__name__}); try again")
        if in_match and match_key != self._trigger_match:
            in_match = False  # a new match began while this look ran: its calls and build are last game's
            if not visible:
                return
        game = snap if in_match else None
        self._apply_lineup(text, game)
        self._apply_draft(text)
        drawn = tuple(marklib.parse(text, shot.get("sizes", {}) if isinstance(shot, dict) else {}))
        drawn = self._vet_marks(drawn, game, t0)
        shown = _strip_tags(text).strip()
        if in_match:
            self._apply_build(text, snap)
            self._arm_triggers(text, snap)
        dropped = ""
        if kind in ("watch", "draft"):
            say, shown = parse_watch(shown)
            if kind == "watch" and in_match and (isinstance(early, Advice) or _is_waiting(shown)):
                # (said something: the next look goes ahead; "waiting" isn't a quiet look about the game)
                self._quiet_sig = None
            if isinstance(early, Advice):  # already said; the drawings came after it, so add them to its card
                if drawn:
                    early.marks, early.speak = drawn, False
                    self.bus.publish(early)
                self._keep_evidence(early.id, kind, summary, shot, text)
            elif isinstance(early, str):
                dropped = early
            else:
                dropped = self._vet_tip(kind, shown, game, snap, t0, in_match)
                if dropped:
                    print(f"[doty] dropped a live tip ({dropped}): {shown[:90]}")
                elif shown and not visible and not _is_waiting(shown):  # a visible answer has its own card below
                    # Said lately (by the coach or an alert): the feed has it, your ears don't.
                    repeat = in_match and self._repeats(shown)
                    published = self._publish_watch(shown, say, t0, drawn, repeat=repeat)
                    if kind == "watch" and in_match:
                        self._quiet_sig = None if published.speak else self.situation(
                            snap, self.signals() if self.signals else None)
                    self._keep_evidence(published.id, kind, summary, shot, text)
        if isinstance(early, Advice):
            shown = early.text  # what was said and checked is what Ask replays
        if kind in ("now", "watch") and in_match and shown and not dropped and not _is_waiting(shown):
            self.cached = Cached(shown, snap.clock, snap.alive, time.monotonic())
        if kind == "question" and question:
            self.history.append((question, shown))
        if visible:
            adv.text, adv.final, adv.created_t, adv.marks = shown, True, time.perf_counter(), drawn
            self.bus.publish(adv)
            self._keep_evidence(adv.id, kind, summary, shot, text, question)
            remaining = SENTENCE_END.split(shown)[spoken_upto:]
            if remaining and any(s.strip() for s in remaining):
                self._speak(" ".join(remaining))
            if in_match and shown and not _is_waiting(shown) and self.cfg.speak_up != "never":
                # (the death answer, then the look before respawning, mustn't say the same thing twice)
                self.said.append(shown)
                self._heard.append((time.monotonic(), _content_words(shown)))
        total = (time.perf_counter() - t0) * 1000.0
        ttft = f"{self.last_ttft_ms:.0f}" if self.last_ttft_ms else "?"
        usage = " ".join(f"{k}={v}" for k, v in self.backend.usage.items())
        print(f"[doty] coach {kind}{'' if visible else ' (bg)'} via {self.backend.label}: ttft={ttft}ms "
              f"total={total:.0f}ms {usage}")

    def _request(self, content: list, kind: str) -> dict:
        """The Claude request body (kept for tests and debugging)."""
        return claude_request(self.cfg, prompts.SYSTEM_PROMPT, content, kind)

    def _vet_tip(self, kind: str, text: str, game: Snapshot | None, snap: Snapshot | None, t0: float,
                 in_match: bool) -> str:
        """Why a live tip shouldn't be shown at all ('' if it's fine): too late, or it names heroes who
        aren't in this game or items this hero doesn't buy."""
        phantom = self._phantom_heroes(text) if in_match else set()
        odd = self._odd_items(text, snap) if in_match else set()
        have = self._already_have(text, snap) if in_match else set()
        return self._too_late(kind, game, t0) or (
            f"says to buy what you already have (or a second pair of boots): "
            f"{', '.join(sorted(self.kb.item_name(i) for i in have))}" if have else "") or (
            "says there's a skill point to spend, but there isn't" if in_match and self._false_skill_claim(text, snap)
            else "") or (
            f"names heroes not in this game: {', '.join(sorted(self._display(h) for h in phantom))}"
            if phantom else "") or (
            f"suggests items this hero doesn't buy: {', '.join(sorted(self.kb.item_name(i) for i in odd))}"
            if odd else "")

    def _early_tip(self, raw: str, snap: Snapshot, t0: float) -> "Advice | str | None":
        """A live tip whose sentence is done while the rest of the reply still streams: said now if it's
        worth saying. Returns the advice said, why it was dropped, or None to leave it to the end (a
        quiet note, "waiting", or a repeat, which go to the feed when the reply is complete)."""
        say, tip = parse_watch(_strip_tags(raw).strip())
        if not say or not tip or _is_waiting(tip) or self._repeats(tip) or self.cfg.speak_up == "never":
            return None
        dropped = self._vet_tip("watch", tip, snap, snap, t0, True)
        if dropped:
            print(f"[doty] dropped a live tip ({dropped}): {tip[:90]}")
            return dropped
        return self._publish_watch(tip, True, t0)

    def _publish_watch(self, text: str, say: bool, t0: float, drawn: tuple = (), repeat: bool = False) -> Advice:
        """Live coaching: speak up (card + voice) when it matters; otherwise note it quietly
        in the app's feed so you can see Doty watching, without talking over you. A repeat is never said."""
        mode = self.cfg.speak_up
        say = (say or mode == "every") and mode != "never" and not repeat
        if say:
            self.said.append(text)
            self._heard.append((time.monotonic(), _content_words(text)))
            # The tip's age counts: a reply that took long has less time left to be heard before it's stale.
            left = WATCH_STALE_S - (time.perf_counter() - t0)
            adv = Advice(text, Priority.HIGH, "coach", source="coach", trigger_t=t0, marks=drawn,
                         stale_s=min(WATCH_STALE_SPEECH_S, max(0.5, left)))
        else:
            adv = Advice(text, Priority.INFO, "watching", source="coach", trigger_t=t0, speak=False, overlay=False)
        self.bus.publish(adv)
        return adv

    # "Wrong tip?" ---------------------------------------------------------------
    EVIDENCE_KEPT = 12

    def _keep_evidence(self, advice_id: int, kind: str, summary: dict, shot, reply: str,
                       question: str | None = None) -> None:
        snap = self.store.current
        self.evidence[advice_id] = {
            "kind": kind, "question": question, "state": summary, "reply": reply,
            "screenshots": shot if isinstance(shot, dict) else ({"screen": shot} if isinstance(shot, str) else {}),
            "clock": snap.clock if snap else None, "hero": snap.hero_name if snap else "",
            "model": getattr(self.backend, "model", ""), "provider": getattr(self.backend, "label", ""),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for old in sorted(self.evidence)[:-self.EVIDENCE_KEPT]:
            del self.evidence[old]

    def report(self, advice_id: int, text: str, folder) -> "Path | None":
        """Save what the coach saw for one line (its game state, screenshots and full answer) so a wrong
        tip can be looked into. Returns the folder, or None if Doty no longer has that line's details."""
        import base64
        import json
        from pathlib import Path

        ev = self.evidence.get(advice_id)
        if ev is None:
            return None
        clock = ev.get("clock")
        stamp = time.strftime("%Y%m%d-%H%M%S") + (f"-{abs(clock) // 60:02d}m{abs(clock) % 60:02d}s"
                                                  if isinstance(clock, int) else "")
        out = Path(folder) / f"{stamp}-{advice_id}"  # two reports in one second keep apart
        out.mkdir(parents=True, exist_ok=True)
        for name, data in ev["screenshots"].items():
            if isinstance(data, str) and name != "sizes":
                (out / f"{name}.jpg").write_bytes(base64.standard_b64decode(data))
        report = {k: v for k, v in ev.items() if k != "screenshots"}
        report["shown"] = text
        (out / "report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        return out

    def _speak(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self.bus.publish(Advice(sentence, Priority.HIGH, "coach", source="coach", display=False))

    def _fail(self, adv: Advice, visible: bool, msg: str) -> None:
        print(f"[doty] {msg}")
        if visible:
            adv.text, adv.final, adv.priority = msg, True, Priority.INFO
            self.bus.publish(adv)

    # who's in the game ---------------------------------------------------------
    def _hero_keys(self, names) -> list[str]:
        """Hero names as read or said -> npc names (or the plain names when offline with no hero list)."""
        out = []
        for name in (str(n).strip(" .") for n in names):
            npc = self.kb.find_hero(name) if self.kb else None
            if npc:
                out.append(npc)
            elif name and not (self.kb and self.kb.heroes):
                out.append(name)  # offline: no hero list to check names against
        return out

    def _display(self, key: str) -> str:
        return self.kb.hero_name(key) if self.kb and key.startswith("npc_dota_hero_") else key

    def _lineup(self, snap: Snapshot | None = None) -> Lineup:
        """This match's lineup votes. Picks read during hero selection count as the first read."""
        tracker = self.store.memory.get("lineup_votes")
        if tracker is None:
            tracker = self.store.memory["lineup_votes"] = Lineup()
            draft = self.store.memory.get("draft") or {}
            if draft.get("radiant") or draft.get("dire"):
                tracker.add(self._hero_keys(draft.get("radiant", [])), self._hero_keys(draft.get("dire", [])),
                            snap.hero_name if snap else "", snap.team if snap else "")
        return tracker

    def _apply_lineup(self, text: str, snap: Snapshot | None = None) -> None:
        """Count the coach's read of the top bar (LINEUP line) as one vote; publish what's confirmed."""
        m = LINEUP_LINE.search(text)
        if not m:
            return
        tracker = self._lineup(snap)
        tracker.add(self._hero_keys(m.group(1).split(",")), self._hero_keys(m.group(2).split(",")),
                    snap.hero_name if snap else "", snap.team if snap else "")
        self._publish_lineup(tracker, snap)

    def screen_read(self, radiant: list[str], dire: list[str], match_id: str = "") -> None:
        """Thread-safe: Doty's own top-bar reader (vision.py) read these heroes (npc names), steadily.
        It counts as a read like the coach's, and it's there seconds into the match instead of after
        a few AI looks."""
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._screen_read, list(radiant), list(dire), match_id)
            except RuntimeError:
                pass  # the loop closed in between

    def _screen_read(self, radiant: list[str], dire: list[str], match_id: str = "") -> None:
        snap = self.store.current
        if snap is None or not snap.has_hero or (match_id and snap.match_id != match_id):
            return  # a read from the previous match, landing after the new one began
        tracker = self._lineup(snap)
        if tracker.add(radiant, dire, snap.hero_name, snap.team):
            self._publish_lineup(tracker, snap)

    def _publish_lineup(self, tracker: Lineup, snap: Snapshot | None) -> None:
        """What's confirmed goes to the match memory (the coach's state, the app); new enemies' build data
        is fetched, for the match brief and their power-spike times."""
        self.store.memory["lineup"] = {s: [self._display(h) for h in names] for s, names in tracker.as_dict().items()}
        self.store.memory["lineup_complete"] = tracker.complete
        team = (snap.team if snap else "").lower()
        if self.kb is None or team not in ("radiant", "dire") or self._loop is None:
            return
        enemy = other(team)
        for npc in tracker.confirmed(enemy):  # (each once; a fetch that failed is asked again after its back-off)
            hero = self.kb.heroes.get(npc)
            if hero:
                self._loop.create_task(self.kb.ensure_enemy(int(hero["id"])))
        for npc in tracker.confirmed(team) + tracker.confirmed(enemy):  # both teams: for the game plan
            hero = self.kb.heroes.get(npc)
            if hero and int(hero["id"]) not in self.kb.durations:
                self._loop.create_task(self.kb.ensure_durations(int(hero["id"])))

    def _phantom_heroes(self, text: str) -> set[str]:
        """Heroes a tip names that aren't in this game (only judged once all ten are confirmed)."""
        tracker = self.store.memory.get("lineup_votes")
        if self.kb is None or tracker is None or not tracker.complete:
            return set()
        playing = set(tracker.confirmed("radiant")) | set(tracker.confirmed("dire"))
        return {h for h in self.kb.hero_mentions(text) if h not in playing}

    def _arm_triggers(self, text: str, snap: Snapshot) -> None:
        """Arm the coach's prepared calls, minus any that name heroes not in the game or odd items, or
        whose has/no item isn't a real item (a wrong name would make "no ..." true forever)."""
        fresh = [t for t in (self._real_items(t) for t in triggerlib.parse(text, snap.clock))
                 if t is not None and not self._phantom_heroes(t.say) and not self._odd_items(t.say, snap)
                 and not self._already_have(t.say, snap)]
        if fresh:
            self.triggers.arm(fresh)
            if self.on_armed is not None:  # voiced now, in idle time, so the call plays the instant it fires
                self.on_armed([t.say for t in fresh])

    def _real_items(self, t: "triggerlib.Trigger") -> "triggerlib.Trigger | None":
        """A call's item clauses with Dota's real item keys ("battle_fury" -> item_bfury), or None if one
        isn't an item at all. Unchanged when Doty has no item data."""
        if self.kb is None or not self.kb.items:
            return t
        clauses = []
        for c in t.clauses:
            if c.kind in ("has", "no", "ready", "afford") and c.var not in self.kb.items:
                found = self.kb.item_mentions(c.var.removeprefix("item_").replace("_", " ").title())
                if len(found) != 1:
                    print(f"[doty] dropped a prepared call (no item called {c.var}): {t.condition}")
                    return None
                c = dataclasses.replace(c, var=next(iter(found)))
            clauses.append(c)
        return dataclasses.replace(t, clauses=tuple(clauses))

    def _match_key(self, snap: Snapshot) -> str:
        """This match, even when Dota gives bot and local games the same id ("0") or none."""
        # (the store's id, not this update's: a menu update mid-match, after a disconnect, has none)
        return f"{self.store.match_id or snap.match_id or 'local'}#{self.store.generation}"

    def _same_match(self, snap: Snapshot) -> None:
        """Prepared calls, the cached answer, the look rhythm and what was said all belong to one match:
        a new match starts with none of them (else Ask could replay last game's tip, and the 30 s look
        rhythm would wait for the new clock to pass the old one)."""
        if self._match_key(snap) != self._trigger_match:
            first = not self._trigger_match
            self._trigger_match = self._match_key(snap)
            if first:
                return  # (Doty's first game update: there's no earlier match to forget)
            if self.kb is not None:
                self.kb.custom_plan.clear()  # last game's BUILD line isn't this game's plan
            self.triggers.clear()
            self.cached = None
            self._last_spec_clock = None
            self._was_fighting = False
            self.said.clear()
            self._heard.clear()
            self.spend = {"looks": 0, "in": 0, "out": 0, "usd": 0.0, "skipped": 0}
            self._quiet_sig, self._skips = None, 0
            self._dead_seen = (None, None)
            self._pre_respawn_done = False
            self.history.clear()
            self.recent_alerts.clear()

    def _false_skill_claim(self, text: str, snap: Snapshot | None) -> bool:
        """The tip tells the player to spend a skill point or pick a talent they don't have. The AI reads level-up
        arrows off the screenshot and gets it wrong (a real match: "Level 4 with Charge 1 and Bash 2 means a
        point is unspent", every few seconds); Doty's count from the game data settles it."""
        if snap is None or not SKILL_CLAIM.search(text):
            return False
        if self.kb is not None:
            points = self.kb.unspent_points(snap)
            if points is None:  # past level 16: only an open talent tier makes a talent tip possible
                return snap.level > 16 and not self.kb.open_talents(snap)
            return points == 0
        points = snap.unspent_skill_points
        return points == 0

    def _odd_items(self, text: str, snap: Snapshot) -> set[str]:
        """Big items a tip suggests that this hero's players don't buy (and that don't answer an enemy)."""
        if self.kb is None:
            return set()
        allowed = self.kb.allowed_items(snap.hero_id)
        if allowed is None:
            return set()
        owned = snap.all_item_names()
        return {i for i in self.kb.item_mentions(text, snap.hero_name) if i not in allowed and i not in owned}

    def _already_have(self, text: str, snap: Snapshot | None) -> set[str]:
        """Big items a line tells the player to buy that they already have (whole, or built into something
        bigger), or that would be a second pair of boots. A real demo run: "Buy Power Treads now", three
        times, to a player wearing Phase Boots."""
        if self.kb is None or snap is None or not self.kb.items:
            return set()
        owned = snap.all_item_names()
        have = self.kb.owned_with_parts(owned)
        return {i for i in self.kb.buy_mentions(text, snap.hero_name)
                if i in have or self.kb.second_boots(i, owned)}

    def _plan_brief(self) -> str:
        """The game plan for the match brief (which lineup the long game favours), with what Turbo changes
        about it when this is a Turbo game."""
        brief = self.store.memory.get("game_plan_brief", "")
        return brief + game_mode.PLAN_NOTE if brief and game_mode.of(self.store.memory).turbo else brief

    def _afford(self, snap: Snapshot):
        """Answers the prepared calls' "afford <item>": the player's gold covers what's left of the item,
        counting the parts of it they hold. None without item data (then such a call waits)."""
        kb = self.kb
        if kb is None or not kb.items:
            return None
        # (late in the game, buyback gold isn't for items)
        owned, gold = snap.item_list(), spendable(snap, game_mode.of(self.store.memory))

        def can(item: str) -> bool | None:
            if item not in kb.items:
                return None
            left = kb.cost_left(item, owned)
            return 0 < left <= gold
        return can

    # freshness ----------------------------------------------------------------
    def _too_late(self, kind: str, snap: Snapshot | None, t0: float) -> str:
        """Why a live tip is too late to show now, or ''."""
        if snap is None or kind != "watch":
            return ""
        age = time.perf_counter() - t0
        now = self.store.current
        if age > WATCH_STALE_S:
            return f"{age:.0f} s late"
        if now is not None and snap.alive and not now.alive:
            return "you died meanwhile"  # (a look started while dead is about respawning: that's its point)
        if self.store.in_fight():
            return "a fight started"
        return ""

    def _vet_marks(self, marks: tuple, snap: Snapshot | None, t0: float) -> tuple:
        """Drop drawings that would point at the wrong thing by now, and color heroes by team: green for
        allies, red for enemies, neutral when Doty can't be sure (so an ally is never marked as a threat)."""
        if not marks:
            return marks
        age = time.perf_counter() - t0
        now = self.store.current
        moved = _distance(snap.position, now.position) if snap is not None and now is not None else 0.0
        fight = snap is not None and self.store.in_fight()
        tracker = self.store.memory.get("lineup_votes")
        team = (snap.team if snap else "").lower()
        own = snap.hero_name if snap else ""
        out = []
        for m in marks:
            if m.space == "screen" and (age > SCREEN_MARKS_MAX_S or moved > MOVED_FAR or fight):
                continue
            if m.space == "minimap" and (age > MINIMAP_MARKS_MAX_S or fight):
                continue
            heroes = self.kb.hero_mentions(m.label) if self.kb else set()
            if heroes:
                sides = {team if h == own else (tracker.side_of(h) if tracker else None) for h in heroes}
                if tracker is not None and tracker.complete and None in sides:
                    continue  # names a hero who isn't in this game
                if team in ("radiant", "dire") and sides == {team}:
                    m = dataclasses.replace(m, tone="good")
                elif team in ("radiant", "dire") and sides == {other(team)}:
                    m = dataclasses.replace(m, tone="danger")
                else:
                    m = dataclasses.replace(m, tone="info")
            out.append(m)
        return tuple(out)

    def _apply_draft(self, text: str) -> None:
        """Remember the picks and bans the coach read off the hero-selection screen (DRAFT line)."""
        m = DRAFT_LINE.search(text)
        if not m:
            return
        out: dict = {"you": "", "radiant": [], "dire": [], "bans": []}
        for part in m.group(1).split("|"):
            key, _, value = part.partition("=")
            key = key.strip().lower()
            if key == "you":
                out["you"] = value.strip().lower() if value.strip().lower() in ("radiant", "dire") else ""
            elif key in ("radiant", "dire", "bans"):
                names = [n.strip(" .") for n in value.split(",") if n.strip(" .")]
                if self.kb and self.kb.heroes:
                    names = [self.kb.hero_name(npc) for npc in (self.kb.find_hero(n) for n in names) if npc]
                out[key] = names[:5] if key != "bans" else names[:16]
        self.store.memory["draft"] = out

    def _apply_build(self, text: str, snap: Snapshot) -> None:
        m = BUILD_LINE.search(text)
        if not m or self.kb is None:
            return
        items = [w.strip().strip(".") for w in m.group(1).split(",")]
        items = [i if i.startswith("item_") else f"item_{i}" for i in items if i]
        known = [i for i in items if i in self.kb.items] if self.kb.items else items
        allowed = self.kb.allowed_items(snap.hero_id)
        if allowed is not None:  # keep the plan to items this hero's players actually buy
            known = [i for i in known if i in allowed]
        if known:
            self.kb.custom_plan[snap.hero_name] = known

    async def _warm(self) -> None:
        self._last_warm = time.monotonic()
        if self.backend is None:
            return
        try:
            await self.backend.check()
        except BackendError as exc:
            if exc.fatal:
                self._disable(str(exc))
            else:
                print(f"[doty] coach warm-up: {exc}")

    def _disable(self, why: str) -> None:
        if self.enabled:
            print(f"[doty] coach disabled: {why}. Rules, timers and voice keep working.")
        self.enabled = False
        self.status = f"off ({why})"


def _distance(a: tuple[int, int] | None, b: tuple[int, int] | None) -> float:
    if a is None or b is None:
        return 0.0
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _strip_tags(text: str) -> str:
    """Hide the lines meant for Doty (BUILD, LINEUP, DRAW, DRAFT), including one still arriving mid-stream."""
    text = TAG_LINE.sub("", text)
    head, _, tail = text.rpartition("\n")
    if head and tail.strip() and any(tag.startswith(tail.strip().upper()) for tag in TAGS):
        text = head
    return text.rstrip()


WATCH_TAG = re.compile(r"^\s*(SAY|QUIET)\s*:\s*", re.IGNORECASE)
LATER_TAG = re.compile(r"(?<![A-Za-z])(SAY|QUIET):\s*")  # a second, corrected answer ("Wait... SAY: ...")
SELF_CORRECTION = re.compile(r"(?:\n|(?<=[.!?])\s)\s*(?:Wait|Actually|Correction)\b[,:.]?", re.IGNORECASE)


def _is_waiting(text: str) -> bool:
    """The coach's "nothing to see" answer (a menu, a loading screen, Dota tabbed out)."""
    return text.lower().strip(" .") == "waiting"


def parse_watch(text: str) -> tuple[bool, str]:
    """('SAY: Back off, three missing.') -> (True, 'Back off, three missing.'). No tag = quiet.
    When the model corrects itself mid-reply ("SAY: ... Wait, that's wrong. SAY: ..."), the last tagged
    answer counts, and a "Wait, ..." with no new tag is cut off."""
    m = WATCH_TAG.match(text)
    if not m:
        return False, SELF_CORRECTION.split(text, 1)[0].strip()
    tag, rest = m.group(1), text[m.end():]
    later = list(LATER_TAG.finditer(rest))
    if later:
        tag, rest = later[-1].group(1), rest[later[-1].end():]
    return tag.upper() == "SAY", SELF_CORRECTION.split(rest, 1)[0].strip()


NEXT_TAG = re.compile(r"\s*(BUILD|LINEUP|DRAW|DRAFT|TRIGGER):", re.IGNORECASE)


def _tip_ready(text: str) -> str | None:
    """The start of a live reply once its tip is surely complete, else None: the tip's line has ended and
    the next line has begun with one of Doty's tags (TRIGGER, DRAW...). Anything else after it (the tip
    going on, or "Wait, ..." with a corrected answer) leaves the reply to be judged whole at the end. A
    tag alone on the first line ("SAY:" then the tip) doesn't count."""
    head = text.lstrip()
    m = WATCH_TAG.match(head)
    start = m.end() if m else 0
    end = head.find("\n", start)
    while end != -1 and not head[start:end].strip():
        start = end + 1
        end = head.find("\n", start)
    if end == -1:
        return None
    if TAG_LINE.match(head[start:end]):
        return head[:start]  # no tip at all, straight to the extra lines
    if not NEXT_TAG.match(head[end + 1:]):
        return None
    return head[:end]


STOPWORDS = frozenset("""a an the and or but so to of in on at for from with by as is are be been was were it its
this that these those there here you your youre you're yours we our they them their he his she her him i me my
now then than just still also very more most less too can cant can't could should would will wont won't do dont
don't does did done get got go going into out up down off over under again once only not no yes if when while
before after until about all any some each every one two three both other others what who which where why how
right next first last soon keep make take use""".split())


def _content_words(text: str) -> frozenset[str]:
    """The words that carry a line's meaning, lowercased, plurals folded ("runes" -> "rune")."""
    out = set()
    for w in re.findall(r"[a-z']+", text.lower()):
        w = w.strip("'")
        if len(w) < 2 or w in STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return frozenset(out)
