"""ElevenLabs text-to-speech: a human-sounding voice for Doty.

- Raw 24 kHz PCM comes from the streaming endpoint over one kept-alive HTTPS connection, so
  speech starts as soon as the first chunk lands (the Flash model is ~100 ms, plus the network).
- Playback is a callback-driven buffer, so clearing the buffer silences Doty within ~20 ms
  (push-to-talk, urgent warnings).
- Short lines Doty repeats ("Bounty runes in 15 seconds", "No TP scroll") are cached on disk.
  They replay instantly and don't spend credits again.

API: POST /v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000 and GET /v2/voices,
with the key in the `xi-api-key` header.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from .config import CACHE_DIR

API_URL = "https://api.elevenlabs.io"
KEY_ENV = "ELEVENLABS_API_KEY"
KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"
RATE = 24000  # Hz, 16-bit mono (pcm_24000 works on every plan; 44.1 kHz needs Pro)
DEFAULT_MODEL = "eleven_flash_v2_5"  # ~100 ms, half the per-character price of the full models
# With no voice chosen, Doty picks the first of these on your account. Clear, confident voices.
PREFERRED_VOICES = ("George", "Brian", "Liam", "Will", "Eric", "Chris", "Daniel", "Adam")
# If your key may not list voices: George, the voice in ElevenLabs' own quickstart.
FALLBACK_VOICE = ("JBFqnCBsd6RMkjVDRZzb", "George")
CACHE_MAX_CHARS = 90  # alerts and short calls; coach answers are one-offs
SAMPLE_LINE = "Bounty runes in fifteen seconds. Their carry is missing from the map, so play safe."


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def speed_for_rate(rate: int) -> float:
    """Doty's speaking-speed slider (the Windows voice's -10..10 rate) as an ElevenLabs speed."""
    return round(min(1.2, max(0.7, 1.0 + rate * 0.04)), 2)


class ElevenError(Exception):
    def __init__(self, message: str, status: int = 0, code: str = "", fatal: bool = False) -> None:
        super().__init__(message)
        self.status, self.code = status, code
        self.fatal = fatal  # retrying won't help (bad key, no credits, voice gone)


def _error(status: int, raw: bytes) -> ElevenError:
    detail = None
    try:
        detail = json.loads(raw).get("detail")
    except Exception:
        pass
    code = str(detail.get("status", "")) if isinstance(detail, dict) else ""
    message = str(detail.get("message", "")) if isinstance(detail, dict) else ""
    if code == "missing_permissions":
        return ElevenError("the ElevenLabs key doesn't allow this", status, code)
    if status == 401 or code in ("invalid_api_key", "needs_authorization"):
        return ElevenError("ElevenLabs didn't accept the API key", status, code, fatal=True)
    if status == 402 or code == "quota_exceeded":
        return ElevenError("out of ElevenLabs credits", status, code, fatal=True)
    if code == "voice_not_found" or status == 404:
        return ElevenError("that ElevenLabs voice isn't on your account", status, code, fatal=True)
    if status == 429:
        return ElevenError("ElevenLabs is busy (too many requests)", status, code)
    return ElevenError(f"ElevenLabs error {status}: {message or code or raw[:120]!r}", status, code)


class ElevenLabs:
    """A small client: one kept-alive connection, used from one thread at a time."""

    def __init__(self, key: str, *, base_url: str | None = None, timeout: float = 8.0) -> None:
        self.key = key
        url = urlsplit(base_url or os.environ.get("DOTY_ELEVENLABS_URL") or API_URL)
        self._https = url.scheme == "https"
        self._host, self._port = url.hostname, url.port
        self.timeout = timeout
        self._conn: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def connect(self) -> None:
        """Open the connection ahead of the first line (saves the TLS handshake then)."""
        with self._lock:
            try:
                self._connection().connect()
            except OSError:
                self.close()

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
            self._conn = cls(self._host, self._port, timeout=self.timeout)
        return self._conn

    def _request(self, method: str, path: str, body: dict | None = None) -> http.client.HTTPResponse:
        headers = {"xi-api-key": self.key, "Accept": "*/*", "User-Agent": "Doty"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in (0, 1):  # the server may have closed an idle kept-alive connection
            try:
                conn = self._connection()
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, OSError) as exc:
                self.close()
                if attempt:
                    raise ElevenError(f"couldn't reach ElevenLabs ({exc.__class__.__name__})") from exc
        if resp.status >= 400:
            raw = resp.read()
            raise _error(resp.status, raw)
        return resp

    def voices(self) -> list[dict]:
        """Voices on your account: [{"id", "name", "category", "labels"}]."""
        out: list[dict] = []
        token = None
        with self._lock:
            while True:
                query = {"page_size": 100, **({"next_page_token": token} if token else {})}
                try:  # the body can be cut off (a dropped connection): that's an ElevenLabs error too
                    data = json.loads(self._request("GET", "/v2/voices?" + urlencode(query)).read())
                except (http.client.HTTPException, OSError, ValueError) as exc:
                    self.close()
                    raise ElevenError(f"couldn't list ElevenLabs voices ({exc.__class__.__name__})") from exc
                out += [{"id": v["voice_id"], "name": v.get("name", ""), "category": v.get("category", ""),
                         "labels": v.get("labels") or {}} for v in data.get("voices", [])]
                token = data.get("next_page_token")
                if not data.get("has_more") or not token or len(out) >= 500:
                    return out

    def stream(self, text: str, voice_id: str, *, model: str = DEFAULT_MODEL, speed: float = 1.0,
               stop: Callable[[], bool] | None = None) -> Iterator[bytes]:
        """Speech for `text` as 16-bit mono PCM chunks, as they arrive. `stop()` abandons it."""
        body: dict = {"text": text, "model_id": model}
        if abs(speed - 1.0) > 0.005:
            body["voice_settings"] = {"speed": speed}
        path = f"/v1/text-to-speech/{quote(voice_id, safe='')}/stream?" + urlencode({"output_format": f"pcm_{RATE}"})
        with self._lock:
            resp = self._request("POST", path, body)
            carry, done = b"", False
            try:
                while True:
                    if stop is not None and stop():
                        return
                    chunk = resp.read1(8192)
                    if not chunk:
                        done = True
                        return
                    chunk = carry + chunk
                    cut = len(chunk) & ~1  # never split a sample
                    carry = chunk[cut:]
                    if cut:
                        yield chunk[:cut]
            except (http.client.HTTPException, OSError) as exc:
                raise ElevenError("lost the connection to ElevenLabs") from exc
            finally:
                if not done:  # unread audio left on the connection: start fresh next time
                    self.close()


def pick_voice(voices: list[dict]) -> tuple[str, str]:
    """A good default from your account's voices: (id, name)."""
    by_name = {v["name"].split(" - ")[0].strip().lower(): v for v in voices}
    for name in PREFERRED_VOICES:
        if name.lower() in by_name:
            v = by_name[name.lower()]
            return v["id"], v["name"]
    for category in ("premade", "default", "high_quality", "professional", "cloned", "generated"):
        for v in voices:
            if v["category"] == category:
                return v["id"], v["name"]
    if voices:
        return voices[0]["id"], voices[0]["name"]
    return FALLBACK_VOICE


class PcmPlayer:
    """Plays 16-bit mono PCM as it arrives. The audio callback pulls from a buffer, so `clear()`
    silences Doty within one ~20 ms block."""

    def __init__(self, rate: int = RATE) -> None:
        self.rate = rate
        self._buf = bytearray()
        self._carry = b""
        self._lock = threading.Lock()
        self._stream = None

    MAX_PENDING_S = 60.0  # more queued than this means the device stopped taking audio

    def _open(self) -> None:
        if self._stream is not None and not self._stream.active:
            # The device went away (headset unplugged, Bluetooth dropped) and the stream stopped: start a
            # fresh one on whatever Windows plays to now, rather than queueing into a dead stream forever.
            print("[doty] voice: the audio device stopped; reopening it")
            self.close()
        if self._stream is None:
            import sounddevice as sd

            # latency="low": ~100 ms of device buffering instead of 200 ms (measured, MME).
            self._stream = sd.RawOutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                              blocksize=self.rate // 50, latency="low", callback=self._fill)
            self._stream.start()

    def _fill(self, outdata, frames, time_info, status) -> None:
        n = len(outdata)
        with self._lock:
            take = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[:len(take)] = take
        if len(take) < n:
            outdata[len(take):] = bytes(n - len(take))

    def write(self, pcm: bytes, volume: float = 1.0) -> None:
        pcm = self._carry + pcm
        cut = len(pcm) & ~1  # whole 16-bit samples only
        pcm, self._carry = pcm[:cut], pcm[cut:]
        if not pcm:
            return
        if volume < 0.999:
            import numpy as np

            pcm = (np.frombuffer(pcm, dtype=np.int16) * max(0.0, volume)).astype(np.int16).tobytes()
        self._open()
        with self._lock:
            if len(self._buf) > self.MAX_PENDING_S * self.rate * 2:
                self._buf.clear()  # stuck: drop the backlog instead of growing without end
            self._buf += pcm

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
        self._carry = b""

    def _dead(self) -> bool:
        """The device went away and the stream stopped: what's buffered will never play."""
        return self._stream is not None and not getattr(self._stream, "active", True)

    @property
    def pending_s(self) -> float:
        if self._dead():  # (else every new line would look "too late" behind audio that never plays)
            self.clear()
            return 0.0
        return len(self._buf) / 2 / self.rate

    def wait(self, stop: Callable[[], bool] = lambda: False, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while self._buf and not stop() and not self._dead() and time.monotonic() < end:
            time.sleep(0.02)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


class PhraseCache:
    """PCM for short lines, on disk, keyed by voice, model, speed and text. Every short line is kept,
    the coach's one-off lines too, so `prune` (run when the voice starts) keeps the folder to the
    MAX_FILES / MAX_BYTES lines used most recently: the fixed alerts, which play every game, stay."""

    MAX_FILES = 1500
    MAX_BYTES = 120 << 20  # ~20 minutes of speech at 24 kHz

    def __init__(self, folder: Path = CACHE_DIR / "voice") -> None:
        self.folder = folder
        self._mem: dict[str, bytes] = {}

    def prune(self, max_files: int | None = None, max_bytes: int | None = None) -> int:
        """Delete the least recently used lines past the limits. Returns how many went."""
        max_files = self.MAX_FILES if max_files is None else max_files
        max_bytes = self.MAX_BYTES if max_bytes is None else max_bytes
        files = []
        for path in self.folder.glob("*.pcm"):
            try:
                st = path.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        files.sort(key=lambda f: -f[0])  # most recently used first
        total, removed = 0, 0
        for i, (_mtime, size, path) in enumerate(files):
            total += size
            if i >= max_files or total > max_bytes:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    @staticmethod
    def key(voice_id: str, model: str, speed: float, text: str) -> str:
        norm = " ".join(text.lower().split())
        return hashlib.sha1(f"{voice_id}|{model}|{speed}|{norm}".encode()).hexdigest()

    def get(self, key: str) -> bytes | None:
        if key in self._mem:
            self._remember(key, self._mem[key])
            return self._mem[key]
        path = self.folder / f"{key}.pcm"
        try:
            data = path.read_bytes()
            os.utime(path)  # "used just now", for prune
        except OSError:
            return None
        self._remember(key, data)
        return data

    MAX_IN_MEMORY = 120  # lines kept in RAM (~10 MB); the rest are read from disk when needed

    def _remember(self, key: str, pcm: bytes) -> None:
        self._mem.pop(key, None)
        self._mem[key] = pcm  # most recent last
        while len(self._mem) > self.MAX_IN_MEMORY:
            del self._mem[next(iter(self._mem))]

    def put(self, key: str, pcm: bytes) -> None:
        self._remember(key, pcm)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            part = self.folder / f"{key}.part"
            part.write_bytes(pcm)
            part.replace(self.folder / f"{key}.pcm")  # a line cut off mid-write never replays
        except OSError:
            pass
