"""Doty's own screen readers (vision.py), on synthetic frames: a fake top bar of random-texture
"portraits" at the default layout (moved a little, to test snapping), a fake minimap with team-coloured
markers, and calibration recovering a known layout. No real game art and no network."""

import importlib.util
import statistics
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from doty import hud, vision
from doty.vision import DEFAULT_LAYOUT, MinimapReader, TopBarLayout, TopBarReader

PLAYER_COLORS = [(51, 117, 255), (102, 255, 191), (191, 0, 191), (243, 240, 11), (255, 107, 0),
                 (254, 134, 194), (161, 180, 71), (101, 217, 247), (0, 131, 33), (164, 105, 0)]
ROOT = Path(__file__).resolve().parent.parent


def _font(size: float):
    return ImageFont.load_default(size=max(8, int(size)))


def make_portraits(n: int, seed: int = 1) -> dict[str, Image.Image]:
    """n smooth random-texture 256x144 "portraits" (Valve's are that size), named like npc heroes."""
    rng = np.random.default_rng(seed)
    out = {}
    for i in range(n):
        img = Image.fromarray(rng.integers(0, 256, (4, 7, 3), dtype=np.uint8)).resize((256, 144), Image.BICUBIC)
        d = ImageDraw.Draw(img)
        for _ in range(6):
            x0, y0 = int(rng.integers(0, 220)), int(rng.integers(0, 110))
            box = (x0, y0, x0 + int(rng.integers(20, 120)), y0 + int(rng.integers(15, 70)))
            color = tuple(int(v) for v in rng.integers(0, 256, 3))
            (d.ellipse if rng.random() < 0.5 else d.rectangle)(box, fill=color)
        out[f"npc_dota_hero_test{i:03d}"] = img.filter(ImageFilter.GaussianBlur(2))
    return out


def compose(w, h, heroes, portraits, layout=DEFAULT_LAYOUT, dead=(), empty=(), seed=0):
    """A fake Dota frame: game-world texture, the dark top-bar panel, a clock with kill scores, the ten
    portraits (dead ones greyed and darkened, with a respawn number) and a player-colour bar under each.
    Returns (frame, the slots' integer rects as drawn)."""
    rng = np.random.default_rng(seed)
    u = vision.unit(w, h)
    world = rng.integers(40, 200, (h // 24 + 2, w // 24 + 2, 3), dtype=np.uint8)
    frame = Image.fromarray(world).resize((w, h), Image.BILINEAR)
    d = ImageDraw.Draw(frame)
    rects = layout.rects(w, h)
    cx = w / 2 + layout.center * u
    d.rectangle((rects[0][0] - 0.03 * u, 0, rects[9][0] + rects[9][2] + 0.03 * u, 0.05 * u), fill=(16, 18, 22))
    d.rectangle((cx - 0.055 * u, 0, cx + 0.055 * u, 0.042 * u), fill=(38, 42, 50))
    d.text((cx, 0.02 * u), "12:34", fill="white", font=_font(0.022 * u), anchor="mm")
    d.text((cx - 0.068 * u, 0.018 * u), "17", fill=(120, 230, 120), font=_font(0.02 * u), anchor="mm")
    d.text((cx + 0.068 * u, 0.018 * u), "9", fill=(230, 100, 100), font=_font(0.02 * u), anchor="mm")
    truth = []
    for i, (x, y, sw, sh) in enumerate(rects):
        X, Y = round(x), round(y)
        W, H = round(x + sw) - X, round(y + sh) - Y
        truth.append((X, Y, W, H))
        d.rectangle((X, Y + H + 1, X + W - 1, Y + H + max(2, round(0.004 * u))), fill=PLAYER_COLORS[i])
        if i in empty:
            continue
        pic = portraits[heroes[i]].convert("RGB").resize((W, H), Image.LANCZOS)
        if i in dead:
            pic = ImageOps.grayscale(pic).point(lambda v: int(v * 0.5)).convert("RGB")
            ImageDraw.Draw(pic).text((W / 2, H / 2), "23", fill="white", font=_font(H * 0.6), anchor="mm")
        frame.paste(pic, (X, Y))
    return frame, truth


def moved(layout=DEFAULT_LAYOUT, dx=0.0, dy=0.0, scale=1.0, u=1080.0) -> TopBarLayout:
    """The layout drawn dx, dy px off (and `scale` times the size): what Dota draws when our constants
    are a little wrong."""
    return replace(layout, slot_w=layout.slot_w * scale, slot_h=layout.slot_h * scale, gap=layout.gap * scale,
                   center=layout.center + dx / u, top=layout.top + dy / u)


@pytest.fixture(scope="module")
def portraits():
    return make_portraits(20)


@pytest.fixture(scope="module")
def reader(portraits):
    return TopBarReader(portraits)


def _assert_rects_close(fit, truth, tol=1.0):
    for (x, y, w, h), (tx, ty, tw, th) in zip(fit.rects, truth):
        assert abs(x - tx) <= tol and abs(x + w - (tx + tw)) <= tol, (x, w, tx, tw)
        assert abs(y - ty) <= tol, (y, ty)


# top bar ------------------------------------------------------------------------------------------
def test_layout_puts_five_and_five_around_the_clock_at_any_aspect():
    for w, h in ((1920, 1080), (2560, 1080), (3440, 1440), (5120, 1440), (3840, 2160)):
        rects = DEFAULT_LAYOUT.rects(w, h)
        assert len(rects) == 10
        rad, dire = rects[:5], rects[5:]
        assert all(x + rw <= w / 2 for x, _, rw, _ in rad) and all(x >= w / 2 for x, _, _, _ in dire)
        assert [r[0] for r in rects] == sorted(r[0] for r in rects)  # player order, left to right
        assert rad[4][0] + rad[4][2] == pytest.approx(w - dire[0][0])  # mirrored about the clock
        assert rects[0][2] == pytest.approx(DEFAULT_LAYOUT.slot_w * h)  # sized by height (16:9 and wider)
        tx, ty, tw, th = hud.topbar_rect(w, h)  # inside the crop the coach used to get
        assert rects[0][0] >= tx and rects[9][0] + rects[9][2] <= tx + tw and rects[0][1] + rects[0][3] <= th


@pytest.mark.parametrize("w,h,dx,dy,scale", [(3440, 1440, 6, 2, 1.0), (1920, 1080, -4, 1, 1.03),
                                             (2560, 1080, 3, -2, 0.97)])
def test_reads_every_slot_and_snaps_to_the_portraits(reader, portraits, w, h, dx, dy, scale):
    names = list(portraits)
    heroes = [names[i] for i in (3, 7, 1, 12, 0, 19, 5, 9, 14, 2)]
    frame, truth = compose(w, h, heroes, portraits, moved(dx=dx, dy=dy, scale=scale, u=h), dead={2})
    slots = reader.read(frame)
    _assert_rects_close(reader.last_fit, truth)
    assert reader.last_fit.snapped
    assert [(s.side, s.index) for s in slots] == [(side, i) for side in ("radiant", "dire") for i in range(5)]
    assert [s.hero for s in slots] == heroes
    for i, s in enumerate(slots):
        assert s.dead == (i == 2), s
        assert s.confidence >= (0.5 if s.dead else 0.7), s
        assert s.margin > 0.1 and s.second and s.second != s.hero


def test_snapping_fixes_a_layout_that_is_off(reader, portraits):
    names = list(portraits)
    heroes = names[5:15]
    frame, truth = compose(1920, 1080, heroes, portraits, moved(dx=14, dy=3, u=1080))
    guess = reader.find_slots(frame, snap=False)
    assert abs(guess.rects[0][0] - truth[0][0]) >= 13  # the guess is a quarter slot off...
    _assert_rects_close(reader.find_slots(frame), truth)  # ...snapping lands on the portraits
    assert [s.hero for s in reader.read(frame)] == heroes


def test_numpy_and_bgra_frames_read_the_same(reader, portraits):
    heroes = list(portraits)[10:20]
    frame, _ = compose(1920, 1080, heroes, portraits, moved(dx=2, u=1080))
    rgb = np.asarray(frame)
    bgra = np.concatenate([rgb[:, :, ::-1], np.full(rgb.shape[:2] + (1,), 255, np.uint8)], axis=2).tobytes()
    view = vision.frame_from_bgra(1920, 1080, bgra)
    assert np.array_equal(view, rgb)
    a, b, c = reader.read(frame), reader.read(rgb), reader.read(view)
    assert [s.hero for s in a] == [s.hero for s in b] == [s.hero for s in c] == heroes
    assert [s.score for s in a] == [s.score for s in b]


def test_empty_slots_and_candidates(reader, portraits):
    names = list(portraits)
    heroes = names[:10]
    frame, _ = compose(1920, 1080, heroes, portraits, empty={6})
    slots = reader.read(frame)
    assert slots[6].hero == "" and slots[6].confidence == 0.0 and not slots[6].dead
    assert [s.hero for i, s in enumerate(slots) if i != 6] == [h for i, h in enumerate(heroes) if i != 6]
    only = set(heroes)
    restricted = reader.read(frame, candidates=only)
    assert {s.hero for s in restricted if s.hero} <= only
    assert [s.hero for s in restricted] == [s.hero for s in slots]
    assert vision.teams(slots) == {"radiant": heroes[:5], "dire": heroes[5:6] + heroes[7:]}


def test_no_top_bar_reads_as_nothing(reader):
    """A frame without the bar (a menu, a loading screen, the bar hidden) must not name anyone surely."""
    rng = np.random.default_rng(4)
    world = Image.fromarray(rng.integers(0, 256, (1440 // 24 + 2, 2560 // 24 + 2, 3), dtype=np.uint8))
    for frame in (world.resize((2560, 1440), Image.BILINEAR), Image.new("RGB", (1920, 1080), (20, 22, 26))):
        slots = reader.read(frame)
        assert max(s.confidence for s in slots) < 0.05
        assert reader.last_fit.found == (0.0, 0.0) and not reader.last_fit.snapped


def test_each_hero_fills_one_slot(portraits):
    """With the same portrait drawn twice (impossible in a real game), only one slot may claim it."""
    names = list(portraits)
    heroes = names[:9] + [names[0]]
    frame, _ = compose(1920, 1080, heroes, portraits)
    r = TopBarReader(portraits)
    slots = r.read(frame)
    assert sum(s.hero == names[0] for s in slots) == 1
    assert [s.hero for s in r.read(frame, unique=False)].count(names[0]) == 2


def test_fast_enough_with_every_hero_at_3440x1440():
    portraits = make_portraits(127, seed=7)
    names = list(portraits)
    heroes = [names[i] for i in (0, 17, 33, 48, 61, 77, 90, 104, 115, 126)]
    frame, _ = compose(3440, 1440, heroes, portraits, moved(dx=-5, dy=1, u=1440), dead={7})
    reader = TopBarReader(portraits)
    rgb = np.asarray(frame)
    for _ in range(3):  # warm up
        reader.read(frame)
    times = []
    for f in (frame, rgb) * 10:
        t0 = time.perf_counter()
        slots = reader.read(f)
        times.append((time.perf_counter() - t0) * 1000)
    assert [s.hero for s in slots] == heroes and slots[7].dead
    assert statistics.median(times) < 20, times


def test_portraits_from_the_art_cache_through_a_loader(tmp_path, portraits):
    names = list(portraits)[:10]
    folder = tmp_path / "hero"
    folder.mkdir()
    for npc in names[:8]:  # two aren't downloaded yet
        portraits[npc].convert("RGBA").save(folder / f"{npc.removeprefix('npc_dota_hero_')}.png")
    heroes_json = tmp_path / "heroes.json"
    heroes_json.write_text('{"1": {"name": "%s"}, "2": {"name": "%s"}}' % (names[0], names[1]), encoding="utf-8")
    assert vision.known_heroes(heroes_json) == sorted(names[:2])
    assert vision.cached_portrait(names[0], folder).size == (256, 144)
    assert vision.cached_portrait(names[9], folder) is None
    assert list(vision.load_portraits(names, lambda npc: vision.cached_portrait(npc, folder))) == names[:8]
    reader = TopBarReader(lambda npc: vision.cached_portrait(npc, folder), heroes=names)
    assert reader.heroes == names[:8] and reader.missing == names[8:]
    frame, _ = compose(1920, 1080, names[:8] + names[:2], portraits, empty={8, 9})
    assert [s.hero for s in reader.read(frame)][:8] == names[:8]
    with pytest.raises(ValueError):
        TopBarReader({})


# minimap --------------------------------------------------------------------------------------------
RADIANT_AT = [(0.20, 0.75), (0.45, 0.55), (0.30, 0.30)]
DIRE_AT = [(0.70, 0.25), (0.62, 0.48)]


def minimap_frame(w=1920, h=1080, side="left", seed=3):
    rng = np.random.default_rng(seed)
    frame = Image.fromarray(rng.integers(60, 180, (h // 30 + 2, w // 30 + 2, 3), dtype=np.uint8)).resize((w, h))
    mx, my, s, _ = hud.minimap_rect(w, h, side)
    terrain = np.clip(np.array([38, 58, 34]) + rng.normal(0, 8, (s, s, 3)), 0, 255).astype(np.uint8)  # dark, muted
    mm = Image.fromarray(terrain)
    d = ImageDraw.Draw(mm)
    d.line((0.1 * s, 0.9 * s, 0.9 * s, 0.1 * s), fill=(120, 110, 90), width=max(2, s // 40))  # mid lane
    r = 0.045 * s / 2
    for team, spots, color in (("radiant", RADIANT_AT, (60, 210, 80)), ("dire", DIRE_AT, (220, 50, 45))):
        for x, y in spots:
            d.ellipse((x * s - r, y * s - r, x * s + r, y * s + r), fill=color)
        for _ in range(6):  # creeps: dots too small for a hero
            x, y = rng.uniform(0.05, 0.95, 2) * s
            d.rectangle((x, y, x + 0.01 * s, y + 0.01 * s), fill=color)
    d.ellipse((0.05 * s, 0.05 * s, 0.27 * s, 0.27 * s), fill=(220, 50, 45))  # too big: a ping or a glow
    d.line((0.55 * s, 0.95 * s, 0.95 * s, 0.6 * s), fill=(220, 50, 45), width=2)  # a thin line: not compact
    frame.paste(mm, (mx, my))
    return frame


@pytest.mark.parametrize("side", ["left", "right"])
def test_minimap_counts_hero_markers_per_team(side):
    reader = MinimapReader()
    assert reader.calibrated is False  # shadow mode until checked on real frames
    markers = reader.read(minimap_frame(side=side), minimap_side=side)
    assert reader.count("radiant") == 3 and reader.count("dire") == 2
    for team, spots in (("radiant", RADIANT_AT), ("dire", DIRE_AT)):
        found = sorted((m.x, m.y) for m in markers if m.team == team)
        for (x, y), (fx, fy) in zip(sorted(spots), found):
            assert abs(x - fx) < 0.01 and abs(y - fy) < 0.01
    assert all(0.0 < m.confidence <= 1.0 and 0.03 < m.size < 0.06 for m in markers)
    assert reader.last_ms < 50


def test_minimap_reads_saved_crops_and_custom_colours():
    frame = minimap_frame()
    mx, my, s, _ = hud.minimap_rect(1920, 1080, "left")
    crop = frame.crop((mx, my, mx + s, my + s))
    reader = MinimapReader({"ally": vision.TEAM_COLORS["radiant"], "enemy": vision.TEAM_COLORS["dire"]})
    markers = reader.read_crop(np.asarray(crop))
    assert reader.count("ally", markers) == 3 and reader.count("enemy", markers) == 2
    assert MinimapReader().read_crop(Image.new("RGB", (300, 300), (30, 40, 30))) == []


def test_components_without_scipy():
    mask = np.zeros((20, 20), bool)
    mask[2:5, 2:5] = True  # a square
    mask[10:18, 3] = True  # a bar
    mask[10, 3:12] = True  # joined to an arm: one L-shaped blob
    mask[15:17, 15:19] = True
    ys, xs, lab = vision._components(mask)
    assert lab.max() + 1 == 3 and sorted(np.bincount(lab)) == [8, 9, 16]


# calibration -----------------------------------------------------------------------------------------
TRUE = TopBarLayout(slot_w=0.062, slot_h=0.0349, gap=0.005, clock_gap=0.095, top=0.004)


def test_calibration_recovers_a_known_layout(tmp_path, portraits):
    names = list(portraits)
    paths = []
    for n, (w, h) in enumerate(((1920, 1080), (2560, 1080), (3440, 1440), (3840, 2160))):
        heroes = [names[(n * 3 + i) % len(names)] for i in range(10)]
        frame, _ = compose(w, h, heroes, portraits, TRUE, dead={n + 4}, seed=n)
        if h >= 1440:  # match_capture saves the top-bar crop too: use that for some
            tx, ty, tw, th = hud.topbar_rect(w, h)
            frame = frame.crop((tx, ty, tx + tw, ty + th))
        paths.append(tmp_path / f"frame-{n}.png")
        frame.save(paths[-1])
    noise = Image.fromarray(np.random.default_rng(9).integers(0, 256, (1080, 1920, 3), dtype=np.uint8))
    noise.save(tmp_path / "noise.png")
    result = vision.calibrate_from_frames([*paths, tmp_path / "noise.png", tmp_path / "missing.png"])
    assert result["frames"] == 6 and result["used"] == 4 and result["reliable"]
    assert result["per_frame"][2]["kind"] == "topbar" and result["per_frame"][2]["unit_px"] == 1440
    assert result["per_frame"][3]["unit_px"] == 2160 and not result["per_frame"][4]["ok"]
    px = 1 / 1080
    for key, tol in (("slot_w", 1.2), ("gap", 1.2), ("clock_gap", 1.5), ("center", 1.0), ("top", 1.5),
                     ("slot_h", 2.0)):
        assert result[key] == pytest.approx(getattr(TRUE, key), abs=tol * px), key
    assert result["layout"].startswith("TopBarLayout(slot_w=")
    measured = eval(result["layout"], {"TopBarLayout": TopBarLayout})  # pasteable as is
    names2 = names[4:14]
    frame, _ = compose(1920, 1080, names2, portraits, TRUE)
    assert [s.hero for s in TopBarReader(portraits, measured).read(frame)] == names2
    frame.save(tmp_path / "frame.jpg", quality=92)  # JPEG alone: flagged as not to be trusted
    assert not vision.calibrate_from_frames([tmp_path / "frame.jpg"])["reliable"]


def test_calibrate_cli(tmp_path, portraits, capsys):
    spec = importlib.util.spec_from_file_location("calibrate_hud", ROOT / "tools" / "calibrate_hud.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    frame, _ = compose(1920, 1080, list(portraits)[:10], portraits, TRUE)
    frame.save(tmp_path / "frame-00m30s-every.png")
    assert tool.main([str(tmp_path), "--draw", str(tmp_path / "checks")]) == 0
    out = capsys.readouterr().out
    assert "TopBarLayout(slot_w=" in out and "frame-00m30s-every.png" in out
    assert (tmp_path / "checks" / "frame-00m30s-every-slots.png").is_file()
    assert tool.main([str(tmp_path / "nothing-here-*.png")]) == 2
