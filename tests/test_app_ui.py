"""The desktop app: config saving, the engine controller, the window, shortcuts."""

import os
import socket
import sys
import time

import pytest

from doty.config import Config, dump_config, load_config, save_config


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from doty.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    return app


def pump(app, seconds: float, until=None) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.02)


def quiet_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.gsi.port = free_port()
    cfg.voice.enabled = False
    cfg.coach.enabled = False
    cfg.listen.enabled = False
    cfg.overlay.enabled = False
    cfg.hotkeys.enabled = False
    cfg.source_path = tmp_path / "doty.toml"
    return cfg


# config ---------------------------------------------------------------------------


def test_dump_config_writes_only_changes_and_round_trips(tmp_path):
    cfg = Config()
    assert "[" not in dump_config(cfg)  # nothing changed, nothing written
    cfg.listen.mic = 'Microphone (Shure "MV6")'
    cfg.coach.speculative_interval_s = 0
    cfg.rules.muted_rules = ["stack", "day_night"]
    cfg.rules.low_hp_pct = 0.25
    cfg.app.close_to_tray = False
    path = save_config(cfg, tmp_path / "doty.toml")
    text = path.read_text(encoding="utf-8")
    assert "[voice]" not in text and "[listen]" in text
    back = load_config(path)
    assert back.listen.mic == 'Microphone (Shure "MV6")'
    assert back.coach.speculative_interval_s == 0
    assert back.rules.muted_rules == ["stack", "day_night"]
    assert back.rules.low_hp_pct == 0.25
    assert back.app.close_to_tray is False
    assert back.voice == Config().voice


# engine under the window's controller ---------------------------------------------------


def test_controller_runs_demo_then_stops_and_restarts(qapp, tmp_path):
    from doty.ui.controller import Controller

    ctl = Controller(quiet_cfg(tmp_path))
    feed = []
    ctl.advice.connect(feed.append)
    assert ctl.start() and ctl.running
    assert ctl.status()["phase"] == "waiting"

    ctl.start_demo(speed=120, minutes=3)
    pump(qapp, 15, until=lambda: any("Bounty" in e["text"] for e in feed))
    assert ctl.demo_running or feed
    st = ctl.status()
    assert st["phase"] in ("live", "menu") and st["received"] > 0
    assert any("Bounty" in e["text"] for e in feed), [e["text"] for e in feed][:5]
    entry = feed[0]
    assert {"id", "source", "priority", "category", "text", "final", "clock"} <= entry.keys()

    ctl.stop()
    assert not ctl.running and ctl.status() is None
    with socket.socket() as s:  # the port is released
        assert s.connect_ex(("127.0.0.1", ctl.cfg.gsi.port)) != 0
    assert ctl.start() and ctl.running  # and can be taken again
    ctl.stop()


def test_controller_reports_port_in_use(qapp, tmp_path):
    from doty.ui.controller import Controller

    cfg = quiet_cfg(tmp_path)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", cfg.gsi.port))
        blocker.listen(1)
        ctl = Controller(cfg)
        assert ctl.start() is False
    assert not ctl.running and "already running" in ctl.error


# the window --------------------------------------------------------------------------


def make_window(qapp, tmp_path):
    from doty.ui.controller import Controller
    from doty.ui.window import MainWindow

    ctl = Controller(quiet_cfg(tmp_path))
    win = MainWindow(ctl)
    pump(qapp, 20, until=lambda: not win.setup_page._busy)
    return ctl, win


def status(**over):
    base = {"phase": "live", "clock": 754, "paused": False, "hero": "Juggernaut", "rate": 10.0, "received": 50,
            "rules_ms": 0.4, "coach": "ready", "coach_model": "claude-opus-5-5", "coach_ttft_ms": 900.0,
            "voice": "on", "listen": "ready", "listen_key": "mouse5", "mic": "Microphone (Shure MV6)",
            "stt_model": "small.en on cuda", "stt_ms": 110.0, "overlay": "on", "port": 53000,
            "live_coaching": True, "game": None}
    base.update(over)
    return base


GAME = {"team": "radiant", "daytime": True, "clock": 1500, "hero": "Juggernaut", "level": 18, "enemies": ["Lion"],
        "alive": True, "hp": 980, "max_hp": 1720, "mana": 226, "max_mana": 540, "mana_pct": 0.42, "respawn_s": 0,
        "gold": 1840, "kda": (9, 2, 7), "last_hits": 251, "denies": 8, "gpm": 612, "xpm": 560,
        "items": ["Battle Fury"], "item_keys": ["bfury"], "neutral": "", "neutral_key": "", "has_tp": True,
        "timers": [{"in_s": 26, "label": "Bounty runes"}]}


def _checks(gsi=False, launch=False, key=False):
    from doty.checks import Check

    return {"dota": Check("dota", "Dota 2 is installed", True), "gsi": Check("gsi", "Dota connection file", gsi),
            "launch": Check("launch", "Steam launch option", launch), "key": Check("key", "Claude API key", key)}


def test_window_shows_every_state(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    ctl.cfg.coach.enabled = True  # (the quiet test config turns the coach off)
    page = win.coach_page
    page.set_checks(_checks())
    page.set_setup_progress(1, 3)
    page.set_next_step("gsi", "Dota connection file")
    page.refresh(None)
    # paused and not set up: Get started shows one step at a time; Resume isn't the bronze button yet
    assert page.title.text() == "Doty is paused" and page.power._text.text() == "Resume"
    assert page.power.objectName() == "ghost" and page.go_setup_btn._text.text() == "Install it"
    assert page.left.currentWidget() is page.welcome and page.strip.isHidden() and page.actions.isHidden()
    page.set_next_step("launch", "Steam launch option")
    assert page.go_setup_btn._text.text() == "Copy option" and not page.recheck_link.isHidden()
    # the voice step: done once a natural voice is ready (tests keep the free voices undownloaded)
    assert not page.step_voice.number.done and "robotic Windows voice" in page.step_voice.detail.text()
    assert page.voice_btn._text.text() == "Get a free voice"
    ctl.cfg.voice.engine = "windows"
    page.set_next_step("launch", "Steam launch option")
    assert page.step_voice.number.done and "as you chose" in page.step_voice.detail.text()
    ctl.cfg.voice.engine = "auto"
    page.set_checks(_checks(gsi=True, launch=True))
    page.set_setup_progress(3, 3)
    page.set_next_step("", "")
    assert page.step_setup.number.done and page.go_setup_btn.isHidden() and page.power.objectName() == "start"
    page.refresh(None)
    assert page.left.currentWidget() is page.welcome  # the AI key is still missing
    page.skip_key.click()
    assert page.left.currentWidget() is page.hub and ctl.cfg.app.ai_step_skipped  # "Not now", remembered

    ctl.error = "couldn't listen for Dota on port 53000 (is Doty already running?)"
    page.refresh(None)
    assert "couldn't start" in page.title.text() and "already running" in page.subtitle.text()
    assert page.power._text.text() == "Try again"
    ctl.error = ""

    # live: your game is the header; all fine = one quiet line, no chips
    page.refresh(status())
    assert page.banner.isHidden() and page.left.currentWidget() is page.live_view
    win._show_match(status())  # the top bar carries the match: LIVE and the clock
    assert win.clock.text() == "12:34" and win.live_tag.text() == "LIVE" and win.side_status.isHidden()
    # the picture leads (a frame says why while there's none), then your hero; the rail has next up and items
    assert not page.screen.isHidden() and page._column.indexOf(page.screen) < page._column.indexOf(page.left)
    assert not page.rail.isHidden() and not page.timers.isHidden() and not page.build.isHidden()
    assert "All set" in page.game.ready.text() and page.strip.isHidden()
    assert page.b_demo.isHidden() and not page.b_mute.isHidden() and not page.b_plan.isHidden()
    assert page.t_talk.value.text() == "Hold Mouse 5" and page.t_talk.detail.text() == "Shure MV6 mic"
    assert page.t_coach.value.text() == "Ready" and "Opus 5.5" in page.t_coach.detail.text()
    assert page.b_ask.isEnabled() and page.b_ask._text.text() == "Ask now"

    # waiting: not connected yet is normal, not trouble; the demo is offered; match-only buttons hide
    page.refresh(status(phase="waiting"))
    assert page.title.text() == "Waiting for Dota 2" and page.eyebrow.text() == "WAITING"
    assert not page.b_demo.isHidden() and page.t_game.state == "off" and page.strip.isHidden()
    assert "All set" in page.ready_line.text() and not page.banner.isHidden()
    assert page.b_mute.isHidden() and page.b_overlay.isHidden() and page.b_plan.isHidden()
    ctl.cfg.overlay.show_latency = True  # technical details: the numbers come back
    page.refresh(status())
    assert "updates/s" in page.t_game.detail.text() and "first words in 0.9 s" in page.t_coach.detail.text()
    ctl.cfg.overlay.show_latency = False

    # no key: the chip carries the fix, and Ask becomes "Add a key"
    page.refresh(status(phase="menu", coach="off (no API key — set ANTHROPIC_API_KEY)"))
    assert page.t_coach.value.text() == "Needs a key" and "Paste key" in page.t_coach.fix.text()
    assert not page.strip.isHidden() and page.b_ask.objectName() == "ghost" and page.b_ask.isEnabled()
    page.b_ask.click()
    assert page.key_popover.isVisible()
    page.key_popover.close()

    page.refresh(status(voice_engine="elevenlabs", voice_name="George"))
    assert page.t_voice.value.text() == "On" and page.t_voice.detail.text() == "ElevenLabs · George"
    page.refresh(status(voice_engine="kokoro", voice_name="Heart", voice_device="GPU"))
    assert page.t_voice.detail.text() == "Kokoro · Heart · GPU"
    page.refresh(status(voice_engine="windows", voice_problem="out of ElevenLabs credits; using the Windows voice"))
    assert page.t_voice.detail.text().startswith("Out of ElevenLabs credits") and page.t_voice.state == "warn"

    # muting and turning things off are choices: neutral, and the all-set line says so
    page.refresh(status(voice="muted", listen="off"))
    assert page.t_voice.value.text() == "Muted" and page.t_voice.state == "off" and page.strip.isHidden()
    assert "All set" in page.game.ready.text() and "Muted" in page.game.ready.toolTip()
    page.refresh(status(phase="stale", voice="muted", overlay="hidden", listen="loading speech model"))
    assert page.b_mute.toolTip().startswith("Unmute voice") and page.b_overlay.toolTip().startswith("Show")
    assert page.t_talk.value.text() == "Loading…" and page.strip.isHidden()  # Dota quiet in its menu is normal


def test_what_doty_sees_shows_whenever_dota_is_open(qapp, tmp_path):
    from PySide6.QtGui import QColor, QImage

    ctl, win = make_window(qapp, tmp_path)
    page, screen = win.coach_page, win.coach_page.screen
    screen.stop()  # frames come from the test, not the real screen
    pump(qapp, 1.3)
    ctl.cfg.app.ai_step_skipped = True
    page.set_checks(_checks(gsi=True, launch=True))
    page.set_setup_progress(3, 3)
    page.set_next_step("", "")
    page.refresh(status(phase="menu"))
    assert page.left.currentWidget() is page.hub and screen.isHidden()  # no picture of Dota yet
    frame = QImage(320, 180, QImage.Format_RGB32)
    frame.fill(QColor("#335533"))
    screen.set_active(True)
    screen._on_frame(frame)
    page.refresh(status(phase="menu"))
    assert not screen.isHidden() and screen.badge == "LIVE"  # in Dota's menu, before a match
    page.refresh(status())
    assert page.left.currentWidget() is page.live_view and not screen.isHidden()  # and through the match

    # Doty in front of Dota: keep the last picture and say why, rather than film Doty itself
    screen.set_active(False, covering=True)
    assert screen.badge == "PAUSED" and screen.note == "Doty is in front of Dota"
    screen._on_frame(frame)  # a frame grabbed just before doesn't bring it back
    assert screen.badge == "PAUSED"
    screen.set_active(True)
    screen._on_frame(frame)
    assert screen.badge == "LIVE" and not screen.note

    page.refresh(None)
    assert screen.isHidden()  # paused: nothing watches

    # Doty's latest line is captioned over the picture (not its quiet "still watching" notes)
    screen.show_line({"id": 9, "source": "coach", "category": "watching", "priority": 4, "text": "Even lane.",
                      "final": True, "clock": "3:10"})
    assert screen.line is None
    screen.show_line({"id": 10, "source": "coach", "category": "coach", "priority": 2, "text": "Buy a BKB",
                      "final": False, "clock": "3:12"})
    assert screen.line["text"] == "Buy a BKB …" and screen._lt_timer.isActive()


def test_tall_window_stacks_the_coach_page(qapp, tmp_path):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QResizeEvent

    ctl, win = make_window(qapp, tmp_path)
    page = win.coach_page
    page.refresh(status())

    def size(w, h):
        page.resize(w, h)
        page.resizeEvent(QResizeEvent(QSize(w, h), QSize()))

    size(960, 1640)  # Doty on a portrait monitor
    assert page._portrait and page._outer.direction() == page._outer.Direction.TopToBottom
    assert page._pair.direction() == page._pair.Direction.LeftToRight
    size(1300, 860)
    assert not page._portrait and page.rail.maximumWidth() == page.RAIL_WIDTH
    col = page._column  # beside the rail, your hero's panel takes the room under the 16:9 picture
    assert col.stretch(col.indexOf(page.left)) == 1 and col.stretch(col.count() - 1) == 0


def test_window_follows_the_game(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from doty.ui.window import COACH, DRAFT, HEROES, SETTINGS

    ctl, win = make_window(qapp, tmp_path)
    now = [status(phase="menu")]
    ctl.status = lambda: now[0]
    win.refresh()
    assert win.stack.currentIndex() == COACH
    now[0] = status(phase="menu", draft=True)  # hero selection: Draft opens
    win.refresh()
    assert win.stack.currentIndex() == DRAFT and win._auto_back == COACH
    assert not win.draft_page._typed  # a new draft starts clean
    QTest.keyClick(win, Qt.Key_Escape)  # Esc goes back
    assert win.stack.currentIndex() == COACH and win._auto_back is None
    win.go(DRAFT)
    now[0] = status()  # the match starts: back to Coach
    win.refresh()
    assert win.stack.currentIndex() == COACH and win._auto_back == DRAFT
    win.go(HEROES)  # you looked something up yourself: nothing to go back to
    assert win._auto_back is None
    now[0] = status(phase="menu", post_game=True)
    win.refresh()
    assert win.stack.currentIndex() == HEROES  # the recap waits on the Coach page
    win.go(SETTINGS)
    now[0] = status(phase="menu", draft=True)
    win.refresh()
    assert win.stack.currentIndex() == SETTINGS  # never pulled out of Settings
    win.draft_page.rank.setCurrentIndex(win.draft_page.rank.findData(7))  # one rank everywhere
    assert win.heroes_page.rank.currentData() == 7 and ctl.cfg.coach.bracket == 7
    ctl.cfg.app.follow_game = False
    win.go(COACH)
    now[0] = status(phase="menu")
    win.refresh()
    now[0] = status(phase="menu", draft=True)
    win.refresh()
    assert win.stack.currentIndex() == COACH  # following is a setting


def test_coach_page_recaps_the_last_game(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    page = win.coach_page
    page.set_checks(_checks(True, True, True))
    page.set_setup_progress(3, 3)
    page.refresh(status(clock=1500, game=GAME))
    line = {"source": "coach", "priority": 2, "category": "coach", "final": True, "latency_ms": None}
    ctl.advice.emit({**line, "id": 1, "text": "Buy BKB now: three stuns.", "clock": "20:00"})
    ctl.advice.emit({**line, "id": 2, "category": "watching", "priority": 4, "text": "Lane is even.", "clock": "20:10"})
    page.refresh(status(phase="menu", post_game=True, win_team="radiant", clock=None, game=None, hero=""))
    assert page.left.currentWidget() is page.hub and not page.recap_card.isHidden()
    assert page.recap_title.text() == "Victory · Juggernaut" and page.title.text() == "Victory"
    page.refresh(status(phase="stale", clock=None, game=None, hero=""))  # later, Dota quiet in its menu
    assert page.title.text() == "Victory" and page.strip.isHidden()
    assert "Buy BKB now" in page.recap_lines.text() and "Lane is even" not in page.recap_lines.text()
    import re as _re

    assert "251 last hits" in _re.sub(r"<[^>]+>", "", page.recap_stats.text()).replace("&nbsp;", " ")
    page._dismiss_recap()
    assert page.recap_card.isHidden()
    # Pausing (or a restart) mid-match isn't the end of the match
    page.refresh(status(clock=900, game=GAME))
    page.refresh(None)
    page.refresh(status(clock=905, game=GAME))
    assert page.recap is None
    # a problem mid-match sits in the header line with its fix; nothing pushes the game down
    page.refresh(status(clock=910, game=GAME, coach="off (no API key — set ANTHROPIC_API_KEY)"))
    assert page.strip.isHidden() and "Paste key" in page.game.ready.text()
    page._fix_link("coach")
    assert page.key_popover.isVisible()
    page.key_popover.close()


def test_coaching_presets_in_the_coach_page_and_tray(qapp, tmp_path):
    from doty.ui.coach_page import _PresetRow
    from doty.ui.pages import coach_preset, preset_cost
    from doty.ui.window import AppTray

    assert coach_preset(30, "important")[0] == "balanced" and coach_preset(0, "every")[0] == "ask"
    assert coach_preset(20, "every") is None
    # Live looks on Haiku 5.5, the rest on Sonnet 5.5 (2026-11 prices): cents a game, not dollars.
    assert preset_cost(30, "claude-sonnet-5-5") == "about 8¢ a game"
    assert preset_cost(15, "claude-sonnet-5-5") == "about 12¢ a game"
    assert preset_cost(30, "claude-sonnet-5-5", watch_model="") == "about 71¢ a game"  # every look on Sonnet
    assert preset_cost(0, "claude-sonnet-5-5") == "about 5¢ a game" and preset_cost(30, "gpt-5", "openai") == ""
    ctl, win = make_window(qapp, tmp_path)
    page = win.coach_page
    page.refresh(status())
    assert page.b_coaching._text.text() == "Coach: Balanced"
    page._coach_menu()
    rows = [r for r in page.coach_menu.findChildren(_PresetRow)]
    assert len(rows) == 4
    rows[2].clicked.emit()  # Chatty
    assert ctl.cfg.coach.speculative_interval_s == 15 and ctl.cfg.coach.speak_up == "every"
    assert not page.coach_menu.isVisible()
    page.refresh(status())
    assert page.b_coaching._text.text() == "Coach: Chatty"
    tray = AppTray(win, ctl)
    tray._update_menu()
    assert tray.a_presets["chatty"].isChecked() and tray.a_power.text() == "Resume coaching"
    tray.a_presets["quiet"].trigger()
    assert ctl.cfg.coach.speculative_interval_s == 60
    ctl.set_coaching(look_every_s=20, speak_up="every")
    page.refresh(status())
    assert page.b_coaching._text.text() == "Coach: custom"


def theme_brand() -> str:
    from doty.ui import theme

    return theme.BRAND


def test_feed_updates_streaming_entries_in_place(qapp, tmp_path):
    _ctl, win = make_window(qapp, tmp_path)
    feed = win.coach_page.feed
    e = {"id": 7, "source": "coach", "priority": 3, "category": "coach", "text": "Go", "final": False,
         "latency_ms": None, "clock": "10:00"}
    feed.upsert(e)
    feed.upsert({**e, "text": "Go Roshan now.", "final": True})
    item = feed.items[7]
    assert len(feed.items) == 1 and item.plain == "Go Roshan now." and item.tag == "DOTY"
    assert "Go Roshan now." in item.text.text() and theme_brand() in item.text.text()  # the tag in Doty's cyan
    feed.clear()
    assert not feed.items and not feed.empty.isHidden()


def test_settings_page_saves_draft(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    assert page.bar.isHidden()
    page.draft.coach.model = "claude-haiku-5-5"
    page.draft.listen.key = "mouse4"
    page._dirty()
    assert not page.bar.isHidden()
    page._save()
    saved = load_config(tmp_path / "doty.toml")
    assert saved.coach.model == "claude-haiku-5-5" and saved.listen.key == "mouse4"
    assert ctl.cfg.coach.model == "claude-haiku-5-5"
    assert page.bar.isHidden()


def test_settings_page_marks_unsaved_changes_and_finds_settings(qapp, tmp_path):
    from doty.ui.pages import SETTINGS_TABS

    _ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    rows = {row.title_text: (row, tab) for row, _keys, tab in page._tracked}
    key_row, tab = rows["Key"]
    assert tab == SETTINGS_TABS.index("Push-to-talk") and key_row.badge.glyph  # every setting has its icon
    box = key_row.control
    start = box.currentIndex()
    box.setCurrentIndex(box.findData("f8"))  # an edit: the row, its section's tab and the save bar say so
    assert key_row.modified and page.nav.tabs[tab].dot and not page.bar.isHidden()
    assert page.bar_title.text() == "1 unsaved change" and page.bar_list.text() == "Key"
    box.setCurrentIndex(start)  # changed back: nothing to save
    assert not key_row.modified and not page.nav.tabs[tab].dot and page.bar.isHidden()

    voice = page._cards["Voice"]  # voice is off here: its settings wait (dimmed), the header says OFF
    assert voice.state.text() == "Off" and all(g.box.veil.dimmed for g in voice.followers)
    rows["Speak advice"][0].control.setChecked(True)
    assert voice.state.text() == "On" and not any(g.box.veil.dimmed for g in voice.followers)

    page.search.setText("minimap")  # find a setting: only its section shows, Advanced opens for it
    shown = [name for name, card in page._cards.items() if not card.isHidden()]
    assert shown == ["Advanced"] and page._advanced.is_open and not rows["Minimap side"][0].isHidden()
    assert rows["Hide from streams and recordings"][0].isHidden() and page.nav.tabs[0].faded
    page.search.setText("zzz")
    assert page._empty.isVisibleTo(page) and all(c.isHidden() for c in page._cards.values())
    page.search.clear()
    assert not any(c.isHidden() for c in page._cards.values()) and not page.nav.tabs[0].faded

    page.resize(1340, 900)  # wide: the sections are a rail on the left; narrow: tabs along the top
    page._fit_layout()
    assert page.nav.mode == "rail"
    page.resize(900, 700)
    page._fit_layout()
    assert page.nav.mode == "strip"


def test_setup_page_lists_checks(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    _ctl, win = make_window(qapp, tmp_path)
    rows = [win.setup_page.rows.itemAt(i).widget() for i in range(win.setup_page.rows.count())]
    texts = " ".join(r.text() for r in rows if hasattr(r, "text"))
    labels_now = " ".join(lab.text() for lab in win.setup_page.findChildren(QLabel))
    assert "OPEN DOTY LIKE AN APP" not in texts  # Doty adds its shortcuts by itself now
    assert "DONE (" in labels_now and "TO DO" in labels_now and "left:" in labels_now  # what is left comes first
    labels = " ".join(lab.text() for lab in win.setup_page.findChildren(QLabel))
    assert "ElevenLabs voice (optional)" in labels and "Free natural voice: Kokoro (optional)" in labels
    assert win.setup_page._kokoro_btn is not None  # not downloaded in tests: offer it
    assert win.setup_page._kokoro_btn._text.text().startswith("Download (")


def test_settings_voice_section_offers_elevenlabs(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QComboBox, QLabel

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    _ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    page._build()
    labels = " ".join(lab.text() for lab in page.findChildren(QLabel))
    assert "Voice service" in labels and "ElevenLabs voice" in labels and "Hear it" in labels
    assert "Kokoro voice" in labels and page._kokoro_btn is not None
    kokoro_box = [c for c in page.findChildren(QComboBox) if c.findData("am_michael") >= 0][0]
    assert kokoro_box.currentData() == "af_heart"
    kokoro_box.setCurrentIndex(kokoro_box.findData("am_michael"))
    assert page.draft.voice.kokoro_voice == "am_michael" and not page.bar.isHidden()
    page.bar.setVisible(False)
    assert not page._eleven_box.isEnabled()  # no key yet: nothing to pick from
    engines = [c for c in page.findChildren(QComboBox) if c.findData("elevenlabs") >= 0][0]
    engines.setCurrentIndex(engines.findData("windows"))
    assert page.draft.voice.engine == "windows" and not page.bar.isHidden()
    # voices arriving from ElevenLabs fill the picker without marking the page dirty
    page.bar.setVisible(False)
    page._eleven_box.setEnabled(True)
    page._on_eleven_loaded([{"id": "g1", "name": "George", "category": "premade", "labels": {"accent": "british"}}])
    assert page._eleven_box.findData("g1") >= 0 and page.bar.isHidden()


def test_kokoro_download_shows_progress_and_switches_doty_over(qapp, tmp_path, monkeypatch):
    from doty import kokoro

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    calls = []

    def fake_download(folder=None, progress=None, stop=None, base_url="", language="en"):
        for done in (0, 50, 100):
            progress(done, 100)
        calls.append("download")
        asked.append(language)

    asked = []

    monkeypatch.setattr(kokoro, "download", fake_download)
    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    page._build()
    seen, finished, restarts = [], [], []
    ctl.kokoro_progress.connect(seen.append)
    ctl.kokoro_done.connect(finished.append)
    monkeypatch.setattr(type(ctl), "running", property(lambda self: True))
    monkeypatch.setattr(ctl, "restart", lambda: restarts.append(1))
    monkeypatch.setattr(win.setup_page, "recheck", lambda: None)
    ctl.download_kokoro()
    pump(qapp, 5, until=lambda: finished)
    assert calls == ["download"] and finished == [""] and seen[-1] == 100
    assert asked == ["en"]  # (in another language Kokoro speaks, that language's voices come with it)
    assert restarts == [1]  # auto with no ElevenLabs key: Kokoro is the voice now
    assert page._kokoro_btn.isHidden() and ctl.kokoro_pct is None

    def broken(folder=None, progress=None, stop=None, base_url="", language="en"):
        raise kokoro.KokoroError("couldn't download the Kokoro voice (offline)")

    monkeypatch.setattr(kokoro, "download", broken)
    ctl.cfg.voice.engine = "windows"
    finished.clear()
    ctl.download_kokoro()
    pump(qapp, 5, until=lambda: finished)
    assert finished == ["couldn't download the Kokoro voice (offline)"] and restarts == [1]


# shortcuts and icon ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_shortcut_carries_app_id_and_icon(qapp, tmp_path):
    from doty import shortcuts
    from doty.ui.icon import write_ico

    import ctypes
    import struct

    icon = write_ico(tmp_path / "doty.ico")
    data = icon.read_bytes()
    sizes = []
    for i in range(struct.unpack("<H", data[4:6])[0]):
        w, _, _, _, planes, bpp, size, off = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        n = w or 256
        sizes.append(n)
        assert planes == 1 and bpp == 32 and off + size <= len(data)
        if data[off:off + 4] == b"\x89PNG":
            continue
        assert struct.unpack("<i", data[off + 8:off + 12])[0] == 2 * n  # colour rows + AND mask
        assert size == 40 + n * n * 4 + (n + 31) // 32 * 4 * n  # ...and the mask is really there
    assert 16 in sizes and 256 in sizes
    load = ctypes.windll.user32.LoadImageW
    load.restype = ctypes.c_void_p
    for n in (16, 32, 48, 256):  # Windows' own loader reads every size
        h = load(None, str(icon), 1, n, n, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        assert h, n
        ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(h))

    target, args = shortcuts.launch_command()
    assert target.lower().endswith("pythonw.exe") and args == "-m doty app"
    link = shortcuts.create_shortcut(tmp_path / "Doty.lnk", target, args, icon, tmp_path)
    info = shortcuts.read_shortcut(link)
    assert info["app_id"] == shortcuts.AUMID
    assert info["arguments"] == "-m doty app"
    assert info["target"].lower() == target.lower()
    assert info["icon"].lower() == str(icon).lower()


def test_installed_build_keeps_settings_in_appdata_and_launches_itself(monkeypatch):
    from doty import config, shortcuts

    monkeypatch.setattr(config, "FROZEN", True)
    assert config.config_candidates() == [config.APPDATA_DIR / "doty.toml"]
    assert shortcuts.launch_command() == (sys.executable, "")
    assert shortcuts.shortcut_icon() == sys.executable
    monkeypatch.setattr(config, "FROZEN", False)
    assert config.config_candidates()[0] == config.PROJECT_ROOT / "doty.toml"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_source_checkout_never_repoints_an_installed_doty(qapp, tmp_path, monkeypatch):
    from doty import shortcuts

    exe = tmp_path / "Doty.exe"
    exe.write_bytes(b"")
    start, desk = tmp_path / "start" / "Doty.lnk", tmp_path / "desk" / "Doty.lnk"
    shortcuts.create_shortcut(start, str(exe), "", exe, tmp_path)
    shortcuts.create_shortcut(desk, str(exe), "", exe, tmp_path)
    monkeypatch.setattr(shortcuts, "start_menu_path", lambda: start)
    monkeypatch.setattr(shortcuts, "desktop_path", lambda: desk)
    before = start.read_bytes(), desk.read_bytes()
    shortcuts.update_existing_shortcuts()
    assert (start.read_bytes(), desk.read_bytes()) == before  # the installer's shortcuts are left alone


def test_engine_status_includes_live_game_view(qapp, tmp_path):
    from doty.app import Doty
    from doty.sim import ScriptedMatch

    from .conftest import TOKEN

    cfg = quiet_cfg(tmp_path)
    doty = Doty(cfg, quiet=True)
    m = ScriptedMatch()
    for _ in range(844):
        m.step(1)
    doty.store.ingest(m.payload(TOKEN))
    doty.server.received = 1
    doty.server.last_payload_t = time.perf_counter()
    st = doty.status()
    g = st["game"]
    assert st["phase"] == "live" and g["hero"] == "Juggernaut" and g["clock"] == doty.store.current.clock
    assert g["alive"] and g["hp"] > 0
    assert g["max_hp"] > 0 and isinstance(g["items"], list) and g["timers"]
    assert [t["in_s"] for t in g["timers"]] == sorted(t["in_s"] for t in g["timers"])
    assert all(t["in_s"] > 0 for t in g["timers"])
    labels = [t["label"] for t in g["timers"]]
    assert "Power rune" in labels
    doty.shutdown()

    _ctl, win = make_window(qapp, tmp_path)
    win.coach_page.game.set(g)
    assert win.coach_page.game.hero.text() == "Juggernaut" and not win.coach_page.game.content.isHidden()
    win.coach_page.game.set(None)
    assert win.coach_page.game.content.isHidden()


def test_quick_coaching_controls_apply_live_and_save(qapp, tmp_path):
    from doty.ui.controller import Controller

    ctl = Controller(quiet_cfg(tmp_path))
    assert ctl.start()
    ctl.set_coaching(look_every_s=15, speak_up="every")
    assert ctl.doty.coach.cfg.speculative_interval_s == 15 and ctl.doty.coach.cfg.speak_up == "every"
    saved = load_config(tmp_path / "doty.toml")
    assert saved.coach.speculative_interval_s == 15 and saved.coach.speak_up == "every"
    st = ctl.status()
    assert st["look_every_s"] == 15 and st["speak_up"] == "every"
    ctl.stop()


def _fake_stats_service():
    from doty.knowledge.stats import Stats
    from doty.ui.stats_pages import StatsService

    svc = StatsService()
    s = Stats(fetch=lambda url: pytest.fail(f"no network: {url}"))

    def hero(hid, name, wins, roles=("Carry",), attr="agi"):
        return {"id": hid, "name": f"npc_dota_hero_{name.lower()}", "localized_name": name, "roles": list(roles),
                "primary_attr": attr, "attack_type": "Melee", "pub_pick": 10000, "pub_win": wins,
                "5_pick": 10000, "5_win": wins}

    s.heroes = {1: hero(1, "Alpha", 5000), 2: hero(2, "Bravo", 5200), 5: hero(5, "Lion", 5000, ("Support",), "int")}
    s._index = {"alpha": 1, "bravo": 2, "lion": 5}
    s._matchups[5] = [{"hero_id": 1, "games_played": 3000, "wins": 1200}, {"hero_id": 2, "games_played": 3000, "wins": 1500}]
    s._matchups[1] = s._matchups[2] = []  # (typed as enemies in some tests; no network)
    s._items_pop[1] = {"start_game_items": {"16": 10}, "mid_game_items": {"116": 9}}
    s.items = {"branches": {"id": 16, "dname": "Iron Branch", "cost": 50}, "black_king_bar": {"id": 116, "dname": "Black King Bar", "cost": 4050}}
    s.abilities = {"alpha_strike": {"dname": "Strike", "cd": ["10", "8"], "target_team": "Enemy", "bkbpierce": "No", "dispellable": "No"}}
    s.hero_abilities = {"npc_dota_hero_alpha": {"abilities": ["alpha_strike"]}}
    s.loaded = True
    svc.stats, svc._started = s, True
    return svc


def test_draft_page_fills_from_the_screen_and_suggests(qapp, tmp_path):
    from doty.ui.controller import Controller
    from doty.ui.stats_pages import DraftPage

    svc = _fake_stats_service()
    page = DraftPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    summaries = []
    page.summary.connect(summaries.append)
    page.refresh({"hero": "Bravo", "draft_read": {"you": "dire", "radiant": ["Lion"], "dire": ["Bravo"], "bans": []}})
    assert page.ally_inputs[0].text() == "Bravo"
    assert page.enemy_inputs[0].text() == "Lion"
    assert "read from your screen" in page.source.text()
    pump(qapp, 5, until=lambda: page.pick_tiles)
    tiles = {t.name.text(): t for t in page.pick_tiles}
    assert "Alpha" in tiles and tiles["Alpha"].sub.text() == "vs Lion"
    assert "vs Lion: +" in tiles["Alpha"].toolTip()  # Alpha counters Lion; the numbers are on hover
    assert page.ally_slots[0].portrait.hero_id == 2 and page.enemy_slots[0].portrait.hero_id == 5
    assert page.ally_slots[0].portrait._badge == "YOU"
    assert summaries and summaries[-1]["groups"] and "lineup" in summaries[-1]["verdict"].lower()
    slot = page.enemy_slots[1]  # Enter on part of a name takes the first match
    slot.edit()
    slot.box.setText("bra")
    slot._finish()
    assert slot.box.text() == "Bravo"
    slot.box.clear()
    opened = []
    page.open_hero.connect(opened.append)
    tiles["Alpha"].clicked.emit()
    assert opened == ["Alpha"]
    page.ally_inputs[1].setText("Alpha")
    page._typed_change()  # once you type, the screen no longer overwrites your picks
    page.refresh({"draft_read": {"you": "radiant", "radiant": ["Lion"], "dire": [], "bans": []}})
    assert page.ally_inputs[0].text() == "Bravo"


def test_heroes_page_shows_build_and_abilities(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from doty.ui.controller import Controller
    from doty.ui.stats_pages import HeroesPage

    svc = _fake_stats_service()
    page = HeroesPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    page.refresh({"hero": "Alpha"})  # follows your hero
    # (the spells show at once; the build comes from a worker thread)
    pump(qapp, 5, until=lambda: {"Strike", "Iron Branch"} <= {l.text() for l in page.findChildren(QLabel)})
    assert page.title.text() == "Alpha" and page.stack.currentIndex() == 1
    texts = " ".join(l.text() for l in page.findChildren(QLabel))
    assert "Iron Branch" in texts and "Black King Bar" in texts and "Strike" in texts and "BKB stops it" in texts
    page.show_hero("Nobody")
    assert "No hero by that name" in page.facts.text()
    page.show_grid()
    page.search.setText("bra")
    page._filter()  # the grid narrows as you type
    shown = [n for n, pic in page._tiles if not pic.isHidden()]
    assert shown == ["bravo"]
    page.search.setText("")
    page._roles.button(2).click()  # Support
    assert [n for n, pic in page._tiles if not pic.isHidden()] == ["lion"]
    page.sort.setCurrentIndex(page.sort.findData("wr"))  # one list, best first, with win-rate badges
    assert page._pics[2]._badge.endswith("%")


# first run and updates -------------------------------------------------------------------


def test_shortcuts_are_added_once_and_only_for_real_settings(tmp_path, monkeypatch):
    from doty import config, shortcuts

    made = []
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "Doty" / "cache")
    monkeypatch.setattr(config, "FROZEN", False)
    monkeypatch.setattr(shortcuts.sys, "platform", "win32")
    monkeypatch.setattr(shortcuts, "installed_shortcuts", lambda: [])
    monkeypatch.setattr(shortcuts, "install_shortcuts", lambda desktop=True: made.append(desktop) or [])
    assert shortcuts.add_shortcuts_once() is True and made == [True]
    assert shortcuts.add_shortcuts_once() is False and made == [True]  # deleting them later sticks
    cfg = Config()
    cfg.source_path = tmp_path / "throwaway.toml"
    assert not config.is_user_config(cfg)  # tests and previews never touch your PC
    cfg.source_path = None
    assert config.is_user_config(cfg)


def test_code_stamp_changes_when_the_code_does(tmp_path, monkeypatch):
    from doty.ui import main

    before = main.code_stamp()
    src = main.Path(main.__file__)
    st = src.stat()
    newest = float(before.rsplit(":", 1)[1])  # (some other file may be newer than this one)
    try:
        os.utime(src, (st.st_atime, max(st.st_mtime, newest) + 86400))
        assert main.code_stamp() != before  # a newer launch replaces the running copy
    finally:
        os.utime(src, (st.st_atime, st.st_mtime))
    assert main.code_stamp() == before


def test_fold_out_section_and_page_fade(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from doty.ui.motion import Collapsible, CountLabel, FadeStack

    fold = Collapsible("Advanced", "rarely needed")
    fold.body_lay.addWidget(QLabel("inside"))
    assert not fold.is_open and fold.body.isHidden()
    fold.set_open(True, animate=False)
    assert fold.is_open and not fold.body.isHidden()
    fold.header.clicked.emit()
    assert not fold.is_open

    stack = FadeStack()
    a, b = QLabel("a"), QLabel("b")
    stack.addWidget(a)
    stack.addWidget(b)
    stack.resize(200, 100)
    stack.show()
    stack.setCurrentIndex(1)
    pump(qapp, 0.5)
    assert stack.currentWidget() is b and b.pos().y() == 0  # settled back in place
    stack.close()

    n = CountLabel()
    n.set_value(100)
    assert n.text() == "100"
    n.set_value(None)
    assert n.text() == "—"


def test_profile_page_shows_recent_games(qapp, tmp_path):
    from PySide6.QtWidgets import QLabel

    from doty.knowledge.profile import Game
    from doty.ui.controller import Controller
    from doty.ui.profile_page import ProfilePage
    from doty.ui.visuals import HeroPortrait

    svc = _fake_stats_service()
    page = ProfilePage(Controller(quiet_cfg(tmp_path)), svc)
    page._shown = 42
    games = [Game(1, 1, True, 10, 2, 8, 600, 700, 250, 2400, int(time.time()) - 3600, 71.0),
             Game(2, 2, False, 3, 9, 4, 380, 450, 120, 1900, int(time.time()) - 7200, None)]
    page._on_result("profile", (42, {"profile": {"personaname": "Tester"}, "rank_tier": 54}, games))
    import re  # (rich-text labels color their parts, e.g. deaths in red: compare the visible words)

    texts = " ".join(re.sub(r"<[^>]+>", "", lab.text()) for lab in page.findChildren(QLabel))
    assert "Tester" in texts and "WON" in texts and "LOST" in texts and "10 / 2 / 8" in texts
    assert {p.hero_id for p in page.findChildren(HeroPortrait)} >= {1, 2}


# rounds 1 and 2 of the UI audit ----------------------------------------------------------------


def test_stack_is_only_as_tall_as_the_page_showing(qapp):
    from PySide6.QtWidgets import QWidget

    from doty.ui.stats_pages import PageStack

    from doty.ui.widgets import FlowLayout

    stack = PageStack()
    tall, short = QWidget(), QWidget()
    flow = FlowLayout(tall)  # like the hero grid: height-for-width
    for _ in range(300):
        w = QWidget()
        w.setFixedSize(120, 70)
        flow.addWidget(w)
    short.setMinimumHeight(200)
    stack.addWidget(tall)
    stack.addWidget(short)
    stack.setCurrentIndex(1)
    assert stack.minimumSizeHint().height() < 400  # a hero's page no longer stretches to the grid's height
    stack.layout().activate()
    assert not stack.layout().hasHeightForWidth()  # the hidden grid doesn't count
    stack.setCurrentIndex(0)
    stack.layout().activate()
    assert stack.layout().hasHeightForWidth() and stack.layout().totalHeightForWidth(900) > 2000


def test_feed_and_overlay_share_one_meaning_per_color():
    from doty.advice import tone

    assert tone("coach", "coach", 3) == "coach" and tone("coach", "watching", 4) == "quiet"
    assert tone("player", "voice", 2) == "you" and tone("rules", "survival", 1) == "now"
    assert tone("rules", "timer", 3) == "soon" and tone("rules", "economy", 2) == "soon"
    assert tone("rules", "map", 3) == "info"


def test_status_dot_rings_three_times_then_holds(qapp):
    from doty.ui import theme
    from doty.ui.widgets import Dot

    dot = Dot(10)
    dot.set_state(theme.GREEN, True)
    assert dot._timer.isActive()
    for _ in range(200):
        dot._tick()
    assert not dot._timer.isActive()
    dot.set_state(theme.GREEN, True)  # same state again: stays still
    assert not dot._timer.isActive()
    dot.set_state(theme.AMBER, True)  # a change rings again
    assert dot._timer.isActive()


def test_gold_snaps_small_changes(qapp):
    from doty.ui.motion import CountLabel

    gold = CountLabel(snap_below=150)
    gold.set_value(1000)
    gold.set_value(1012)  # passive gold: straight there
    assert gold.text() == "1,012" and gold._anim.state() != gold._anim.State.Running
    gold.set_value(1700)  # a kill: counts up
    assert gold._anim.state() == gold._anim.State.Running


def test_disabled_bronze_button_stays_readable(qapp):
    from doty.ui import theme
    from doty.ui.widgets import button

    from doty.ui.theme import Glyph

    b = button("Ask now", "accent", Glyph.CHAT)
    b.setEnabled(False)
    assert theme.INK not in b._text.styleSheet()


def test_broadcast_controls_read_well_and_speak_their_state(qapp):
    from doty.ui import theme
    from doty.ui.theme import Glyph
    from doty.ui.widgets import Tag, button, set_button

    # tag letters: whichever ink reads better (dark on green, amber and cyan; white on the LIVE red)
    assert theme.ink_on(theme.GREEN) == theme.INK and theme.ink_on(theme.AMBER) == theme.INK
    assert theme.ink_on(theme.BRAND) == theme.INK and theme.ink_on(theme.LIVE) == "#ffffff"
    tag = Tag("Buy now", theme.GREEN, solid=True)
    tag.set("No TP", theme.RED)
    assert tag.accessibleName() == "No TP"
    # a cyan button without an icon still has room for its words (it paints them itself)
    plain = button("Continue with ChatGPT", "primary")
    assert plain.minimumWidth() > 150 and plain.minimumHeight() >= 38
    # icon buttons say what they do now, not what they did when made
    mute = button("Mute", "icon", Glyph.MUTE, "Mute voice")
    assert mute.accessibleName() == "Mute"
    set_button(mute, "Unmute", Glyph.VOLUME)
    assert mute.accessibleName() == "Unmute"
    assert mute.focusPolicy() == mute.focusPolicy().TabFocus  # the keyboard reaches it; a click doesn't


def test_build_panel_shows_the_engines_one_shopping_list(qapp):
    """The panel draws what the engine decided (Knowledge.shopping), so it can't disagree with the strip, the
    spoken calls or the coach; and only the item being built now can ever say Buy now."""
    from doty.ui.live import BuildPanel

    panel = BuildPanel()
    panel.set({"build": None})  # no item data in the engine yet
    assert not panel._lines
    bkb = {"key": "black_king_bar", "name": "Black King Bar", "cost": 4050, "left": 3050, "short": 1350,
           "buy": "mithril_hammer", "buy_name": "Mithril Hammer", "buy_cost": 1600, "finish": False}
    manta = {"key": "manta", "name": "Manta Style", "cost": 4850, "left": 4850}
    panel.set({"build": [bkb, manta], "gold": 9999})
    first, later = panel._lines["black_king_bar"], panel._lines["manta"]
    assert first.name.text() == "Black King Bar" and first.ready.isHidden()
    assert not first.part.isHidden() and first.part.text() == "Buy Mithril Hammer now"
    assert first.togo.text() == "−1,350" and not first.bar.isHidden() and 0.66 < first.bar.frac < 0.67
    # What comes after shows its price, never "Buy now": the gold goes to the first item.
    assert later.ready.isHidden() and later.part.isHidden() and later.bar.isHidden() and later.togo.text() == "4,850"
    panel.set({"build": [{**bkb, "left": 1450, "short": 0, "buy": "black_king_bar", "finish": True}, manta]})
    assert panel._lines["black_king_bar"] is first  # (the same rows: only their numbers changed)
    assert not first.ready.isHidden() and first.part.isHidden() and first.togo.isHidden() and later.ready.isHidden()
    panel.set({"build": []})
    assert not panel._lines


def test_quit_works_while_the_window_is_open(qapp, tmp_path, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from doty.ui import window as window_mod
    from doty.ui.window import AppTray

    tray_available = type("Tray", (), {"isSystemTrayAvailable": staticmethod(lambda: True)})
    monkeypatch.setattr(window_mod, "QSystemTrayIcon", tray_available)  # (the test platform has no tray)
    ctl, win = make_window(qapp, tmp_path)
    assert ctl.start()
    win.tray = AppTray(win, ctl)
    win.show()
    ev = QCloseEvent()
    win.closeEvent(ev)  # closing the window: hide to the tray, keep coaching
    assert not ev.isAccepted() and ctl.running
    qapp.setProperty("doty_quitting", True)  # Quit Doty (tray) or an update taking over
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()
    qapp.setProperty("doty_quitting", False)
    ctl.stop()


def test_splash_shows_the_mark_without_qsplashscreen_delay(qapp):
    """The launch screen is a plain window (QSplashScreen waits up to ~1 s for an expose event first),
    shows Doty's mark, and closes once the main window is up."""
    from PySide6.QtWidgets import QSplashScreen

    from doty.ui.splash import make_splash, mark_rect, render_splash

    splash = make_splash()
    assert not isinstance(splash, QSplashScreen)
    img = render_splash().toImage()
    dpr = img.devicePixelRatio()
    m = mark_rect()
    stem = img.pixelColor(int((m.left() + m.width() * 0.29) * dpr), int(m.center().y() * dpr))  # the mark's cyan stem
    assert stem.blue() > 200 and stem.green() > 150 and stem.red() < 140, stem.name()
    splash.show()
    qapp.processEvents()
    assert splash.isVisible()
    splash.finish(None)
    qapp.processEvents()
    assert not splash.isVisible()


def _drop(target, name, origin=None):
    """Drop a hero on `target` as a drag from `origin` (a slot) or from the pool would."""
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent

    from doty.ui import visuals

    mime = QMimeData()
    mime.setData(visuals.HERO_MIME, name.encode())
    visuals._drag_origin = origin
    try:
        target.dropEvent(QDropEvent(QPointF(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier))
    finally:
        visuals._drag_origin = None


def test_draft_hero_pool_filters_sorts_and_drag_and_drop(qapp, tmp_path):
    from doty.ui.controller import Controller
    from doty.ui.stats_pages import DraftPage

    svc = _fake_stats_service()
    page = DraftPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    pool = page.pool

    def shown():
        return [h for h, p in pool.pics.items() if p.parent() is not None and not p.isHidden()]

    assert set(shown()) == {1, 2, 5}
    pool.attr.setCurrentIndex(pool.attr.findData("int"))
    assert shown() == [5]  # Lion is the only intelligence hero
    pool.attr.setCurrentIndex(0)
    pool.search.setText("al")
    pool.refresh()
    assert shown() == [1]
    pool.search.setText("")
    # Drop Lion on the enemy team: sorting "best vs their picks" puts Alpha (who counters Lion) first.
    _drop(page.enemy_slots[0], "Lion")
    assert page.enemy_inputs[0].text() == "Lion"
    order = [h for h in pool.pics if pool.pics[h].parent() is not None]
    flow = pool.holder.findChildren(type(pool.pics[1]))
    assert flow[0].hero_id == 1 and pool.pics[1]._badge.startswith("+")
    assert pool.pics[5]._dim and not pool.pics[5].drag_text  # already picked: dimmed, can't be dragged again
    # Double-click in the pool fills your team's first empty slot.
    pool.picked.emit("Bravo")
    assert page.ally_inputs[0].text() == "Bravo"
    # Drag one slot onto another: they swap.
    _drop(page.ally_slots[0], "Lion", origin=page.enemy_slots[0])
    assert page.ally_inputs[0].text() == "Lion" and page.enemy_inputs[0].text() == "Bravo"
    # Drag a slot back to the pool: it's emptied.
    _drop(pool, "Lion", origin=page.ally_slots[0])
    assert page.ally_inputs[0].text() == ""


def test_five_tabs_heroes_holds_draft_and_setup_opens_from_settings(qapp, tmp_path):
    from doty.ui.window import DRAFT, HEROES, LEARN, SETTINGS, SETUP

    ctl, win = make_window(qapp, tmp_path)
    labels = [b.accessibleName() or b.text() for b in win.nav_group.buttons()]
    assert len(labels) == 5
    win.nav_group.button(LEARN).click()
    assert win.stack.currentIndex() == LEARN and win.stack.currentWidget() is win.learn_page
    win.go(DRAFT)
    assert win.stack.currentIndex() == DRAFT and win.nav_buttons[DRAFT].isChecked()
    assert win.draft_page.mode.b_draft.isChecked()
    win.draft_page.mode.b_all.click()  # Draft | All heroes
    assert win.stack.currentIndex() == HEROES and win.heroes_page.mode.b_all.isChecked()
    win.go(SETTINGS)
    win.nav_group.button(HEROES).click()  # the Heroes tab reopens where you were
    assert win.stack.currentIndex() == HEROES
    win._setup_badge(2)
    assert "2 to do" in win.settings_page.setup_btn.accessibleName() and win.nav_buttons[SETUP] is win.nav_buttons[SETTINGS]
    win.settings_page.setup_btn.click()
    assert win.stack.currentIndex() == SETUP


def test_recap_shows_the_post_game_review(qapp, tmp_path, monkeypatch):
    import time as _time

    from doty import review as rv

    ctl, win = make_window(qapp, tmp_path)
    page = win.coach_page
    page.set_checks(_checks(True, True, True))
    page.set_setup_progress(3, 3)
    page.refresh(status(clock=1500, game=GAME))
    page.refresh(status(phase="menu", post_game=True, win_team="radiant", clock=None, game=None, hero=""))
    death = rv.Death(clock=754, causes=["low_hp_fight", "no_tp", "after_warning"],
                     said=[{"clock": 748, "text": "Back off, low HP", "source": "rules", "category": "survival"}])
    review = rv.Review(match_id="1", hero_name="Juggernaut", deaths=[death], saved_at=_time.time(),
                       focus=["You died 4 times without a TP scroll: always carry one."])
    monkeypatch.setattr(rv, "latest_review", lambda *a, **k: review)
    page._attach_review()
    assert not page.recap_focus.isHidden() and "without a TP scroll" in page.recap_focus.text()
    text = page.recap_deaths.text()
    assert "12:34" in text and "stayed in a fight on low HP" in text and "Back off, low HP" in text
    old = rv.Review(match_id="0", saved_at=_time.time() - 3600)  # an older game's review isn't shown
    monkeypatch.setattr(rv, "latest_review", lambda *a, **k: old)
    page.review = None
    page._show_recap()
    page._attach_review()
    assert page.review is None and page.recap_focus.isHidden()


def test_a_broken_settings_file_is_kept_and_reported_not_wiped(tmp_path):
    path = tmp_path / "doty.toml"
    path.write_text('[voice]\nkokoro_voice = "af_bella\n', encoding="utf-8")  # a missing quote
    cfg = load_config(path)
    assert "couldn't read doty.toml" in cfg.load_problem and "doty.toml.broken" in cfg.load_problem
    assert (tmp_path / "doty.toml.broken").read_text(encoding="utf-8").startswith("[voice]")
    path.write_text('capture = true\n[voice]\nrate = "fast"\nkokoro_voice = "af_heart"\n', encoding="utf-8")
    cfg = load_config(path)  # wrong kinds of value: those settings stay default, the rest load, no crash
    assert cfg.voice.kokoro_voice == "af_heart" and cfg.voice.rate == Config().voice.rate
    assert "capture" in cfg.load_problem and "voice.rate" in cfg.load_problem


def test_one_run_switches_never_become_saved_settings(tmp_path):
    """`doty run --no-voice`, then dragging the overlay, saves only the overlay position."""
    from doty import app as appmod

    path = tmp_path / "doty.toml"
    path.write_text('[voice]\nkokoro_voice = "af_bella"\n', encoding="utf-8")
    cfg = load_config(path)
    doty = appmod.Doty(cfg, overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.cfg.overlay.position = [0.25, 0.5]
    doty._save_overlay_position()
    saved = load_config(path)
    assert saved.overlay.position == [0.25, 0.5] and saved.voice.enabled and saved.coach.enabled
    assert saved.voice.kokoro_voice == "af_bella"
    doty.shutdown()


def test_draft_never_takes_a_hero_twice_and_the_pool_never_blocks(qapp, tmp_path):
    """A banned hero double-clicked in the pool stays banned; a hero dropped on a second slot leaves the
    first; the pool's matchup sort fetches in the background, never on the window's thread; the search box
    doesn't take a dragged hero as typing."""
    from doty.ui.controller import Controller
    from doty.ui.stats_pages import DraftPage

    svc = _fake_stats_service()
    page = DraftPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    pool = page.pool
    _drop(page.ban_slots[0], "Lion")
    pool._double_clicked(5)  # Lion is banned
    assert page.ally_inputs[0].text() == "" and page.ban_inputs[0].text() == "Lion"
    page._add_to_team("Lion")
    assert page.ally_inputs[0].text() == ""
    _drop(page.enemy_slots[2], "Lion")  # e.g. a stale suggestion tile dragged in
    assert page.enemy_inputs[2].text() == "Lion" and page.ban_inputs[0].text() == ""
    assert not pool.search.acceptDrops()

    fetched = []
    svc.stats._fetch = lambda url: (fetched.append(url), [])[1]
    svc.stats._matchups.pop(2, None)
    ran = []
    real_run = svc.run
    svc.run = lambda name, fn: (ran.append(name), real_run(name, fn))
    page.enemy_inputs[3].setText("Bravo")
    page._typed_change()
    assert "pool-matchups" in ran  # Bravo's matchups load on a worker thread
    pump(qapp, 5, until=lambda: svc.stats.has_matchups(2))
    assert svc.stats.has_matchups(2)


def test_a_restart_keeps_your_draft_picks(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    page = win.draft_page
    win._game_state = "draft"
    page.ally_inputs[1].setText("Lion")
    ctl.status = lambda: status(phase="waiting")  # the engine just restarted: no game data yet
    win.refresh()
    assert win._game_state == "draft" and page.ally_inputs[1].text() == "Lion"
    ctl.status = lambda: {**status(phase="menu"), "draft": {"you": "radiant"}}
    win.refresh()
    assert page.ally_inputs[1].text() == "Lion"  # the same draft: not a new one


def test_saving_settings_never_undoes_a_change_made_elsewhere(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    page.draft.coach.model = "claude-haiku-5-5"  # an unsaved edit on the Settings page
    page._dirty()
    ctl.cfg.overlay.position = [0.1, 0.2]  # meanwhile: the overlay was dragged
    ctl.cfg.coach.speculative_interval_s = 15  # and the Coach menu's preset changed
    page._save()
    saved = load_config(tmp_path / "doty.toml")
    assert saved.coach.model == "claude-haiku-5-5"
    assert saved.overlay.position == [0.1, 0.2] and saved.coach.speculative_interval_s == 15


def test_rank_from_settings_reaches_draft_and_heroes(qapp, tmp_path):
    ctl, win = make_window(qapp, tmp_path)
    ctl.cfg.coach.bracket = 8  # saved in Settings as Immortal
    win.refresh()
    assert win.draft_page.rank.currentData() == 8 and win.heroes_page.rank.currentData() == 8


def test_heroes_page_survives_missing_data_and_unknown_names(qapp, tmp_path):
    from doty.ui.controller import Controller
    from doty.ui.stats_pages import HeroesPage

    svc = _fake_stats_service()
    svc.stats.heroes[2]["primary_attr"] = None  # OpenDota sometimes sends null
    svc.stats.heroes[1]["pub_pick"] = None
    page = HeroesPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    page.sort.setCurrentIndex(page.sort.findData("pick"))
    page._layout_grid()  # no crash
    page.show_hero("Alpha")
    page.show_hero("zzz")
    assert page.title.text() == "zzz" and page.banner.portrait.hero_id is None


def test_heroes_grid_filters_highlights_sorts_and_fits_the_window(qapp, tmp_path):
    """Search ignores case and punctuation and lights up the match; attribute chips mean any of them, role chips
    all of them; the count and the empty state follow; tiles get more columns on a wider window."""
    from doty.ui.controller import Controller
    from doty.ui.stats_pages import HeroesPage, _name_span

    assert _name_span("Anti-Mage", "anti mage") == (0, 9) and _name_span("Nature's Prophet", "natures") == (0, 8)
    assert _name_span("Lion", "xy") is None
    svc = _fake_stats_service()
    svc.stats.heroes[2]["attack_type"] = "Ranged"
    page = HeroesPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()

    def shown():
        return sorted(n for n, pic in page._tiles if not pic.isHidden())

    assert shown() == ["alpha", "bravo", "lion"] and page.count.text() == "All 3 heroes"
    page.search.setText("LI")
    page._filter()
    assert shown() == ["lion"] and page._pics[5].match == (0, 2) and page._pics[5].top_hit
    assert page.count.text().startswith("1 of 3") and not page.clear_btn.isHidden()
    page.clear_filters()
    page._attrs.button(1).click()  # Agility
    page._attrs.button(2).click()  # and Intelligence: either
    assert shown() == ["alpha", "bravo", "lion"]
    page._roles.button(1).click()  # Carry
    assert shown() == ["alpha", "bravo"]
    page._roles.button(2).click()  # and Support: both, which nobody is
    assert shown() == [] and not page.empty.isHidden() and page.count.text().startswith("0 of 3")
    page.clear_filters()
    page._attacks.button(1).click()  # Ranged
    assert shown() == ["bravo"]
    page.clear_filters()
    assert page.empty.isHidden() and page.clear_btn.isHidden()
    page.sort.setCurrentIndex(page.sort.findData("wr"))
    assert page._order[0] == 2 and page.sort.currentData() == "wr"  # Bravo wins most at your rank

    page.resize(1500, 900)
    page.show()
    pump(qapp, 0.3)
    gap = 10
    wide_w = page._tile_w
    wide_cols = (page._scroll.viewport().width() - 12 + gap) // (wide_w + gap)
    page.resize(920, 900)
    pump(qapp, 0.3)
    narrow_cols = (page._scroll.viewport().width() - 12 + gap) // (page._tile_w + gap)
    assert wide_cols > narrow_cols >= 3 and page._tile_w >= 150
    page.close()


def test_heroes_detail_matchups_neighbors_and_keys(qapp, tmp_path):
    """A hero's page lists who counters it and whom it beats (from a worker thread, never the window's),
    steps to the next hero in the grid's order, and Esc or typing goes back to the grid."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from doty.ui.controller import Controller
    from doty.ui.stats_pages import HeroesPage, _MatchRow

    svc = _fake_stats_service()
    svc.stats._items_pop[2] = svc.stats._items_pop[5] = {}
    ran = []
    real_run = svc.run
    svc.run = lambda name, fn: (ran.append(name), real_run(name, fn))
    page = HeroesPage(Controller(quiet_cfg(tmp_path)), svc)
    page._on_ready()
    page.show_hero("Lion")
    assert ran == ["hero"]
    pump(qapp, 5, until=lambda: page.findChildren(_MatchRow))
    rows = {r.other: r for r in page.findChildren(_MatchRow)}
    assert set(rows) == {"Alpha", "Bravo"}  # Lion wins 40% against Alpha (countered), 50% against Bravo
    assert rows["Alpha"].toolTip().startswith("Lion vs Alpha: wins 40.0%") and "-" in rows["Alpha"].toolTip()
    rows["Alpha"].clicked.emit()
    assert page.title.text() == "Alpha" and page.stack.currentIndex() == 1
    page._step(1)  # next in the grid: Alpha, Bravo (agility), Lion (intelligence)
    assert page.title.text() == "Bravo" and page.next_btn.text().endswith("›")
    QTest.keyClick(page, Qt.Key_Escape)
    assert page.stack.currentIndex() == 0 and page.bars.currentIndex() == 0
    page.show_hero("Lion")
    QTest.keyClick(page, Qt.Key_B)  # typing anywhere searches
    assert page.stack.currentIndex() == 0 and page.search.text() == "b"
    assert sorted(n for n, pic in page._tiles if not pic.isHidden()) == ["bravo"]
