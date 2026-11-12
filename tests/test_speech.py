"""What Doty says out loud (speech.py): one gate for the rules and the coach. From the bench's scores of real past
games: 3.7 to 7.0 spoken lines a minute, and most failures were about talking too much, not about facts."""

import time
from types import SimpleNamespace

import pytest

from doty import speech
from doty.advice import Advice, AdviceBus, Priority
from doty.config import CoachConfig, Config, HotkeyConfig, dump_config, load_config

from .conftest import Recorder
from .test_app_ui import qapp  # noqa: F401  (the fixture)


class Game:
    """A stand-in for the match: alive or dead, fighting or not."""

    def __init__(self):
        self.snap = SimpleNamespace(has_hero=True, alive=True, clock=600)
        self.fighting = False

    def state(self):
        return self.snap, self.fighting


def _gate(talk="normal", level="some", **kw):
    game = Game()
    gate = speech.SpeechGate(CoachConfig(talk=talk, level=level), Priority.NORMAL, state=game.state, **kw)
    return gate, game


def _line(text="Farm the bottom jungle.", priority=Priority.NORMAL, category="economy", source="rules", **kw):
    return Advice(text, priority, category, source=source, **kw)


def _said(gate, adv, now):
    gate.admit(adv, now)
    return adv.speak


# the setting --------------------------------------------------------------------------------------------------------
def test_how_much_doty_talks_defaults_by_level_and_round_trips(tmp_path):
    assert speech.talk_of(CoachConfig()) == "normal"
    assert speech.talk_of(CoachConfig(level="experienced")) == speech.talk_of(CoachConfig(level="pro")) == "less"
    assert speech.talk_of(CoachConfig(level="pro", talk="more")) == "more"  # (chosen: the level doesn't decide)
    assert speech.talk_of(CoachConfig(talk="shout")) == "normal" and speech.talk_of(None) == "normal"
    less, normal, more = (speech.TALK[k] for k in ("less", "normal", "more"))
    assert less.per_min < normal.per_min < more.per_min and less.gap_s > normal.gap_s > more.gap_s
    assert HotkeyConfig().why == "ctrl+alt+y" and HotkeyConfig().why not in (
        HotkeyConfig().ask, HotkeyConfig().plan, HotkeyConfig().mute, HotkeyConfig().toggle_overlay,
        HotkeyConfig().skip_item)
    cfg = Config()
    cfg.coach.talk = "less"
    path = tmp_path / "doty.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    assert load_config(path).coach.talk == "less"


def test_settings_have_how_much_doty_talks(qapp, tmp_path):
    from PySide6.QtWidgets import QComboBox

    from .test_app_ui import make_window

    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    keys = {key for _row, keys, _tab in page._tracked for key in keys}
    assert ("coach", "talk") in keys and ("coach", "speak_up") not in keys  # (one control for what's heard)
    combo = next(row.control for row, keys, _tab in page._tracked if ("coach", "talk") in keys)
    assert isinstance(combo, QComboBox) and combo.currentData() == ""  # automatic: by your experience
    combo.setCurrentIndex(combo.findData("less"))
    page._save()
    assert load_config(tmp_path / "doty.toml").coach.talk == "less"
    win.close()


# the policy ----------------------------------------------------------------------------------------------------------
def test_a_budget_a_minute_and_a_gap_between_lines():
    gate, _game = _gate("normal")  # 4 a minute, 10 s apart
    t = 100.0
    assert _said(gate, _line("Buy a Magic Wand."), t)
    assert not _said(gate, _line("Stack the big camp."), t + 3)  # too soon after the last
    said = [t]
    for i in range(1, 8):
        if _said(gate, _line(f"Line number {i}."), t + 11 * i):
            said.append(t + 11 * i)
    assert said == [100.0, 111.0, 122.0, 133.0, 166.0, 177.0]  # four, then one each time an old one ages out
    assert all(sum(1 for u in said if s - 60 < u <= s) <= speech.TALK["normal"].per_min for s in said)
    gate, _game = _gate("less")
    assert sum(_said(gate, _line(f"Line {i}."), 100.0 + 21 * i) for i in range(3)) == 2  # 2 a minute


def test_danger_and_what_the_player_asked_for_are_always_said():
    gate, game = _gate("less")
    assert _said(gate, _line("Buy a Magic Wand."), 100.0)
    game.fighting = True
    assert _said(gate, _line("Back off, low HP", Priority.CRITICAL, "survival"), 101.0)
    assert _said(gate, _line("Back off now: three are missing.", Priority.HIGH, "trigger", "coach"), 101.5)
    assert _said(gate, _line("Farm the triangle next.", Priority.HIGH, "coach", "coach", asked=True), 102.0)
    assert not _said(gate, _line("Bounty runes in 15s.", Priority.NORMAL, "timer"), 103.0)  # mid-fight: held
    assert not _said(gate, _line("No TP scroll — buy one", Priority.NORMAL, "survival"), 140.0)  # (not act-now)
    # Danger calls don't use up the budget or the gap.
    game.fighting = False
    assert _said(gate, _line("Push mid after this."), 150.0)


def test_quiet_after_a_fight_and_dead_is_a_good_moment():
    gate, game = _gate("more")
    game.fighting = True
    gate.release(100.0)
    game.fighting = False
    assert not _said(gate, _line("Buy Ogre Axe (1,000).", key="afford:item_x:item_ogre_axe"), 102.0)  # just after
    assert gate.release(104.0) == []
    assert [a.text for a in gate.release(106.0)] == ["Buy Ogre Axe (1,000)."]  # the fight is 5 s gone: said now
    game.snap.alive = False
    game.fighting = True  # (the fight around your body isn't yours any more)
    assert _said(gate, _line("You died to the stun: buy a Glimmer Cape.", Priority.HIGH, "coach", "coach"), 130.0)


def test_a_held_line_is_said_when_its_slot_comes_or_never():
    gate, _game = _gate("normal")
    assert _said(gate, _line("Buy a Magic Wand."), 100.0)
    late = _line("Power rune in 15s (10:00)", category="timer", stale_s=4.0)
    later = _line("Farm the jungle until your Black King Bar.", source="coach", category="coach",
                  priority=Priority.HIGH)
    assert not _said(gate, late, 101.0) and not _said(gate, later, 102.0)
    assert gate.release(105.5) == []  # the rune call went stale before the gap opened: dropped, never late
    out = gate.release(110.5)
    assert out == [later] and later.speak and later.stale_s <= 12  # said, with what's left of its time
    assert gate.held_dropped == 1


def test_the_line_that_changes_most_what_you_do_wins():
    gate, _game = _gate("normal")
    assert _said(gate, _line("Buy a Magic Wand."), 100.0)
    general = _line("Your farm is behind the average.", category="progression")
    coach = _line("Take the tower with Luna.", source="coach", category="coach", priority=Priority.HIGH)
    timer = _line("Stack in 7s (pull at 2:53)", category="timer")
    for adv, t in ((general, 101.0), (coach, 102.0), (timer, 103.0)):
        assert not _said(gate, adv, t)
    assert gate.release(110.5) == [timer]  # the call that needs doing now first
    assert gate.release(121.0) == [] and gate.held_dropped == 2  # one slot, one line: the others never said late
    assert not coach.speak and not general.speak
    assert speech.SCORES["timer_now"] > speech.SCORES["buy_now"] > speech.SCORES["focus"] > \
        speech.SCORES["coach"] > speech.SCORES["general"]
    # The game's focus topic (store.memory["focus"]) counts above general advice.
    # (store.memory["focus"] holds the topic's key, knowledge/focus.TOPICS: "lh10" is last hits at 10:00)
    focused = speech.SpeechGate(CoachConfig(), state=Game().state, focus=lambda: "lh10")
    assert speech.kind_of(_line("12 last hits at 5:00.", rule_id="farm_check"), "lh10") == "focus"
    assert speech.kind_of(_line("12 last hits at 5:00.", rule_id="farm_check"), "buyback") == "general"
    assert speech.kind_of(_line("This game: 45 last hits by 10:00.", category="focus"), "") == "focus"
    assert focused.focus() == "lh10"
    from doty.knowledge.focus import TOPICS

    assert set(speech.FOCUS_RULES) == set(TOPICS)  # (every practice topic has its rules)
    # Dead: a buying line is worth more, a call to go and do something now worth less.
    assert speech.score_of("buy_now", dead=True) > speech.score_of("timer_now", dead=True)


def test_one_items_buy_lines_are_capped_across_rules_and_coach():
    owned = {"left": 4050}
    gate, _game = _gate("more", buys=lambda text, snap: {"item_black_king_bar"} if "Black King Bar" in text else set(),
                        cost_left=lambda item, snap: owned["left"])
    t = 100.0
    assert _said(gate, _line("You can afford Black King Bar (4,050)", key="afford:item_black_king_bar"), t)
    assert _said(gate, _line("Buy Black King Bar now.", Priority.HIGH, "coach", "coach"), t + 30)
    assert not _said(gate, _line("Buy Black King Bar before the fight.", Priority.HIGH, "coach", "coach"), t + 60)
    assert gate.release(t + 90) == []  # (over the cap: dropped, not held)
    owned["left"] = 3050  # an Ogre Axe bought: they're building it
    assert _said(gate, _line("Finish Black King Bar next.", Priority.HIGH, "coach", "coach"), t + 120)


def test_an_answer_is_said_or_held_as_one():
    gate, _game = _gate("normal")
    card = Advice("…", Priority.HIGH, "coach", source="coach", speak=False)
    first = _line("You fought alone.", Priority.HIGH, "coach", "coach", display=False, group=card.id)
    rest = _line("Farm with your team next.", Priority.HIGH, "coach", "coach", display=False, group=card.id)
    assert _said(gate, first, 100.0) and _said(gate, rest, 100.5)  # (one line in the budget, both sentences heard)
    other = Advice("…", Priority.HIGH, "coach", source="coach", speak=False)
    a = _line("Push top.", Priority.HIGH, "coach", "coach", display=False, group=other.id)
    b = _line("Then group mid.", Priority.HIGH, "coach", "coach", display=False, group=other.id)
    assert not _said(gate, a, 101.0) and not _said(gate, b, 101.5)  # held together...
    assert gate.release(140.0) == [] and not a.speak and not b.speak  # ...and dropped together (the coach's gap)
    gate, _game = _gate("normal")
    assert _said(gate, _line("Buy a Magic Wand."), 100.0)  # (a rule's line: only the plain gap applies)
    card = Advice("…", Priority.HIGH, "coach", source="coach", speak=False)
    a = _line("Push top.", Priority.HIGH, "coach", "coach", display=False, group=card.id)
    b = _line("Then group mid.", Priority.HIGH, "coach", "coach", display=False, group=card.id)
    assert not _said(gate, a, 101.0) and not _said(gate, b, 101.5)
    assert gate.release(111.0) == [a, b] and a.speak and b.speak


def test_the_bus_runs_the_gate_and_the_voice_hears_only_what_it_lets_through():
    bus, heard = AdviceBus(), Recorder()
    bus.subscribe(heard)
    game = Game()
    bus.gate = speech.SpeechGate(CoachConfig(talk="normal"), state=game.state)
    bus.publish(_line("Buy a Magic Wand."))
    bus.publish(_line("Stack the big camp.", category="timer"))
    assert [a.speak for a in heard.items] == [True, False]  # shown both, said one
    bus.tick()  # (nothing to release yet: the gap)
    assert len(heard.items) == 2


# "why?" -----------------------------------------------------------------------------------------------------------------
def test_why_explains_the_last_tip_from_what_the_code_knows():
    from doty.coach import Coach
    from doty.coach.llm import WHY_ASK
    from doty.rules import RuleEngine
    from doty.state import GameStateStore

    from .test_coach_etiquette import _right_click, _spells_kb
    from .test_game_sense import _feed

    assert all(WHY_ASK.match(q) for q in ("why?", "Why that?", "and why is that?", "Why"))
    assert not WHY_ASK.match("why did Lion kill me") and not WHY_ASK.match("Why should I buy Black King Bar?")
    kb, store = _spells_kb(), GameStateStore()
    bus, heard = AdviceBus(), Recorder()
    bus.subscribe(heard)
    game = Game()
    bus.gate = speech.SpeechGate(CoachConfig(talk="less"), state=game.state)
    coach = Coach(CoachConfig(), store, bus, kb)
    coach.gate = bus.gate
    engine = RuleEngine(store, bus, Config().rules, kb)
    coach.rule_reason = lambda rule_id: speech.rule_reason(engine, rule_id)
    _feed(store, 1.0, clock=900, gold=500, items=["item_power_treads"])
    coach._why_on_loop()
    assert heard.items[-1].text == "Nothing to explain yet." and not heard.items[-1].speak
    # 1. the reason the line's maker gave
    bus.publish(_line("Your call, but think about Black King Bar.", why="it blocks Lion's Earth Spike and Hex."))
    coach._question_on_loop("why?", None)
    assert heard.items[-1].text == "it blocks Lion's Earth Spike and Hex." and heard.items[-1].speak
    # 2. the case for an item the line names, against this lineup
    kb.set_enemies(_right_click(), complete=True)
    bus.gate.last = _line("Buy Black King Bar.")
    coach._why_on_loop()
    assert heard.items[-1].text.startswith("Black King Bar: it blocks") and heard.items[-1].asked
    # 3. the rule's own reason, even when Doty has used up its budget (the player asked)
    bus.gate.last = _line("Items waiting in stash — send the courier", rule_id="stash_items")
    for i in range(5):
        bus.publish(_line(f"Filler {i}."))
    coach._why_on_loop()
    assert heard.items[-1].text.startswith("Items bought while away from the shop") and heard.items[-1].speak
    # 4. nothing the code knows: the coach is asked
    asked = []
    coach.backend, coach.enabled = object(), True
    coach._launch = lambda kind, visible, question=None, **kw: asked.append((kind, question))
    bus.gate.last = _line("Rotate top with Luna.", Priority.HIGH, "coach", "coach")
    coach._why_on_loop()
    assert asked == [("question", 'Why did you say "Rotate top with Luna."? Answer in one or two sentences, '
                                  "from this game.")]


# the bench: a flood, at each level ------------------------------------------------------------------------------
CAMPS = ["big", "small", "ancient", "hard", "medium", "easy", "triangle", "river"]


def _flood(kind, clock, system, content):
    """A coach with something to say on every look, and a prepared call to back off."""
    n = clock // 10
    if kind == "death":
        return "You died alone. Farm with your team next time.\nTRIGGER: hp < 30 => Back off now, you're too low."
    if kind in ("watch", "now"):
        return (f"SAY: Farm the {CAMPS[n % len(CAMPS)]} camp, then look for tower {n % 3 + 1}.\n"
                "TRIGGER: hp < 30 => Back off now, you're too low.")
    return ""


class Chatty:
    """A rule that always has something new to say, every five seconds."""

    id = "chatty"
    cooldown_s = 0.0
    needs_hero = True

    def evaluate(self, ctx):
        if ctx.snap.clock % 5 == 0 and ctx.snap.clock > 0:
            yield Advice(f"Remember to look at the minimap ({ctx.snap.clock}).", Priority.NORMAL, "general",
                         key=f"chatty:{ctx.snap.clock}")


@pytest.mark.parametrize("talk", ["less", "normal", "more"])
def test_a_flood_is_brought_down_to_the_budget(talk):
    from doty.bench import Replay, ScriptedBackend, scorecard, scripted_payloads

    from .test_bench import _kb as bench_kb

    cfg = Config()
    cfg.rules.announce_stacks = cfg.rules.announce_day_night = True
    cfg.coach.speculative_interval_s = 10
    cfg.coach.talk = talk
    replay = Replay(bench_kb(), cfg=cfg, backend=ScriptedBackend(respond=_flood))
    replay.engine.rules.append(Chatty())
    match = replay.run(scripted_payloads(end_clock=1200))[0]
    card = scorecard([match])["overall"]
    assert card["checks"]["rate"]["failed"] == 0 and card["checks"]["coach_budget"]["failed"] == 0
    dangers = [x for x in match.lines if speech.danger(x.priority, x.category, x.text)]
    assert dangers and all(x.said for x in dangers)  # no danger call dropped
    minutes = (match.end_clock - match.start_clock) / 60
    assert card["said"] / minutes <= speech.TALK[talk].per_min
    assert match.talk == talk
