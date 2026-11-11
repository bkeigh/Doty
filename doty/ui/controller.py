"""Owns the Doty engine for the window: start/stop/restart, the demo match, and
relaying advice from engine threads to the GUI thread."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal

from ..advice import Advice
from ..config import Config, save_config
from ..rules.base import mmss


class Controller(QObject):
    advice = Signal(dict)  # one feed entry; emitted from any thread, delivered on the GUI thread
    changed = Signal()  # started, stopped, failed, demo started/ended
    kokoro_progress = Signal(int)  # percent, while the free Kokoro voice downloads
    kokoro_done = Signal(str)  # "" = downloaded (and Doty switched to it if it's the voice to use)
    # "Never suggest X again" said out loud (items, added): from the engine's thread, saved on the GUI thread.
    remembered = Signal(list, bool)
    preferences_changed = Signal()  # the never-suggest list or the instructions' reading changed (Settings shows it)

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.doty = None
        self.error = ""
        self.kokoro_pct: int | None = None  # None = not downloading
        self.kokoro_done.connect(self._kokoro_downloaded)
        self.remembered.connect(self._remembered)
        self._transcriber = None  # the Whisper model survives restarts (it takes seconds to load)
        self._transcriber_key: tuple | None = None
        self._demo_loop: asyncio.AbstractEventLoop | None = None
        self._demo_task: asyncio.Task | None = None
        self._demo_thread: threading.Thread | None = None
        self._restart_after_match = False  # a restart that waits until you're out of the match
        self._reader = None  # a voice for "Read to me" while Doty is paused (made on first use)

    def _overlay_moved(self, position: list[float]) -> None:
        """The cards were dragged over the game (or reset): remember it in the settings the window owns."""
        self.cfg.overlay.position = list(position)
        save_config(self.cfg)

    def report_tip(self, advice_id: int, text: str):
        """'Wrong tip?': save what the coach saw for that line to Documents\\Doty\\reports. Returns the folder,
        or None if Doty no longer has its details (an older line, or the engine restarted)."""
        if self.doty is None:
            return None
        from ..config import documents_dir

        return self.doty.coach.report(advice_id, text, documents_dir() / "Doty" / "reports")

    # engine ------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.doty is not None and self.doty.running

    @property
    def demo_running(self) -> bool:
        return self._demo_thread is not None and self._demo_thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        self._close_reader()  # (one voice at a time: Doty's own takes over)
        from ..app import Doty, StartError

        cfg = copy.deepcopy(self.cfg)  # Doty adjusts its config; keep ours as the user set it
        key = (cfg.listen.whisper_model, cfg.listen.device)
        doty = Doty(cfg, quiet=True, transcriber=self._transcriber if key == self._transcriber_key else None)
        doty.bus.subscribe(lambda adv, d=doty: self._relay(d, adv))
        doty.on_overlay_moved = self._overlay_moved
        doty.coach.on_remember = lambda items, added: self.remembered.emit(list(items), bool(added))
        try:
            doty.start()
        except StartError as exc:
            self.error = str(exc)
            self.changed.emit()
            return False
        except Exception as exc:  # show it rather than crash the window
            self.error = f"{exc.__class__.__name__}: {exc}"
            print(f"[doty] start failed: {exc!r}", file=sys.stderr)
            self.changed.emit()
            return False
        self.doty, self.error = doty, ""
        if doty.listen.transcriber is not None:
            self._transcriber, self._transcriber_key = doty.listen.transcriber, key
        self.changed.emit()
        return True

    def stop(self) -> None:
        self.stop_demo()
        if self.doty is not None:
            self.doty.shutdown()
            self.doty = None
        self.changed.emit()

    def restart(self) -> None:
        """Stop and start again with the current settings. Muted stays muted, hidden cards stay hidden."""
        was = self.running
        muted = was and self.doty.voice.muted
        hidden = was and self.doty.overlay is not None and not self.doty.overlay.isVisible()
        self.stop()
        if was and self.start():
            if muted and not self.doty.voice.muted:
                self.doty._toggle_mute()
            if hidden and self.doty.overlay is not None:
                self.doty.overlay.hide()

    def restart_when_free(self) -> None:
        """Restart now, or once the match you're in is over (a restart mid-match loses the coach's memory of
        it and splits the post-game review)."""
        st = self.status() if self.running else None
        if st is not None and st.get("phase") == "live":
            self._restart_after_match = True
        else:
            self.restart()

    def apply_config(self, cfg: Config) -> None:
        """Save new settings and restart the engine with them if it's running."""
        self.cfg = cfg
        save_config(cfg)
        self._close_reader()  # (the reading voice too: a new voice or speed is heard next time)
        self.restart()

    def preview_drawing(self, width: float, opacity: float) -> None:
        """Show the coach's drawings at this thickness and opacity now, before Saving (Settings' sliders): only the
        running overlay changes; the saved settings don't until Save."""
        overlay = self.doty.overlay if self.doty is not None else None
        if overlay is None:
            return
        overlay.cfg.mark_width, overlay.cfg.mark_opacity = float(width), float(opacity)
        overlay.update()

    def delete_records(self) -> int:
        """Settings > Delete records: every match record on this PC, the one being written included."""
        if self.doty is not None:
            return self.doty.record.delete_all()
        from ..match_record import delete_records, record_folder

        return delete_records(record_folder(self.cfg.record))

    def _remembered(self, items: list, added: bool) -> None:
        """Said out loud: these items join (or leave) the never-suggest list in the saved settings."""
        now = list(self.cfg.coach.never_suggest)
        now = now + [i for i in items if i not in now] if added else [i for i in now if i not in items]
        self.set_preferences(never_suggest=now)

    def set_preferences(self, suggest: dict | None = None, **coach) -> None:
        """Change the player's coaching preferences (CoachConfig fields such as never_suggest, hero_builds,
        speak_up, instructions_style; and SuggestConfig switches), live in the running engine and saved, without a
        restart: the rules and the coach read them on every update."""
        for cfg in [self.cfg] + ([self.doty.cfg] if self.doty is not None else []):
            for name, value in coach.items():
                setattr(cfg.coach, name, copy.deepcopy(value))
            for name, value in (suggest or {}).items():
                setattr(cfg.suggest, name, bool(value))
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self.preferences_changed.emit()

    def set_coaching(self, look_every_s: int | None = None, speak_up: str | None = None) -> None:
        """Change how often the coach looks and when it talks, live (no restart), and save it."""
        for cfg in [self.cfg] + ([self.doty.cfg] if self.doty is not None else []):
            if look_every_s is not None:
                cfg.coach.speculative_interval_s = int(look_every_s)
            if speak_up is not None:
                cfg.coach.speak_up = speak_up
        save_config(self.cfg)
        self.changed.emit()

    # reading aloud (the Learn tab) -----------------------------------------------------------------------
    def _voice_for_reading(self):
        if self.running:
            return self.doty.voice if self.doty.voice._thread is not None else None
        if self._reader is None:
            from ..voice import Voice

            vcfg = copy.deepcopy(self.cfg.voice)
            vcfg.enabled = True
            self._reader = Voice(vcfg)
            self._reader.language = getattr(self.cfg.app, "language", "en")  # (a voice that speaks it)
            self._reader.start()
        return self._reader

    def read_aloud(self, lines: list[str]) -> str:
        """Say these lines with Doty's voice: the running one, or a reader voice while Doty is paused. Stops
        anything being read before. Returns why it can't ("" if it's reading)."""
        if self.running and self.doty.voice.muted:
            return "Doty is muted. Unmute it to hear this."
        if self.running and (self.doty.status().get("phase") == "live"):
            return "Not during a match: Doty's voice is coaching you. Read it after the game."
        voice = self._voice_for_reading()
        if voice is None or not voice.available:
            return "The voice is off. Turn it on in Settings > Voice."
        voice.interrupt()
        for line in lines:
            if line.strip():
                voice.say(line.strip())
        self._reading = True
        return ""

    def stop_reading(self) -> None:
        if not getattr(self, "_reading", False):
            return  # (never cut off the coach for nothing)
        self._reading = False
        for voice in (self._reader, self.doty.voice if self.running else None):
            if voice is not None:
                voice.interrupt()

    def reading(self) -> bool:
        voice = self._reader if not self.running else self.doty.voice
        busy = voice is not None and voice._thread is not None and not voice.idle()
        if not busy:
            self._reading = False
        return busy and getattr(self, "_reading", False)

    def _close_reader(self) -> None:
        if self._reader is not None:
            self._reader.stop()
            self._reader = None

    def set_level(self, level: str, bracket: int | None = None, from_rank: bool | None = None) -> None:
        """The player's level (and the rank used for statistics), live and saved."""
        for cfg in [self.cfg] + ([self.doty.cfg] if self.doty is not None else []):
            cfg.coach.level = level
            if bracket is not None:
                cfg.coach.bracket = int(bracket)
            if from_rank is not None:
                cfg.coach.level_from_rank = bool(from_rank)
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self.changed.emit()

    def status(self) -> dict | None:
        if self.doty is None:
            return None
        if not self.doty.alive:
            self.error = "Doty's engine stopped unexpectedly. See the log for details."
            self.doty.shutdown()
            self.doty = None
            self.changed.emit()
            return None
        st = self.doty.status()
        if self._restart_after_match and st.get("phase") != "live":
            self._restart_after_match = False
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self.restart)  # (the match is over: now it's free to)
        return st

    # actions -----------------------------------------------------------------------
    def ask(self, kind: str) -> None:
        if self.running:
            self.doty.coach.ask(kind)

    def ask_question(self, text: str) -> None:
        """A typed question for the coach (e.g. "Ask Doty about this game"); answered in the feed and aloud."""
        if self.running:
            self.doty.coach.ask_question(text)

    def skip_item(self, key: str = "") -> None:
        """Leave an item out of this match's build (the Buy Next panel's "not this game"; `key` as the app
        names items, "black_king_bar"; '' = the item Doty is pointing at)."""
        if self.running:
            self.doty.coach.skip_item(f"item_{key.removeprefix('item_')}" if key else None)

    def toggle_mute(self) -> None:
        if self.running:
            self.doty._toggle_mute()

    def toggle_overlay(self) -> None:
        if self.running and self.doty.overlay is not None:
            self.doty.overlay.setVisible(not self.doty.overlay.isVisible())

    def start_demo(self, speed: float = 6.0, minutes: float = 26.5) -> None:
        """Feed the scripted match into the engine, as if Dota were sending it (starts coaching if needed)."""
        if self.demo_running or (not self.running and not self.start()):
            return
        from ..sim import run_sim

        g = self.cfg.gsi
        loop = asyncio.new_event_loop()
        task = loop.create_task(run_sim(f"http://{g.host}:{g.port}/", g.auth_token, speed=speed,
                                        end_clock=int(minutes * 60), verbose=False))
        # Sped up, alerts and tips arrive close together: each is said in turn (late ones are skipped) instead of
        # cutting each other off.
        self.doty.voice.calm = True
        self._demo_loop, self._demo_task = loop, task

        def go() -> None:
            try:
                loop.run_until_complete(task)
            except BaseException:  # cancelled, or the engine stopped mid-demo
                pass
            finally:
                loop.close()
                self.changed.emit()

        self._demo_thread = threading.Thread(target=go, name="doty-demo", daemon=True)
        self._demo_thread.start()
        self.changed.emit()

    def stop_demo(self) -> None:
        loop, task, thread = self._demo_loop, self._demo_task, self._demo_thread
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(3)
        self._demo_loop = self._demo_task = self._demo_thread = None
        if self.doty is not None:
            self.doty.voice.calm = False
            if thread is not None:
                self.doty.match_gone()  # (the top bar's LIVE and the overlay's strip go with the demo)

    # the free voice ------------------------------------------------------------------
    def kokoro_would_speak(self) -> bool:
        """With the saved settings, would Doty speak with Kokoro once it's downloaded?"""
        from ..eleven import api_key

        from .. import kokoro

        engine = self.cfg.voice.engine
        return kokoro.speaks(self.cfg.app.language) and (engine == "kokoro" or (engine == "auto" and not api_key()))

    def fetch_language_voices(self) -> None:
        """Kokoro is on this PC but its voices for your language aren't (you switched to Spanish, Portuguese
        or French after downloading it): fetch them, 1 to 2 MB, so Doty keeps its natural voice. Your real
        settings only."""
        from .. import kokoro
        from ..config import is_user_config

        lang = getattr(self.cfg.app, "language", "en")
        if (lang in kokoro.LANG_VOICES and self.cfg.voice.engine in ("auto", "kokoro") and is_user_config(self.cfg)
                and kokoro.installed() and not kokoro.installed(language=lang)):
            self.download_kokoro()

    def download_kokoro(self) -> None:
        """Fetch the Kokoro voice in the background (Setup and Settings share this one download)."""
        if self.kokoro_pct is not None:
            return
        from .. import kokoro

        self.kokoro_pct = 0
        self.kokoro_progress.emit(0)

        def work() -> None:
            def progress(done: int, total: int) -> None:
                pct = int(100 * done / max(1, total))
                if pct != self.kokoro_pct:
                    self.kokoro_pct = pct
                    self.kokoro_progress.emit(pct)

            try:
                kokoro.download(progress=progress, language=self.cfg.app.language)  # (its voices too)
                msg = ""
            except kokoro.KokoroError as exc:
                msg = str(exc)
            except Exception as exc:
                msg = f"couldn't download the Kokoro voice ({exc})"
            self.kokoro_pct = None
            self.kokoro_done.emit(msg)

        threading.Thread(target=work, name="doty-kokoro-download", daemon=True).start()

    def _kokoro_downloaded(self, msg: str) -> None:
        if msg:
            return
        if self.cfg.voice.engine == "windows":
            # You downloaded the natural voice because you want it: switch to it.
            self.cfg.voice.engine = "kokoro"
            try:
                save_config(self.cfg)
            except OSError:
                pass
        if self.running and self.kokoro_would_speak():
            self.restart_when_free()  # the voice thread picks its engine at start

    # plumbing ------------------------------------------------------------------------
    def _relay(self, doty, adv: Advice) -> None:
        if not adv.display:
            return
        snap = doty.store.current
        clock = mmss(snap.clock) if snap is not None and snap.has_hero else time.strftime("%H:%M")
        self.advice.emit({
            "id": adv.id, "source": adv.source, "priority": adv.priority, "category": adv.category,
            "text": adv.text, "final": adv.final, "latency_ms": adv.latency_ms, "clock": clock,
        })


def save_api_key(key: str, name: str = "ANTHROPIC_API_KEY") -> None:
    """Keep a key between runs and use it in this process right away. Windows: Credential Manager, encrypted
    for your account. Linux: the desktop's keyring, or a private file. See doty/keystore.py."""
    from .. import keystore

    key = key.strip()
    os.environ[name] = key
    keystore.save(name, key)
