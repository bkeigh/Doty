"""Instant calls: the AI writes them ahead, Doty fires them itself the moment they come true."""

import time

import pytest

from doty import triggers as tl
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN


def _snap(store, t, clock=600, hp=1000, gold=1000, x=-3000, y=-3000, items=(), alive=True, tp=True):
    inv = {f"slot{i}": {"name": n} for i, n in enumerate(items)}
    if tp:
        inv["teleport0"] = {"name": "item_tpscroll", "cooldown": 0}
    return store.ingest({"map": {"matchid": "1", "clock_time": clock,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": gold},
                         "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": alive, "health": hp,
                                  "max_health": 1000, "xpos": x, "ypos": y, "level": 8},
                         "items": inv}, received_t=t)


def test_parse_accepts_the_vocabulary_and_drops_anything_else():
    text = ("Farm the jungle.\n"
            "TRIGGER: hp < 35 and enemy_side => Back off now, you're too low to stay.\n"
            "TRIGGER: gold >= 4050 and no black_king_bar => Buy Black King Bar now.\n"
            "TRIGGER: clock >= 19:40 and alive => Roshan is likely up: group mid.\n"
            "TRIGGER: __import__('os').system('x') => nope\n"
            "TRIGGER: enemy_carry_missing => unknown word, dropped\n")
    armed = tl.parse(text, clock=600)
    assert [t.say for t in armed] == ["Back off now, you're too low to stay.", "Buy Black King Bar now.",
                                      "Roshan is likely up: group mid."]
    assert armed[2].clauses[0].value == 19 * 60 + 40
    assert tl.parse_condition("has blink and hp > 50")[0].var == "item_blink"


def test_fires_once_on_the_moment_it_turns_true():
    store, book, t = GameStateStore(), tl.TriggerBook(), 100.0
    book.arm(tl.parse("TRIGGER: hp < 35 and enemy_side => Back off now.", clock=600))
    assert book.check(_snap(store, t, hp=900, x=3000, y=2000), store) == []  # on their side, but healthy
    t += 0.1
    fired = book.check(_snap(store, t, hp=300, x=3000, y=2000), store)
    assert [a.text for a in fired] == ["Back off now."] and fired[0].source == "coach"
    t += 0.1
    assert book.check(_snap(store, t, hp=250, x=3000, y=2000), store) == []  # once
    assert not book.active


def test_already_true_when_armed_waits_for_the_next_time():
    store, book, t = GameStateStore(), tl.TriggerBook(), 100.0
    book.arm(tl.parse("TRIGGER: gold >= 4050 and no black_king_bar => Buy Black King Bar now.", clock=600))
    assert book.check(_snap(store, t, gold=5000), store) == []  # it was already true: the coach just said it
    t += 0.1
    book.check(_snap(store, t, gold=3000), store)  # spent it
    t += 0.1
    assert [a.text for a in book.check(_snap(store, t, gold=4100), store)] == ["Buy Black King Bar now."]
    book.arm(tl.parse("TRIGGER: gold >= 4050 and no black_king_bar => Buy Black King Bar now.", clock=600))
    t += 0.1
    book.check(_snap(store, t, gold=3000, items=("item_black_king_bar",)), store)
    t += 0.1
    tl.TriggerBook._last_fired = -1e9
    assert book.check(_snap(store, t, gold=4100, items=("item_black_king_bar",)), store) == []  # already bought


def test_calls_expire_and_unknown_facts_wait():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: enemies_dead >= 3 => Three of them are dead: take the tower.", clock=600))
    book.check(_snap(store, 100, clock=600), store)
    assert book.active and book.active[0].was_true is None  # no screen reading yet: it just waits
    fired = book.check(_snap(store, 100.1, clock=601), store, extra={"enemies_dead": 3})
    assert fired == []  # first known value was already true
    book.check(_snap(store, 100.2, clock=600 + tl.VALID_S + 1), store)
    assert not book.active  # six game minutes later it's gone


@pytest.mark.parametrize("bad", ["hp <", "gold >= lots", "has ../../x", "", "hp < 35 or dead"])
def test_bad_conditions_are_rejected(bad):
    assert tl.parse_condition(bad) is None


def test_a_call_that_comes_true_during_the_gap_waits_instead_of_being_lost(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tl.time, "monotonic", lambda: now[0])
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: hp < 50 => Back off.\nTRIGGER: gold >= 4050 => Buy Black King Bar now.", 600))
    assert book.check(_snap(store, 1, hp=900, gold=1000), store) == []
    assert [a.text for a in book.check(_snap(store, 2, hp=400, gold=1000), store)] == ["Back off."]
    now[0] += 1  # one second later the gold arrives: inside the 4 s gap
    assert book.check(_snap(store, 3, hp=400, gold=4100), store) == []
    now[0] += 4
    assert [a.text for a in book.check(_snap(store, 4, hp=400, gold=4100), store)] == ["Buy Black King Bar now."]


def test_calls_for_the_living_wait_while_you_are_dead():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: hp < 35 => Back off now.\nTRIGGER: dead and respawn_s < 10 => Buy a TP.", 600))
    book.check(_snap(store, 1, hp=900), store)
    out = book.check(_snap(store, 2, hp=0, alive=False), store)  # died in one burst: 0 HP on the death screen
    assert [a.text for a in out] == ["Buy a TP."]  # only the call about being dead
    book.check(_snap(store, 9, hp=1000), store)  # respawned at full HP
    assert [t.say for t in book.active] == ["Back off now."]  # still armed, not spent on the death


def test_new_flags_ultimate_day_night_and_allies_dead():
    conds = tl.parse_condition("ult_ready and night and allies_dead < 2")
    assert conds is not None and [c.var for c in conds] == ["ult_ready", "night", "allies_dead"]
    store = GameStateStore()
    m = ScriptedMatch()
    m.step(600)
    snap = store.ingest(m.payload(TOKEN))
    facts = tl.facts_for(snap, store, {"allies_dead": 1})
    assert facts["day"] == snap.daytime and facts["night"] == (not snap.daytime) and facts["allies_dead"] == 1


def test_a_fired_call_cuts_in_and_is_never_said_late():
    store = GameStateStore()
    book = tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: hp < 35 => Back off now.", clock=600))
    m = ScriptedMatch()
    m.step(600)
    raw = m.payload(TOKEN)
    hero = {**raw["hero"], "alive": True}
    book.check(store.ingest({**raw, "hero": {**hero, "health": hero["max_health"]}}), store)
    fired = book.check(store.ingest({**raw, "hero": {**hero, "health": int(hero["max_health"] * 0.2)}}), store)
    assert fired and fired[0].cut_in and fired[0].stale_s == tl.STALE_S
