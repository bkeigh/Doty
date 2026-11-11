import sys
import time

import numpy as np
import pytest

from doty.advice import AdviceBus
from doty.config import ListenConfig
from doty.holdkey import HoldKey, parse_hold
from doty.listen import RATE, VoiceInput

from .conftest import Recorder as AdviceRecorder


def test_parse_hold_keys():
    assert parse_hold("mouse5") == [0x06]
    assert parse_hold("mouse4") == [0x05]
    assert parse_hold("Ctrl+Alt+T") == [0x11, 0x12, ord("T")]
    assert parse_hold("f8") == [0x77]
    with pytest.raises(ValueError):
        parse_hold("hyper+q")


def test_mic_ranking_skips_capture_cards():
    from doty.listen import _mic_rank

    assert _mic_rank("HDMI (Elgato 4K S)") == 2
    assert _mic_rank("Microsoft Sound Mapper - Input") == 2
    assert _mic_rank("Stereo Mix (Realtek Audio)") == 2
    assert _mic_rank("Microphone (C922 Pro Stream Web") == 1
    assert _mic_rank("Microphone (NVIDIA Broadcast)") == 1
    assert _mic_rank("Microphone (Shure MV6)") == 0
    assert _mic_rank("Headset Microphone (Razer Nari ") == 0


def test_holdkey_reports_press_and_release_once():
    down = {0x11: False, 0x54: False}
    events = []
    hk = HoldKey("ctrl+t", lambda: events.append("press"), lambda: events.append("release"),
                 is_down=lambda vk: down[vk])
    hk.poll_once()
    down[0x11] = True
    hk.poll_once()  # only half the chord
    down[0x54] = True
    hk.poll_once()
    hk.poll_once()  # still held: no repeat
    down[0x11] = False
    hk.poll_once()
    hk.poll_once()
    assert events == ["press", "release"]


class FakeRecorder:
    device_name = "fake mic"

    def __init__(self, seconds=2.0):
        self.seconds = seconds
        self.started = 0

    def start(self):
        self.started += 1

    def stop(self):
        t = np.arange(int(self.seconds * RATE)) / RATE
        return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)


class FakeTranscriber:
    model = object()
    desc = "fake"

    def __init__(self, text):
        self.text = text
        self.prompts = []

    def load(self):
        pass

    def transcribe(self, pcm, extra_prompt=""):
        self.prompts.append(extra_prompt)
        return self.text


class FakeCoach:
    def __init__(self):
        self.questions = []

    def ask_question(self, q, shot):
        self.questions.append((q, shot))


class FakeVoice:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1


def make_vi(text="When should we go Roshan?", seconds=2.0):
    bus, rec = AdviceBus(), AdviceRecorder()
    bus.subscribe(rec)
    coach, voice = FakeCoach(), FakeVoice()
    vi = VoiceInput(ListenConfig(earcons=False), bus, coach, voice,
                    screenshot=lambda: ("SHOT", 1.0), context_words=lambda: "Juggernaut",
                    recorder=FakeRecorder(seconds), transcriber=FakeTranscriber(text))
    vi._load()
    return vi, rec, coach, voice


def _hold(vi, seconds):
    vi.on_press()
    vi._pressed_t = time.perf_counter() - seconds  # pretend the key was held this long
    vi.on_release()
    vi._pool.shutdown(wait=True)


def test_question_flows_to_coach_with_screenshot():
    vi, rec, coach, voice = make_vi()
    _hold(vi, 2.0)
    assert voice.interrupted == 1  # Doty stops talking when you start
    assert coach.questions == [("When should we go Roshan?", "SHOT")]
    texts = [a.text for a in rec.items]
    assert texts[0].startswith("Listening")
    assert "Transcribing…" in texts
    assert rec.items[-1].text == "“When should we go Roshan?”" and rec.items[-1].final
    assert len({a.id for a in rec.items}) == 1  # one card, updated in place
    assert vi.transcriber.prompts == ["Juggernaut"]


def test_short_press_is_ignored():
    vi, rec, coach, _ = make_vi()
    _hold(vi, 0.1)
    assert coach.questions == []
    assert rec.items[-1].text == "Hold the key while you talk"


def test_nothing_heard():
    vi, rec, coach, _ = make_vi(text="")
    _hold(vi, 1.5)
    assert coach.questions == []
    assert rec.items[-1].text.startswith("Didn't catch that")


def test_press_before_model_ready_explains():
    bus, rec = AdviceBus(), AdviceRecorder()
    bus.subscribe(rec)
    vi = VoiceInput(ListenConfig(earcons=False), bus, FakeCoach(), recorder=FakeRecorder(),
                    transcriber=FakeTranscriber("x"))
    vi.status = "loading speech model"
    vi.on_press()
    vi.on_release()
    assert "loading" in rec.items[0].text


def _cached_whisper_model() -> str | None:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    for name in ("small.en", "base.en"):
        if isinstance(try_to_load_from_cache(f"Systran/faster-whisper-{name}", "model.bin"), str):
            return name
    return None


@pytest.mark.skipif(sys.platform != "win32", reason="uses Windows TTS to synthesize speech")
def test_real_whisper_transcribes_spoken_question(tmp_path):
    model = _cached_whisper_model()
    if model is None:
        pytest.skip("no Whisper model downloaded yet (run `python -m doty mic` once)")
    import wave

    import pythoncom
    import win32com.client

    from doty.listen import WhisperTranscriber

    wav = tmp_path / "q.wav"
    pythoncom.CoInitialize()
    try:
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 18  # 16 kHz 16-bit mono
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format = fmt
        stream.Open(str(wav), 3)
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        sp.AudioOutputStream = stream
        sp.Speak("Should I buy Black King Bar or Manta Style next?")
        stream.Close()
        del sp, stream, fmt
    finally:
        pythoncom.CoUninitialize()
    with wave.open(str(wav)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    tr = WhisperTranscriber(model)
    tr.load()
    t0 = time.perf_counter()
    text = tr.transcribe(pcm).lower()
    ms = (time.perf_counter() - t0) * 1000
    assert "black king bar" in text and "manta" in text, text
    print(f"{tr.desc}: {ms:.0f} ms")
