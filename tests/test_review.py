"""Post-game review: what the recorder notices during a match, and what it saves afterwards."""

import json
import os

from doty.advice import Advice, AdviceBus, Priority
from doty.config import Config
from doty.knowledge.constants import Knowledge
from doty.review import (
    Death, MatchRecorder, Review, focus_points, latest_review, list_reviews, load_review, review_prompt,
)
from doty.rules import RuleEngine
from doty.sim import ScriptedMatch
from doty.state import IN_PROGRESS, POST_GAME, GameStateStore, Snapshot

from .conftest import TOKEN

SB = "npc_dota_hero_spirit_breaker"


def _kb() -> Knowledge:
    kb = Knowledge()
    kb.heroes = {SB: {"id": 71, "localized_name": "Spirit Breaker"},
                 "npc_dota_hero_juggernaut": {"id": 8, "localized_name": "Juggernaut"}}
    kb.items = {
        "item_phase_boots": {"dname": "Phase Boots", "cost": 1500, "components": ["boots", "chainmail"]},
        "item_wraith_band": {"dname": "Wraith Band", "cost": 505, "components": ["slippers", "circlet"]},
        "item_magic_wand": {"dname": "Magic Wand", "cost": 450, "components": ["magic_stick", "branches"]},
        "item_cornucopia": {"dname": "Cornucopia", "cost": 1200},
        "item_quelling_blade": {"dname": "Quelling Blade", "cost": 100},
        "item_blink": {"dname": "Blink Dagger", "cost": 2250},
        "item_tango": {"dname": "Tango", "cost": 90},
    }
    kb.benchmarks = {71: {"result": {"last_hits_per_min": [{"percentile": 0.1, "value": 2.9},
                                                           {"percentile": 0.5, "value": 5.2}]}}}
    return kb


def payload(clock, *, match="m1", state=IN_PROGRESS, hp=1.0, alive=True, pos=(-6000, -6000), gold=500,
            deaths=0, tp=True, items=(), team="radiant", win="none", lh=0, dn=0, level=5):
    inv = {f"slot{i}": {"name": items[i] if i < len(items) else "empty"} for i in range(9)}
    return {
        "map": {"matchid": match, "clock_time": clock, "game_time": clock + 90, "game_state": state,
                "win_team": win},
        "player": {"team_name": team, "gold": gold, "kills": 2, "deaths": deaths, "assists": 7, "last_hits": lh,
                   "denies": dn, "gpm": 410, "xpm": 455},
        "hero": {"name": SB, "id": 71, "level": level, "alive": alive, "health": int(1000 * hp) if alive else 0,
                 "max_health": 1000, "xpos": pos[0], "ypos": pos[1]},
        "items": {**inv, "teleport0": {"name": "item_tpscroll", "charges": 1} if tp else {"name": "empty"}},
    }


def feed(rec, store, raw):
    snap = store.ingest(raw, received_t=raw["map"]["clock_time"] + 1000.0)  # fake wall time = game clock
    rec.on_snapshot(snap, store)
    return snap


def test_scripted_match_items_death_and_auto_finish(tmp_path):
    """The whole pipeline as the app wires it: recorder before the rules, subscribed to the bus."""
    store, bus = GameStateStore(), AdviceBus()
    rec = MatchRecorder(_kb(), out_dir=tmp_path)
    bus.subscribe(rec.on_advice)
    engine = RuleEngine(store, bus, Config().rules, kb=None)
    match = ScriptedMatch(end_clock=700)
    while match.s.clock < 705:
        match.step(1.0)
        snap = store.ingest(match.payload(TOKEN), received_t=match.s.clock + 1000.0)
        rec.on_snapshot(snap, store)
        engine.on_snapshot(snap)

    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == ["match-doty-sim-1.json"]
    review = rec.last_review
    assert review.hero_name == "Juggernaut" and review.team == "radiant" and review.won is True
    # Magic Wand (450), Cornucopia (a component) and the starting items aren't build targets.
    assert review.items == [("Phase Boots", 400), ("Wraith Band", 400)]
    assert review.lh_10 == 50 and review.kda == (0, 1, 0) and review.duration >= 700

    [death] = review.deaths
    assert death.clock == 286 and death.had_tp is True and death.gold_before >= 2000
    assert 190 <= death.gold_lost <= 200
    # The low-HP rule spoke a few seconds before; HP was under a third for only ~3 s, so no fight tag.
    assert any(line["category"] == "survival" for line in death.said)
    assert death.causes == ["after_warning", "unspent_gold"]
    assert rec.finish() is None  # already written at post-game
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_death_on_low_hp_without_tp_on_the_enemy_side(tmp_path):
    store, rec = GameStateStore(), MatchRecorder(None, out_dir=tmp_path)
    deep = (4500, 3800)  # Dire's jungle, for a Radiant hero
    for clock in range(600, 612):
        feed(rec, store, payload(clock, hp=0.8 if clock < 603 else 0.3, pos=deep, tp=False, gold=600,
                                 items=["item_blink", "item_tango"]))
    feed(rec, store, payload(612, alive=False, pos=deep, tp=False, gold=400, deaths=1))
    feed(rec, store, payload(613, alive=False, pos=(-7000, -6500), tp=False, gold=405, deaths=1))

    [death] = rec._deaths
    assert death.causes == ["low_hp_fight", "no_tp", "enemy_side"]
    assert death.position == deep and death.level == 5 and death.gold_lost == 200
    assert death.low_hp_s >= 8 and death.hp_trace[-1] == (-1.0, 0.3)
    assert death.items == ["Blink", "Tango"]  # no item data: names from the item keys


def test_death_counted_once_when_signals_arrive_a_payload_apart(tmp_path):
    store, rec = GameStateStore(), MatchRecorder(None, out_dir=tmp_path)
    feed(rec, store, payload(300, deaths=2))  # joined with two deaths already: not new ones
    feed(rec, store, payload(301, deaths=3))  # the counter moves first...
    feed(rec, store, payload(302, alive=False, deaths=3))  # ...then the alive flag
    feed(rec, store, payload(340, deaths=3))
    raw = payload(400, deaths=4)
    del raw["hero"]["alive"]  # no alive flag at all: the counter alone still counts
    feed(rec, store, raw)
    assert [d.clock for d in rec._deaths] == [301, 400]


def test_death_soon_after_a_warning(tmp_path):
    store, rec = GameStateStore(), MatchRecorder(None, out_dir=tmp_path)

    def at(clock, **kw):
        feed(rec, store, payload(clock, **kw))

    at(250)
    rec.on_advice(Advice("Low HP — back off", Priority.CRITICAL, "survival"))  # 35 s before: too old
    at(275)
    rec.on_advice(Advice("2,600 gold unspent", Priority.NORMAL, "economy"))
    rec.on_advice(Advice("Their mid is missing", Priority.INFO, "watching", source="coach"))
    rec.on_advice(Advice("Half a sentence", Priority.HIGH, "coach", source="coach", final=False))
    rec.on_advice(Advice("Spoken only", Priority.HIGH, "coach", source="coach", display=False))
    rec.on_advice(Advice("should I gank?", Priority.INFO, "coach", source="player"))
    at(285)
    at(286, alive=False, deaths=1)
    first = rec._deaths[0]
    assert [line["text"] for line in first.said] == ["2,600 gold unspent", "Their mid is missing"]
    assert "after_warning" not in first.causes  # neither line was a warning

    at(320)
    rec.on_advice(Advice("Back off, they have five up", Priority.HIGH, "coach", source="coach"))
    at(330)
    at(334, alive=False, deaths=2)
    second = rec._deaths[1]
    assert second.causes == ["after_warning"]
    assert second.said == [{"clock": 320, "source": "coach", "category": "coach", "priority": 2,
                            "text": "Back off, they have five up"}]


def test_post_game_finishes_once_and_round_trips(tmp_path):
    kb = _kb()
    store, rec = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)
    for clock in range(0, 640, 10):
        feed(rec, store, payload(clock, lh=int(clock * 38 / 600), dn=clock // 100, tp=clock < 600,
                                 gold=2500 if clock >= 600 else 500, items=["item_blink"] if clock >= 500 else []))
    feed(rec, store, payload(650, alive=False, tp=False, deaths=1, gold=2500))
    for clock in (660, 670):
        feed(rec, store, payload(clock, state=POST_GAME, win="dire", deaths=1, lh=40))

    path = tmp_path / "match-m1.json"
    assert list_reviews(tmp_path) == [path]
    review = load_review(path)
    assert review == rec.last_review
    assert review.won is False and review.hero_name == "Spirit Breaker" and review.kda == (2, 1, 7)
    assert review.items == [("Blink Dagger", 500)] and review.lh_10 == 38 and review.dn_10 == 6
    assert review.benchmark_lh_per_min == 5.2 and review.lh_per_min_10 == 3.8
    assert review.minutes[10]["clock"] == 600 and review.minutes[10]["last_hits"] == 38
    assert review.deaths[0].causes == ["no_tp", "unspent_gold"]
    assert review.focus[0].startswith("38 last hits at 10:00; Spirit Breaker players average 5.2 a minute (52 ")
    json.loads(path.read_text(encoding="utf-8"))  # plain JSON

    assert rec.finish() is None  # post-game already wrote it
    feed(rec, store, payload(0, match="m2"))  # the next match starts fresh
    feed(rec, store, payload(30, match="m2"))
    os.utime(path, (1_000_000, 1_000_000))  # make the first file clearly older
    second = rec.finish()  # Doty stopping mid-game
    assert second.match_id == "m2" and second.won is None and second.deaths == []
    assert list_reviews(tmp_path) == [tmp_path / "match-m2.json", path]
    assert latest_review(tmp_path) == second


def test_a_new_match_id_finishes_the_previous_match(tmp_path):
    store, rec = GameStateStore(), MatchRecorder(None, out_dir=tmp_path)
    feed(rec, store, payload(100, match="a"))
    feed(rec, store, payload(-60, match="b"))
    assert rec.last_review.match_id == "a"
    assert [p.name for p in list_reviews(tmp_path)] == ["match-a.json"]


def test_joining_mid_game_leaves_earlier_buys_untimed(tmp_path):
    store, rec = GameStateStore(), MatchRecorder(_kb(), out_dir=tmp_path)
    feed(rec, store, payload(900, items=["item_blink"]))
    feed(rec, store, payload(960, items=["item_blink", "item_phase_boots"]))
    review = rec.finish()
    assert review.items == [("Phase Boots", 960)] and review.recorded_from == 900 and review.lh_10 is None
    assert "only saw the game from 15:00" in review_prompt(review)


def test_focus_points_rank_by_impact():
    def died(*causes):
        return Death(clock=600, causes=list(causes))

    review = Review(hero_name="Spirit Breaker", lh_10=38, lh_per_min_10=3.8, benchmark_lh_per_min=5.2,
                    deaths=[died("no_tp", "after_warning"), died("no_tp", "after_warning"),
                            died("no_tp", "after_warning"), died("no_tp")])
    assert focus_points(review) == [
        "You died 4 times without a TP scroll: always carry one.",
        "3 of your deaths came within 20 s of a warning: back off when Doty says so.",
        "38 last hits at 10:00; Spirit Breaker players average 5.2 a minute (52 in ten minutes): "
        "last-hit every wave and farm between fights.",
    ]
    review.benchmark_lh_per_min = None  # no benchmark, no farm point
    assert len(focus_points(review)) == 2
    review.benchmark_lh_per_min = 4.0  # close enough to typical
    assert not any("last hits" in p for p in focus_points(review))

    one = Review(deaths=[died("low_hp_fight")])
    assert focus_points(one) == ["Your death likely came from staying in a fight on low HP: walk away once you "
                                 "drop under a third of your health."]
    assert focus_points(Review()) == []
    many = Review(deaths=[died() for _ in range(9)] + [died("enemy_side")])
    assert focus_points(many)[0].startswith("You died 10 times: before you walk forward")


def test_missing_sections_never_raise(tmp_path):
    rec = MatchRecorder(None, out_dir=tmp_path)
    assert rec.finish() is None  # nothing seen yet
    for raw in [
        {},
        {"provider": {"name": "Dota 2"}},
        {"hero": {"name": SB, "alive": False}},  # no map: no clock to stamp, skipped
        {"map": {"matchid": "x", "clock_time": 100, "game_state": IN_PROGRESS}},  # no hero yet
        {"map": {"matchid": "x", "clock_time": 101, "game_state": IN_PROGRESS}, "hero": {"name": SB},
         "player": "oops", "items": "nope"},
        {"map": {"matchid": "x", "clock_time": 102}, "hero": {"name": SB, "alive": False}},
        {"map": {"matchid": "x", "clock_time": 103}, "hero": {"name": SB, "alive": True, "health": 500},
         "player": {"deaths": None}},
        {"map": "garbage", "hero": {"name": SB}},
        {"map": {"matchid": "x", "clock_time": 110}, "hero": {"name": SB, "alive": False}},
    ]:
        rec.on_snapshot(Snapshot(raw))  # no store: the recorder keeps its own HP history
    assert not rec._failed  # handled, not swallowed by the safety net
    review = rec.finish()
    assert review.match_id == "x" and review.hero_name == "Spirit Breaker" and review.kda == (0, 0, 0)
    [death] = review.deaths
    assert death.clock == 110 and death.had_tp is None and death.position is None  # nothing to judge by
    assert death.causes == [] and "Deaths (1):" in review_prompt(review)

    # Doty opened after the match was over: nothing worth a review.
    late = MatchRecorder(None, out_dir=tmp_path / "late")
    late.on_snapshot(Snapshot(payload(2400, state=POST_GAME, win="radiant")))
    assert late.last_review is None and late.finish() is None and not (tmp_path / "late").exists()


def test_unreadable_files_are_skipped(tmp_path):
    good = tmp_path / "match-1.json"
    good.write_text(json.dumps(Review(match_id="1", hero_name="Axe").to_dict()), encoding="utf-8")
    os.utime(good, (1_000_000, 1_000_000))
    (tmp_path / "match-2.json").write_text("{not json", encoding="utf-8")
    assert latest_review(tmp_path).hero_name == "Axe"
    assert latest_review(tmp_path / "missing") is None and list_reviews(tmp_path / "missing") == []
    # Unknown keys from a newer version are ignored.
    assert Review.from_dict({"match_id": "9", "future_field": 1}).match_id == "9"


def test_review_prompt_is_compact_and_complete():
    review = Review(
        match_id="1", hero_name="Spirit Breaker", team="radiant", won=False, duration=2292, kda=(4, 9, 12),
        gpm=380, xpm=420, last_hits=140, denies=5, lh_10=38, dn_10=3, benchmark_lh_per_min=5.2,
        items=[("Phase Boots", 450), ("Shadow Blade", 962)],
        deaths=[Death(clock=286, level=4, gold_before=2100, gold_lost=190, causes=["no_tp", "after_warning"],
                      said=[{"clock": 280, "source": "rules", "category": "survival", "priority": 1,
                             "text": "Low HP (20%) and dropping — back off"}])],
        focus=["You died once without a TP scroll: always carry one."])
    text = review_prompt(review)
    assert "Spirit Breaker (radiant), lost, game length 38:12" in text
    assert "Phase Boots 7:30, Shadow Blade 16:02" in text
    assert "4:46, level 4 | had no TP scroll; died within 20 s of a warning from Doty" in text
    assert 'Doty said "Low HP (20%) and dropping — back off" at 4:40' in text
    assert "three short coaching points" in text and len(text) < 2000


def test_core_items_are_timed_against_the_heros_players(tmp_path):
    """Item pace: each core item next to when half the hero's players have it; a late one (3+ min)
    becomes a focus point, an on-time one doesn't, and the AI prompt carries both."""
    kb = _kb()
    kb.item_timings = {(71, "item_blink"): [{"hero_id": 71, "time": 900, "games": 40},
                                            {"hero_id": 71, "time": 1200, "games": 60},
                                            {"hero_id": 71, "time": 1500, "games": 20}],
                       (71, "item_phase_boots"): [{"hero_id": 71, "time": 600, "games": 80}]}
    store, rec = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)
    for clock in range(0, 1700, 20):
        items = (["item_phase_boots"] if clock >= 540 else []) + (["item_blink"] if clock >= 1620 else [])
        feed(rec, store, payload(clock, items=items, lh=clock // 10))
    review = rec.finish()
    assert review.item_pace == [("Phase Boots", 540, 600), ("Blink Dagger", 1620, 1200)]
    assert any(p.startswith("Your Blink Dagger came at 27:00; Spirit Breaker players usually have it by 20:00")
               for p in review.focus)
    assert not any("Phase Boots" in p for p in review.focus)
    text = review_prompt(review)
    assert "Blink Dagger 27:00 (half of Spirit Breaker's players have it by 20:00)" in text
    assert load_review(rec.last_path).item_pace == review.item_pace  # survives the JSON round trip


def test_recap_card_lists_core_item_pace():
    from doty.ui.coach_page import CoachPage

    row = CoachPage._pace_row("Blink Dagger", 1620, 1200)
    assert "Blink Dagger" in row and "27:00 · usually by 20:00" in row and "7 min late" in row
    assert "on time" in CoachPage._pace_row("Phase Boots", 590, 600)
    assert "2 min early" in CoachPage._pace_row("Phase Boots", 480, 600)


def test_a_restart_mid_match_picks_the_review_back_up(tmp_path):
    """Saving settings restarts Doty's engine mid-game: the deaths and items from before must survive."""
    kb = _kb()
    store, rec = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)
    for clock in range(0, 600, 10):
        feed(rec, store, payload(clock, items=["item_blink"] if clock >= 400 else []))
    feed(rec, store, payload(610, alive=False, deaths=1, items=["item_blink"]))
    first = rec.finish()  # the engine stops (settings saved)
    assert first.won is None and len(first.deaths) == 1
    store2, rec2 = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)  # the new engine
    for clock in range(700, 900, 10):
        feed(rec2, store2, payload(clock, deaths=1, items=["item_blink", "item_phase_boots"] if clock >= 800
                                   else ["item_blink"]))
    for clock in (900, 910):
        feed(rec2, store2, payload(clock, state=POST_GAME, win="radiant", deaths=1, items=["item_blink"]))
    final = load_review(tmp_path / "match-m1.json")
    assert final.won is True and final.recorded_from == 0 and len(final.deaths) == 1
    assert final.items == [("Blink Dagger", 400), ("Phase Boots", 800)]


def test_a_late_core_item_comes_with_what_it_cost_in_win_rate(tmp_path):
    """OpenDota's item timings carry wins too: a late item's focus point says how often the hero's players
    win when it lands that late, against when it usually does (only when the gap is real)."""
    kb = _kb()
    kb.item_timings = {(71, "item_blink"): [{"hero_id": 71, "time": 900, "games": "400", "wins": "236"},
                                            {"hero_id": 71, "time": 1200, "games": "600", "wins": "330"},
                                            {"hero_id": 71, "time": 1800, "games": "300", "wins": "120"}]}
    store, rec = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)
    for clock in range(0, 1700, 20):
        feed(rec, store, payload(clock, items=["item_blink"] if clock >= 1620 else [], lh=clock // 10))
    review = rec.finish()
    assert review.item_odds == [("Blink Dagger", 0.4, 0.55)]
    point = next(p for p in review.focus if p.startswith("Your Blink Dagger came at 27:00"))
    assert point.endswith("Players who finish it as late as you did win 40% of their games; by 20:00, 55%.")
    assert load_review(rec.last_path).item_odds == review.item_odds


def test_the_coach_sees_how_the_next_items_value_falls_off():
    kb = _kb()
    kb.item_popularity[71] = {"mid_game_items": {"1": 60}}
    kb.items["item_blink"]["id"] = 1
    kb.item_timings = {(71, "item_blink"): [{"hero_id": 71, "time": 720, "games": "158", "wins": "102"},
                                            {"hero_id": 71, "time": 900, "games": "784", "wins": "456"},
                                            {"hero_id": 71, "time": 1200, "games": "473", "wins": "156"},
                                            {"hero_id": 71, "time": 1500, "games": "12", "wins": "2"}]}
    assert kb.win_rate_by(71, "item_blink", 1100) == 156 / 473
    assert kb.win_rate_by(71, "item_blink", 1400) is None  # (12 games: too few to say)
    s = kb.summary_for_prompt(SB, 800, owned=set())
    assert s["next_item_odds"] == {"Blink Dagger": "by 15:00 58% · by 20:00 33%"}
    assert "next_item_odds" not in kb.summary_for_prompt(SB, 800, owned={"item_blink"})
