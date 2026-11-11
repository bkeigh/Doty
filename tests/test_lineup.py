"""Who's in the game: lineup votes, the hero-name check, late tips and drawings."""

import time

from doty.coach.llm import MOVED_FAR, SCREEN_MARKS_MAX_S, Coach
from doty.config import CoachConfig
from doty.advice import AdviceBus
from doty.knowledge.constants import Knowledge
from doty.lineup import Lineup
from doty.marks import Mark
from doty.state import GameStateStore

R = ["sb", "jakiro", "cm", "axe", "lina"]
D = ["pudge", "lion", "pa", "sniper", "tiny"]


def test_one_read_confirms_nothing_two_agreeing_reads_do():
    lu = Lineup()
    assert lu.add(R, D, own="sb", team="radiant")
    assert lu.confirmed("radiant") == ["sb"] and lu.confirmed("dire") == []
    assert lu.add(R, D, own="sb", team="radiant")
    assert sorted(lu.confirmed("radiant")) == sorted(R) and sorted(lu.confirmed("dire")) == sorted(D)
    assert lu.complete and lu.side_of("jakiro") == "radiant" and lu.side_of("luna") is None


def test_a_read_with_the_sides_swapped_is_flipped_by_the_players_own_hero():
    lu = Lineup()
    lu.add(R, D, own="sb", team="radiant")
    lu.add(D, R, own="sb", team="radiant")  # the model put the player's team on the right
    assert lu.side_of("jakiro") == "radiant" and lu.side_of("lion") == "dire"


def test_misreads_dont_stick():
    lu = Lineup()
    # A full side without the player's hero is a misread and is dropped.
    assert not lu.add(["jakiro", "cm", "axe", "lina", "luna"], D, own="sb", team="radiant")
    lu.add(R, D, own="sb", team="radiant")
    lu.add(R, ["pudge", "lion", "pa", "sniper", "pugna"], own="sb", team="radiant")  # one wrong portrait
    lu.add(R, D, own="sb", team="radiant")
    assert "pugna" not in lu.confirmed("dire") and "tiny" in lu.confirmed("dire")
    # A hero read on both teams in one read counts for neither.
    lu2 = Lineup()
    lu2.add(["sb", "jakiro"], ["jakiro", "lion", "pa"], own="sb", team="radiant")
    lu2.add(["sb", "jakiro"], ["jakiro", "lion", "pa"], own="sb", team="radiant")
    assert lu2.side_of("jakiro") is None


def _kb():
    kb = Knowledge()
    kb.heroes = {f"npc_dota_hero_{n}": {"id": i, "localized_name": loc} for i, (n, loc) in enumerate([
        ("spirit_breaker", "Spirit Breaker"), ("jakiro", "Jakiro"), ("crystal_maiden", "Crystal Maiden"),
        ("axe", "Axe"), ("lina", "Lina"), ("pudge", "Pudge"), ("lion", "Lion"),
        ("phantom_assassin", "Phantom Assassin"), ("sniper", "Sniper"), ("tiny", "Tiny"), ("pugna", "Pugna"),
        ("luna", "Luna")], start=1)}
    return kb


def test_hero_mentions_ignore_ordinary_words():
    kb = _kb()
    assert kb.hero_mentions("Back off: Lion and PA are missing, and Tiny can toss you.") == {
        "npc_dota_hero_lion", "npc_dota_hero_phantom_assassin", "npc_dota_hero_tiny"}
    assert kb.hero_mentions("a tiny gap; walk back to the lion statue") == set()


def _coach(kb):
    store = GameStateStore()
    store.ingest({"map": {"matchid": "1", "clock_time": 300, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                  "player": {"team_name": "radiant"},
                  "hero": {"name": "npc_dota_hero_spirit_breaker", "id": 71, "alive": True, "health": 900,
                           "max_health": 1000, "xpos": 0, "ypos": 0, "level": 5}})
    return Coach(CoachConfig(), store, AdviceBus(), kb), store


def _confirm(coach, kb):
    rad = "Spirit Breaker, Jakiro, Crystal Maiden, Axe, Lina"
    dire = "Pudge, Lion, Phantom Assassin, Sniper, Tiny"
    for _ in range(2):
        coach._apply_lineup(f"LINEUP: radiant={rad} | dire={dire}", coach.store.current)


def test_tips_naming_heroes_not_in_the_game_are_caught():
    kb = _kb()
    coach, _ = _coach(kb)
    assert coach._phantom_heroes("Pugna will ward the lane") == set()  # can't tell before all ten are known
    _confirm(coach, kb)
    assert coach.store.memory["lineup_complete"]
    assert coach._phantom_heroes("Pugna and Luna are farming top") == {"npc_dota_hero_pugna", "npc_dota_hero_luna"}
    assert coach._phantom_heroes("Lion is missing; back off") == set()


def test_marked_heroes_are_colored_by_team_and_stale_drawings_dropped():
    kb = _kb()
    coach, store = _coach(kb)
    _confirm(coach, kb)
    snap = store.current
    t0 = time.perf_counter()
    marks = (Mark("circle", "screen", 0.5, 0.5, label="Jakiro", tone="danger"),
             Mark("circle", "minimap", 0.2, 0.2, label="Lion"),
             Mark("circle", "screen", 0.4, 0.4, label="Luna"),
             Mark("box", "screen", 0.1, 0.1, 0.2, 0.2, label="buy this"))
    vetted = coach._vet_marks(marks, snap, t0)
    assert [(m.label, m.tone) for m in vetted] == [("Jakiro", "good"), ("Lion", "danger"), ("buy this", "coach")]
    # Seconds later, screen drawings point at the wrong spot; the minimap ones still hold.
    late = coach._vet_marks(marks, snap, t0 - SCREEN_MARKS_MAX_S - 1)
    assert [m.space for m in late] == ["minimap"]
    # So does walking off: the camera moved.
    store.ingest({**store.current.raw, "hero": {**store.current.raw["hero"], "xpos": MOVED_FAR + 100}})
    moved = coach._vet_marks(marks, snap, t0)
    assert [m.space for m in moved] == ["minimap"]


def test_in_fight_follows_real_damage():
    store = GameStateStore()
    t = 100.0

    def tick(hp):
        nonlocal t
        t += 0.5
        store.ingest({"map": {"matchid": "1", "clock_time": 300}, "hero": {"name": "npc_dota_hero_axe", "id": 2,
                      "alive": True, "health": hp, "max_health": 1000}}, received_t=t)

    for hp in (1000, 1000, 990, 1000):
        tick(hp)
    assert not store.in_fight()
    for hp in (950, 880, 820):
        tick(hp)
    assert store.in_fight()
    for _ in range(12):  # six quiet seconds later it's over
        tick(820)
    assert not store.in_fight()
