"""Kitten, the second free local voice: KittenML's symbol table, text to tokens, the voice list, the
offset filter, the verified download, and (when the real model is on this PC) real speech."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from doty import kitten
from doty.kitten import Kitten, KittenError, highpass, phonemize, tokens
from doty.kokoro import KokoroError

try:
    import espeakng_loader  # noqa: F401

    HAVE_ESPEAK = True
except ImportError:
    HAVE_ESPEAK = False


def test_symbol_table_is_kittenmls():
    """The ids must be exactly the ones KittenML's TextCleaner gives, or the model hears gibberish."""
    assert len(kitten._SYMBOLS) == 178
    v = kitten.VOCAB
    assert (v["$"], v[";"], v["."], v["…"], v['"'], v[" "]) == (0, 1, 4, 10, 15, 16)  # last '"' wins
    assert (v["A"], v["a"], v["z"], v["ɑ"], v["ɡ"], v["ˈ"], v["ˌ"], v["ː"]) == (17, 43, 68, 69, 92, 156, 157, 158)
    assert (v["'"], v["̩"], v["ᵻ"]) == (176, 175, 177)
    assert (kitten.PAD, kitten.END) == (0, 10)


def test_tokens_drop_symbols_the_model_never_saw():
    assert tokens("ɡˌoʊ hˈoʊm .") == [92, 157, 57, 135, 16, 50, 156, 57, 135, 55, 16, 4]
    assert tokens("a(b)“c”") == tokens("abc") == [43, 44, 45]


def test_unterminated_pieces_get_a_comma_like_kittenml():
    assert kitten._finish("Back off, low HP") == "Back off, low HP,"
    assert kitten._finish("Ward. ") == "Ward."
    assert kitten._finish("Wait…") == "Wait…"


@pytest.mark.skipif(not HAVE_ESPEAK, reason="espeakng-loader not installed")
def test_phonemes_are_espeak_ipa_with_stress_and_spaced_punctuation():
    """What KittenML's phonemizer(en-us, with_stress, preserve_punctuation) + basic_english_tokenize make."""
    assert phonemize("Bounty runes in fifteen seconds.") == "bˈaʊnti ɹˈuːnz ɪn fˈɪftiːn sˈɛkəndz ."
    assert phonemize("Go home, it's over.") == "ɡˌoʊ hˈoʊm , ɪts ˈoʊvɚ ."  # diphthongs stay two letters
    assert phonemize("4,250 gold — buy BKB.") == \
        "fˈɔːɹ θˈaʊzənd tˈuː hˈʌndɹɪd fˈɪfti ɡˈoʊld — bˈaɪ bˌiːkˌeɪbˈiː ."  # Doty's number clean-up applies
    assert phonemize("Roshan at 31:20!") == "ɹˈɑːʃən æt θˈɜːɾi wˈʌn twˈɛnti !"
    assert phonemize("Tie the bounty") == "tˈaɪ ðə bˈaʊnti"  # no "^" ties: Kitten wants plain IPA
    assert phonemize("Go (now)") == "ɡˈoʊ ( nˈaʊ )"  # as KittenML's front end; tokens() drops the brackets


@pytest.mark.skipif(not HAVE_ESPEAK, reason="espeakng-loader not installed")
def test_long_text_is_split_to_fit_the_models_context():
    long = " ".join(["Their carry is missing from the map and their mid has a blink"] * 12)
    parts = kitten._fit(long)
    assert len(parts) >= 2 and all(0 < len(ids) <= kitten.MAX_TOKENS for _text, _ps, ids in parts)
    assert " ".join(text for text, _ps, _ids in parts) == long  # no words lost
    short = kitten._fit("Back off, low HP,")
    assert len(short) == 1 and short[0][2] == tokens(short[0][1])


def test_voice_list():
    assert len(kitten.VOICES) == 8 and kitten.DEFAULT_VOICE in kitten.VOICES
    names = [name for _label, name in kitten.VOICES.values()]
    assert sorted(names) == sorted(f"expr-voice-{n}-{g}" for n in (2, 3, 4, 5) for g in "mf")
    for vid, (label, _name) in kitten.VOICES.items():
        assert vid == vid.lower() and label.split(":")[0].lower() == vid
        assert kitten.voice_label(vid) == label.split(":")[0]
    assert kitten.voice_label("nobody") == "nobody"
    assert kitten.DOWNLOAD_MB == 82 and [f[1] for f in kitten.FILES] == [kitten.MODEL[1], kitten.STYLES[1]]


def test_highpass_removes_the_offset_and_keeps_the_voice():
    t = np.arange(kitten.RATE * 2) / kitten.RATE

    def gain_db(signal):
        out = highpass(signal.astype(np.float32))[4000:-4000]
        return 20 * np.log10(np.sqrt(np.mean(out ** 2)) / np.sqrt(np.mean(signal[4000:-4000] ** 2)))

    assert gain_db(np.full(t.size, 0.2)) < -40  # a flat offset: gone
    assert gain_db(np.sin(2 * np.pi * 5 * t)) < -30  # the slow drift: gone
    assert abs(gain_db(np.sin(2 * np.pi * 110 * t))) < 0.3  # a low male voice: kept
    assert abs(gain_db(np.sin(2 * np.pi * 2000 * t))) < 0.05
    assert highpass(np.ones(10, dtype=np.float32)).size == 10  # too short to filter: as is


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


def test_download_verifies_every_file_and_fetches_only_what_is_missing(tmp_path, monkeypatch):
    blobs = {"kitten.onnx": b"model" * 4000, "voices.npz": bytes(range(256)) * 4}  # not palindromes
    monkeypatch.setattr(kitten, "FILES", [(n, n, len(b), hashlib.sha256(b).hexdigest()) for n, b in blobs.items()])
    hub = FakeHub(blobs)
    try:
        hub.damaged.add("voices.npz")
        seen = []
        with pytest.raises(KittenError, match="damaged"):
            kitten.download(tmp_path, progress=lambda done, total: seen.append((done, total)), base_url=hub.url)
        assert (tmp_path / "kitten.onnx").read_bytes() == blobs["kitten.onnx"]
        assert not (tmp_path / "voices.npz").exists() and not list(tmp_path.rglob("*.part"))
        assert not kitten.installed(tmp_path) and seen[-1][1] == sum(len(b) for b in blobs.values())

        hub.damaged.clear()
        hub.hits.clear()
        kitten.download(tmp_path, base_url=hub.url)
        kitten.download(tmp_path, base_url=hub.url)
        assert hub.hits == ["voices.npz"] and kitten.installed(tmp_path)
        with pytest.raises(KittenError, match="cancelled"):
            (tmp_path / "voices.npz").unlink()
            kitten.download(tmp_path, stop=lambda: True, base_url=hub.url)
        assert not list(tmp_path.rglob("*.part"))
    finally:
        hub.close()
    with pytest.raises(KittenError, match="couldn't download"):
        kitten.download(tmp_path, base_url=hub.url)  # the server is gone


def test_missing_model_is_a_clear_error_that_kokoro_handlers_catch(tmp_path):
    k = Kitten(folder=tmp_path)
    assert not k.installed()
    with pytest.raises(KokoroError, match="isn't downloaded"):
        k.load()
    assert issubclass(KittenError, KokoroError)


@pytest.mark.skipif(not (HAVE_ESPEAK and kitten.installed(kitten.models_dir())),
                    reason="the Kitten model isn't downloaded on this PC")
def test_real_kitten_speaks():
    k = Kitten(folder=kitten.models_dir())
    k.load()
    try:
        chunks = list(k.synth("Bounty runes in fifteen seconds. Their carry is missing, so play safe.", "jasper"))
        other = b"".join(k.synth("Back off, low HP", "kiki", speed=1.2, tone="bright"))
    finally:
        k.close()
    assert len(chunks) == 2 and other
    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    assert 3.0 < pcm.size / kitten.RATE < 8.0, pcm.size / kitten.RATE
    first = np.frombuffer(chunks[0], dtype=np.int16)
    assert np.argmax(np.abs(first) > 300) < 0.1 * kitten.RATE  # speech starts right away (silence trimmed)
    x = pcm / 32768
    assert abs(x.mean()) < 0.002  # no offset left
    assert -22.5 < 20 * np.log10(np.sqrt(np.mean(x ** 2))) < -18.5  # as loud as Kokoro
