"""Hero cards and the study pipeline (knowledge/cards.py, knowledge/study.py), offline on a small synthetic
fixture shaped like the real answers (tests/data/cards_fixture.json)."""

from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from doty.knowledge import cards, study
from doty.knowledge.mode import Mode
from doty.knowledge.roles import Role

FIXTURE = json.loads((Path(__file__).parent / "data" / "cards_fixture.json").read_text(encoding="utf-8"))
NOW = time.time()


def ctx(**over) -> dict:
    base = {"patch": "9.99b", "major": "9.99", "heroes": FIXTURE["heroes"], "items": FIXTURE["items"],
            "abilities": FIXTURE["abilities"], "hero_abilities": FIXTURE["hero_abilities"],
            "ability_ids": FIXTURE["ability_ids"], "patch_notes": FIXTURE["patchnotes"],
            "hero_stats": FIXTURE["hero_stats"], "rank_pct": cards.rank_percentiles(FIXTURE["rank"]["rows"]),
            "basis": {"public": "9.99b, last 4 days", "blended": False, "pro": "9.99, 152 games"}}
    base.update(over)
    return base


def raw(**over) -> dict:
    base = {"pro": FIXTURE["pro"]["rows"], "pub": FIXTURE["pub"]["rows"], "timings": FIXTURE["timings"],
            "bench": FIXTURE["bench"]}
    base.update(over)
    return base


@pytest.fixture()
def card() -> dict:
    return cards.build_card(1, raw(), ctx(), now=NOW)


def items_of(view: dict) -> set[str]:
    return {e["item"] for ph in ("boots", "early", "mid", "late") for e in view["build"][ph]}


# --- builds by role -------------------------------------------------------------------------------------------
def test_core_and_support_of_the_same_hero_get_different_builds(card):
    core = cards.card(1, 1, 4, data=card)
    support = cards.card(1, 5, 4, data=card)
    assert core["role"] == "1" and support["role"] == "5"
    assert {"phase_boots", "bfury", "invis_sword"} <= items_of(core)
    assert {"tranquil_boots", "blink", "glimmer_cape"} <= items_of(support)
    assert "bfury" not in items_of(support) and "tranquil_boots" not in items_of(core)
    # With how many of the role's players finish each item, and by when.
    bfury = next(e for e in core["build"]["early"] if e["item"] == "bfury")
    assert bfury["n"] == 50 and bfury["share"] == pytest.approx(50 / 60, abs=0.001) and bfury["by_s"] == 860
    assert [e["item"] for e in core["build"]["start"]] == ["tango"]
    assert core["build"]["boots"][0]["item"] == "phase_boots"
    # Components and cheap items aren't build items (Claymore, Magic Wand).
    assert "claymore" not in items_of(core) and "magic_wand" not in items_of(support)
    assert core["patch"] == "9.99b"


def test_a_role_from_roles_py_and_the_fallbacks(card):
    assert cards.card(1, Role(5, "safe", False, True), 4, data=card)["role"] == "5"
    assert cards.card(1, Role(0, "", True, False), 4, data=card)["role"] == "core"  # a first guess: core
    thin = cards.card(1, 3, 4, data=card)  # 12 pro games as an offlaner: the cores' build stands in
    assert thin["role"] == "core" and thin["role_note"] == "thin_position"
    assert cards.card(1, None, 4, data=card)["role"] == "5"  # the most played position


def test_sample_size_floor_leaves_thin_facts_out(card):
    core = cards.card(1, 1, 4, data=card)
    assert "radiance" not in items_of(core)  # 5 players
    assert "glimmer_cape" not in card["timings"]  # 20 timed games
    mu = cards.matchups(card, 4, "normal")
    assert 1 not in {r["hero_id"] for r in mu["worst"] + mu["best"]}  # 300 games against it
    # A rank with too few games of its own falls back to all ranks, and says so.
    assert cards.matchups(card, 5, "normal")["bracket"] == 0
    assert cards.card(1, 1, 5, data=card)["bracket_used"] == 0
    # A role with too few pro games has no pro pace.
    tiny = cards.build_card(1, raw(pro=[r for r in FIXTURE["pro"]["rows"] if r["pos"] != 1 or r["k"] != "g"]
                                   + [{"k": "g", "pos": 1, "x": "", "n": 5, "w": 3, "v1": "50", "v2": "4000",
                                       "v3": "4000", "v4": 5, "v5": "100", "v6": "9000", "v7": "9000", "v8": "6",
                                       "v9": "550", "v10": "700"}]), ctx(), now=NOW)
    view = cards.card(1, 1, 4, data=tiny)  # 5 carry games, 17 core games: only every position together is enough
    assert view["role"] == "all" and view["role_note"] == "all_roles"


# --- item timings ------------------------------------------------------------------------------------------
def test_timing_buckets_become_a_target_time():
    buckets = [[900, 60, 40], [1200, 100, 60], [1500, 80, 40], [1800, 60, 24]]
    fact = cards.item_timing(buckets)
    assert fact["target_s"] == 1200  # half of 300 buyers have it by 20:00
    assert fact["by"] == [pytest.approx(100 / 160, abs=0.001), 160]
    assert fact["after"] == [pytest.approx(64 / 140, abs=0.001), 140]
    assert not fact["turbo_estimate"]
    assert cards.item_timing(buckets[:1]) == {}  # too few games
    assert cards.item_timing([[900, 200, 100], [1200, 10, 5]]) == {}  # one side too thin


def test_turbo_halves_the_target_and_says_so(card):
    turbo = cards.item_timing([[900, 60, 40], [1200, 100, 60], [1500, 80, 40]], Mode("turbo"))
    assert turbo["target_s"] == 600 and turbo["normal_target_s"] == 1200 and turbo["turbo_estimate"]
    view = cards.card(1, 1, 4, "turbo", data=card)
    sb = next(t for t in view["timings"] if t["item"] == "invis_sword")
    assert sb["target_s"] == 600 and sb["turbo_estimate"]
    assert next(e for e in view["build"]["early"] if e["item"] == "bfury")["by_s"] == 430
    block = cards.prompt_block(1, 1, 4, "turbo", data=card)
    assert "TURBO" in block and "by 10:00" in block


def test_key_item_timings_and_power_spikes(card):
    view = cards.card(1, 1, 4, data=card)
    assert [t["item"] for t in view["timings"]] == ["bfury", "invis_sword"]
    bf = view["timings"][0]
    assert bf["target_s"] == 900 and bf["by"][0] > bf["after"][0]
    kinds = {(s["kind"], s.get("item") or s.get("ability") or s.get("name")) for s in view["spikes"]}
    assert ("ult", "Test Strike") in kinds and ("item", "bfury") in kinds and ("talent", "+4 Armor") in kinds
    ult = next(s for s in view["spikes"] if s["kind"] == "ult")
    assert ult["points"] == [6, 13]
    assert view["skills"]["max_order"] == ["Test Bash", "Test Charge"]


# --- pace by rank -------------------------------------------------------------------------------------------
def test_pace_at_your_rank_and_one_rank_up(card):
    p = cards.pace(1, 1, 4, data=card)
    assert p["available"] and p["up_bracket"] == 5 and p["patch"] == "9.99b"
    assert p["ref"][600]["lh"] < p["up"][600]["lh"] <= p["pro"][600]["lh"] == 50
    assert p["ref"][600]["gpm"] < p["up"][600]["gpm"]
    assert p["ref"][1200]["lh"] > p["ref"][600]["lh"]
    assert p["pro"]["n"] == 60
    # A support's numbers come from the support part of the benchmark: far fewer last hits.
    s = cards.pace(1, 5, 4, data=card)
    assert s["ref"][600]["lh"] < p["ref"][600]["lh"] / 3
    assert cards.pace(1, 1, 4, "turbo", data=card) == {"available": False, "why": "turbo"}
    assert cards.pace(1, 1, 8, data=card).get("up") is None  # nobody above Immortal


def test_rank_percentiles():
    pct = cards.rank_percentiles(FIXTURE["rank"]["rows"])
    assert pct["1"] == pytest.approx(0.025) and pct["4"] == pytest.approx(0.4) and pct["8"] == pytest.approx(0.975)


# --- matchups and game length -------------------------------------------------------------------------------
def test_matchups_with_effect_size_and_reasons(card):
    mu = cards.matchups(card, 4, "normal", enemies=["Test Purge", "npc_dota_hero_testmage"])
    worst = {r["hero_id"]: r for r in mu["worst"]}
    assert set(worst) == {2, 4} and worst[4]["delta"] == pytest.approx(-0.1, abs=0.001)
    assert worst[4]["why"] == ["their_dispel", "Test Purge", "Test Charge"]
    # Judged against both heroes' own strength: Test Mage wins only 45% overall.
    best = mu["best"][0]
    assert best["hero_id"] == 3 and best["delta"] == pytest.approx(0.05, abs=0.001) and best["n"] == 3000
    assert {r["hero_id"] for r in mu["lineup"]} == {3, 4}


def test_matchup_reason_rules():
    mute = {"silence": "Hush", "spells": False}
    mage = {"spells": True}
    assert cards.matchup_reason(mage, mute, -0.05) == ["their_silence", "Hush"]
    assert cards.matchup_reason(mute, mage, 0.05) == ["your_silence", "Hush"]
    assert cards.matchup_reason(mute, mage, -0.05) == []  # the reason must point the way the numbers do


def test_game_length(card):
    gl = cards.game_length(card, 4, "normal")
    assert gl["lean"] == "early" and gl["early"] == [0.6, 1500] and gl["late"] == [0.35, 1000]
    assert cards.game_length(card, 4, "turbo") == {}  # not enough Turbo games in either span


# --- patches --------------------------------------------------------------------------------------------------
def test_a_new_patch_makes_a_card_stale(card):
    assert not cards.is_stale(card, "9.99b", NOW + 60)
    assert cards.is_stale(card, "9.99c", NOW + 60)  # a letter patch counts
    assert cards.is_stale(card, "9.99b", NOW + cards.CARD_TTL_S + 1)
    assert cards.is_stale(None, "9.99b")
    assert cards.is_stale({**card, "version": cards.CARD_VERSION - 1}, "9.99b", NOW)
    thin = {**card, "basis": {**card["basis"], "blended": True}}
    assert not cards.is_stale(thin, "9.99b", NOW + 3600) and cards.is_stale(thin, "9.99b", NOW + 25 * 3600)


def test_current_patch_from_the_patch_list(tmp_path):
    (tmp_path / "patch.json").write_text(json.dumps(FIXTURE["patch"]), encoding="utf-8")
    assert cards.current_patch(tmp_path) == ("9.99", 0)
    (tmp_path / "patchnoteslist.json").write_text(json.dumps(FIXTURE["patchnoteslist"]), encoding="utf-8")
    assert cards.current_patch(tmp_path) == ("9.99b", 1785000000)
    assert cards.major("9.99b") == "9.99"


def test_patch_note_extraction():
    facts = cards.patch_facts(1, FIXTURE["patchnotes"], FIXTURE["ability_ids"], FIXTURE["abilities"],
                              FIXTURE["items"], {"phase_boots", "invis_sword"})
    assert facts["hero"] == ["9.99b: Base strength increased by 2",
                             "9.99b: Talent: Level 10 Armor talent increased from +4 to +5",
                             "9.99a: Test Bash: Cooldown increased from 1.2 to 1.5"]
    assert facts["items"] == ["9.99b: Shadow Blade: Cooldown increased from 22s to 23s",
                              "9.99a: Phase Boots: Armor decreased from 4 to 3"]


# --- the prompt block ------------------------------------------------------------------------------------------
def test_prompt_block_is_tight_and_names_the_patch(card):
    block = cards.prompt_block(1, 1, 4, "normal", ["Test Purge", "Test Mage"], data=card)
    words = len(block.split())
    assert 60 <= words <= cards.PROMPT_WORDS
    first = block.splitlines()[0]
    assert "patch 9.99b" in first and "Archon" in first
    assert block.splitlines()[1].startswith("Vs this lineup")  # the most decision-relevant first
    assert "Test Purge" in block and "dispels your Test Charge" in block
    assert "Battle Fury" in block
    # A tight budget drops whole lines, never the patch.
    small = cards.prompt_block(1, 1, 4, "normal", data=card, budget=40)
    assert len(small.split()) <= 40 and "patch 9.99b" in small
    assert cards.prompt_block(999, cache_dir=Path("nowhere")) == ""


def test_saved_cards_load_by_id_and_name(tmp_path, card):
    (tmp_path / "heroes.json").write_text(json.dumps(FIXTURE["heroes"]), encoding="utf-8")
    cards.save(card, tmp_path)
    assert cards.load(1, tmp_path)["hero_id"] == 1
    assert cards.load("Test Bull", tmp_path)["hero_id"] == 1
    assert cards.load("npc_dota_hero_testbull", tmp_path)["hero_id"] == 1
    assert cards.load(2, tmp_path) is None
    assert "patch 9.99b" in cards.prompt_block("Test Bull", 5, 4, cache_dir=tmp_path)


# --- the study pipeline ----------------------------------------------------------------------------------------
class FakeHTTP:
    """Answers study.py's requests from the fixture; counts them; can fail."""

    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url: str):
        self.urls.append(url)
        if self.fail:
            raise urllib.error.URLError("offline")
        if "/explorer?sql=" in url:
            sql = urllib.parse.unquote(url.split("sql=", 1)[1])
            if "player_matches" in sql:
                return copy.deepcopy(FIXTURE["pro"])
            if "unnest(foes)" in sql:
                return copy.deepcopy(FIXTURE["pub"])
            return copy.deepcopy(FIXTURE["rank"])
        for name in ("heroes", "items", "abilities", "hero_abilities", "ability_ids", "patch"):
            if url.endswith(f"/{name}.json"):
                return copy.deepcopy(FIXTURE[name])
        if url.endswith("/heroStats"):
            return copy.deepcopy(FIXTURE["hero_stats"])
        if "patchnoteslist" in url:
            return copy.deepcopy(FIXTURE["patchnoteslist"])
        if "patchnotes?version=" in url:
            return copy.deepcopy(FIXTURE["patchnotes"][url.split("version=")[1].split("&")[0]])
        if "benchmarks" in url:
            return copy.deepcopy(FIXTURE["bench"])
        if "itemTimings" in url:
            return copy.deepcopy(FIXTURE["timings"])
        raise urllib.error.URLError(f"unexpected {url}")


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make_study(tmp_path, http, clock=None, per_day=1000, per_minute=50) -> study.Study:
    clock = clock or FakeClock()
    limiter = study.RateLimiter(per_minute=per_minute, per_day=per_day, clock=clock, sleep=clock.sleep,
                                today=lambda: "day-1")
    return study.Study(tmp_path, fetch=http, sleep=clock.sleep, limiter=limiter, log=lambda *_: None)


def test_rate_limiter_paces_by_minute_and_by_day():
    clock = FakeClock()
    state: dict = {}
    lim = study.RateLimiter(per_minute=3, per_day=5, min_gap_s=1.0, clock=clock, sleep=clock.sleep,
                            today=lambda: "d1", state=state)
    assert all(lim.acquire() for _ in range(4))
    assert clock.slept == [1.0, 1.0, 58.0]  # a second apart, then wait out the minute
    assert lim.acquire() and not lim.acquire()  # the day's five are spent
    assert state == {"day": "d1", "calls": 5}
    lim.today = lambda: "d2"
    assert lim.acquire() and state["calls"] == 1  # a new day


def test_study_builds_cards_and_resumes_where_it_stopped(tmp_path):
    http = FakeHTTP()
    first = make_study(tmp_path, http).run([1], max_calls=5)
    assert first["stopped"] == "step_budget" and first["built"] == [] and len(http.urls) == 5
    second = make_study(tmp_path, http).run([1])
    assert second["built"] == [1] and second["stopped"] == ""
    total = len(http.urls)
    assert total == 15  # nothing asked twice: 11 shared answers, 4 for the hero
    c = cards.load(1, tmp_path)
    assert c["patch"] == "9.99b" and c["basis"]["pro_games"] == 152 and not c["basis"]["blended"]
    # A current card isn't studied again, and no request goes out.
    third = make_study(tmp_path, http).run([1])
    assert third["current"] == 1 and len(http.urls) == total
    # No account id, match id or player anywhere in what was asked.
    assert not any("account" in u or "players/" in u for u in http.urls)


def test_a_new_patch_marks_the_card_for_study_again(tmp_path):
    http = FakeHTTP()
    make_study(tmp_path, http).run([1])
    listing = copy.deepcopy(FIXTURE["patchnoteslist"])
    listing["patches"].append({"patch_number": "9.99c", "patch_name": "9.99c", "patch_timestamp": 1786000000})
    (tmp_path / "patchnoteslist.json").write_text(json.dumps(listing), encoding="utf-8")
    assert cards.is_stale(cards.load(1, tmp_path), cards.current_patch(tmp_path)[0])
    notes_c = copy.deepcopy(FIXTURE["patchnotes"]["9.99b"])
    http_c = FakeHTTP()

    def fetch(url):
        return notes_c if "version=9.99c" in url else http_c(url)
    result = make_study(tmp_path, fetch).run([1])
    assert result["built"] == [1] and cards.load(1, tmp_path)["patch"] == "9.99c"


def test_failures_back_off_then_pause_without_hammering(tmp_path):
    clock = FakeClock()
    http = FakeHTTP(fail=True)
    result = make_study(tmp_path, http, clock).run([1])
    assert result["stopped"] == "paused_after_errors" and len(http.urls) == study.MAX_FAILURES
    assert clock.slept == [study.BACKOFF_S, study.BACKOFF_S * 2]  # a growing wait, then a pause
    # The pause is remembered: the next run sends nothing until it's over.
    again = FakeHTTP()
    assert make_study(tmp_path, again).run([1])["stopped"] == "paused_after_errors" and again.urls == []


def test_the_day_budget_stops_the_study(tmp_path):
    http = FakeHTTP()
    result = make_study(tmp_path, http, per_day=4).run([1])
    assert result["stopped"] == "day_budget" and len(http.urls) == 4


def test_explorer_errors_are_not_cached(tmp_path):
    def fetch(url):
        if "/explorer?sql=" in url and "player_matches" in urllib.parse.unquote(url):
            return {"err": "statement timeout", "rows": None}
        return FakeHTTP()(url)
    result = make_study(tmp_path, fetch).run([1])
    assert result["failed"] == [1]
    assert not list((tmp_path / study.STUDY_DIR).glob("pro_*"))


def test_background_order_and_steps(tmp_path):
    assert study.priority([3, 1], [1, 2], [1, 2, 3, 4]) == [3, 1, 2, 4]
    http = FakeHTTP()
    s = make_study(tmp_path, http)
    stop = threading.Event()
    result = study.study_in_background(current=[1], favourites=[], stop=stop, cache_dir=tmp_path,
                                       calls_per_step=3, gap_s=0, study=s)
    assert 1 in result["built"]
    assert cards.load(1, tmp_path) is not None


def test_queries_are_hero_level_and_well_formed():
    pro = study.PRO_SQL.format(hero_id=71, patches="'7.41'")
    pub = study.PUB_SQL.format(hero_id=71, start=1700000000)
    for sql in (pro, pub, study.RANK_SQL.format(start=1700000000)):
        assert "account_id" not in sql and "{" not in sql
    assert "hero_id=71" in pro and "mp.patch IN ('7.41')" in pro
    assert "71 = ANY(radiant_team)" in pub and "start_time >= 1700000000" in pub
    assert cards.position_of(1, 1) == 1 and cards.position_of(2, 2) == 2 and cards.position_of(3, 4) == 3
    assert cards.position_of(4, 1) == 5 and cards.position_of(5, 3) == 4
