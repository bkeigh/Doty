"""One thing to practise per game (knowledge/focus.py, rules/focus.py): each topic measured from a review, the
choice of this game's focus, what's said during the game (at most three lines), the history file, and the
review card."""

import json

from doty.advice import AdviceBus
from doty.config import Config, SuggestConfig
from doty.knowledge import focus
from doty.review import Death, MatchRecorder, Review
from doty.rules import RuleEngine, default_rules
from doty.rules.focus import TITLES, Focus
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN, Recorder
from .test_app_ui import make_window, qapp  # noqa: F401  (the fixture)


def _review(**kw):
    base = dict(match_id="", hero="npc_dota_hero_juggernaut", hero_id=8, duration=2400, recorded_from=-60,
                lh_10=31, mode="normal")
    base.update(kw)
    return Review(**base)


NO_TARGET = focus.Target(0, "fixed")


# measuring ---------------------------------------------------------------------------------------------------------
def test_last_hits_at_ten_and_its_target():
    t = focus.TOPICS["lh10"]
    assert t.measure(_review(), NO_TARGET) == 31
    assert t.measure(_review(recorded_from=700), NO_TARGET) is None  # (the lane wasn't seen)
    pace = {"available": True, "ref": {600: {"lh": 38}}, "up": {600: {"lh": 45}}}
    assert t.target({"pace": pace}) == focus.Target(45, "card_up")
    assert t.target({"pace": {"available": True, "ref": {600: {"lh": 38}}}}) == focus.Target(38, "card_rank")
    assert t.target({"pace": {"available": False, "why": "turbo"}, "level": "new"}) == focus.Target(20, "fixed")
    assert t.hit(45, 45) and not t.hit(44, 45) and t.gap(30, 45) == 1.5


def test_deaths_without_a_tp_or_holding_gold():
    deaths = [Death(clock=300, had_tp=False, gold_before=100), Death(clock=900, had_tp=True, gold_before=2600),
              Death(clock=1200, had_tp=False, gold_before=2100), Death(clock=1300, had_tp=None)]
    r = _review(deaths=deaths)
    assert focus.TOPICS["deaths_no_tp"].measure(r, NO_TARGET) == 2
    assert focus.TOPICS["deaths_with_gold"].measure(r, NO_TARGET) == 2
    assert focus.TOPICS["deaths_no_tp"].measure(_review(duration=300), NO_TARGET) is None  # (too short to judge)


def test_first_key_item_against_the_cards_timing():
    t = focus.TOPICS["key_item"]
    card = {"timings": [{"item": "bfury", "name": "Battle Fury", "target_s": 900}]}
    target = t.target({"card": card})
    assert (target.item, target.item_name, target.at, target.value) == ("item_bfury", "Battle Fury", 900, 0)
    assert t.measure(_review(purchases=[("item_bfury", 1020)]), target) == 120  # two minutes late
    assert t.measure(_review(purchases=[("item_bfury", 840)]), target) == -60
    assert t.measure(_review(duration=1500), target) == 600  # never bought: at least this late
    assert t.target({"card": {"timings": []}}) is None


def test_skill_points_buyback_and_farm_stalls():
    assert focus.TOPICS["skill_points"].measure(_review(unspent_point_s=150), NO_TARGET) == 2.5
    late = [Death(clock=1900, buyback_ready=False), Death(clock=2000, buyback_ready=True),
            Death(clock=1000, buyback_ready=False)]
    assert focus.TOPICS["buyback"].measure(_review(deaths=late), NO_TARGET) == 1
    assert focus.TOPICS["buyback"].measure(_review(deaths=late, mode="turbo"), NO_TARGET) is None  # (normal only)
    # 400 gold a minute, except minutes 14 and 15 at 50.
    earned, minutes = 0.0, []
    for m in range(0, 21):
        earned += 0 if m == 0 else (50 if m in (14, 15) else 400)
        minutes.append({"clock": m * 60, "gpm": int(earned / m) if m else 0})
    assert focus.farm_stall_minutes(_review(minutes=minutes)) == 2
    assert focus.farm_stall_minutes(_review(minutes=minutes, deaths=[Death(clock=890)])) == 1  # (dead: not a stall)


def test_grade_measures_every_topic_it_can():
    r = _review(deaths=[Death(clock=500, had_tp=False)], unspent_point_s=30)
    got = focus.grade(r, {"level": "some", "pace": {"available": False}, "card": {}})
    assert got["lh10"] == [31, 40] and got["deaths_no_tp"] == [1, 0] and got["skill_points"] == [0.5, 1]
    assert "key_item" not in got  # (no card)


# choosing ------------------------------------------------------------------------------------------------------------
def _game(results, focus_key="", mode="normal", role="core"):
    return {"when": 0, "mode": mode, "role": role, "level": "some", "hero": "", "focus": focus_key,
            "results": results}


def test_with_no_history_the_levels_default():
    assert focus.choose([], "new").key == "lh10"
    assert focus.choose([], "new", "support").key == "deaths_no_tp"
    assert focus.choose([], "experienced", "core").key == "key_item"
    assert focus.choose([], "some", topics_off={"farm"}).key == "deaths_with_gold"  # (farm talk is switched off)
    assert focus.choose([], "pro").key == "key_item" and "lh10" not in {
        t.key for t in focus.TOPICS.values() if focus.fits(t, "pro", "")}


def test_the_worst_gap_wins_and_mastery_and_rotation_move_on():
    history = [_game({"lh10": [30, 45], "deaths_no_tp": [2, 0]}) for _ in range(2)]
    assert focus.choose(history, "some", "core").key == "deaths_no_tp"  # 2 deaths > 1.5 tens of last hits
    assert focus.choose(history, "some", "core").recent == (2, 2)
    # Three games in a row without a death with no TP: mastered, last hits next.
    history += [_game({"lh10": [32, 45], "deaths_no_tp": [0, 0]}) for _ in range(3)]
    assert focus.mastered(history, "deaths_no_tp") and focus.choose(history, "some", "core").key == "lh10"
    # The same focus three games running gives another topic with a gap its turn.
    stuck = [_game({"lh10": [30, 45], "deaths_no_tp": [3, 0]}, focus_key="deaths_no_tp") for _ in range(3)]
    choice = focus.choose(stuck, "some", "core")
    assert choice.key == "lh10" and choice.why == "rotation"


def test_turbo_and_normal_games_never_mix():
    turbo = [_game({"deaths_no_tp": [4, 0]}, mode="turbo") for _ in range(3)]
    normal = [_game({"lh10": [20, 45]}) for _ in range(3)]
    assert focus.choose(turbo + normal, "some", "core", "normal").key == "lh10"
    assert focus.choose(turbo + normal, "some", "core", "turbo").key == "deaths_no_tp"
    assert focus.trend(turbo + normal, "lh10", "turbo") == [] and focus.trend(turbo + normal, "lh10") == [20] * 3


def test_a_level_gets_only_its_topics():
    history = [_game({"skill_points": [9, 1], "key_item": [300, 0]}) for _ in range(3)]
    assert focus.choose(history, "experienced", "core").key == "key_item"  # (skill points are for beginners)
    assert focus.choose(history, "new", "core").key == "skill_points"


# during the game ----------------------------------------------------------------------------------------------------
def _play(tmp_path, end_clock, history=None, level="some", suggest=None):
    path = tmp_path / "focus.json"
    if history is not None:
        path.write_text(json.dumps({"format": 1, "games": history}), encoding="utf-8")
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, Config().rules, None, rules=[Focus(history_path=path)])
    engine.level_of = lambda: level
    engine.suggest = suggest
    m = ScriptedMatch(end_clock=end_clock)
    while m.s.clock < end_clock:
        m.step(1.0)
        engine.on_snapshot(store.ingest(m.payload(TOKEN)))
    return store, rec.items


def test_the_focus_is_said_at_the_horn_and_checked_twice_then_nothing(tmp_path):
    history = [_game({"lh10": [v, 60]}, role="") for v in (28, 30, 35)]
    store, said = _play(tmp_path, 26 * 60, history)
    assert store.memory["focus"] == "lh10" and store.memory["focus_plan"]["target"] == 40  # (level "some", no card)
    texts = [a.text for a in said]
    assert texts[0] == "This game: 40 last hits by 10:00. You've averaged 31."
    assert texts[1] == "23 last hits at 5:00: on pace for your 40."  # (the scripted farm: 5 a minute, a death)
    assert texts[2] == "50 last hits at 10:00: target hit."
    assert len(said) == 3 and all(a.category == "focus" and a.priority == 3 for a in said)


def test_behind_at_five_minutes_says_what_the_target_needs(tmp_path):
    history = [_game({"lh10": [v, 60]}, role="") for v in (30, 32, 34)]
    store, said = _play(tmp_path, 700, history, level="experienced")  # (no hero card: key item can't be the focus)
    assert store.memory["focus"] == "lh10"
    assert [a.text for a in said][1:] == ["23 last hits at 5:00. Your 50 needs 25 by now: stay on the wave.",
                                          "50 last hits at 10:00: target hit."]


def test_a_switched_off_subject_is_never_the_focus_nor_talked_about(tmp_path):
    store, said = _play(tmp_path, 700, suggest=SuggestConfig(farm=False))
    assert store.memory["focus"] != "lh10"
    assert said and said[0].text.startswith("This game:") and len(said) == 1


def test_the_focus_rule_is_registered_and_the_prompt_says_it(tmp_path):
    from doty.coach import prompts

    assert any(isinstance(r, Focus) for r in default_rules())
    store, _said = _play(tmp_path, 30)
    summary = prompts.state_summary(store.current, store, None, [])
    assert summary["practice_focus"] == "Last hits at 10:00 (target 40)"
    assert set(TITLES) == set(focus.TOPICS) and all(TITLES[k] == t.title for k, t in focus.TOPICS.items())


# after the game ----------------------------------------------------------------------------------------------------
def test_the_review_grades_the_focus_and_the_history_holds_nothing_identifying(tmp_path):
    path = tmp_path / "focus.json"
    history = [_game({"lh10": [v, 40]}, role="") for v in (29, 31)]
    path.write_text(json.dumps({"format": 1, "games": history}), encoding="utf-8")
    store, bus = GameStateStore(), AdviceBus()
    engine = RuleEngine(store, bus, Config().rules, None, rules=[Focus(history_path=path)])
    rec = MatchRecorder(None, out_dir=tmp_path / "reviews")
    rec.focus_history = path
    m = ScriptedMatch(match_id="8123456789", end_clock=1500)  # (not the demo's id: a game that counts)
    while m.s.clock < 1500:
        m.step(1.0)
        payload = m.payload(TOKEN)
        payload["player"].update(steamid="76561190000000001", accountid="123456789", name="Secret Player")
        snap = store.ingest(payload)
        rec.on_snapshot(snap, store)
        engine.on_snapshot(snap)
    review = rec.last_review
    p = review.practice
    assert p["key"] == "lh10" and p["value"] == 50 and p["target"] == 40 and p["hit"] is True
    assert p["trend"] == [29, 31, 50]
    assert review.unspent_point_s > 60  # (the scripted player holds a point from 1:15 to 3:20)
    text = path.read_text(encoding="utf-8")
    for private in ("8123456789", "76561190000000001", "123456789", "Secret Player", "Doty Sim"):
        assert private not in text
    games = focus.load_history(path)
    assert set(games[-1]) == {"when", "mode", "role", "level", "hero", "focus", "results"}
    assert games[-1]["results"]["lh10"] == [50, 40] and games[-1]["mode"] == "normal"
    again = Review.from_dict(json.loads(json.dumps(review.to_dict())))
    assert again.practice == p and Review.from_dict({}).practice == {}  # (older reviews load)


def test_history_round_trip_keeps_the_last_ones(tmp_path):
    path = tmp_path / "h.json"
    for n in range(focus.HISTORY_KEPT + 5):
        focus.add_game(_game({"lh10": [n, 40]}), path)
    games = focus.load_history(path)
    assert len(games) == focus.HISTORY_KEPT and games[-1]["results"]["lh10"] == [focus.HISTORY_KEPT + 4, 40]
    assert focus.load_history(tmp_path / "missing.json") == []


# the review card --------------------------------------------------------------------------------------------------
def test_the_review_card_shows_the_focus_and_how_doty_did(qapp, tmp_path):
    from doty.ui.coach_page import CoachPage

    p = {"key": "lh10", "title": "Last hits at 10:00", "value": 38, "target": 45, "hit": False,
         "trend": [29, 31, 30, 36, 38]}
    html = CoachPage.practice_html(p)
    assert "Focus: Last hits at 10:00 — 38 (target 45)." in html and "Last 5: 29, 31, 30, 36, 38." in html
    assert "Missed" in html
    item = CoachPage.practice_html({"key": "key_item", "value": -60, "target": 0, "hit": True,
                                    "item_name": "Battle Fury", "trend": [300, -60]})
    assert "Battle Fury: 1 min early" in item and "Last 2: 5, -1." in item and "Hit" in item
    ctl, win = make_window(qapp, tmp_path)
    page = win.coach_page
    page.recap = {"result": "lost", "game": {"hero": "Juggernaut", "kda": (1, 2, 3)}, "clock": 1500, "lines": []}
    page.review = _review(practice=p)
    page._show_recap()
    assert not page.recap_practice.isHidden() and "38" in page.recap_practice.text()
    page._on_coach_summary("Last game Doty said 40 lines in 25 minutes.")
    assert page.recap_coach.text() == "How Doty did: Last game Doty said 40 lines in 25 minutes."
    page.review = _review()
    page._show_recap()
    assert page.recap_practice.isHidden()
    win.close()
