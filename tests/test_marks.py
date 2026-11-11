"""The coach's on-screen drawings: parsing DRAW lines, mapping them onto any window size, and the
overlay showing them (offscreen; nothing appears on the desktop)."""

import os
import time

import pytest

from doty import hud
from doty.marks import Mark, parse

SIZES = {"screen": (1280, 536), "minimap": (440, 440)}


def test_parse_shapes_spaces_labels_and_tones():
    text = ("Back off now.\n"
            'DRAW: circle minimap 132 88 "3 missing" danger\n'
            'DRAW: arrow screen 640 300 320 400 "back to tower"\n'
            "DRAW: box screen 900 100 1000 200 good\n"
            "DRAW: point screen 10 10\n")
    marks = parse(text, SIZES)
    assert len(marks) == 3  # at most three
    c, a, b = marks
    assert (c.shape, c.space, c.tone, c.label) == ("circle", "minimap", "danger", "3 missing")
    assert c.x == pytest.approx(0.3) and c.y == pytest.approx(0.2)
    assert (a.shape, a.x, a.y, a.x2, a.y2) == ("arrow", 0.5, pytest.approx(300 / 536), 0.25, pytest.approx(400 / 536))
    assert (b.shape, b.tone, b.label) == ("box", "good", "")


def test_parse_drops_what_it_cannot_place():
    assert parse('DRAW: circle minimap 100 100 "x"', {"screen": (1280, 536)}) == []  # no minimap was sent
    assert parse("DRAW: arrow screen 10 10", SIZES) == []  # an arrow needs two points
    assert parse("DRAW: circle screen 5000 20", SIZES) == []  # off the image
    assert parse("DRAW: squiggle screen 10 10", SIZES) == []
    assert parse("no drawing here", SIZES) == []
    edge = parse("DRAW: circle screen 1290 540", SIZES)[0]  # a hair outside the edge is clamped
    assert edge.x == 1.0 and edge.y == 1.0


def test_marks_land_on_the_same_spot_at_any_window_size():
    m = Mark("circle", "minimap", 0.5, 0.5)
    for w, h in ((1920, 1080), (3440, 1440), (2560, 1080), (3840, 2160)):
        x, y, _, _, unit = m.to_window(w, h, "left")
        mx, my, ms, _ = hud.minimap_rect(w, h, "left")
        assert (x, y) == (mx + ms / 2, my + ms / 2) and unit == pytest.approx(ms * 0.07)
    x, _, _, _, _ = m.to_window(1920, 1080, "right")
    assert x > 1920 - hud.minimap_rect(1920, 1080, "right")[2]
    s = Mark("arrow", "screen", 0.1, 0.2, 0.3, 0.4).to_window(3440, 1440)
    assert s[:4] == (344.0, 288.0, 1032.0, 576.0)


def test_card_column_scales_and_stays_clear_of_the_hud():
    for w, h in ((1920, 1080), (3440, 1440), (1280, 720), (3840, 2160)):
        x, y, cw, ch = hud.card_column(w, h, "right")
        assert x + cw < w and y > h * hud.TOPBAR_H_FRAC and y + ch < h * (1 - hud.BOTTOM_HUD_FRAC)
    assert hud.card_column(3440, 1440)[2] > hud.card_column(1920, 1080)[2]  # bigger HUD, bigger cards
    assert hud.card_column(1920, 1080, "left")[0] < 100


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_overlay_shows_cards_and_the_coachs_drawing(qapp, monkeypatch):
    from PySide6.QtCore import QRect

    from doty import overlay as ov
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, 3440, 1440), 1.0))
    o = ov.Overlay(OverlayConfig(), "left")
    o.show()
    o._place()
    assert o.width() > 450 and o.x() > 2800  # right side, scaled up for 1440p
    marks = (Mark("circle", "minimap", 0.3, 0.2, label="3 missing", tone="danger"),)
    o.submit(Advice("Three missing: back off.", Priority.HIGH, "coach", source="coach", marks=marks))
    o.submit(Advice("Bounty runes in 15s", Priority.NORMAL, "timer"))
    end = time.time() + 1.0
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(o.cards) == 2 and o.marks.isVisible() and o.marks.marks == marks
    coach_card = next(c for c in o.cards.values() if c.coach)
    assert coach_card.tag_text == "DOTY"
    timer_card = next(c for c in o.cards.values() if not c.coach)
    assert timer_card.tag_text == "TIMER" and timer_card.color == ov.theme.AMBER  # color-coded by kind
    assert "font-weight:700" in coach_card.body.text() and "back off." in coach_card.body.text()  # action first, bold
    img = o.marks.grab().toImage()
    mx, my, ms, _ = hud.minimap_rect(3440, 1440)
    ring = img.pixelColor(int(mx + 0.3 * ms), int(my + 0.2 * ms - ms * 0.07))  # top of the circle
    assert ring.alpha() > 0
    o.marks.ttl = 0.01
    o.marks._frame()  # it sleeps while the drawing is still; wake it
    end = time.time() + 1.0
    while time.time() < end and o.marks.isVisible():
        qapp.processEvents()
        time.sleep(0.01)
    assert not o.marks.isVisible()  # hidden again when it's done: no cost while idle
    o.close()


def test_status_badge_shows_state_and_drags_the_cards(qapp, monkeypatch):
    from PySide6.QtCore import QPoint, QRect

    from doty import overlay as ov
    from doty.config import OverlayConfig

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, 3440, 1440), 1.0))
    saved = []
    o = ov.Overlay(OverlayConfig(), "left", on_moved=lambda: saved.append(list(o.cfg.position)))
    o.show()
    o._place()
    assert o.badge.isVisible()
    o.set_status("Waiting for Dota's game data", "waiting")
    qapp.processEvents()
    assert o.badge.state == "waiting" and o.badge._spin.isActive()  # the arrows turn while it waits
    o.set_status("Watching your game · AI coach ready", "live")
    qapp.processEvents()
    assert o.badge.state == "live" and not o.badge._spin.isActive() and "Watching" in o.badge.accessibleName()
    default_x = o.x()
    o.drag_to(QPoint(400, 300), right=False)
    o.drag_done()
    assert o.column_origin() == QPoint(400, 300) and o.x() == 400 and saved[-1] == [round(400 / 3440, 4),
                                                                                      round(300 / 1440, 4)]
    assert o.badge.geometry().topLeft() == QPoint(400, 300)  # the badge leads the column
    o.reset_position()  # double-click: back to the usual spot
    assert o.cfg.position == [] and o.x() == default_x and saved[-1] == []
    o.close()


def test_drawing_can_be_turned_off(qapp, monkeypatch):
    from PySide6.QtCore import QRect

    from doty import overlay as ov
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, 1920, 1080), 1.0))
    o = ov.Overlay(OverlayConfig(draw=False))
    o.show()
    o._place()
    o.submit(Advice("Look here.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("point", "screen", 0.5, 0.5),)))
    qapp.processEvents()
    qapp.processEvents()
    assert len(o.cards) == 1 and not o.marks.isVisible()
    o.close()


def test_laning_gauge_shows_pace_and_hides_after(qapp, monkeypatch):
    from PySide6.QtCore import QRect

    from doty import overlay as ov
    from doty.config import OverlayConfig

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, 1920, 1080), 1.0))
    o = ov.Overlay(OverlayConfig())
    o.show()
    o.set_laning({"lh": 20, "dn": 4, "pace": 4.0, "target": 5.0, "ratio": 0.8})
    qapp.processEvents()
    assert o.gauge.isVisible() and o.gauge.color() == ov.theme.AMBER  # a bit behind
    o.set_laning(None)
    qapp.processEvents()
    assert not o.gauge.isVisible()
    o.close()


def test_the_badge_stays_put_while_you_drag_past_the_middle(qapp, monkeypatch):
    """The 2 s re-placing must not move the badge mid-drag, nor flip it to the other side after."""
    from PySide6.QtCore import QPoint, QRect

    from doty import overlay as ov
    from doty.config import OverlayConfig

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, 3440, 1440), 1.0))
    o = ov.Overlay(OverlayConfig(), "left")
    o.show()
    o._place()
    o.drag_to(QPoint(1900, 300), right=False)  # past the middle, dragged by its left end
    at = o.badge.geometry().topLeft()
    o._place()  # the pin timer fires mid-drag
    assert o.badge.geometry().topLeft() == at
    o.drag_done()
    o._placed = None
    o._place()  # and again later
    assert o.badge.geometry().topLeft() == at
    o.close()


def test_scaled_screens_map_physical_pixels_to_the_right_place(qapp):
    from doty import overlay as ov

    rect, dpr = ov.native_to_logical(100, 200, 1920, 1080)
    screen = ov._screen_at_native(160, 260)
    assert ov.logical_to_native(screen, rect.x(), rect.y()) == (100, 200)  # there and back
    assert rect.width() == round(1920 / dpr)
