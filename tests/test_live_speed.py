"""Live coaching, as fast as it can be: tips said the moment their sentence is done, repeats kept off the
voice, prepared calls voiced ahead, the lineup from Doty's own screen reads, and the match brief."""

import asyncio
import time

import pytest
from aiohttp.test_utils import TestServer

from doty.advice import Advice, AdviceBus, Priority
from doty.coach import Coach
from doty.coach import prompts
from doty.coach.llm import _content_words, _tip_ready, parse_watch
from doty.config import CoachConfig
from doty.knowledge.constants import Knowledge
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN, Recorder
from .fake_anthropic import FakeAnthropic

HEROES = {
    "npc_dota_hero_juggernaut": {"id": 8, "localized_name": "Juggernaut", "roles": ["Carry", "Pusher", "Escape"]},
    "npc_dota_hero_lion": {"id": 26, "localized_name": "Lion", "roles": ["Support", "Disabler"]},
    "npc_dota_hero_bane": {"id": 3, "localized_name": "Bane"},
    "npc_dota_hero_zuus": {"id": 22, "localized_name": "Zeus"},
    "npc_dota_hero_dazzle": {"id": 50, "localized_name": "Dazzle"},
    "npc_dota_hero_jakiro": {"id": 64, "localized_name": "Jakiro"},
    "npc_dota_hero_bloodseeker": {"id": 4, "localized_name": "Bloodseeker"},
    "npc_dota_hero_dragon_knight": {"id": 49, "localized_name": "Dragon Knight"},
    "npc_dota_hero_warlock": {"id": 37, "localized_name": "Warlock"},
    "npc_dota_hero_nevermore": {"id": 11, "localized_name": "Shadow Fiend"},
}


async def _wait_inflight(coach):
    for _ in range(400):
        if coach._inflight and coach._inflight[2].done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("coach request never finished")


@pytest.fixture
async def live(monkeypatch):
    fake = FakeAnthropic("QUIET: waiting", chunk_s=0.03)
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    kb = Knowledge()
    coach = Coach(CoachConfig(), store, bus, kb, screenshot=lambda *a: ("QUJD", 1.0))
    bus.subscribe(coach.note_alert)
    await coach.start()
    m = ScriptedMatch()
    for _ in range(700):  # past the scripted death, alive
        m.step(1)
    store.ingest(m.payload(TOKEN))
    yield coach, fake, rec, kb
    await coach.close()
    await server.close()


def _times(rec):
    return [(a.text, a.created_t) for a in rec.items if a.display and a.final]


# the tip goes out as soon as its sentence is done -------------------------------------------------------
async def test_a_live_tip_is_said_before_its_prepared_calls_finish_streaming(live):
    coach, fake, rec, kb = live
    tip = "Back off to your tower: three are missing from the map."
    calls = " ".join(["TRIGGER: hp < 30 => Walk back now, you're too low."] * 6)  # a long tail of extra lines
    fake.reply = f"SAY: {tip}\n{calls}"
    t0 = time.perf_counter()
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    done = time.perf_counter()
    said = [a for a in rec.items if a.display and a.final and a.text == tip]
    assert len(said) == 1 and said[0].speak and said[0].stale_s  # once, spoken, and dropped if it'd be late
    # it went out while the trigger lines were still streaming (each word 30 ms apart)
    assert said[0].created_t < done - 0.3, (said[0].created_t - t0, done - t0)
    assert len(coach.triggers.active) == 1  # the calls still arrived and were armed


async def test_drawings_that_follow_an_early_tip_join_its_card_without_saying_it_again(live):
    coach, fake, rec, kb = live
    coach.screenshot = lambda *a: ({"screen": "SCR", "minimap": "MAP", "sizes": {"screen": (1280, 536),
                                                                                  "minimap": (440, 440)}}, 1.0)
    fake.reply = 'SAY: Ward the river before the rune.\nDRAW: circle minimap 132 88 "ward here" good'
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    versions = [a for a in rec.items if a.display and a.text == "Ward the river before the rune."]
    assert len({a.id for a in versions}) == 1
    assert [a.speak for a in versions] == [True, False]  # said once; the update only adds the drawing
    assert versions[-1].marks and versions[-1].marks[0].label == "ward here"


async def test_a_tip_naming_a_hero_not_in_the_game_is_dropped_before_it_is_said(live):
    coach, fake, rec, kb = live
    kb.heroes = {**HEROES, "npc_dota_hero_pudge": {"id": 14, "localized_name": "Pudge"}}
    coach.store.memory.clear()
    snap = coach.store.current
    for _ in range(2):
        coach._screen_read(["npc_dota_hero_juggernaut", "npc_dota_hero_zuus", "npc_dota_hero_dazzle",
                            "npc_dota_hero_jakiro", "npc_dota_hero_bloodseeker"],
                           ["npc_dota_hero_lion", "npc_dota_hero_bane", "npc_dota_hero_dragon_knight",
                            "npc_dota_hero_warlock", "npc_dota_hero_nevermore"], snap.match_id)
    assert coach.store.memory["lineup_complete"]
    fake.reply = "SAY: Watch out for Pudge hooks in the river.\nTRIGGER: hp < 30 => Walk back."
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    assert not [a for a in rec.items if a.display and "Pudge" in a.text]


# repeats --------------------------------------------------------------------------------------------------
async def test_a_live_tip_that_repeats_a_recent_alert_goes_to_the_feed_only(live):
    coach, fake, rec, kb = live
    coach.bus.publish(Advice("No TP scroll — buy one", Priority.NORMAL, "survival", speech="Buy a TP scroll"))
    fake.reply = "SAY: Buy a TP scroll now with your 347 gold."
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    tip = [a for a in rec.items if a.display and a.final and a.source == "coach"][-1]
    assert tip.text == "Buy a TP scroll now with your 347 gold."
    assert not tip.speak and tip.category == "watching"  # noted, not said again

    rec.items.clear()
    fake.reply = "SAY: Farm the bottom triangle until Battle Fury."
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    assert [a for a in rec.items if a.display and a.final][-1].speak  # something new is said


def test_content_words_ignore_filler_and_fold_plurals():
    assert _content_words("Bounty runes in 15s (0:00)") == {"bounty", "rune"}
    assert _content_words("Buy a TP scroll") == {"buy", "tp", "scroll"}


# parsing ----------------------------------------------------------------------------------------------------
def test_a_self_corrected_live_reply_keeps_the_last_answer():
    reply = ("SAY: Spend 468 gold on a TP scroll. Bounty rune spawns at 0:00.\n\n"
             "Wait, that conflicts with the clock. Correcting:\n\nSAY: Learn Bulldoze, then back off.")
    assert parse_watch(reply) == (True, "Learn Bulldoze, then back off.")
    assert parse_watch("SAY: Charge Sniper now. Wait, correct that: Charge Lion.") == (True, "Charge Sniper now.")
    assert parse_watch("QUIET: nothing urgent right now.") == (False, "nothing urgent right now.")


def test_tip_ready_waits_until_the_extra_lines_begin():
    assert _tip_ready("SAY: Back off to") is None
    assert _tip_ready("SAY: Back off to your tower.\nTRIG") is None  # not sure yet what comes next
    assert _tip_ready("SAY: Back off to your tower.\nTRIGGER: hp") == "SAY: Back off to your tower."
    assert _tip_ready("SAY:\nBack off.\nDRAW: circle") == "SAY:\nBack off."
    assert _tip_ready("QUIET:\nTRIGGER: hp < 30 => Go.\n") == "QUIET:\n"  # no tip at all: nothing to say
    # a tip that goes on, or corrects itself, is judged whole at the end
    assert _tip_ready("SAY: Spend 468 gold on a TP scroll.\n\nWait, that conflicts. SAY: Learn Bulldoze.") is None
    assert _tip_ready("SAY: Back off to your tower,\nthree are missing.\nTRIGGER: hp") is None


async def test_a_self_corrected_live_reply_is_said_once_as_corrected(live):
    coach, fake, rec, kb = live
    fake.reply = ("SAY: Spend 468 gold on a TP scroll.\n\nWait, that conflicts with the clock. Correcting:\n\n"
                  "SAY: Learn Bulldoze, then back off.\nTRIGGER: hp < 30 => Walk back now.")
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    said = [a.text for a in rec.items if a.display and a.final and a.speak]
    assert said == ["Learn Bulldoze, then back off."]
    assert coach.cached.text == "Learn Bulldoze, then back off."


def test_a_short_alert_does_not_silence_a_tip_that_says_more(live):
    coach, fake, rec, kb = live
    coach.note_alert(Advice("Power rune in 15s (4:00)", Priority.NORMAL, "timer", speech="Power rune in 15"))
    assert not coach._repeats("Take the power rune at 4:00, then gank bottom with Lion.")
    assert coach._repeats("Grab the power rune.")
    coach.note_alert(Advice("Dead for 30s with 1500 gold", Priority.NORMAL, "economy",
                            speech="Spend your gold while dead"))
    assert not coach._repeats("Spend gold on Black King Bar.")


async def test_the_opening_look_never_cuts_off_an_answer_being_given(live):
    coach, fake, rec, kb = live
    fake.reply = "Farm the safe lane until Battle Fury, then join fights."
    m = ScriptedMatch()
    m.step(70)
    snap = coach.store.ingest(m.payload(TOKEN))  # -0:20: the opening look is due
    coach._started_match = None
    coach._question_on_loop("what should I buy?", None)
    coach.on_snapshot(snap)
    assert coach._started_match is None  # waits for the answer
    await _wait_inflight(coach)
    assert [a for a in rec.items if a.display and a.final and a.text.startswith("Farm the safe lane")]
    coach.on_snapshot(snap)
    assert coach._started_match == coach._match_key(snap)
    await _wait_inflight(coach)


# the opening look waits for the lineup ---------------------------------------------------------------------
async def test_the_opening_look_waits_for_the_lineup_until_shortly_before_the_horn(live):
    coach, fake, rec, kb = live
    m = ScriptedMatch()
    snap = coach.store.ingest(m.payload(TOKEN))  # -1:30, nobody known yet
    coach._started_match = None
    coach.on_snapshot(snap)
    assert coach._started_match is None  # waits
    m.step(65)
    snap = coach.store.ingest(m.payload(TOKEN))  # -0:25
    coach.on_snapshot(snap)
    assert coach._started_match == coach._match_key(snap)
    await _wait_inflight(coach)


# prepared calls ---------------------------------------------------------------------------------------------
async def test_prepared_calls_are_handed_to_the_voice_and_cut_in_when_they_fire(live):
    coach, fake, rec, kb = live
    armed = []
    coach.on_armed = armed.extend
    fake.reply = "Farm the safe lane.\nTRIGGER: hp < 30 => Walk back now, you're too low to stay."
    coach._ask_on_loop("plan")
    await _wait_inflight(coach)
    assert armed == ["Walk back now, you're too low to stay."]
    raw = coach.store.current.raw
    hero = dict(raw["hero"])
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": hero["max_health"]}}))
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": int(hero["max_health"] * 0.2)}}))
    fired = [a for a in rec.items if a.category == "trigger"]
    assert fired and fired[0].cut_in and fired[0].stale_s


# the lineup from the screen ---------------------------------------------------------------------------------
async def test_screen_reads_confirm_the_lineup_and_fetch_enemy_spikes(live, monkeypatch):
    coach, fake, rec, kb = live
    kb.heroes = dict(HEROES)
    coach.store.memory.clear()
    fetched = []

    async def ensure_enemy(hero_id):
        fetched.append(hero_id)

    monkeypatch.setattr(kb, "ensure_enemy", ensure_enemy)
    snap = coach.store.current
    radiant = ["npc_dota_hero_juggernaut", "npc_dota_hero_zuus", "npc_dota_hero_dazzle"]
    dire = ["npc_dota_hero_lion", "npc_dota_hero_bane"]
    coach.screen_read(radiant, dire, snap.match_id)  # (thread-safe: lands on the loop)
    await asyncio.sleep(0.05)
    assert coach.store.memory["lineup"]["dire"] == []  # one read isn't enough
    coach.screen_read(radiant, dire, snap.match_id)
    await asyncio.sleep(0.05)
    team = snap.team.lower()
    enemies = coach.store.memory["lineup"]["dire" if team == "radiant" else "radiant"]
    assert set(enemies) == ({"Lion", "Bane"} if team == "radiant" else {"Zeus", "Dazzle"} | {"Juggernaut"} - {
        kb.hero_name(snap.hero_name)})
    assert fetched  # their build data is on its way, for the brief and the spike calls
    coach.screen_read(["npc_dota_hero_lion"] * 3, dire, "another-match")  # a stale read from another match
    await asyncio.sleep(0.05)
    assert coach.store.memory["lineup"]["dire" if team == "radiant" else "radiant"] == enemies


# the match brief ----------------------------------------------------------------------------------------------
def _brief_kb():
    kb = Knowledge()
    kb.heroes = dict(HEROES)
    kb.hero_abilities = {"npc_dota_hero_lion": {"abilities": ["lion_impale", "lion_finger_of_death", "lion_hidden"]}}
    kb.ability_info = {
        "lion_impale": {"dname": "Earth Spike", "desc": "Rock spikes burst from the earth along a straight path. "
                        "Units are hurled into the air.", "cd": 12.0, "bkb": False},
        "lion_finger_of_death": {"dname": "Finger of Death", "desc": "Rips at an enemy unit.", "cd": 100.0,
                                 "bkb": True},
        "lion_hidden": {"dname": "Hidden", "desc": "", "hidden": True},
    }
    kb.items = {"item_blink": {"dname": "Blink Dagger", "cost": 2250, "id": 1, "components": []}}
    kb.item_popularity[26] = {"early_game_items": {"1": 50}}
    kb.item_timings[(26, "item_blink")] = [{"hero_id": 26, "time": 900, "games": 80}]
    return kb


def test_match_brief_names_the_heroes_and_what_each_enemy_does():
    kb = _brief_kb()
    brief = prompts.match_brief({"radiant": ["Juggernaut", "Zeus"], "dire": ["Lion"]}, "radiant",
                                "npc_dota_hero_juggernaut", kb, complete=False)
    assert "Your team (Radiant): Juggernaut (you), Zeus." in brief
    assert "Enemies (Dire): Lion. (Not all known yet.)" in brief
    assert "- Earth Spike: Rock spikes burst from the earth along a straight path. (12 s cooldown)" in brief
    assert "Finger of Death" in brief and "goes through BKB" in brief and "Hidden" not in brief
    assert "Usually finishes: Blink Dagger by ~15:00" in brief
    assert prompts.match_brief({"radiant": ["Zeus"], "dire": []}, "radiant", "", kb) == ""  # no enemies yet


def test_the_brief_goes_first_and_is_cached():
    content = prompts.user_content("watch", {"clock": "10:00"}, 18, "QUJD", brief="Match brief: ...")
    assert content[0] == {"type": "text", "text": "Match brief: ...", "cache_control": {"type": "ephemeral"}}
    assert content[1]["type"] == "image"
    assert prompts.user_content("watch", {}, 18, "QUJD")[0]["type"] == "image"  # none: as before


async def test_live_looks_send_the_brief_and_a_one_sentence_limit(live):
    coach, fake, rec, kb = live
    coach.store.memory["lineup"] = {"radiant": ["Juggernaut"], "dire": ["Lion"]}
    kb.heroes = dict(HEROES)
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    blocks = fake.requests[-1]["messages"][0]["content"]
    assert blocks[0]["text"].startswith("Match brief") and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "one sentence, at most 14 words" in blocks[-1]["text"]
    from doty.coach.llm import WATCH_MAX_TOKENS

    assert fake.requests[-1]["max_tokens"] == WATCH_MAX_TOKENS  # a rambling reply is cut off early


async def test_a_black_capture_is_named_instead_of_sent(live):
    coach, fake, rec, kb = live
    coach.screenshot = lambda *a: ({"unavailable": "no screenshot this time", "sizes": {}}, 1.0)
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    blocks = fake.requests[-1]["messages"][0]["content"]
    assert not [b for b in blocks if b["type"] == "image"]
    assert '"screenshot":"no screenshot this time"' in next(b["text"] for b in blocks if b["text"].startswith("Game"))
    n = len(fake.requests)
    coach._launch("watch", visible=False, screen_only=True)  # from the screen alone: nothing to look at
    await asyncio.sleep(0.2)
    assert len(fake.requests) == n


def test_dead_now_comes_from_a_fresh_screen_reading():
    kb = Knowledge()
    kb.heroes = dict(HEROES)
    store = GameStateStore()
    store.memory["screen"] = {"t": time.monotonic(), "match": "", "dead": {"radiant": ["npc_dota_hero_zuus"],
                                                               "dire": ["npc_dota_hero_lion", "npc_dota_hero_bane"]}}
    assert prompts.dead_now(store, "radiant", kb) == {"allies": ["Zeus"], "enemies": ["Lion", "Bane"]}
    store.memory["screen"]["t"] -= 10
    assert prompts.dead_now(store, "radiant", kb) == {}


def test_connection_resets_are_not_reported_as_errors():
    from doty.app import _quiet_resets

    reported = []

    class Loop:
        def default_exception_handler(self, context):
            reported.append(context)

    _quiet_resets(Loop(), {"message": "Exception in callback", "exception": ConnectionResetError(10054)})
    assert reported == []
    _quiet_resets(Loop(), {"message": "boom", "exception": ValueError("real")})
    assert len(reported) == 1


def test_a_pick_off_on_the_top_bar_is_a_moment_to_look(live):
    coach, fake, rec, kb = live
    sig = {}
    coach.signals = lambda: dict(sig)
    snap, prev = coach.store.current, coach.store.current
    coach._was_fighting = False
    sig.update(enemies_dead=1, allies_dead=0)
    assert coach._calm_moment(snap, prev, False) == ""
    sig["enemies_dead"] = 2
    assert coach._calm_moment(snap, prev, True) == ""  # mid-fight: it waits...
    assert coach._calm_moment(snap, prev, False).startswith("2 enemies are dead")  # ...and counts once it's over
    assert coach._calm_moment(snap, prev, False) == ""  # once
