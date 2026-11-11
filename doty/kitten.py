"""Kitten: a second free voice that runs on this PC, darker and softer than Kokoro. No account, no key.

- **Model:** Kitten TTS Mini 0.8 by KittenML (Apache 2.0; 80M parameters, a StyleTTS 2 model) as
  ONNX from Hugging Face. Doty downloads it once (~82 MB, checked against SHA-256) into
  %LOCALAPPDATA%\\Doty\\models\\kitten. One small file holds all eight voices.
- **Text to sounds:** the same eSpeak NG as Kokoro (`doty.kokoro.Espeak`), but Kitten takes eSpeak's
  IPA as it comes (US English, with stress marks) through the StyleTTS 2 symbol table, the way
  KittenML's own `kittenml` package does with `phonemizer`. No torch, no phonemizer package.
- **Speed:** ONNX Runtime on the CPU, on about a quarter of the threads (`kokoro.default_threads`).
  On a Ryzen 9 3900X (6 threads): loads in ~2 s; a 2-second line takes ~0.6 s, an 11-word line
  ~0.85 s (Kokoro takes ~0.7 s for the 2-second line there).
- **Sound:** the raw model has far less top end than Kokoro (8-11 kHz sits ~33 dB lower; Kokoro's
  ringing lives there), so "warm" and "clear" tilt it only lightly (`KITTEN_TILT`); the notches for
  a vocoder whistle still apply. Its output also drifts on a slow offset while
  it speaks; `highpass` removes that.
- **Output:** 24 kHz 16-bit mono, like Kokoro and ElevenLabs, so it uses the same player and phrase
  cache, at the same loudness as Kokoro. The model's quiet ~0.2 s tail (KittenML cuts it too) and its
  leading silence are trimmed.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import CACHE_DIR
from .kokoro import (_PUNCT, TONES, Espeak, KokoroError, _close_session, _open_session, clean,
                     default_threads, normalize, split_text, trim)

MODELS_DIR = CACHE_DIR.parent / "models" / "kitten"
FOLDER_ENV = "DOTY_KITTEN_DIR"
RATE = 24000
MODEL_ID = "kitten-mini-0.8"  # part of the phrase-cache key
REPO = "KittenML/kitten-tts-mini-0.8"
REVISION = "c02725660cea441db4c383af69f1f26f5cd00947"  # pinned, so the SHA-256s below hold
SOURCE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"
MODEL = ("kitten_tts_mini_v0_8.onnx", "kitten_tts_mini_v0_8.onnx", 78268016,
         "0f5bbae4fc4800c98dbc544a87ecfa79510de2fb8222db30d12e5bfe9177df91")
STYLES = ("voices.npz", "voices.npz", 3278902,
          "40ad2638952b77b7b2f30127e2608e169fc69dd256b53bd8aaa3409a33193c42")
FILES = [MODEL, STYLES]
DOWNLOAD_MB = round(sum(f[2] for f in FILES) / 1e6)
DEFAULT_VOICE = "jasper"
# id -> (label, the voice's name inside voices.npz). KittenML's names; all US English. The notes come
# from measuring a 12-word line: pitch, and how long each voice takes to say it at speed 1.0.
VOICES: dict[str, tuple[str, str]] = {
    "jasper": ("Jasper: man, the quickest", "expr-voice-2-m"),
    "kiki": ("Kiki: woman, higher and lively", "expr-voice-5-f"),
    "bruno": ("Bruno: man, deep", "expr-voice-3-m"),
    "luna": ("Luna: woman", "expr-voice-3-f"),
    "hugo": ("Hugo: man", "expr-voice-4-m"),
    "rosie": ("Rosie: woman, steady", "expr-voice-4-f"),
    "leo": ("Leo: man, unhurried", "expr-voice-5-m"),
    "bella": ("Bella: woman, slow", "expr-voice-2-f"),
}
MAX_TOKENS = 500  # StyleTTS 2's context is 512, including the start, end and pad tokens
KITTEN_TILT = 0.4  # how much of Kokoro's top-end tilt (kokoro.TONES) Kitten gets
END_TAIL = 5000  # samples KittenML cuts from the end of every line (~0.2 s; quiet, but not speech)
LOUDNESS = {True: 10 ** (-20.5 / 20), False: 10 ** (-19.5 / 20)}  # RMS for warm / bright, as Kokoro plays


class KittenError(KokoroError):
    """A KokoroError, so code that already catches Kokoro's problems catches Kitten's too."""


# --------------------------------------------------------------------------- files


def voice_label(voice: str) -> str:
    return VOICES.get(voice, (voice, ""))[0].split(":")[0]


def models_dir() -> Path:
    return Path(os.environ.get(FOLDER_ENV) or MODELS_DIR)


def missing_files(folder: Path | None = None) -> list[tuple[str, str, int, str]]:
    folder = folder or models_dir()
    return [f for f in FILES if not ((folder / f[1]).is_file() and (folder / f[1]).stat().st_size == f[2])]


def installed(folder: Path | None = None) -> bool:
    return not missing_files(folder)


_download_lock = threading.Lock()


def download(folder: Path | None = None, progress: Callable[[int, int], None] | None = None,
             stop: Callable[[], bool] | None = None, base_url: str = SOURCE_URL) -> None:
    """Fetch whatever is missing, verifying each file's SHA-256. Safe to call from two places at
    once (the second waits, then finds everything there)."""
    folder = folder or models_dir()
    with _download_lock:
        todo = missing_files(folder)
        total, done = sum(f[2] for f in todo), 0
        for src, dest_name, size, sha in todo:
            dest = folder / dest_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = dest.with_name(dest.name + ".part")
            digest = hashlib.sha256()
            req = urllib.request.Request(base_url + src, headers={"User-Agent": "Doty"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp, open(part, "wb") as out:
                    while chunk := resp.read(1 << 20):
                        if stop is not None and stop():
                            raise KittenError("download cancelled")
                        out.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            except OSError as exc:
                part.unlink(missing_ok=True)
                raise KittenError(f"couldn't download the Kitten voice ({exc})") from exc
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            if digest.hexdigest() != sha or part.stat().st_size != size:
                part.unlink(missing_ok=True)
                raise KittenError(f"the downloaded {dest_name} was damaged; try again")
            part.replace(dest)


# --------------------------------------------------------------------------- text -> tokens

# StyleTTS 2's symbol table, exactly as KittenML's TextCleaner builds it (the plain '"' is there three
# times and "'" twice; as in theirs, the last one wins).
_SYMBOLS = (["$"] + list(';:,.!?¡¿—…"«»"" ')
            + list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
            + list("ɑɐɒæɓʙβɔɕçɗɖðʤəɘɚɛɜɝɞɟʄɡɠɢʛɦɧħɥʜɨɪʝɭɬɫɮʟɱɯɰŋɳɲɴøɵɸθœɶʘɹɺɾɻʀʁɽʂʃʈʧʉʊʋⱱʌɣɤʍχʎʏʑʐʒʔʡʕʢǀǁǂǃ"
                   "ˈˌːˑʼʴʰʱʲʷˠˤ˞↓↑→↗↘'̩'ᵻ"))
VOCAB: dict[str, int] = {s: i for i, s in enumerate(_SYMBOLS)}
PAD, END = 0, VOCAB["…"]  # every line is [PAD, ..., "…", PAD], as KittenML feeds it
_WORDS = re.compile(r"\w+|[^\w\s]")  # KittenML's basic_english_tokenize


def _from_espeak(ps: str) -> str:
    ps = re.sub(r"\([a-z-]+\)", "", ps)  # eSpeak's language-switch flags, e.g. "(fr)"
    ps = re.sub(r"(?<=[a-zɹː])(?=hˈʌndɹɪd)", " ", ps)  # "two hundred", not "twohundred"
    return ps.replace("^", "")  # Espeak.phonemes ties multi-letter phonemes with "^"; Kitten doesn't


def phonemize(text: str) -> str:
    """Kitten's phonemes for `text`: eSpeak's US-English IPA with stress, punctuation kept, and every
    word and mark separated by one space (what KittenML's phonemizer + tokenizer produce)."""
    text = normalize(text)
    parts, pos = [], 0
    for m in list(_PUNCT.finditer(text)) + [None]:
        chunk = text[pos:m.start() if m else len(text)]
        if chunk.strip():
            parts.append(_from_espeak(Espeak.phonemes(chunk)))
        if m is not None:
            parts.append(m.group().strip())
            pos = m.end()
    return " ".join(_WORDS.findall(" ".join(parts)))


def tokens(ps: str) -> list[int]:
    """Token ids for a phoneme string, without the start/end tokens. Unknown symbols are dropped."""
    return [VOCAB[c] for c in ps if c in VOCAB]


def _finish(piece: str) -> str:
    """KittenML's ensure_punctuation: a piece that doesn't end in punctuation gets a comma."""
    piece = piece.strip()
    return piece if not piece or piece[-1] in ".!?,;:…" else piece + ","


def _fit(text: str) -> list[tuple[str, str, list[int]]]:
    """(text, phonemes, ids) parts, split at spaces until each fits the model's context."""
    ps = phonemize(text)
    ids = tokens(ps)
    words = text.split()
    if len(ids) <= MAX_TOKENS or len(words) < 2:
        return [(text, ps, ids[:MAX_TOKENS])]
    half = len(words) // 2
    return _fit(" ".join(words[:half])) + _fit(" ".join(words[half:]))


# --------------------------------------------------------------------------- the model


def highpass(audio, cutoff_hz: float = 40.0):
    """Remove the slow offset Kitten's output drifts on while it speaks (about a third of the raw
    signal's energy sits below 20 Hz). Inaudible itself, but it eats headroom and can thump at the
    start and end of a line. A zero-phase 2nd-order Butterworth response applied in the frequency
    domain: -24 dB at 10 Hz, -0.2 dB at 85 Hz (a low male voice). Numpy only; ~1 ms a sentence."""
    import numpy as np

    if audio.size < 64:
        return audio
    n = 1 << int(audio.size + RATE // 5 - 1).bit_length()  # >= 0.2 s of zero padding against wrap-around
    spec = np.fft.rfft(audio, n)
    r = (np.fft.rfftfreq(n, 1 / RATE) / cutoff_hz) ** 2
    return np.fft.irfft(spec * (r / np.sqrt(1 + r * r)), n)[:audio.size].astype(np.float32)


class Kitten:
    """Loads the model once; `synth` yields 16-bit PCM a sentence (or clause) at a time.
    Same shape as `kokoro.Kokoro`, so the voice thread can use either."""

    model_id = MODEL_ID
    rate = RATE

    def __init__(self, folder: Path | None = None, threads: int | None = None, device: str = "cpu") -> None:
        """`device` is accepted so callers can treat Kitten like Kokoro, but Kitten always runs on the CPU:
        Kokoro's GPU helper rewrites Kokoro's own graph, and Kitten is quick enough without it."""
        self.folder = folder or models_dir()
        self.threads = threads or default_threads()
        self.device_pref = "cpu"
        self.device = ""  # "CPU" once loaded
        self._session = None
        self._styles: dict[str, object] = {}

    def installed(self) -> bool:
        return installed(self.folder)

    def load(self) -> None:
        """Blocking: ~2 s. Raises KittenError."""
        import numpy as np

        if self._session is not None:
            return
        if not self.installed():
            raise KittenError("the Kitten voice isn't downloaded yet")
        try:
            with np.load(self.folder / STYLES[1], allow_pickle=False) as npz:
                styles = {name: np.asarray(npz[name], dtype=np.float32) for name in npz.files}
        except (OSError, ValueError) as exc:
            raise KittenError(f"couldn't read the Kitten voices ({exc})") from exc
        if any(name not in styles for _label, name in VOICES.values()):
            raise KittenError("the Kitten voices file is missing a voice")
        self._styles = styles
        Espeak.phonemes("ready")
        try:
            self._session, self.device = _open_session(self.folder / MODEL[1], self.threads, self.device_pref)
        except KokoroError as exc:
            raise KittenError(str(exc).replace("Kokoro", "Kitten")) from exc
        self._run(tokens(phonemize("Ready.")), self._style(DEFAULT_VOICE, 6), 1.0)  # first run is slow; pay it now

    def close(self) -> None:
        if self._session is not None:
            self._session = None
            _close_session(self.folder / MODEL[1], self.threads, self.device_pref)

    def _style(self, voice: str, n_chars: int):
        """The voice's style vector. Like KittenML, picked by the length of the text in characters."""
        if voice not in VOICES:
            raise KittenError(f"no Kitten voice called {voice}")
        styles = self._styles[VOICES[voice][1]]
        i = min(n_chars, len(styles) - 1)
        return styles[i:i + 1]

    def _run(self, ids: list[int], style, speed: float):
        import numpy as np

        out = self._session.run(None, {"input_ids": np.array([[PAD, *ids, END, PAD]], dtype=np.int64),
                                       "style": style, "speed": np.array([speed], dtype=np.float32)})
        audio = out[0].reshape(-1)
        return audio[:-END_TAIL] if audio.size > 2 * END_TAIL else audio

    def synth(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0,
              stop: Callable[[], bool] | None = None, tone: str = "warm") -> Iterator[bytes]:
        """Speech for `text` as 16-bit mono PCM, one sentence or clause per chunk. `tone` "warm" (the
        default) or "clear" cleans the highs a little (`kokoro.clean`); "bright" is the model's raw sound."""
        import numpy as np

        if self._session is None:
            self.load()
        if voice not in VOICES:
            voice = DEFAULT_VOICE
        pieces = split_text(text)
        for i, piece in enumerate(pieces):
            if stop is not None and stop():
                return
            clause = not re.search(r"[.!?…]\s*$", piece) and i < len(pieces) - 1
            for part, _ps, ids in _fit(_finish(normalize(piece))):
                if stop is not None and stop():
                    return
                if not ids:
                    continue
                audio = highpass(self._run(ids, self._style(voice, len(part)), speed))
                audio = trim(audio, tail_s=0.08 if clause else 0.18)
                warm = tone in TONES
                if warm:  # Kitten's top end is far darker than Kokoro's, so tilt it only lightly
                    audio = clean(audio, tone, top_db=TONES[tone] * KITTEN_TILT)
                peak = float(np.abs(audio).max()) if audio.size else 0.0
                if peak <= 0:
                    continue
                # As loud as Kokoro (measured: warm ~-20.5 dBFS RMS, bright ~-19.5). Kitten's waveform is
                # less peaky, so matching Kokoro's peaks instead would play it ~3 dB louder.
                rms = float(np.sqrt(np.mean(audio * audio)))
                gain = min(LOUDNESS[warm] / rms, 0.95 / peak, 4.0)
                yield (np.clip(audio * gain, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
