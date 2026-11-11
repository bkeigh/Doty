"""Kokoro, the free local voice: text clean-up, phonemes, trimming, the verified download, and (when
the real model is on this PC) real speech that Whisper can understand."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from doty import kokoro
from doty.kokoro import Kokoro, KokoroError, normalize, phonemize, split_text, trim

try:
    import espeakng_loader  # noqa: F401

    HAVE_ESPEAK = True
except ImportError:
    HAVE_ESPEAK = False


def test_normalize_reads_game_numbers_and_symbols_aloud():
    assert normalize("You have 4,250 gold") == "You have 4250 gold"
    assert normalize("Roshan at 31:20, Aegis gone by 4:00 or 1:00.") == \
        "Roshan at 31 20, Aegis gone by 4 minutes or 1 minute."
    assert normalize("Back at 12:05") == "Back at 12 oh 5"
    assert normalize("1.5k gold, 50% HP & **BKB** vs Manta") == \
        "1 point 5 thousand gold, 50 percent HP and BKB versus Manta"


def test_split_text_starts_long_lines_sooner():
    assert split_text("Bounty runes in 15.  Then stack!") == ["Bounty runes in 15.", "Then stack!"]
    long = "Their carry is missing from the map and their mid has a blink, so play safe in lane. Ward."
    assert split_text(long) == ["Their carry is missing from the map and their mid has a blink,",
                                "so play safe in lane.", "Ward."]
    assert split_text("Back off, low HP") == ["Back off, low HP"]  # short lines stay whole


def test_trim_cuts_the_padding_but_keeps_the_words():
    audio = np.zeros(kokoro.RATE * 2, dtype=np.float32)
    audio[8000:20000] = 0.3  # "speech" from 0.33 s to 0.83 s
    out = trim(audio)
    assert out.size == (20000 - 1) + int(0.18 * kokoro.RATE) - (8000 - int(0.02 * kokoro.RATE))
    assert np.abs(out).max() == pytest.approx(0.3)
    assert trim(np.zeros(100, dtype=np.float32)).size == 0


@pytest.mark.skipif(not HAVE_ESPEAK, reason="espeakng-loader not installed")
def test_phonemes_match_what_kokoro_was_trained_on():
    assert phonemize("Bounty runes in fifteen seconds.") == "bˈWnti ɹˈunz ɪn fˈɪftin sˈɛkəndz."
    assert phonemize("Go home, it's over.") == "ɡˌO hˈOm, ɪts ˈOvəɹ."  # diphthongs as Kokoro's letters
    assert phonemize("4,250 gold — buy BKB.") == "fˈɔɹ θˈWzənd tˈu hˈʌndɹɪd fˈɪfti ɡˈOld — bˈI bˌikˌAbˈi."
    assert phonemize("Go home.", british=True) == "ɡˌQ hˈQm."


class FakeHub:
    """Serves made-up model files; `damaged` serves different bytes for one of them."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files, self.damaged, self.hits = files, set(), []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                name = self.path.lstrip("/")
                hub.hits.append(name)
                if name not in hub.files:
                    self.send_error(404)
                    return
                body = hub.files[name][::-1] if name in hub.damaged else hub.files[name]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_download_verifies_every_file_and_resumes_what_is_missing(tmp_path, monkeypatch):
    blobs = {"onnx/model.onnx": b"model" * 4000, "tokenizer.json": b'{"model": {"vocab": {}}}',
             "voices/af_heart.bin": bytes(range(256)) * 4}  # not a palindrome: "damaged" = reversed
    files = [(src, src.replace("onnx/", ""), len(b), hashlib.sha256(b).hexdigest()) for src, b in blobs.items()]
    monkeypatch.setattr(kokoro, "FILES", files)
    hub = FakeHub(blobs)
    try:
        hub.damaged.add("voices/af_heart.bin")
        seen = []
        with pytest.raises(KokoroError, match="damaged"):
            kokoro.download(tmp_path, progress=lambda done, total: seen.append((done, total)), base_url=hub.url)
        assert (tmp_path / "model.onnx").read_bytes() == blobs["onnx/model.onnx"]
        assert not (tmp_path / "voices" / "af_heart.bin").exists()
        assert not list(tmp_path.rglob("*.part")) and not kokoro.installed(tmp_path)
        assert seen and seen[-1][1] == sum(len(b) for b in blobs.values())

        hub.damaged.clear()
        hub.hits.clear()
        kokoro.download(tmp_path, base_url=hub.url)
        assert hub.hits == ["voices/af_heart.bin"]  # only what was missing
        assert kokoro.installed(tmp_path) and kokoro.missing_files(tmp_path) == []
        kokoro.download(tmp_path, base_url=hub.url)
        assert hub.hits == ["voices/af_heart.bin"]  # nothing left to fetch
    finally:
        hub.close()


def test_download_can_be_cancelled_and_reports_offline(tmp_path, monkeypatch):
    blobs = {"onnx/model.onnx": b"m" * (3 << 20)}
    monkeypatch.setattr(kokoro, "FILES", [("onnx/model.onnx", "model.onnx", 3 << 20,
                                           hashlib.sha256(blobs["onnx/model.onnx"]).hexdigest())])
    hub = FakeHub(blobs)
    try:
        with pytest.raises(KokoroError, match="cancelled"):
            kokoro.download(tmp_path, stop=lambda: True, base_url=hub.url)
        assert not (tmp_path / "model.onnx").exists() and not list(tmp_path.rglob("*.part"))
    finally:
        hub.close()
    with pytest.raises(KokoroError, match="couldn't download"):
        kokoro.download(tmp_path, base_url=hub.url)  # the server is gone


def test_missing_model_is_a_clear_error(tmp_path):
    k = Kokoro(folder=tmp_path)
    assert not k.installed()
    with pytest.raises(KokoroError, match="isn't downloaded"):
        k.load()


def _cached_whisper_model() -> str | None:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    for name in ("small.en", "base.en"):
        if isinstance(try_to_load_from_cache(f"Systran/faster-whisper-{name}", "model.bin"), str):
            return name
    return None


@pytest.mark.skipif(not (HAVE_ESPEAK and kokoro.installed(kokoro.MODELS_DIR)),
                    reason="the Kokoro model isn't downloaded on this PC (Settings > Voice > Download)")
def test_real_kokoro_speaks_clearly():
    k = Kokoro(folder=kokoro.MODELS_DIR)
    k.load()
    try:
        chunks = list(k.synth("Bounty runes in fifteen seconds. Their carry is missing, so play safe.", "af_heart"))
    finally:
        k.close()
    assert len(chunks) == 2
    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    seconds = pcm.size / kokoro.RATE
    assert 2.5 < seconds < 6.5, seconds
    first = np.frombuffer(chunks[0], dtype=np.int16)
    assert np.argmax(np.abs(first) > 300) < 0.1 * kokoro.RATE  # speech starts right away (padding trimmed)
    model = _cached_whisper_model()
    if model is None:
        return
    from doty.listen import WhisperTranscriber

    tr = WhisperTranscriber(model)
    tr.load()
    at16k = np.interp(np.arange(0, pcm.size, 1.5), np.arange(pcm.size), pcm).astype(np.int16)
    heard = tr.transcribe(at16k).lower()
    assert "bounty runes" in heard and "carry" in heard and "play safe" in heard, heard


def test_clean_removes_the_vocoder_whistles_and_keeps_the_voice():
    """clean(): the steady whistles Kokoro's vocoder leaves at 4.8 and 9.6 kHz are notched out, the
    fizzy top end tilts down (Clear less than Warm), and the voice's body and presence pass untouched."""
    import numpy as np

    from doty.kokoro import RATE, clean

    t = np.arange(RATE) / RATE

    def level(freq, tone="warm"):
        """How loud a faint tone at `freq` comes out, over a voice-like 200 Hz body (a lone high tone
        with nothing under it would look like an "s" to the de-esser)."""
        body = 0.3 * np.sin(2 * np.pi * 200 * t)
        probe = 0.01 * np.sin(2 * np.pi * freq * t)
        out = clean((body + probe).astype(np.float32), tone)[2400:-2400]  # 0.8 s: whole cycles of each
        ref = np.exp(-2j * np.pi * freq * t[2400:-2400])
        return 20 * np.log10(abs(np.dot(out, ref)) / abs(np.dot(probe[2400:-2400], ref)))

    assert abs(level(300)) < 0.5 and abs(level(2000)) < 0.5  # body and presence: unchanged
    assert level(4800) < -30 and level(9600) < -30  # the whistles: gone
    assert level(4600) > -2.5 and level(5000) > -4  # right next to them: only the gentle tilt
    assert level(10500) < -14 and -9 < level(10500, "clear") < -7  # the very top: down, less for Clear
    sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    assert clean(sine, "bright") is sine  # Bright is the raw voice
    assert clean(sine[:100]).size == 100 and clean(sine).dtype == np.float32


def test_clean_turns_harsh_s_sounds_down_only_while_they_spike():
    """The de-esser: a burst of hiss over a quiet voice is turned down; the voice around it isn't."""
    import numpy as np

    from doty.kokoro import RATE, clean

    rng = np.random.default_rng(1)
    t = np.arange(RATE) / RATE
    voice = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    hiss = rng.normal(0, 1, RATE).astype(np.float32)
    spec = np.fft.rfft(hiss)
    spec[np.fft.rfftfreq(RATE, 1 / RATE) < 5500] = 0
    spec[np.fft.rfftfreq(RATE, 1 / RATE) > 7500] = 0
    hiss = np.fft.irfft(spec, RATE).astype(np.float32)
    hiss *= 0.3 / np.abs(hiss).max()
    burst = voice.copy()
    burst[9600:14400] += hiss[9600:14400]  # an "s" 0.4 to 0.6 s in
    out_plain, out_burst = clean(voice), clean(burst)
    s_in = np.sqrt(np.mean(hiss[10000:14000] ** 2))
    s_out = np.sqrt(np.mean((out_burst - out_plain)[10000:14000] ** 2))
    assert 20 * np.log10(s_out / s_in) < -12  # tilt (~-6 dB there) plus the de-esser
    calm = slice(2000, 8000)
    assert np.allclose(out_burst[calm], out_plain[calm], atol=1e-3)  # the voice before it: untouched
