"""Your own profile: account ids from GSI or a link, rank names, and the per-game performance score."""

import pytest

from doty.knowledge.profile import Profile, account_from_gsi, ago, parse_account, performance, rank_name
from doty.knowledge.stats import Stats


def test_account_ids():
    assert account_from_gsi({"accountid": "123456789"}) == 123456789
    assert account_from_gsi({"steamid": "76561198083722517"}) == 123456789
    assert account_from_gsi({}) == 0
    assert parse_account("https://www.dotabuff.com/players/123456789/matches") == 123456789
    assert parse_account("https://www.opendota.com/players/76561198083722517") == 123456789
    assert parse_account("not a number") == 0


def test_rank_names():
    assert rank_name(54) == "Legend 4" and rank_name(11) == "Herald 1" and rank_name(80, 42) == "Immortal #42"
    assert rank_name(None) == "Unranked (or hidden)"


def test_performance_uses_hero_percentiles():
    bench = {"result": {"gold_per_min": [{"percentile": 0.1, "value": 300}, {"percentile": 0.5, "value": 450},
                                         {"percentile": 0.9, "value": 650}],
                        "last_hits_per_min": [{"percentile": 0.5, "value": 5.0}, {"percentile": 0.9, "value": 8.0}]}}
    # 500 GPM: 50th percentile; 270 last hits in 30 min = 9/min: 90th
    assert performance({"gold_per_min": 500, "last_hits": 270, "duration": 1800}, bench) == 70
    assert performance({"gold_per_min": 100, "duration": 600}, bench) == 0
    assert performance({"gold_per_min": 500}, {}) is None


def test_recent_games_from_opendota(tmp_path):
    urls = []

    def fetch(url):
        urls.append(url)
        if url.endswith("/recentMatches"):
            return [{"match_id": 1, "hero_id": 8, "player_slot": 0, "radiant_win": True, "kills": 9, "deaths": 2,
                     "assists": 7, "gold_per_min": 600, "xp_per_min": 700, "last_hits": 250, "duration": 2100,
                     "start_time": 1000},
                    {"match_id": 2, "hero_id": 8, "player_slot": 130, "radiant_win": True, "kills": 1, "deaths": 8,
                     "assists": 2, "gold_per_min": 300, "xp_per_min": 350, "last_hits": 60, "duration": 1500,
                     "start_time": 900}]
        if "benchmarks" in url:
            return {"result": {"gold_per_min": [{"percentile": 0.5, "value": 450}, {"percentile": 0.9, "value": 590}]}}
        if url.endswith("/players/123"):
            return {"rank_tier": 54, "profile": {"personaname": "Me"}}
        pytest.fail(url)

    s = Stats(cache_dir=tmp_path, fetch=fetch)
    games = Profile(s).recent(123)
    assert [g.won for g in games] == [True, False]  # slot 130 is Dire; Radiant won
    assert games[0].score == 90 and games[1].score == 0
    assert Profile(s).player(123)["rank_tier"] == 54
    Profile(s).recent(123)
    assert sum(u.endswith("/recentMatches") for u in urls) == 1  # cached for a few minutes


def test_ago():
    assert ago(1000, now=1000 + 3 * 86400) == "3 days ago" and ago(1000, now=1030) == "just now"
    assert ago(1000, now=1000 + 3600) == "1 hour ago"
