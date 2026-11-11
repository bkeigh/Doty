"""Save calls: the moment you're bursted (or silenced mid-fight) with a save item ready, Doty names it itself,
in milliseconds, without waiting on the AI."""

from doty import triggers as tl
from doty.advice import AdviceBus, Priority
from doty.config import RulesConfig
from doty.rules import RuleEngine, spoken_lines, urgent_lines
from doty.rules.survival import SAVE_GAP_S, save_call
from doty.state import GameStateStore

from .conftest import Recorder


def _snap(store, t, hp=1000, items=None, hero=None, clock=900):
    raw_hero = {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "health": hp, "max_health": 1000,
                "xpos": 0, "ypos": 0, "level": 12, **(hero or {})}
    return store.ingest({"map": {"matchid": "1", "clock_time": clock,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": 500},
                         "hero": raw_hero, "items": items if items is not None else {}}, received_t=t)


def _items(*names, cooldown=0, charges=0):
    return {f"slot{i}": {"name": n, "cooldown": cooldown, "can_cast": cooldown == 0, "charges": charges}
            for i, n in enumerate(names)}


def _engine():
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    return store, RuleEngine(store, bus, RulesConfig()), rec


def _burst(store, engine, items, hero=None):
    """Full HP, then 45% of it gone a second later."""
    engine.on_snapshot(_snap(store, 1.0, hp=1000, items=items))
    engine.on_snapshot(_snap(store, 1.5, hp=800, items=items, clock=901))
    return engine.on_snapshot(_snap(store, 2.0, hp=550, items=items, hero=hero, clock=901))


def test_a_burst_with_glimmer_ready_calls_glimmer_at_once():
    store, engine, rec = _engine()
    out = _burst(store, engine, _items("item_glimmer_cape", "item_magic_wand", charges=12))
    said = [a for a in out if a.key.startswith("save@")]
    assert [a.spoken for a in said] == ["Glimmer yourself"]  # the best one, not the wand
    assert said[0].priority == Priority.CRITICAL
    assert engine.last_eval_ms < 50  # (all the rules, this update)


def test_nothing_to_call_when_stunned_or_the_item_is_on_cooldown():
    store, engine, _ = _engine()
    assert not [a for a in _burst(store, engine, _items("item_glimmer_cape"), hero={"stunned": True})
                if a.key.startswith("save@")]
    store, engine, _ = _engine()
    assert not [a for a in _burst(store, engine, _items("item_glimmer_cape", cooldown=30))
                if a.key.startswith("save@")]


def test_a_wand_needs_real_charges_and_bkb_is_skipped_when_already_immune():
    store = GameStateStore()
    assert save_call(_snap(store, 1.0, items=_items("item_magic_wand", charges=3))) is None
    assert save_call(_snap(store, 1.1, items=_items("item_magic_wand", charges=15))) == ("item_magic_wand",
                                                                                        "Wand now")
    immune = _snap(store, 1.2, items=_items("item_black_king_bar", "item_force_staff"), hero={"magicimmune": True})
    assert save_call(immune) == ("item_force_staff", "Force Staff out")


def test_silenced_in_a_fight_calls_the_dispel():
    store, engine, _ = _engine()
    items = _items("item_manta", "item_black_king_bar")
    engine.on_snapshot(_snap(store, 1.0, hp=1000, items=items))
    engine.on_snapshot(_snap(store, 2.0, hp=850, items=items, clock=901))  # a 15% hit: a fight, not a burst
    out = engine.on_snapshot(_snap(store, 2.4, hp=850, items=items, hero={"silenced": True}, clock=901))
    assert [a.spoken for a in out if a.key.startswith("save@")] == ["Manta the silence"]


def test_one_save_call_per_fight_and_back_off_doesnt_talk_over_it():
    store, engine, rec = _engine()
    items = _items("item_glimmer_cape", "item_force_staff")
    _burst(store, engine, items)
    engine.on_snapshot(_snap(store, 2.5, hp=250, items=items, clock=902))  # low and still dropping
    spoken = [a.spoken for a in rec.items]
    assert spoken.count("Glimmer yourself") == 1
    assert "Force Staff out" not in spoken  # (the next save waits SAVE_GAP_S)
    assert "Back off, low HP" not in spoken  # the save call was just made
    assert SAVE_GAP_S >= 10


def test_a_prepared_call_waits_its_turn_after_a_save_call():
    store, engine, _ = _engine()
    book = tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: hp < 70 => Walk back to your tower.", clock=900))
    items = _items("item_glimmer_cape")
    book.check(_snap(store, 0.5, hp=1000, items=items), store)
    _burst(store, engine, items)  # Glimmer called on this update
    assert book.check(store.current, store) == []  # its moment comes right after, not over the save call


def test_save_lines_are_prepared_ahead_and_said_faster():
    lines = spoken_lines(RulesConfig())
    assert {"Glimmer yourself", "BKB now", "Manta the silence"} <= set(lines)
    assert lines[0] == "Back off, low HP"
    assert "Glimmer yourself" in urgent_lines() and "Bounty runes in 15" not in urgent_lines()
