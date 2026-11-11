"""End-to-end coach test against a local fake of the streaming Messages API."""

import asyncio
import time

import pytest
from aiohttp.test_utils import TestServer

from doty.advice import AdviceBus
from doty.coach import Coach
from doty.config import CoachConfig
from doty.knowledge.constants import Knowledge
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN, Recorder
from .fake_anthropic import FakeAnthropic

REPLY = "Farm the bottom jungle until Battle Fury. Then join your team at the Radiant tier two."


@pytest.fixture
async def coach_env(monkeypatch):
    fake = FakeAnthropic(REPLY)
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    kb = Knowledge()
    kb.items = {"item_bfury": {"cost": 3900}}
    coach = Coach(CoachConfig(), store, bus, kb, screenshot=lambda: ("QUJD", 1.0))
    await coach.start()
    m = ScriptedMatch()
    m.step(600)
    store.ingest(m.payload(TOKEN))
    yield coach, fake, rec, kb
    await coach.close()
    await server.close()


async def _wait_inflight(coach):
    for _ in range(200):
        if coach._inflight and coach._inflight[2].done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("coach request never finished")


async def test_streams_card_then_speaks_each_sentence(coach_env):
    coach, fake, rec, kb = coach_env
    coach._ask_on_loop("now")
    await _wait_inflight(coach)

    cards = [a for a in rec.items if a.display]
    spoken = [a.text for a in rec.items if not a.display]
    assert cards[0].text == "…" and not cards[0].final
    assert cards[-1].final and cards[-1].text == REPLY
    assert len({c.id for c in cards}) == 1  # one card updated in place
    assert spoken == ["Farm the bottom jungle until Battle Fury.",
                      "Then join your team at the Radiant tier two."]
    assert coach.last_ttft_ms is not None and coach.last_ttft_ms < 2000

    req = fake.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["fallbacks"] == "default"
    assert req["messages"][0]["content"][0]["type"] == "image"
    assert "server-side-fallback-2026-07-01" in fake.headers[0].get("anthropic-beta", "")


async def test_second_ask_is_instant_from_cache(coach_env):
    coach, fake, rec, kb = coach_env
    coach._ask_on_loop("now")
    await _wait_inflight(coach)
    rec.items.clear()

    t0 = time.perf_counter()
    coach._ask_on_loop("now")
    instant_ms = (time.perf_counter() - t0) * 1000
    first_card = next(a for a in rec.items if a.display)
    assert first_card.text == REPLY and first_card.final
    assert instant_ms < 50, instant_ms  # no network on the hot path
    await _wait_inflight(coach)  # background refresh ran too
    assert len(fake.requests) == 2


async def test_spoken_question_uses_its_screenshot_and_remembers(coach_env):
    coach, fake, rec, kb = coach_env
    coach._question_on_loop("Should I buy BKB or Manta?", "PRESSSHOT")
    await _wait_inflight(coach)
    req = fake.requests[-1]
    blocks = req["messages"][0]["content"]
    assert blocks[0]["source"]["data"] == "PRESSSHOT"  # the screen when you started asking
    assert any(b.get("text") == 'The player asks: "Should I buy BKB or Manta?"' for b in blocks)
    assert list(coach.history) == [("Should I buy BKB or Manta?", REPLY)]

    coach._question_on_loop("What about after that?", "SHOT2")
    await _wait_inflight(coach)
    state = next(b["text"] for b in fake.requests[-1]["messages"][0]["content"] if b.get("text", "").startswith("Game"))
    assert "Should I buy BKB or Manta?" in state  # follow-ups see the previous Q&A


async def test_question_works_outside_a_match(coach_env):
    coach, fake, rec, kb = coach_env
    coach.store.reset()
    coach._question_on_loop("Who counters Pudge?", None)
    await _wait_inflight(coach)
    state = next(b["text"] for b in fake.requests[-1]["messages"][0]["content"] if b.get("text", "").startswith("Game"))
    assert "not in a match" in state
    assert [a for a in rec.items if a.display and a.final][-1].text == REPLY


async def test_plan_build_line_feeds_rule_engine(coach_env, monkeypatch):
    coach, fake, rec, kb = coach_env
    fake.reply = "Farm safely and hit Battle Fury by 14 minutes.\nBUILD: item_bfury"
    coach._ask_on_loop("plan")
    await _wait_inflight(coach)
    final = [a for a in rec.items if a.display and a.final][-1]
    assert "BUILD" not in final.text
    assert kb.custom_plan["npc_dota_hero_juggernaut"] == ["item_bfury"]


async def test_live_coaching_speaks_up_only_when_it_matters(coach_env):
    coach, fake, rec, kb = coach_env
    m = ScriptedMatch()
    for _ in range(700):  # step gradually so the scripted death has respawned
        m.step(1)
    coach.store.ingest(m.payload(TOKEN))
    assert coach.store.current.alive
    fake.reply = "SAY: Back off to tower, three enemies are missing from the map."
    coach.on_snapshot(coach.store.current)  # the 30-second look is due
    await _wait_inflight(coach)
    assert fake.requests[-1]["messages"][0]["content"][-1]["text"].startswith("You are watching this game live")
    said = [a for a in rec.items if a.display and a.final]
    assert said[-1].text == "Back off to tower, three enemies are missing from the map."
    assert said[-1].speak and said[-1].overlay and said[-1].source == "coach"
    assert coach.cached.text == said[-1].text  # Ask is instant afterwards

    rec.items.clear()
    fake.reply = "QUIET: Keep farming the bottom triangle until Battle Fury."
    coach._last_spec_wall = 0.0
    coach._last_spec_clock = -999
    coach.on_snapshot(coach.store.current)
    await _wait_inflight(coach)
    quiet = [a for a in rec.items if a.display][-1]
    assert quiet.text == "Keep farming the bottom triangle until Battle Fury."
    assert not quiet.speak and not quiet.overlay and quiet.category == "watching"
    state = next(b["text"] for b in fake.requests[-1]["messages"][0]["content"] if b.get("text", "").startswith("Game"))
    assert "three enemies are missing" in state  # it knows what it already said


def test_parse_watch():
    from doty.coach.llm import parse_watch

    assert parse_watch("SAY: Go now.") == (True, "Go now.")
    assert parse_watch("quiet:  farm") == (False, "farm")
    assert parse_watch("No tag at all.") == (False, "No tag at all.")


def _content_text(req):
    return next(b["text"] for b in req["messages"][0]["content"] if b.get("text", "").startswith("Game"))


async def test_lineup_line_is_hidden_and_feeds_enemies_and_matchups(coach_env):
    coach, fake, rec, kb = coach_env
    kb.heroes = {
        "npc_dota_hero_juggernaut": {"id": 8, "localized_name": "Juggernaut"},
        "npc_dota_hero_lion": {"id": 26, "localized_name": "Lion"},
        "npc_dota_hero_lina": {"id": 25, "localized_name": "Lina"},
        "npc_dota_hero_axe": {"id": 2, "localized_name": "Axe"},
        "npc_dota_hero_crystal_maiden": {"id": 5, "localized_name": "Crystal Maiden"},
        "npc_dota_hero_phantom_assassin": {"id": 44, "localized_name": "Phantom Assassin"},
        "npc_dota_hero_sniper": {"id": 35, "localized_name": "Sniper"},
    }
    kb.matchups[8] = [{"hero_id": 26, "games_played": 400, "wins": 180}, {"hero_id": 44, "games_played": 300,
                                                                           "wins": 165}]
    fake.reply = ("Buy a Black King Bar next; Lion and Lina can chain-disable you.\n"
                  "LINEUP: radiant=Juggernaut, CM, Axe | dire=Lion, Lina, PA, Sniper")
    coach._ask_on_loop("now")
    await _wait_inflight(coach)
    card = [a for a in rec.items if a.display and a.final][-1]
    assert "LINEUP" not in card.text and card.text.startswith("Buy a Black King Bar")
    # One read isn't trusted yet: only the player's own hero (from the game data) is confirmed.
    assert coach.store.memory["lineup"] == {"radiant": ["Juggernaut"], "dire": []}
    coach._ask_on_loop("plan")  # a second, agreeing read confirms the rest
    await _wait_inflight(coach)
    assert coach.store.memory["lineup"] == {"radiant": ["Juggernaut", "Crystal Maiden", "Axe"],
                                            "dire": ["Lion", "Lina", "Phantom Assassin", "Sniper"]}
    assert coach.store.memory["lineup_complete"] is False
    coach._ask_on_loop("plan")
    await _wait_inflight(coach)
    state = _content_text(fake.requests[-1])
    assert '"enemies":["Lion","Lina","Phantom Assassin","Sniper"]' in state
    assert '"vs":"Lion","win_rate":"45%"' in state and '"vs":"Phantom Assassin","win_rate":"55%"' in state


async def test_draft_looks_suggest_picks(coach_env):
    coach, fake, rec, kb = coach_env
    coach.store.reset()
    snap = coach.store.ingest({"map": {"game_state": "DOTA_GAMERULES_STATE_HERO_SELECTION", "clock_time": -60},
                               "player": {"team_name": "radiant"}})
    assert snap.in_draft
    fake.reply = "SAY: Pick Axe, Legion Commander or Mars; they punish Anti-Mage and Sniper."
    coach.on_snapshot(snap)
    await _wait_inflight(coach)
    req = fake.requests[-1]
    assert req["messages"][0]["content"][-1]["text"].startswith("It's hero selection")
    assert '"status":"hero selection (draft)"' in _content_text(req)
    said = [a for a in rec.items if a.display and a.final][-1]
    assert said.text.startswith("Pick Axe") and said.speak
    n = len(fake.requests)
    coach.on_snapshot(snap)  # too soon for another draft look
    await asyncio.sleep(0.05)
    assert len(fake.requests) == n


@pytest.mark.parametrize("mode,reply,spoken", [
    ("never", "SAY: Back off now.", False),
    ("every", "QUIET: Farm the bottom camps.", True),
    ("important", "QUIET: Farm the bottom camps.", False),
])
async def test_speak_up_modes(coach_env, mode, reply, spoken):
    coach, fake, rec, kb = coach_env
    m = ScriptedMatch()
    for _ in range(700):
        m.step(1)
    coach.store.ingest(m.payload(TOKEN))
    coach.cfg.speak_up = mode
    fake.reply = reply
    coach.on_snapshot(coach.store.current)
    await _wait_inflight(coach)
    out = [a for a in rec.items if a.display][-1]
    assert out.speak is spoken and out.overlay is spoken


async def test_screen_only_look_uses_screenshots_and_crops(coach_env):
    coach, fake, rec, kb = coach_env
    coach.store.reset()
    coach.screenshot = lambda: ({"screen": "SCR", "minimap": "MAP", "topbar": "TOP"}, 1.0)
    fake.reply = "SAY: Three enemies are missing from the minimap; back off to your tower."
    coach._launch("watch", visible=False, screen_only=True)
    await _wait_inflight(coach)
    blocks = fake.requests[-1]["messages"][0]["content"]
    assert [b["source"]["data"] for b in blocks if b["type"] == "image"] == ["SCR", "MAP", "TOP"]
    assert any(b.get("text", "").startswith("Minimap") for b in blocks)
    assert "no game-data feed" in _content_text(fake.requests[-1])
    assert [a for a in rec.items if a.display][-1].text.startswith("Three enemies")

    rec.items.clear()
    fake.reply = "QUIET: waiting"  # in the menus: nothing to show
    coach._launch("watch", visible=False, screen_only=True)
    await _wait_inflight(coach)
    assert not [a for a in rec.items if a.display]


def test_find_hero_handles_screen_and_spoken_names():
    kb = Knowledge()
    kb.heroes = {"npc_dota_hero_antimage": {"id": 1, "localized_name": "Anti-Mage"},
                 "npc_dota_hero_furion": {"id": 53, "localized_name": "Nature's Prophet"},
                 "npc_dota_hero_nevermore": {"id": 11, "localized_name": "Shadow Fiend"}}
    assert kb.find_hero("Anti-Mage") == kb.find_hero("antimage") == kb.find_hero("AM") == "npc_dota_hero_antimage"
    assert kb.find_hero("Natures Prophet") == kb.find_hero("NP") == "npc_dota_hero_furion"
    assert kb.find_hero("SF") == "npc_dota_hero_nevermore"
    assert kb.find_hero("Nobody") is None


async def test_coach_drawings_are_hidden_from_text_and_attached_as_marks(coach_env):
    coach, fake, rec, kb = coach_env
    coach.store.reset()
    coach.screenshot = lambda: ({"screen": "SCR", "minimap": "MAP", "sizes": {"screen": (1280, 536),
                                                                              "minimap": (440, 440)}}, 1.0)
    fake.reply = ('SAY: Three are missing; back off to your tower.\n'
                  'DRAW: circle minimap 132 88 "3 missing" danger\n'
                  'DRAW: arrow screen 640 300 320 400 "back"')
    coach._launch("watch", visible=False, screen_only=True)
    await _wait_inflight(coach)
    req = fake.requests[-1]
    texts = " ".join(b.get("text", "") for b in req["messages"][0]["content"])
    assert "1280x536 px" in texts and "440x440 px" in texts  # image sizes, for the coordinates
    assert "DRAW:" in req["system"][0]["text"]
    shown = [a for a in rec.items if a.display][-1]
    assert shown.text == "Three are missing; back off to your tower." and "DRAW" not in shown.text
    assert [(m.shape, m.space, m.tone) for m in shown.marks] == [("circle", "minimap", "danger"),
                                                                 ("arrow", "screen", "coach")]
    assert shown.marks[0].x == pytest.approx(0.3)


async def test_draft_line_is_read_and_stats_feed_the_next_look(coach_env):
    from doty.knowledge.stats import Stats

    coach, fake, rec, kb = coach_env
    stats = Stats(fetch=lambda url: pytest.fail("no network"))
    stats.heroes = {26: {"id": 26, "localized_name": "Lion", "pub_pick": 100, "pub_win": 50, "roles": ["Support"]},
                    8: {"id": 8, "localized_name": "Juggernaut", "pub_pick": 100, "pub_win": 52, "roles": ["Carry"]}}
    stats._index = {"lion": 26, "juggernaut": 8}
    stats._matchups[26] = [{"hero_id": 8, "games_played": 1000, "wins": 400}]
    stats.loaded = True
    coach.stats = stats
    fake.reply = "SAY: Pick Juggernaut: Lion can't stop Blade Fury.\nDRAFT: you=radiant | radiant= | dire=Lion | bans="
    coach._launch("draft", visible=False)
    await _wait_inflight(coach)
    assert coach.store.memory["draft"]["dire"] == ["Lion"] and coach.store.memory["draft"]["you"] == "radiant"
    assert "DRAFT" not in [a for a in rec.items if a.display][-1].text
    coach._launch("draft", visible=False)
    await _wait_inflight(coach)
    assert "counter_picks_from_stats" in _content_text(fake.requests[-1]) and "Juggernaut (+" in _content_text(fake.requests[-1])


async def test_prepared_calls_are_hidden_armed_and_fired_by_doty(coach_env):
    coach, fake, rec, kb = coach_env
    fake.reply = ("Farm the safe lane until your level 6.\n"
                  "TRIGGER: hp < 30 => Walk back now, you're too low to stay.")
    coach._ask_on_loop("now")
    await _wait_inflight(coach)
    card = [a for a in rec.items if a.display and a.final][-1]
    assert "TRIGGER" not in card.text and len(coach.triggers.active) == 1
    snap = coach.store.current
    raw = snap.raw
    hero = dict(raw["hero"])  # (the scripted hero is dead at 10:00: on the death screen, nothing fires)
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "alive": False, "health": 0}}))
    assert not [a for a in rec.items if a.category == "trigger"]
    hero["alive"] = True
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": hero["max_health"]}}))
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": int(hero["max_health"] * 0.2)}}))
    fired = [a for a in rec.items if a.category == "trigger"]
    assert [a.text for a in fired] == ["Walk back now, you're too low to stay."] and fired[0].speak


async def test_live_looks_use_the_fast_model_and_a_smaller_screenshot(coach_env):
    coach, fake, rec, kb = coach_env
    widths = []
    coach.screenshot = lambda width=None: (widths.append(width), ("QUJD", 1.0))[1]
    fake.reply = "QUIET: farm on."
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    coach._launch("now", visible=True)
    await _wait_inflight(coach)
    models = [r["model"] for r in fake.requests[-2:]]
    assert models == ["claude-haiku-5-5", "claude-sonnet-5-5"]  # live look fast; the ask on the main model
    assert widths == [1024, None]


def test_calm_moments_prompt_a_look(coach_env):
    coach, fake, rec, kb = coach_env
    from doty.state import Snapshot

    def snap(alive=True, level=7, items=()):
        return Snapshot({"hero": {"name": "npc_dota_hero_axe", "alive": alive, "level": level, "health": 900,
                                  "max_health": 1000},
                         "items": {f"slot{i}": {"name": n} for i, n in enumerate(items)}})

    kb.items = {"item_blink": {"cost": 2250, "dname": "Blink Dagger"}}
    coach.store.just_respawned = True  # (the store saw a real death end)
    assert coach._calm_moment(snap(), snap(alive=False), False) == "the player just respawned"
    coach.store.just_respawned = False
    assert coach._calm_moment(snap(), snap(alive=False), False) == ""  # an Aegis revive: not a respawn
    assert coach._calm_moment(snap(level=6), snap(level=5), False) == "the player just reached level 6"
    assert coach._calm_moment(snap(items=("item_blink",)), snap(), False) == "the player just finished Blink Dagger"
    coach.store.just_respawned = True
    assert coach._calm_moment(snap(), snap(alive=False), True) == ""  # never mid-fight


async def test_wrong_tip_saves_what_the_coach_saw(coach_env, tmp_path):
    import json

    coach, fake, rec, kb = coach_env
    coach._ask_on_loop("now")
    await _wait_inflight(coach)
    card = [a for a in rec.items if a.display and a.final][-1]
    folder = coach.report(card.id, card.text, tmp_path)
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["shown"] == card.text and report["kind"] == "now" and "clock" in report["state"]
    assert (folder / "screen.jpg").read_bytes() == b"ABC"  # the screenshot it was sent
    assert coach.report(999999, "old line", tmp_path) is None  # not kept: says so instead of guessing


async def test_a_new_match_starts_without_last_games_tip_or_rhythm(coach_env):
    """Ask must never replay a tip from the match before, and the 30 s look rhythm starts over."""
    from doty.coach.llm import Cached

    coach, fake, rec, kb = coach_env
    snap = coach.store.current
    coach._same_match(snap)
    coach.cached = Cached("Push the tier two with your team.", 2310, True, 0.0)
    coach._last_spec_clock = 2310
    coach.said.append("Last game's line")
    raw = dict(snap.raw)
    new = coach.store.ingest({**raw, "map": {**raw["map"], "matchid": "next-match", "clock_time": 60}})
    coach.on_snapshot(new)
    assert coach.cached is None and not coach.said
    assert coach._last_spec_clock in (None, 60)  # unset, or already the new match's first look


async def test_a_visible_draft_answer_is_one_card_spoken_once_without_its_tag(coach_env):
    coach, fake, rec, kb = coach_env
    fake.reply = "SAY: Pick Axe. He punishes Anti-Mage."
    coach._launch("draft", visible=True)
    await _wait_inflight(coach)
    cards = [a for a in rec.items if a.display]
    spoken = [a.text for a in rec.items if not a.display]
    assert len({c.id for c in cards}) == 1 and not any("SAY" in c.text for c in cards)
    assert cards[-1].text == "Pick Axe. He punishes Anti-Mage."
    assert spoken == ["Pick Axe.", "He punishes Anti-Mage."]


async def test_waiting_is_never_the_cached_answer(coach_env):
    coach, fake, rec, kb = coach_env
    fake.reply = "QUIET: waiting"
    coach._launch("watch", visible=False)
    await _wait_inflight(coach)
    assert coach.cached is None


async def test_prepared_calls_get_real_item_keys_or_are_dropped(coach_env):
    coach, fake, rec, kb = coach_env
    fake.reply = ("Farm.\nTRIGGER: gold >= 4100 and no battle_fury => Buy Battle Fury now.\n"
                  "TRIGGER: gold >= 2250 and no blink_dagger => Buy Blink now.")
    coach._ask_on_loop("now")
    await _wait_inflight(coach)
    assert [t.say for t in coach.triggers.active] == ["Buy Battle Fury now."]  # no such item as blink_dagger here
    assert coach.triggers.active[0].clauses[1].var == "item_bfury"
