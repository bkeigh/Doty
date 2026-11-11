"""Draft help from public hero stats: scoring, the matchup grid, bans and counter items. Runs on a
tiny made-up dataset (no network); one test checks the real hero list when it's cached."""

import pytest

from doty.knowledge import counters, draft
from doty.knowledge.stats import Stats


def hero(hid, name, wins, picks, roles=("Carry",)):
    return {"id": hid, "name": f"npc_dota_hero_{name.lower()}", "localized_name": name, "roles": list(roles),
            "pub_pick": picks, "pub_win": wins, "5_pick": picks // 10, "5_win": wins // 10}


@pytest.fixture
def stats(tmp_path):
    s = Stats(cache_dir=tmp_path, fetch=lambda url: pytest.fail(f"no network in tests: {url}"))
    s.heroes = {1: hero(1, "Alpha", 5000, 10000), 2: hero(2, "Bravo", 5200, 10000),
                3: hero(3, "Charlie", 4500, 10000, ("Support",)), 4: hero(4, "Delta", 5000, 10000),
                5: hero(5, "Enemy", 5000, 10000)}
    s._index = {"alpha": 1, "bravo": 2, "charlie": 3, "delta": 4, "enemy": 5}
    # From Enemy's side: Alpha beats it (Enemy wins 40%), Bravo is even, Charlie loses to it; no data for Delta.
    s._matchups[5] = [{"hero_id": 1, "games_played": 3000, "wins": 1200},
                      {"hero_id": 2, "games_played": 3000, "wins": 1500},
                      {"hero_id": 3, "games_played": 3000, "wins": 1800}]
    return s


def test_counter_pick_beats_a_merely_strong_hero(stats):
    picks = draft.suggest(stats, allies=[], enemies=[5])
    assert [p.name for p in picks[:2]] == ["Alpha", "Bravo"]
    alpha = picks[0]
    assert alpha.vs[0][0] == "Enemy" and alpha.vs[0][1] == pytest.approx(0.1 * 3000 / 3300)
    assert alpha.score > picks[1].score > 0  # Bravo: no matchup edge, but a 52% hero
    assert all(p.hero_id != 5 for p in picks)  # never suggests a hero that's taken


def test_role_filter_and_taken_heroes(stats):
    assert [p.name for p in draft.suggest(stats, [], [5], role="support")] == ["Charlie"]
    assert "Alpha" not in [p.name for p in draft.suggest(stats, [1], [5])]
    assert "Bravo" not in [p.name for p in draft.suggest(stats, [], [5], banned=[2])]


def test_matrix_has_a_cell_per_pair(stats):
    grid = draft.matrix(stats, [1, 4], [5])
    assert grid[0][0] == pytest.approx(0.1 * 3000 / 3300) and grid[1][0] is None


def test_small_samples_are_shrunk(stats):
    stats._matchups[5] = [{"hero_id": 1, "games_played": 30, "wins": 6}]  # 80% over 30 games
    assert draft.advantage(stats, 1, 5) == pytest.approx(0.3 * 30 / 330)


def test_counter_items_rank_shared_answers_first():
    items = counters.counter_items(["Phantom Assassin", "Lion", "Riki"])
    keys = [i for i, _why, _who in items]
    assert keys[0] == "black_king_bar"
    mkb = next(x for x in items if x[0] == "monkey_king_bar")
    assert "Evasion (your attacks miss)" in mkb[1] and mkb[2] == ["Phantom Assassin"]
    assert "dust" in keys and counters.counter_items(["Nobody"]) == []


def test_counter_table_names_real_heroes_and_items():
    s = Stats()
    if not ((s.cache_dir / "heroStats.json").is_file() and (s.cache_dir / "items.json").is_file()):
        pytest.skip("hero stats aren't cached on this PC yet")
    s._fetch = lambda url: pytest.fail("cache only")
    assert s.load()
    assert sorted({h for t in counters.TRAITS for h in t.heroes if s.find(h) is None}) == []
    assert sorted({i for t in counters.TRAITS for i in t.items if i not in s.items}) == []


def test_draft_summary_for_the_coach(stats):
    picks = {"you": "radiant", "radiant": ["Delta"], "dire": ["Enemy"], "bans": ["Bravo"]}
    out = draft.draft_summary(stats, picks)
    assert out["your_team"] == ["Delta"] and out["enemy_team"] == ["Enemy"]
    assert out["counter_picks_from_stats"][0].startswith("Alpha (+") and "Bravo" not in " ".join(out["counter_picks_from_stats"])
    assert draft.draft_summary(stats, {"you": "", "radiant": ["Delta"]}) == {"draft_read_so_far": {"you": "", "radiant": ["Delta"]}}
