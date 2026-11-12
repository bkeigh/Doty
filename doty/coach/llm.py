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
from ..knowledge import build as buildlib
from ..knowledge import mode as game_mode
from ..lineup import Lineup, other
from ..rules import own_way
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
SKIP_LINE = re.compile(r"^\s*SKIP:\s*(.+)$", re.MULTILINE)
TAG_LINE = re.compile(r"^\s*(BUILD|LINEUP|DRAW|DRAFT|TRIGGER|SKIP):.*$", re.MULTILINE | re.IGNORECASE)
TAGS = ("BUILD:", "LINEUP:", "DRAW:", "DRAFT:", "TRIGGER:", "SKIP:")
# "Not that item", said over push-to-talk: handled at once by Doty itself, with no AI call.
SKIP_ASK = re.compile(
    r"\b(skip|stop\s+(suggesting|recommending|telling\s+me|saying|asking|with)|no\s+more|forget(\s+about)?|"
    r"(do\s+not|don'?t|won'?t|will\s+not|not|never)\s+(want|need|buy|buying|get|getting|going|build|building|"
    r"suggest|recommend|make|making|doing))\b", re.IGNORECASE)
UNSKIP_ASK = re.compile(r"\b(bring\s+back|add\s+back|put\s+back|un-?skip)\b", re.IGNORECASE)
THIS_ITEM = re.compile(r"\bskip\s+(it|that|this)(\s+(item|one))?\s*[.!]?\s*$", re.IGNORECASE)
# Words that may stand between the order and the item ("stop telling me to get Phase Boots").
COMMAND_FILLER = frozenset("the a an any to get buy me more that this my about on for building getting buying "
                           "another your".split())
# A question about an item is for the coach to answer, not an order ("Should I skip Black King Bar?").
ASKING = re.compile(r"\?|^\s*(should|do|does|is|are|can|could|would|what|why|how|when|which|will|if)\b", re.IGNORECASE)
COMMAND_MAX_WORDS = 14
# A tip that says the gold is there ("Buy Ogre Axe now", "you can afford it"): checked against the gold.
NOW_CLAIM = re.compile(r"\b(now|right\s+away|immediately|have\s+the\s+gold|can\s+afford|affordable|covers?\s+it)\b",
                       re.IGNORECASE)
# The coach may tell you to buy one item this many times out loud; after that, with no part of it bought, a
# live tip that pushes it again goes to the feed instead. (A real match: Black King Bar, twenty-five times.)
# That is "When I go my own way" on "remind"; otherwise the first time is advice and any more is an argument,
# which "once" allows once, with a reason, and "follow" never does (rules/own_way.py).
MAX_ITEM_PUSHES = 3
# "Never suggest Black King Bar again", "don't ever recommend Radiance": an item the player never wants, said out
# loud. It's added to their settings (CoachConfig.never_suggest), so it outlasts the match.
NEVER_ASK = re.compile(
    r"\b(never\s+(again\s+)?(suggest|recommend|mention|tell\s+me\s+to\s+(buy|get))|"
    r"(don'?t|do\s+not)\s+ever\s+(suggest|recommend|mention))\b", re.IGNORECASE)
# "...again", "remember that", "for good": what makes a spoken skip outlast the match.
FOR_GOOD = re.compile(r"\b(ever\s+again|again|remember\s+(that|this|it)|for\s+good|from\s+now\s+on|in\s+any\s+game|"
                      r"every\s+game)\b", re.IGNORECASE)
# A claim that the gold covers the whole item now (not only a part of it).
AFFORD_CLAIM = re.compile(r"\b(have\s+the\s+gold|can\s+afford|affordable\s+now|enough\s+gold|covers?\s+it)\b",
                          re.IGNORECASE)
# A claim that the player already holds an item or spent their gold on it (checked against the inventory).
OWNED_CLAIM = re.compile(r"\b(?:you(?:'ve|\s+have|\s+already\s+have|\s+already\s+bought|\s+bought|\s+got|\s+own|"
                         r"\s+finished|\s+built|'re\s+holding|\s+are\s+holding)|your\s+gold\s+(?:is|was|went)\s+"
                         r"(?:spent|going|into)\s+(?:on|to|into)?|now\s+that\s+you\s+have|with\s+your)\b",
                         re.IGNORECASE)
# The model talking about the player in the third person, or to itself ("Note: the player has gold..."): never for
# the player's ears. (A real run had it read out after a blank line.)
META = re.compile(r"(?:^|\n|(?<=[.!?])\s)\s*(?:note|n\.b\.|aside|reasoning|thinking)\s*:|\bthe\s+(?:player|user)\b",
                  re.IGNORECASE)
# Telling the player which skill to take, or to skip one (checked against the order Doty itself says: _wrong_skill).
LEVEL_SKILL = r"\b(?:level|level\s+up|skill|take|learn|max|put\s+(?:a\s+|your\s+)?points?\s+(?:in|into))"
SKIP_SKILL = r"\b(?:skip|delay|hold\s+off\s+on|don'?t\s+(?:level|take|skill|learn))"
# "Why?", "why that?", "and why is that?" said out loud: the last tip explained (Coach._why_on_loop).
WHY_ASK = re.compile(r"^\s*(and\s+|but\s+|so\s+)?why(\s+(that|this|that\s+one|is\s+that|so|did\s+you\s+say\s+"
                     r"that|should\s+i))?\s*[?.!]?\s*$", re.IGNORECASE)
REMEMBER_THAT = re.compile(r"^\s*(please\s+)?(remember|keep)\s+(that|this|it)(\s+for\s+good)?\s*[.!]?\s*$",
                           re.IGNORECASE)
# ...unless the line is a call to get out, which is never held back for naming an item.
RETREAT = re.compile(r"\b(back\s+off|walk\s+(back|away)|get\s+out|retreat|leave\b|run\b|teleport|tp\s+out|"
                     r"step\s+back|pull\s+back|head\s+back)", re.IGNORECASE)
# "Buy a TP scroll", said to a player who has one. (A real match: thirty-two times over two games.)
TP_BUY = re.compile(r"\b(buy|get|grab|purchase|pick\s+up|carry)\b[^.;:!?]{0,40}?\b(tp|town\s+portal)\b", re.IGNORECASE)
# One of Doty's tags written on the same line as the answer ("...don't walk the river alone. TRIGGER: hp < 30
# => ..."): a real match had it read out loud. Each tag gets its own line before anything is parsed or shown.
INLINE_TAG = re.compile(r"[ \t]+(BUILD|LINEUP|DRAW|DRAFT|TRIGGER|SKIP):")
TRIGGER_REPEAT_S = 20.0  # a prepared call that says what was just heard this recently isn't said again
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
# ...but the speech gate (speech.py) may hold a live tip for a quiet slot this long, while it's still right
# (Coach._still_right, checked again just before it's said).
TIP_HOLD_S = 25.0
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
        self.suggest = None  # config.SuggestConfig: the subjects Doty brings up by itself (the app sets it)
        self._pushes: dict[str, list[int]] = {}  # item -> [times the coach said "buy it", gold left on it then]
        # "Never suggest X again" changed the player's settings: (items, added) -> None, so the app saves them.
        self.on_remember: Callable[[list[str], bool], None] | None = None
        self._last_skipped: list[str] = []  # what "skip ..." last took out this match (for "remember that")
        self.gate = None  # speech.SpeechGate: what was last said out loud (for "why?"); the app sets it
        self._held_tips: dict[int, str] = {}  # live tips the gate is holding for a slot (advice id -> text)
        self._held_cards: dict[int, Advice] = {}  # answers whose sentences the gate is holding (card id -> card)
        self._feed_notes: deque[tuple[float, frozenset[str]]] = deque(maxlen=12)  # quiet notes shown lately
        self._feed_items: dict[str, list[int]] = {}  # item -> [quiet notes naming it, gold left on it then]
        self._noted: deque[int] = deque(maxlen=64)  # rule lines already noted (a held one comes again when said)
        self.rule_reason: Callable[[str], str] | None = None  # rule id -> its reason (speech.rule_reason)

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
        if adv.source == "coach" and adv.speak and adv.id in self._held_tips:  # a held tip, said after all
            self._tip_heard(self._held_tips.pop(adv.id))
            return
        if adv.source == "coach" and not adv.held and adv.group in self._held_cards:
            # The verdict on an answer the gate held: its card is sent again, now saying whether it was heard (the
            # session log and the match record write it down then).
            card = self._held_cards.pop(adv.group)
            card.held, card.heard = False, bool(adv.speak)
            self.bus.publish(card)
            return
        if adv.source == "rules" and adv.final and adv.id in self._noted:
            if adv.speak and adv.display and adv.priority <= self.spoken_max:  # (held, and said now)
                self._heard.append((time.monotonic(), content_words(adv.text + " " + adv.spoken)))
            return
        if adv.source == "rules" and adv.final:
            self._noted.append(adv.id)
            self.recent_alerts.append(adv.text)
            if adv.speak and adv.display and adv.priority <= self.spoken_max:  # (shown-only isn't heard)
                self._heard.append((time.monotonic(), content_words(adv.text + " " + adv.spoken)))
                # "You can afford Black King Bar" pointed the player at it too: unless the old "remind" way is
                # on, the coach's next push of it is already a second one (an argument, see own_way).
                key = adv.key or ""
                if (key.startswith("afford:") and key.count(":") == 1 and self._own_way() != own_way.REMIND
                        and self.kb is not None):
                    self._count_push(key.removeprefix("afford:"), self.store.current)

    def _repeats(self, text: str, within: float = REPEAT_S) -> bool:
        """The line says again what was said out loud in the last REPEAT_S ("Buy a TP scroll" for the
        twentieth time): it's noted in the feed instead of spoken."""
        words = content_words(text)
        if len(words) < 2:
            return False
        now = time.monotonic()
        return any(now - when <= within and says_again(words, heard) for when, heard in self._heard)

    # subjects and the player's own build --------------------------------------------
    def _off(self, topic: str) -> bool:
        """A subject the player turned off in Settings (config.SuggestConfig): never brought up unasked."""
        return self.suggest is not None and not getattr(self.suggest, topic, True)

    def _build_mentions(self, text: str, snap: Snapshot) -> set[str]:
        """Items of the player's own build that a line names and they don't have yet: told to buy it, or told
        to "farm until" it, it's the same reminder. (An enemy's Blink Dagger isn't in their build.)"""
        named = self.kb.item_mentions(text, snap.hero_name)
        if not named:
            return set()
        have = self.kb.owned_with_parts(snap.all_item_names())
        hero = self.kb.heroes.get(snap.hero_name)
        build = set(self.kb.build_plan(snap.hero_name, snap.clock))
        build |= set(self.kb.typical_items(int(hero["id"]), 10)) if hero else set()
        return {i for i in named if i not in have and (i in build or i in self.kb.buy_mentions(text, snap.hero_name))}

    def _own_way(self) -> str:
        """"When I go my own way" (CoachConfig.own_way): "once", "follow" or "remind"."""
        return own_way.mode_of(self.cfg)

    def _pushed(self, text: str, snap: Snapshot | None) -> None:
        """Count a spoken line that points the player at an item they don't have (see MAX_ITEM_PUSHES). Buying
        a part of the item starts the count again: they're building it, so it's worth talking about. A push past
        the free ones that "once" let through was Doty's one argument for the item: it's noted (own_way)."""
        if self.kb is None or snap is None or not self.kb.items:
            return
        for item in self._build_mentions(text, snap):
            self._count_push(item, snap)

    def _count_push(self, item: str, snap: Snapshot | None) -> None:
        if snap is None or item not in self.kb.items:
            return
        left = self.kb.cost_left(item, snap.item_list())
        count, was = self._pushes.get(item, (0, left))
        count = 0 if left < was else count
        if count >= self._free_pushes() and self._own_way() == own_way.ONCE:
            reason = own_way.item_reason(self.kb, item, snap.hero_name, own_way.enemies_of(self.store, snap))
            if reason is not None:
                own_way.argued(self.store, item, reason)
        self._pushes[item] = [count + 1, left]

    def _free_pushes(self) -> int:
        return own_way.free_pushes(self.cfg, MAX_ITEM_PUSHES)

    def _overpushed(self, text: str, snap: Snapshot | None, asked: bool = False) -> set[str]:
        """Items this line points the player at that they've heard about often enough, with no part of it
        bought since: three times on "remind", else once, after which only "once" lets one more through, as its
        argument (a reason from this match, at a buying moment or when `asked`). A call to get out is never
        held back for it."""
        if self.kb is None or snap is None or not self.kb.items or RETREAT.search(text):
            return set()
        owned = snap.item_list()
        over = {i for i in self._build_mentions(text, snap)
                if i in self._pushes and self._pushes[i][0] >= self._free_pushes()
                and self.kb.cost_left(i, owned) >= self._pushes[i][1]}
        return {i for i in over
                if own_way.may_argue(self.store, self.kb, i, snap, self.cfg, asked) is None}

    def _held_back(self, text: str, snap: Snapshot | None, asked: bool = False) -> set[str]:
        """Pushes held back by "When I go my own way" in a line that can't go to the feed instead (an answer
        being read out, a prepared call firing). "remind" holds none of these back, as before it existed."""
        if self._own_way() == own_way.REMIND:
            return set()
        return self._overpushed(text, snap, asked)

    def skip_item(self, item: str | None = None, undo: bool = False) -> None:
        """Thread-safe (the skip hotkey, the app's Buy Next panel): leave `item` out of this match's build, or
        with no item, the one Doty is pointing at now. `undo` brings it back."""
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._skip_items, [item] if item else None, undo)
            except RuntimeError:
                pass  # the loop closed in between

    def _skip_items(self, items: list[str] | None, undo: bool = False, for_good: bool = False) -> None:
        snap, kb = self.store.current, self.kb
        if kb is None or snap is None or not snap.has_hero or not kb.items:
            return
        owned = snap.item_list()
        if items is None:
            target = kb.next_target(snap.hero_name, snap.clock, owned)
            items = [target] if target else []
        if not items:
            self.bus.publish(Advice("There's no item to skip right now.", Priority.INFO, "coach", source="coach",
                                    speak=False))
            return
        names = " and ".join(kb.item_name(i) for i in items)
        if undo:
            never = [i for i in items if i in own_way.never_items(self.cfg)]
            for item in items:
                kb.unskip(item)
                self._pushes.pop(item, None)
            text = f"{names} is back in your build."
            if never:  # (it was one the player never wanted: it's off that list too)
                self._remember(never, add=False)
                text = f"{names} is back in your build, and off your never-suggest list."
        else:
            for item in items:
                kb.skip(item, own_way.NEVER if for_good else buildlib.SAID_SO)
            self._last_skipped = list(items)
            # Calls armed earlier that push it, and the answer kept for the ask key, go with it.
            gone = set(items)
            self.triggers.active = [t for t in self.triggers.active
                                    if not (kb.item_mentions(t.say, snap.hero_name) & gone)
                                    and not any(c.kind == "afford" and c.var in gone for c in t.clauses)]
            nxt = kb.next_target(snap.hero_name, snap.clock, owned)
            if for_good:
                self._remember(items, add=True)
                text = f"Okay, I won't suggest {names} again, in any game." + (
                    f" Next: {kb.item_name(nxt)}." if nxt else "")
            else:
                text = f"Okay, no {names} this game." + (f" Next: {kb.item_name(nxt)}." if nxt else "")
        buildlib.passed_on(self.store)
        self.cached = None
        self.bus.publish(Advice(text, Priority.HIGH, "coach", source="coach"))

    def _remember(self, items: list[str], add: bool = True) -> None:
        """Add items to (or take them off) the player's never-suggest list, live, and tell the app so it saves it
        (Settings shows the same list, under "What Doty took from this")."""
        now = [i for i in (self.cfg.never_suggest or []) if isinstance(i, str)]
        if add:
            now += [i for i in items if i not in now]
        else:
            now = [i for i in now if i not in items]
        self.cfg.never_suggest = now
        if self.on_remember is not None:
            try:
                self.on_remember(list(items), add)
            except Exception as exc:  # (saving is the app's business: a failure there never stops coaching)
                print(f"[doty] couldn't save the never-suggest list: {exc!r}")

    def _remember_command(self, question: str) -> list[str] | None:
        """A spoken order that outlasts the match: "never suggest Black King Bar again", "skip Radiance, for
        good", or "remember that" after a skip. The items, or None when it isn't one. [] = "remember that" with
        nothing skipped to remember."""
        snap = self.store.current
        if self.kb is None or snap is None or not snap.has_hero or not self.kb.items:
            return None
        if ASKING.search(question) or len(question.split()) > COMMAND_MAX_WORDS:
            return None
        if REMEMBER_THAT.match(question):
            return list(self._last_skipped)
        never = NEVER_ASK.search(question)
        if never:
            items = self._named_next(question[never.end():], snap)
            return items or None
        order = SKIP_ASK.search(question)
        if order and FOR_GOOD.search(question):
            items = self._named_next(question[order.end():], snap)
            return items or None
        return None

    def _build_command(self, question: str) -> tuple[list[str] | None, bool] | None:
        """A spoken order about the build ("skip Black King Bar", "I don't need BKB", "skip it", "bring back
        Manta") as (items or None for the current one, undo), or None when it's something for the coach to
        answer. Deliberately narrow: short, not phrased as a question, and naming an item (or "it")."""
        snap = self.store.current
        if self.kb is None or snap is None or not snap.has_hero or not self.kb.items:
            return None
        if ASKING.search(question) or len(question.split()) > COMMAND_MAX_WORDS:
            return None
        back = UNSKIP_ASK.search(question)
        if back:
            items = self._named_next(question[back.end():], snap)
            return (items, True) if items else None
        order = SKIP_ASK.search(question)
        if not order:
            return None
        items = self._named_next(question[order.end():], snap)
        if items:
            return items, False
        return (None, False) if THIS_ITEM.search(question) else None

    def _named_next(self, rest: str, snap: Snapshot) -> list[str]:
        """The item named right after an order's words, [] if what follows is something else. "I don't want
        to die to Lion's Blink Dagger again" names an item, but not as the thing not wanted."""
        words = re.findall(r"[\w'’-]+", rest)
        while words and words[0].lower() in COMMAND_FILLER:
            words.pop(0)
        for n in range(1, 6):  # (item names run to four or five words: "Eul's Scepter of Divinity")
            found = self.kb.item_mentions(" ".join(words[:n]), snap.hero_name)
            if found and not self.kb.item_mentions(" ".join(words[1:n]), snap.hero_name):
                return sorted(found)
        return []

    def on_snapshot(self, snap: Snapshot) -> None:
        """Called on the event loop for every GSI update; schedules coach work."""
        if snap.custom_game:  # (an arcade game: Dota's own coaching doesn't apply)
            return
        self._same_match(snap)  # (first, so a new game's draft doesn't carry the last game's talk)
        own_way.honour(self.kb, self.cfg, snap, self.store)  # (after _same_match: a new match cleared the plan)
        if snap.has_hero and snap.in_game and not snap.in_draft:
            # Before the on/off check: prepared calls need no AI, so they keep firing if the coach goes off.
            for adv in self.triggers.check(snap, self.store, self.signals() if self.signals else None,
                                           self._afford(snap)):
                if self._already_have(adv.text, snap) or self._unwanted(adv.text, snap):
                    continue  # armed minutes ago, and since then the player bought it (or other boots)
                if self._held_back(adv.text, snap):
                    continue  # a push past what "When I go my own way" allows
                if self._cant_afford(adv.text, snap) or self._false_ownership(adv.text, snap):
                    continue  # written when it was true, maybe; checked as it fires ("you have the gold")
                if self._repeats(adv.text, TRIGGER_REPEAT_S):
                    continue  # (three "walk back to your tower" calls in nine seconds, in a real match)
                self._pushed(adv.text, snap)
                self.said.append(adv.text)
                self._heard.append((time.monotonic(), content_words(adv.text)))
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
        if self.store.just_died and not self._off("deaths"):
            # (a real death: Aegis and Reincarnation don't start a death look)
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
                         speak=False, heard=True)  # (asked for: always said)
            self.bus.publish(adv)
            self._speak(c.text, adv.id, asked=True)
            self._launch("now", visible=False)
            return
        self._launch(kind, visible=True)

    def ask_question(self, question: str, screenshot_b64: str | None = None) -> None:
        """Thread-safe: a spoken question from push-to-talk. Works outside matches too."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._question_on_loop, question, screenshot_b64)

    # "why?" ------------------------------------------------------------------------------------------------------
    def why(self) -> None:
        """Thread-safe (the why hotkey): explain the last tip said out loud."""
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._why_on_loop)
            except RuntimeError:
                pass

    def explain(self, adv: Advice | None) -> str:
        """Why a line was said, from what the code already knows: the reason its maker gave (Advice.why), the
        case for an item it names (its long form: what it answers in this lineup), or the rule's own reason. ''
        when only the AI could say."""
        if adv is None:
            return ""
        if adv.why:
            return adv.why
        snap, kb = self.store.current, self.kb
        if kb is not None and kb.items and snap is not None:
            for item in sorted(kb.item_mentions(adv.text, snap.hero_name)):
                case = kb.case_here(item)
                if case is not None and case.detail:
                    return f"{kb.item_name(item)}: {case.detail}."
        fact = self._card_fact(adv)
        if fact:
            return fact
        if adv.rule_id and self.rule_reason is not None:
            return self.rule_reason(adv.rule_id) or ""
        return ""

    def _card_fact(self, adv: Advice) -> str:
        """The hero card's own fact behind a coach tip that named a timing ("by 14:00") or an item of the build: the
        line of the card (what public games say for this hero, role and rank) that holds it. '' if none."""
        if adv.source != "coach":
            return ""
        snap, kb = self.store.current, self.kb
        ev = self.evidence.get(adv.id) or self.evidence.get(adv.group) or {}
        block = (ev.get("state") or {}).get("hero_card") or ""
        if not block and snap is not None and snap.has_hero:
            block = prompts.hero_card(snap, self.store, kb, game_mode.of(self.store.memory))
        if not block:
            return ""
        wanted = [m.group(0) for m in re.finditer(r"\b\d{1,2}:\d{2}\b", adv.text)]
        if kb is not None and kb.items and snap is not None:
            wanted += [kb.item_name(i) for i in kb.item_mentions(adv.text, snap.hero_name)]
        for part in re.split(r"\n|(?<=[.;])\s+", block):
            if any(w and w.lower() in part.lower() for w in wanted):
                return f"From the {kb.hero_name(snap.hero_name) if kb and snap else 'hero'} card: {part.strip()}"
        return ""

    def _why_on_loop(self) -> None:
        """Said even when Doty has talked a lot: the player asked."""
        last = self.gate.last if self.gate is not None else None
        text = self.explain(last)
        if text:
            self.bus.publish(Advice(text, Priority.HIGH, "coach", source="coach", asked=True))
        elif last is not None and last.source == "coach" and self.enabled and self.backend is not None:
            self._launch("question", visible=True, question=f'Why did you say "{last.text}"? Answer in one or '
                                                            "two sentences, from this game.")
        else:
            self.bus.publish(Advice("Nothing to explain yet.", Priority.INFO, "coach", source="coach", speak=False))

    def _question_on_loop(self, question: str, shot: str | None) -> None:
        if WHY_ASK.match(question):  # "why?", "why that?": the last tip, explained
            self._why_on_loop()
            return
        lasting = self._remember_command(question)
        if lasting is not None:  # "never suggest Black King Bar again": into the player's settings, at once
            if lasting:
                self._skip_items(lasting, for_good=True)
            else:
                self.bus.publish(Advice('Nothing to remember yet: say "never suggest" and the item.', Priority.INFO,
                                        "coach", source="coach", speak=False))
            return
        command = self._build_command(question)
        if command is not None:  # "skip Black King Bar": done at once, by Doty itself (works with the AI off)
            self._skip_items(*command)
            return
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
        if in_match:
            self._settings_into(summary, snap, asked=visible and kind in ("now", "question"))
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
                                       language=self.language, instructions=self.cfg.instructions)

        adv = Advice("…", Priority.HIGH, "coach", source="coach", trigger_t=t0, final=False, speak=False)
        # The player asked for this one (the ask key, a question, a plan): said even when Doty has talked a lot.
        # The player asked for this one (the ask key, a question, a plan), or it is one of the two things the AI
        # says that must be heard: the plan as the game starts and the answer after a death. Said even when Doty
        # has talked a lot (speech.py).
        asked = visible and kind in ("now", "question", "plan", "start", "death")
        heard_any = held_any = False  # a sentence of this answer was said out loud / is held for a slot
        adv.asked = asked  # (the card says so too: the bench leaves such a line out of its budget checks)
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
                    if in_match and kind != "question":
                        shown = self._without_bad_sentences(shown, snap)
                    adv.text, adv.created_t = shown, time.perf_counter()
                    self.bus.publish(adv)
                    # Speak each completed sentence as soon as it exists.
                    parts = SENTENCE_END.split(shown)
                    done = parts[:-1]
                    for sentence in done[spoken_upto:]:
                        line = self._speak(sentence, adv.id, asked)
                        heard_any, held_any = self._spoken(line, heard_any, held_any)
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
        text = _tag_lines(text)
        self._apply_lineup(text, game)
        self._apply_draft(text)
        # Named things are placed by code: "me" needs the game data's position; a minimap point naming a team
        # snaps to a hero marker Doty sees, when the minimap reader has left fresh ones.
        now_snap = self.store.current
        drawn = tuple(marklib.parse(text, shot.get("sizes", {}) if isinstance(shot, dict) else {},
                                    have_me=now_snap is not None and now_snap.position is not None,
                                    team=(snap.team if snap else "") or "",
                                    markers=marklib.fresh_markers(self.store.memory.get("minimap_markers"))))
        drawn = self._vet_marks(drawn, game, t0)
        shown = _strip_tags(text).strip()
        if visible and in_match and kind not in ("question", "watch", "draft"):
            shown = self._without_bad_sentences(shown, snap, complete=True).strip()
        if in_match:
            if not self._off("items"):
                self._apply_build(text, snap)
            if kind == "question":
                self._apply_skip(text, snap)
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
                    repeat = in_match and (self._repeats(shown) or bool(self._overpushed(shown, snap)))
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
            remaining = SENTENCE_END.split(shown)[spoken_upto:]
            if remaining and any(s.strip() for s in remaining):
                heard_any, held_any = self._spoken(self._speak(" ".join(remaining), adv.id, asked), heard_any,
                                                   held_any)
            adv.text, adv.final, adv.created_t, adv.marks = shown, True, time.perf_counter(), drawn
            adv.heard = heard_any  # (the card itself isn't spoken: its sentences were, if the speech gate let them)
            if held_any and not heard_any:  # (held for a slot: the log and the record wait for the verdict)
                adv.held = True
                self._held_cards[adv.id] = adv
            self.bus.publish(adv)
            self._keep_evidence(adv.id, kind, summary, shot, text, question)
            if in_match and shown and not _is_waiting(shown) and self.cfg.speak_up != "never" and heard_any:
                # (the death answer, then the look before respawning, mustn't say the same thing twice)
                self.said.append(shown)
                self._heard.append((time.monotonic(), content_words(shown)))
                self._pushed(shown, snap)
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
        unwanted = (self._unwanted(text, snap) or self._has_tp_already(text, snap)) if in_match else ""
        broke = self._cant_afford(text, snap) if in_match else set()
        foreign = self._foreign_spells(text) if in_match else []
        skill = self._wrong_skill(text, snap) if in_match else ""
        claimed = self._false_ownership(text, snap) if in_match else set()
        return self._too_late(kind, game, t0) or (
            f"gives a hero a spell that isn't theirs: {', '.join(foreign)}" if foreign else "") or (
            f"contradicts the skill order Doty says ({skill})" if skill else "") or (
            f"says the player has what they don't: {', '.join(sorted(self.kb.item_name(i) for i in claimed))}"
            if claimed else "") or (
            "talks about the player instead of to them" if META.search(text) else "") or (
            "starts mid-sentence" if text[:1].islower() else "") or (
            f"says to buy what you already have (or a second pair of boots): "
            f"{', '.join(sorted(self.kb.item_name(i) for i in have))}" if have else "") or unwanted or (
            f"says to buy now what the gold doesn't cover: "
            f"{', '.join(sorted(self.kb.item_name(i) for i in broke))}" if broke else "") or (
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
        if (not say or not tip or _is_waiting(tip) or self._repeats(tip) or self.cfg.speak_up == "never"
                or self._overpushed(tip, snap)):
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
            # The tip's age counts: a reply that took long has less time left to be heard before it's stale.
            left = WATCH_STALE_S - (time.perf_counter() - t0)
            # The speech gate may hold it for a quiet slot (a timer was just said): it stays good while it's still
            # right, checked again just before it's said (TIP_HOLD_S at most), rather than for a fixed few seconds:
            # a paid look isn't thrown away because a rune call came first.
            now = self.store.current
            born = (now.alive, now.clock) if now is not None else (True, 0)
            adv = Advice(text, Priority.HIGH, "coach", source="coach", trigger_t=t0, marks=drawn,
                         stale_s=min(WATCH_STALE_SPEECH_S, max(0.5, left)), hold_s=TIP_HOLD_S,
                         recheck=lambda: self._still_right(text, *born))
        else:
            adv = Advice(text, Priority.INFO, "watching", source="coach", trigger_t=t0, speak=False, overlay=False)
            if self._feed_has(text):
                return adv  # (the feed said this lately, or named this item often enough: nothing new to show)
        self.bus.publish(adv)
        if say and adv.speak:  # (heard now: the speech gate may have held it for a quieter moment, or dropped it)
            self._tip_heard(text)
        elif say:
            self._held_tips[adv.id] = text  # (heard later, if the gate finds it a slot: see note_alert)
            for old in list(self._held_tips)[:-8]:  # (the rest were dropped long ago)
                del self._held_tips[old]
        return adv

    def _feed_has(self, text: str) -> bool:
        """A quiet look's note (shown, not said) that adds nothing: near the same words as a note in the last
        REPEAT_S, or a buy line for an item the feed has already named speech.ITEM_CAP times with no part of it
        bought since. A real run showed Power Treads seven times and Wraith Band four in ten minutes. Notes it
        lets through are counted."""
        from ..speech import ITEM_CAP

        now, snap = time.monotonic(), self.store.current
        words = content_words(text)
        self._feed_notes = deque(((t, w) for t, w in self._feed_notes if now - t <= REPEAT_S), maxlen=12)
        if len(words) >= 2 and any(says_again(words, w) for _t, w in self._feed_notes):
            return True
        items = {}
        if self.kb is not None and self.kb.items and snap is not None and snap.has_hero:
            owned = snap.item_list()
            named = self.kb.buy_mentions(text, snap.hero_name) | (_items_named(self.kb, text) & self._build_items(snap))
            for item in named:
                left = self.kb.cost_left(item, owned)
                n, was = self._feed_items.get(item, (0, left))
                n = 0 if left < was else n
                if n >= ITEM_CAP:
                    return True
                items[item] = [n + 1, left]
        self._feed_notes.append((now, words))
        self._feed_items.update(items)
        return False

    def _build_items(self, snap: Snapshot) -> set[str]:
        """The items of the player's build at this point (what a note about "your next buy" is about)."""
        return set(self.kb.build_plan(snap.hero_name, snap.clock))

    def _tip_heard(self, text: str) -> None:
        self.said.append(text)
        self._heard.append((time.monotonic(), content_words(text)))
        self._pushed(text, self.store.current)

    def _still_right(self, text: str, alive: bool, clock: int) -> bool:
        """A live tip held by the speech gate, about to be said after all: still the same moment (alive or dead as
        it was, no fight since, at most TIP_HOLD_S of game time later) and it still passes the vetting it passed
        when it was written (nothing bought meanwhile that it sells, the gold still there, not said by now)."""
        snap = self.store.current
        if snap is None or not snap.has_hero or snap.alive != alive or self.store.in_fight():
            return False
        if snap.clock - clock > TIP_HOLD_S:
            return False
        return not (self._already_have(text, snap) or self._unwanted(text, snap) or self._has_tp_already(text, snap)
                    or self._cant_afford(text, snap) or self._held_back(text, snap) or self._repeats(text))

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

    def _speak(self, sentence: str, group: int = 0, asked: bool = False) -> "Advice | None":
        """One sentence of an answer, to the voice. `group`: the answer's card, so the speech gate says or holds
        the answer as one; `asked`: the player asked for it (always said). The line sent (its `speak` and `held`
        say what the gate made of it), or None for nothing to say."""
        sentence = sentence.strip()
        if not sentence:
            return None
        line = Advice(sentence, Priority.HIGH, "coach", source="coach", display=False, group=group, asked=asked)
        self.bus.publish(line)
        return line

    @staticmethod
    def _spoken(line: "Advice | None", heard: bool, held: bool) -> tuple[bool, bool]:
        if line is None:
            return heard, held
        return heard or line.speak, held or line.held

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
                 and not self._foreign_spells(t.say)
                 and not self._already_have(t.say, snap) and not self._unwanted(t.say, snap, t)]
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
            self._pushes.clear()
            self._feed_notes.clear()
            self._feed_items.clear()

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
        buys = self.kb.buy_mentions(text, snap.hero_name)
        if not buys:
            return set()
        # (a part of something built is fine when an item still being built needs it: knowledge/build.py)
        return buildlib.already_have(self.kb, buys, snap.all_item_names(), snap.hero_name, snap.clock, text)

    def _unwanted(self, text: str, snap: Snapshot | None, call: "triggerlib.Trigger | None" = None) -> str:
        """Why a line about buying isn't wanted ('' if it's fine): it pushes an item the player passed on this
        game, or item advice is turned off in Settings."""
        if self.kb is None or snap is None or not self.kb.items:
            return ""
        buys = self.kb.buy_mentions(text, snap.hero_name)
        afford = {c.var for c in call.clauses if c.kind == "afford"} if call is not None else set()
        if self._off("items") and (buys or afford):
            return "item advice is turned off"
        never = (buys | afford) & own_way.never_items(self.cfg)
        if never:  # (in every match: the player's settings say so, whatever this match's build holds)
            return f"pushes an item you never want suggested: {', '.join(sorted(self.kb.item_name(i) for i in never))}"
        passed = (buys | afford) & set(self.kb.skipped)
        if passed:
            return f"pushes an item you passed on: {', '.join(sorted(self.kb.item_name(i) for i in passed))}"
        # A counter item that answers little of this lineup (knowledge/judgement.py: a weak case, judged once all
        # five enemies are known). A real match: Black King Bar, 57 times, against right-click heroes. (An answer
        # to the player's own question never comes through here: asked about it, the coach may answer.)
        weak = {i for i in buys | afford if self.kb.weak_here(i)}
        if weak:
            return f"pushes an item that answers little of this lineup: " \
                   f"{', '.join(sorted(self.kb.item_name(i) for i in weak))}"
        return ""

    def _has_tp_already(self, text: str, snap: Snapshot | None) -> str:
        """The line says to buy a Town Portal Scroll while one sits in the player's TP slot."""
        if snap is None or snap.teleport is None or not TP_BUY.search(text):
            return ""
        return "says to buy a TP scroll you already have"

    def _without_bad_sentences(self, text: str, snap: Snapshot | None, complete: bool = False) -> str:
        """An answer the player is reading and hearing as it arrives (after a death, on the ask key) can't be
        dropped whole like a live tip, so it's checked a sentence at a time: one that sells what the player
        has, passed on or can't pay for, or a TP scroll they hold, is left out. `complete`: the whole answer
        is in, so its last sentence is judged too."""
        if snap is None or self.kb is None or not self.kb.items:
            return text
        parts = SENTENCE_END.split(text)
        last = parts[-1]
        if (complete or last.rstrip().endswith((".", "!", "?"))) and self._bad_sentence(last, snap):
            last = ""  # (otherwise the last part may still be arriving: judged once it's complete)
        return " ".join(p for p in [*(p for p in parts[:-1] if not self._bad_sentence(p, snap)), last] if p)

    def _bad_sentence(self, sentence: str, snap: Snapshot) -> bool:
        # (These answers are asked for, or come after a death, with the shop open: a moment for the one argument.)
        return bool(self._already_have(sentence, snap) or self._unwanted(sentence, snap)
                    or self._has_tp_already(sentence, snap) or self._cant_afford(sentence, snap)
                    or self._held_back(sentence, snap, asked=True) or bool(self._foreign_spells(sentence))
                    or bool(self._wrong_skill(sentence, snap)) or bool(self._false_ownership(sentence, snap))
                    or bool(META.search(sentence)))

    def _false_ownership(self, text: str, snap: Snapshot | None) -> set[str]:
        """Items a line says the player has, or has spent their gold on, that they don't hold. A real run: "Your
        gold is spent on Wraith Band now" to a player who never bought it, then "Buy Wraith Band now" a minute
        later. Any item counts, cheap ones too (the claim is about what's in the inventory)."""
        if self.kb is None or snap is None or not self.kb.items:
            return set()
        claimed = owned_claims(self.kb, text)
        return claimed - self.kb.owned_with_parts(snap.all_item_names()) if claimed else set()

    def _wrong_skill(self, text: str, snap: Snapshot | None) -> str:
        return wrong_skill(self.kb, text, snap)

    def _foreign_spells(self, text: str) -> list[str]:
        """"Lion's Fiend's Grip": a hero named with another hero's spell (knowledge/threats.foreign_spells, the
        bench's own test). The player would look for the wrong thing."""
        from ..knowledge import threats

        return threats.foreign_spells(self.kb, text) if self.kb is not None else []

    def _cant_afford(self, text: str, snap: Snapshot | None) -> set[str]:
        """Items a tip says to buy *now* when the player's gold covers no real part of them ("Buy Ogre Axe
        now" at 453 gold). A tip without "now" ("save for Black King Bar") is a plan, and fine."""
        if self.kb is None or snap is None or not self.kb.items or not NOW_CLAIM.search(text):
            return set()
        owned, gold = snap.item_list(), spendable(snap, game_mode.of(self.store.memory))
        buys = self.kb.buy_mentions(text, snap.hero_name) | (_items_named(self.kb, text) & set(self.kb.items)
                                                             if AFFORD_CLAIM.search(text) else set())
        if AFFORD_CLAIM.search(text):
            # "you have the gold", "you can afford it": the whole of what's left, not a part of it (a prepared call
            # in a real run: "Buy Power Treads next; you have the gold." with a few hundred gold).
            return {i for i in buys if 0 < self.kb.cost_left(i, owned) and self.kb.cost_left(i, owned) > gold}
        return {i for i in buys if self.kb.cost_left(i, owned) > 0 and self.kb.next_buy(i, owned, gold) is None}

    def _settings_into(self, summary: dict, snap: Snapshot, asked: bool = False) -> None:
        """What the player's settings and their own build change about the state the coach sees: subjects
        turned off (and for items, the build lists themselves, so there's nothing to recite), how often it has
        already said to buy each item, the items they never want, and how far it may argue for an item
        ("When I go my own way"). `asked`: the player asked for this answer."""
        from ..rules.engine import topics_off

        off = topics_off(self.suggest)
        if "items" in off:
            for key in ("next_item", "popular_next_items", "typical_items", "next_item_odds", "passed_on",
                        "counter_item_cases"):
                summary.pop(key, None)
        if "skills" in off:
            for key in ("unspent_skill_points", "next_skill", "talent_tiers_not_picked", "pro_skill_order"):
                summary.pop(key, None)
        if "deaths" in off:
            summary.pop("death_cause_candidates", None)  # (no death reviews: nothing to say why with)
        # This game's practice focus follows its own subject's switch, like the focus rule (rules/focus.py). (The
        # match brief's threat lines stay with "enemy" off: they're the facts that keep a death answer from naming
        # a spell nobody here has; "enemy" off stops the threat call and the coach bringing the enemy up.)
        plan = self.store.memory.get("focus_plan") or {}
        if "practice_focus" in summary and plan.get("key"):
            from ..knowledge.focus import TOPICS

            topic = TOPICS.get(plan["key"])
            if topic is not None and topic.switch in off:
                summary.pop("practice_focus", None)
        if off:
            summary["dont_advise_on"] = [prompts.TOPIC_WORDS[t] for t in sorted(off) if t in prompts.TOPIC_WORDS]
        if self.kb is None or "items" in off:
            return
        never = own_way.never_items(self.cfg)
        if never:
            names = sorted(self.kb.item_name(i) for i in never)
            summary["never_suggest"] = names
            passed = [n for n in summary.get("passed_on", []) if n not in names]
            if passed:
                summary["passed_on"] = passed
            else:
                summary.pop("passed_on", None)
        told = {self.kb.item_name(i): n for i, (n, _left) in self._pushes.items()
                if n >= 2 and i not in self.kb.skipped and i not in snap.all_item_names()}
        if told:
            summary["told_to_buy"] = told
        mode = self._own_way()
        if mode == own_way.REMIND:
            return
        summary["own_way"] = prompts.OWN_WAY_WORDS[mode]
        if mode == own_way.ONCE and self.kb.items:
            # The items the coach already pointed at that it could argue for right now, with the reason: the only
            # ones it may bring up again, once. (An item passed on is argued for by Doty itself: rules/own_way.)
            owned = snap.all_item_names()
            argue = {}
            for item in sorted(i for i, (n, _left) in self._pushes.items()
                               if n >= self._free_pushes() and i not in owned and i not in self.kb.skipped):
                reason = own_way.may_argue(self.store, self.kb, item, snap, self.cfg, asked)
                if reason is not None:
                    argue[self.kb.item_name(item)] = reason.text or "most players on this hero buy it"
            if argue:
                summary["may_argue_once"] = argue

    def _apply_skip(self, text: str, snap: Snapshot) -> None:
        """A SKIP line in the answer to a spoken question: the player said they won't buy these."""
        m = SKIP_LINE.search(text)
        if not m or self.kb is None or not self.kb.items:
            return
        items = [w.strip().strip(".").lower().replace(" ", "_") for w in m.group(1).split(",")]
        items = [i if i.startswith("item_") else f"item_{i}" for i in items if i]
        known = [i for i in items if i in self.kb.items and i not in snap.all_item_names()]
        for item in known:
            if self.kb.skip(item, buildlib.SAID_SO):
                self.bus.publish(buildlib.note(self.kb, item, buildlib.SAID_SO))
        if known:
            buildlib.passed_on(self.store)

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
        known = [i for i in known if i not in own_way.never_items(self.cfg)]
        if known:
            self.kb.custom_plan[snap.hero_name] = known
            own_way.honour(self.kb, self.cfg, snap, self.store)  # (the player's own build for the hero still leads)

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


def _tag_lines(text: str) -> str:
    """Each of Doty's tags on a line of its own, wherever the model wrote it."""
    return INLINE_TAG.sub(lambda m: f"\n{m.group(1)}:", text)


def _strip_tags(text: str) -> str:
    """Hide the lines meant for Doty (BUILD, LINEUP, DRAW, DRAFT), including one still arriving mid-stream."""
    text = TAG_LINE.sub("", _tag_lines(text))
    head, _, tail = text.rpartition("\n")
    if head and tail.strip() and any(tag.startswith(tail.strip().upper()) for tag in TAGS):
        text = head
    return text.rstrip()


WATCH_TAG = re.compile(r"^\s*(SAY|QUIET)\s*:\s*", re.IGNORECASE)
LATER_TAG = re.compile(r"(?<![A-Za-z])(SAY|QUIET):\s*")  # a second, corrected answer ("Wait... SAY: ...")
# A second thought after a finished sentence ("... Wait, correct that: learn Bash.") is cut off. "Wait" only counts
# as one when a pause or a correction word follows it: "Wait for your team" and "Wait until Luna is here" are
# instructions, and cutting them would leave half the tip (the bench caught it).
SELF_CORRECTION = re.compile(r"(?:\n|(?<=[.!?])\s)\s*(?:Wait(?=\s*[,:.;—–-]|\s+(?:no|sorry|scratch|"
                             r"correct|actually|i\s+mean)\b)|Actually|Correction)\b[,:.]?", re.IGNORECASE)
# A change of mind inside a sentence ("buy Tranquil... no: once back, buy a TP scroll", from a real match): what
# comes before it is the abandoned half, and what comes after is the tip.
MID_CORRECTION = re.compile(r"(?:\.{2,}|\u2026|\s[\u2014\u2013-]|,)\s*(?:no|wait|scratch\s+that|i\s+mean|actually|"
                            r"correction)\s*[,:;\u2014\u2013-]\s*", re.IGNORECASE)


def _is_waiting(text: str) -> bool:
    """The coach's "nothing to see" answer (a menu, a loading screen, Dota tabbed out)."""
    return text.lower().strip(" .") == "waiting"


def parse_watch(text: str) -> tuple[bool, str]:
    """('SAY: Back off, three missing.') -> (True, 'Back off, three missing.'). No tag = quiet.
    When the model corrects itself mid-reply ("SAY: ... Wait, that's wrong. SAY: ..."), the last tagged
    answer counts, and a "Wait, ..." with no new tag is cut off."""
    m = WATCH_TAG.match(text)
    if not m:
        return False, _settled(text)
    tag, rest = m.group(1), text[m.end():]
    later = list(LATER_TAG.finditer(rest))
    cut = False
    if later:
        tag, rest, cut = later[-1].group(1), rest[later[-1].end():], True
    return tag.upper() == "SAY", _settled(rest, cut)


def _settled(tip: str, cut: bool = False) -> str:
    """A tip with the coach's second thoughts taken out: a "Wait, ..." after a finished answer is cut off, a
    change of mind inside a sentence ("... no: ...") leaves only what came after it, and what follows a blank line
    or talks about "the player" (the model writing to itself: "Note: the player has gold for...", read out in a
    real run) is dropped. Whatever is left after a cut must stand alone as a sentence (_whole)."""
    tip = re.split(r"\n\s*\n", tip.strip(), maxsplit=1)[0]  # (a blank line: what follows is a note, not the tip)
    tip = SELF_CORRECTION.split(tip, 1)[0].strip()
    turns = list(MID_CORRECTION.finditer(tip))
    if turns:
        tip, cut = tip[turns[-1].end():].strip(), True
    parts = SENTENCE_END.split(tip)
    kept = [p for p in parts if p.strip() and not META.search(p)]
    if len(kept) < len(parts):
        tip, cut = " ".join(kept), True
    return _whole(tip) if cut else tip


# A piece that can't start a sentence of its own: what's left after a cut that took the start of the sentence.
DANGLING = re.compile(r"^(?:and|or|but|so|then|because|which|until|while|if|when|to|with|of|for|than|as|"
                      r"after|before|unless)\b", re.IGNORECASE)


def _whole(text: str) -> str:
    """What's left of a line after part of it was cut: a sentence of its own (re-capitalised), or nothing at all.
    A real run said "keep farming the safe lane." with its start gone."""
    text = text.strip()
    if not text or DANGLING.match(text) or len(re.findall(r"[\w']+", text)) < 3:
        return ""
    return text[:1].upper() + text[1:]


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


_ITEM_NAMES: dict = {}


def _items_named(kb: "Knowledge", text: str) -> set[str]:
    """Every item a piece of text names, cheap ones too (Knowledge.item_mentions is big items only): Wraith Band,
    Magic Wand, a TP scroll by its full name."""
    key = (id(kb.items), len(kb.items))
    if _ITEM_NAMES.get("key") != key:
        names = {str(v.get("dname") or "").lower(): k for k, v in kb.items.items()
                 if len(str(v.get("dname") or "")) >= 4 and "recipe" not in k}
        pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        _ITEM_NAMES.update(key=key, names=names, re=re.compile(rf"(?<![a-z])({pattern})(?![a-z])") if names else None)
    found = _ITEM_NAMES["re"].findall(text.lower()) if _ITEM_NAMES["re"] is not None else []
    return {_ITEM_NAMES["names"][n] for n in found} | set(kb.item_mentions(text))


def wrong_skill(kb: "Knowledge | None", text: str, snap: Snapshot | None) -> str:
    """The line tells the player to skill something other than the next skill in the order Doty itself says
    out loud ("Skill point: take Healing Ward"; the hero card's skill order), or to skip that one. A real run:
    "Skip Blade Fury until level 2" with Blade Fury first in the order, and "level Blade Dance" right after the
    rule said Healing Ward. The player heard two coaches disagree. The ability it names, or ''."""
    if kb is None or snap is None or not snap.has_hero or not snap.abilities:
        return ""
    have = {a.name: a.level for a in snap.abilities if not a.is_talent_or_attr}
    upcoming: list[str] = []  # the next few upgrades in the order, from where the player is now
    need: dict[str, int] = {}
    for ab in kb.skill_order(snap.hero_id):
        if ab not in have:
            continue
        need[ab] = need.get(ab, 0) + 1
        if need[ab] > have[ab]:
            upcoming.append(ab)
            if len(upcoming) >= 3:
                break
    if not upcoming:
        return ""
    want = kb.ability_name(upcoming[0]).lower()
    soon = {kb.ability_name(a).lower() for a in upcoming}
    names = {kb.ability_name(a.name).lower() for a in snap.abilities if not a.is_talent_or_attr}
    low = text.lower()
    for name in sorted((n for n in names if len(n) >= 4), key=len, reverse=True):
        esc = re.escape(name)
        if name in soon and re.search(rf"{SKIP_SKILL}\s+(?:\w+\s+)?{esc}\b", low):
            return name
        if name != want and re.search(rf"{LEVEL_SKILL}\s+(?:your\s+|the\s+|a\s+point\s+(?:in|into)\s+)?{esc}\b",
                                      low):
            return name
    return ""



def owned_claims(kb: "Knowledge", text: str) -> set[str]:
    """The items a line says the player has, bought or spent their gold on ("Your gold is spent on Wraith Band
    now"). One test for the coach's vetting (Coach._false_ownership) and the bench (checks.claims_owned)."""
    out: set[str] = set()
    for m in OWNED_CLAIM.finditer(text):
        clause = re.split(r"[.;:!?]|,\s*(?:so|then|and|but)\b", text[m.end():], maxsplit=1)[0]
        out |= _items_named(kb, clause)
    return out


def content_words(text: str) -> frozenset[str]:
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


def says_again(words: frozenset[str], before: frozenset[str]) -> bool:
    """A line (its `content_words`) says again what an earlier one said: at least two words in common, and most of
    the new line's words (REPEAT_OVERLAP). Measured against the new line: a short alert ("Power rune in 15") shares
    two words with many new tips ("Take the power rune, then gank bottom with Lion") that say much more. The coach
    (Coach._repeats) and the bench's repeat check both use this."""
    common = len(words & before)
    return common >= 2 and common >= REPEAT_OVERLAP * len(words)


_content_words = content_words  # (the old name)
