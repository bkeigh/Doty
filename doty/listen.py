"""Push-to-talk questions (the Clicky idea, tuned for a game).

Hold the key -> Doty goes quiet, beeps, records the mic and grabs a screenshot
of the moment you started asking. Release -> beep, local Whisper transcribes
(~0.1-0.5 s), and the question goes to the coach, whose answer streams back
into the overlay and your ear.

Audio never leaves the machine: only the transcript goes to Claude.
"""

from __future__ import annotations

import math
import struct
import sys
import threading
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .advice import Advice, AdviceBus, Priority
from .config import CACHE_DIR, ListenConfig
from .holdkey import HoldKey

if TYPE_CHECKING:
    import numpy as np

    from .coach import Coach
    from .voice import Voice

RATE = 16000
# The sound system the mics are listed from: MME on Windows (it resamples to 16 kHz for us; WASAPI shared mode
# would not). Linux: ALSA, whose "default" device goes through PulseAudio or PipeWire and resamples too.
HOSTAPI = "MME" if sys.platform == "win32" else "ALSA"
# Whisper's initial prompt biases it toward Dota vocabulary it would otherwise mishear.
DOTA_PROMPT = (
    "Dota 2 question. Roshan, Aegis, Tormentor, BKB, Black King Bar, Blink Dagger, Aghanim's Scepter, "
    "Shard, Battle Fury, Manta, Midas, Linken's, Eul's, Force Staff, TP, buyback, smoke, wards, "
    "stack, pull, jungle, safe lane, offlane, mid, carry, support, gank, high ground, tier two, "
    "barracks, power rune, bounty rune, lotus, Shrine of Wisdom, Madstone, neutral item."
)
HALLUCINATIONS = {"", "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.", "bye."}


NOT_A_MIC = ("hdmi", "capture", "elgato", "cam link", "stereo mix", "line in", "line-in", "what u hear",
             "wave out", "loopback", "sound mapper", "primary sound")
FALLBACK_MICS = ("webcam", "c920", "c922", "brio", "nvidia broadcast", "virtual", "voicemeeter", "cable output")


def _mic_rank(name: str) -> int:
    """0 = a real mic, 1 = usable but second-best (webcams, virtual mics that need
    their app running), 2 = not a mic at all."""
    n = name.lower()
    if any(k in n for k in NOT_A_MIC):
        return 2
    if any(k in n for k in FALLBACK_MICS):
        return 1
    return 0 if any(k in n for k in ("mic", "headset", "microphone")) else 1


def list_mics() -> list[str]:
    """Input devices that look like microphones, best first (capture cards and
    stereo mix left out)."""
    import sounddevice as sd

    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == HOSTAPI), None)
    names = {d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0 and d["hostapi"] == mme}
    return sorted((n for n in names if _mic_rank(n) < 2), key=lambda n: (_mic_rank(n), n.lower()))


class Recorder:
    """16 kHz mono int16 from a mic, opened only while the key is held."""

    def __init__(self, mic: str = "") -> None:
        import sounddevice as sd

        self.sd = sd
        self.device = self._resolve(mic)
        self._chunks: list = []
        self._stream = None

    def _mme_inputs(self) -> list[tuple[int, str]]:
        # MME resamples to 16 kHz for us; WASAPI shared mode would not. (HOSTAPI: ALSA on Linux.)
        mme = next((i for i, h in enumerate(self.sd.query_hostapis()) if h["name"] == HOSTAPI), None)
        return [(i, d["name"]) for i, d in enumerate(self.sd.query_devices())
                if d["max_input_channels"] > 0 and d["hostapi"] == mme]

    def _resolve(self, mic: str) -> int | None:
        inputs = self._mme_inputs()
        if mic:
            matches = [i for i, name in inputs if mic.lower() in name.lower()]
            if matches:
                return matches[0]
            print(f"[doty] no mic matching {mic!r}; picking one automatically")
        default = self.sd.query_devices(self.sd.default.device[0])["name"]
        if _mic_rank(default) == 0:
            return None  # the Windows default recording device is a real mic
        # The default is a capture card / line-in / stereo mix (seen in the wild:
        # "HDMI (Elgato 4K S)"). Pick the most mic-like device instead.
        ranked = sorted(inputs, key=lambda d: _mic_rank(d[1]))
        if ranked and _mic_rank(ranked[0][1]) < _mic_rank(default):
            return ranked[0][0]
        return None

    @property
    def device_name(self) -> str:
        idx = self.device if self.device is not None else self.sd.default.device[0]
        return self.sd.query_devices(idx)["name"]

    def start(self) -> None:
        self._chunks = []
        self._stream = self.sd.InputStream(samplerate=RATE, channels=1, dtype="int16", device=self.device,
                                           callback=self._on_audio, latency="low")
        self._stream.start()

    def _on_audio(self, indata, _frames, _time, _status) -> None:
        self._chunks.append(indata[:, 0].copy())

    def stop(self) -> "np.ndarray":
        import numpy as np

        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.int16)


class WhisperTranscriber:
    def __init__(self, model: str = "auto", device: str = "auto", language: str = "en") -> None:
        self.model_name = model
        self.device = device
        # English: the English-only models (faster, sharper). Any other language: the multilingual ones.
        self.language = "en" if (language or "en") == "en" else language.split("-")[0]
        self.model = None
        self.desc = "not loaded"

    def load(self) -> None:
        """Blocking (seconds; downloads the model the first time). Run off the hot path."""
        import ctranslate2
        import numpy as np
        from faster_whisper import WhisperModel

        attempts = []
        if self.device in ("auto", "cuda") and ctranslate2.get_cuda_device_count() > 0 and cuda_libs_available():
            attempts.append(("cuda", "float16", "small.en"))
        if self.device in ("auto", "cpu"):
            attempts.append(("cpu", "int8", "base.en"))
        errors = []
        for device, compute, default_name in attempts:
            name = default_name if self.model_name == "auto" else self.model_name
            if self.language != "en" and self.model_name == "auto":
                name = default_name.removesuffix(".en")
            try:
                model = WhisperModel(name, device=device, compute_type=compute)
                # CUDA libraries load on first inference; fail here, not mid-game.
                list(model.transcribe(np.zeros(RATE // 2, dtype=np.float32), language=self.language,
                                      beam_size=1)[0])
            except Exception as exc:
                errors.append(f"{name}/{device}: {exc}")
                continue
            self.model, self.desc = model, f"{name} on {device}"
            return
        raise RuntimeError("couldn't load Whisper: " + "; ".join(errors))

    def transcribe(self, pcm: "np.ndarray", extra_prompt: str = "") -> str:
        import numpy as np

        if self.model is None or pcm.size == 0:
            return ""
        audio = pcm.astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(audio ** 2))) < 0.003:
            return ""  # silence; Whisper would invent "Thank you."
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=1, temperature=0.0, condition_on_previous_text=False,
            without_timestamps=True, initial_prompt=(DOTA_PROMPT + " " + extra_prompt).strip(),
        )
        text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()
        return "" if text.lower() in HALLUCINATIONS else text


def cuda_libs_available() -> bool:
    """Whisper on the GPU needs cuBLAS 12 + cuDNN 9. They come from the optional
    `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels (see README) or a CUDA install."""
    if sys.platform != "win32":
        return True
    import ctypes
    import os
    import sysconfig

    nvidia = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    for bin_dir in nvidia.glob("*/bin"):
        # ctranslate2 loads these lazily with LoadLibrary, which searches PATH.
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
        os.add_dll_directory(str(bin_dir))
    try:
        ctypes.WinDLL("cublas64_12.dll")
        ctypes.WinDLL("cudnn64_9.dll")
    except OSError:
        return False
    return True


def _tone_wav(path: Path, freqs: list[float], ms: int = 70, volume: float = 0.22) -> None:
    rate = 22050
    frames = bytearray()
    for f in freqs:
        n = int(rate * ms / 1000)
        for i in range(n):
            env = min(1.0, i / 120, (n - i) / 120)  # tiny fade to avoid clicks
            frames += struct.pack("<h", int(32767 * volume * env * math.sin(2 * math.pi * f * i / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def linux_player(which=None) -> str | None:
    """Linux: a command that plays a .wav without a window (PipeWire, PulseAudio or ALSA's), or None."""
    import shutil

    which = which or shutil.which
    return next((exe for exe in (which(name) for name in ("pw-play", "paplay", "aplay")) if exe), None)


class Earcons:
    def __init__(self, enabled: bool) -> None:
        self._linux_player = linux_player() if enabled and sys.platform.startswith("linux") else None
        self.enabled = enabled and (sys.platform == "win32" or self._linux_player is not None)
        self.start_wav = CACHE_DIR / "ptt_start.wav"
        self.stop_wav = CACHE_DIR / "ptt_stop.wav"
        if self.enabled:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            if not self.start_wav.exists():
                _tone_wav(self.start_wav, [660, 880])
            if not self.stop_wav.exists():
                _tone_wav(self.stop_wav, [880, 660])

    def play(self, path: Path) -> None:
        if self.enabled and self._linux_player:
            import subprocess

            try:  # in the background, like SND_ASYNC
                subprocess.Popen([self._linux_player, str(path)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        elif self.enabled:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


class VoiceInput:
    def __init__(self, cfg: ListenConfig, bus: AdviceBus, coach: "Coach", voice: "Voice | None" = None,
                 screenshot: Callable[[], tuple[str, float]] | None = None,
                 context_words: Callable[[], str] | None = None,
                 recorder: Recorder | None = None, transcriber: WhisperTranscriber | None = None) -> None:
        self.cfg = cfg
        self.bus = bus
        self.coach = coach
        self.voice = voice
        self.screenshot = screenshot
        self.context_words = context_words or (lambda: "")
        self.recorder = recorder
        self.transcriber = transcriber
        self.earcons = Earcons(cfg.earcons)
        self.status = "off"
        self.ready = threading.Event()
        self.holdkey: HoldKey | None = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="doty-listen")
        self._card: Advice | None = None
        self._shot: Future | None = None
        self._pressed_t = 0.0
        self.last_stt_ms: float | None = None

    def start(self) -> None:
        if not self.cfg.enabled or self.cfg.engine == "off":
            return
        try:
            if self.recorder is None:
                self.recorder = Recorder(self.cfg.mic)
            if self.transcriber is None:
                self.transcriber = WhisperTranscriber(self.cfg.whisper_model, self.cfg.device,
                                                      getattr(self, "language", "en"))
        except ImportError as exc:
            self.status = f"unavailable ({exc.name} not installed)"
            print(f"[doty] push-to-talk {self.status}")
            return
        self.status = "loading speech model"
        threading.Thread(target=self._load, name="doty-whisper-load", daemon=True).start()
        self.holdkey = HoldKey(self.cfg.key, self.on_press, self.on_release)
        self.holdkey.start()

    def _load(self) -> None:
        try:
            if self.transcriber.model is None:
                self.transcriber.load()
        except Exception as exc:
            self.status = "unavailable"
            print(f"[doty] push-to-talk unavailable: {exc}")
            return
        self.status = f"ready ({self.transcriber.desc})"
        self.ready.set()
        print(f"[doty] push-to-talk ready: hold {self.cfg.key} and ask. "
              f"mic: {self.recorder.device_name}; speech model: {self.transcriber.desc}")

    def stop(self) -> None:
        if self.holdkey:
            self.holdkey.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # key events (poller thread) ---------------------------------------------------
    def on_press(self) -> None:
        if not self.ready.is_set():
            self.bus.publish(Advice(f"Speech model {self.status}…", Priority.INFO, "voice", source="player",
                                    speak=False, ttl_s=3))
            return
        self._pressed_t = time.perf_counter()
        if self.voice is not None:
            self.voice.interrupt()  # stop talking over the player
        self.earcons.play(self.earcons.start_wav)
        try:
            self.recorder.start()
        except Exception as exc:
            self._card = None
            self.bus.publish(Advice(f"Mic error: {exc}", Priority.INFO, "voice", source="player", speak=False))
            return
        self._card = Advice("Listening… release to ask", Priority.HIGH, "voice", source="player",
                            final=False, speak=False, ttl_s=self.cfg.max_s + 5)
        self.bus.publish(self._card)
        # Capture the screen as it was when the question started, in parallel with speech.
        self._shot = self._pool.submit(self.screenshot) if self.screenshot else None

    def on_release(self) -> None:
        card = self._card
        if card is None:
            return
        self._card = None
        pcm = self.recorder.stop()
        self.earcons.play(self.earcons.stop_wav)
        held = time.perf_counter() - self._pressed_t
        if held < self.cfg.min_s:
            self._update(card, "Hold the key while you talk", final=True, ttl=2.5)
            return
        pcm = pcm[: int(self.cfg.max_s * RATE)]
        self._update(card, "Transcribing…")
        self._pool.submit(self._finish, pcm, card, self._shot)

    # worker thread ------------------------------------------------------------------
    def _finish(self, pcm, card: Advice, shot_future: Future | None) -> None:
        t0 = time.perf_counter()
        try:
            text = self.transcriber.transcribe(pcm, self.context_words())
        except Exception as exc:
            self._update(card, f"Transcription failed: {exc}", final=True, ttl=4)
            return
        self.last_stt_ms = (time.perf_counter() - t0) * 1000.0
        if not text:
            self._update(card, "Didn't catch that — try again", final=True, ttl=3)
            return
        self._update(card, f"“{text}”", final=True, ttl=12)
        shot = None
        if shot_future is not None:
            try:
                shot = shot_future.result(timeout=2.0)[0]
            except Exception as exc:
                print(f"[doty] question screenshot failed: {exc!r}")
        print(f"[doty] heard {text!r} ({len(pcm) / RATE:.1f}s audio, transcribed in {self.last_stt_ms:.0f} ms)")
        self.coach.ask_question(text, shot)

    def _update(self, card: Advice, text: str, final: bool = False, ttl: float | None = None) -> None:
        card.text, card.final, card.created_t = text, final, time.perf_counter()
        if ttl is not None:
            card.ttl_s = ttl
        self.bus.publish(card)
