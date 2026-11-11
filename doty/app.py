"""Wires the pieces together and owns the threads.

Threads:
  main      Qt event loop (overlay + tray), or asyncio when running headless
  doty-loop asyncio: GSI server, rule engine, coach, knowledge fetches
  doty-voice speech (ElevenLabs streaming or Kokoro on this PC, Windows SAPI as the fallback)
  doty-hotkeys Win32 message loop for RegisterHotKey
"""

from __future__ import annotations

import asyncio
import json
import math
import signal
import sys
import threading
import time
from collections import deque

from .advice import Advice, AdviceBus, Priority
from .capture import StripGrabber, grab_views, grab_window
from .coach import Coach
from .coach.prompts import SCREEN_FRESH_S
from .config import LOG_DIR, Config
from .gsi.server import GsiServer
from .hotkeys import Hotkeys
from .knowledge import mode as game_mode
from .knowledge import build, roles, timings
from .knowledge.constants import Knowledge
from .listen import VoiceInput
from .rules import RuleEngine, spoken_lines, urgent_lines
from .rules.economy import spendable
from .rules.progression import LaningTips
from .rules.base import mmss
from .state import POST_GAME, GameStateStore
from .voice import Voice
from .win import dota_is_foreground, find_dota_rect



class StartError(RuntimeError):
    """Doty couldn't start (usually: the GSI port is taken by another Doty)."""


def _quiet_resets(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows' asyncio reports a connection the other side reset (Dota closing its game-data connection,
    an API connection timing out) as an error with a traceback, though nothing is wrong. Everything else
    is reported as usual."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        return
    loop.default_exception_handler(context)


class Doty:
    def __init__(self, cfg: Config, *, overlay: bool = True, voice: bool = True, coach: bool = True,
                 listen: bool = True, quiet: bool = False, transcriber=None) -> None:
        self.cfg = cfg
        self.quiet = quiet
        cfg.overlay.enabled = cfg.overlay.enabled and overlay
        cfg.voice.enabled = cfg.voice.enabled and voice
        cfg.coach.enabled = cfg.coach.enabled and coach
        cfg.listen.enabled = cfg.listen.enabled and listen

        self.bus = AdviceBus()
        self._printed: deque[int] = deque(maxlen=32)
        self._logged: deque[int] = deque(maxlen=32)
        self.store = GameStateStore()
        self.kb = Knowledge()
        self.engine = RuleEngine(self.store, self.bus, cfg.rules, self.kb)
        self.engine.level_of = lambda: self.cfg.coach.level  # (live: the Learn tab can change it mid-game)
        self.engine.suggest = cfg.suggest  # which subjects Doty brings up (Settings > Alerts)
        self.voice = Voice(cfg.voice)
        from . import i18n

        self.voice.urgent = frozenset(i18n.tr(line) for line in urgent_lines())
        self.voice.language = getattr(cfg.app, "language", "en")
        self.voice.prepare([i18n.tr(line) for line in spoken_lines(cfg.rules)])
        from .knowledge.stats import Stats

        self.stats = Stats()
        self.coach = Coach(cfg.coach, self.store, self.bus, self.kb, screenshot=self._screenshot, stats=self.stats)
        self.coach.on_armed = self.voice.prepare_now  # prepared calls are voiced ahead, so they play instantly
        self.coach.spoken_max = cfg.voice.min_priority  # (what the voice doesn't say, the coach hasn't "said")
        self.coach.language = getattr(cfg.app, "language", "en")
        self.coach.signals = self._screen_signals
        self.coach.suggest = cfg.suggest
        self.server = GsiServer(cfg.gsi.host, cfg.gsi.port, cfg.gsi.auth_token, self._on_payload,
                                cfg.gsi.record_dir)
        self.hotkeys = Hotkeys()
        from .match_capture import MatchCapture
        from .review import MatchRecorder

        self.capture = MatchCapture(cfg.capture, grab=grab_window, foreground=dota_is_foreground)
        # The post-game review: deaths and what led to them, item timings, farm pace, three things to work on.
        self.review = MatchRecorder(self.kb)
        self.listen = VoiceInput(cfg.listen, self.bus, self.coach, self.voice,
                                 screenshot=self._screenshot if cfg.coach.use_screenshot else None,
                                 context_words=self._hero_words, transcriber=transcriber)
        self.listen.language = getattr(cfg.app, "language", "en")  # (push-to-talk in the player's language)
        self.overlay = None
        self.on_overlay_moved = None  # set by the window's controller: (position) -> None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self.started_error: BaseException | None = None
        self.rate = 0.0  # GSI updates per second, measured each second
        self.running = False
        self._second: tuple | None = None  # the game second the once-a-second work last ran for
        self._marks_at: tuple | None = None  # (where your hero stood, when) as the screen drawings went up
        self._voiced_ahead: set[str] = set()  # lines already handed to the voice to make ahead of time
        self._voiced_match = -1  # the match (store.generation) those lines belong to
        self._usual_mode: str | None = None  # "turbo" | "normal": what your recent matches were (None: not known)
        self._usual_for = 0  # the account that was looked up for it
        self._usual_pending = False  # that lookup is running
        self._usual_asked = 0.0  # when it last started (monotonic)
        self._match_gone = False  # the match's source was stopped on purpose (the demo): not live, as of now

        # The session log opens with its first line, so a launch without a match leaves no empty file.
        self._log_path = LOG_DIR / time.strftime("session-%Y%m%d-%H%M%S.jsonl")
        self._log = None

        self.bus.subscribe(self._console)
        self.bus.subscribe(self._session_log)
        self.bus.subscribe(self.coach.note_alert)
        self.bus.subscribe(self.voice.on_advice)
        self.bus.subscribe(self.review.on_advice)
        self.bus.subscribe(self._note_marks)

    # sinks ---------------------------------------------------------------------
    def _first_time(self, adv: Advice, seen: "deque[int]") -> bool:
        """A card can be published again once final (the coach adds its drawings): log it once."""
        if adv.id in seen:
            return False
        seen.append(adv.id)
        return True

    def _console(self, adv: Advice) -> None:
        if self.quiet or not adv.final or not adv.display or not self._first_time(adv, self._printed):
            return
        snap = self.store.current
        clock = mmss(snap.clock) if snap else "--:--"
        lat = f"{adv.latency_ms:6.1f} ms" if adv.latency_ms is not None else "        "
        print(f"[{clock:>6} | {adv.category:<11} | {lat}] {adv.text}", flush=True)

    def _session_log(self, adv: Advice) -> None:
        if not adv.final or not adv.display or not self._first_time(adv, self._logged):
            return
        snap = self.store.current
        if self._log is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._log = self._log_path.open("a", encoding="utf-8")
        self._log.write(json.dumps({
            "t": time.time(), "clock": snap.clock if snap else None, "source": adv.source,
            "category": adv.category, "priority": int(adv.priority), "text": adv.text,
            "latency_ms": None if adv.latency_ms is None else round(adv.latency_ms, 2),
        }) + "\n")
        self._log.flush()

    def _hero_words(self) -> str:
        """Current hero's name, to help Whisper spell it."""
        snap = self.store.current
        return self.kb.hero_name(snap.hero_name) if snap and snap.has_hero else ""

    def _screenshot(self, width: int | None = None) -> tuple[dict[str, str], float]:
        mask = self.overlay.mask_rect_px if self.overlay is not None else None
        return grab_views(min(width or self.cfg.coach.screenshot_width, self.cfg.coach.screenshot_width),
                          mask=mask, minimap_side=self.cfg.coach.minimap_side)

    # GSI -----------------------------------------------------------------------
    def _on_payload(self, payload: dict, received_t: float) -> None:
        self._match_gone = False
        snap = self.store.ingest(payload, received_t)
        # The review sees each update first: before the rules, so a line the death itself triggers (the death
        # streak) isn't counted as a warning given before it; and before the mode is set for this update, so a
        # match that ended without a post-game screen is closed under its own mode, not the next match's.
        self.review.on_snapshot(snap, self.store)
        # A normal game or Turbo (the game data doesn't say: knowledge/mode.py). The rules, the timers, the
        # coach and every timing the knowledge gives out follow it.
        self._learn_usual_mode(snap)
        self.kb.mode = game_mode.observe(snap, self.store, self.cfg.rules.game_mode, self._usual_mode)
        if self._usual_pending:  # (the start card holds its timings until the answer is in)
            self.store.memory["mode_pending"] = True
        else:
            self.store.memory.pop("mode_pending", None)
        roles.observe(snap, self.store, self.kb)  # which role the player has this game (the rules and coach use it)
        # The player's own build: an item they were pointed at and bought around leaves it (knowledge/build.py).
        for item in build.observe(snap, self.store, self.kb):
            self.bus.publish(build.note(self.kb, item))
        self.engine.on_snapshot(snap)
        self.coach.on_snapshot(snap)
        if self.capture.on:
            self.capture.on_payload(payload, snap, self.store)
        if snap.hero_id and not self.kb.has_hero_data(snap.hero_id):
            asyncio.get_running_loop().create_task(self.kb.ensure_hero(snap.hero_id))
        self._stale_marks(snap)
        second = (snap.has_hero and snap.in_game, snap.clock)
        if second != self._second:  # once per game second: the overlay's strip, lines to voice ahead
            self._second = second
            self._each_second(snap)

    USUAL_RETRY_S = 180.0

    def _learn_usual_mode(self, snap) -> None:
        """Once per account: what you usually play, from your own recent matches on OpenDota (the same
        public data the Profile page shows), fetched off the game-data thread."""
        if not self.running or self.cfg.rules.game_mode != "auto" or snap.is_demo:
            return
        account = self._account_id(snap)
        if not account or self._usual_pending:
            return
        now = time.monotonic()
        if account == self._usual_for and (self._usual_mode is not None or now - self._usual_asked < self.USUAL_RETRY_S):
            return  # (known, or asked a moment ago and OpenDota had nothing: asked again in a while)
        if account != self._usual_for:
            self._usual_mode = None  # (another account's habits say nothing about this one)
        self._usual_for, self._usual_asked = account, now
        self._usual_pending = True

        def work() -> None:
            from .knowledge.profile import Profile

            try:
                self._usual_mode = Profile(self.stats).usual_mode(account)
            except Exception as exc:  # no network, a bad reply: Doty assumes a normal game until the match shows
                print(f"[doty] couldn't read your recent matches for the game mode: {exc!r}")
            finally:
                self._usual_pending = False

        threading.Thread(target=work, name="doty-usual-mode", daemon=True).start()

    # the overlay's live strip, and keeping drawings honest -----------------------------------------
    MARKS_MOVED = 700  # world units your hero walks before a drawing on the game world points at the wrong spot
    MARKS_SHOWN_S = 8.0  # by then they've faded by themselves
    STRIP_AHEAD_S = 300  # the strip shows spawns up to this far away
    VOICE_AHEAD_EVERY_S = 10

    def _note_marks(self, adv: Advice) -> None:
        """A card went up with drawings on the game world: remember where you stood."""
        if adv.final and adv.marks and any(m.space == "screen" and not m.on_hud for m in adv.marks):
            snap = self.store.current
            self._marks_at = (snap.position if snap is not None else None, time.monotonic())

    def _stale_marks(self, snap) -> None:
        """Drawings on the game world were placed on a screenshot: once you've walked on, a fight has
        started or you've died, they point at the wrong thing, so they come down. (Minimap drawings stay:
        the minimap doesn't move.)"""
        if self._marks_at is None:
            return
        pos, when = self._marks_at
        now = snap.position
        moved = pos is not None and now is not None and math.hypot(now[0] - pos[0], now[1] - pos[1]) > self.MARKS_MOVED
        if time.monotonic() - when > self.MARKS_SHOWN_S:
            self._marks_at = None
        elif moved or (snap.has_hero and not snap.alive) or self.store.in_fight():
            self._marks_at = None
            if self.overlay is not None:
                self.overlay.drop_marks("screen")

    def _each_second(self, snap) -> None:
        if self.overlay is not None:
            self.overlay.set_hud(self._hud(snap))
        if snap.has_hero and snap.in_game and snap.clock % self.VOICE_AHEAD_EVERY_S == 0:
            self._voice_ahead(snap)

    def _hud(self, snap) -> dict | None:
        """The overlay's live strip: the next spawns with how far away they are, and the item you're
        building with the gold still to pay (None outside a match, or with the strip turned off)."""
        if not (snap.has_hero and snap.in_game) or snap.custom_game or not self.cfg.overlay.strip:
            return None
        groups: dict[int, list[str]] = {}
        for in_s, label in self._timers(snap, strip=True):
            if in_s <= self.STRIP_AHEAD_S:
                groups.setdefault(in_s, []).append(label)
        timers = [(in_s, labels) for in_s, labels in sorted(groups.items())][:2]
        item = None
        plan = self._shopping(snap, 1) if self.cfg.suggest.items else None  # (item advice off: no item on the strip)
        if plan and plan[0]["cost"] > 0 and plan[0]["left"] > 0:
            now = plan[0]
            short = now["short"]
            gpm = float(snap.player("gpm", 0) or 0)
            eta = snap.clock + int(short / gpm * 60) if short and gpm >= 100 else None
            item = {"name": now["name"], "left": short, "eta": eta, "done": max(0.0, 1.0 - short / now["cost"])}
        if not timers and item is None:
            return None
        return {"timers": timers, "item": item}

    def _shopping(self, snap, n: int = 3) -> list[dict] | None:
        """What to buy, in order (Knowledge.shopping): the one answer the strip, the app's Buy Next panel, the
        rules and the coach all show. None until there's item data to decide from."""
        if not self.kb.items or not snap.has_items_section:
            return None
        if not self.kb.item_popularity.get(snap.hero_id) and snap.hero_name not in self.kb.custom_plan:
            return None  # (the hero's build is still loading: that isn't "nothing left to buy")
        # (late in the game, buyback gold isn't for items)
        return self.kb.shopping(snap.hero_name, snap.clock, snap.item_list(),
                                spendable(snap, game_mode.of(self.store.memory)), n)

    def _voice_ahead(self, snap) -> None:
        """Lines the rules are about to say with a name in them ("Buy Ogre Axe", "Level Blade Fury", "Lion
        gets Blink Dagger soon") can't be made before the match like the fixed ones. They're predictable a
        while ahead, though, so a local voice makes them now and they play the instant they're needed."""
        kb, lines = self.kb, []
        plan = self._shopping(snap, 1) if self.cfg.suggest.items else None
        if plan:
            target, name = plan[0]["item"], plan[0]["name"]
            lines += [f"Buy {name}", f"Finish {name}"]
            lines += [f"Buy {kb.item_name(part)}"
                      for part in dict.fromkeys(kb.missing_parts(target, snap.item_list()))
                      if "item_recipe_" not in part]
        nxt = kb.next_skill(snap) if kb.skill_rows.get(snap.hero_id) else None
        if nxt:
            lines.append(f"Level {kb.ability_name(nxt)}")
        tracker = self.store.memory.get("lineup_votes")
        team = (snap.team or "").lower()
        if tracker is not None and team in ("radiant", "dire"):
            for npc in tracker.confirmed("dire" if team == "radiant" else "radiant"):
                hero = kb.heroes.get(npc)
                for when, item in (kb.spikes(int(hero["id"])) if hero else []):
                    if 0 <= when - snap.clock <= 240:
                        lines.append(f"{kb.hero_name(npc)} gets {kb.item_name(item)} soon")
        if self._voiced_match != self.store.generation:  # a new match: its lines, not the last one's
            self._voiced_match = self.store.generation
            self._voiced_ahead.clear()
        new = [line for line in dict.fromkeys(lines) if line not in self._voiced_ahead]
        if new and self.voice.prepare_now(new) is not False:  # (False: no local voice to make them yet)
            self._voiced_ahead.update(new)

    async def _status_task(self) -> None:
        last_count, last_t = 0, time.perf_counter()
        while True:
            await asyncio.sleep(1.0)
            now = time.perf_counter()
            self.rate = (self.server.received - last_count) / (now - last_t)
            last_count, last_t = self.server.received, now
            if self.overlay is not None:
                self.overlay.set_status(*self._overlay_status(now))
                self.overlay.set_laning(self._laning())
                if not self._match_running():  # the match ended or Dota closed: its strip goes with it
                    self.overlay.set_hud(None)

    EARLY_PACE = LaningTips.EARLY_PACE  # OpenDota's benchmark is a whole-match average; laning runs at ~3/4 of it

    def _laning(self) -> dict | None:
        """The laning gauge, minutes 1 to 10: last hits a minute against a target for this hero."""
        snap = self.store.current
        mode = game_mode.of(self.store.memory)  # (a Turbo lane is over in half the time)
        if snap is None or not snap.has_hero or not 60 <= snap.clock < mode.at(600) or not self._match_running():
            return None
        role = self.store.memory.get("role")
        if role is not None and role.support:
            return None  # (a support isn't measured by last hits)
        median = self.kb.benchmark_median(snap.hero_id, "last_hits_per_min")
        if not median:
            return None
        lh, dn = int(snap.player("last_hits", 0) or 0), int(snap.player("denies", 0) or 0)
        pace = lh / (snap.clock / 60.0)
        target = median * self.EARLY_PACE
        return {"lh": lh, "dn": dn, "pace": pace, "target": target, "ratio": pace / target if target else 1.0}

    def _overlay_status(self, now: float) -> tuple[str, str]:
        """(text, state) for the overlay's status badge: shown on hover, colored by state."""
        if self.server.received == 0:
            return "Waiting for Dota's game data (see Setup in Doty)", "waiting"
        if self.server.last_payload_t and now - self.server.last_payload_t > 35:
            return "Dota stopped sending game data", "stopped"
        coach = self.coach.status if self.cfg.coach.enabled else "off"
        if coach.startswith("off"):
            why = coach.removeprefix("off").strip(" ()")
            return f"Watching your game · AI coach off{f' ({why})' if why else ''}", "limited"
        return "Watching your game · AI coach ready", "live"

    # A match that's running sends an update every second (its clock moved) and Dota sends a heartbeat every
    # ten whatever happens, so silence past that means Dota stopped sending: it closed or crashed. (A match
    # that ends says so itself, and the demo's Stop button calls match_gone.) A shorter wait would call a
    # reconnect or a frozen client the end of the match. A paused match gets the long wait: an older
    # game-data file may have a 30 s heartbeat.
    LIVE_QUIET_S = 12.0
    STALE_S = 35.0

    def _match_running(self) -> bool:
        """You're in a match and its data is still coming in."""
        snap, last = self.store.current, self.server.last_payload_t
        if snap is None or last is None or self._match_gone or not (snap.has_hero and snap.in_game):
            return False
        return time.perf_counter() - last <= (self.STALE_S if snap.paused else self.LIVE_QUIET_S)

    def match_gone(self) -> None:
        """Whatever was sending the match was stopped on purpose (the demo's Stop button): it's over now,
        without waiting out the quiet seconds. The next update from a game makes it live again."""
        self._match_gone = True
        if self.overlay is not None:
            self.overlay.set_hud(None)

    def status(self) -> dict:
        """A snapshot of what Doty is doing, for the app window. Safe to call from any thread."""
        snap = self.store.current
        last = self.server.last_payload_t
        if self.server.received == 0:
            phase = "screen" if self.screen_only_active() else "waiting"
        elif last is not None and time.perf_counter() - last > self.STALE_S:
            phase = "stale"
        elif self._match_running():
            phase = "live"
        else:
            phase = "menu"
        listen = self.listen
        return {
            "phase": phase,
            "clock": snap.clock if snap is not None and snap.has_hero else None,
            "paused": bool(snap and snap.paused),
            "hero": self.kb.hero_name(snap.hero_name) if snap is not None and snap.has_hero else "",
            "rate": self.rate,
            "received": self.server.received,
            "rules_ms": self.engine.last_eval_ms,
            "coach": self.coach.status if self.cfg.coach.enabled else "off",
            "coach_model": (self.coach.backend.model if self.coach.backend is not None and self.coach.backend.model
                            else self.cfg.coach.model),
            "coach_provider": self.coach.backend.label if self.coach.backend is not None else "",
            "coach_ttft_ms": self.coach.last_ttft_ms,
            "coach_spend": dict(self.coach.spend),  # this match: looks, tokens, about how many dollars
            "voice": "off" if not self.cfg.voice.enabled else ("muted" if self.voice.muted else "on"),
            "voice_engine": self.voice.engine,  # "elevenlabs" | "kokoro" | "windows"
            "voice_name": self.voice.voice_name,
            "voice_device": self.voice.device,  # Kokoro: "GPU" or "CPU"
            "voice_problem": self.voice.problem,  # why the chosen voice isn't the one speaking
            "voice_ms": self.voice.last_speak_ms,
            "listen": "off" if not self.cfg.listen.enabled else ("ready" if listen.ready.is_set() else listen.status),
            "listen_key": self.cfg.listen.key,
            "mic": listen.recorder.device_name if listen.recorder is not None else "",
            "stt_model": getattr(listen.transcriber, "desc", "") if listen.ready.is_set() else "",
            "stt_ms": listen.last_stt_ms,
            "overlay": "off" if self.overlay is None else ("on" if self.overlay.isVisible() else "hidden"),
            "port": self.cfg.gsi.port,
            "live_coaching": self.cfg.coach.enabled and self.cfg.coach.speculative_interval_s > 0,
            "look_every_s": self.cfg.coach.speculative_interval_s,
            "speak_up": self.cfg.coach.speak_up,
            "draft": bool(snap and snap.in_draft),
            "post_game": bool(snap and snap.game_state == POST_GAME),  # the match just ended
            "win_team": (snap.raw.get("map") or {}).get("win_team", "") if snap is not None else "",
            "lineup": self.store.memory.get("lineup"),
            "draft_read": self.store.memory.get("draft"),  # picks the coach read off the draft screen
            "account_id": self._account_id(snap),
            "game": self._game_view(snap) if snap is not None and snap.has_hero else None,
        }

    def _account_id(self, snap) -> int:
        """Your own Dota account id from the game data (for the Profile page), 0 if not seen yet."""
        from .knowledge.profile import account_from_gsi

        return account_from_gsi(snap.raw.get("player") or {}) if snap is not None else 0

    def screen_only_active(self) -> bool:
        """Coaching from screenshots alone: no game data yet, coach ready, Dota window open."""
        c = self.cfg.coach
        return (c.enabled and c.screen_only and c.use_screenshot and c.speculative_interval_s > 0
                and self.coach.status.startswith("ready") and self.server.received == 0
                and find_dota_rect() is not None)

    async def _screen_only_task(self) -> None:
        """Without game data (launch option not set yet) the coach still looks at the screen on
        the usual interval, but only while Dota is the window you're using."""
        last = 0.0
        while True:
            await asyncio.sleep(2.0)
            c = self.cfg.coach
            fresh = self.server.last_payload_t is not None and time.perf_counter() - self.server.last_payload_t < 15
            if (fresh or not (c.enabled and c.screen_only and c.use_screenshot) or c.speculative_interval_s <= 0
                    or not self.coach.status.startswith("ready")):
                continue
            if time.monotonic() - last < max(c.speculative_interval_s, 15) or not dota_is_foreground():
                continue
            last = time.monotonic()
            self.coach.look_screen_only()

    def _enemies(self, snap) -> list[str]:
        lineup = self.store.memory.get("lineup") or {}
        team = (snap.team or "").lower()
        if team not in ("radiant", "dire"):
            return []
        return lineup.get("dire" if team == "radiant" else "radiant", [])

    def _timers(self, snap, strip: bool = False) -> list[tuple[int, str]]:
        """(seconds away, what) for what's coming up, soonest first. For the overlay's strip: no day and
        night (it's on Dota's own clock), and the next stack time for a support."""
        clock = snap.clock
        timers: list[tuple[int, str]] = []
        role = self.store.memory.get("role")
        mode = game_mode.of(self.store.memory)
        stacks = strip and (self.cfg.rules.announce_stacks
                            or (role is not None and role.support and clock < mode.at(1200)))
        for sp in timings.spawns(mode.turbo):
            if sp.key == "stack":
                if not stacks:
                    continue
            elif sp.key == "lotus" or (sp.toggle and not getattr(self.cfg.rules, sp.toggle, True)):
                continue
            upcoming = sp.times_after(clock, 1200)
            if upcoming:
                timers.append((upcoming[0] - clock, sp.label))
        if clock >= 0 and not strip:
            half = timings.DAY_NIGHT_CYCLE_S // 2
            timers.append(((clock // half + 1) * half - clock, "Night falls" if timings.is_day(clock) else "Day breaks"))
        rosh = self.store.memory.get("roshan_killed_clock")
        if rosh is not None:
            lo, hi = timings.roshan_window(rosh, mode.turbo)
            if clock < lo:
                timers.append((lo - clock, "Roshan can respawn"))
            elif clock < hi:
                timers.append((hi - clock, "Roshan is surely up"))
        timers.sort()
        return timers

    def _game_view(self, snap) -> dict:
        """What Doty sees of your game right now, for the app's live panel."""
        clock = snap.clock
        timers = self._timers(snap)
        mode = game_mode.of(self.store.memory)
        plan = self._shopping(snap)
        short = lambda item: (item or "").removeprefix("item_")  # noqa: E731  (the app's item keys have no prefix)
        return {
            "mode": mode.key,  # "normal" | "turbo", and why Doty thinks so
            "mode_source": mode.source,
            # What to buy, in order: the same list the strip, the rules and the coach go by (None: no item data yet).
            "build": None if plan is None else [
                {**e, "key": short(e["item"]), "buy": short(e.get("buy")) or None,
                 "buy_name": self.kb.item_name(e["buy"]) if e.get("buy") else ""} for e in plan],
            "hero": self.kb.hero_name(snap.hero_name),
            "team": snap.team,
            "level": snap.level,
            "alive": snap.alive,
            "respawn_s": snap.hero("respawn_seconds", 0) or 0,
            "hp": snap.hp,
            "max_hp": snap.max_hp,
            "mana_pct": snap.mana_pct,
            "mana": int(snap.hero("mana", 0) or 0),
            "max_mana": int(snap.hero("max_mana", 0) or 0),
            "gold": snap.gold,
            "kda": (snap.player("kills"), snap.player("deaths"), snap.player("assists")),
            "last_hits": snap.player("last_hits"),
            "denies": snap.player("denies"),
            "gpm": snap.player("gpm"),
            "items": [self.kb.item_name(i.name) for i in snap.inventory],
            "item_keys": [i.name.removeprefix("item_") for i in snap.inventory],
            "neutral": self.kb.item_name(snap.neutral.name) if snap.neutral else "",
            "neutral_key": snap.neutral.name.removeprefix("item_") if snap.neutral else "",
            "has_tp": snap.teleport is not None,
            "daytime": snap.daytime,
            "clock": clock,
            "timers": [{"in_s": s, "label": label} for s, label in timers[:5]],
            "enemies": self._enemies(snap),
        }

    # lifecycle -------------------------------------------------------------------
    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.loop.set_exception_handler(_quiet_resets)
        self._stop = asyncio.Event()
        try:
            await self.server.start()
        except OSError as exc:
            self.started_error = exc
            self._ready.set()
            return
        # The coach's warm-up is a network round trip; don't make startup wait for it.
        # One yield lets Coach.start bind this loop before anything can ask it a question.
        coach_started = asyncio.create_task(self.coach.start())
        await asyncio.sleep(0)
        async def knowledge() -> None:
            await self.kb.load()
            await self.kb.ensure_patch_notes()  # (this patch's changes, for the coach)

        asyncio.create_task(knowledge())
        if self.cfg.coach.enabled:  # hero statistics for draft looks, loaded off the event loop
            asyncio.create_task(asyncio.to_thread(self.stats.load))
        asyncio.create_task(self._status_task())
        asyncio.create_task(self._screen_only_task())
        self._ready.set()
        try:
            await coach_started
        except Exception as exc:  # whatever the coach's start-up hit, the game data, rules and voice keep going
            self.coach._disable(f"couldn't start ({exc.__class__.__name__})")
        print(f"[doty] listening for Dota GSI on http://{self.cfg.gsi.host}:{self.cfg.gsi.port}/ "
              f"(coach: {self.coach.status}, voice: {'on' if self.cfg.voice.enabled else 'off'}, "
              f"overlay: {'on' if self.cfg.overlay.enabled else 'off'})", flush=True)
        await self._stop.wait()
        await self.server.stop()
        await self.coach.close()
        await self.kb.close()

    def _start_loop_thread(self) -> threading.Thread:
        t = threading.Thread(target=lambda: asyncio.run(self._main()), name="doty-loop", daemon=True)
        t.start()
        self._ready.wait(10)
        if self.started_error:
            raise StartError(f"couldn't listen for Dota on port {self.cfg.gsi.port} "
                             f"(is Doty already running?): {self.started_error}")
        return t

    def request_stop(self) -> None:
        if self.loop and self._stop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass  # loop finished between the check and the call

    def _bind_hotkeys(self) -> None:
        hk = self.cfg.hotkeys
        if not hk.enabled:
            return
        self.hotkeys.bind(hk.ask, lambda: self.coach.ask("now"))
        self.hotkeys.bind(hk.plan, lambda: self.coach.ask("plan"))
        self.hotkeys.bind(hk.mute, self._toggle_mute)
        if hk.skip_item:
            self.hotkeys.bind(hk.skip_item, self.coach.skip_item)  # "not that one": the next item instead
        if self.overlay is not None:
            self.hotkeys.bind(hk.toggle_overlay, self.overlay.toggle_visible)
        self.hotkeys.start()
        if self.hotkeys.failed:
            print(f"[doty] couldn't register hotkeys {self.hotkeys.failed} (another app owns them?)")
        else:
            print(f"[doty] hotkeys: ask={hk.ask} plan={hk.plan} mute={hk.mute} overlay={hk.toggle_overlay} "
                  f"skip item={hk.skip_item}")

    def _toggle_mute(self) -> None:
        muted = self.voice.toggle_mute()
        self.bus.publish(Advice("Voice muted" if muted else "Voice on", Priority.INFO, "general", speak=False,
                                ttl_s=2))

    def start(self) -> None:
        """Non-blocking start. With the overlay on, call from the Qt GUI thread
        (a QApplication must exist). Raises StartError if Dota's port is taken."""
        try:
            self.voice.start()
            if self.cfg.overlay.enabled:
                from .overlay import Overlay

                self.overlay = Overlay(self.cfg.overlay, self.cfg.coach.minimap_side,
                                       on_moved=self._save_overlay_position)
                self.bus.subscribe(self.overlay.submit)
                self.overlay.show()
            self._loop_thread = self._start_loop_thread()
            self._bind_hotkeys()
            self.listen.start()  # after the loop: questions go to the coach's event loop
        except BaseException:
            self.shutdown()
            raise
        self.running = True
        self._start_vision()

    # Doty's own top-bar reader ------------------------------------------------------------------
    VISION_EVERY_S = 1.0
    VISION_REFRESH_S = 300.0  # rebuild the reader now and then: more portraits arrive in the art cache
    VISION_MIN_CONF = 0.6  # a portrait read this surely counts towards the lineup...
    VISION_VOTE_EVERY_S = 3.0  # ...as one read every few seconds, once two reads in a row agree
    VISION_DEAD_CONF = 0.4  # greyed-out portraits score lower (the respawn timer covers them): with the ten known, enough
    VISION_BAR_FOUND = 0.8  # the bar must clearly show on that side for its dead heroes to count

    def _start_vision(self) -> None:
        if getattr(self.cfg.coach, "vision", "off") not in ("shadow", "on"):
            return
        threading.Thread(target=self._vision_loop, name="doty-vision", daemon=True).start()

    def _vision_loop(self) -> None:
        """Read the top bar about once a second during a match, and log it beside the AI's lineup
        (vision-<time>.jsonl in the logs folder). With vision "on" (the default), steady reads count
        towards the lineup (who's in the game, on which side) and greyed-out portraits tell the coach
        who's dead; "shadow" only logs."""
        from . import vision

        old = sorted(LOG_DIR.glob("vision-*.jsonl"))[:-self.VISION_LOGS_KEPT]
        for path in old:  # one log per session: keep the latest few
            path.unlink(missing_ok=True)
        state = {"reader": None, "built": 0.0, "log": None}
        errors, last_error = 0, ""
        try:
            while self.running:
                time.sleep(self.VISION_EVERY_S if errors < 5 else 30.0)  # failing over and over: try less often
                try:
                    self._vision_step(vision, state)
                    errors = 0
                except Exception as exc:  # shadow mode must never take coaching down, nor stop for one bad read
                    errors += 1
                    if repr(exc) != last_error:
                        last_error = repr(exc)
                        print(f"[doty] vision (shadow): {exc!r}")
        finally:
            if state["log"] is not None:
                state["log"].close()
            if state.get("grabber") is not None:
                state["grabber"].close()

    VISION_LOGS_KEPT = 20

    def _vision_step(self, vision, state: dict) -> None:
        snap = self.store.current
        if snap is None or not snap.in_game or not snap.has_hero or not self.kb.heroes:
            return
        if not dota_is_foreground():
            return
        if state["reader"] is None or time.monotonic() - state["built"] > self.VISION_REFRESH_S:
            state["built"] = time.monotonic()
            try:
                state["reader"] = vision.TopBarReader.from_cache(heroes=list(self.kb.heroes))
            except ValueError:  # no portraits cached yet
                return
        reader = state["reader"]
        # Only the strip the reader looks at is grabbed (a few dozen rows of pixels, not the whole window
        # every second). If that can't be had, or comes back black, the window's own picture is used.
        strip = None
        if hasattr(reader, "read_band"):
            if state.get("grabber") is None:
                state["grabber"] = StripGrabber()
            strip = state["grabber"].grab(reader.band_box)
        frame = None
        if strip is None:
            frame = grab_window()
            if frame is None:
                return

        def read(candidates=None):
            if strip is not None:
                return reader.read_band(*strip, candidates=candidates)
            return reader.read(frame) if candidates is None else reader.read(frame, candidates=candidates)

        slots = read()  # against every hero: what lineup votes come from
        fit, ms = reader.last_fit, reader.last_ms
        on = getattr(self.cfg.coach, "vision", "") == "on"
        lineup = self.store.memory.get("lineup_votes")
        ten = [h for names in lineup.as_dict().values() for h in names] if lineup is not None and lineup.complete else []
        # With all ten known, a second read compares only them: a greyed-out portrait (respawn timer over it)
        # still reads surely, so who's dead can be told.
        known = read(ten) if on and ten else None
        if on and fit is not None:
            self._use_reading(vision, slots, fit, snap, state, known)
        agree = disagree = 0
        if lineup is not None:
            for sl in slots:
                side = lineup.side_of(sl.hero) if sl.hero and sl.confidence >= 0.7 else None
                if side is not None:
                    agree, disagree = agree + (side == sl.side), disagree + (side != sl.side)
        if state["log"] is None:
            state["log"] = (LOG_DIR / time.strftime("vision-%Y%m%d-%H%M%S.jsonl")).open("a", encoding="utf-8")
        state["log"].write(json.dumps({
            "t": round(time.time(), 2), "clock": snap.clock, "ms": round(ms, 1),
            "found": [round(f, 2) for f in fit.found] if fit else None,
            "slots": [[s.side, s.index, s.hero.removeprefix("npc_dota_hero_"), round(s.confidence, 2),
                       s.dead] for s in slots],
            "known": [[s.hero.removeprefix("npc_dota_hero_"), round(s.confidence, 2), s.dead] for s in known]
            if known else None,
            "dead": (self.store.memory.get("screen") or {}).get("dead"),
            "ai_lineup": self.store.memory.get("lineup"), "agree": agree, "disagree": disagree,
        }) + "\n")
        state["log"].flush()

    def _use_reading(self, vision, slots, fit, snap, state: dict, known) -> None:
        """Vision "on": a steady read of the top bar counts towards the lineup, and greyed-out portraits
        (once all ten are known) tell the coach and the calls who is dead right now."""
        now = time.monotonic()
        teams = vision.teams(slots, self.VISION_MIN_CONF)
        last = state.get("last_teams") or {}
        # Heroes two reads in a row agree on, side by side. A portrait read near the cut-off (your own
        # highlighted one often is) flickers in and out; the rest still counts.
        steady = {side: [h for h in names if h in last.get(side, [])] for side, names in teams.items()}
        state["last_teams"] = teams
        if (sum(len(v) for v in steady.values()) >= 3
                and now - state.get("voted", -1e9) >= self.VISION_VOTE_EVERY_S):
            state["voted"] = now
            self.coach.screen_read(steady["radiant"], steady["dire"], snap.match_id)
        if not known:
            return
        dead: dict[str, list[str]] = {"radiant": [], "dire": []}
        for s in known:
            if fit.found[0 if s.side == "radiant" else 1] < self.VISION_BAR_FOUND:
                return  # that side of the bar isn't clearly there (a menu over it, the camera's edge): no reading
            if s.hero and s.dead and s.confidence >= self.VISION_DEAD_CONF and s.hero != snap.hero_name:
                dead[s.side].append(s.hero)  # (your own hero's death comes from the game data)
        if dead == state.get("last_dead"):  # two reads in a row agree
            down = {h for names in dead.values() for h in names}
            before = (self.store.memory.get("screen") or {}).get("since") or {}
            # When each was first seen dead (game clock), so the coach knows how long a window has been open.
            since = {h: before.get(h, snap.clock) for h in down}
            self.store.memory["screen"] = {"t": now, "dead": dead, "match": snap.match_id, "since": since}
        state["last_dead"] = dead

    def _screen_signals(self) -> dict[str, float]:
        """What the top-bar reader sees right now, for the coach's prepared calls ({} when it has no fresh
        reading: a call that needs one then waits)."""
        screen = self.store.memory.get("screen") or {}
        snap = self.store.current
        team = (snap.team or "").lower() if snap is not None else ""
        if team not in ("radiant", "dire") or time.monotonic() - screen.get("t", -1e9) > SCREEN_FRESH_S:
            return {}
        if screen.get("match") != snap.match_id:
            return {}
        dead = screen.get("dead") or {}
        enemy = "dire" if team == "radiant" else "radiant"
        return {"enemies_dead": len(dead.get(enemy, [])), "allies_dead": len(dead.get(team, []))}

    def _save_overlay_position(self) -> None:
        """You dragged the overlay (or reset it): keep it there next time. The window's controller keeps
        its own copy of the settings, so it's told too (else its next save would undo the move)."""
        from .config import load_config, save_config

        try:
            if self.on_overlay_moved is not None:
                self.on_overlay_moved(list(self.cfg.overlay.position))
            else:
                # Only the position: this session's settings may hold one-run switches (--no-voice,
                # --record) that must not become permanent.
                saved = load_config(self.cfg.source_path) if self.cfg.source_path else load_config()
                saved.overlay.position = list(self.cfg.overlay.position)
                save_config(saved, self.cfg.source_path or saved.source_path)
        except OSError as exc:
            print(f"[doty] couldn't save the overlay position: {exc!r}")

    @property
    def alive(self) -> bool:
        return self._loop_thread is not None and self._loop_thread.is_alive()

    def shutdown(self) -> None:
        """Stop everything and release the port, hotkeys and mic. Safe to call twice."""
        self.running = False
        self.request_stop()
        if self._loop_thread is not None:
            self._loop_thread.join(5)
        try:
            self.review.finish()  # a match cut short (Doty closed mid-game) still gets its review
        except Exception as exc:
            print(f"[doty] couldn't save the review: {exc!r}")
        self.listen.stop()
        self.voice.stop()
        self.hotkeys.stop()
        self.capture.close()
        if self.overlay is not None:
            self.overlay.close()
            self.overlay.deleteLater()
            self.overlay = None
        if self._log is not None and not self._log.closed:
            self._log.close()

    def run(self, after_start=None) -> None:
        """Blocking (the CLI). `after_start(doty)` runs once the server is listening (used by demo)."""
        if self.cfg.overlay.enabled:
            from PySide6.QtWidgets import QApplication

            if sys.platform.startswith("linux"):
                from .x11 import prefer_xcb

                prefer_xcb()  # on Wayland, the overlay needs X11 to sit over the game
            app = QApplication.instance() or QApplication(sys.argv)
            app.setQuitOnLastWindowClosed(False)
        try:
            self.start()
        except StartError as exc:
            raise SystemExit(f"[doty] {exc}")
        if after_start:
            after_start(self)
        try:
            if self.cfg.overlay.enabled:
                self._exec_qt(app)
            else:
                while self.alive:
                    self._loop_thread.join(0.25)
        except KeyboardInterrupt:
            print("\n[doty] stopping")
        self.shutdown()

    def _exec_qt(self, app) -> None:
        from PySide6.QtCore import QTimer

        from .tray import make_tray

        tray = make_tray(app, self)  # noqa: F841 - must stay referenced
        # Let Ctrl+C in the console quit the Qt loop.
        signal.signal(signal.SIGINT, lambda *_: app.quit())
        pump = QTimer()
        pump.timeout.connect(lambda: None if self.alive else app.quit())
        pump.start(250)
        app.exec()
