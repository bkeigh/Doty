"""Coaching that fits the player, from never played to Immortal, and the Learn tab."""

from doty.advice import AdviceBus, Priority
from doty.config import RulesConfig
from doty.knowledge import lessons
from doty.rules import RuleEngine, spoken_lines, teaching_lines
from doty.state import GameStateStore

from .conftest import Recorder
from .test_app_ui import qapp  # noqa: F401  (the fixture)


def _snap(store, clock, gold=600, items=(), level=3):
    inv = {f"slot{i}": {"name": n} for i, n in enumerate(items)}
    return store.ingest({"map": {"matchid": "1", "clock_time": clock,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": gold},
                         "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "health": 1000,
                                  "max_health": 1000, "xpos": -6000, "ypos": -6000, "level": level},
                         "items": inv}, received_t=float(clock))


def _engine(level):
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, RulesConfig())
    engine.level_of = lambda: level
    return store, engine, rec


def test_a_new_player_hears_the_basics_one_at_a_time():
    store, engine, rec = _engine("new")
    for clock in range(0, 200, 5):
        engine.on_snapshot(_snap(store, clock))
    basics = [a.spoken for a in rec.items if a.key.startswith("basics@")]
    assert basics == list(teaching_lines()[:3])  # 0:20, 1:40, 3:00, each once
    assert all(line in spoken_lines(RulesConfig()) for line in teaching_lines())  # voiced ahead
    store, engine, rec = _engine("some")
    for clock in range(0, 200, 5):
        engine.on_snapshot(_snap(store, clock))
    assert not [a for a in rec.items if a.key.startswith("basics@")]  # not for anyone else


def test_experienced_players_hear_fewer_shopping_reminders():
    for level, spoken in (("some", True), ("experienced", False)):
        store, engine, rec = _engine(level)
        engine.on_snapshot(_snap(store, 300, gold=200))
        engine.on_snapshot(_snap(store, 301, gold=200))
        tp = [a for a in rec.items if a.key == "missing_tp"]
        assert tp and tp[0].speak is spoken and tp[0].display


def test_learning_players_hear_the_teaching_lines():
    from doty.rules.engine import SPEAK_MORE

    assert "farm_check" in SPEAK_MORE["learning"] and "power_spike" in SPEAK_MORE["new"]
    store, engine, rec = _engine("learning")
    engine.on_snapshot(_snap(store, 400, level=5))
    engine.on_snapshot(_snap(store, 401, level=6))
    lvl6 = [a for a in rec.items if a.key == "lvl6"]
    assert lvl6 and lvl6[0].priority == Priority.NORMAL  # INFO for others; said out loud here


def test_the_level_follows_the_rank():
    assert lessons.level_for_rank(24) == "learning"  # Guardian 4
    assert lessons.level_for_rank(45) == "some" and lessons.level_for_rank(71) == "experienced"
    assert lessons.level_for_rank(80) == "pro" and lessons.level_for_rank(None) == "some"


def test_every_level_has_a_plan_targets_and_tips():
    for key, _name, _who in lessons.LEVELS:
        assert len(lessons.FOCUS[key]) == 3 and lessons.LAST_HIT_TARGETS[key][0] < lessons.LAST_HIT_TARGETS[key][1]
        topics = lessons.tips_for(key)
        assert len(topics) >= 8 and all(tips for _t, tips in topics)
    assert lessons.tips_for("learning")[0][0].key == "die_less"  # what wins most at that rank, first
    assert lessons.tips_for("new")[0][0].key == "basics"
    assert all(t.key != "basics" for t, _ in lessons.tips_for("pro"))


def test_the_learn_tab_sets_your_level_and_follows_your_rank(qapp, tmp_path):
    from .test_app_ui import make_window, pump
    from doty.ui.window import LEARN

    ctl, win = make_window(qapp, tmp_path)
    win.go(LEARN)
    page = win.learn_page
    page._on_result("learn_rank", {"rank_tier": 24})  # Guardian 4, from OpenDota
    assert ctl.cfg.coach.level == "learning" and ctl.cfg.coach.bracket == 2
    assert page.switch.buttons["learning"].isChecked()
    page.pick("experienced")
    assert ctl.cfg.coach.level == "experienced" and not ctl.cfg.coach.level_from_rank
    page._on_result("learn_rank", {"rank_tier": 24})  # picked by hand: the rank no longer changes it
    assert ctl.cfg.coach.level == "experienced"
    pump(qapp, 0.3)
    texts = [page.topics.itemAt(i).widget() for i in range(page.topics.count())]
    assert len(texts) >= 8
    win.close()


def test_learn_search_topics_and_every_level(qapp, tmp_path):
    from .test_app_ui import make_window, pump
    from doty.ui.learn_page import TopicSection
    from doty.ui.window import LEARN

    ctl, win = make_window(qapp, tmp_path)
    win.go(LEARN)
    page = win.learn_page
    ctl.set_level("learning", from_rank=False)
    page.search.setText("black king bar")
    page._apply_search()
    sections = [s for s in page._sections() if s.isVisibleTo(page)]
    assert sections and all(isinstance(s, TopicSection) for s in sections)
    shown = [c.tip for s in sections for c in s.cards if c.isVisibleTo(page)]
    assert shown and all("black king bar" in f"{t.title} {t.text} {' '.join(t.items)}".lower().replace("_", " ")
                         for t in shown)
    assert not page.plan_card.isVisibleTo(page)  # searching: only the tips
    page.search.setText("")
    page._apply_search()
    page.pick_topic("items")
    assert [s.topic.key for s in page._sections()] == ["items"]
    assert page.menu["items"].isChecked() and page.chip_buttons["items"].isChecked()
    page.pick_topic("basics")  # (for Learning, the basics are there, last)
    pro_count = len(page._sections()[0].tips)
    ctl.set_level("pro", from_rank=False)
    page.render(force=True)
    assert not page._sections()  # no basics for top players...
    page.every_level.setChecked(True)
    pump(qapp, 0.2)
    assert len(page._sections()[0].tips) >= pro_count  # ...unless every level is shown
    win.close()


def test_read_to_me_uses_the_voice(qapp, tmp_path):
    from .test_app_ui import make_window

    ctl, win = make_window(qapp, tmp_path)
    said = []

    class FakeVoice:
        available, muted, _thread = True, False, object()

        def interrupt(self):
            said.append("<stop>")

        def say(self, text, **_kw):
            said.append(text)

        def idle(self):
            return True

    ctl._reader = FakeVoice()
    assert ctl.read_aloud(["Dying less.", "Press your save. Use it before the stun."]) == ""
    assert said == ["<stop>", "Dying less.", "Press your save. Use it before the stun."]
    assert not ctl.reading()
    ctl._reader = None
    win.close()
