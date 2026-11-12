"""The real model's lines from the first real run (findings 2 to 8), served by the local fake at the same moment of
the scripted match, and what Doty now does with each. Nothing here calls the real API."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestServer

from doty.advice import AdviceBus
from doty.coach import Coach, prompts
from doty.coach.backends import claude_request
from doty.coach.llm import _whole, parse_watch, wrong_skill
from doty.config import CoachConfig
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN, Recorder
from .fake_anthropic import FakeAnthropic

JUGG = "npc_dota_hero_juggernaut"
IDS = {"5028": "juggernaut_blade_fury", "5029": "juggernaut_healing_ward", "5030": "juggernaut_blade_dance",
       "5031": "juggernaut_omni_slash"}


def _kb():
    """Juggernaut's abilities and the real card's skill order (Blade Fury › Healing Ward › Blade Fury › Blade
    Dance), and the items the run talked about."""
    from .test_item_judgement import _kb as spells

    kb = spells()
    kb.ability_ids = dict(IDS)
    order = ["5028", "5029", "5028", "5030", "5028", "5031", "5028", "5030"]
    kb.skill_rows[8] = [{"lvl": i + 1, "ab": ab, "n": 50} for i, ab in enumerate(order)]

    def item(i, name, cost, parts=None):
        return {"id": i, "dname": name, "cost": cost, "components": parts}

    kb.items.update({f"item_{k}": v for k, v in {
        "slippers": item(14, "Slippers of Agility", 140), "circlet": item(20, "Circlet", 155),
        "recipe_wraith_band": item(74, "Wraith Band Recipe", 210),
        "wraith_band": item(75, "Wraith Band", 505, ["slippers", "circlet"]),
        "quelling_blade": item(11, "Quelling Blade", 100), "tango": item(44, "Tango", 90),
        "branches": item(16, "Iron Branch", 50),
    }.items()})
    return kb


@pytest.fixture
async def coach_at(monkeypatch):
    """A coach on the fake server, with the scripted match fed to a given clock."""
    fake = FakeAnthropic("")
    server = TestServer(fake.app())
    await server.start_server()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", str(server.make_url("")).rstrip("/"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    made = []

    async def make(clock, **cfg):
        store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
        bus.subscribe(rec)
        kb = _kb()
        coach = Coach(CoachConfig(use_screenshot=False, **cfg), store, bus, kb)
        await coach.start()
        made.append(coach)
        match = ScriptedMatch(match_id="4242")
        while match.s.clock < clock:
            match.step(1.0)
            store.ingest(match.payload(TOKEN))
        return coach, fake, rec, store, match

    yield make
    for c in made:
        await c.close()
    await server.close()


async def _look(coach, kind="watch", visible=False):
    coach._launch(kind, visible=visible)
    for _ in range(300):
        if coach._inflight and coach._inflight[2].done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the look never finished")


def _said(rec):
    return [a.text for a in rec.items if a.speak and a.final]


def _shown(rec):
    return [a.text for a in rec.items if a.display and a.final]


# 2. the skill order --------------------------------------------------------------------------------------------------
async def test_2_skill_advice_follows_the_order_doty_says(coach_at):
    coach, fake, rec, store, _m = await coach_at(125)  # 2:05, right after "Skill point: take Healing Ward"
    fake.reply = "SAY: Skip Blade Fury for now; level Blade Dance and keep Bladeform stacking."
    await _look(coach)
    assert not any("Blade Dance" in t for t in _shown(rec))  # dropped: Healing Ward is next in the order
    assert wrong_skill(coach.kb, "Level Healing Ward now, then farm.", store.current) == ""
    assert wrong_skill(coach.kb, "Skip trades until Blade Fury is up.", store.current) == ""  # (not about skills)
    # At -0:10 the run's plan said "Skip Blade Fury until level 2": Blade Fury is due again two points on.
    coach, fake, rec, store, _m = await coach_at(-10)
    fake.reply = ("Take the bounty rune at 0:00, then go to the bottom lane. Skip Blade Fury until level 2, keep "
                  "Bladeform stacking by not trading hits. Check who is in the enemy lane for ganks.")
    await _look(coach, "start", visible=True)
    final = [a for a in rec.items if a.display and a.final][-1].text
    assert "Skip Blade Fury" not in final and final.startswith("Take the bounty rune at 0:00")
    assert "follow" in prompts.SYSTEM_PROMPT and "never tell the player to skip or delay it" in prompts.SYSTEM_PROMPT


# 3. what the player owns, and what the gold covers -------------------------------------------------------------------
async def test_3_no_claim_that_an_item_was_bought_when_it_wasnt(coach_at):
    coach, fake, rec, store, _m = await coach_at(169)  # 2:49
    assert "item_wraith_band" not in store.current.all_item_names()
    fake.reply = "SAY: Your gold is spent on Wraith Band now, so stay in the bottom lane and farm."
    await _look(coach)
    assert not any("Wraith Band" in t for t in _shown(rec))
    assert coach._false_ownership("You have Wraith Band, so trade now.", store.current) == {"item_wraith_band"}
    assert coach._false_ownership("Buy Wraith Band now; you already have the gold, then head for Power Treads.",
                                  store.current) == set()  # (the gold is a claim about gold: checked by the gold)


async def test_3_a_prepared_call_is_checked_for_the_gold_when_it_fires(coach_at):
    coach, fake, rec, store, match = await coach_at(170)
    snap = store.current
    assert snap.gold < coach.kb.cost_left("item_power_treads", snap.item_list())  # (the run: a few hundred gold)
    fake.reply = "QUIET: Farm the lane.\nTRIGGER: alive => Buy Power Treads next; you have the gold."
    await _look(coach)
    assert [t.say for t in coach.triggers.active] == ["Buy Power Treads next; you have the gold."]
    match.step(1.0)
    coach.enabled = False  # (only the prepared calls: no new look)
    coach.on_snapshot(store.ingest(match.payload(TOKEN)))
    assert not any(a.category == "trigger" for a in rec.items)  # 2:56 in the run: said with ~800 gold


# 4. notes to itself ----------------------------------------------------------------------------------------------------
async def test_4_a_note_after_a_blank_line_is_not_read_out(coach_at):
    coach, fake, rec, store, _m = await coach_at(399)  # 6:39
    fake.reply = ("SAY: Buy Power Treads now; you have 2,625 gold and are dying with it unspent.\n\nNote: the player "
                  "has gold for Power Treads and no Town Portal Scroll, so buy the Scroll too if the shop is in reach.")
    await _look(coach)
    assert not any("Note" in t or "the player" in t for t in _shown(rec) + _said(rec))
    assert parse_watch(fake.reply)[1] == "Buy Power Treads now; you have 2,625 gold and are dying with it unspent."


# 5. a line with its start cut away ------------------------------------------------------------------------------------
def test_5_what_is_left_after_a_cut_stands_alone_or_goes():
    assert parse_watch("SAY: Buy Power Treads now. QUIET: keep farming the safe lane.") == (
        False, "Keep farming the safe lane.")
    assert parse_watch("SAY: Push top — no: and then rotate.")[1] == ""  # (a piece of a sentence: the line goes)
    assert _whole("keep farming the safe lane.") == "Keep farming the safe lane." and _whole("then mid.") == ""
    assert parse_watch("SAY: Farm the big camp near your tower.")[1] == "Farm the big camp near your tower."


# 6. the death fact ----------------------------------------------------------------------------------------------------
def test_6_the_death_fact_reads_as_a_sentence():
    assert prompts.hp_words("100% HP 3.0 s before dying") == "from full HP to dead in 3 seconds (burst: too fast to react)"
    assert prompts.hp_words("62% HP 9.5 s before dying") == "from 62% HP to dead in 10 seconds (slow: time to walk away)"
    assert prompts.hp_words("odd") == "odd"


# 7. the death look's speed -----------------------------------------------------------------------------------------------
async def test_7_the_death_answer_starts_without_thinking_first(coach_at):
    sonnet = CoachConfig(model="claude-sonnet-5-5", effort="low")
    req = claude_request(sonnet, "s", [], "death")
    assert req["thinking"] == {"type": "between_tools"} and "fallbacks" not in req  # (Sonnet 5.5 refuses "disabled")
    assert "thinking" not in claude_request(sonnet, "s", [], "question")  # (questions keep their thinking)
    assert claude_request(CoachConfig(model="claude-haiku-5-5", effort="low"), "s", [], "death")["thinking"] == {
        "type": "disabled"}
    assert "thinking" not in claude_request(CoachConfig(model="claude-sonnet-5-5", effort="xhigh"), "s", [], "death")
    coach, fake, rec, store, _m = await coach_at(315, model="claude-sonnet-5-5", effort="low")
    fake.reply = "You were caught alone. Walk back with your team next time."
    await _look(coach, "death", visible=True)
    assert fake.requests[-1]["thinking"] == {"type": "between_tools"}


# 8. the feed, look after look -------------------------------------------------------------------------------------------
async def test_8_the_feed_names_an_item_a_few_times_not_seven(coach_at):
    coach, fake, rec, store, match = await coach_at(250)
    lines = ["QUIET: Power Treads are your next buy; farm toward them.",
             "QUIET: Keep farming the safe lane until Power Treads.",
             "QUIET: Farm the jungle camp, then buy Power Treads.",
             "QUIET: Stay in lane; Power Treads next.",
             "QUIET: Power Treads first, then Battle Fury."]
    for reply in lines:
        match.step(30.0)
        store.ingest(match.payload(TOKEN))
        fake.reply = reply
        await _look(coach)
    notes = [t for t in _shown(rec) if "Power Treads" in t]
    assert 1 <= len(notes) <= 2  # (the run: seven in ten minutes)


def test_the_real_lines_as_bench_fixtures():
    from doty.bench import fixtures

    from .test_bench import DATA
    from .test_coach_etiquette import _bench_kb

    kb = _bench_kb()
    for name, checks in (("real_note_after_blank_line", ["format_leak"]),
                         ("real_line_with_its_start_cut", ["format_leak"]),
                         ("real_gold_spent_on_wraith_band", ["claims_owned"]),
                         ("real_you_have_the_gold_for_treads", ["not_affordable"])):
        fx = fixtures.load_fixture(DATA / f"{name}.json")
        result = fixtures.run_fixture(fx, kb)
        assert fx["checks"] == checks and result["status"] == "ok" and result["coach"] == "drops", (name, result)
    assert json  # (fixtures are JSON files under tests/data/bench)
