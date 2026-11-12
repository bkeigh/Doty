"""The pieces together: the speech gate, own-way, the item judgement, the threat call and the practice focus, in
one replayed match, with a coach that answers every look and sometimes says the wrong thing."""

import pytest

from doty import speech
from doty.advice import Advice, Priority
from doty.bench import Replay, ScriptedBackend, score, scorecard, scripted_payloads
from doty.config import CoachConfig, Config
from doty.knowledge import build
from doty.lineup import Lineup
from doty.match_record import tag_rules
from doty.rules.engine import SELF_SILENCED, TOPIC_RULES, default_rules
from doty.rules.focus import Focus

from .test_coach_etiquette import _bench_kb, _right_click
from .test_game_sense import JUGG, LION

ALLIES = [JUGG, LION, "npc_dota_hero_lina", "npc_dota_hero_bane", "npc_dota_hero_tidehunter"]
CAMPS = ["big", "small", "ancient", "hard", "medium", "easy", "triangle", "river"]
BAD = {
    600: "SAY: Buy Black King Bar now, you have the gold.",  # a counter item that answers nothing of theirs
    720: "SAY: Sniper's Fiend's Grip will lock you down: stay behind your creeps.",  # Bane's spell, not Sniper's
}


def answer(kind, clock, system, content):
    """A coach with something to say on every look: mostly fine, a repeat now and then, two bad lines."""
    if kind == "death":
        return "You died alone. Farm with your team next time."
    if kind not in ("watch", "now"):
        return ""
    for at, line in BAD.items():
        if at <= clock < at + 30:
            return line
    if 900 <= clock < 1000:
        return "SAY: Farm the big camp near your tower."  # (the same line, look after look)
    n = clock // 20
    return f"SAY: Farm the {CAMPS[n % len(CAMPS)]} camp, then look at tower {n % 3 + 1}."


def _replay(talk, tmp_path, monkeypatch):
    enemies = _right_click()

    def confirmed(snap, store, kb):  # (the scripted match has no top bar to read: all ten as if confirmed twice)
        if "lineup_votes" not in store.memory:
            votes = store.memory["lineup_votes"] = Lineup()
            for _ in range(2):
                votes.add(list(ALLIES), list(enemies), own=JUGG, team="radiant")
        kb.set_enemies(enemies, complete=True)

    monkeypatch.setattr(build, "lineup", confirmed)
    cfg = Config()
    cfg.coach.talk, cfg.coach.own_way, cfg.coach.speculative_interval_s = talk, "once", 20
    replay = Replay(_bench_kb(), cfg=cfg, backend=ScriptedBackend(respond=answer), heroes=ALLIES + enemies)
    replay.engine.rules = [Focus(history_path=tmp_path / "focus.json") if r.id == "focus" else r
                           for r in replay.engine.rules]  # (a history of its own, not the player's)
    tag_rules(replay.engine)
    return replay.run(scripted_payloads(end_clock=1500))[0]


@pytest.mark.parametrize("talk", ["less", "normal", "more"])
def test_everything_together(talk, tmp_path, monkeypatch):
    match = _replay(talk, tmp_path, monkeypatch)
    card = scorecard([match])["overall"]
    heard = [x for x in match.lines if x.said]
    focus = [x for x in heard if x.rule == "focus" and x.category == "focus"]
    threat = [x for x in heard if x.rule == "threat_call"]
    assert len(focus) >= 1 and sum(1 for x in focus if x.clock < 120) == 1  # the horn line, once
    assert len(threat) == 1  # the threat call, once
    dangers = [x for x in match.lines if speech.danger(x.priority, x.category, x.text)]
    assert all(x.said for x in dangers)
    serious = [f for f in score.score_match(match)["fails"] if f["severity"] >= 2]
    assert serious == [], serious[:3]
    assert not any("Black King Bar" in x.text or "Fiend's Grip" in x.text for x in heard if x.source == "coach")
    minutes = (match.end_clock - match.start_clock) / 60
    assert card["said"] / minutes <= speech.TALK[talk].per_min
    assert card["checks"]["rate"]["failed"] == card["checks"]["coach_budget"]["failed"] == 0


def test_every_rule_belongs_to_a_subject_or_silences_itself():
    ids = {r.id for r in default_rules()}
    assert "focus" in ids and "threat_call" in TOPIC_RULES["enemy"]
    switched = set().union(*TOPIC_RULES.values())
    assert SELF_SILENCED <= ids and not (SELF_SILENCED & switched)


def test_death_causes_and_the_focus_follow_their_switches():
    from doty.advice import AdviceBus
    from doty.coach import Coach
    from doty.config import SuggestConfig
    from doty.state import GameStateStore

    from .test_game_sense import _feed
    from .test_own_build import _kb

    kb, store = _kb(), GameStateStore()
    snap = _feed(store, 1.0, clock=600, items=["item_power_treads"])
    store.memory["focus_plan"] = {"key": "lh10", "target": 50}
    coach = Coach(CoachConfig(), store, AdviceBus(), kb)
    summary = {"death_cause_candidates": ["stunned: could be Lion's Earth Spike"], "practice_focus": "Last hits"}
    coach.suggest = SuggestConfig(deaths=False, farm=False)
    coach._settings_into(summary, snap)
    assert "death_cause_candidates" not in summary and "practice_focus" not in summary
    summary = {"death_cause_candidates": ["x"], "practice_focus": "Last hits"}
    coach.suggest = SuggestConfig(gold=False)  # (another subject: both stay)
    coach._settings_into(summary, snap)
    assert summary["death_cause_candidates"] == ["x"] and summary["practice_focus"] == "Last hits"


def test_once_a_match_lines_wait_for_a_calm_slot():
    """The threat call and the focus line stay true for minutes: they may wait far longer than a timer."""
    from .test_speech import Game, _line

    game = Game()
    gate = speech.SpeechGate(CoachConfig(talk="less"), state=game.state)
    gate.admit(_line("Bounty runes in 15s (0:00)", category="timer"), 100.0)  # the horn: a timer first
    plan = _line("They scale better. Win early.", category="plan")
    focus = _line("This game: 45 last hits by 10:00.", category="focus")
    rune = _line("Water runes in 15s (2:00)", category="timer", stale_s=8.0)
    for adv, t in ((focus, 101.0), (plan, 102.0)):
        gate.admit(adv, t)
        assert not adv.speak
    out = []
    for t in range(103, 260):
        if t == 200:
            gate.admit(rune, float(t))
        out += [(t, a.text) for a in gate.release(float(t))]
    assert [text for _t, text in out] == [plan.text, focus.text]  # the plan first, then the focus: one at a time
    assert out[1][0] - out[0][0] >= speech.TALK["less"].gap_s


def test_why_after_the_threat_call_and_the_focus_line():
    from doty.knowledge import focus as lib
    from doty.rules.matchup import _threat_why

    text = lib.why_text({"key": "lh10", "why": "gap", "recent": [31, 35, 38], "source": "card_up"})
    assert text.startswith("Last hits at 10:00 is this game's focus because it's where your last games fell")
    assert "31, 35, 38" in text and "a rank above you" in text
    assert lib.why_text({"key": "nope"}) == ""

    class Card:
        hero = "Lion"

        @staticmethod
        def as_dict():
            return {"threats": [{"spell": "Hex", "what": "hexes you", "answer": "Linken's Sphere"}]}

    assert _threat_why(Card()) == "Lion: Hex hexes you (answer: Linken's Sphere)."
    assert Advice  # (imported for the module's other tests)
    assert Priority
