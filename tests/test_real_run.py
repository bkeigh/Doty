"""What the first run against the real model found (a scripted Juggernaut match, ten game minutes, the real Claude
answering): each mistake reproduced here with the model's own words from that run, served by the local fake, then
fixed. Nothing here calls the real API."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestServer

from doty.advice import Priority
from doty.match_record import ADVICE_FILE, list_records
from doty.sim import ScriptedMatch

from .conftest import TOKEN, Recorder
from .fake_anthropic import FakeAnthropic

# The real model's answers in that run (the start look at -0:10, the death look at 5:15).
PLAN = ("Take the bounty rune at 0:00, then go to the bottom lane. Last hit with Quelling Blade and stay out of "
        "harass range. Check who is in the enemy lane for ganks.")
DEATH = ("Likely a gank: you went from full HP to dead in 3 seconds. Buy Power Treads and a Town Portal Scroll "
         "at the fountain, then walk back and stay near your tower.")


@pytest.fixture
async def fake_ai(monkeypatch):
    fake = FakeAnthropic("")
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    yield fake
    await server.close()


async def _wait(coach):
    for _ in range(300):
        if coach._inflight and coach._inflight[2].done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the look never finished")


@pytest.mark.parametrize("talk", ["less", "normal", "more"])
async def test_the_start_plan_and_the_death_answer_are_heard(talk, fake_ai, tmp_path, monkeypatch):
    """The run: "Bounty runes in 15s" at -0:15 then the plan at -0:10; "No TP scroll" at 5:10 and "Level 6" at 5:15,
    then the death answer at 5:15. Not one coach line was marked spoken. Both answers are now said in full, and the
    session log and the match record say so."""
    from doty import app as appmod
    from doty.coach.backends import make_backend

    from .test_app_ui import quiet_cfg

    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    cfg = quiet_cfg(tmp_path)
    cfg.coach.enabled, cfg.coach.talk, cfg.coach.use_screenshot = True, talk, False
    cfg.coach.speculative_interval_s = 0  # (only the two looks of this test)
    cfg.record.enabled, cfg.record.folder = True, str(tmp_path / "records")
    doty = appmod.Doty(cfg, overlay=False, voice=False, coach=True, listen=False, quiet=True)
    doty._log_path = tmp_path / "session-test.jsonl"
    voice = Recorder()  # what a voice would be handed: said, and within its priority
    doty.bus.subscribe(lambda a: voice(a) if a.speak and a.final and a.priority <= cfg.voice.min_priority else None)
    coach = doty.coach
    coach._loop = asyncio.get_running_loop()
    coach.backend, coach.enabled, coach.status = make_backend(cfg.coach), True, "ready"
    launch = coach._launch
    coach._launch = lambda *a, **k: False  # (the looks come exactly when the run had them)

    match = ScriptedMatch(match_id="4242")
    t = 1000.0

    def feed_to(clock):
        nonlocal t
        while match.s.clock < clock:
            match.step(1.0)
            t += 1.0 / 6  # (the demo's speed: six game seconds a second)
            doty._on_payload(match.payload(TOKEN), t)

    feed_to(-10)
    fake_ai.reply = PLAN
    launch("start", visible=True)
    await _wait(coach)
    feed_to(315)
    fake_ai.reply = DEATH
    launch("death", visible=True)
    await _wait(coach)
    feed_to(330)
    doty.record.close()
    doty._log.close()

    heard = " ".join(a.text for a in voice.items if a.source == "coach")
    for sentence in PLAN.split(". ") + DEATH.split(". "):
        assert sentence.rstrip(".") in heard, sentence
    log = [json.loads(x) for x in (tmp_path / "session-test.jsonl").read_text(encoding="utf-8").splitlines()]
    answers = [r for r in log if r["source"] == "coach" and r["category"] == "coach"]
    assert [r["speak"] for r in answers] == [True, True] and answers[0]["clock"] < 0 <= 300 < answers[1]["clock"]
    (folder,) = list_records(tmp_path / "records")
    rows = [json.loads(x) for x in (folder / ADVICE_FILE).read_text(encoding="utf-8").splitlines()]
    said = [r for r in rows if r["source"] == "coach" and r["category"] == "coach"]
    assert [r["spoken"] for r in said] == [True, True]
    # The rule lines around them were judged too, each once: nothing logged twice, nothing left out.
    texts = [r["text"] for r in log]
    assert len(texts) == len(set((r["text"], r["clock"]) for r in log))
    assert any(r["text"].startswith("Bounty runes in 15s") for r in log)
    assert Priority  # (imported for readers: the voice's cut is VoiceConfig.min_priority)


def test_the_gate_counts_game_time_not_wall_time():
    """The demo runs six times faster than a game: counted on the wall clock, the minute's budget covered six game
    minutes, and nothing the coach said got a slot."""
    from types import SimpleNamespace

    from doty import speech
    from doty.config import CoachConfig

    from .test_speech import _line

    snap = SimpleNamespace(has_hero=True, alive=True, clock=100)
    gate = speech.SpeechGate(CoachConfig(talk="normal"), state=lambda: (snap, False))
    said = []
    for clock in range(100, 400, 15):  # a line every 15 game seconds, whatever the wall clock does
        snap.clock = clock
        adv = _line(f"Line at {clock}.")
        gate.admit(adv)
        said.append(adv.speak)
    assert all(said)  # 4 a minute and a 10 s gap: one every 15 game seconds fits
    snap.clock = 50  # a new match: the old one's history doesn't count against it
    assert gate._clock() == 50 and gate._said == []
