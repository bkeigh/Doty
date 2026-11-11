"""The overlay's live strip, drawings that stay honest and cheap, and the faster screen reads behind them."""

import os
import time

import numpy as np
import pytest
from PIL import Image

from doty import app as appmod
from doty.advice import Advice, Priority
from doty.config import Config, OverlayConfig
from doty.marks import Mark
from doty.sim import ScriptedMatch

from .conftest import TOKEN
from .test_game_sense import JUGG, _kb


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _overlay(monkeypatch, cfg=None, size=(3440, 1440)):
    from PySide6.QtCore import QRect

    from doty import overlay as ov

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, *size), 1.0))
    monkeypatch.setattr(ov, "dota_covered", lambda: False)
    o = ov.Overlay(cfg or OverlayConfig(), "left")
    o.show()
    o._place()
    return o, ov


def _pump(qapp, seconds=0.05):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)


STRIP = {"timers": [(14, ["Bounty runes", "Power rune"]), (74, ["Wisdom shrines"])],
         "item": {"name": "Battle Fury", "left": 1143, "eta": 800, "done": 0.7}}


def test_the_live_strip_shows_what_is_next_and_is_kept_out_of_the_coachs_screenshots(qapp, monkeypatch):
    o, ov = _overlay(monkeypatch)
    assert not o.strip.isVisible() and o.mask_rect_px is None
    o.set_hud(STRIP)
    _pump(qapp)
    assert o.strip.isVisible() and o.strip.rows() == 2
    assert o.strip.item_text() == "1,143 to go · ~13:20"
    assert o.mask_rect_px is not None and o.mask_rect_px[3] >= o.strip.height()  # masked with no card up
    assert not o.strip.grab().toImage().isNull()  # (it paints)
    o.set_hud({"timers": [], "item": {"name": "Black King Bar", "left": 0, "eta": None, "done": 1.0}})
    _pump(qapp)
    assert o.strip.rows() == 1 and o.strip.item_text() == "Affordable now"
    o.submit(Advice("Bounty runes in 15s", Priority.NORMAL, "timer"))
    _pump(qapp)
    card = next(iter(o.cards.values()))
    assert card.geometry().top() >= o.strip.geometry().bottom()  # cards stack under the strip
    o.set_hud(None)
    _pump(qapp)
    assert not o.strip.isVisible()
    o.close()


def test_the_strip_can_be_turned_off(qapp, monkeypatch):
    o, ov = _overlay(monkeypatch, OverlayConfig(strip=False))
    o.set_hud(STRIP)
    _pump(qapp)
    assert not o.strip.isVisible()
    o.close()


def test_drawings_on_the_game_world_come_down_and_minimap_ones_stay(qapp, monkeypatch):
    o, ov = _overlay(monkeypatch)
    marks = (Mark("circle", "minimap", 0.3, 0.2, label="3 missing"), Mark("circle", "screen", 0.6, 0.4, label="Lion"))
    o.submit(Advice("Back off.", Priority.HIGH, "coach", source="coach", marks=marks))
    _pump(qapp, 0.1)
    assert o.marks.isVisible() and len(o.marks.marks) == 2
    o.drop_marks("screen")
    _pump(qapp)
    assert o.marks.isVisible() and [m.space for m in o.marks.marks] == ["minimap"]
    o.drop_marks("minimap")
    _pump(qapp)
    assert not o.marks.isVisible()
    # A box on Dota's own bottom bar (an ability, the shop button) doesn't move with the camera: it stays.
    hud_box = Mark("box", "screen", 0.4, 0.9, 0.45, 0.97, label="level this")
    assert hud_box.on_hud and not Mark("circle", "screen", 0.5, 0.5).on_hud
    o.submit(Advice("Level it.", Priority.HIGH, "coach", source="coach", marks=(hud_box,)))
    _pump(qapp, 0.1)
    o.drop_marks("screen")
    _pump(qapp)
    assert o.marks.isVisible() and o.marks.marks == (hud_box,)
    o.close()


def test_only_the_patches_with_marks_are_redrawn(qapp, monkeypatch):
    """The mark layer is as big as the game window (3440x1440); a frame of animation repaints the marks and
    the pointer, not five million pixels."""
    o, ov = _overlay(monkeypatch)
    o.submit(Advice("Look.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("circle", "screen", 0.6, 0.4, label="Lion"),)))
    _pump(qapp, 0.1)
    layer = o.marks
    box = layer._dirty.boundingRect()
    assert 0 < box.width() * box.height() < 0.05 * layer.width() * layer.height()
    s = layer._scale()
    start, _ = layer._pointer_at(0.0, s)
    target = layer._pointer_target(s)
    far = ((start.x() - target.x()) ** 2 + (start.y() - target.y()) ** 2) ** 0.5
    assert far <= ov.POINTER_FROM * s + 1  # the pointer darts in from nearby, not from across the screen
    assert layer._dirty.contains(layer._pointer_target(s).toPoint())
    o.close()


def test_a_long_label_past_a_slanted_arrow_is_inside_what_gets_redrawn(qapp, monkeypatch):
    o, ov = _overlay(monkeypatch, size=(1920, 1080))
    arrow = Mark("arrow", "screen", 0.3, 0.3, 0.5, 0.6, label="Phantom Assassin missing from the top lane")
    o.submit(Advice("Careful.", Priority.HIGH, "coach", source="coach", marks=(arrow,)))
    _pump(qapp, 0.1)
    layer = o.marks
    s = layer._scale()
    anchor, beyond = layer._label_anchor(arrow, layer.width(), layer.height())
    box = layer._label_box(arrow.label, anchor, layer.width(), layer.height(), s, beyond).toAlignedRect()
    assert layer._dirty.intersected(box).boundingRect() == box  # every pixel of the label is repainted
    o.close()


def test_an_arrows_label_isnt_flooded_with_the_arrows_colour(qapp, monkeypatch):
    """The label used to be drawn with the arrowhead's fill still set: white words on a cyan slab."""
    o, ov = _overlay(monkeypatch, size=(1920, 1080))
    o.submit(Advice("Go back.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("arrow", "screen", 0.5, 0.5, 0.3, 0.5, label="back to tower"),)))
    _pump(qapp, 0.9)
    img = o.marks.grab().toImage()
    # Just past the tip (the arrow points left), inside the label: the dark panel, not the arrow's cyan.
    dark = [img.pixelColor(int(0.3 * 1920) - dx, int(0.5 * 1080)) for dx in range(30, 120, 6)]
    assert sum(1 for c in dark if c.alpha() > 150 and c.red() < 60 and c.blue() < 60) >= 5
    o.close()


def test_everything_hides_while_another_app_covers_dota(qapp, monkeypatch):
    o, ov = _overlay(monkeypatch)
    o.submit(Advice("Look.", Priority.HIGH, "coach", source="coach", marks=(Mark("point", "screen", 0.5, 0.5),)))
    _pump(qapp, 0.1)
    assert o.marks.isVisible() and o.badge.isVisible()
    monkeypatch.setattr(ov, "dota_covered", lambda: True)
    o._check_covered()
    assert not o.marks.isVisible() and not o.badge.isVisible() and o.windowOpacity() == 0.0
    o.submit(Advice("Again.", Priority.HIGH, "coach", source="coach", marks=(Mark("point", "screen", 0.5, 0.5),)))
    _pump(qapp)
    assert not o.marks.isVisible()  # nothing is drawn over the other app
    monkeypatch.setattr(ov, "dota_covered", lambda: False)
    o._check_covered()
    assert o.badge.isVisible() and o.windowOpacity() == 1.0
    o.close()


# the app side -----------------------------------------------------------------------------------------------------
class FakeOverlay:
    def __init__(self):
        self.hud, self.dropped = [], []

    def set_hud(self, info):
        self.hud.append(info)

    def drop_marks(self, space="screen"):
        self.dropped.append(space)


def _doty(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb = _kb()
    doty.engine.kb = doty.kb
    doty.kb._full.add(8)  # (the hero's data counts as fetched: no downloads in a test)
    return doty


def test_the_strip_gets_the_next_spawns_and_the_item_being_built(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch)
    doty.kb.custom_plan[JUGG] = ["item_black_king_bar"]
    m = ScriptedMatch()
    m.step(90 + 340)  # 5:40
    payload = m.payload(TOKEN)
    payload["items"]["slot5"] = {"name": "item_ogre_axe"}
    payload["player"]["gold"] = 1050
    snap = doty.store.ingest(payload)
    hud = doty._hud(snap)
    assert hud["timers"][0] == (20, ["Power rune"]) and hud["timers"][1] == (80, ["Wisdom shrines"])
    assert hud["item"]["name"] == "Black King Bar" and hud["item"]["left"] == 2000  # 3,050 left, 1,050 in hand
    assert hud["item"]["eta"] == 340 + int(2000 / 450 * 60) and 0.5 < hud["item"]["done"] < 0.51
    doty.cfg.overlay.strip = False
    assert doty._hud(snap) is None
    doty.cfg.overlay.strip = True
    doty.overlay = FakeOverlay()
    doty._second = None
    doty._on_payload(payload, 1.0)
    doty._on_payload(payload, 1.1)  # the same game second: once
    assert len(doty.overlay.hud) == 1 and doty.overlay.hud[0]["item"]["name"] == "Black King Bar"
    doty.overlay = None


def test_screen_drawings_come_down_once_you_walk_on(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch)
    doty.overlay = FakeOverlay()
    m = ScriptedMatch()
    m.step(90 + 100)
    doty._on_payload(m.payload(TOKEN), 1.0)
    mark = Mark("circle", "screen", 0.5, 0.5, label="here")
    doty.bus.publish(Advice("Look here.", Priority.HIGH, "coach", source="coach", marks=(mark,)))
    doty._on_payload(m.payload(TOKEN), 1.1)
    assert doty.overlay.dropped == []  # standing still: they stay
    walked = m.payload(TOKEN)
    walked["hero"]["xpos"] += 900
    doty._on_payload(walked, 1.2)
    assert doty.overlay.dropped == ["screen"]
    doty._on_payload(walked, 1.3)
    assert doty.overlay.dropped == ["screen"]  # once
    doty.bus.publish(Advice("Map.", Priority.HIGH, "coach", source="coach",
                            marks=(Mark("circle", "minimap", 0.5, 0.5),)))
    walked["hero"]["xpos"] += 900
    doty._on_payload(walked, 1.4)
    assert doty.overlay.dropped == ["screen"]  # minimap drawings don't depend on where you stand
    doty.overlay = None


def test_lines_with_names_in_them_are_voiced_ahead(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch)
    doty.kb.custom_plan[JUGG] = ["item_black_king_bar"]
    made = []
    doty.voice.prepare_now = lambda lines: made.extend(lines)
    m = ScriptedMatch()
    m.step(90 + 340)
    snap = doty.store.ingest(m.payload(TOKEN))
    doty._voice_ahead(snap)
    assert {"Buy Black King Bar", "Finish Black King Bar", "Buy Ogre Axe", "Buy Mithril Hammer"} <= set(made)
    assert not any("Recipe" in line for line in made)
    n = len(made)
    doty._voice_ahead(snap)
    assert len(made) == n  # each line once


# faster screen reads --------------------------------------------------------------------------------------------------
def test_the_top_bar_reads_the_same_from_just_its_strip():
    from doty import vision

    from .test_vision import compose, make_portraits

    portraits = make_portraits(14)
    reader = vision.TopBarReader(portraits)
    heroes = list(portraits)[:10]
    frame, _rects = compose(3440, 1440, heroes, portraits, dead=(2, 7))
    whole = reader.read(frame)
    box = reader.band_box(3440, 1440)
    band = np.asarray(frame.crop(box))
    assert (box[2] - box[0]) * (box[3] - box[1]) < 0.06 * 3440 * 1440  # a sliver of the window
    strip = reader.read_band(band, box, (3440, 1440))
    assert [(s.hero, s.dead, s.side, s.index) for s in strip] == [(s.hero, s.dead, s.side, s.index) for s in whole]
    assert [s.hero for s in strip] == heroes


def test_the_reader_grabs_only_the_strip_and_falls_back_to_the_window(monkeypatch):
    from doty import capture

    grabbed = []

    class Shot:
        def __init__(self, w, h, level):
            self.width, self.height, self.bgra = w, h, bytes([level]) * (w * h * 4)

    class Sct:
        level = 90

        def grab(self, region):
            grabbed.append(region)
            return Shot(region["width"], region["height"], self.level)

        def close(self):
            pass

    monkeypatch.setattr(capture, "find_dota_rect", lambda: (100, 50, 1920, 1080))
    monkeypatch.setattr(capture.mss, "MSS", Sct)
    g = capture.StripGrabber()
    band, box, size = g.grab(lambda w, h: (400, 0, 1500, 60))
    assert grabbed == [{"left": 500, "top": 50, "width": 1100, "height": 60}] and size == (1920, 1080)
    assert band.shape == (60, 1100, 3) and box == (400, 0, 1500, 60)
    Sct.level = 0  # a black strip (a display mode the screen grab can't see): the caller falls back
    assert g.grab(lambda w, h: (400, 0, 1500, 60)) is None
    monkeypatch.setattr(capture, "find_dota_rect", lambda: None)
    assert g.grab(lambda w, h: (0, 0, 10, 10)) is None
    g.close()


def test_screenshots_shrink_in_whole_steps_first():
    from doty.capture import _shrink

    img = Image.new("RGB", (3440, 1440), (10, 200, 30))
    small = _shrink(img, 1024)
    assert small.size == (1024, round(1440 * 1024 / 3440)) and small.getpixel((500, 200)) == (10, 200, 30)
    assert _shrink(Image.new("RGB", (800, 600)), 1024).size == (800, 600)  # never enlarged
    assert _shrink(Image.new("RGB", (2048, 1000)), 1024).size == (1024, 500)
