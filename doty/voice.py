"""Doty's voice: ElevenLabs or Kokoro (both human-sounding), with the Windows SAPI voice behind them.

Speech runs on its own thread (COM apartment for SAPI) fed by a queue.
- **Engines:** ElevenLabs (paid, cloud) when a key is set, else Kokoro (free, on this PC) once it's
  downloaded, else the Windows voice. `voice.engine` in doty.toml can force one.
- **Urgent lines:** a CRITICAL advice, push-to-talk or mute drops everything queued and cuts off
  what's playing, so "back off" is never stuck behind a rune timer.
- **No gaps:** both engines feed one playback buffer. The next line (or sentence) is fetched or
  made while the current one plays.
- **Ready ahead:** with Kokoro, Doty's fixed alerts ("Back off, low HP", rune timers) are made in
  idle moments and cached on disk, so they play instantly.
- **Fallback:** if ElevenLabs is out (bad key, no credits) Kokoro takes over when it's downloaded,
  otherwise the Windows voice. A line that fails is spoken by the Windows voice. Doty never goes
  quiet because of a network problem.
- **Linux:** the backup voice is eSpeak NG or speech-dispatcher (`SystemSpeaker`), whichever is installed;
  everything else is the same.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable

from . import kokoro as kokoro_files
from . import languages
from .advice import Advice, Priority
from .config import VoiceConfig
from .eleven import (CACHE_MAX_CHARS, DEFAULT_MODEL, FALLBACK_VOICE, SAMPLE_LINE, ElevenError, ElevenLabs,
                     PcmPlayer, PhraseCache, api_key, pick_voice, speed_for_rate)
from .kokoro import DEFAULT_VOICE as KOKORO_DEFAULT
from .kokoro import VOICES as KOKORO_VOICES
from .kokoro import FILTER_VERSION as KOKORO_FILTER_VERSION
from .kokoro import Kokoro, KokoroError, voice_label

SVSF_ASYNC = 1
SVSF_PURGE = 2
WAKE = ("", False, None)  # wakes the voice thread to make newly prepared lines (not a line itself)
# The robotic backup voice (engine "windows" in the settings, whatever the system): SAPI on Windows,
# eSpeak NG or speech-dispatcher on Linux.
BACKUP = "the Windows voice" if sys.platform == "win32" else "the system voice"
ENGINE_NAMES = {"elevenlabs": "ElevenLabs", "kokoro": "the free Kokoro voice", "kitten": "the free Kitten voice",
                "windows": BACKUP}
KITTEN_PACE = 1.1  # Kitten speaks more slowly than Kokoro at the same setting; this evens them out


# Windows' primary language ids (the low bits of a voice's LCID) for Doty's languages, to pick a voice that speaks it.
LANG_IDS = {"en": 0x09, "es": 0x0A, "pt": 0x16, "ru": 0x19, "zh": 0x04, "fr": 0x0C, "de": 0x07, "pl": 0x15,
            "uk": 0x22, "tr": 0x1F, "ko": 0x12, "ja": 0x11, "th": 0x1E, "vi": 0x2A, "id": 0x21, "fil": 0x64}


def _speaks(token, language: str) -> bool:
    """A Windows voice token speaks this language (its Language attribute: hex LCIDs like "409;9")."""
    want = LANG_IDS.get(language)
    try:
        langs = str(token.GetAttribute("Language") or "")
    except Exception:
        return False
    for part in langs.split(";"):
        try:
            if want is not None and int(part, 16) & 0x3FF == want:
                return True
        except ValueError:
            continue
    return False


def _onecore_voices() -> list:
    """Windows' newer speech voices: the ones a language's speech pack installs (Settings > Time & language >
    Speech). `SpVoice.GetVoices()` lists only the older desktop voices, and several languages (Indonesian,
    Thai, Vietnamese, Turkish) have no desktop voice at all; SpVoice speaks with these all the same."""
    import win32com.client

    try:
        cat = win32com.client.Dispatch("SAPI.SpObjectTokenCategory")
        cat.SetId("HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices", False)
        tokens = cat.EnumerateTokens()
        return [tokens.Item(i) for i in range(tokens.Count)]
    except Exception:
        return []


class SapiSpeaker:
    """The built-in Windows voice. Create and use it on the voice thread (COM)."""

    def __init__(self, cfg: VoiceConfig, language: str = "en") -> None:
        import win32com.client

        self.sp = sp = win32com.client.Dispatch("SAPI.SpVoice")
        sp.Rate = cfg.rate
        picked = False
        english = language in ("en", "fil")  # (no Windows voice is Filipino: an English one reads it, as on Linux)
        if cfg.voice:
            for v in sp.GetVoices():
                if cfg.voice.lower() in v.GetDescription().lower() and (english or _speaks(v, language)):
                    sp.Voice = v
                    picked = True
                    break
        if not picked and not english:  # a voice that speaks the player's language, if Windows has one
            for v in [*sp.GetVoices(), *_onecore_voices()]:
                if _speaks(v, language):
                    sp.Voice = v
                    picked = True
                    break
        # No voice for the language: an English voice reading it is noise (or, for another script, silence),
        # so Doty stays quiet and says why.
        self.speaks_language = picked or english
        # The first Speak() opens the audio device (~100 ms); pay that now, silently, so the
        # first real alert starts in ~10 ms.
        sp.Volume = 0
        sp.Speak("ready", 0)
        sp.Volume = cfg.volume

    def speak(self, text: str, purge: bool) -> None:
        self.sp.Speak(text, SVSF_ASYNC | (SVSF_PURGE if purge else 0))

    def purge(self) -> None:
        self.sp.Speak("", SVSF_ASYNC | SVSF_PURGE)

    def wait(self, interrupted: Callable[[], bool]) -> None:
        import pythoncom

        # Pump COM messages while speaking so purge requests aren't starved.
        while not self.sp.WaitUntilDone(25):
            if interrupted():
                break
            pythoncom.PumpWaitingMessages()

    def close(self) -> None:
        self.sp = None


class SystemSpeaker:
    """Linux's own speech, the backup voice there (SAPI's place on Windows): eSpeak NG, or speech-dispatcher's
    spd-say. Each line is a short-lived process, so cutting a line off ends it. Use it on the voice thread."""

    # eSpeak NG's names where they differ from Doty's language codes.
    ESPEAK_LANGS = {"zh": "cmn", "fil": "en"}

    def __init__(self, cfg: VoiceConfig, language: str = "en", which=shutil.which, popen=subprocess.Popen) -> None:
        self.cfg, self.language, self._popen = cfg, language or "en", popen
        self._proc = None
        espeak = which("espeak-ng") or which("espeak")
        spd = which("spd-say")
        if espeak:
            self.kind, self.exe = "espeak", espeak
        elif spd:
            self.kind, self.exe = "spd", spd
        else:
            raise RuntimeError("no system voice: install espeak-ng or speech-dispatcher")

    def command(self, text: str) -> list[str]:
        rate = max(-10, min(10, int(self.cfg.rate)))
        volume = max(0, min(100, int(self.cfg.volume)))
        if self.kind == "espeak":
            lang = self.ESPEAK_LANGS.get(self.language, self.language)
            return [self.exe, "-s", str(175 + rate * 12), "-a", str(volume), "-v", lang, "--", text]
        # spd-say: rate and volume run -100..100; --wait keeps the process alive until the line is said.
        return [self.exe, "--wait", "-r", str(rate * 10), "-i", str(volume * 2 - 100), "-l", self.language,
                "--", text]

    def speak(self, text: str, purge: bool) -> None:
        if purge:
            self.purge()
        self._proc = self._popen(self.command(text), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)

    def purge(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def wait(self, interrupted: Callable[[], bool]) -> None:
        while self._proc is not None and self._proc.poll() is None:
            if interrupted():
                break
            time.sleep(0.025)

    def close(self) -> None:
        self.purge()


def kokoro_voice(cfg: VoiceConfig, language: str = "en") -> str:
    """Kokoro's voice for the app's language: your English pick, or (Spanish, Portuguese, French) your pick
    among that language's voices."""
    if language in kokoro_files.LANG_VOICES:
        v = getattr(cfg, "kokoro_lang_voice", "")
        return v if v in kokoro_files.LANG_VOICES[language] else kokoro_files.default_voice(language)
    v = getattr(cfg, "kokoro_voice", "") or KOKORO_DEFAULT
    return v if v in KOKORO_VOICES else KOKORO_DEFAULT


def uses_kitten(cfg: VoiceConfig) -> bool:
    """The free local voice is Kitten (chosen in Settings) rather than Kokoro."""
    return getattr(cfg, "engine", "auto") == "kitten"


def local_voice(cfg: VoiceConfig, language: str = "en") -> str:
    """The voice id for whichever free local engine is in use."""
    if uses_kitten(cfg):
        from . import kitten

        v = getattr(cfg, "kitten_voice", "") or kitten.DEFAULT_VOICE
        return v if v in kitten.VOICES else kitten.DEFAULT_VOICE
    return kokoro_voice(cfg, language)


def nothing_speaks(language: str, why: str = "", eleven_model: str = "") -> str:
    """The status line when no voice on this PC has the app's language: why the voice you'd expect isn't
    speaking (if there's a reason), then what to add."""
    name = languages.english_name(language)
    fix = f"add {name} speech in Windows' settings (Time & language > Speech)"
    if not api_key() and languages.eleven_speaks(language, eleven_model):
        fix += ", or an ElevenLabs key"
    return (f"{why}; " if why else "") + f"no voice on this PC speaks {name}: {fix}"


def local_speaks(cfg: VoiceConfig, language: str) -> bool:
    """The free local engine in use has a voice for this language."""
    return languages.kitten_speaks(language) if uses_kitten(cfg) else kokoro_files.speaks(language)


def _local_engine(cfg: VoiceConfig):
    if uses_kitten(cfg):
        from .kitten import Kitten

        return Kitten()
    return Kokoro(device=getattr(cfg, "kokoro_device", "auto"))


# Urgent calls ("Back off, low HP", "Glimmer yourself") are said this much faster: pace is what tells the ear
# a line is urgent (more than pitch or loudness), and they're over sooner.
URGENT_PACE = 1.12


class Voice:
    def __init__(self, cfg: VoiceConfig, *, eleven_url: str | None = None,
                 player_factory: Callable[[], PcmPlayer] = PcmPlayer,
                 sapi_factory: Callable[[VoiceConfig], SapiSpeaker] | None = None,
                 cache: PhraseCache | None = None,
                 kokoro_factory: Callable[[], Kokoro] | None = None) -> None:
        self.cfg = cfg
        self.muted = False
        self.available = sys.platform == "win32" or sys.platform.startswith("linux")
        self.last_speak_ms: float | None = None  # request -> first audio
        # What's speaking ("elevenlabs" / "kokoro" / "windows"), which voice, and why the voice
        # you chose isn't the one speaking, if it isn't (shown in the app).
        self.engine = "windows"
        self.voice_name = ""
        self.device = ""  # Kokoro: "GPU" or "CPU"
        self.problem = ""
        # (text, cut off what's playing, deadline: monotonic time after which it's not worth starting, or None)
        self._q: queue.Queue[tuple[str, bool, float | None] | None] = queue.Queue()
        self.dropped_late = 0  # lines dropped because they'd have started too late
        self._critical_t = -1e9  # when a critical line was last asked for
        self._hush = threading.Event()
        self._thread: threading.Thread | None = None
        self._eleven_url = eleven_url
        # The backup voice: SAPI on Windows, eSpeak NG / speech-dispatcher on Linux.
        self._player_factory = player_factory
        self._sapi_factory = sapi_factory or (SapiSpeaker if sys.platform == "win32" else SystemSpeaker)
        # The free local engine: Kokoro, or Kitten when chosen (same shape, so the rest doesn't care).
        self._kokoro_factory = kokoro_factory or (lambda: _local_engine(cfg))
        self._cache = cache
        self._client: ElevenLabs | None = None
        self._kokoro: Kokoro | None = None
        self._player: PcmPlayer | None = None
        self._sapi: SapiSpeaker | None = None
        self.voice_id = ""
        self._failures = 0
        self._pause_until = 0.0
        self._kokoro_failures = 0
        self._pending = 0  # lines queued or being spoken
        self._pending_lock = threading.Lock()
        self._prepare: list[str] = []  # lines to make ahead of time (Kokoro)
        self._warm: list[str] = []
        self.prepared = 0
        self.ready = threading.Event()  # the voice thread has picked its engine
        self.urgent: frozenset[str] = frozenset()  # lines said faster (URGENT_PACE)
        self.calm = False  # no line cuts another off (the demo, sped up: lines would trample each other)
        # The app's language. Kokoro speaks English, Spanish, Portuguese and French; Kitten English; the rest
        # is said by ElevenLabs or the system's own voice (languages.py).
        self.language = "en"
        self._why_not_local = ""  # why the free local voice isn't speaking, when it isn't

    # public ------------------------------------------------------------------------------
    def wants_eleven(self) -> bool:
        engine = getattr(self.cfg, "engine", "auto")
        return engine == "elevenlabs" or (engine == "auto" and bool(api_key()))

    def prepare(self, lines: Iterable[str]) -> None:
        """Lines Doty says often. With Kokoro they're made in idle moments, then play instantly."""
        self._prepare = [s for s in dict.fromkeys(lines) if s and len(s) <= CACHE_MAX_CHARS]

    def prepare_now(self, lines: Iterable[str]) -> bool:
        """Thread-safe: lines that may be said any second (the coach's prepared calls). With a local voice
        they're made next, in the gaps between lines, so they play the instant they're needed. False when
        there's no local voice running to make them (ask again later)."""
        if self._thread is None or self._kokoro is None:
            return False
        new = [s for s in dict.fromkeys(lines) if s and len(s) <= CACHE_MAX_CHARS and s not in self._warm]
        if new:
            self._warm[:0] = new  # ahead of the start-up lines still waiting
            self._q.put(WAKE)
        return True

    def start(self) -> None:
        if not self.cfg.enabled or not self.available:
            return
        self._thread = threading.Thread(target=self._run, name="doty-voice", daemon=True)
        self._thread.start()

    def stop(self, join: float = 0.0) -> None:
        if self._thread:
            self._drop_queued()
            self._q.put(None)
            if join:
                self._thread.join(join)

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self.interrupt()
        return self.muted

    def interrupt(self) -> None:
        """Stop talking now (the player is about to speak, or Doty was muted)."""
        if self._thread:
            self._drop_queued()
            self._put(("", True, None))

    CRITICAL_HOLD_S = 4.0  # a cut-in line never cuts off a critical one asked for this recently

    def on_advice(self, adv: Advice) -> None:
        if not adv.speak or not adv.final or adv.priority > self.cfg.min_priority:
            return
        critical = adv.priority == Priority.CRITICAL
        if critical:
            self._critical_t = time.monotonic()
        # A prepared call cuts in over coach tips and timers, but waits its (short) turn behind "Back off,
        # low HP": its deadline drops it if that takes too long.
        cut_in = adv.cut_in and time.monotonic() - self._critical_t > self.CRITICAL_HOLD_S
        self.say(adv.spoken, interrupt=(critical or cut_in) and not self.calm, stale_s=adv.stale_s)

    def say(self, text: str, interrupt: bool = False, stale_s: float | None = None) -> None:
        """Queue a line. `stale_s`: drop it if it can't start playing within that many seconds. An interrupting
        line cuts off what's playing and drops what's queued about a moment (lines with a deadline); the rest
        of an answer you asked for still comes, after it."""
        if self._thread and not self.muted:
            kept = self._drop_queued(keep_unhurried=True) if interrupt else []
            self._put((text, interrupt, None if stale_s is None else time.monotonic() + stale_s))
            for item in kept:
                self._put(item)

    def idle(self) -> bool:
        return self._pending == 0 and (self._player is None or self._player.pending_s == 0)

    def _put(self, item: tuple[str, bool, float | None]) -> None:
        with self._pending_lock:
            self._pending += 1
        self._q.put(item)

    def _done(self, n: int = 1) -> None:
        with self._pending_lock:
            self._pending = max(0, self._pending - n)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready.is_set() and self.idle():
                return True
            time.sleep(0.02)
        return False

    # voice thread ---------------------------------------------------------------------------
    def _drop_queued(self, keep_unhurried: bool = False) -> list:
        """Forget queued lines and cut off the current one (keeps a pending stop). With `keep_unhurried`, the
        lines without a deadline (an answer's later sentences) are returned instead, to be queued again."""
        self._hush.set()
        stopping, dropped, kept = False, 0, []
        try:
            while True:
                item = self._q.get_nowait()
                if item is None:
                    stopping = True
                elif item is not WAKE:
                    dropped += 1
                    if keep_unhurried and item[0] and item[2] is None:
                        kept.append((item[0], False, None))
        except queue.Empty:
            pass
        self._done(dropped)
        if stopping:
            self._q.put(None)
        return kept

    def _line_waiting(self) -> bool:
        """A line (or a stop) is queued: not just a wake-up to prepare lines."""
        return any(item is not WAKE for item in tuple(self._q.queue))

    def _run(self) -> None:
        com = sys.platform == "win32"
        if com:
            import pythoncom

            pythoncom.CoInitialize()
        try:
            try:
                try:
                    self._sapi = self._sapi_factory(self.cfg, self.language)
                except TypeError:  # (a factory that takes only the settings)
                    self._sapi = self._sapi_factory(self.cfg)
            except Exception as exc:
                print(f"[doty] {BACKUP} unavailable: {exc!r}")
            try:
                self._setup()
            except Exception as exc:  # whatever went wrong, Doty still talks (with the Windows voice)
                print(f"[doty] voice setup failed: {exc!r}")
                self.engine, self.voice_name = "windows", ""
                self.problem = f"voice setup failed ({exc.__class__.__name__}); using {BACKUP}"
            self._note_if_nothing_speaks()  # (known now, not at the first alert of a match)
            self.ready.set()
            while True:
                if self._warm:
                    try:
                        item = self._q.get(timeout=0.1)
                    except queue.Empty:
                        self._warm_one()
                        continue
                else:
                    item = self._q.get()
                if item is None:
                    break
                if item is WAKE:
                    continue  # new lines to prepare: the loop above makes them while nothing is queued
                text, interrupt, deadline = item
                try:
                    if interrupt or not text:
                        self._hush.clear()
                        if self._player is not None:
                            self._player.clear()
                        if self._sapi is not None:
                            self._sapi.purge()
                    if text and self._too_late(deadline):
                        self.dropped_late += 1
                        print(f"[doty] voice: skipped a line that would have started too late: {text[:60]!r}")
                    elif text:
                        self._speak(text, interrupt)
                except Exception as exc:  # never let one bad line kill the voice
                    print(f"[doty] voice error: {exc!r}")
                finally:
                    self._done()
        finally:
            self.ready.set()
            if self._player is not None:
                self._player.close()
            if self._client is not None:
                self._client.close()
            if self._kokoro is not None:
                self._kokoro.close()
            if self._sapi is not None:
                self._sapi.close()
                self._sapi = None
            if com:
                pythoncom.CoUninitialize()

    def _setup(self) -> None:
        self.engine, self.voice_name, self.problem = "windows", "", ""
        engine = getattr(self.cfg, "engine", "auto")
        model = getattr(self.cfg, "eleven_model", DEFAULT_MODEL) or DEFAULT_MODEL
        self._why_not_local = ""
        if self.wants_eleven():
            if languages.eleven_speaks(self.language, model):
                reason = self._setup_eleven()
            else:
                reason = f"ElevenLabs' {model} doesn't speak {languages.english_name(self.language)}"
            if reason:
                self._fall_back(reason)
        elif engine in ("kokoro", "kitten", "auto"):
            reason = self._why_not_local = self._setup_kokoro()
            if reason and engine in ("kokoro", "kitten"):  # in auto, no download yet just means Windows
                # The voice you chose doesn't have your language, and there's an ElevenLabs key: it speaks.
                if (not local_speaks(self.cfg, self.language or "en") and api_key()
                        and languages.eleven_speaks(self.language, model) and not self._setup_eleven()):
                    self.problem = f"{reason}; using ElevenLabs"
                else:
                    self.problem = f"{reason}; using {BACKUP}"

    def _note_if_nothing_speaks(self) -> None:
        """The system's voice is all that's left and it has no voice for the app's language: say so (keeping
        the reason the voice you'd expect isn't speaking), instead of a status that reads as fine."""
        if (self.engine != "windows" or self._sapi is None or getattr(self._sapi, "speaks_language", True)
                or "no voice on this PC" in self.problem):
            return
        why = self.problem.rsplit("; using", 1)[0] if self.problem else self._why_not_local
        self.problem = nothing_speaks(self.language or "en", why, getattr(self.cfg, "eleven_model", ""))

    def _fall_back(self, reason: str) -> None:
        """ElevenLabs can't be used: Kokoro if it's downloaded, else the Windows voice."""
        self.engine, self.voice_name = "windows", ""
        if self._kokoro is None:
            self._setup_kokoro()
        self.problem = f"{reason}; using {ENGINE_NAMES[self.engine]}"
        print(f"[doty] voice: {self.problem}")

    def _use_player(self) -> None:
        if self._player is None:
            self._player = self._player_factory()
        if self._cache is None:
            self._cache = PhraseCache()
            try:
                gone = self._cache.prune()
            except OSError:
                gone = 0
            if gone:
                print(f"[doty] voice: cleared {gone} old saved lines")

    def _setup_eleven(self) -> str:
        """Returns why ElevenLabs can't be used, or ""."""
        key = api_key()
        if not key:
            return "no ElevenLabs key yet"
        client = ElevenLabs(key, base_url=self._eleven_url)
        chosen = getattr(self.cfg, "eleven_voice", "")
        try:
            voices = client.voices()
        except ElevenError as exc:
            if exc.fatal:
                client.close()
                return str(exc)
            voices = []  # offline for now, or a key that can't list voices: still try to speak
        if chosen:
            self.voice_id = chosen
            self.voice_name = next((v["name"] for v in voices if v["id"] == chosen), "")
        else:
            self.voice_id, self.voice_name = pick_voice(voices) if voices else FALLBACK_VOICE
        client.connect()
        self._client = client
        self._use_player()
        self.engine = "elevenlabs"
        return ""

    def _setup_kokoro(self) -> str:
        """Returns why the free local voice (Kokoro, or Kitten) can't be used, or ""."""
        kitten = uses_kitten(self.cfg)
        name = "Kitten" if kitten else "Kokoro"
        language = self.language or "en"
        if not local_speaks(self.cfg, language):  # (not loaded at all: it would only take memory)
            other = " (Kokoro does: Settings > Voice)" if kitten and kokoro_files.speaks(language) else ""
            return f"{name} doesn't speak {languages.english_name(language)}{other}"
        try:
            k = self._kokoro_factory()
            if not k.installed():
                return f"the {name} voice isn't downloaded yet (Settings > Voice)"
            has_language = getattr(k, "has_language", None)
            if language != "en" and has_language is not None and not has_language(language):
                return (f"Kokoro's {languages.english_name(language)} voices aren't downloaded yet "
                        "(Settings > Voice)")
            k.load()
        except KokoroError as exc:
            return str(exc)
        except Exception as exc:  # a broken model file, a missing runtime
            return f"{name} couldn't start ({exc.__class__.__name__})"
        self._kokoro = k
        self._use_player()
        if kitten:
            from .kitten import voice_label as kitten_label

            self.engine, self.voice_name = "kitten", kitten_label(local_voice(self.cfg))
        else:
            self.engine, self.voice_name = "kokoro", voice_label(kokoro_voice(self.cfg, self.language))
        self.device = getattr(k, "device", "")
        self._warm = list(self._prepare)
        return ""

    def _too_late(self, deadline: float | None) -> bool:
        """The line would start after its deadline: the audio already waiting to play (Kokoro and
        ElevenLabs queue it in the player) plays first."""
        if deadline is None:
            return False
        waiting = self._player.pending_s if self._player is not None else 0.0
        return time.monotonic() + waiting > deadline

    def _eleven_ready(self) -> bool:
        return self._client is not None and time.monotonic() >= self._pause_until

    def _speak(self, text: str, interrupt: bool) -> None:
        t0 = time.perf_counter()
        if self._eleven_ready():
            try:
                self._speak_eleven(text, t0)
                self._failures = 0
                if self.engine == "elevenlabs":
                    self.problem = ""
                return
            except ElevenError as exc:
                self._eleven_failed(exc)
        if self._kokoro is not None and local_speaks(self.cfg, self.language or "en"):
            try:
                self._speak_kokoro(text, t0)
                self._kokoro_failures = 0
                return
            except Exception as exc:
                self._kokoro_failed(exc)
        if self._sapi is None:
            return
        if not getattr(self._sapi, "speaks_language", True):
            self._note_if_nothing_speaks()  # (a reason already shown stays: a failed line, a missing download)
            return
        if self._player is not None:  # let any audio already playing finish first
            self._player.wait(self._hush.is_set)
        self._sapi.speak(text, purge=interrupt)
        self.last_speak_ms = (time.perf_counter() - t0) * 1000.0
        # Until it's done, unless a line cuts in: SAPI keeps its own queue, so moving on early would put the
        # next line behind this one, past the deadline check (which only sees audio Doty's player holds).
        self._sapi.wait(self._hush.is_set)

    def _play(self, chunks, key: str, cacheable: bool, t0: float) -> int | None:
        """Stream PCM chunks into the player and cache short lines. Returns the bytes played, or
        None if the line was cut off."""
        volume = self.cfg.volume / 100.0
        got = bytearray()
        try:
            for chunk in chunks:
                if self._hush.is_set():
                    return None
                if not got:
                    self.last_speak_ms = (time.perf_counter() - t0) * 1000.0
                self._player.write(chunk, volume)
                got += chunk
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
        if self._hush.is_set():
            return None
        if got and cacheable:
            self._cache.put(key, bytes(got))
        return len(got)

    def _cached(self, key: str, cacheable: bool, t0: float) -> bool:
        pcm = self._cache.get(key) if cacheable else None
        if pcm is None:
            return False
        self._player.write(pcm, self.cfg.volume / 100.0)
        self.last_speak_ms = (time.perf_counter() - t0) * 1000.0
        return True

    def _speak_eleven(self, text: str, t0: float) -> None:
        model = getattr(self.cfg, "eleven_model", DEFAULT_MODEL) or DEFAULT_MODEL
        speed = speed_for_rate(self.cfg.rate)
        if text in self.urgent:
            speed = round(min(1.2, speed * URGENT_PACE), 2)  # (ElevenLabs' fastest is 1.2)
        key = self._cache.key(self.voice_id, model, speed, text)
        cacheable = len(text) <= CACHE_MAX_CHARS
        if self._cached(key, cacheable, t0):
            return
        chunks = self._client.stream(text, self.voice_id, model=model, speed=speed, stop=self._hush.is_set)
        if self._play(chunks, key, cacheable, t0) == 0:
            raise ElevenError("ElevenLabs sent no audio")

    def _kokoro_tone(self) -> str:
        tone = getattr(self.cfg, "kokoro_tone", "warm")
        return tone if tone in ("warm", "clear", "bright") else "warm"

    def _kokoro_key(self, text: str) -> tuple[str, str, float]:
        voice, speed = local_voice(self.cfg, self.language or "en"), speed_for_rate(self.cfg.rate)
        if uses_kitten(self.cfg):
            speed *= KITTEN_PACE
        if text in self.urgent:
            speed *= URGENT_PACE
        # The tone (and the filter's version) is part of the key: lines saved before a tone or filter
        # change must not play back.
        tone = self._kokoro_tone()
        if tone != "bright":
            tone += f"-f{KOKORO_FILTER_VERSION}"
        return self._cache.key(f"{voice}+{tone}", self._kokoro.model_id, speed, text), voice, speed

    def _speak_kokoro(self, text: str, t0: float) -> None:
        key, voice, speed = self._kokoro_key(text)
        cacheable = len(text) <= CACHE_MAX_CHARS
        if self._cached(key, cacheable, t0):
            return
        self._play(self._kokoro.synth(text, voice, speed, stop=self._hush.is_set, tone=self._kokoro_tone()), key,
                   cacheable, t0)
        if self.engine == "kokoro" and self.problem.endswith("filling in"):
            self.problem = ""

    def _warm_one(self) -> None:
        """Make one prepared line ahead of time, unless a real line is waiting."""
        text = self._warm.pop(0)
        if self._kokoro is None:
            self._warm.clear()
            return
        try:
            key, voice, speed = self._kokoro_key(text)
            if self._cache.get(key) is None:
                pcm = b"".join(self._kokoro.synth(text, voice, speed, stop=self._line_waiting,
                                                  tone=self._kokoro_tone()))
                if self._line_waiting():  # a real line came in mid-way: make this one again afterwards
                    self._warm.insert(0, text)
                    return
                if pcm:
                    self._cache.put(key, pcm)
            self.prepared += 1
        except Exception as exc:
            print(f"[doty] voice: couldn't prepare {text!r}: {exc!r}")
            self._warm.clear()

    def _eleven_failed(self, exc: ElevenError) -> None:
        if exc.fatal:
            self._client.close()
            self._client = None
            self._fall_back(str(exc))
        else:
            self._failures += 1
            self.problem = f"{exc}; {BACKUP} is filling in"
            if self._failures >= 3:  # stop hammering a dead connection for a bit
                self._pause_until = time.monotonic() + 120
                self._failures = 0
            print(f"[doty] voice: {self.problem}")

    def _kokoro_failed(self, exc: Exception) -> None:
        self._kokoro_failures += 1
        msg = str(exc) if isinstance(exc, KokoroError) else f"Kokoro error ({exc.__class__.__name__})"
        if self._kokoro_failures >= 3:
            self._kokoro.close()
            self._kokoro = None
            self._warm.clear()
            self.engine, self.voice_name, self.device = "windows", "", ""
            self.problem = f"{msg}; using {BACKUP}"
        else:
            self.problem = f"{msg}; {BACKUP} is filling in"
        print(f"[doty] voice: {self.problem}")


def preview(cfg: VoiceConfig, text: str = SAMPLE_LINE, timeout: float = 25.0, language: str = "en") -> Voice:
    """Say one line with these settings and wait for it to finish (Settings > Play sample). `language`:
    the app's language, which picks the voice (`text` is already in it)."""
    import copy

    cfg = copy.deepcopy(cfg)
    cfg.enabled = True
    v = Voice(cfg)
    v.language = language or "en"
    v.start()
    v.ready.wait(15)
    v.say(text)
    v.wait_idle(timeout)
    v.stop(join=3)
    return v


def system_languages() -> set[str]:
    """Doty's languages the system's own voice can speak: on Windows the ones an installed speech voice
    has (more come with a language's speech pack, in Windows' own settings); on Linux, eSpeak NG has all."""
    if sys.platform != "win32":
        has = shutil.which("espeak-ng") or shutil.which("espeak") or shutil.which("spd-say")
        return set(LANG_IDS) if has else set()
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        voices = sp.GetVoices()
        tokens = [voices.Item(i) for i in range(voices.Count)] + _onecore_voices()
        found = {code for code in LANG_IDS if any(_speaks(t, code) for t in tokens)}
        if "en" in found:
            found.add("fil")  # (no Windows voice is Filipino: the English one reads it, as on Linux)
        del tokens, voices, sp
        return found
    except Exception:
        return set()
    finally:
        pythoncom.CoUninitialize()


def list_voices() -> list[str]:
    if sys.platform != "win32":
        return []
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        voices = sp.GetVoices()
        names = [voices.Item(i).GetDescription() for i in range(voices.Count)]
        del voices, sp
        return names
    finally:
        pythoncom.CoUninitialize()
