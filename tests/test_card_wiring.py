"""The hero cards wired into the coach: the card in every look, matchups and game length in the match brief,
counter answers weighed against the lineup, pace by rank in the farm check, the role choosing the build, and the
background study. Offline: cards are built from the synthetic fixture into a temporary cache."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from doty import app as appmod
from doty.coach import prompts
from doty.config import Config
from doty.knowledge import cards, study
from doty.knowledge.roles import Role
from doty.rules.base import RuleContext
from doty.rules.progression import FarmCheck, ItemPace, UnspentSkillPoint
from doty.state import GameStateStore

from .test_cards import FIXTURE, ctx, raw
from .test_game_sense import JUGG, _feed
from .test_item_judgement import BB, PA, SNIPER, TROLL, URSA
from .test_item_judgement import _kb as _judgement_kb

CORE, SUPPORT = Role(1, "safe", True, True), Role(5, "safe", False, True)


def _item(i, name, cost, parts=None):
    return {"id": i, "dname": name, "cost": cost, "components": parts}


def _carded(tmp_path, kb=None):
    """Juggernaut (id 8) with a hero card from the fixture: carries build Phase Boots, Battle Fury and Shadow
    Blade; hard supports Tranquil Boots, Blink Dagger and Glimmer Cape."""
    kb = kb or _judgement_kb()
    kb.cache_dir = tmp_path
    kb.items.update({f"item_{k}": v for k, v in {
        "quarterstaff": _item(67, "Quarterstaff", 875), "claymore": _item(21, "Claymore", 1350),
        "bfury": _item(145, "Battle Fury", 4100, ["quarterstaff", "claymore"]),
        "blink": _item(1, "Blink Dagger", 2250), "shadow_amulet": _item(215, "Shadow Amulet", 1000),
        "cloak": _item(31, "Cloak", 800),
        "glimmer_cape": _item(254, "Glimmer Cape", 2150, ["shadow_amulet", "cloak"]),
        "invis_sword": _item(152, "Shadow Blade", 3000, ["shadow_amulet", "claymore"]),
    }.items()})
    heroes = dict(FIXTURE["heroes"])
    heroes["8"] = {**FIXTURE["heroes"]["1"], "id": 8, "name": JUGG, "localized_name": "Juggernaut"}
    card = cards.build_card(8, raw(), ctx(heroes=heroes, hero_abilities={
        **FIXTURE["hero_abilities"], JUGG: FIXTURE["hero_abilities"]["npc_dota_hero_testbull"]}), now=time.time())
    cards.save(card, tmp_path)
    return kb


# --- the role chooses the build -----------------------------------------------------------------------------
def test_the_players_role_chooses_the_build(tmp_path):
    kb = _carded(tmp_path)
    everyone = kb.build_plan(JUGG, 600)
    kb.role = SUPPORT
    support = kb.build_plan(JUGG, 600)
    assert support[0] == "item_tranquil_boots" and "item_blink" in support and "item_bfury" not in support
    kb.role = CORE
    core = kb.build_plan(JUGG, 600)
    assert core[0] == "item_phase_boots" and "item_bfury" in core and "item_blink" not in core
    # Not known yet, or only a first guess from the hero's tags: the all-roles list, as before.
    kb.role = None
    assert kb.build_plan(JUGG, 600) == everyone
    kb.role = Role(0, "", True, False)
    assert kb.build_plan(JUGG, 600) == everyone


def test_without_a_card_the_build_is_unchanged(tmp_path):
    kb = _judgement_kb()
    kb.cache_dir = tmp_path  # (an empty cache: no card)
    before = kb.build_plan(JUGG, 600)
    kb.role = SUPPORT
    assert kb.build_plan(JUGG, 600) == before


def test_the_role_build_keeps_what_the_player_decided(tmp_path):
    kb = _carded(tmp_path)
    kb.role = SUPPORT
    assert kb.next_target(JUGG, 600, []) == "item_tranquil_boots"
    # A second pair of boots is never the target: the player wears Power Treads.
    assert kb.next_target(JUGG, 600, ["item_power_treads"]) == "item_blink"
    # An item passed on stays out.
    kb.skip("item_blink")
    assert "item_blink" not in [e["item"] for e in kb.shopping(JUGG, 600, ["item_power_treads"], 0, n=4)]
    # The player's own build for the hero (CoachConfig.hero_builds, set as the coach's plan) still leads.
    kb.custom_plan[JUGG] = ["item_black_king_bar"]
    assert kb.build_plan(JUGG, 600)[0] == "item_black_king_bar"
    # Held parts decide what's being built, card or not (a Claymore and a Quarterstaff: Battle Fury).
    assert kb.held_target(["item_quarterstaff", "item_claymore"]) == "item_bfury"


def test_the_role_build_drops_a_weak_counter_item_once_the_lineup_is_known(tmp_path):
    kb = _carded(tmp_path)
    kb.role = CORE
    card = cards.load(8, tmp_path)
    card["roles"]["1"]["build"]["mid"].append({"item": "black_king_bar", "n": 40, "share": 0.66, "wr": 0.5,
                                               "by_s": 1300})
    cards.save(card, tmp_path)
    assert "item_black_king_bar" in kb.build_plan(JUGG, 600)
    kb.set_enemies([BB, PA, TROLL, SNIPER, URSA], complete=True)  # right-clickers: Black King Bar is weak here
    assert "item_black_king_bar" not in kb.build_plan(JUGG, 600)


# --- the card in every look ---------------------------------------------------------------------------------
def _look(store, kb, role=CORE):
    snap = _feed(store, 1.0, clock=600, items=["item_power_treads"])  # (a new match: its memory starts empty)
    store.memory["role"] = role
    store.memory["lineup"] = {"radiant": ["Juggernaut"], "dire": ["Test Purge", "Test Mage", "Test Mute"]}
    return snap


def test_the_card_is_in_every_look_and_replaces_what_it_covers(tmp_path):
    kb, store = _carded(tmp_path), GameStateStore()
    kb.bracket = 4
    kb.item_timings[(8, "item_black_king_bar")] = [{"hero_id": 8, "time": t, "games": 100, "wins": 50}
                                                   for t in (900, 1200, 1500)]
    snap = _look(store, kb)
    said = prompts.state_summary(snap, store, kb, [])
    card = said["hero_card"]
    assert card.startswith("HERO CARD Juggernaut, patch 9.99b (Archon")
    assert "Test Purge" in card and "dispels your Test Charge" in card  # this lineup, with the reason
    assert "safe-lane carry" in card and "Battle Fury" in card  # the player's role
    for gone in ("next_item_odds", "pro_skill_order", "your_matchups"):
        assert gone not in said
    assert "popular_next_items" in said and "typical_items" in said  # (what the coach may suggest is unchanged)
    # No card: the old fields, and no card.
    kb2, store2 = _judgement_kb(), GameStateStore()
    kb2.cache_dir = tmp_path / "empty"
    kb2.item_timings = kb.item_timings
    plain = prompts.state_summary(_look(store2, kb2), store2, kb2, [])
    assert "hero_card" not in plain and "next_item_odds" in plain


def test_the_system_prompt_makes_the_card_the_source():
    text = prompts.SYSTEM_PROMPT
    assert '"hero_card"' in text and "never use your own memory of Dota numbers" in text
    assert "Name the patch only when the player asks" in text


def test_prompt_size_with_and_without_the_card(tmp_path, capsys):
    """Not a limit: prints what the card costs in the look (characters and a token estimate)."""
    kb, store = _carded(tmp_path), GameStateStore()
    kb.bracket = 4
    snap = _look(store, kb)
    with_card = json.dumps(prompts.state_summary(snap, store, kb, []), separators=(",", ":"))
    kb.cache_dir = tmp_path / "empty"
    without = json.dumps(prompts.state_summary(snap, store, kb, []), separators=(",", ":"))
    print(f"\nstate JSON: {len(without)} chars without the card, {len(with_card)} with it")
    assert len(with_card) - len(without) < 1600  # (about 180 words, less what it replaces)


# --- the match brief ------------------------------------------------------------------------------------------
def test_the_match_brief_has_the_cards_matchups_and_game_length(tmp_path):
    kb = _carded(tmp_path)
    kb.bracket = 4
    brief = prompts.match_brief({"radiant": ["Juggernaut"], "dire": ["Test Purge", "Test Mage"]}, "radiant", JUGG,
                                kb)
    line = next(ln for ln in brief.splitlines() if ln.startswith("Your matchups against them"))
    assert "Archon, patch 9.99b" in line and "Test Purge -10.0 win-rate points (3,000 games)" in line
    assert "their Test Purge dispels your Test Charge" in line and "Test Mage +5.0" in line
    assert "Your hero and game length: wins more of the short games" in brief
    # The brief is the same however the role settles (it's cached for the match).
    kb.role = SUPPORT
    assert prompts.match_brief({"radiant": ["Juggernaut"], "dire": ["Test Purge", "Test Mage"]}, "radiant", JUGG,
                               kb) == brief


def test_counter_answers_are_weighed_against_the_lineup():
    kb = _judgement_kb()
    spells = prompts._answers(["Lion", "Lina"], JUGG, kb)
    assert spells and spells[0].startswith("Black King Bar (it blocks")  # strong: first, with what it answers
    right_click = prompts._answers(["Bristleback", "Phantom Assassin", "Troll Warlord", "Sniper", "Ursa"], JUGG, kb)
    assert not any(a.startswith("Black King Bar") for a in right_click)  # weak here: left out


# --- the rules --------------------------------------------------------------------------------------------------
def _ctx(store, kb):
    return RuleContext(store.current, None, store, Config().rules, kb, [])


def test_farm_check_compares_with_one_rank_up(tmp_path):
    kb, store = _carded(tmp_path), GameStateStore()
    kb.bracket = 4
    _feed(store, 1.0, clock=600, lh=20)
    store.memory["role"] = CORE
    out = list(FarmCheck().evaluate(_ctx(store, kb)))
    up = cards.pace(8, CORE, 4, "normal", tmp_path)["up"][600]["lh"]
    assert len(out) == 1 and out[0].text.startswith(f"Players one rank up have about {up} last hits at 10:00; "
                                                    "you have 20")
    _feed(store, 2.0, clock=600, lh=up + 2)
    assert "ahead of players one rank up" in list(FarmCheck().evaluate(_ctx(store, kb)))[0].text
    # No card (or Turbo): the hero's whole-game median, as before.
    kb.cache_dir = tmp_path / "empty"
    _feed(store, 3.0, clock=600, lh=20)
    assert "Typical Juggernaut games average" in list(FarmCheck().evaluate(_ctx(store, kb)))[0].text


def test_item_pace_skips_a_weak_counter_item():
    kb, store = _judgement_kb(), GameStateStore()
    kb.item_popularity[8] = {"mid_game_items": {"116": 60}}
    kb.item_timings[(8, "item_black_king_bar")] = [{"hero_id": 8, "time": 1200, "games": 100, "wins": 50}]
    _feed(store, 1.0, clock=1100, gold=1000)
    assert list(ItemPace().evaluate(_ctx(store, kb)))  # against an unknown lineup: the usual reminder
    kb.set_enemies([BB, PA, TROLL, SNIPER, URSA], complete=True)
    _feed(store, 2.0, clock=1101, gold=1000)
    store.memory.pop("pace:item_black_king_bar", None)
    assert list(ItemPace().evaluate(_ctx(store, kb))) == []


def test_a_skill_point_is_said_once_until_a_long_wait():
    store, rule = GameStateStore(), UnspentSkillPoint()
    said = []
    for t in range(0, 400):
        snap = SimpleNamespace(level=3 if t < 300 else 4, received_t=float(t), unspent_skill_points=1)
        ctx = RuleContext(snap, None, store, Config().rules, None, [])
        said += [(t, a.text) for a in rule.evaluate(ctx)]
    # Held from 0:00: once after the dwell, once more after AGAIN_S, and once for the next level's point.
    assert [t for t, _ in said] == [6, 6 + int(rule.AGAIN_S), 300]


# --- the background study ---------------------------------------------------------------------------------------
class FakeStudy:
    def __init__(self, stop: threading.Event, steps: int = 4) -> None:
        self.runs: list[list[int]] = []
        self.stop, self.steps = stop, steps
        self.log = lambda *_: None

    def hero_ids(self):
        return [1, 2, 3, 4, 5]

    def run(self, ids, max_calls=None, stop=None):
        self.runs.append(list(ids))
        if len(self.runs) == 2:
            raise RuntimeError("offline")  # (never escapes)
        if len(self.runs) >= self.steps:
            self.stop.set()
        return {"built": [], "current": 0, "failed": [], "calls": 3, "stopped": "step_budget" if len(ids) else ""}


def test_the_background_study_puts_this_match_first_and_never_raises():
    stop = threading.Event()
    fake = FakeStudy(stop, steps=4)
    study.watch(lambda: [3], lambda: [5, 3], stop, study=fake, first_gap_s=0, rest_gap_s=0, idle_s=0, held_s=0)
    assert fake.runs[0] == [3, 5]  # this match's heroes, then the player's own; each once
    assert len(fake.runs) == 4


def test_the_app_starts_the_study_only_when_allowed(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(study, "watch", lambda *a, **k: started.append(a))
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty._start_study()  # tests run with DOTY_STUDY=0 (conftest)
    assert doty._study_stop is None and not started
    monkeypatch.setenv("DOTY_STUDY", "1")
    doty.cfg.app.study = False  # Settings' switch
    doty._start_study()
    assert doty._study_stop is None
    doty.cfg.app.study = True
    doty._start_study()
    for _ in range(50):
        if started:
            break
        time.sleep(0.01)
    assert started and doty._study_stop is not None
    doty._study_stop.set()


def test_the_players_most_played_heroes_come_from_the_saved_file_only(tmp_path, monkeypatch):
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.stats.cache_dir = tmp_path
    monkeypatch.setattr(doty, "_account_id", lambda snap: 4242)  # (a made-up id: only names the file)
    assert doty._study_favourites() == []  # nothing saved: nothing fetched either
    rows = [{"hero_id": h} for h in (8, 8, 8, 26, 26, 1)]
    (tmp_path / "recent_4242.json").write_text(json.dumps(rows), encoding="utf-8")
    assert doty._study_favourites() == [8, 26, 1]


@pytest.mark.parametrize("names", [["Juggernaut", "Test Purge"], []])
def test_the_study_asks_for_this_matchs_heroes(tmp_path, names):
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb.heroes = {JUGG: {"id": 8, "localized_name": "Juggernaut"},
                      "npc_dota_hero_testpurge": {"id": 4, "localized_name": "Test Purge"}}
    doty.store.memory["lineup"] = {"radiant": names[:1], "dire": names[1:]}
    assert doty._study_current() == [8, 4][:len(names)]
