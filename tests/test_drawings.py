"""The coach's drawings, lighter and exact: named things placed by code (map places, your own hero, the HUD's
fixed parts), snapping onto seen hero markers, and a thin, see-through look (offscreen; nothing appears on
the desktop)."""

import os
import time
from types import SimpleNamespace

import pytest

from doty import hud
from doty.knowledge import mapgeo
from doty.marks import Mark, fresh_markers, parse, snap_point

SIZES = {"screen": (1280, 536), "minimap": (440, 440)}


# parsing ----------------------------------------------------------------------------------------------------------
def test_named_map_places_and_me_are_parsed_onto_the_minimap():
    text = ('DRAW: arrow me radiant_bot_t1 "back to tower" danger\n'
            'DRAW: circle roshan_top "Roshan"\n'
            "DRAW: point minimap power_rune_bottom\n")
    a, c, p = parse(text, SIZES)
    assert (a.shape, a.space, a.ref, a.ref2, a.label, a.tone) == ("arrow", "minimap", "me", "radiant_bot_t1",
                                                                 "back to tower", "danger")
    assert a.follows_me and not c.follows_me
    assert (c.shape, c.space, c.ref, c.ref2) == ("circle", "minimap", "roshan_top", "")
    assert (p.shape, p.ref) == ("point", "power_rune_bottom")
    box = parse("DRAW: box map dire_ancient", SIZES)[0]
    assert (box.shape, box.space, box.ref) == ("box", "minimap", "dire_ancient")
    alias = parse("DRAW: circle Radiant_Bottom_T1", SIZES)[0]  # (case and a likely spelling)
    assert alias.ref == "radiant_bot_t1"


def test_named_hud_anchors_are_parsed_onto_the_hud():
    box, circle, arrow = parse('DRAW: box hud item_slot_3 "use this"\n'
                               "DRAW: circle hud ultimate good\n"
                               "DRAW: arrow hud item_slot_1 shop_button\n", SIZES)
    assert (box.shape, box.space, box.ref, box.label) == ("box", "hud", "item_slot_3", "use this")
    assert (circle.ref, circle.tone) == ("ultimate", "good")
    assert (arrow.ref, arrow.ref2) == ("item_slot_1", "shop_button")
    assert box.on_hud and circle.on_hud
    assert parse("DRAW: box screen shop_button", SIZES)[0].space == "hud"  # "screen" with a HUD name is the HUD
    assert parse("DRAW: box hud dire_hero_2", SIZES)[0].ref == "dire_hero_2"


def test_unknown_or_mixed_names_are_dropped_never_guessed():
    assert parse('DRAW: circle radiant_bot_t4 "x"', SIZES) == []  # no such tower
    assert parse("DRAW: circle somewhere_nice", SIZES) == []
    assert parse("DRAW: box hud item_slot_9", SIZES) == []
    assert parse("DRAW: arrow me", SIZES) == []  # an arrow needs two names
    assert parse("DRAW: arrow me item_slot_1", SIZES) == []  # map and HUD don't mix
    assert parse("DRAW: circle roshan_top 300 200", SIZES) == []  # names and pixels don't mix
    assert parse("DRAW: circle hud roshan_top", SIZES) == []  # a map place isn't on the HUD
    assert parse("DRAW: circle minimap item_slot_1", SIZES) == []
    assert parse("DRAW: circle me", SIZES, have_me=False) == []  # Doty doesn't know where you are
    assert parse("DRAW: arrow me radiant_fountain", SIZES, have_me=False) == []
    # Pixels still work, and need the image they're in.
    assert parse("DRAW: circle hud 10 10", SIZES) == []
    assert parse("DRAW: circle screen 640 268", SIZES)[0].x == pytest.approx(0.5)


def test_every_place_in_the_table_parses():
    for name in mapgeo.PLACES:
        (m,) = parse(f"DRAW: circle {name}", SIZES)
        assert m.ref == name and m.space == "minimap"
    for name in hud.ANCHORS:
        (m,) = parse(f"DRAW: box hud {name}", SIZES)
        assert m.ref == name and m.space == "hud"
    assert mapgeo.place("nowhere") is None and mapgeo.canonical("") == ""


# world -> minimap ----------------------------------------------------------------------------------------------------
def test_world_to_minimap_corners_centre_and_a_tower():
    assert mapgeo.to_minimap(mapgeo.WORLD_MIN, mapgeo.WORLD_MAX) == (0.0, 0.0)  # top-left
    assert mapgeo.to_minimap(mapgeo.WORLD_MAX, mapgeo.WORLD_MIN) == (1.0, 1.0)  # bottom-right
    mid = (mapgeo.WORLD_MIN + mapgeo.WORLD_MAX) / 2
    assert mapgeo.to_minimap(mid, mid) == pytest.approx((0.5, 0.5))
    assert mapgeo.to_minimap(-99999, 99999) == (0.0, 0.0)  # clamped
    fx, fy = mapgeo.to_minimap(*mapgeo.place("radiant_bot_t1"))  # bottom lane, right of centre, low on the map
    assert 0.5 < fx < 0.8 and 0.75 < fy < 0.9
    rx, ry = mapgeo.to_minimap(*mapgeo.place("radiant_fountain"))
    dx, dy = mapgeo.to_minimap(*mapgeo.place("dire_fountain"))
    assert rx < 0.2 and ry > 0.8 and dx > 0.8 and dy < 0.2  # Radiant bottom-left, Dire top-right


def test_the_map_is_laid_out_the_way_dota_is():
    """Every tower sits on its own side of the river (x + y < 0 for Radiant), in its own lane's part of the map."""
    from doty.knowledge import mapinfo

    for name, pos in mapgeo.PLACES.items():
        side = name.split("_")[0]
        if side in ("radiant", "dire") and ("_t" in name or "rax" in name or "ancient" in name):
            assert (pos[0] + pos[1] < 0) == (side == "radiant"), name
        for lane in ("top", "bot"):  # (tier 3s stand at the base, between the lanes' strips)
            if name.startswith((f"{side}_{lane}_t1", f"{side}_{lane}_t2")):
                assert mapinfo.band(pos) == {"top": "top", "bot": "bottom"}[lane], name


@pytest.mark.parametrize("side", ["left", "right"])
def test_named_places_land_on_the_minimap_picture(side):
    for w, h in ((1920, 1080), (2560, 1440), (3440, 1440)):
        mx, my, ms, _ = hud.map_rect(w, h, side)
        crop = hud.minimap_rect(w, h, side)
        assert crop[0] <= mx and mx + ms <= crop[0] + crop[2] + 0.5 and crop[1] <= my and my + ms <= h  # inside the crop
        if side == "right":
            assert mx + ms < w and mx > w / 2
        corner = Mark("circle", "minimap", 0, 0, ref="radiant_fountain").to_window(w, h, side)
        assert mx < corner[0] < mx + ms * 0.2 and my + ms * 0.8 < corner[1] < my + ms
        tower = Mark("circle", "minimap", 0, 0, ref="radiant_bot_t1").to_window(w, h, side)
        fx, fy = mapgeo.to_minimap(4860, -6379)
        assert tower[:2] == pytest.approx((mx + fx * ms, my + fy * ms))
        centre = Mark("point", "minimap", 0, 0, ref="me").to_window(w, h, side, me=(-32, -32))
        assert centre[:2] == pytest.approx((mx + ms / 2, my + ms / 2))


def test_an_arrow_from_me_follows_where_i_am():
    m = Mark("arrow", "minimap", 0, 0, ref="me", ref2="radiant_bot_t1")
    a = m.to_window(1920, 1080, "left", me=(-1000, -1000))
    b = m.to_window(1920, 1080, "left", me=(0, -1000))
    assert b[0] > a[0] and b[1] == pytest.approx(a[1]) and a[2:4] == b[2:4]  # the tail moved, the tip didn't


# snapping ----------------------------------------------------------------------------------------------------------
def _marker(x, y, team):
    return SimpleNamespace(x=x, y=y, team=team)


def test_a_minimap_point_snaps_to_the_nearest_seen_marker_of_its_team():
    markers = [_marker(0.32, 0.21, "dire"), _marker(0.30, 0.22, "radiant"), _marker(0.6, 0.6, "dire")]
    assert snap_point(0.3, 0.2, "dire", markers) == (0.32, 0.21)
    assert snap_point(0.3, 0.2, "dire", []) == (0.3, 0.2)  # no markers: nothing changes
    assert snap_point(0.3, 0.2, "dire", None) == (0.3, 0.2)
    assert snap_point(0.1, 0.9, "dire", markers) == (0.1, 0.9)  # none near enough
    # Through parse: "enemy" is the other side from yours; without a team word, no snapping.
    (m,) = parse('DRAW: circle minimap 132 88 "Lion" enemy', SIZES, team="radiant", markers=markers)
    assert (m.x, m.y) == (0.32, 0.21)
    (m,) = parse('DRAW: circle minimap 132 88 "Lion" ally', SIZES, team="radiant", markers=markers)
    assert (m.x, m.y) == (0.30, 0.22)
    (m,) = parse('DRAW: circle minimap 132 88 "Lion"', SIZES, team="radiant", markers=markers)
    assert (m.x, m.y) == pytest.approx((0.3, 0.2))
    (m,) = parse('DRAW: circle minimap 132 88 "Lion" enemy', SIZES, team="radiant")  # the reader isn't running
    assert (m.x, m.y) == pytest.approx((0.3, 0.2))


def test_only_fresh_markers_count():
    seen = [_marker(0.5, 0.5, "dire")]
    assert fresh_markers((100.0, seen), now=100.5) == seen
    assert fresh_markers((100.0, seen), now=110.0) == []
    assert fresh_markers(None) == []


# HUD anchors ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1440), (3440, 1440), (5120, 1440), (1680, 1050), (1024, 768)])
def test_hud_anchors_stay_on_the_bottom_bar_at_any_aspect_ratio(size):
    w, h = size
    for name in hud.ANCHORS:
        x, y, rw, rh = hud.anchor(name, w, h)
        assert 0 <= x and x + rw <= w + 0.5 and 0 <= y and y + rh <= h + 0.5, name
        assert rw > 0 and rh > 0
    u = hud.unit(w, h)
    slots = [hud.anchor(f"item_slot_{k}", w, h) for k in range(1, 7)]
    assert slots[0][1] == slots[2][1] < slots[3][1] and slots[0][0] < slots[1][0] < slots[2][0]  # two rows of three
    abilities = [hud.anchor(f"ability_{k}", w, h) for k in range(1, 5)]
    assert all(a[1] > h - 0.2 * u for a in abilities)  # in the bottom bar
    assert abs((abilities[0][0] + abilities[-1][0] + abilities[-1][2]) / 2 - (w / 2 + hud.ABILITY_CENTER * u)) < 1
    assert hud.anchor("ultimate", w, h) == hud.anchor("ability_4", w, h)
    assert hud.anchor("ultimate", w, h, abilities=6) == hud.anchor("ability_6", w, h, abilities=6)
    shop = hud.anchor("shop_button", w, h)
    assert shop[0] + shop[2] < w and shop[0] > w - 0.3 * u  # the bottom-right corner
    # The HUD scales with the height on wide screens: the same slot is the same size at 16:9 and 32:9.
    assert hud.anchor("item_slot_1", 5120, 1440)[2] == pytest.approx(hud.anchor("item_slot_1", 2560, 1440)[2])
    assert hud.anchor("nowhere", w, h) is None


def test_hud_marks_resolve_onto_their_anchor():
    box = Mark("box", "hud", 0, 0, ref="item_slot_3")
    x, y, x2, y2, _ = box.to_window(1920, 1080)
    ax, ay, aw, ah = hud.anchor("item_slot_3", 1920, 1080)
    assert x < ax and y < ay and x2 > ax + aw and y2 > ay + ah  # around it, a hair outside
    cx, cy, *_rest = Mark("circle", "hud", 0, 0, ref="item_slot_3").to_window(1920, 1080)
    assert (cx, cy) == pytest.approx((ax + aw / 2, ay + ah / 2))
    mm = Mark("box", "hud", 0, 0, ref="minimap").to_window(1920, 1080, "right")
    assert mm[0] > 1500  # the minimap anchor follows Dota's minimap-on-the-right option


# the look -----------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _layer(monkeypatch, cfg, size=(1920, 1080)):
    from PySide6.QtCore import QRect

    from doty import overlay as ov

    monkeypatch.setattr(ov, "dota_area", lambda: (QRect(0, 0, *size), 1.0))
    monkeypatch.setattr(ov, "dota_covered", lambda: False)
    o = ov.Overlay(cfg, "left")
    o.show()
    o._place()
    return o, ov


def _pump(qapp, seconds):
    end = time.time() + seconds
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)


def _line_profile(img, x: int, y0: int, y1: int):
    """Alpha down one column."""
    return [img.pixelColor(x, y).alpha() for y in range(y0, y1)]


@pytest.mark.parametrize("width,opacity", [(2.0, 0.65), (6.0, 1.0)])
def test_the_painter_uses_the_configured_width_and_opacity(qapp, monkeypatch, width, opacity):
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    o, ov = _layer(monkeypatch, OverlayConfig(mark_width=width, mark_opacity=opacity))
    # A level arrow across the middle, no label: a column through its shaft shows the line's thickness.
    o.submit(Advice("Look.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("arrow", "screen", 0.2, 0.5, 0.6, 0.5),)))
    _pump(qapp, 0.8)
    img = o.marks.grab().toImage()
    col = _line_profile(img, int(0.4 * 1920), 520, 560)
    solid = [a for a in col if a > 0]
    peak = max(col)
    assert peak == pytest.approx(255 * opacity, abs=12)  # the line itself, at the configured opacity
    assert width <= len(solid) <= width + 2 * ov.EDGE_PX + 3  # the line plus a hairline edge, no glow
    o.close()


def test_drawings_are_much_lighter_than_before(qapp, monkeypatch):
    """The default line is a few px across (the old one was ~14 px with its glow) and see-through."""
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    cfg = OverlayConfig()
    assert (cfg.mark_width, cfg.mark_opacity) == (2.0, 0.65)
    o, ov = _layer(monkeypatch, cfg)
    o.submit(Advice("Look.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("circle", "screen", 0.5, 0.5),)))
    _pump(qapp, 0.8)
    img = o.marks.grab().toImage()
    r = 1080 * 0.045
    col = _line_profile(img, 960, int(540 - r - 12), int(540 - r + 12))  # across the ring's top
    assert 2 <= sum(1 for a in col if a > 0) <= 6 and max(col) < 200
    inside = img.pixelColor(960, 540)
    assert inside.alpha() == 0  # hollow
    o.close()


def test_a_mark_on_me_follows_my_hero_and_comes_down_without_it(qapp, monkeypatch):
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    o, ov = _layer(monkeypatch, OverlayConfig())
    layer = o.marks
    mark = Mark("circle", "minimap", 0, 0, ref="me", label="you")
    o.submit(Advice("Here.", Priority.HIGH, "coach", source="coach", marks=(mark,)))
    _pump(qapp, 0.1)
    assert not layer.isVisible()  # Doty doesn't know where you are yet: nothing to draw on "me"
    o.set_me((-2000, -2000))
    o.submit(Advice("Here.", Priority.HIGH, "coach", source="coach",
                    marks=(mark, Mark("circle", "minimap", 0, 0, ref="roshan_top"))))
    _pump(qapp, 0.1)
    assert layer.isVisible() and len(layer.marks) == 2
    before = layer._win(mark, layer.width(), layer.height())
    o.set_me((2000, -2000))
    _pump(qapp, 0.05)
    after = layer._win(mark, layer.width(), layer.height())
    assert after[0] > before[0]
    box = layer._dirty.boundingRect()
    assert box.contains(int(after[0]), int(after[1]))  # the new spot gets painted
    o.set_me(None)  # dead, or no game data
    _pump(qapp, 0.05)
    assert [m.ref for m in layer.marks] == ["roshan_top"]  # the fixed place stays
    o.close()


def test_hud_drawings_survive_walking_on(qapp, monkeypatch):
    from doty.advice import Advice, Priority
    from doty.config import OverlayConfig

    o, ov = _layer(monkeypatch, OverlayConfig())
    o.submit(Advice("Use it.", Priority.HIGH, "coach", source="coach",
                    marks=(Mark("box", "hud", 0, 0, ref="item_slot_2", label="use this"),
                           Mark("circle", "screen", 0.5, 0.5))))
    _pump(qapp, 0.1)
    o.drop_marks("screen")
    _pump(qapp, 0.05)
    assert [m.space for m in o.marks.marks] == ["hud"]
    o.close()
