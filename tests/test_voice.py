"""Doty's voice: ElevenLabs streaming against a local fake of its API, a fake Kokoro, and the Windows
voice as the fallback. Nothing here plays audio, calls the real ElevenLabs or loads the real Kokoro."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from doty.config import VoiceConfig
from doty.eleven import (FALLBACK_VOICE, KEY_ENV, ElevenError, ElevenLabs, PhraseCache, pick_voice,
                         speed_for_rate)
from doty.kokoro import KokoroError
from doty.voice import Voice

GOOD_KEY = "sk_test_good_key_000000000000"
GEORGE = "JBFqnCBsd6RMkjVDRZzb"
VOICES = [
    {"voice_id": "aria0000", "name": "Aria", "category": "premade", "labels": {"accent": "american"}},
    {"voice_id": GEORGE, "name": "George", "category": "premade", "labels": {"accent": "british"}},
    {"voice_id": "mine0000", "name": "My clone", "category": "cloned", "labels": {}},
]
# Odd chunk sizes on purpose: samples (2 bytes) get split across network chunks.
CHUNKS = [b"\x01" * 1001, b"\x02" * 2000, b"\x03" * 999]
PCM = b"".join(CHUNKS)


class FakeElevenLabs:
    """Just enough of the API: GET /v2/voices and the streaming TTS endpoint (chunked PCM)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.voice_lists = 0
        self.tts_status = 200  # or 402 / 500 to simulate failures
        self.slow_words: tuple[str, ...] = ()  # texts containing these stream slowly
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if self.headers.get("xi-api-key") == GOOD_KEY:
                    return True
                self._json(401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})
                return False

            def do_GET(self):
                if not self._authorized():
                    return
                fake.voice_lists += 1
                self._json(200, {"voices": VOICES, "has_more": False, "next_page_token": None})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                url = urlsplit(self.path)
                fake.requests.append({"path": url.path, "query": parse_qs(url.query), "body": body})
                if not self._authorized():
                    return
                if fake.tts_status == 402:
                    return self._json(402, {"detail": {"status": "quota_exceeded", "message": "No credits"}})
                if fake.tts_status != 200:
                    return self._json(fake.tts_status, {"detail": "boom"})
                self.send_response(200)
                self.send_header("Content-Type", "audio/pcm")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                slow = any(w in body["text"] for w in fake.slow_words)
                for chunk in CHUNKS * (20 if slow else 1):
                    try:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                    except OSError:
                        return  # the client hung up (interrupted)
                    if slow:
                        time.sleep(0.05)
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def texts(self) -> list[str]:
        return [r["body"]["text"] for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakePlayer:
    """Records audio instead of playing it; 'plays' instantly."""

    def __init__(self) -> None:
        self.audio = bytearray()
        self.clears = 0

    def write(self, pcm: bytes, volume: float = 1.0) -> None:
        self.audio += pcm

    def clear(self) -> None:
        self.clears += 1

    pending_s = 0.0

    def wait(self, stop=lambda: False, timeout: float = 60.0) -> None:
        pass

    def close(self) -> None:
        pass


class FakeSapi:
    """Records what the Windows voice would have said."""

    spoken: list[str] = []

    def __init__(self, cfg) -> None:
        FakeSapi.spoken = []

    def speak(self, text: str, purge: bool) -> None:
        FakeSapi.spoken.append(text)

    def purge(self) -> None:
        pass

    def wait(self, interrupted) -> None:
        pass

    def close(self) -> None:
        pass


class FakeKokoro:
    """Kokoro without the model: each sentence of a line becomes a fixed block of PCM."""

    model_id = "fake-kokoro"
    device = "CPU"
    BLOCK = b"\x05\x00" * 500

    def __init__(self, installed: bool = True, fail: bool = False, delay: float = 0.0) -> None:
        self._installed, self.fail, self.delay = installed, fail, delay
        self.calls: list[tuple[str, str, float]] = []
        self.loaded = self.closed = False

    def installed(self) -> bool:
        return self._installed

    def load(self) -> None:
        self.loaded = True

    def close(self) -> None:
        self.closed = True

    def synth(self, text, voice="af_heart", speed=1.0, stop=None, tone="warm"):
        self.calls.append((text, voice, speed))
        self.tones = getattr(self, "tones", []) + [tone]
        if self.fail:
            raise KokoroError("Kokoro broke")
        for _sentence in text.split(". "):
            if stop is not None and stop():
                return
            if self.delay:
                time.sleep(self.delay)
            yield self.BLOCK


@pytest.fixture
def fake():
    server = FakeElevenLabs()
    yield server
    server.close()


def make_voice(fake, tmp_path, monkeypatch, key=GOOD_KEY, kokoro=None, prepare=(), **cfg_over):
    if key:
        monkeypatch.setenv(KEY_ENV, key)
    else:
        monkeypatch.delenv(KEY_ENV, raising=False)
    cfg = VoiceConfig(volume=100, **cfg_over)
    player = FakePlayer()
    kokoro = kokoro or FakeKokoro(installed=False)
    v = Voice(cfg, eleven_url=fake.url, player_factory=lambda: player, sapi_factory=FakeSapi,
              cache=PhraseCache(tmp_path / "voice"), kokoro_factory=lambda: kokoro)
    v.prepare(prepare)
    v.start()
    assert v.ready.wait(10)
    return v, player


def say_and_wait(v, text, interrupt=False):
    v.say(text, interrupt=interrupt)
    assert v.wait_idle(10)


def test_speaks_with_elevenlabs_streaming_pcm(fake, tmp_path, monkeypatch):
    v, player = make_voice(fake, tmp_path, monkeypatch)
    assert v.engine == "elevenlabs" and v.voice_name == "George" and v.problem == ""
    say_and_wait(v, "Bounty runes in fifteen seconds.")
    assert bytes(player.audio) == PCM  # every byte, in order, across odd chunk boundaries
    req = fake.requests[0]
    assert req["path"] == f"/v1/text-to-speech/{GEORGE}/stream"
    assert req["query"]["output_format"] == ["pcm_24000"]
    assert req["body"]["model_id"] == "eleven_flash_v2_5"
    assert req["body"]["voice_settings"] == {"speed": speed_for_rate(2)}  # Doty's default speed
    assert FakeSapi.spoken == [] and v.last_speak_ms is not None
    v.stop(join=3)


def test_repeated_lines_replay_from_cache_without_a_request(fake, tmp_path, monkeypatch):
    v, player = make_voice(fake, tmp_path, monkeypatch)
    say_and_wait(v, "No TP scroll.")
    say_and_wait(v, "No TP scroll.")
    assert fake.texts() == ["No TP scroll."] and bytes(player.audio) == PCM * 2
    long = "Their carry has BKB and a blink now, so stop fighting in the open and farm the safe lane. " * 2
    say_and_wait(v, long)
    say_and_wait(v, long)
    assert fake.texts().count(long) == 2  # one-off coach answers aren't cached
    v.stop(join=3)


def test_chosen_voice_is_used(fake, tmp_path, monkeypatch):
    v, _player = make_voice(fake, tmp_path, monkeypatch, eleven_voice="mine0000")
    assert v.voice_name == "My clone"
    say_and_wait(v, "Smoke up.")
    assert fake.requests[0]["path"].endswith("/mine0000/stream")
    v.stop(join=3)


def test_bad_key_falls_back_to_the_windows_voice(fake, tmp_path, monkeypatch):
    v, player = make_voice(fake, tmp_path, monkeypatch, key="sk_wrong_key_000000000000")
    assert v.engine == "windows" and "didn't accept the API key" in v.problem
    say_and_wait(v, "Back off.")
    assert FakeSapi.spoken == ["Back off."] and fake.requests == [] and not player.audio
    v.stop(join=3)


def test_out_of_credits_switches_to_windows_voice_for_good(fake, tmp_path, monkeypatch):
    v, _player = make_voice(fake, tmp_path, monkeypatch)
    fake.tts_status = 402
    say_and_wait(v, "Rune in fifteen.")
    say_and_wait(v, "Stack now.")
    assert FakeSapi.spoken == ["Rune in fifteen.", "Stack now."]  # nothing lost
    assert len(fake.requests) == 1  # it stopped asking after the quota error
    assert v.engine == "windows" and "out of ElevenLabs credits" in v.problem
    v.stop(join=3)


def test_a_failed_line_is_spoken_by_windows_then_elevenlabs_resumes(fake, tmp_path, monkeypatch):
    v, player = make_voice(fake, tmp_path, monkeypatch)
    fake.tts_status = 500
    say_and_wait(v, "Roshan window opens.")
    assert FakeSapi.spoken == ["Roshan window opens."] and "filling in" in v.problem
    fake.tts_status = 200
    say_and_wait(v, "Tormentor is up.")
    assert bytes(player.audio) == PCM and v.engine == "elevenlabs" and v.problem == ""
    v.stop(join=3)


def test_urgent_line_cuts_off_and_drops_the_queue(fake, tmp_path, monkeypatch):
    v, player = make_voice(fake, tmp_path, monkeypatch)
    fake.slow_words = ("long",)
    v.say("A long explanation of the lane.")
    deadline = time.time() + 5
    while not player.audio and time.time() < deadline:  # it's mid-stream
        time.sleep(0.01)
    v.say("Queued rune reminder.", stale_s=8)  # (alerts about a moment carry a deadline, like the rules' timers)
    v.say("Back off now!", interrupt=True)
    assert v.wait_idle(10)
    assert "Queued rune reminder." not in fake.texts()  # dropped, never fetched
    assert fake.texts()[-1] == "Back off now!" and player.clears >= 1
    assert len(player.audio) < len(PCM) * 20 + len(PCM)  # the long line was cut short
    v.stop(join=3)


def test_a_cut_in_keeps_the_rest_of_an_answer_you_asked_for(fake, tmp_path, monkeypatch):
    """A prepared call cuts in over an answer, but the answer's later sentences (no deadline: you asked for
    them) still come, after the call; a timer queued meanwhile is dropped."""
    k = FakeKokoro(delay=0.05)
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    v.say("First sentence of the answer.")
    deadline = time.time() + 5
    while not player.audio and time.time() < deadline:
        time.sleep(0.01)
    v.say("Second sentence of the answer.")
    v.say("Power rune in 15", stale_s=8)
    v.say("Glimmer yourself", interrupt=True, stale_s=3)
    assert v.wait_idle(10)
    said = [c[0] for c in k.calls]
    assert said[-2:] == ["Glimmer yourself", "Second sentence of the answer."]
    assert "Power rune in 15" not in said
    v.stop(join=3)


def test_windows_engine_never_calls_elevenlabs(fake, tmp_path, monkeypatch):
    v, _player = make_voice(fake, tmp_path, monkeypatch, engine="windows")
    assert v.engine == "windows" and v.problem == ""
    say_and_wait(v, "Day time.")
    assert FakeSapi.spoken == ["Day time."] and fake.requests == [] and fake.voice_lists == 0
    v.stop(join=3)


def test_auto_without_a_key_quietly_uses_windows(fake, tmp_path, monkeypatch):
    v, _player = make_voice(fake, tmp_path, monkeypatch, key=None)
    assert v.engine == "windows" and v.problem == "" and fake.voice_lists == 0
    v.stop(join=3)
    v2, _ = make_voice(fake, tmp_path, monkeypatch, key=None, engine="elevenlabs")
    assert v2.engine == "windows" and "no ElevenLabs key" in v2.problem
    v2.stop(join=3)


def test_client_errors_are_friendly(fake):
    with pytest.raises(ElevenError) as bad:
        ElevenLabs("sk_nope_000000000000000000", base_url=fake.url).voices()
    assert bad.value.fatal and "didn't accept" in str(bad.value)
    voices = ElevenLabs(GOOD_KEY, base_url=fake.url).voices()
    assert [v["name"] for v in voices] == ["Aria", "George", "My clone"]


def test_default_voice_choice_and_speed():
    voices = [{"id": v["voice_id"], "name": v["name"], "category": v["category"], "labels": v["labels"]}
              for v in VOICES]
    assert pick_voice(voices) == (GEORGE, "George")
    assert pick_voice(voices[2:]) == ("mine0000", "My clone")
    assert pick_voice([]) == FALLBACK_VOICE
    assert speed_for_rate(0) == 1.0 and speed_for_rate(-10) == 0.7 and speed_for_rate(10) == 1.2


# Kokoro ------------------------------------------------------------------------------------------


def test_auto_without_a_key_uses_kokoro_once_downloaded(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k, kokoro_voice="am_michael")
    assert v.engine == "kokoro" and v.voice_name == "Michael" and v.problem == "" and k.loaded
    assert v.device == "CPU"
    say_and_wait(v, "Bounty runes in 15. Then stack.")
    assert bytes(player.audio) == FakeKokoro.BLOCK * 2  # sentence by sentence, in order
    assert k.calls == [("Bounty runes in 15. Then stack.", "am_michael", speed_for_rate(2))]
    say_and_wait(v, "Bounty runes in 15. Then stack.")
    assert len(k.calls) == 1 and bytes(player.audio) == FakeKokoro.BLOCK * 4  # second time from the cache
    assert FakeSapi.spoken == [] and fake.requests == [] and fake.voice_lists == 0
    v.stop(join=3)
    assert k.closed


def test_kokoro_chosen_but_not_downloaded_says_so(fake, tmp_path, monkeypatch):
    v, _player = make_voice(fake, tmp_path, monkeypatch, key=None, engine="kokoro")
    assert v.engine == "windows" and "isn't downloaded yet" in v.problem
    say_and_wait(v, "Smoke up.")
    assert FakeSapi.spoken == ["Smoke up."]
    v.stop(join=3)
    v2, _ = make_voice(fake, tmp_path, monkeypatch, key=None)  # auto: just the Windows voice, no fuss
    assert v2.engine == "windows" and v2.problem == ""
    v2.stop(join=3)


def test_unknown_kokoro_voice_falls_back_to_the_default(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, _player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k, kokoro_voice="zz_gone")
    say_and_wait(v, "Tormentors spawning")
    assert v.voice_name == "Heart" and k.calls[0][1] == "af_heart"
    v.stop(join=3)


def test_rejected_elevenlabs_key_hands_over_to_kokoro(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, key="sk_wrong_key_000000000000", kokoro=k)
    assert v.engine == "kokoro" and "didn't accept the API key" in v.problem and "Kokoro" in v.problem
    say_and_wait(v, "Back off.")
    assert bytes(player.audio) == FakeKokoro.BLOCK and FakeSapi.spoken == [] and fake.requests == []
    v.stop(join=3)


def test_out_of_credits_mid_game_switches_to_kokoro(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, kokoro=k)
    assert v.engine == "elevenlabs" and not k.loaded  # not loaded while ElevenLabs works
    say_and_wait(v, "Rune in fifteen.")
    fake.tts_status = 402
    say_and_wait(v, "Stack now.")
    say_and_wait(v, "Ward the jungle.")
    assert [c[0] for c in k.calls] == ["Stack now.", "Ward the jungle."]  # the failed line wasn't lost
    assert len(fake.requests) == 2 and FakeSapi.spoken == []
    assert v.engine == "kokoro" and "out of ElevenLabs credits" in v.problem
    assert bytes(player.audio) == PCM + FakeKokoro.BLOCK * 2
    v.stop(join=3)


def test_kokoro_failures_fall_back_to_windows(fake, tmp_path, monkeypatch):
    k = FakeKokoro(fail=True)
    v, _player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    say_and_wait(v, "One.")
    assert FakeSapi.spoken == ["One."] and "filling in" in v.problem and v.engine == "kokoro"
    say_and_wait(v, "Two.")
    say_and_wait(v, "Three.")
    say_and_wait(v, "Four.")
    assert FakeSapi.spoken == ["One.", "Two.", "Three.", "Four."] and len(k.calls) == 3  # gave up after 3
    assert v.engine == "windows" and "using the Windows voice" in v.problem
    v.stop(join=3)


def test_kokoro_prepares_fixed_lines_while_idle(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    lines = ["Back off, low HP", "Roshan is up", "Bounty runes in 15"]
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k, prepare=lines + ["Roshan is up"])
    deadline = time.time() + 5
    while v.prepared < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert [c[0] for c in k.calls] == lines and not player.audio  # made quietly, played nothing
    say_and_wait(v, "Roshan is up")
    assert len(k.calls) == 3 and bytes(player.audio) == FakeKokoro.BLOCK  # straight from the cache
    v.stop(join=3)
    # a restart finds them on disk and doesn't make them again
    k2 = FakeKokoro()
    v2, _ = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k2, prepare=lines)
    deadline = time.time() + 5
    while v2.prepared < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert k2.calls == []
    v2.stop(join=3)


def test_urgent_line_cuts_off_kokoro_between_sentences(fake, tmp_path, monkeypatch):
    k = FakeKokoro(delay=0.05)
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    v.say(". ".join(f"Sentence {i}" for i in range(40)))
    deadline = time.time() + 5
    while not player.audio and time.time() < deadline:
        time.sleep(0.01)
    v.say("Queued rune reminder.", stale_s=8)  # (alerts about a moment carry a deadline, like the rules' timers)
    v.say("Back off now!", interrupt=True)
    assert v.wait_idle(10)
    said = [c[0] for c in k.calls]
    assert said[-1] == "Back off now!" and "Queued rune reminder." not in said
    assert len(player.audio) < len(FakeKokoro.BLOCK) * 40 and player.clears >= 1
    v.stop(join=3)


def test_kitten_chosen_speaks_with_its_voice_and_pace(fake, tmp_path, monkeypatch):
    from doty.voice import KITTEN_PACE

    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k, engine="kitten", kitten_voice="bruno")
    assert v.engine == "kitten" and v.voice_name.startswith("Bruno") and v.problem == ""
    say_and_wait(v, "Rune up top.")
    assert k.calls == [("Rune up top.", "bruno", speed_for_rate(2) * KITTEN_PACE)]  # Kitten talks slower
    assert k.tones == ["warm"]
    v.stop(join=3)
    v2, _ = make_voice(fake, tmp_path, monkeypatch, key=None, engine="kitten")  # not downloaded: says so
    assert v2.engine == "windows" and "Kitten voice isn't downloaded" in v2.problem
    v2.stop(join=3)


def test_phrase_cache_keeps_the_lines_used_most_recently(tmp_path):
    """The cache holds every short line (the coach's one-off lines too), so it's pruned to the most
    recently used: a fixed alert played again stays even when it was saved first."""
    import os

    from doty.eleven import PhraseCache

    cache = PhraseCache(tmp_path)
    for i in range(6):
        cache.put(f"k{i}", bytes(100))
        os.utime(tmp_path / f"k{i}.pcm", (1000 + i, 1000 + i))
    assert PhraseCache(tmp_path).get("k0") == bytes(100)  # played again: now the most recent
    assert cache.prune(max_files=3) == 3
    assert sorted(p.stem for p in tmp_path.glob("*.pcm")) == ["k0", "k4", "k5"]
    assert cache.prune(max_files=10, max_bytes=250) == 1  # and a size cap
    assert PhraseCache(tmp_path).get("missing") is None


def test_player_reopens_a_stream_whose_device_went_away(monkeypatch):
    import sys
    import types

    from doty.eleven import PcmPlayer

    opened = []

    class Stream:
        def __init__(self, **kw):
            self.active, self.closed = True, False
            opened.append(self)

        def start(self):
            pass

        def stop(self):
            self.active = False

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=Stream))
    player = PcmPlayer()
    player.write(bytes(480))
    opened[0].active = False  # the headset was unplugged: PortAudio stopped the stream
    player.write(bytes(480))
    assert len(opened) == 2 and opened[0].closed and opened[1].active


def test_a_cut_off_voice_list_is_an_elevenlabs_error_not_a_crash():
    import http.client

    from doty.eleven import ElevenError, ElevenLabs

    client = ElevenLabs("key", base_url="http://127.0.0.1:9")

    class Cut:
        def read(self):
            raise http.client.IncompleteRead(b'{"voices": [', 400)

    client._request = lambda *a, **k: Cut()
    try:
        client.voices()
    except ElevenError as exc:
        assert not exc.fatal and "voices" in str(exc)
    else:
        raise AssertionError("expected ElevenError")


# never late, ready ahead -----------------------------------------------------------------------------------
def test_a_line_that_would_start_too_late_is_dropped(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    player.pending_s = 9.0  # nine seconds of audio still waiting to play
    v.say("Bounty runes in 15", stale_s=8.0)
    v.say("What to buy next, as you asked.")  # no deadline: always said
    deadline = time.time() + 5
    while len(k.calls) < 1 and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)
    assert [c[0] for c in k.calls] == ["What to buy next, as you asked."]
    assert v.dropped_late == 1
    player.pending_s = 0.0
    v.say("Roshan is up", stale_s=8.0)
    deadline = time.time() + 5
    while len(k.calls) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert k.calls[-1][0] == "Roshan is up"
    v.stop(join=3)


def test_prepared_calls_are_made_ahead_then_play_from_the_cache(fake, tmp_path, monkeypatch):
    k = FakeKokoro()
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    v.prepare_now(["Walk back now, you're too low."])
    deadline = time.time() + 5
    while not k.calls and time.time() < deadline:
        time.sleep(0.02)
    assert k.calls[0][0] == "Walk back now, you're too low." and not player.audio  # made quietly
    assert v.idle()  # the wake-up isn't counted as a line
    say_and_wait(v, "Walk back now, you're too low.")
    assert len(k.calls) == 1 and bytes(player.audio) == FakeKokoro.BLOCK  # straight from the cache
    v.stop(join=3)


def test_a_cut_in_advice_interrupts_what_is_playing(fake, tmp_path, monkeypatch):
    from doty.advice import Advice, Priority

    k = FakeKokoro(delay=0.05)
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    v.say(". ".join(f"Sentence {i}" for i in range(40)))
    deadline = time.time() + 5
    while not player.audio and time.time() < deadline:
        time.sleep(0.01)
    v.on_advice(Advice("Back off now.", Priority.HIGH, "trigger", source="coach", cut_in=True, stale_s=3.0))
    assert v.wait_idle(10)
    assert k.calls[-1][0] == "Back off now." and player.clears >= 1
    assert len(player.audio) < len(FakeKokoro.BLOCK) * 40
    v.stop(join=3)


def test_a_cut_in_never_cuts_off_a_critical_alert(fake, tmp_path, monkeypatch):
    from doty.advice import Advice, Priority

    k = FakeKokoro(delay=0.05)
    v, player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=k)
    v.on_advice(Advice("Back off. Back off. Back off. Back off, low HP", Priority.CRITICAL, "survival"))
    deadline = time.time() + 5
    while not player.audio and time.time() < deadline:
        time.sleep(0.01)
    v.on_advice(Advice("Buy Black King Bar now.", Priority.HIGH, "trigger", source="coach", cut_in=True,
                       stale_s=10.0))
    assert v.wait_idle(10)
    said = [c[0] for c in k.calls]
    assert said == ["Back off. Back off. Back off. Back off, low HP", "Buy Black King Bar now."]
    assert len(player.audio) == len(FakeKokoro.BLOCK) * 5  # the alert played whole, then the call
    v.stop(join=3)


def test_a_wake_up_is_not_a_line_waiting(fake, tmp_path, monkeypatch):
    from doty.voice import WAKE

    v, _player = make_voice(fake, tmp_path, monkeypatch, key=None, kokoro=FakeKokoro())
    v.stop(join=3)
    v._q.put(WAKE)
    assert not v._line_waiting()
    v._q.put(("Roshan is up", False, None))
    assert v._line_waiting()


class SlowSapi(FakeSapi):
    """The Windows voice, taking LINE_S to say each line (it keeps its own queue, like the real one)."""

    LINE_S = 0.6

    def wait(self, interrupted) -> None:
        end = time.monotonic() + self.LINE_S
        while time.monotonic() < end and not interrupted():
            time.sleep(0.01)


def test_the_windows_voice_drops_a_line_that_would_start_too_late(fake, tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    v = Voice(VoiceConfig(volume=100), eleven_url=fake.url, player_factory=FakePlayer, sapi_factory=SlowSapi,
              cache=PhraseCache(tmp_path / "voice"), kokoro_factory=lambda: FakeKokoro(installed=False))
    v.start()
    assert v.ready.wait(10) and v.engine == "windows"
    v.say("You died because you were alone in their jungle.")
    time.sleep(0.05)
    v.say("Power rune in 15", stale_s=0.3)  # can't start until the first line is over: dropped, not said late
    assert v.wait_idle(10)
    assert FakeSapi.spoken == ["You died because you were alone in their jungle."] and v.dropped_late == 1
    v.say("Farm the bottom jungle until Battle Fury.")
    time.sleep(0.05)
    v.say("Back off, low HP", interrupt=True)  # a critical line still cuts in
    assert v.wait_idle(10)
    assert FakeSapi.spoken[-2:] == ["Farm the bottom jungle until Battle Fury.", "Back off, low HP"]
    v.stop(join=3)
