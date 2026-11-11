from doty.sim import ScriptedMatch
from doty.state import GameStateStore, Snapshot

from .conftest import TOKEN


def test_snapshot_tolerates_empty_payload():
    s = Snapshot({})
    assert s.clock == 0
    assert not s.has_hero
    assert s.inventory == [] and s.teleport is None
    assert s.unspent_skill_points is None
    assert s.hp_pct == 1.0


def test_snapshot_reads_scripted_payload():
    m = ScriptedMatch()
    m.step(100)  # clock -90 -> 10
    s = Snapshot(m.payload(TOKEN))
    assert s.has_hero and s.hero_name == "npc_dota_hero_juggernaut"
    assert s.clock == 10 and s.in_game
    assert s.team == "radiant"
    assert {i.name for i in s.inventory} >= {"item_tango", "item_quelling_blade"}
    assert s.teleport is not None and s.teleport.name == "item_tpscroll"
    assert [a.name for a in s.abilities][0] == "juggernaut_blade_fury"


def test_unspent_skill_points_counts_talents_and_abilities():
    raw = {
        "hero": {"name": "npc_dota_hero_axe", "level": 11, "talent_1": True},
        "abilities": {f"ability{i}": {"name": f"axe_{i}", "level": lvl} for i, lvl in enumerate([4, 3, 1, 1])},
    }
    # 4+3+1+1 = 9 points + 1 talent = 10 spent at level 11 -> 1 unspent
    assert Snapshot(raw).unspent_skill_points == 1


def test_event_clock_converts_game_time():
    s = Snapshot({"map": {"clock_time": 600, "game_time": 690}})
    assert s.event_clock({"game_time": 680, "event_type": "roshan_killed"}) == 590


def test_store_resets_on_new_match_and_dedupes_events():
    store = GameStateStore()
    ev = {"game_time": 100, "event_type": "roshan_killed"}
    a = store.ingest({"map": {"matchid": "1"}, "events": [ev]})
    assert len(store.new_events(a)) == 1
    b = store.ingest({"map": {"matchid": "1"}, "events": [ev]})
    assert store.new_events(b) == []
    store.memory["x"] = 1
    store.ingest({"map": {"matchid": "2"}})
    assert "x" not in store.memory and store.seen_events == set()


def test_hp_trend_uses_history():
    store = GameStateStore()
    for i, hp in enumerate([1000, 900, 700, 500]):
        store.ingest({"hero": {"name": "npc_dota_hero_axe", "health": hp, "max_health": 1000}},
                     received_t=float(i))
    assert store.hp_trend(2.0) < -0.3


def test_casting_your_ultimate_counts_as_a_fight_even_at_full_hp():
    from doty.state import GameStateStore

    store = GameStateStore()

    def payload(cd, t):
        return {"map": {"clock_time": 900}, "player": {"team_name": "radiant"},
                "hero": {"name": "npc_dota_hero_juggernaut", "alive": True, "health": 1000, "max_health": 1000},
                "abilities": {"ability0": {"name": "juggernaut_blade_fury", "level": 4, "cooldown": 0},
                              "ability3": {"name": "juggernaut_omni_slash", "level": 1, "cooldown": cd,
                                           "ultimate": True, "can_cast": cd == 0}}}

    store.ingest(payload(0, 1.0), received_t=1.0)
    store.ingest(payload(0, 1.1), received_t=1.1)
    assert not store.in_fight()
    store.ingest(payload(120, 1.2), received_t=1.2)  # Omnislash went off
    assert store.in_fight()


def test_short_cooldown_and_farming_ultimates_are_not_fights():
    from doty.state import GameStateStore

    def payload(name, cd):
        return {"map": {"clock_time": 900}, "player": {"team_name": "radiant"},
                "hero": {"name": "npc_dota_hero_invoker", "alive": True, "health": 1000, "max_health": 1000},
                "abilities": {"ability5": {"name": name, "level": 1, "cooldown": cd, "ultimate": True}}}

    for name, cd in (("invoker_invoke", 6), ("alchemist_chemical_rage", 55), ("tinker_rearm", 3)):
        store = GameStateStore()
        store.ingest(payload(name, 0), received_t=1.0)
        store.ingest(payload(name, cd), received_t=1.1)
        assert not store.in_fight(), name
