"""Kokoro: a free, human-sounding voice that runs on this PC. No account, no key, no limits.

- **Model:** Kokoro-82M v1.0 by hexgrad (Apache 2.0), as the ONNX export from onnx-community on
  Hugging Face. Doty downloads it once (~330 MB, checked against SHA-256) into
  %LOCALAPPDATA%\\Doty\\models\\kokoro.
- **Text to sounds:** eSpeak NG (GPLv3; the `espeakng-loader` wheel ships its DLL and data), called
  through ctypes. Its IPA is then mapped onto the phoneme set Kokoro v1.0 was trained on (the
  mapping misaki, Kokoro's own front end, uses for eSpeak; Apache 2.0).
- **Languages:** English, and Spanish, Brazilian Portuguese and French (`LANG_VOICES`): the languages
  of Doty's whose Kokoro voices need nothing but eSpeak NG. Their voice files (0.5 MB each) are
  fetched when that language is the app's, so an English install downloads nothing extra. Kokoro's
  Chinese and Japanese voices need their own text front ends (misaki[zh], misaki[ja]); not shipped.
- **Speed:** ONNX Runtime on the CPU, on about a quarter of the threads so Dota keeps the rest.
  A Ryzen 9 3900X makes a 2-second line in ~0.5 s. Doty prepares its fixed alerts ahead of time
  (see `Voice.prepare`), so those play instantly.
- **Output:** 24 kHz 16-bit mono, the same as ElevenLabs, so it uses the same player and
  phrase cache. Kokoro pads every line with ~0.3 s of silence; that's trimmed, so speech starts
  sooner.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import threading
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

from .config import CACHE_DIR

MODELS_DIR = CACHE_DIR.parent / "models" / "kokoro"
FOLDER_ENV = "DOTY_KOKORO_DIR"  # tests point this at an empty folder
RATE = 24000
MODEL_ID = "kokoro-v1.0"  # part of the phrase-cache key
REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"  # pinned, so the SHA-256s below hold
SOURCE_URL = f"https://huggingface.co/{REPO}/resolve/{REVISION}/"
MODEL = ("onnx/model.onnx", "model.onnx", 325532232,
         "8fbea51ea711f2af382e88c833d9e288c6dc82ce5e98421ea61c058ce21a34cb")
TOKENIZER = ("tokenizer.json", "tokenizer.json", 3497,
             "77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34")
DEFAULT_VOICE = "af_heart"
# The best-rated English voices (hexgrad's VOICES.md grades), id -> (label, sha256). 0.5 MB each.
VOICES: dict[str, tuple[str, str]] = {
    "af_heart": ("Heart: American woman (the most natural)",
                 "d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b"),
    "af_bella": ("Bella: American woman", "f69d836209b78eb8c66e75e3cda491e26ea838a3674257e9d4e5703cbaf55c8b"),
    "am_michael": ("Michael: American man", "1d1f21dd8da39c30705cd4c75d039d265e9bc4a2a93ed09bc9e1b1225eb95ba1"),
    "am_fenrir": ("Fenrir: American man", "c27989f741f7ee34d273a39d8a595cc0837d35f5ced9a29b7cc162614616df43"),
    "am_puck": ("Puck: American man", "fcf73c989033e9233e0b98713eca600c8c74dcc1614b37009d5450ff4a2274a0"),
    "bf_emma": ("Emma: British woman", "669fe0647f9dd04fcab92f1439a40eeb4c8b4ab1f82e4996fe3d918ce4a63b73"),
    "af_nicole": ("Nicole: American woman, soft and close (good on headphones)",
                  "cd2191ab31b914ed7b318416b0e4440fdf392ddad9106a060819aa600a64f59a"),
    "bm_george": ("George: British man", "c4b235a4c1f2cd3b939fed08b899ce9385638b763f7b73a59616c4fc9bd6c9bc"),
    "bm_fable": ("Fable: British man", "f889083196807b4adb15e9204252165f503b8d33d3982e681c52443c49d798f1"),
}
# Voices for Doty's other languages: language -> {id: (label, sha256)}. hexgrad's VOICES.md has fewer and
# less polished voices here than for English (they were trained on far less speech).
LANG_VOICES: dict[str, dict[str, tuple[str, str]]] = {
    "es": {
        "ef_dora": ("Dora: woman", "f66ec66bd295acb18372e37008533a9a3228483ccd294e7538d5d9294ac9a532"),
        "em_alex": ("Alex: man", "27809e9eafdcbcfff90a3016c697568676531de2a2c39cee29c96c7bd6b83e95"),
        "em_santa": ("Santa: man", "ad43b774e1ca24d05c6161297d8aeb770ac3d29bb95daf516727af5f7d543683"),
    },
    "pt": {
        "pf_dora": ("Dora: woman", "3da7b5b2d91847ebf5646f57631af6ececae3c29a89cd300f06edf9aa6cfe9ee"),
        "pm_alex": ("Alex: man", "0175c753f59c54e7fd5a995bedef0c5ff2fb67e0043dd3dcb2ae74ec2acbeb2a"),
        "pm_santa": ("Santa: man", "8b012db3185778afe2e45a62cbad69db73021774fe68dda634bcc748a982eede"),
    },
    "fr": {
        "ff_siwis": ("Siwis: woman", "a35f5675ad08948e326ae75fd0ea16ba5d0042e4f76b5f3d1df77d0a48c54861"),
    },
}
# eSpeak NG's voice for each (the ones misaki, and so Kokoro's training, used: es, pt-br and fr-fr, which
# eSpeak's own voice list calls "fr").
ESPEAK_NAMES = {"es": "es", "pt": "pt-br", "fr": "fr"}
VOICE_BYTES = 522240  # 510 styles x 256 floats
FILES = [MODEL, TOKENIZER] + [(f"voices/{v}.bin", f"voices/{v}.bin", VOICE_BYTES, sha)
                              for v, (_label, sha) in VOICES.items()]
DOWNLOAD_MB = round(sum(f[2] for f in FILES) / 1e6)
MAX_TOKENS = 500  # the model's context is 512 including the two pad tokens


def default_threads() -> int:
    """About a quarter of the logical CPUs (2 to 6): nearly full speed, and Dota keeps the rest."""
    return max(2, min(6, (os.cpu_count() or 8) // 4))


class KokoroError(Exception):
    pass


# --------------------------------------------------------------------------- files


def speaks(language: str) -> bool:
    """Kokoro has a voice for this language of Doty's."""
    return (language or "en") == "en" or language in LANG_VOICES


def voices_for(language: str = "en") -> dict[str, tuple[str, str]]:
    """The voices to pick from in this language (English's when Kokoro doesn't speak it)."""
    return LANG_VOICES.get(language or "en", VOICES)


def default_voice(language: str = "en") -> str:
    return next(iter(LANG_VOICES[language])) if language in LANG_VOICES else DEFAULT_VOICE


def voice_language(voice: str) -> str:
    """The language a voice speaks ("en" for the English ones and for a name that isn't a voice)."""
    return next((lang for lang, voices in LANG_VOICES.items() if voice in voices), "en")


def voice_label(voice: str) -> str:
    found = VOICES.get(voice) or LANG_VOICES.get(voice_language(voice), {}).get(voice) or (voice, "")
    return found[0].split(":")[0]


def models_dir() -> Path:
    return Path(os.environ.get(FOLDER_ENV) or MODELS_DIR)


def files_for(language: str = "en") -> list[tuple[str, str, int, str]]:
    """What Kokoro needs on this PC to speak a language: the model and the English voices, plus that
    language's own voices."""
    extra = [(f"voices/{v}.bin", f"voices/{v}.bin", VOICE_BYTES, sha)
             for v, (_label, sha) in LANG_VOICES.get(language or "en", {}).items()]
    return FILES + extra


def missing_files(folder: Path | None = None, language: str = "en") -> list[tuple[str, str, int, str]]:
    folder = folder or models_dir()
    return [f for f in files_for(language)
            if not ((folder / f[1]).is_file() and (folder / f[1]).stat().st_size == f[2])]


def installed(folder: Path | None = None, language: str = "en") -> bool:
    return not missing_files(folder, language)


def missing_mb(folder: Path | None = None, language: str = "en") -> int:
    """How big the download still is (at least 1, so a couple of voice files don't read as "0 MB")."""
    todo = sum(f[2] for f in missing_files(folder, language))
    return max(1, round(todo / 1e6)) if todo else 0


_download_lock = threading.Lock()


def download(folder: Path | None = None, progress: Callable[[int, int], None] | None = None,
             stop: Callable[[], bool] | None = None, base_url: str = SOURCE_URL, language: str = "en") -> None:
    """Fetch whatever is missing (for `language`: its voices too), verifying each file's SHA-256. Safe to
    call from two places at once (the second waits, then finds everything there)."""
    folder = folder or models_dir()
    with _download_lock:
        todo = missing_files(folder, language)
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
                            raise KokoroError("download cancelled")
                        out.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            except OSError as exc:
                part.unlink(missing_ok=True)
                raise KokoroError(f"couldn't download the Kokoro voice ({exc})") from exc
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            if digest.hexdigest() != sha or part.stat().st_size != size:
                part.unlink(missing_ok=True)
                raise KokoroError(f"the downloaded {dest_name} was damaged; try again")
            part.replace(dest)


# --------------------------------------------------------------------------- text -> phonemes

_SYMBOLS = re.compile(r"[*_#`~<>\[\]{}|\\^=]")
_PUNCT = re.compile(r'\s*[;:,.!?¡¿—…"«»“”()]+\s*')
# eSpeak IPA (with "^" joining multi-letter phonemes) -> the phonemes Kokoro v1.0 learned
# (misaki's EspeakFallback mapping). Longest first.
_E2M = sorted({
    "ʔˌn̩": "ʔn", "ʔn̩": "ʔn", "ʔn": "ʔn", "ʔ": "t", "a^ɪ": "I", "a^ʊ": "W", "d^ʒ": "ʤ",
    "e": "A", "e^ɪ": "A", "r": "ɹ", "t^ʃ": "ʧ", "x": "k", "ç": "k", "ɐ": "ə", "ɔ^ɪ": "Y",
    "ə^l": "ᵊl", "ɚ": "əɹ", "ɬ": "l", "ʲ": "",
}.items(), key=lambda kv: -len(kv[0]))


# eSpeak IPA -> Kokoro's phonemes for the languages other than English (misaki's EspeakG2P).
_E2M_PLAIN = (("a^ɪ", "I"), ("a^ʊ", "W"), ("d^z", "ʣ"), ("d^ʒ", "ʤ"), ("e^ɪ", "A"), ("o^ʊ", "O"), ("ə^ʊ", "Q"),
              ("s^s", "S"), ("t^s", "ʦ"), ("t^ʃ", "ʧ"), ("ɔ^ɪ", "Y"))


def normalize_plain(text: str) -> str:
    """`normalize` for the other languages: only what's the same in all of them (eSpeak reads the numbers
    and signs in its own language; the English rewrites would put English words in the middle)."""
    t = text.replace("’", "'").replace("‘", "'")
    t = _SYMBOLS.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # 4,250 gold (Doty writes its numbers the English way)
    t = re.sub(r"(?<=\d)\.(?=\d)", ",", t)  # 2.5: these languages write (and eSpeak reads) a decimal comma
    t = re.sub(r"\b(\d{1,2}):(\d\d)\b", r"\1 \2", t)  # game time: "4:30" as two numbers, not a ratio
    return re.sub(r"\s+", " ", t).strip()


def normalize(text: str) -> str:
    """Rewrite things eSpeak reads badly ("4:00" is "four zero zero") or that split sentences."""
    t = text.replace("’", "'").replace("‘", "'")
    t = _SYMBOLS.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)  # 4,250 gold
    t = re.sub(r"(?<=\d)\.(?=\d)", " point ", t)  # 1.5
    t = re.sub(r"(?<=\d)[kK]\b", " thousand", t)  # 1.5k
    t = re.sub(r"\b1:00\b", "1 minute", t)
    t = re.sub(r"\b(\d{1,2}):00\b", r"\1 minutes", t)  # game time: "at 4:00"
    t = re.sub(r"\b(\d{1,2}):0(\d)\b", r"\1 oh \2", t)
    t = re.sub(r"\b(\d{1,2}):(\d\d)\b", r"\1 \2", t)  # 31:20 -> thirty-one twenty
    t = re.sub(r"\bvs\.?(?=\s)", "versus", t, flags=re.I)
    t = t.replace("&", " and ").replace("%", " percent")
    return re.sub(r"\s+", " ", t).strip()


class Espeak:
    """eSpeak NG through ctypes. It's one global engine per process, so calls are serialized."""

    _lock = threading.Lock()
    _lib = None
    _voice = b""

    @classmethod
    def _load(cls):
        if cls._lib is None:
            try:
                import espeakng_loader

                lib = ctypes.cdll.LoadLibrary(espeakng_loader.get_library_path())
                data = espeakng_loader.get_data_path()
            except (ImportError, OSError, RuntimeError) as exc:
                raise KokoroError(f"eSpeak NG is missing ({exc}); run: pip install espeakng-loader") from exc
            lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
            lib.espeak_TextToPhonemes.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int]
            lib.espeak_TextToPhonemes.restype = ctypes.c_char_p
            # AUDIO_OUTPUT_SYNCHRONOUS (no sound device), espeakINITIALIZE_DONT_EXIT
            if lib.espeak_Initialize(0x02, 0, data.encode(), 0x8000) <= 0:
                raise KokoroError("eSpeak NG didn't start")
            cls._lib = lib
        return cls._lib

    @classmethod
    def phonemes(cls, text: str, british: bool = False, voice: str = "") -> str:
        """IPA for one stretch of text without punctuation, words separated by spaces. `voice`: an eSpeak
        voice name for another language ("es", "pt-br"); "" is English, American or British."""
        with cls._lock:
            lib = cls._load()
            want = voice.encode() if voice else b"en-gb-x-rp" if british else b"en-us"
            if cls._voice != want:
                if lib.espeak_SetVoiceByName(want) != 0:  # never phonemize without a voice: it crashes
                    raise KokoroError(f"eSpeak NG has no {want.decode()} voice")
                cls._voice = want
            ptr = ctypes.pointer(ctypes.c_char_p(text.encode("utf-8")))
            out = []
            while ptr.contents.value:
                got = lib.espeak_TextToPhonemes(ptr, 1, 0x02 | 0x80 | ord("^") << 8)  # UTF-8 in, IPA, "^" ties
                if got:
                    out.append(got.decode("utf-8", "ignore").strip())
            return " ".join(out)


def _to_kokoro(ps: str, british: bool) -> str:
    if british:  # before _E2M turns the "e" into "A"
        ps = ps.replace("e^ə", "ɛː").replace("ə^ʊ", "Q")
    for old, new in _E2M:
        ps = ps.replace(old, new)
    ps = re.sub(r"(\S)̩", r"ᵊ\1", ps).replace("̩", "")
    if british:
        ps = ps.replace("iə", "ɪə")
    else:
        ps = ps.replace("o^ʊ", "O").replace("ɜːɹ", "ɜɹ").replace("ɜː", "ɜɹ").replace("ɪə", "iə").replace("ː", "")
    ps = re.sub(r"\([a-z-]+\)", "", ps)  # eSpeak's language-switch flags, e.g. "(fr)"
    ps = re.sub(r"(?<=[a-zɹː])(?=hˈʌndɹɪd)", " ", ps)  # "two hundred", not "twohundred"
    return ps.replace("^", "")


def _to_kokoro_plain(ps: str) -> str:
    # eSpeak's language-switch flags around a word it reads as another language ("(en)" before an English
    # name, "(fr)" after it; their letters come tied, "(e^n)").
    ps = re.sub(r"\([a-z^-]+\)", "", ps)
    for old, new in _E2M_PLAIN:
        ps = ps.replace(old, new)
    return ps.replace("^", "").replace("-", "")


def phonemize(text: str, british: bool = False, language: str = "en") -> str:
    """Kokoro phonemes for `text`, keeping the punctuation (Kokoro uses it for pauses and tone).
    `language`: "en", or one of `LANG_VOICES`."""
    espeak = ESPEAK_NAMES.get(language or "en", "")
    text = normalize_plain(text) if espeak else normalize(text)
    parts, pos = [], 0
    for m in list(_PUNCT.finditer(text)) + [None]:
        chunk = text[pos:m.start() if m else len(text)]
        if chunk.strip():
            parts.append(_to_kokoro_plain(Espeak.phonemes(chunk, voice=espeak)) if espeak else
                         _to_kokoro(Espeak.phonemes(chunk, british), british))
        if m is not None:
            mark = m.group()
            lead = " " if mark[0].isspace() and mark.strip()[0] in "—…(“\"«" else ""  # " — ", not "—"
            parts.append(lead + mark.strip() + (" " if mark[-1].isspace() else ""))
            pos = m.end()
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def split_text(text: str) -> list[str]:
    """Sentences, with a long first sentence cut at its first comma so speech starts sooner."""
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+", " ".join(text.split())) if s]
    if sentences and len(sentences[0]) > 70:
        m = re.search(r"[,;—]\s", sentences[0][25:])
        if m and len(sentences[0]) - 25 - m.end() > 12:
            cut = 25 + m.end()
            sentences[:1] = [sentences[0][:cut].strip(), sentences[0][cut:].strip()]
    return sentences


# --------------------------------------------------------------------------- the model

_sessions: dict[tuple[str, int, str], list] = {}  # (path, threads, device) -> [session, users, "GPU"/"CPU"]
_sessions_lock = threading.Lock()


def _open_session(path: Path, threads: int, device: str = "auto"):
    """One ONNX session per model, shared by Doty's voice and Settings' sample button.
    Returns (session, "GPU" or "CPU"). `device`: "auto" uses the GPU helper when it's set up."""
    key = (str(path), threads, device)
    with _sessions_lock:
        if key in _sessions:
            _sessions[key][1] += 1
            return _sessions[key][0], _sessions[key][2]
        from . import kokoro_gpu

        if device in ("auto", "gpu") and kokoro_gpu.available():
            try:
                session = kokoro_gpu.GpuSession(path)
                _sessions[key] = [session, 1, "GPU"]
                return session, "GPU"
            except KokoroError as exc:
                if device == "gpu":
                    raise
                print(f"[doty] voice: {exc}; using the CPU")
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise KokoroError("onnxruntime is missing; run: pip install -e .") from exc
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")  # don't burn CPU between lines
        so.add_session_config_entry("session.set_denormal_as_zero", "1")
        so.log_severity_level = 3
        try:
            session = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise KokoroError(f"couldn't load the Kokoro model ({exc})") from exc
        _sessions[key] = [session, 1, "CPU"]
        return session, "CPU"


def _close_session(path: Path, threads: int, device: str = "auto") -> None:
    key = (str(path), threads, device)
    with _sessions_lock:
        if key in _sessions:
            _sessions[key][1] -= 1
            if _sessions[key][1] <= 0:
                session = _sessions.pop(key)[0]
                close = getattr(session, "close", None)  # the GPU helper process
                if close is not None:
                    close()


def trim(audio, lead_s: float = 0.02, tail_s: float = 0.18, threshold: float = 0.01):
    """Cut Kokoro's padding: ~0.3 s of silence before each line and ~0.45 s after."""
    import numpy as np

    loud = np.flatnonzero(np.abs(audio) > threshold)
    if loud.size == 0:
        return audio[:0]
    start = max(0, loud[0] - int(lead_s * RATE))
    end = min(audio.size, loud[-1] + int(tail_s * RATE))
    return audio[start:end]


# --------------------------------------------------------------------------- cleaning up the sound
#
# Measured on Heart, Bella, Nicole and Michael (13-19 s of speech each):
# - **The ringing:** Kokoro's vocoder builds its sound in 5-sample steps at 24 kHz, which leaves
#   steady, near-pure whistles at 4.8 kHz and 9.6 kHz (±20 Hz wide) whenever it speaks, 13-22 dB
#   above the sound around them, in every voice. A fainter one sits at 7.2 kHz.
# - **The sharpness:** its top end is far brighter than real speech. 5-10 kHz sits only 3-10 dB below
#   the 500 Hz band, where ANSI S3.5's average speech spectrum is 29-35 dB below it.
# `clean` notches the whistles out, tilts the top end down toward natural speech, and turns harsh
# "s" sounds down only while they spike. Warm leaves Bella at 5 kHz -15 dB, 8 kHz -26 dB (ANSI: -29,
# -33) with the whistles below their surroundings. ~7 ms a second of speech.

TONES = {"warm": -16.0, "clear": -8.0}  # dB the top end (8.5 kHz and up) is turned down; "bright" is raw
WHISTLES = ((4800.0, 1.0), (9600.0, 1.0), (7200.0, 0.5))  # Hz, how much of it to remove (1 = all)
FILTER_VERSION = 2  # part of the phrase-cache key: lines an older filter made must not play back
_FRAME, _HOP = 480, 120  # the de-esser's 20 ms frames: 50 Hz bins, 75% overlap
_PAD = 2400  # 0.1 s of silence each side, so the notches don't wrap around


def _shape(freqs, top_db: float):
    """The fixed part: flat below 3.5 kHz, a smooth tilt down to `top_db` by 8.5 kHz, and narrow
    notches (±30 Hz) at the vocoder's whistles. Zero phase, so nothing shifts in time."""
    import numpy as np

    t = np.clip((freqs - 3500.0) / 5000.0, 0.0, 1.0)
    g_db = top_db * (0.5 - 0.5 * np.cos(np.pi * t))
    for f0, depth in WHISTLES:
        g_db = g_db + 20 * np.log10(np.maximum(1e-3, 1 - depth * np.exp(-((freqs - f0) / 30.0) ** 2)))
    return 10 ** (g_db / 20)


def _deess(x, thr_db: float = 0.0, ratio: float = 2.5):
    """Turn the top end (above ~4 kHz) down in the frames where it jumps above the voice's body
    (300 Hz-4 kHz): above `thr_db`, every dB over becomes 1/`ratio` dB. Smoothed over ~15 ms."""
    import numpy as np

    win = np.sqrt(np.hanning(_FRAME + 1)[:_FRAME]).astype(np.float32)  # periodic sqrt-Hann, in and out
    freqs = np.fft.rfftfreq(_FRAME, 1 / RATE)
    n = 1 + (x.size - _FRAME) // _HOP
    frames = np.fft.rfft(x[np.arange(n)[:, None] * _HOP + np.arange(_FRAME)] * win, axis=1)
    power = np.abs(frames) ** 2
    top = power[:, (freqs >= 4500) & (freqs < 11000)].sum(1)
    body = power[:, (freqs >= 300) & (freqs < 4000)].sum(1)
    over = 10 * np.log10((top + 1e-12) / (body + 1e-12)) - thr_db
    cut_db = np.convolve(-np.maximum(0.0, over) * (1 - 1 / ratio), np.ones(3) / 3, mode="same")
    ramp = np.clip((freqs - 3500.0) / 1500.0, 0.0, 1.0)
    frames *= 10 ** (cut_db[:, None] * ramp[None, :] / 20)
    parts = np.fft.irfft(frames, n=_FRAME, axis=1) * win
    out = np.zeros((n + 3, _HOP), np.float32)
    norm = np.zeros((n + 3, _HOP), np.float32)
    for j in range(_FRAME // _HOP):  # overlap-add, a quarter frame at a time
        out[j:j + n] += parts[:, j * _HOP:(j + 1) * _HOP]
        norm[j:j + n] += (win * win)[j * _HOP:(j + 1) * _HOP]
    out, norm = out.reshape(-1), norm.reshape(-1)
    y = x.copy()
    y[:out.size] = out / np.maximum(norm, 1e-3)
    return y


def clean(audio, tone: str = "warm", top_db: float | None = None):
    """Kokoro's voice without its whistles and fizz (see above). `tone` "warm" or "clear" ("bright"
    returns `audio` untouched); `top_db` overrides how far the top end goes down. Numpy only."""
    import numpy as np

    if tone not in TONES or audio.size < _FRAME:
        return audio
    top = TONES[tone] if top_db is None else top_db
    n = 1 << int(audio.size + 2 * _PAD - 1).bit_length()
    x = np.zeros(n, np.float32)
    x[_PAD:_PAD + audio.size] = audio
    x = np.fft.irfft(np.fft.rfft(x) * _shape(np.fft.rfftfreq(n, 1 / RATE), top), n).astype(np.float32)
    start = _PAD - _FRAME  # the de-esser only needs the speech plus a frame each side
    seg = _deess(x[start:_PAD + audio.size + _FRAME + _HOP])
    out = seg[_FRAME:_FRAME + audio.size].astype(np.float32)
    fade = min(120, out.size // 4)  # 5 ms in and out, so a line never starts or stops on a click
    out[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
    out[out.size - fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    return out


class Kokoro:
    """Loads the model once; `synth` yields 16-bit PCM a sentence (or clause) at a time."""

    model_id = MODEL_ID
    rate = RATE

    def __init__(self, folder: Path | None = None, threads: int | None = None, device: str = "auto") -> None:
        self.folder = folder or models_dir()
        self.threads = threads or default_threads()
        self.device_pref = device if device in ("auto", "cpu", "gpu") else "auto"
        self.device = ""  # "GPU" or "CPU" once loaded
        self._session = None
        self._vocab: dict[str, int] = {}
        self._styles: dict[str, object] = {}

    def installed(self) -> bool:
        return installed(self.folder)

    def has_language(self, language: str) -> bool:
        """This language's voices are on this PC too."""
        return speaks(language) and installed(self.folder, language)

    def load(self) -> None:
        """Blocking: ~2.5 s on the CPU, ~6 s on the GPU (a helper process starts). Raises KokoroError."""
        if self._session is not None:
            return
        if not self.installed():
            raise KokoroError("the Kokoro voice isn't downloaded yet")
        self._vocab = json.loads((self.folder / TOKENIZER[1]).read_text(encoding="utf-8"))["model"]["vocab"]
        Espeak.phonemes("ready")
        self._session, self.device = _open_session(self.folder / MODEL[1], self.threads, self.device_pref)
        self._run("rˈɛdi.", self._style(DEFAULT_VOICE, 6), 1.0)  # first run is slow; pay it now

    def close(self) -> None:
        if self._session is not None:
            self._session = None
            _close_session(self.folder / MODEL[1], self.threads, self.device_pref)

    def _style(self, voice: str, n_tokens: int):
        import numpy as np

        if voice not in self._styles:
            path = self.folder / "voices" / f"{voice}.bin"
            if not path.is_file():
                raise KokoroError(f"no Kokoro voice called {voice}")
            self._styles[voice] = np.fromfile(path, dtype=np.float32).reshape(-1, 1, 256)
        styles = self._styles[voice]
        return styles[min(n_tokens, len(styles) - 1)]

    def tokens(self, ps: str) -> list[int]:
        return [self._vocab[c] for c in ps if c in self._vocab]

    def _run(self, ps: str, style, speed: float):
        import numpy as np

        ids = self.tokens(ps)[:MAX_TOKENS]
        out = self._session.run(None, {"input_ids": np.array([[0, *ids, 0]], dtype=np.int64), "style": style,
                                       "speed": np.array([speed], dtype=np.float32)})
        return out[0].reshape(-1)

    def synth(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0,
              stop: Callable[[], bool] | None = None, tone: str = "warm") -> Iterator[bytes]:
        """Speech for `text` as 16-bit mono PCM, one sentence or clause per chunk. `tone` "warm" (the
        default) or "clear" removes the vocoder's whistles and fizz (`clean`) and plays a little
        quieter; "bright" is the model's raw sound."""
        import numpy as np

        if self._session is None:
            self.load()
        language = voice_language(voice)
        british = language == "en" and voice.startswith("b")
        pieces = split_text(text)
        for i, piece in enumerate(pieces):
            if stop is not None and stop():
                return
            ps = phonemize(piece, british, language)
            ids = self.tokens(ps)
            if not ids:
                continue
            for part in _fit(ps, ids, self):
                audio = self._run(part, self._style(voice, len(self.tokens(part))), speed)
                clause = not re.search(r"[.!?…]\s*$", piece) and i < len(pieces) - 1
                audio = trim(audio, tail_s=0.08 if clause else 0.18)
                warm = tone in TONES
                if warm:
                    audio = clean(audio, tone)
                peak = float(np.abs(audio).max()) if audio.size else 0.0
                # Kokoro is ~3 dB quieter than ElevenLabs. Warm and Clear peak at -3.5 dB rather than nearly
                # full scale: ~-20.5 dBFS RMS while speaking, the level Kitten matches.
                target = 0.67 if warm else 0.95
                gain = min(1.4, target / peak) if peak > 0 else 1.0
                yield (np.clip(audio * gain, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _fit(ps: str, ids: list[int], k: Kokoro) -> list[str]:
    """Split phonemes at spaces until each part fits the model's context."""
    if len(ids) <= MAX_TOKENS:
        return [ps]
    words = ps.split(" ")
    half = len(words) // 2
    if half == 0:
        return [ps[:MAX_TOKENS]]
    a, b = " ".join(words[:half]), " ".join(words[half:])
    return _fit(a, k.tokens(a), k) + _fit(b, k.tokens(b), k)
