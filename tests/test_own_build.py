"""The player's own build, from a real Turbo match on Spirit Breaker (2026-11-10): "Buy Phase Boots", about
twenty times, to a player wearing Boots of Bearing, and "hold until Black King Bar is done", about twenty-five
times, to a player who had gone Shadow Blade, Aghanim's Scepter and Silver Edge instead. Doty now knows every
pair of boots, drops an item the player has passed on, takes "skip it" for an answer, and has a switch for
each subject it brings up."""

import time

from doty import triggers as tl
from doty.advice import AdviceBus
from doty.coach import Coach, prompts
from doty.coach.llm import MAX_ITEM_PUSHES, parse_watch
from doty.config import Config, CoachConfig, RulesConfig, SuggestConfig, dump_config, load_config
from doty.knowledge import build
from doty.rules import RuleEngine
from doty.rules.base import RuleContext
from doty.rules.economy import FINISH_CALLS, AffordableBuildItem
from doty.rules.engine import TOPIC_RULES, default_rules, topics_off
from doty.rules.progression import ItemPace
from doty.state import GameStateStore

from .conftest import Recorder
from .test_app_ui import qapp  # noqa: F401  (the fixture)
from .test_coach_stream import _wait_inflight, coach_env  # noqa: F401  (the fixture)
from .test_game_sense import JUGG, _feed
from .test_game_sense import _kb as _base_kb

BKB, PHASE, BEARING = "item_black_king_bar", "item_phase_boots", "item_boots_of_bearing"


def _kb():
    """test_game_sense's items plus the boots that are made from other boots, and a few big items to buy
    instead of the plan."""
    kb = _base_kb()

    def item(i, name, cost, parts=None):
        return {"id": i, "dname": name, "cost": cost, "components": parts}

    kb.items.update({f"item_{k}": v for k, v in {
        "wind_lace": item(244, "Wind Lace", 250), "ring_of_regen": item(27, "Ring of Regen", 175),
        "tranquil_boots": item(214, "Tranquil Boots", 900, ["boots", "wind_lace", "ring_of_regen"]),
        "ancient_janggo": item(185, "Drum of Endurance", 1625),
        "ring_of_tarrasque": item(279, "Ring of Tarrasque", 1800),
        "boots_of_bearing": item(931, "Boots of Bearing", 4225,
                                 ["tranquil_boots", "ancient_janggo", "ring_of_tarrasque"]),
        "arcane_boots": item(180, "Arcane Boots", 1500, ["boots", "energy_booster"]),
        "guardian_greaves": item(231, "Guardian Greaves", 4450, ["arcane_boots", "mekansm"]),
        "travel_boots": item(48, "Boots of Travel", 2500, ["boots"]),
        "travel_boots_2": item(220, "Boots of Travel 2", 4500, ["travel_boots"]),
        "invis_sword": item(152, "Shadow Blade", 3250), "ultimate_scepter": item(108, "Aghanim's Scepter", 4200),
        "silver_edge": item(249, "Silver Edge", 5700, ["invis_sword", "demon_edge"]),
        "tpscroll": item(46, "Town Portal Scroll", 100), "gem": item(30, "Gem of True Sight", 900),
    }.items()})
    return kb


def _coach(kb, store, **suggest):
    # (These tests pin the old way, "remind"; the new default "once" is in test_coach_etiquette.py.)
    coach = Coach(CoachConfig(own_way="remind"), store, AdviceBus(), kb)
    coach.suggest = SuggestConfig(**suggest)
    heard = Recorder()
    coach.bus.subscribe(heard)
    return coach, heard


# boots ------------------------------------------------------------------------------------------------------
def test_boots_made_from_other_boots_are_still_boots():
    kb = _kb()
    assert kb.second_boots(PHASE, {BEARING}) and kb.second_boots("item_power_treads", {"item_guardian_greaves"})
    assert kb.second_boots(PHASE, {"item_travel_boots_2"})
    # The pair a pair is made from is an upgrade, not a second pair.
    assert not kb.second_boots(BEARING, {"item_tranquil_boots"})
    assert not kb.second_boots("item_guardian_greaves", {"item_arcane_boots"})
    assert kb.second_boots(BEARING, {"item_phase_boots"})  # (it needs Tranquil Boots: that would be a second pair)
    assert not kb.second_boots(PHASE, {"item_boots"}) and not kb.second_boots("item_travel_boots", {BEARING})


def test_phase_boots_arent_the_next_item_for_a_player_in_boots_of_bearing():
    """The match: the coach's plan was Phase Boots then Black King Bar; the player wore Boots of Bearing."""
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [PHASE, BKB]
    owned = [BEARING, "item_invis_sword"]
    assert kb.next_target(JUGG, 1213, owned) == BKB
    assert PHASE not in [e["item"] for e in kb.shopping(JUGG, 1213, owned, 3000, n=6)]
    snap = _feed(store, 1.0, clock=1586, gold=3000, items=owned)
    spoken = [a.spoken for a in AffordableBuildItem().evaluate(RuleContext(snap, None, store, RulesConfig(), kb, []))]
    assert spoken and all("Phase Boots" not in line and "Blades of Attack" not in line for line in spoken)
    said = prompts.state_summary(snap, store, kb, [])
    assert "Phase Boots" not in str(said["popular_next_items"]) + str(said.get("typical_items")) + str(said["next_item"])
    coach, _heard = _coach(kb, store)
    assert coach._already_have("Buy Phase Boots now, you have the gold.", snap) == {PHASE}


def test_the_build_goes_on_after_the_coachs_short_plan():
    kb = _kb()
    kb.items["item_monkey_king_bar"]["components"] = ["demon_edge", "javelin"]  # (a finished item, like the real one)
    kb.custom_plan[JUGG] = [BKB]
    plan = kb.build_plan(JUGG, 1000)
    assert plan[0] == BKB and "item_manta" in plan  # (the hero's usual items follow the coach's three or four)
    assert kb.next_target(JUGG, 1000, [BKB, "item_power_treads"]) == "item_manta"
    # With the phase's plan bought or passed on, the hero's usual items are next: never "nothing left".
    kb.skip("item_manta")
    assert kb.next_target(JUGG, 1000, [BKB, "item_power_treads"]) == "item_monkey_king_bar"


def test_an_item_hardly_anyone_buys_isnt_the_plan():
    """The match's hero: Bracer, bought in 2 of 75 games, was third in the build."""
    kb = _kb()
    kb.item_popularity[8] = {"early_game_items": {"63": 90, "50": 2}, "mid_game_items": {"116": 60, "135": 3}}
    assert kb.build_plan(JUGG, 300) == ["item_power_treads"]  # (Phase Boots: 2 games of 90)
    assert "item_monkey_king_bar" not in kb.build_plan(JUGG, 1000)


# passing on an item ------------------------------------------------------------------------------------------
def test_an_item_you_bought_around_leaves_the_build():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [BKB, "item_manta"]
    boots = ["item_power_treads"]
    assert build.observe(_feed(store, 1.0, clock=400, items=boots), store, kb) == []
    # Saving up, wards and a TP scroll, and a small item aren't a choice against it.
    assert build.observe(_feed(store, 2.0, clock=500, gold=3000, items=boots + ["item_tpscroll"]), store, kb) == []
    assert build.observe(_feed(store, 3.0, clock=640, items=boots + ["item_ancient_janggo"]), store, kb) == []
    # One big item bought first is only another order...
    went = boots + ["item_ancient_janggo", "item_invis_sword"]
    assert build.observe(_feed(store, 4.0, clock=745, items=went), store, kb) == [] and not kb.skipped
    # ...a second one on top (twice Black King Bar's price in other things, and no part of it) is a choice.
    went = went + ["item_ultimate_scepter"]
    assert build.observe(_feed(store, 4.5, clock=745, items=went), store, kb) == [BKB]
    assert kb.skipped == {BKB: build.BOUGHT_OTHER} and kb.next_target(JUGG, 745, went) == "item_manta"
    assert "Black King Bar" in build.note(kb, BKB).text and not build.note(kb, BKB).speak
    snap = _feed(store, 5.0, clock=750, gold=4100, items=went)
    assert [a for a in AffordableBuildItem().evaluate(RuleContext(snap, None, store, RulesConfig(), kb, []))
            if "Black King Bar" in a.text] == []
    said = prompts.state_summary(snap, store, kb, [])
    assert said["passed_on"] == ["Black King Bar"] and "Black King Bar" not in str(said["popular_next_items"])
    assert "Black King Bar" not in str(said["next_item"])


def test_holding_a_part_means_youre_building_it():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [BKB]
    build.observe(_feed(store, 1.0, clock=400, items=["item_power_treads"]), store, kb)
    held = ["item_power_treads", "item_ogre_axe", "item_invis_sword", "item_ultimate_scepter"]
    assert build.observe(_feed(store, 2.0, clock=900, items=held), store, kb) == [] and not kb.skipped


def test_a_new_match_starts_with_nothing_passed_on():
    kb, store = _kb(), GameStateStore()
    build.observe(_feed(store, 1.0, clock=400, items=["item_power_treads"]), store, kb)
    kb.skip(BKB, build.SAID_SO)
    store.reset("8")
    build.observe(_feed(store, 2.0, clock=10, items=[]), store, kb)
    assert not kb.skipped


def test_you_can_afford_it_is_said_twice_then_the_item_is_dropped(monkeypatch):
    """The match: "You can afford Phase Boots (1,450)" kept coming back every few minutes."""
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [BKB, "item_manta"]
    bus, rec = AdviceBus(), Recorder()
    bus.subscribe(rec)
    rule, clock = AffordableBuildItem(), [1000.0]
    engine = RuleEngine(store, bus, RulesConfig(), kb, rules=[rule])
    engine.prefs = CoachConfig(own_way="remind")  # (the old way: "once", the default, says it once)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for _ in range(FINISH_CALLS + 1):
        for tick in range(3):  # (several updates a second: still one call)
            engine.on_snapshot(_feed(store, clock[0] + tick, clock=900, gold=4500, items=["item_power_treads"]))
        clock[0] += rule.cooldown_s + 1
    said = [a.text for a in rec.items]
    assert said[:2] == ["You can afford Black King Bar (4,050)"] * 2
    assert said[2].startswith("Black King Bar is out of your build") and kb.skipped[BKB] == build.COULD_AFFORD
    assert not rec.items[2].speak and len(said) == 4
    assert said[3].startswith("Buy ") and "Manta Style" in said[3]  # on to the next item, an update later


def test_item_pace_leaves_out_what_you_passed_on():
    kb, store = _kb(), GameStateStore()
    kb.item_timings[(8, BKB)] = [{"hero_id": 8, "time": 1200, "games": 100, "wins": 50}]
    kb.item_timings[(8, "item_power_treads")] = [{"hero_id": 8, "time": 450, "games": 100, "wins": 50}]
    snap = _feed(store, 1.0, clock=1100, gold=100, items=["item_power_treads"])
    ctx = RuleContext(snap, None, store, RulesConfig(), kb, [], prefs=CoachConfig(own_way="remind"))
    assert any("Black King Bar" in a.text for a in ItemPace().evaluate(ctx))
    store.memory.clear()
    kb.skip(BKB)
    assert not any("Black King Bar" in a.text for a in ItemPace().evaluate(ctx))


# the coach takes no for an answer ------------------------------------------------------------------------------
def test_the_coach_doesnt_push_what_you_passed_on_or_cant_pay_for():
    kb, store = _kb(), GameStateStore()
    coach, _heard = _coach(kb, store)
    snap = _feed(store, 1.0, clock=900, gold=453, items=["item_power_treads"])
    now = time.perf_counter()
    assert "doesn't cover" in coach._vet_tip("watch", "Buy the Ogre Axe now.", snap, snap, now, True)
    assert coach._vet_tip("watch", "Save for Black King Bar and farm the jungle.", snap, snap, now, True) == ""
    rich = _feed(store, 2.0, clock=905, gold=1200, items=["item_power_treads"])
    assert coach._vet_tip("watch", "Buy the Ogre Axe now.", rich, rich, time.perf_counter(), True) == ""
    kb.skip(BKB, build.BOUGHT_OTHER)
    assert "passed on" in coach._vet_tip("watch", "Buy Black King Bar when you can.", rich, rich, time.perf_counter(), True)
    assert coach._unwanted("Use Black King Bar before you jump.", rich) == ""  # (not a buy)
    call = tl.parse("TRIGGER: afford black_king_bar => Finish it now.", 905)[0]
    assert "passed on" in coach._unwanted(call.say, rich, call)


def test_an_item_pushed_three_times_goes_quiet_until_you_start_it():
    kb, store = _kb(), GameStateStore()
    coach, _heard = _coach(kb, store)
    snap = _feed(store, 1.0, clock=900, gold=300, items=["item_power_treads"])
    tip = "Farm safely and save for Black King Bar."
    for _ in range(MAX_ITEM_PUSHES):
        assert not coach._overpushed(tip, snap)
        coach._pushed("Buy Black King Bar as soon as you can.", snap)
    assert coach._overpushed("Get Black King Bar before the next fight.", snap) == {BKB}
    summary: dict = {}
    coach._settings_into(summary, snap)
    assert summary["told_to_buy"] == {"Black King Bar": MAX_ITEM_PUSHES}
    part = _feed(store, 2.0, clock=960, gold=100, items=["item_power_treads", "item_ogre_axe"])
    assert not coach._overpushed("Get Black King Bar before the next fight.", part)  # a part bought: it's live again


def test_skip_it_said_out_loud_is_done_at_once_without_the_ai():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [BKB, "item_manta"]
    coach, heard = _coach(kb, store)
    _feed(store, 1.0, clock=900, gold=500, items=["item_power_treads"])
    assert coach._build_command("Should I skip Black King Bar?") is None  # a question is for the coach
    assert coach._build_command("What do I buy after Black King Bar") is None
    assert coach._build_command("I don't want to fight right now") is None  # (no item named)
    assert coach._build_command("I don't need BKB, they're both man fighters") == ([BKB], False)
    assert coach._build_command("I'm not going to buy Black King Bar this game") == ([BKB], False)
    assert coach._build_command("Stop telling me to get the Manta Style") == (["item_manta"], False)
    # An item named, but not as the thing not wanted: that's talk for the coach, not an order.
    assert coach._build_command("I don't want to die to their Blink Dagger again") is None
    assert coach._build_command("Skip this fight, I have no Black King Bar") is None
    assert coach._build_command("skip it") == (None, False)
    assert coach._build_command("Bring back Black King Bar.") == ([BKB], True)
    coach.triggers.arm(tl.parse("TRIGGER: afford black_king_bar => Finish Black King Bar now.\n"
                                "TRIGGER: hp < 30 => Back off.", 900))
    coach._question_on_loop("Stop suggesting Black King Bar.", None)
    assert BKB in kb.skipped and heard.items[-1].text == "Okay, no Black King Bar this game. Next: Manta Style."
    assert [t.say for t in coach.triggers.active] == ["Back off."]  # the call that pushed it went too
    coach._question_on_loop("skip that one", None)  # the item Doty points at now
    assert "item_manta" in kb.skipped
    coach._question_on_loop("Bring back Black King Bar", None)
    assert BKB not in kb.skipped and heard.items[-1].text == "Black King Bar is back in your build."


def test_a_skip_line_in_an_answer_is_honoured():
    kb, store = _kb(), GameStateStore()
    coach, heard = _coach(kb, store)
    snap = _feed(store, 1.0, clock=900, items=["item_power_treads"])
    coach._apply_skip("Fair: go Silver Edge for the break.\nSKIP: item_black_king_bar, not_an_item", snap)
    assert list(kb.skipped) == [BKB] and "out of your build" in heard.items[-1].text


# what Doty suggests: a switch for each subject -----------------------------------------------------------------
def test_every_rule_with_a_switch_exists_and_settings_round_trip(tmp_path):
    ids = {r.id for r in default_rules()}
    assert all(rules <= ids for rules in TOPIC_RULES.values())
    assert set(TOPIC_RULES) == {f for f in SuggestConfig.__dataclass_fields__} == set(prompts.TOPIC_WORDS)
    cfg = Config()
    assert topics_off(cfg.suggest) == frozenset() and cfg.hotkeys.skip_item
    cfg.suggest.items = cfg.suggest.timers = False
    path = tmp_path / "doty.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    assert "[suggest]" in path.read_text(encoding="utf-8")
    assert topics_off(load_config(path).suggest) == {"items", "timers"}


def test_item_advice_off_silences_the_buy_calls_and_the_coach():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = [BKB]
    bus, rec = AdviceBus(), Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, RulesConfig(), kb)
    snap = _feed(store, 1.0, clock=900, gold=4100, items=["item_power_treads"])
    engine.on_snapshot(snap)
    assert any(a.key.startswith("afford:") for a in rec.items)
    rec.items.clear()
    store.memory.clear()
    engine.suggest = SuggestConfig(items=False)
    engine.on_snapshot(_feed(store, 2.0, clock=901, gold=4100, items=["item_power_treads"]))
    assert not any(a.category == "economy" and "Black King Bar" in a.text for a in rec.items)

    coach, _heard = _coach(kb, store, items=False, deaths=False)
    snap = store.current
    assert "turned off" in coach._vet_tip("watch", "Buy Black King Bar now.", snap, snap, time.perf_counter(), True)
    assert coach._vet_tip("watch", "Walk back to your tower: three are missing.", snap, snap, time.perf_counter(), True) == ""
    summary = prompts.state_summary(snap, store, kb, [])
    coach._settings_into(summary, snap)
    assert "next_item" not in summary and "popular_next_items" not in summary
    assert summary["dont_advise_on"] == [prompts.TOPIC_WORDS["deaths"], prompts.TOPIC_WORDS["items"]]
    coach._apply_build("BUILD: item_manta", snap)  # (still parses, but _run doesn't call it with items off)
    assert coach._off("items") and coach._off("deaths") and not coach._off("timers")


def test_the_prompt_tells_the_coach_about_the_new_state():
    for word in ('"passed_on"', '"told_to_buy"', '"dont_advise_on"', "affordable now"):
        assert word in prompts.SYSTEM_PROMPT
    assert "SKIP:" in prompts.KIND_INSTRUCTIONS["question"]


# what the coach says ---------------------------------------------------------------------------------------------
def test_a_change_of_mind_inside_a_sentence_isnt_said():
    """The match: "Respawn in 3 seconds, buy Tranquil... no: once back, buy a Town Portal Scroll"."""
    say, tip = parse_watch("SAY: Respawn in 3 seconds, buy Tranquil... no: once back, buy a Town Portal Scroll "
                           "and last-hit creeps near your tower.")
    assert say and tip == "Once back, buy a Town Portal Scroll and last-hit creeps near your tower."
    assert parse_watch("SAY: No, don't fight there: walk back to your tower.")[1].startswith("No, don't fight")
    assert parse_watch("QUIET: Farm the jungle, no need to fight yet.")[1] == "Farm the jungle, no need to fight yet."


def test_a_call_for_a_time_on_the_clock_isnt_said_long_after_it():
    """The match: "Grab the water rune at 2:00", said at 2:47 on respawning."""
    store = GameStateStore()
    book = tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: clock >= 2:00 and alive => Grab the water rune at 2:00 with your team.", 100))
    dead = {"alive": False, "respawn_seconds": 20}
    assert book.check(_feed(store, 1.0, clock=110), store) == []  # not yet
    assert book.check(_feed(store, 2.0, clock=118, extra=dead), store) == []  # dead as the rune spawns
    assert book.check(_feed(store, 3.0, clock=167), store) == [] and book.active == []  # 47 s late: dropped
    book.arm(tl.parse("TRIGGER: clock >= 4:00 and alive => Bounty runes are up at 4:00.", 200))
    book.check(_feed(store, 4.0, clock=235), store)
    assert [a.text for a in book.check(_feed(store, 5.0, clock=241), store)] == ["Bounty runes are up at 4:00."]
    # A clock in the condition alone is "from then on": this one is right whenever it comes true.
    late = tl.parse("TRIGGER: clock >= 35:00 and hp < 30 => Back off and keep buyback.", 2000)[0]
    assert not late.late(2400) and tl.parse("TRIGGER: alive => Roshan respawns by 27:25.", 1500)[0].late(1700)


async def test_a_prepared_call_isnt_said_right_after_the_same_thing():
    """The match: "Back off toward your tower", "Walk back to your tower now", "Walk back to your tower now",
    within nine seconds."""
    kb, store = _kb(), GameStateStore()
    coach, heard = _coach(kb, store)
    gap, tl.MIN_GAP_S = tl.MIN_GAP_S, 0.0
    try:
        coach.triggers.arm(tl.parse("TRIGGER: hp < 30 => Walk back to your tower now; don't fight at this HP.\n"
                                    "TRIGGER: hp < 28 => Walk back to your tower now, you're too low to fight.", 890))
        coach.on_snapshot(_feed(store, 1.0, clock=890, hp=600))
        coach.on_snapshot(_feed(store, 2.0, clock=894, hp=290))
        coach.on_snapshot(_feed(store, 3.0, clock=899, hp=270))
    finally:
        tl.MIN_GAP_S = gap
    assert [a.text for a in heard.items] == ["Walk back to your tower now; don't fight at this HP."]


# the app ----------------------------------------------------------------------------------------------------------
def test_buy_next_has_a_not_this_game_button(qapp):
    from doty.ui.live import BuildPanel

    panel, skipped = BuildPanel(), []
    bkb = {"key": "black_king_bar", "name": "Black King Bar", "cost": 4050, "left": 4050, "short": 4050}
    panel.set({"build": [bkb]})
    assert panel._lines["black_king_bar"].skip is None  # (a panel nobody listens to has nothing to press)
    panel.on_skip = skipped.append
    panel.set({"build": [bkb, {"key": "manta", "name": "Manta Style", "cost": 4850, "left": 4850}]})
    panel._lines["manta"].skip.click()
    assert skipped == ["manta"] and panel._lines["manta"].skip.toolTip() == "Not this game"


def test_settings_have_a_switch_for_each_subject(qapp, tmp_path):
    from doty.ui.widgets import Switch

    from .test_app_ui import make_window

    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    keys = {key for _row, keys, _tab in page._tracked for key in keys}
    assert {("suggest", topic) for topic in TOPIC_RULES} <= keys
    switch = next(row.findChild(Switch) for row, keys, _tab in page._tracked if ("suggest", "items") in keys)
    switch.click()
    assert page.draft.suggest.items is False and ctl.cfg.suggest.items is True  # (a draft until Save)
    page._save()
    assert load_config(tmp_path / "doty.toml").suggest.items is False and ctl.cfg.suggest.items is False
    win.close()


# the second game in the same log ---------------------------------------------------------------------------------
def test_until_black_king_bar_counts_as_bringing_it_up_again():
    """The match: nearly every line for ten minutes ended "...until Black King Bar"."""
    kb, store = _kb(), GameStateStore()
    coach, _heard = _coach(kb, store)
    snap = _feed(store, 1.0, clock=900, gold=700, items=["item_power_treads"])
    for line in ("Farm the safe jungle near your tower until Black King Bar.",
                 "Stay under your tower and wait for Black King Bar.",
                 "Farm your lane under tower and head for Black King Bar."):
        assert not coach._overpushed(line, snap)
        coach._pushed(line, snap)
    assert coach._overpushed("Farm the bottom jungle until Black King Bar, don't go in alone.", snap) == {BKB}
    # A call to get out is never held back, and an enemy's item isn't the player's build.
    assert not coach._overpushed("Walk back to your tower now: no Black King Bar yet.", snap)
    coach._pushed("Nyx Assassin has Blink Dagger: stay behind your creeps.", snap)
    assert "item_blink" not in coach._pushes


def test_a_tag_on_the_answers_own_line_isnt_read_out():
    """The match: "...don't walk the river alone. TRIGGER: hp < 30 and alive => Back under your tower right now"
    was shown and spoken."""
    from doty.coach.llm import _strip_tags, _tag_lines

    reply = ("Farm your lane under tower and wait for your team; don't walk the river alone. "
             "TRIGGER: hp < 30 and alive => Back under your tower right now; burst kills you.")
    assert _strip_tags(reply) == "Farm your lane under tower and wait for your team; don't walk the river alone."
    assert [t.say for t in tl.parse(_tag_lines(reply), 480)] == ["Back under your tower right now; burst kills you."]
    assert _strip_tags("Group mid.\nBUILD: item_manta") == "Group mid."  # (a tag on its own line, as before)


def test_an_answer_being_read_out_loses_the_sentence_that_sells_what_you_have():
    """The match: "Respawn, buy a TP scroll and Magic Wand" to a player with a TP scroll, after most deaths."""
    kb, store = _kb(), GameStateStore()
    coach, _heard = _coach(kb, store)
    snap = _feed(store, 1.0, clock=900, gold=300, items=["item_power_treads"], tp=True)
    said = ("You charged in alone and got stunned. Respawn, buy a TP scroll and stay with Luna. "
            "Buy Ogre Axe now. Fight only next to your team.")
    assert coach._without_bad_sentences(said, snap) == ("You charged in alone and got stunned. "
                                                        "Fight only next to your team.")
    assert coach._without_bad_sentences("Walk back. Buy a TP scro", snap) == "Walk back. Buy a TP scro"  # still arriving
    bare = _feed(store, 2.0, clock=905, gold=300, items=["item_power_treads"], tp=False)
    assert "TP scroll" in coach._without_bad_sentences(said, bare)  # no TP: that's the right call
    assert "already have" in coach._vet_tip("watch", "Buy a Town Portal Scroll, then farm.", snap, snap,
                                            time.perf_counter(), True)


async def test_a_death_answer_is_cleaned_as_it_streams(coach_env):
    """End to end against the fake AI: the sentence that sells a TP scroll the player holds is neither shown nor
    said, the tag written on the answer's line is armed instead of read out, and the rest arrives in order."""
    from doty.sim import ScriptedMatch

    from .conftest import TOKEN

    coach, fake, rec, kb = coach_env
    match = ScriptedMatch()
    match.step(600)
    match.s.has_tp = True
    assert coach.store.ingest(match.payload(TOKEN)).teleport is not None
    fake.reply = ("You charged in alone and got stunned. Respawn, buy a TP scroll and a wand. "
                  "Fight only next to your team. TRIGGER: hp < 30 and alive => Back under your tower now.")
    coach._launch("death", visible=True)
    await _wait_inflight(coach)
    cards = [a for a in rec.items if a.display]
    spoken = [a.text for a in rec.items if not a.display]
    assert cards[-1].final and cards[-1].text == "You charged in alone and got stunned. Fight only next to your team."
    assert spoken == ["You charged in alone and got stunned.", "Fight only next to your team."]
    assert not any("TRIGGER" in a.text or "TP scroll" in a.text for a in rec.items if a.final or not a.display)
    assert [t.say for t in coach.triggers.active] == ["Back under your tower now."]
