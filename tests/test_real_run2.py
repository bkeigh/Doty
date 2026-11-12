"""What the second run against the real model found (the scripted Juggernaut match, ten game minutes at speed 6, no
screenshots, the lineup unknown): the model's own words served by the local fake at the same moment of the match, and
what Doty now does with each. Nothing here calls the real API."""

import asyncio
from types import SimpleNamespace

import pytest

from doty import triggers
from doty.coach import prompts
from doty.coach.backends import HOUR_WRITE, PRICES, _usage, claude_request, cost_usd, request_model
from doty.coach.llm import asks_player, wrong_parts
from doty.config import CoachConfig
from doty.knowledge import build as buildlib
from doty.state import GameStateStore

from .conftest import TOKEN
from .test_real_lines import _look, _said, _shown
from .test_real_lines import coach_at as coach_at  # noqa: F401  (the fixture)

DEATH = ("You died holding 2,026 gold, and burst took you from full HP in 3 seconds. On respawn, buy Broadsword plus "
         "Blades of Attack for Battle Fury, then Power Treads. Don't walk into trades while your wave is pushed out.")
PLAN = ("Take the bounty rune at 0:00, then go to the bottom lane. Last hit with Quelling Blade and stay out of harass "
        "range. Check the top bar and tell me their lineup.")


def battle_fury_recipe(kb):
    """Battle Fury as the item data has it (Perseverance, two Broadswords, Quelling Blade), with the parts the run
    talked about, and Power Treads' other attribute items."""
    def item(i, name, cost, parts=None):
        return {"id": i, "dname": name, "cost": cost, "components": parts}

    for key, value in {"broadsword": item(3, "Broadsword", 1000), "perseverance": item(69, "Perseverance", 1400),
                       "blades_of_attack": item(2, "Blades of Attack", 450),
                       "boots_of_elves": item(18, "Band of Elvenskin", 450), "robe": item(19, "Robe of the Magi", 450),
                       "boots": item(29, "Boots of Speed", 500), "gloves": item(25, "Gloves of Haste", 450),
                       "belt_of_strength": item(14, "Belt of Strength", 450),
                       "power_treads": item(63, "Power Treads", 1400, ["boots", "gloves", "belt_of_strength"]),
                       }.items():
        kb.items.setdefault(f"item_{key}", value)
    kb.items["item_bfury"] = item(145, "Battle Fury", 3900, ["perseverance", "broadsword", "broadsword",
                                                            "quelling_blade"])
    kb._item_re = None  # (the name pattern is built once per item list)
    return kb


def _kb():
    from .test_real_lines import _kb as real_kb

    return battle_fury_recipe(real_kb())


@pytest.fixture
def kb():
    return _kb()


# 1. a part that isn't in the item --------------------------------------------------------------------------------------
def test_1_wrong_parts_reads_the_ways_a_part_is_named(kb):
    assert wrong_parts(kb, DEATH) == [("item_blades_of_attack", "item_bfury")]
    for fine in ("Buy Broadsword for Battle Fury.", "Buy a Quelling Blade and a Broadsword towards Battle Fury.",
                 "Perseverance, a part of Battle Fury, is your next buy.", "Battle Fury's Broadsword is next.",
                 "Buy Band of Elvenskin for Power Treads.",  # (the shop takes any of the three)
                 "Buy Magic Wand for the lane, and save for Battle Fury.",  # (not named as a part of it)
                 "Sell Wraith Band for Battle Fury's slot.", "Buy Broadsword (1,000) for Battle Fury, then Power Treads."):
        assert wrong_parts(kb, fine) == [], fine
    assert wrong_parts(kb, "Blades of Attack, a part of Battle Fury, is next.") == [
        ("item_blades_of_attack", "item_bfury")]
    assert wrong_parts(kb, "Finish Battle Fury's Blades of Attack.") == [("item_blades_of_attack", "item_bfury")]
    assert wrong_parts(kb, "Buy Slippers of Agility towards Power Treads.") == [("item_slippers", "item_power_treads")]
    assert wrong_parts(None, DEATH) == []


async def test_1_the_death_answer_loses_the_sentence_with_the_wrong_part(coach_at):
    coach, fake, rec, store, _m = await coach_at(293)  # 4:53, as in the run
    battle_fury_recipe(coach.kb)
    fake.reply = DEATH
    await _look(coach, "death", visible=True)
    final = [a for a in rec.items if a.display and a.final][-1].text
    voiced = [a.text for a in rec.items if not a.display]  # each sentence handed to the voice
    assert "Blades of Attack" not in final and not any("Blades of Attack" in t for t in voiced + _said(rec))
    assert any(t.startswith("Don't walk into trades") for t in voiced)
    assert final.startswith("You died holding 2,026 gold") and final.endswith("your wave is pushed out.")
    assert coach._wrong_fact("Buy Broadsword plus Blades of Attack for Battle Fury.", store.current).startswith(
        "names a part that isn't in the item: Blades of Attack for Battle Fury")


async def test_1_an_answer_to_a_question_is_checked_for_facts_but_may_talk_about_what_was_passed_on(coach_at):
    coach, fake, rec, store, _m = await coach_at(293)
    battle_fury_recipe(coach.kb)
    snap = store.current
    coach.kb.skip("item_bfury", buildlib.SAID_SO)
    answer = "Yes, Battle Fury is still right for you. Buy Blades of Attack for Battle Fury on your next back."
    assert coach._without_bad_sentences(answer, snap, complete=True, question=True) == (
        "Yes, Battle Fury is still right for you.")  # (the item passed on: asked about, the coach may answer)
    assert coach._without_bad_sentences("Buy Broadsword for Battle Fury.", snap, complete=True) == ""  # (unasked)
    fake.reply = answer
    coach._launch("question", visible=True, question="Should I still get Battle Fury?")
    for _ in range(300):
        if coach._inflight[2].done():
            break
        await asyncio.sleep(0.01)
    assert [a for a in rec.items if a.display and a.final][-1].text == "Yes, Battle Fury is still right for you."


async def test_1_a_prepared_call_with_a_wrong_part_is_neither_armed_nor_fired(coach_at):
    coach, fake, rec, store, match = await coach_at(170)
    battle_fury_recipe(coach.kb)
    fake.reply = "QUIET: Farm the lane.\nTRIGGER: alive => Buy Blades of Attack for Battle Fury next."
    await _look(coach)
    assert coach.triggers.active == []
    # One armed before the check existed (or by any other way) is checked again as it fires.
    coach.triggers.arm(triggers.parse("TRIGGER: level >= 1 => Buy Blades of Attack for Battle Fury next.",
                                      store.current.clock))
    coach.enabled = False
    for _ in range(3):
        match.step(1.0)
        coach.on_snapshot(store.ingest(match.payload(TOKEN)))
    assert not any(a.category == "trigger" for a in rec.items)


# 2. asking the player to do Doty's job -----------------------------------------------------------------------------------
def test_2_asks_player():
    assert asks_player("Check the top bar and tell me their lineup.")
    assert asks_player("Let me know when Lion shows.") and asks_player("Do you have a TP scroll?")
    for fine in ("Check the minimap before you farm the triangle.", "Why fight alone? Walk back with your team.",
                 "Tell your team to smoke up.", "Keep the wave near your tower."):
        assert not asks_player(fine), fine


async def test_2_the_plan_with_no_lineup_doesnt_ask_for_it(coach_at):
    coach, fake, rec, store, _m = await coach_at(-10)
    fake.reply = PLAN
    await _look(coach, "start", visible=True)
    final = [a for a in rec.items if a.display and a.final][-1].text
    assert final == ("Take the bounty rune at 0:00, then go to the bottom lane. Last hit with Quelling Blade and stay "
                     "out of harass range.")
    assert "tell me" not in " ".join(_said(rec) + _shown(rec))
    # At the source: the state says what to do about the unknown lineup, and the system prompt forbids asking.
    sent = fake.requests[-1]
    state = next(b["text"] for b in sent["messages"][0]["content"] if b["text"].startswith("Game state:"))
    assert "don't mention it" in state and "tell me" not in state
    assert "Never ask them to tell you anything" in prompts.SYSTEM_PROMPT
    assert "say nothing about the missing lineup" in prompts.SYSTEM_PROMPT


async def test_2_a_lineup_the_player_says_still_counts(coach_at):
    """There's no voice command for the lineup: a spoken question goes to the coach, whose LINEUP line counts as a
    read (Coach._apply_lineup, for every kind of look). The vetting only drops sentences, never that line."""
    coach, fake, rec, store, _m = await coach_at(30)
    fake.reply = "Then play safe: Lion stuns. Stay behind your creeps.\nLINEUP: radiant=Juggernaut | dire=Lion"
    coach._launch("question", visible=True, question="They have Lion, what do I do?")
    for _ in range(300):
        if coach._inflight[2].done():
            break
        await asyncio.sleep(0.01)
    assert store.memory.get("lineup_votes") is not None
    assert [a for a in rec.items if a.display and a.final][-1].text == "Then play safe: Lion stuns. Stay behind your creeps."


# 3. a prepared call about a moment that has passed -------------------------------------------------------------------------
def test_3_lateness_depends_on_what_the_call_is_about():
    rune = "Grab the power rune at 6:00 if your lane is safe."
    assert triggers.late_by(rune, 386) == 26 and triggers.late_by(rune, 365) is None
    assert triggers.late_by("Have Black King Bar by 14:00.", 860) is None  # (a standing condition: half a minute)
    assert triggers.late_by("Have Black King Bar by 14:00.", 871) == 31
    assert triggers.late_by("Stack the big camp at 7:53.", 480) == 7
    assert triggers.late_by("Farm the jungle.", 900) is None


def test_3_two_calls_for_one_moment_queue_and_a_late_one_is_dropped(monkeypatch):
    """At 6:00 two calls came true. One went; the other waited MIN_GAP_S of wall time behind it, which at the demo's
    speed 6 is 24 game seconds, and was shown at 6:26. It's dropped instead: its moment (the rune) was gone."""
    from .test_triggers import _snap as snap_at

    store = GameStateStore()

    def _snap(clock):
        return snap_at(store, 0.0, clock=clock)

    wall = [1000.0]
    monkeypatch.setattr(triggers.time, "monotonic", lambda: wall[0])
    book = triggers.TriggerBook()
    book.arm(triggers.parse("TRIGGER: clock >= 6:00 => Your gold covers Battle Fury's next part; buy it on your next "
                            "back.\nTRIGGER: clock >= 6:00 and alive => Grab the power rune at 6:00 if your lane is "
                            "safe.", 330))
    assert book.check(_snap(clock=359)) == []
    first = book.check(_snap(clock=360))
    assert [a.text for a in first] == ["Your gold covers Battle Fury's next part; buy it on your next back."]
    for clock in range(361, 386):  # the queue: the other call waits its turn (no wall time passes)
        wall[0] += 1 / 6
        assert book.check(_snap(clock=clock)) == []
    wall[0] += 1 / 6
    assert book.check(_snap(clock=386)) == [] and book.active == []  # 6:26: dropped, not shown
    # A standing call waits its turn and still goes.
    book.arm(triggers.parse("TRIGGER: clock >= 6:30 => Buy a Broadsword next.\nTRIGGER: clock >= 6:30 and alive "
                            "=> Farm the triangle until 8:00.", 386))
    book.check(_snap(clock=389))
    assert len(book.check(_snap(clock=390))) == 1
    wall[0] += triggers.MIN_GAP_S
    assert [a.text for a in book.check(_snap(clock=414))] == ["Farm the triangle until 8:00."]


def test_3_the_bench_agrees():
    from doty.bench import fixtures

    from .test_bench import _check

    assert _check("stale_time", "Grab the power rune at 6:00 if your lane is safe.", clock=386)
    assert _check("stale_time", "Grab the power rune at 6:00 if your lane is safe.", clock=364) == ""
    assert _check("stale_time", "Have Black King Bar by 14:00.", clock=860) == ""
    assert fixtures  # (the committed fixture: tests/data/bench/real_rune_call_26_s_late.json)


# 4. which model gives the death answer, and what it costs -----------------------------------------------------------------
BRIEF = [{"type": "text", "text": "Match brief", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "x"}]


@pytest.mark.parametrize("choice", ["model", "model_1h", "watch"])
@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"])
def test_4_each_choice_builds_a_valid_death_request(choice, model):
    cfg = CoachConfig(model=model, watch_model="claude-haiku-5-5", effort="low", death_answer=choice)
    req = claude_request(cfg, "s", BRIEF, "death")
    want = "claude-haiku-5-5" if choice == "watch" else model
    assert req["model"] == want == request_model(cfg, "death")
    if want == "claude-sonnet-5-5":
        assert req["thinking"] == {"type": "between_tools"}  # (Sonnet 5.5 refuses "disabled")
    elif want == "claude-haiku-5-5":
        assert req["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in req  # (Opus 5.5: thinking can't be turned off; effort low keeps it short)
    assert req["thinking"]["type"] != "between_tools" if want != "claude-sonnet-5-5" and "thinking" in req else True
    if want.startswith(("claude-sonnet-5-5", "claude-opus")):
        assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    else:
        assert "fallbacks" not in req and "betas" not in req  # (Haiku has no server-side fallback)
    hour = choice == "model_1h" and want != "claude-haiku-5-5"
    ttl = {"type": "ephemeral", "ttl": "1h"} if hour else {"type": "ephemeral"}
    assert req["system"][0]["cache_control"] == ttl
    assert req["messages"][0]["content"][0]["cache_control"] == ttl  # (a 1-hour entry before any 5-minute one)
    assert BRIEF[0]["cache_control"] == {"type": "ephemeral"}  # (the caller's content isn't changed)


def test_4_the_default_is_unchanged_and_the_live_looks_keep_their_cache():
    assert CoachConfig().death_answer == "model"
    sonnet = CoachConfig(model="claude-sonnet-5-5", effort="low")
    assert claude_request(sonnet, "s", BRIEF, "death")["model"] == "claude-sonnet-5-5"
    hour = CoachConfig(model="claude-sonnet-5-5", effort="low", death_answer="model_1h")
    for kind in ("watch",):
        assert claude_request(hour, "s", BRIEF, kind)["system"][0]["cache_control"] == {"type": "ephemeral"}
    for kind in ("start", "plan", "question", "now", "death"):  # the main model's requests share the hour
        assert claude_request(hour, "s", BRIEF, kind)["system"][0]["cache_control"]["ttl"] == "1h"
    watch = CoachConfig(model="claude-sonnet-5-5", effort="low", death_answer="watch")
    assert claude_request(watch, "s", BRIEF, "question")["model"] == "claude-sonnet-5-5"  # (only the death moves)
    # The death answer on the watch model is the same request as a live look up to the cache breakpoints: same model,
    # thinking, effort, system prompt and brief, so it reads the entry the looks keep warm.
    d, w = claude_request(watch, "s", BRIEF, "death"), claude_request(watch, "s", BRIEF, "watch")
    assert (d["model"], d["thinking"], d["output_config"], d["system"]) == (
        w["model"], w["thinking"], w["output_config"], w["system"])


async def test_4_the_death_look_goes_to_the_watch_model_when_chosen(coach_at):
    coach, fake, rec, store, _m = await coach_at(315, model="claude-sonnet-5-5", effort="low", death_answer="watch")
    fake.reply = "You were caught alone. Walk back with your team next time."
    await _look(coach, "death", visible=True)
    sent = fake.requests[-1]
    assert sent["model"] == "claude-haiku-5-5" and sent["thinking"] == {"type": "disabled"}
    assert "fallbacks" not in sent


async def test_4_background_looks_while_dead_go_to_the_watch_model(coach_at, monkeypatch):
    """While dead, the routine look (one every look interval: kind "now", kept for the ask key) went to the main
    model, cold-cache Sonnet requests nobody waited for. They're background looks like the live ones now; the death
    answer itself stays where death_answer puts it."""
    coach, fake, rec, store, match = await coach_at(285, model="claude-sonnet-5-5", effort="low",
                                                    speculative_interval_s=10)
    coach._last_warm = 1e12  # (no warm-up pings in this test)
    fake.reply = "Buy a Broadsword on respawn and farm the safe lane."
    kinds = []
    real = coach._launch

    def launch(kind, visible, *a, **k):
        started = real(kind, visible, *a, **k)
        if started:
            kinds.append((kind, visible, store.current.alive))
        return started

    coach._launch = launch
    while match.s.clock < 312:  # death at 4:46, respawn at 5:10
        match.step(1.0)
        coach._last_spec_wall = 0.0  # (as if a game second were a wall second: MIN_SPEC_WALL_S never holds a look)
        coach.on_snapshot(store.ingest(match.payload(TOKEN)))
        for _ in range(30):
            await asyncio.sleep(0.01)
    dead = [k for k in kinds if not k[2]]
    assert ("death", True, False) in dead
    quiet = [k for k in dead if k[0] == "now" and not k[1]]
    assert 1 <= len(quiet) <= 3  # (24 s dead, a look every 10 s once the death answer is done)
    models = {(r["model"], r.get("thinking", {}).get("type")) for r in fake.requests if r.get("stream")}
    assert ("claude-sonnet-5-5", "between_tools") in models  # the death answer
    assert [r["model"] for r in fake.requests if r.get("stream")].count("claude-sonnet-5-5") == 1
    assert claude_request(coach.cfg, "s", BRIEF, "now_quiet")["thinking"] == {"type": "disabled"}
    assert claude_request(coach.cfg, "s", BRIEF, "now")["model"] == "claude-sonnet-5-5"  # (the ask key, waited for)


def test_4_the_cost_of_one_death_answer():
    """The table in the report, from the prices: an 8,500-token cached prefix, 2,000 more input, 80 out."""
    prefix, rest, out = 8500, 2000, 80
    cold = cost_usd("claude-sonnet-5-5", {"in": rest, "written": prefix, "out": out})
    warm = cost_usd("claude-sonnet-5-5", {"in": rest, "cached": prefix, "out": out})
    hour = cost_usd("claude-sonnet-5-5", {"in": rest, "written": prefix, "written_1h": prefix, "out": out})
    haiku = cost_usd("claude-haiku-5-5", {"in": rest, "cached": prefix, "out": out})
    near = lambda x: pytest.approx(x, abs=1e-6)  # noqa: E731
    assert (cold, warm, hour, haiku) == (near(0.02605), near(0.00565), near(0.0388), near(0.000325))
    assert (hour + 4 * warm) / 5 == near(0.01228)  # five deaths in an hour, the first one writing
    # Anthropic's reference (2026-11): Sonnet 5.5 $2 / $10, cache reads $0.10; Opus 5.5 $4 / $20, reads $0.20; Haiku
    # 5.5 $0.10 / $0.50 (prompts up to 100K tokens).
    assert PRICES["claude-sonnet-5-5"] == (2.0, 10.0, 0.10, 2.50) and HOUR_WRITE == 2.0
    assert PRICES["claude-opus-5-5"][:3] == (4.0, 20.0, 0.20) and PRICES["claude-haiku-5-5"][:2] == (0.10, 0.50)


def test_4_one_hour_writes_are_counted():
    u = SimpleNamespace(input_tokens=2000, cache_read_input_tokens=0, cache_creation_input_tokens=8500,
                        output_tokens=80, cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=8500,
                                                                         ephemeral_5m_input_tokens=0))
    assert _usage(u) == {"in": 2000, "cached": 0, "written": 8500, "written_1h": 8500, "out": 80}
    old = SimpleNamespace(input_tokens=5, cache_read_input_tokens=None, cache_creation_input_tokens=None,
                          output_tokens=1)
    assert _usage(old) == {"in": 5, "cached": 0, "written": 0, "out": 1}


def test_the_real_lines_as_bench_fixtures():
    from doty.bench import fixtures

    from .test_bench import DATA
    from .test_coach_etiquette import _bench_kb

    kb = _bench_kb()
    for name, checks, coach in (("real_wrong_part_for_battle_fury", ["wrong_part"], "drops"),
                                ("real_tell_me_their_lineup", ["asks_player"], "drops"),
                                ("real_rune_call_26_s_late", ["stale_time"], "keeps")):
        fx = fixtures.load_fixture(DATA / f"{name}.json")
        result = fixtures.run_fixture(fx, kb)
        # (the late call: its lateness is judged by the trigger book as it fires, not by the sentence checks)
        assert fx["checks"] == checks and result["status"] == "ok" and result["coach"] == coach, (name, result)
    assert GameStateStore  # (imported for readers: the fixtures' state becomes a game update)
