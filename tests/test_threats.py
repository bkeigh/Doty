"""Knowing the enemy: the ability facts, reviewed for every hero (`python -m doty.knowledge.judgement --audit`)
and pinned here on a sample of real entries; a threat card per enemy hero, ranked for the player's own hero; and
its use in the match brief, the one spoken threat line and the death look."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doty.coach import prompts
from doty.config import RulesConfig
from doty.knowledge import judgement, threats
from doty.knowledge.constants import ability_entry
from doty.lineup import Lineup
from doty.rules.base import RuleContext
from doty.rules.matchup import ThreatCall
from doty.state import GameStateStore

from .test_game_sense import JUGG, LION, _feed
from .test_item_judgement import BANE, BB, LINA, PA, SNIPER, SPELLS, TROLL, URSA
from .test_item_judgement import _kb as _spells_kb

SAMPLE = json.loads((Path(__file__).parent / "data" / "ability_sample.json").read_text(encoding="utf-8"))["abilities"]
SILENCER = "npc_dota_hero_silencer"


# the ability facts ------------------------------------------------------------------------------------------------
def test_a_broad_sample_of_real_abilities_reads_as_reviewed():
    """57 entries from dotaconstants (7.41), each with the disable kind the audit settled on. A data update or a
    regex change that reads one differently fails here."""
    assert len(SAMPLE) >= 25
    wrong = {name: (judgement.ability_facts(e["raw"], name)["disable"], e["disable"]) for name, e in SAMPLE.items()
             if judgement.ability_facts(e["raw"], name)["disable"] != e["disable"]}
    assert wrong == {}
    kinds = {e["disable"] for e in SAMPLE.values()}
    assert kinds >= {"hex", "stun", "disable", "sleep", "taunt", "silence", "fear", "root", "cyclone", "disarm", ""}


def test_the_other_facts_of_the_sample():
    def facts(name):
        return ability_entry(SAMPLE[name]["raw"], name)

    assert facts("lion_voodoo")["unit"] and not facts("lion_impale")["unit"]  # (Earth Spike can be aimed at a point)
    assert facts("legion_commander_duel")["bkb"] and not facts("lion_voodoo")["bkb"]
    assert facts("phantom_assassin_immaterial")["evasion"] and facts("windrunner_windrun")["evasion"]
    assert facts("necrolyte_ghost_shroud")["self_buff"] and not facts("necrolyte_ghost_shroud")["hostile"]
    assert facts("bounty_hunter_wind_walk")["invis"]
    # Cast on a friend, but it stuns the enemies where it lands.
    assert facts("snapfire_firesnap_cookie")["hostile"] and facts("phoenix_supernova")["hostile"]
    # Overrides: Howl is a debuff on the enemy (not a buff for Nullifier); Tree Dance's stun is Monkey King's own.
    assert facts("lycan_howl")["hostile"] and not facts("lycan_howl")["self_buff"]
    assert not facts("monkey_king_tree_dance")["hostile"]
    assert facts("lycan_howl")["why"].startswith("override: ")


def test_every_override_says_why_and_when():
    for name, (fix, why, patch) in judgement.OVERRIDES.items():
        assert fix and len(why) > 20 and patch.startswith("7."), name


def test_the_audit_prints_what_the_engine_believes(capsys):
    kb = _sample_kb()
    lines = judgement.audit(kb, ["Lion"], why=True)
    assert lines[0].startswith("Lion (lion)")
    assert any("Hex" in x and "hex" in x and "single target" in x and "BKB blocks it" in x for x in lines)
    assert any('<- "' in x for x in lines)


@pytest.mark.skipif(not (judgement.load_cached().ability_info), reason="no cached ability data on this machine")
def test_the_cached_data_still_reads_as_reviewed():
    """On a machine with Doty's cache: the same sample from the live data. A dotaconstants update that changes how
    one reads shows here first (review it with --audit, then update the sample)."""
    kb = judgement.load_cached()
    changed = {n: (kb.ability_info[n]["disable"], e["disable"]) for n, e in SAMPLE.items()
               if n in kb.ability_info and kb.ability_info[n]["disable"] != e["disable"]}
    assert changed == {}


# threat cards ------------------------------------------------------------------------------------------------------
def _sample_kb():
    """test_item_judgement's heroes, plus Lion, Silencer and Invoker-free real entries from the sample."""
    kb = _spells_kb()
    kb.heroes[LION]["primary_attr"] = "int"
    kb.heroes[LINA].update(primary_attr="int", roles=["Carry", "Nuker", "Support"])
    kb.heroes[JUGG]["primary_attr"] = "agi"
    kb.heroes[SILENCER] = {"id": 75, "localized_name": "Silencer", "roles": ["Carry", "Support", "Disabler"],
                           "primary_attr": "int", "attack_type": "Ranged"}
    kb.hero_abilities[SILENCER] = {"abilities": ["silencer_last_word"]}
    kb.ability_info["silencer_last_word"] = ability_entry(SAMPLE["silencer_last_word"]["raw"], "silencer_last_word")
    kb.hero_abilities[LION] = {"abilities": ["lion_impale", "lion_voodoo"]}
    for name in ("lion_impale", "lion_voodoo"):
        kb.ability_info[name] = ability_entry(SAMPLE[name]["raw"], name)
    return kb


LINEUP = [LION, BANE, PA, SILENCER, BB]


def test_every_spell_a_card_names_is_that_heros_own():
    """By construction: a card is built from the enemy's own ability list, so no line can name another hero's
    spell (the bench's foreign_spell checker catches the AI doing it)."""
    kb = _sample_kb()
    for own in (JUGG, LINA, LION):
        for role in (None, SimpleNamespace(position=1, core=True), SimpleNamespace(position=5, core=False)):
            for card in threats.cards(kb, LINEUP + [TROLL, SNIPER, URSA, LINA], own, role):
                mine = threats.hero_spells(kb, card.npc)
                assert {t.spell for t in card.threats if t.spell} <= mine, card.hero
                assert {spell for _hero, spell in card.facts} <= mine
                assert all(hero == card.hero for hero, _spell in card.facts)
                assert len(card.line.split()) <= threats.LINE_WORDS, card.line


def test_a_card_names_the_danger_and_its_answer():
    kb = _sample_kb()
    lion = threats.card(kb, LION, JUGG, lineup=LINEUP)
    assert lion.top.kind == "hex" and lion.top.spell == "Hex" and lion.top.single_target
    assert lion.line.startswith("Lion's Hex hexes you")
    # Fiend's Grip goes through Black King Bar: the card says so, and doesn't offer BKB for it.
    bane = threats.card(kb, BANE, JUGG, lineup=LINEUP)
    grip = next(t for t in bane.threats if t.spell == "Fiend's Grip")
    assert grip.through_bkb and grip.answer_item != "item_black_king_bar" and "through BKB" in grip.what()


def test_the_card_is_ranked_for_your_hero():
    kb = _sample_kb()
    jugg, lina = (threats.card(kb, SILENCER, own, lineup=LINEUP) for own in (JUGG, LINA))
    assert lina.top.kind == "silence" and lina.top.weight > jugg.top.weight  # a spell-reliant hero fears silences
    # A right-clicker fears evasion first, and Monkey King Bar (bought on Juggernaut) answers it.
    pa = threats.card(kb, PA, JUGG, lineup=LINEUP)
    assert pa.top.kind == "evasion" and pa.top.spell == "Immaterial" and pa.top.answer == "Monkey King Bar"
    assert threats.card(kb, PA, LINA, lineup=LINEUP).top.kind != "evasion"


def test_no_data_no_card():
    kb = _sample_kb()
    assert threats.card(kb, "npc_dota_hero_brand_new", JUGG) is None
    assert threats.card(kb, "npc_dota_hero_tidehunter", JUGG) is None  # (a hero, but no ability data)
    assert threats.cards(kb, ["Nobody", "npc_dota_hero_tidehunter"], JUGG) == []


# where the cards are used ------------------------------------------------------------------------------------------
def test_the_brief_has_their_threats_and_stays_the_same_all_match():
    kb = _sample_kb()
    lineup = {"radiant": ["Juggernaut"], "dire": ["Lion", "Bane", "Phantom Assassin", "Silencer", "Bristleback"]}
    brief = prompts.match_brief(lineup, "radiant", JUGG, kb)
    assert "Their threats to you, most dangerous first: " in brief and "Lion's Hex hexes you" in brief
    assert prompts.match_brief(lineup, "radiant", JUGG, kb) == brief  # (it's cached: the same text every look)


def _lineup_store(enemies=LINEUP):
    store = GameStateStore()
    snap = _feed(store, 1.0, clock=200, items=["item_power_treads"])
    votes = store.memory["lineup_votes"] = Lineup()
    for _ in range(2):
        votes.add([JUGG, LINA, "npc_dota_hero_zuus", "npc_dota_hero_axe", "npc_dota_hero_pudge"], list(enemies),
                  own=JUGG, team="radiant")
    return store, snap


def _evaluate(store, snap, kb, **kw):
    return list(ThreatCall().evaluate(RuleContext(snap, None, store, RulesConfig(), kb, [], **kw)))


def test_one_spoken_threat_line_once_the_lineup_is_complete():
    kb = _sample_kb()
    store, snap = _lineup_store()
    said = _evaluate(store, snap, kb)
    spoken = [a for a in said if a.speak]
    assert len(spoken) == 1 and len(said) == 2  # one line heard, the full card shown
    assert spoken[0].text == threats.cards(kb, LINEUP, JUGG)[0].line
    assert not said[0].speak and said[0].text.startswith("Their threats: ")
    assert _evaluate(store, snap, kb) == []  # once a match


def test_the_threat_line_waits_for_the_lineup_a_calm_moment_and_the_switch():
    kb = _sample_kb()
    store, snap = _lineup_store(LINEUP[:4])  # four known
    assert _evaluate(store, snap, kb) == []
    store, snap = _lineup_store()
    assert _evaluate(store, snap, kb, topics_off=frozenset({"enemy"})) == []  # enemy subject off
    store.memory["fight_t"] = snap.received_t  # in a fight
    assert _evaluate(store, snap, kb) == []
    store.memory.pop("fight_t")
    terse = [a for a in _evaluate(store, snap, kb, level="pro") if a.speak][0]
    assert terse.spoken != terse.text and len(terse.spoken.split()) < len(terse.text.split())


def test_the_death_look_is_told_which_of_their_spells_did_it():
    kb = _sample_kb()
    store = GameStateStore()
    snap = _feed(store, 1.0, clock=900, items=["item_power_treads"])
    store.memory["lineup"] = {"radiant": ["Juggernaut"], "dire": ["Lion", "Bane", "Silencer"]}
    store.memory["last_death"] = {"clock": 900, "disabled_s": {"hexed": 1.6, "silenced": 2.0}}
    store.memory["death_open"] = True
    dead = _feed(store, 2.0, clock=905, items=["item_power_treads"], extra={"alive": False})
    said = prompts.state_summary(dead, store, kb, [])
    causes = said["death_cause_candidates"]
    assert causes[0].startswith("silenced 2 s: could be Silencer's Last Word")
    assert causes[1].startswith("hexed 1.6 s: could be Lion's Hex")
    # Only spells that do it, and only these enemies' own.
    assert "Earth Spike" not in " ".join(causes) and "Nightmare" not in " ".join(causes)
    store.memory["last_death"]["disabled_s"] = {}
    assert "death_cause_candidates" not in prompts.state_summary(dead, store, kb, [])
