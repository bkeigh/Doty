"""Quicker and simpler: spawns at the same time share one line, only the fight is talked about mid-fight,
live looks don't think before the tip, the first look of a match reads a warm cache, urgent calls are said
faster, and a launch without a match leaves no empty log."""

import asyncio

from doty.advice import AdviceBus
from doty.coach.backends import claude_request
from doty.config import CoachConfig, Config, RulesConfig, VoiceConfig
from doty.eleven import PhraseCache
from doty.rules import RuleEngine
from doty.rules.timers import joined, timer_lines
from doty.state import GameStateStore
from doty.voice import URGENT_PACE, Voice

from .conftest import Recorder
from .test_live_speed import _wait_inflight, live  # noqa: F401  (the fixture)


def _snap(store, t, clock, hp=1000):
    return store.ingest({"map": {"matchid": "1", "clock_time": clock,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": 0},
                         "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "health": hp,
                                  "max_health": 1000, "xpos": 0, "ypos": 0, "level": 8},
                         "items": {}}, received_t=t)


def _engine():
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    return store, RuleEngine(store, bus, RulesConfig()), rec


def test_runes_at_the_same_time_are_one_line():
    store, engine, rec = _engine()
    engine.on_snapshot(_snap(store, 1.0, 465))  # 7:45: bounty and power runes both at 8:00
    timers = [a for a in rec.items if a.category == "timer"]
    assert [a.spoken for a in timers] == ["Bounty and power runes in 15"]
    engine.on_snapshot(_snap(store, 2.0, 466))
    assert len([a for a in rec.items if a.category == "timer"]) == 1  # once
    assert joined(["Power rune", "Wisdom shrines"]) == "Power rune and wisdom shrines"
    assert "Bounty and power runes in 15" in timer_lines(RulesConfig())  # voiced ahead of time


def test_mid_fight_a_timer_shows_but_isnt_said():
    store, engine, rec = _engine()
    engine.on_snapshot(_snap(store, 1.0, 340, hp=1000))
    engine.on_snapshot(_snap(store, 2.0, 341, hp=820))  # being hit: a fight
    engine.on_snapshot(_snap(store, 3.0, 346, hp=800))  # 5:46: power rune (and lotus) at 6:00
    timer = [a for a in rec.items if a.category == "timer"]
    assert timer and not any(a.speak for a in timer) and all(a.display for a in timer)


def test_live_looks_on_the_fast_model_dont_think_first():
    cfg = CoachConfig()
    assert claude_request(cfg, "s", [], "watch")["thinking"] == {"type": "disabled"}
    assert "thinking" not in claude_request(cfg, "s", [], "now")  # asks and plans think as before
    assert "thinking" not in claude_request(CoachConfig(effort="max"), "s", [], "watch")  # (not allowed there)
    assert "thinking" not in claude_request(CoachConfig(watch_model="claude-sonnet-5-5"), "s", [], "watch")


async def test_the_first_look_of_a_match_reads_a_warm_cache(live):
    coach, fake, rec, kb = live
    coach._prewarm_watch(coach.store.current)
    for _ in range(100):
        if fake.requests:
            break
        await asyncio.sleep(0.01)
    warm = fake.requests[-1]
    assert warm["max_tokens"] == 0 and not warm.get("stream")
    assert warm["model"] == "claude-haiku-5-5" and warm["thinking"] == {"type": "disabled"}
    assert warm["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The live look that follows has the same prefix (model, system, thinking): it hits what was written.
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    look = fake.requests[-1]
    assert look["model"] == warm["model"] and look["system"] == warm["system"] and look["thinking"] == warm["thinking"]


def test_urgent_calls_are_said_faster(tmp_path):
    v = Voice(VoiceConfig(), cache=PhraseCache(tmp_path))
    v._kokoro = type("K", (), {"model_id": "kokoro-test"})()
    v.urgent = frozenset({"Glimmer yourself"})
    fast, calm = v._kokoro_key("Glimmer yourself")[2], v._kokoro_key("Bounty runes in 15")[2]
    assert abs(fast / calm - URGENT_PACE) < 1e-6


def test_a_launch_without_a_match_leaves_no_empty_log(tmp_path, monkeypatch):
    from doty import app as appmod

    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path / "logs")
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    assert not (tmp_path / "logs").exists() or not list((tmp_path / "logs").glob("session-*.jsonl"))
    from doty.advice import Advice

    doty.bus.publish(Advice("Bounty runes in 15", category="timer"))
    assert len(list((tmp_path / "logs").glob("session-*.jsonl"))) == 1
    doty._log.close()


def test_what_a_look_costs_is_counted_at_anthropics_prices():
    from doty.coach.backends import cost_usd

    # A typical live look on Haiku 5.5: 1,500 new tokens, 5,500 read from the cache, 200 written out.
    usd = cost_usd("claude-haiku-5-5", {"in": 1500, "cached": 5500, "out": 200})
    assert abs(usd - (1500 * 0.10 + 5500 * 0.01 + 200 * 0.50) / 1e6) < 1e-12 and usd < 0.001
    assert cost_usd("gpt-something", {"in": 10}) is None  # (other providers: tokens only)


async def test_the_match_tally_and_a_skipped_look_when_nothing_changed(live):
    coach, fake, rec, kb = live
    fake.reply = "QUIET: Keep farming the safe lane."
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    assert coach.spend["looks"] == 1 and coach.spend["usd"] > 0  # (the fake answers as claude-haiku-5-5)
    assert coach._quiet_sig is not None  # nothing to say: remembered
    snap = coach.store.current
    raw = snap.raw
    hero = dict(raw["hero"], alive=True, health=raw["hero"]["max_health"])
    alive = coach.store.ingest({**raw, "hero": hero})
    coach._quiet_sig = coach.situation(alive)  # (the same situation as that quiet look)
    coach._last_spec_clock, coach._last_spec_wall = alive.clock - 60, 0.0
    before = len(fake.requests)
    coach.on_snapshot(alive)
    assert coach.spend["skipped"] == 1 and len(fake.requests) == before  # same situation: no new look
    coach._last_spec_clock, coach._last_spec_wall = alive.clock - 60, 0.0
    coach.on_snapshot(coach.store.ingest({**raw, "hero": dict(hero, level=hero.get("level", 1) + 1)}))
    await _wait_inflight(coach)
    assert len(fake.requests) == before + 1  # a level up: the next look goes ahead
