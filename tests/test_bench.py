"""The bench (doty/bench): replaying a match offline, each checker on a line that must fail and one that must pass,
the scorecard's numbers, comparing two runs, scoring a past session log, and "Wrong tip?" reports as fixtures.

Everything here is synthetic and offline: a small hand-made item list (no cache), the scripted match, and logs,
reviews and reports written by hand in the real formats."""

import json
import time
from pathlib import Path

from doty.bench import CHECKERS, Context, Line, Match, Replay, ScriptedBackend, scorecard, scripted_payloads
from doty.bench import checks, fixtures, logs, score
from doty.bench.__main__ import main as bench_main
from doty.bench.replay import read_recording
from doty.knowledge.constants import Knowledge

JUGG, LION, LUNA = "npc_dota_hero_juggernaut", "npc_dota_hero_lion", "npc_dota_hero_luna"
BKB, PHASE, BEARING = "item_black_king_bar", "item_phase_boots", "item_boots_of_bearing"
DATA = Path(__file__).parent / "data" / "bench"


def _kb():
    """Real item names, costs and parts for the items these tests talk about (no cache, no network)."""
    kb = Knowledge(cache_dir=Path("does-not-exist"))
    kb.heroes = {JUGG: {"id": 8, "localized_name": "Juggernaut", "roles": ["Carry"]},
                 LION: {"id": 26, "localized_name": "Lion", "roles": ["Support"]},
                 LUNA: {"id": 48, "localized_name": "Luna", "roles": ["Carry"]}}

    def item(i, name, cost, parts=None):
        return {"id": i, "dname": name, "cost": cost, "components": parts}

    kb.items = {f"item_{k}": v for k, v in {
        "boots": item(29, "Boots of Speed", 500), "gloves": item(25, "Gloves of Haste", 450),
        "belt_of_strength": item(14, "Belt of Strength", 450),
        "power_treads": item(63, "Power Treads", 1400, ["boots", "gloves", "belt_of_strength"]),
        "chainmail": item(4, "Chainmail", 550), "blades_of_attack": item(2, "Blades of Attack", 450),
        "phase_boots": item(50, "Phase Boots", 1500, ["boots", "chainmail", "blades_of_attack"]),
        "ogre_axe": item(21, "Ogre Axe", 1000), "mithril_hammer": item(10, "Mithril Hammer", 1600),
        "recipe_black_king_bar": item(115, "Black King Bar Recipe", 1450),
        "black_king_bar": item(116, "Black King Bar", 4050, ["mithril_hammer", "ogre_axe"]),
        "wind_lace": item(244, "Wind Lace", 250), "ring_of_regen": item(27, "Ring of Regen", 175),
        "tranquil_boots": item(214, "Tranquil Boots", 900, ["boots", "wind_lace", "ring_of_regen"]),
        "ancient_janggo": item(185, "Drum of Endurance", 1625),
        "ring_of_tarrasque": item(279, "Ring of Tarrasque", 1800),
        "boots_of_bearing": item(931, "Boots of Bearing", 4225, ["tranquil_boots", "ancient_janggo", "ring_of_tarrasque"]),
        "point_booster": item(60, "Point Booster", 1200), "staff_of_wizardry": item(13, "Staff of Wizardry", 1000),
        "blade_of_alacrity": item(22, "Blade of Alacrity", 1000),
        "ultimate_scepter": item(108, "Aghanim's Scepter", 4200,
                                 ["point_booster", "staff_of_wizardry", "ogre_axe", "blade_of_alacrity"]),
        "tpscroll": item(46, "Town Portal Scroll", 100), "magic_wand": item(36, "Magic Wand", 450),
        "blink": item(1, "Blink Dagger", 2250), "quelling_blade": item(11, "Quelling Blade", 100),
        "bfury": item(145, "Battle Fury", 3900),
    }.items()}
    return kb


KB = _kb()


def _line(text, clock=600, source="coach", priority=2, category="coach", spoken=True, **ctx):
    ctx.setdefault("hero", JUGG)
    return Line(clock=clock, text=text, source=source, priority=priority, category=category, spoken=spoken,
                ctx=Context(clock=clock, **ctx))


def _check(name, text, kb=KB, **kw):
    """One checker's verdict on one line: None (n/a), "" (pass) or why it failed."""
    match = Match("t", [_line(text, **kw)], kb=kb)
    return CHECKERS[name].run(match.lines[0], match)


def _run(name, lines, kb=KB):
    """One checker over a sequence of lines (for the checks that look at what came before)."""
    match = Match("t", lines, kb=kb)
    match.renumber()
    return [CHECKERS[name].run(line, match) for line in match.lines]


# the replay ------------------------------------------------------------------------------------------------------------
def test_the_scripted_match_replays_through_the_real_rules_deterministically():
    real_monotonic = time.monotonic
    first = Replay(KB, label="sim").run(scripted_payloads(end_clock=600))
    assert time.monotonic is real_monotonic  # (the simulated clock is put back)
    again = Replay(_kb(), label="sim").run(scripted_payloads(end_clock=600))
    assert len(first) == 1 and first[0].label == "sim#1" and first[0].kind == "replay"
    lines = first[0].lines
    assert [(x.clock, x.rule, x.text) for x in lines] == [(x.clock, x.rule, x.text) for x in again[0].lines]
    assert all(x.rule for x in lines if x.source == "rules")  # (which rule said each line)
    low = next(x for x in lines if x.rule == "low_hp")  # the scripted fight at 4:30
    assert low.spoken and low.ctx.fighting and low.ctx.alive and low.priority == 1
    dead = next(x for x in lines if x.ctx.alive is False)  # died at 4:46
    assert 286 <= dead.clock < 310 and dead.ctx.has_tp is False
    assert first[0].start_clock < 0 and first[0].end_clock >= 600


def test_a_replay_scores_and_the_scorecard_is_stable():
    card = scorecard(Replay(KB).run(scripted_payloads(end_clock=600)))
    again = scorecard(Replay(_kb()).run(scripted_payloads(end_clock=600)))
    assert json.dumps(card, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert card["overall"]["said"] > 0 and card["overall"]["score"] is not None
    assert set(card["checkers"]) == set(CHECKERS)


def test_the_coach_replays_with_recorded_answers_and_its_vetting_runs():
    answers = [
        {"kind": "start", "clock": -30, "reply": "Stay in lane and last-hit with Quelling Blade."},
        {"kind": "death", "clock": 280, "reply": "You fought alone and died. On respawn, buy a TP scroll."},
        {"kind": "watch", "clock": 700, "reply": "SAY: Grab the water rune at 2:00 with your team."},
    ]
    backend = ScriptedBackend(answers)
    match = Replay(KB, backend=backend).run(scripted_payloads(end_clock=800))[0]
    kinds = [k for k, _clock in backend.requests]
    assert kinds[0] == "start" and "death" in kinds and "watch" in kinds
    coach = [x for x in match.lines if x.source == "coach"]
    texts = [x.text for x in coach]
    assert "Stay in lane and last-hit with Quelling Blade." in texts
    death = next(x for x in coach if x.text.startswith("You fought alone"))
    assert death.spoken and death.ctx.alive is False and death.rule == "coach:coach"
    card = score.score_match(match)
    late = [f for f in card["fails"] if f["checker"] == "stale_time"]
    assert late and late[0]["text"].startswith("Grab the water rune at 2:00")


def test_a_function_can_answer_for_the_coach():
    asked = []

    def respond(kind, clock, system, content):
        asked.append((kind, clock))
        return "SAY: Back off to your tower now." if kind == "watch" else ""

    match = Replay(KB, backend=ScriptedBackend(respond=respond)).run(scripted_payloads(end_clock=200))[0]
    assert asked and any(x.text == "Back off to your tower now." for x in match.lines)


# the checkers: one line that must fail, one that must pass ---------------------------------------------------------------
def test_contradicts_inventory():
    assert "Phase Boots" in _check("contradicts_inventory", "Buy Phase Boots now.", owned=frozenset({BEARING}))
    assert _check("contradicts_inventory", "Buy Power Treads.", owned=frozenset({"item_power_treads"}))
    assert _check("contradicts_inventory", "Buy Ogre Axe.", owned=frozenset({BKB}))  # (a part of what you built)
    assert _check("contradicts_inventory", "Buy Black King Bar.", owned=frozenset({"item_power_treads"})) == ""
    # A part that went into one item can be bought again for another the line names.
    assert _check("contradicts_inventory", "Buy Ogre Axe for Black King Bar.",
                  owned=frozenset({"item_ultimate_scepter"})) == ""
    assert _check("contradicts_inventory", "Buy Phase Boots now.") is None  # (inventory unknown)
    assert _check("contradicts_inventory", "Walk back to your tower.", owned=frozenset()) is None


def test_not_affordable():
    assert "985" in _check("not_affordable", "Buy Black King Bar now.", gold=985, items=())
    assert _check("not_affordable", "Buy Black King Bar now.", gold=4100, items=()) == ""
    assert _check("not_affordable", "Buy Black King Bar now.", gold=1100, items=()) == ""  # (Ogre Axe is in reach)
    assert _check("not_affordable", "Save for Black King Bar.", gold=10, items=()) is None  # (a plan, not "now")
    # A session log has no gold, but the line may say it: "with your 985 gold".
    assert _check("not_affordable", "Buy Black King Bar now with your 985 gold.")
    assert _check("not_affordable", "Buy Black King Bar now.") is None


def test_tp_nag():
    assert _check("tp_nag", "Buy a TP scroll, then walk with your team.", has_tp=True)
    assert _check("tp_nag", "No TP scroll — buy one", source="rules", priority=3, has_tp=True)
    assert _check("tp_nag", "Buy a TP scroll, then walk with your team.", has_tp=False) == ""
    assert _check("tp_nag", "Buy a TP scroll.") is None
    assert _check("tp_nag", "TP to your tower and defend.", has_tp=True) is None


def test_passed_on():
    assert "Black King Bar" in _check("passed_on", "Buy Black King Bar when you can.", skipped=frozenset({BKB}))
    assert _check("passed_on", "Buy Black King Bar when you can.", skipped=frozenset()) == ""
    assert _check("passed_on", "Use Black King Bar before you jump.", skipped=frozenset({BKB})) is None


def test_item_overuse_counts_until_a_part_is_bought():
    said = [_line("Save for Black King Bar.", clock=c, items=()) for c in (100, 200, 300, 400)]
    assert _run("item_overuse", said)[-1] and not any(_run("item_overuse", said)[:3])
    # Buying a part starts the count again: the player is building it.
    said[3] = _line("Save for Black King Bar.", clock=400, items=("item_ogre_axe",))
    assert not any(_run("item_overuse", said))
    # "Farm until Black King Bar" is the same reminder; "back off" never counts.
    until = [_line("Farm until Black King Bar.", clock=c, items=()) for c in (100, 200, 300, 400)]
    assert _run("item_overuse", until)[-1]
    retreat = [_line("Back off, no Black King Bar yet.", clock=c, items=()) for c in (100, 200, 300, 400)]
    assert not any(_run("item_overuse", retreat))


def test_format_leak():
    assert "TRIGGER:" in _check("format_leak", "Don't walk the river alone. TRIGGER: hp < 30 => Back off.")
    assert _check("format_leak", "Respawn in 3 seconds, buy Tranquil... no: once back, buy a TP scroll.")
    assert _check("format_leak", "Learn Charge now.\n\nWait, correct that: learn Bash.")
    assert _check("format_leak", "Wait for Luna before you fight.") == ""
    assert _check("format_leak", "Farm the jungle. Wait until Luna is with you.") == ""


def test_unknown_names():
    assert "Dragon Saber" in _check("unknown_names", "Buy Dragon Saber now.")
    assert "Lion" in _check("unknown_names", "Lion is missing, back off.", heroes=frozenset({JUGG, LUNA}))
    assert _check("unknown_names", "Lion is missing, back off.", heroes=frozenset({JUGG, LION})) == ""
    assert _check("unknown_names", "Grab the Bounty rune, then buy Black King Bar's Ogre Axe.") == ""
    assert _check("unknown_names", "Buy a Town Portal Scroll and Magic Wand.") == ""


def test_stale_time():
    assert "47 s late" in _check("stale_time", "Grab the water rune at 2:00 with your team.", clock=167)
    assert _check("stale_time", "Grab the water rune at 2:00 with your team.", clock=110) == ""
    assert _check("stale_time", "Roshan died at 15:00; respawns from 23:00.", clock=1000) == ""
    assert _check("stale_time", "You were killed at 12:30 by Lion.", clock=800) == ""
    assert _check("stale_time", "Farm the jungle.") is None


def test_timing():
    rule = {"source": "rules", "priority": 3, "category": "timer"}
    assert _check("timing", "Take the bounty rune now.", alive=False, **rule)
    assert _check("timing", "Bounty runes in 15s (4:00)", alive=False, **rule) == ""
    assert _check("timing", "Once you respawn, take the bounty rune.", alive=False, **rule) == ""
    assert _check("timing", "You died alone. Farm with your team.", alive=False) == ""  # (the death answer)
    assert _check("timing", "Buy a TP scroll.", alive=True, fighting=True, source="rules", priority=3)
    assert _check("timing", "Farm the jungle after this.", alive=True, fighting=True)
    assert _check("timing", "Back off to your tower now.", alive=True, fighting=True) == ""
    assert _check("timing", "Use Black King Bar and fight.", alive=True, fighting=True) == ""
    assert _check("timing", "Farm the jungle.", alive=True, fighting=False) == ""
    assert _check("timing", "Buy a TP scroll.", alive=True, fighting=True, spoken=False) is None  # (shown only)


def test_repeats():
    lines = [_line("Walk back to your tower now.", clock=c) for c in (100, 104, 109)]
    verdicts = _run("repeats", lines)  # three near-identical calls in nine seconds (a real match)
    assert verdicts[0] == "" and "4 s before" in verdicts[1] and verdicts[2]
    far = [_line("Walk back to your tower now.", clock=c) for c in (100, 300)]
    assert _run("repeats", far) == ["", ""]
    steps = [_line("Buy Ogre Axe (1,000): a part of Black King Bar", clock=100, source="rules", priority=3),
             _line("Buy Mithril Hammer (1,600): a part of Black King Bar", clock=130, source="rules", priority=3)]
    assert _run("repeats", steps) == ["", ""]  # (the next part, not the same line)


def test_rate_and_the_coach_budget():
    busy = [_line(f"Tip number {n}: farm the {w} camp.", clock=100 + 8 * n, source="rules", priority=3,
                  category="timer") for n, w in enumerate("big small ancient hard medium easy".split())]
    verdicts = _run("rate", busy)
    assert verdicts[:4] == [""] * 4 and verdicts[4] and verdicts[5]
    coach = [_line("Farm the bottom jungle.", clock=100), _line("Take the tower with Luna.", clock=110),
             _line("Group for Roshan.", clock=150)]
    assert _run("coach_budget", coach) == ["", "10 s after the coach's last line", ""]


def test_adding_a_checker_is_a_few_lines():
    @checks.checker("bench_test_shouting", 1, "a line written in capitals")
    def shouting(line, match):
        return "written in capitals" if line.text.isupper() else ""
    try:
        assert _check("bench_test_shouting", "BACK OFF NOW") and _check("bench_test_shouting", "Back off") == ""
    finally:
        del checks.CHECKERS["bench_test_shouting"]


# the scorecard ---------------------------------------------------------------------------------------------------------
def test_the_scorecard_maths():
    pick = [CHECKERS["tp_nag"], CHECKERS["repeats"]]
    lines = [_line("Buy a TP scroll now.", clock=100, has_tp=True),  # severity 3 fail
             _line("Walk back to your tower.", clock=200, has_tp=True),  # passes
             _line("Walk back to your tower.", clock=205, has_tp=True)]  # severity 1 fail (repeat)
    card = scorecard([Match("m", lines, kb=KB, start_clock=0, end_clock=600)], pick)
    o, m = card["overall"], card["matches"][0]
    assert (m["lines"], m["said"], m["scored"], m["failing"]) == (3, 3, 3, 2)
    assert m["score"] == round(100 * (1 - (1 + 1 / 3) / 3), 1) == 55.6
    assert m["fail_rate"] == 66.7 and m["per_minute"] == 0.3 and m["repeat_rate"] == 33.3
    assert m["checks"]["tp_nag"] == {"severity": 3, "checked": 1, "failed": 1, "pass_rate": 0.0}
    assert m["checks"]["repeats"]["checked"] == 3 and m["checks"]["repeats"]["failed"] == 1
    assert m["worst"][0]["checker"] == "tp_nag" and m["worst"][0]["at"] == "1:40"
    assert o["score"] == m["score"] and o["said"] == 3
    text = score.render(card)
    assert "SCORE 55.6" in text and "tp_nag" in text and '"Buy a TP scroll now."' in text


def test_compare_shows_what_got_better_and_worse():
    pick = [CHECKERS["tp_nag"]]
    before = scorecard([Match("m", [_line("Buy a TP scroll now.", has_tp=True),
                                    _line("Carry a TP scroll.", clock=700, has_tp=False)], kb=KB, end_clock=900)], pick)
    after = scorecard([Match("m", [_line("Farm the jungle now.", has_tp=True),
                                   _line("Carry a TP scroll.", clock=700, has_tp=True)], kb=KB, end_clock=900)], pick)
    text = score.compare(before, after)
    assert "no longer failing: 1" in text and "newly failing: 1" in text
    assert '"Buy a TP scroll now."' in text and '"Carry a TP scroll."' in text
    assert "score" in text and "same" in score.compare(before, before)


# past games: session logs and reviews --------------------------------------------------------------------------------------
T0 = 2_000_000_000.0


def _write_session(tmp_path):
    """A session log with two games, in the format app.Doty._session_log writes, and the first game's review."""
    rows = [
        (-30, "rules", "timer", 3, "Bounty runes in 15s (0:00)"),
        (95, "coach", "coach", 2, "Stay in lane and grab the water rune at 2:00."),
        (167, "coach", "coach", 2, "Grab the water rune at 2:00 with your team."),
        (295, "coach", "coach", 2, "You charged in alone. On respawn, buy a TP scroll and farm."),
        (700, "coach", "coach", 2, "Buy Phase Boots now, you have the gold."),
        (760, "coach", "coach", 2, "Buy Black King Bar now with your 985 gold."),
        (800, "coach", "coach", 2, "Farm your lane. TRIGGER: hp < 30 => Back off."),
        (850, "coach", "watching", 4, "Quiet note about the jungle."),
        # the second game: the clock starts over
        (-60, "rules", "plan", 4, "Skills: Blade Fury"),
        (60, "rules", "timer", 3, "Water runes in 15s (2:00)"),
        (200, "coach", "coach", 2, "Farm the bottom lane."),
    ]
    logs_dir, reviews_dir = tmp_path / "logs", tmp_path / "reviews"
    logs_dir.mkdir()
    reviews_dir.mkdir()
    log = logs_dir / "session-test.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for n, (clock, source, category, priority, text) in enumerate(rows):
            t = T0 + n * 30 + (2000 if n >= 8 else 0)
            fh.write(json.dumps({"t": t, "clock": clock, "source": source, "category": category,
                                 "priority": priority, "text": text, "latency_ms": 1.0}) + "\n")
        fh.write('{"t": 1, "clock": 5, "text": "cut off when the app clo')  # (a half-written last line)
    review = {"match_id": "1", "hero": JUGG, "hero_id": 8, "team": "radiant", "duration": 860,
              "recorded_from": -40, "bought": [["item_power_treads", 400], [BEARING, 650]],
              "deaths": [{"clock": 290, "had_tp": True}], "saved_at": T0 + 7 * 30 + 60, "demo": False}
    (reviews_dir / "match-1.json").write_text(json.dumps(review), encoding="utf-8")
    (reviews_dir / "match-doty-sim-1.json").write_text(json.dumps(dict(review, demo=True)), encoding="utf-8")
    return log


def test_a_session_log_is_split_into_matches_and_scored_with_its_review(tmp_path):
    log = _write_session(tmp_path)
    assert logs.is_session_log(log)
    found = logs.log_matches(log, KB)
    assert [m.label for m in found] == ["session-test#1", "session-test#2"]
    first, second = found
    assert first.kind == "log" and first.start_clock == -40 and first.end_clock == 860
    assert "with its review" in " ".join(first.notes) and "no review" in " ".join(second.notes)
    quiet = next(x for x in first.lines if x.category == "watching")
    assert quiet.spoken is None and not quiet.said  # (a quiet note isn't counted as said)
    card = score.score_match(first)
    failed = {(f["checker"], f["at"]) for f in card["fails"]}
    assert ("stale_time", "2:47") in failed  # "the water rune at 2:00", said at 2:47
    assert ("tp_nag", "4:55") in failed  # a TP scroll was held at the death at 4:50
    assert ("contradicts_inventory", "11:40") in failed  # Phase Boots to a player in Boots of Bearing
    assert ("not_affordable", "12:40") in failed  # "now with your 985 gold"
    assert ("format_leak", "13:20") in failed
    assert card["checks"]["timing"]["checked"] == 0  # (a log has no HP or alive state)


def test_the_cli_scores_logs_writes_json_and_compares(tmp_path, capsys):
    log = _write_session(tmp_path)
    out = tmp_path / "card.json"
    assert bench_main([str(tmp_path / "logs" / "session-*.jsonl"), "--json", str(out), "--quiet"]) == 0
    card = json.loads(out.read_text(encoding="utf-8"))
    assert [m["label"] for m in card["matches"]] == ["session-test#1", "session-test#2"]
    assert bench_main(["--compare", str(out), str(out)]) == 0
    assert "newly failing: 0" in capsys.readouterr().out
    assert log.exists()


def test_what_was_said_is_judged_against_a_recording_of_the_same_game(tmp_path):
    """Capture mode on: the session log's lines get the recorded game state at the moment they were said."""
    rec = tmp_path / "gsi.jsonl"
    with rec.open("w", encoding="utf-8") as fh:
        for t, payload in scripted_payloads(end_clock=900):
            fh.write(json.dumps({"t": T0 + t, "payload": payload}) + "\n")
    assert sum(1 for _ in read_recording(tmp_path)) > 900
    log = tmp_path / "session-said.jsonl"
    rows = [(400, "Buy a TP scroll now."),  # the scripted player has none from 4:46 to 11:40: right
            (800, "Buy a TP scroll now.")]  # and one again from 11:40: wrong
    with log.open("w", encoding="utf-8") as fh:
        for clock, text in rows:
            fh.write(json.dumps({"t": T0 + 1000.0 + clock + 1, "clock": clock, "source": "coach", "category": "coach",
                                 "priority": 2, "text": text}) + "\n")
    out = tmp_path / "said.json"
    assert bench_main([str(tmp_path), "--said", str(log), "--json", str(out), "--quiet", "--no-kb"]) == 0
    card = json.loads(out.read_text(encoding="utf-8"))
    (m,) = card["matches"]
    assert m["kind"] == "log+recording" and m["checks"]["tp_nag"] == {
        "severity": 3, "checked": 2, "failed": 1, "pass_rate": 50.0}
    assert m["fails"][0]["at"] == "13:20" and "2 of 2 lines" in m["notes"][0]


# "Wrong tip?" reports become fixtures ---------------------------------------------------------------------------------------
def _write_report(tmp_path):
    folder = tmp_path / "20261110-190000-15m00s-42"
    folder.mkdir()
    report = {
        "kind": "watch", "question": "a question in the player's own words", "clock": 900, "hero": JUGG,
        "model": "some-model", "provider": "Claude", "time": "2026-11-10 19:00:00",
        "reply": "SAY: Buy a TP scroll now, then farm.\nTRIGGER: hp < 30 => Back off.",
        "shown": "Buy a TP scroll now, then farm.",
        "state": {"clock": "15:00", "gold": 300, "alive": True, "items": ["Power Treads", "Magic Wand (12 charges)"],
                  "backpack": [], "stash": [], "tp": "ready", "passed_on": ["Black King Bar"],
                  "allies": ["Juggernaut"], "enemies": ["Lion"], "lineup_complete": False, "kda": "1/2/3"},
    }
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (folder / "screen.jpg").write_bytes(b"\xff\xd8not really a jpeg")
    return folder


def test_a_wrong_tip_report_becomes_an_anonymised_fixture(tmp_path):
    fx = fixtures.convert_report(_write_report(tmp_path), KB, note="had a TP")
    assert fx["line"] == "Buy a TP scroll now, then farm." and fx["clock"] == 900 and fx["kind"] == "watch"
    assert fx["state"]["items"] == ["item_power_treads", "item_magic_wand"] and fx["state"]["tp"] is True
    assert fx["state"]["passed_on"] == [BKB] and "heroes" not in fx["state"]  # (the lineup wasn't confirmed)
    assert fx["expect"] == "fail" and fx["checks"] == ["tp_nag"]
    text = json.dumps(fx)
    for private in ("question", "player's own words", "some-model", "2026-11-10", "TRIGGER", "kda", "20261110"):
        assert private not in text
    result = fixtures.run_fixture(fx, KB)
    assert result["status"] == "ok" and result["coach"] == "drops"  # (the coach's own vetting catches it today)


def test_fixture_statuses():
    fx = {"line": "Farm the jungle.", "clock": 600, "hero": JUGG, "state": {"tp": True}, "expect": "fail",
          "checks": []}
    assert fixtures.run_fixture(fx, KB)["status"] == "uncaught"  # a known wrong line no checker catches yet
    fx["line"] = "Buy a TP scroll."
    assert fixtures.run_fixture(fx, KB)["status"] == "caught"
    fx["checks"] = ["tp_nag", "passed_on"]
    assert fixtures.run_fixture(fx, KB)["status"] == "regressed"
    assert fixtures.run_fixture(dict(fx, expect="pass", state={"tp": False}), KB)["status"] == "ok"


def test_the_committed_fixtures_still_hold(capsys):
    found = sorted(DATA.glob("*.json"))
    assert found
    for path in found:
        fx = fixtures.load_fixture(path)
        assert fx["fixture"] == fixtures.FORMAT
        assert fixtures.run_fixture(fx, KB)["status"] == "ok", path.name
    assert bench_main(["--no-kb", "--fixtures", str(DATA / "*.json")]) in (0, 1)  # (no item data: runs, judges less)
    assert "tp_scroll_held" in capsys.readouterr().out
