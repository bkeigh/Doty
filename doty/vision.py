"""Doty's own eyes: reading the top bar's ten hero portraits and the minimap's hero markers straight
from the screen, in plain image code (numpy and Pillow), many times a second.

Why: the coach used to ask the AI to read these from screenshots. That takes 2-4 s and goes wrong in
ways that matter (an ally put on the enemy team, a hero named who isn't in the game). Comparing each
portrait with Valve's own hero pictures takes a few milliseconds, can only ever name a real hero, and
can't swap the sides: Radiant is always the five slots left of the clock.

Fair play: this only looks at the pixels of the player's own screen, the same thing the player sees.
No game memory is read and no input is sent.

Status: shadow mode. The geometry here is a best guess at Dota's current HUD, kept in constants
(TopBarLayout, the minimap colour ranges) so that `calibrate_from_frames` (tools/calibrate_hud.py) can
replace it with numbers measured on real frames. Until then these reads are logged next to the AI's,
not shown to the player.

How the top bar is read:
1. Geometry: TopBarLayout gives the ten portrait rectangles in fractions of the HUD unit (the window
   height on 16:9 and wider: Dota scales its HUD with the height and centres the top bar).
2. Snapping (find_slots): the bar's panel and the gaps between portraits are flat, while a portrait
   (even a dark or dead one) has texture. Each side's five slots are shifted and scaled together until
   they hold the most texture and the gaps the least; then the rows are placed the same way. A few
   pixels or a few percent of error in the layout don't matter. (Border edges were tried first: real
   portraits are too busy inside, and some fade to black at their sides.)
3. Matching: each slot is shrunk to 32x18 and compared with every hero's portrait shrunk the same way,
   by normalised cross-correlation in grey (works for dead, greyed-out portraits too), plus a colour
   term for living ones. One matrix product scores all ten slots against all heroes. Both sides are
   blurred a little first, and a slot read unsurely is re-read a pixel or two around, so being a
   pixel off doesn't matter.
4. A hero is in the game once: slots are filled best-score-first.
5. Confidence needs a good score, a clear lead over the runner-up, and a bar that showed at all
   (texture in the slots, flat gaps), so a frame without the top bar reads as nothing.
6. Dead: the slot's colour has drained and it's darker than the hero's own portrait.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image

from . import hud
from .config import CACHE_DIR

Frame = Image.Image | np.ndarray  # the whole game window: a PIL image, or an RGB array (height, width, 3)
RectF = tuple[float, float, float, float]  # x, y, w, h in window pixels (floats: snapping is sub-pixel)
PortraitLoader = Callable[[str], "Image.Image | None"]  # npc name -> Valve's portrait, or None

SIDES = ("radiant", "dire")
HERO_PREFIX = "npc_dota_hero_"
PORTRAIT_DIR = CACHE_DIR / "art" / "hero"  # where doty.ui.art keeps Valve's portraits (key "hero/<name>")
HEROES_JSON = CACHE_DIR / "heroes.json"  # dotaconstants' hero list, as doty.knowledge caches it
LUMA = np.array([0.299, 0.587, 0.114], np.float32)

# Matching ---------------------------------------------------------------------------------------
THUMB = (32, 18)  # w, h: every slot and portrait is shrunk to this before comparing (16:9)
COLOR_THUMB = (16, 9)  # the colour term uses half that
COLOR_WEIGHT = 0.35  # share of the score from colour, for clearly coloured slots and heroes
CHROMA_FULL = 0.06  # rms chroma (0..1) at which colour gets its full weight; greyish heroes get less
GREY_SAT = 0.06  # a slot with less mean colour than this is greyed out (dead): use the masked grey score
RESPAWN_BOX = (0.25, 0.12, 0.75, 0.88)  # x0, y0, x1, y1 of a portrait: where the respawn timer is drawn
DEAD_SAT_RATIO = 0.45  # dead: slot colour below this share of the hero portrait's colour...
DEAD_LUM_RATIO = 0.85  # ...and its median brightness below this share of the portrait's
DEAD_LUM_RATIO_GREY = 0.70  # for a hero whose portrait has no colour to lose: brightness alone
EMPTY_STD = 6.0  # grey-level spread (0..255) below which a slot is empty (no player, or bar hidden)
SCORE_FLOOR = 0.40  # a best score at or below this gives no confidence (random textures reach ~0.6)
CONF_MARGIN = 0.20  # a lead this big over the runner-up counts as fully sure
RECHECK_CONF = 0.6  # slots read less surely than this are re-read a pixel or two around...
RECHECK_STEP = 0.015  # ...in steps of this share of a slot width (at least 1 px)

# Snapping (find_slots) ----------------------------------------------------------------------------
SNAP_SHIFT = 0.30  # each side may move up to this share of a slot width...
SNAP_SCALE = 0.08  # ...and grow or shrink by this much
SNAP_V_SHIFT = 0.35  # the rows may move up to this share of a slot height
SNAP_MIN_CONTRAST = 3.0  # slots must have this much more texture than the gaps (see BAR_CONTRAST), else keep the guess
SNAP_REG = 0.01  # a slight pull towards the guess, so ties don't wander
# Is there a top bar at all? Slot-vs-gap texture contrast: game-world texture alone reaches ~3 (rarely 4),
# a real bar of portraits 10 and up (about 4 at worst, in a JPEG with several dead). No confidence at or
# below the first, full confidence from the second; calibration only uses frames at the second or above.
BAR_CONTRAST = (3.0, 6.0)

# Calibration (search ranges, in HUD units) -------------------------------------------------------------
CAL_SLOT_W = (0.040, 0.080)
CAL_GAP = (0.0, 0.012)
CAL_CLOCK_GAP = (0.030, 0.160)
CAL_TOP = (0.0, 0.030)
CAL_ASPECT = (0.45, 0.75)  # slot height / width
CAL_CENTER_PX = 3.0  # the bar's centre may sit this many px (at 1080p) off the window's centre
CAL_FULL, CAL_FLOOR = 0.1, 1.0  # a column counts as portrait from 10% of the typical texture (>= 1 grey level)
LOSSLESS = {".png", ".bmp", ".tif", ".tiff"}
COMMON_HEIGHTS = (720, 768, 864, 900, 1024, 1050, 1080, 1200, 1440, 1600, 2160, 2880)

# Minimap ---------------------------------------------------------------------------------------------
MINIMAP_CALIBRATED = False  # the colour ranges and sizes below are guesses, not measured on real frames
MINIMAP_WORK_PX = 192  # the minimap crop is shrunk to this width before looking for markers
MARKER_MIN_SIZE = 0.022  # a hero marker's diameter, as a share of the minimap crop width (creeps are smaller)
MARKER_MAX_SIZE = 0.10  # (pings, glows and big icons are larger)
MARKER_MIN_FILL = 0.35  # share of its bounding box a marker fills (rules out lines and arcs)
MAX_PER_TEAM = 5


def unit(w: int, h: int) -> float:
    """The HUD unit in pixels: Dota scales its HUD with the window height on 16:9 and wider (ultrawide
    only widens the play area). Narrower windows (16:10, 4:3) are assumed to scale it by width."""
    return float(min(h, w * 9 / 16))


# Top bar geometry -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class TopBarLayout:
    """Where the ten portraits are, in HUD units (see unit()), measured from the top centre of the
    window. Radiant's five sit left of the clock and Dire's five right of it, each in player order
    left to right. The defaults are a best guess (about 62x35 px portraits at 1080p): replace them
    with tools/calibrate_hud.py's output once real frames are in."""

    slot_w: float = 0.0574  # portrait width
    slot_h: float = 0.0324  # portrait height (16:9, like Valve's portrait art)
    gap: float = 0.0037  # space between neighbouring portraits
    clock_gap: float = 0.0815  # window centre to the inner edge of the portraits next to the clock
    top: float = 0.0028  # window top to the top of the portraits
    center: float = 0.0  # the bar's centre minus the window's centre
    inset: float = 0.06  # share trimmed off each side of a slot (and of a portrait) before matching
    portrait_crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # part of Valve's art the bar shows

    def rects(self, w: int, h: int) -> list[RectF]:
        """The ten slot rectangles for a w x h window: Radiant 0-4, then Dire 0-4 (left to right)."""
        u = unit(w, h)
        cx = w / 2 + self.center * u
        sw, sh, cg, top = self.slot_w * u, self.slot_h * u, self.clock_gap * u, self.top * u
        p = sw + self.gap * u
        radiant = [(cx - cg - sw - (4 - k) * p, top, sw, sh) for k in range(5)]
        dire = [(cx + cg + k * p, top, sw, sh) for k in range(5)]
        return radiant + dire


DEFAULT_LAYOUT = TopBarLayout()


@dataclass(frozen=True)
class SlotFit:
    """Where find_slots put the ten slots, and how sure it was."""

    rects: list[RectF]  # Radiant 0-4, then Dire 0-4, window pixels
    snapped: bool  # False: the image didn't show the slots clearly, so these are the layout's guess
    contrast: tuple[float, float, float]  # texture in the slots vs the gaps (radiant, dire), and rows vs around
    shift: tuple[float, float]  # px each side moved (radiant, dire); + is to the right
    scale: tuple[float, float]  # how much each side grew (1.0 = as the layout says)
    dy: float  # px the rows moved; + is down
    found: tuple[float, float] = (0.0, 0.0)  # 0..1 per side (radiant, dire): how clearly slots showed at all


@dataclass(frozen=True)
class Slot:
    """One top-bar portrait, read."""

    side: str  # radiant | dire
    index: int  # 0-4, left to right (player order)
    hero: str  # npc name (npc_dota_hero_x); "" when the slot looks empty
    confidence: float  # 0..1: how good the best match is, how far it leads the runner-up, and whether a bar showed
    margin: float  # best score minus the runner-up's
    dead: bool  # greyed out
    score: float = 0.0  # the best match's score (about -1..1; 1 = identical)
    second: str = ""  # the runner-up, for logging
    rect: RectF = (0.0, 0.0, 0.0, 0.0)  # where it was read, window pixels


# Frames --------------------------------------------------------------------------------------------
def frame_from_bgra(width: int, height: int, data: bytes) -> np.ndarray:
    """An RGB view of the BGRA bytes capture.FrameGrabber.grab() returns, without copying the frame
    (only the parts the readers crop are copied)."""
    return np.frombuffer(data, np.uint8).reshape(height, width, 4)[:, :, 2::-1]


def _size(frame: Frame) -> tuple[int, int]:
    if isinstance(frame, Image.Image):
        return frame.size
    return int(frame.shape[1]), int(frame.shape[0])


def _crop_rgb(frame: Frame, box: tuple[int, int, int, int]) -> np.ndarray:
    """The box (x0, y0, x1, y1) of a frame as a contiguous RGB uint8 array. Only the box is converted,
    never the whole 3440x1440 frame."""
    if isinstance(frame, Image.Image):
        part = frame.crop(box)
        return np.asarray(part if part.mode == "RGB" else part.convert("RGB"))
    x0, y0, x1, y1 = box
    arr = np.asarray(frame)
    if arr.ndim == 2:
        arr = arr[:, :, None].repeat(3, axis=2)
    return np.ascontiguousarray(arr[y0:y1, x0:x1, :3], dtype=np.uint8)


def _gray(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float32) @ LUMA


# Snapping -----------------------------------------------------------------------------------------
def _band_box(w: int, h: int, layout: TopBarLayout) -> tuple[int, int, int, int]:
    """The strip of the window the top-bar reader looks at: the slots plus room to snap."""
    rects = layout.rects(w, h)
    sw, sh, top = rects[0][2], rects[0][3], rects[0][1]
    span = 4 * (sw + layout.gap * unit(w, h)) + sw
    mx = SNAP_SHIFT * sw + SNAP_SCALE * span + 4
    x0 = max(0, int(rects[0][0] - mx))
    x1 = min(w, int(math.ceil(rects[9][0] + sw + mx)))
    y1 = min(h, int(math.ceil(top + sh * (1 + SNAP_SCALE) + SNAP_V_SHIFT * sh + 4)))
    return x0, 0, max(x1, x0 + 1), max(y1, 1)


def find_slots(frame: Frame, layout: TopBarLayout = DEFAULT_LAYOUT, snap: bool = True) -> SlotFit:
    """The ten slot rectangles for this frame: the layout's guess, snapped to where the image shows the
    portraits (see the module notes). snap=False returns the guess as is."""
    w, h = _size(frame)
    box = _band_box(w, h, layout)
    return _fit_slots(_gray(_crop_rgb(frame, box)), box[0], box[1], w, h, layout, snap)


def _step(u: float) -> int:
    """The pixel step texture is measured over: Dota draws the same art bigger on bigger screens, which
    makes neighbouring pixels more alike, so at 4K compare pixels two apart (one at 1080p and 1440p)."""
    return max(1, int(round(u / 1080)))


def _column_texture(gray: np.ndarray, r0: float, r1: float, step: int = 1) -> np.ndarray:
    """How much each column changes down its rows (between band rows r0 and r1): about 0 on the flat
    panel and in the gaps between portraits, clearly more inside any portrait (even a dark one). Edges
    don't work as well: a busy portrait has edges all over, and some fade to black at their sides."""
    a = max(0, int(round(r0)))
    b = min(gray.shape[0], max(int(round(r1)), a + step + 1))
    return np.abs(gray[a + step:b] - gray[a:b - step]).mean(axis=0)


def _box_sum(csum: np.ndarray, x0: float, lefts: np.ndarray, rights: np.ndarray) -> np.ndarray:
    """A column profile summed over boxes [left, right) (window x, fractional). `csum` is its running
    sum with a leading 0; the profile's first column is window column x0."""
    xb = x0 + np.arange(csum.size, dtype=np.float64)
    return np.interp(rights, xb, csum) - np.interp(lefts, xb, csum)


def _texture_score(tex: np.ndarray, typical: float, flat: float, full: float = 0.5,
                   floor: float = 0.0) -> np.ndarray:
    """Per column, -1 (flat, like the panel) to +1 (portrait). A column counts as fully portrait from
    flat + `full` x (typical - flat) (at least flat + `floor`), so dark and dead portraits do; clipping
    keeps a few very busy columns (the clock's digits, the kill scores) from outweighing the gaps."""
    half = max(floor, full * max(typical - flat, 1e-3)) / 2
    return np.clip((tex - flat - half) / half, -1.0, 1.0)


def _pattern(sw, g, margin) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """One side's five slots as offsets from its inner edge outwards (Dire's direction), arrays broadcast
    together: (slot lefts, slot rights) (..., 5) and (gap lefts, gap rights) (..., 6): the four gaps
    between the slots plus a margin at each end, which should all be flat."""
    arrays = np.broadcast_arrays(*(np.asarray(v, np.float64) for v in (sw, g, margin)))
    sw, g, margin = (a[..., None] for a in arrays)
    lefts = np.arange(5) * (sw + g)
    rights = lefts + sw
    end = rights[..., 4:]
    gap_l = np.concatenate([lefts[..., 1:] - g, -margin, end], axis=-1)
    gap_r = np.concatenate([lefts[..., 1:], np.zeros_like(margin), end + margin], axis=-1)
    return lefts, rights, gap_l, gap_r


def _box_contrast(tex: np.ndarray, x0: float, boxes: Sequence[tuple[float, float]], margin: float) -> float:
    """Median texture inside the boxes over the median in the gaps between them (and a margin around).
    Medians, so a digit next to the bar doesn't sink it."""
    xs = x0 + np.arange(tex.size) + 0.5
    inside = np.zeros(tex.size, bool)
    for left, right in boxes:
        inside |= (xs >= left) & (xs < right)
    near = (xs >= min(b[0] for b in boxes) - margin) & (xs < max(b[1] for b in boxes) + margin) & ~inside
    if not inside.any():
        return 0.0
    return float(np.median(tex[inside])) / ((float(np.median(tex[near])) if near.any() else 0.0) + 1e-3)


def _fit_side(csum: np.ndarray, x0: float, inner: float, sign: int, pattern: tuple[np.ndarray, ...],
              reach: float, norm: float) -> tuple[float, float]:
    """The shift and scale of one side's slots (`pattern` from its inner edge; sign -1 mirrors it for
    Radiant) that best match texture in the slots and flat gaps between them (a matched filter): 1 px
    and 1% steps first, then a quarter pixel and 0.1% around the best."""
    a0, s0 = 0.0, 1.0
    grids = ((np.arange(-reach, reach + 1e-9, 1.0), np.arange(-SNAP_SCALE, SNAP_SCALE + 1e-9, 0.01)),
             (np.arange(-1.0, 1.0 + 1e-9, 0.25), np.arange(-0.01, 0.01 + 1e-9, 0.001)))
    for n, (da, ds) in enumerate(grids):
        shifts = a0 + da if n else da
        scales = s0 + ds if n else 1.0 + ds
        base = inner + shifts[:, None, None]
        score = 0.0
        for weight, (lo, hi) in ((1.0, pattern[:2]), (-1.0, pattern[2:])):
            lo, hi = (-hi, -lo) if sign < 0 else (lo, hi)
            left = base + scales[None, :, None] * lo
            right = base + scales[None, :, None] * hi
            sums = _box_sum(csum, x0, left.ravel(), right.ravel()).reshape(left.shape)
            score = score + weight * sums.sum(axis=2)
        score = score / norm
        pull = SNAP_REG * (np.abs(shifts)[:, None] / reach + np.abs(scales - 1)[None, :] / SNAP_SCALE)
        i, j = np.unravel_index(int(np.argmax(score - pull)), score.shape)
        a0, s0 = float(shifts[i]), float(scales[j])
    return a0, s0


def _fit_slots(gray: np.ndarray, ox: int, oy: int, w: int, h: int, layout: TopBarLayout,
               snap: bool) -> SlotFit:
    """find_slots on a grey band whose top-left is window pixel (ox, oy)."""
    u = unit(w, h)
    guess = layout.rects(w, h)
    sw, sh, top = guess[0][2], guess[0][3], guess[0][1]
    p = sw + layout.gap * u
    cx, cg = w / 2 + layout.center * u, layout.clock_gap * u

    # Columns: each side's five boxes go where the texture is, over rows that are surely inside the portraits.
    step = _step(u)
    tex = _column_texture(gray, top + 0.2 * sh - oy, top + 0.8 * sh - oy, step)
    xs = ox + np.arange(tex.size) + 0.5
    in_guess = np.zeros(tex.size, bool)
    for x, _, rw, _ in guess:
        in_guess |= (xs >= x) & (xs < x + rw)
    lo = float(np.percentile(tex, 5))
    hi = float(np.median(tex[in_guess])) if in_guess.any() else lo
    csum = np.concatenate([[0.0], np.cumsum(_texture_score(tex, hi, lo))])
    pattern = _pattern(sw, p - sw, 0.1 * sw)  # Dire's slots and gaps, from its inner edge; Radiant mirrors them
    dire_l, dire_r = pattern[0], pattern[1]
    reach = SNAP_SHIFT * sw
    fits = []
    for inner, sign in ((cx - cg, -1), (cx + cg, 1)):
        lefts, rights = (-dire_r, -dire_l) if sign < 0 else (dire_l, dire_r)
        a, s = 0.0, 1.0
        if snap and reach > 0 and hi > lo + 1e-3:
            a, s = _fit_side(csum, ox, inner, sign, pattern, reach, 5 * sw)
        boxes = list(zip(inner + a + s * lefts, inner + a + s * rights))
        contrast = _box_contrast(tex, ox, boxes, 0.15 * sw)
        fits.append((a, s, contrast) if contrast >= SNAP_MIN_CONTRAST else
                    (0.0, 1.0, _box_contrast(tex, ox, list(zip(inner + lefts, inner + rights)), 0.15 * sw)))
    (a_r, s_r, c_r), (a_d, s_d, c_d) = fits
    spans = [(cx - cg + a_r - s_r * (4 - j) * p - s_r * sw, s_r * sw) for j in range(5)]
    spans += [(cx + cg + a_d + s_d * j * p, s_d * sw) for j in range(5)]

    # Rows: the slots' rows are where they have texture (the bar under them and the panel around are flat).
    sh_fit = sh * (s_r + s_d) / 2
    new_top, c_v = top, 0.0
    cols = np.concatenate([np.arange(int(math.ceil(x + 0.15 * sw_)), int(x + 0.85 * sw_)) for x, sw_ in spans])
    cols = cols - ox
    cols = cols[(cols >= 0) & (cols < gray.shape[1] - step)]
    rows_h = max(2, int(round(sh_fit)))
    if cols.size and gray.shape[0] > rows_h:
        energy = np.abs(gray[:, cols + step] - gray[:, cols]).mean(axis=1)
        csum = np.concatenate([[0.0], np.cumsum(energy)])
        reach_v = SNAP_V_SHIFT * sh
        starts = np.arange(int(math.floor(top - oy - reach_v)), int(math.ceil(top - oy + reach_v)) + 1)
        first = np.clip(starts, 0, energy.size)
        end = np.clip(starts + rows_h, 0, energy.size)
        sums = csum[end] - csum[first]
        guess_start = int(round(top - oy))
        best = int(np.argmax(sums - 0.01 * sums.max() * np.abs(starts - guess_start) / max(reach_v, 1)))
        a, b = int(first[best]), int(end[best])
        inside = float(energy[a:b].mean()) if b > a else 0.0
        outside_rows = np.concatenate([energy[:a], energy[b:]])
        outside = float(outside_rows.mean()) if outside_rows.size else 0.0
        c_v = inside / (outside + 1e-6)
        if snap and c_v >= SNAP_MIN_CONTRAST:
            new_top = float(a + oy)
    rects = [(x, new_top, sw_, sh_fit) for x, sw_ in spans]
    snapped = bool(snap and (a_r, s_r, a_d, s_d, new_top) != (0.0, 1.0, 0.0, 1.0, top))
    lo_c, hi_c = BAR_CONTRAST
    found = tuple(float(np.clip((c - lo_c) / (hi_c - lo_c), 0.0, 1.0)) for c in (c_r, c_d))
    return SlotFit(rects=rects, snapped=snapped, contrast=(c_r, c_d, c_v), shift=(a_r, a_d),
                   scale=(s_r, s_d), dy=new_top - top, found=found)


# Thumbnails and features ------------------------------------------------------------------------------
def _keep_mask() -> np.ndarray:
    """The 32x18 pixels outside the respawn-timer box, flattened."""
    tw, th = THUMB
    xs = (np.arange(tw) + 0.5) / tw
    ys = (np.arange(th) + 0.5) / th
    x0, y0, x1, y1 = RESPAWN_BOX
    inside = ((ys[:, None] >= y0) & (ys[:, None] <= y1)) & ((xs[None, :] >= x0) & (xs[None, :] <= x1))
    return ~inside.ravel()


_KEEP = _keep_mask()


def _unit_rows(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)


def _centered(v: np.ndarray) -> np.ndarray:
    return _unit_rows(v - v.mean(axis=1, keepdims=True))


@dataclass(frozen=True)
class _Features:
    gray: np.ndarray  # (n, 576) zero-mean unit vectors: dot product = normalised cross-correlation
    masked: np.ndarray  # (n, k) the same without the respawn-timer box
    chroma: np.ndarray  # (n, 288) unit vectors of the colour (red and blue minus brightness)
    chroma_rms: np.ndarray  # (n,) how colourful, 0..1
    sat: np.ndarray  # (n,) mean colourfulness (max - min channel), 0..1
    lum: np.ndarray  # (n,) median brightness outside the respawn-timer box (its digits are white), 0..1
    std: np.ndarray  # (n,) grey spread, 0..255


def _blur(x: np.ndarray) -> np.ndarray:
    """A light [1, 2, 1] blur over the rows and columns of (n, h, w, c) thumbnails: it makes a match
    forgiving of a slot read a pixel or two off, at little cost in telling heroes apart."""
    for axis in (1, 2):
        pad = np.concatenate([x.take([0], axis), x, x.take([-1], axis)], axis=axis)
        n = x.shape[axis]
        x = (pad.take(range(0, n), axis) + 2 * pad.take(range(1, n + 1), axis) + pad.take(range(2, n + 2), axis)) / 4
    return x


def _features(thumbs: np.ndarray) -> _Features:
    """Features of (n, 18, 32, 3) uint8 thumbnails (slots or portraits; both go through here)."""
    n = thumbs.shape[0]
    x = _blur(thumbs.astype(np.float32) / 255.0)
    g = (x @ LUMA).reshape(n, -1)
    cw, ch = COLOR_THUMB
    c = x.reshape(n, ch, THUMB[1] // ch, cw, THUMB[0] // cw, 3).mean(axis=(2, 4))
    y = c @ LUMA
    chroma = np.concatenate([(c[..., 0] - y).reshape(n, -1), (c[..., 2] - y).reshape(n, -1)], axis=1)
    return _Features(gray=_centered(g), masked=_centered(g[:, _KEEP]), chroma=_unit_rows(chroma),
                     chroma_rms=np.sqrt((chroma ** 2).mean(axis=1)),
                     sat=(x.max(axis=-1) - x.min(axis=-1)).reshape(n, -1).mean(axis=1),
                     lum=np.median(g[:, _KEEP], axis=1), std=g.std(axis=1) * 255.0)


def _portrait_thumb(img: Image.Image, layout: TopBarLayout) -> np.ndarray:
    """Valve's portrait, cropped as the top bar shows it and shrunk like a slot."""
    if img.mode != "RGB":
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba).convert("RGB")
    w, h = img.size
    cx0, cy0, cx1, cy1 = layout.portrait_crop
    x0, y0, x1, y1 = cx0 * w, cy0 * h, cx1 * w, cy1 * h
    i = layout.inset
    box = (x0 + i * (x1 - x0), y0 + i * (y1 - y0), x1 - i * (x1 - x0), y1 - i * (y1 - y0))
    return np.asarray(img.resize(THUMB, Image.BOX, box=box))


def _slot_thumbs(band: Image.Image, ox: int, oy: int, rects: Sequence[RectF], inset: float) -> np.ndarray:
    out = np.empty((len(rects), THUMB[1], THUMB[0], 3), np.uint8)
    bw, bh = band.size
    for n, (x, y, w, h) in enumerate(rects):
        x0 = min(max(x + inset * w - ox, 0.0), bw - 1.0)
        y0 = min(max(y + inset * h - oy, 0.0), bh - 1.0)
        x1 = min(max(x + (1 - inset) * w - ox, x0 + 1.0), float(bw))
        y1 = min(max(y + (1 - inset) * h - oy, y0 + 1.0), float(bh))
        out[n] = np.asarray(band.resize(THUMB, Image.BOX, box=(x0, y0, x1, y1)))
    return out


# Portraits -------------------------------------------------------------------------------------------
def cached_portrait(npc: str, folder: Path | None = None) -> Image.Image | None:
    """Valve's portrait for a hero from Doty's art cache (doty.ui.art downloads and keeps them there,
    as "hero/<name>.png"), or None if it isn't cached yet. Never downloads anything itself."""
    file = (folder or PORTRAIT_DIR) / f"{npc.removeprefix(HERO_PREFIX)}.png"
    if not file.is_file():
        return None
    try:
        with Image.open(file) as img:
            img.load()
            return img.copy()
    except OSError:
        return None


def known_heroes(path: Path | None = None) -> list[str]:
    """Every hero's npc name, from the dotaconstants hero list doty.knowledge caches (empty if it
    hasn't been fetched yet)."""
    try:
        data = json.loads((path or HEROES_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.values() if isinstance(data, dict) else data if isinstance(data, list) else []
    return sorted({h["name"] for h in rows if isinstance(h, dict) and str(h.get("name", "")).startswith(HERO_PREFIX)})


def load_portraits(heroes: Iterable[str] | None = None,
                   loader: PortraitLoader = cached_portrait) -> dict[str, Image.Image]:
    """{npc name: portrait} for the heroes whose portraits the loader has (default: all known heroes,
    from the art cache)."""
    out = {}
    for npc in (known_heroes() if heroes is None else heroes):
        img = loader(npc)
        if img is not None:
            out[npc] = img
    return out


# The top-bar reader -------------------------------------------------------------------------------------
class TopBarReader:
    """Reads the ten top-bar portraits: which hero is in each slot, and whether it's dead.

    `portraits` is {npc name: Valve portrait} or a loader (npc name -> portrait or None) used for each
    name in `heroes` (default: every known hero). The portraits are shrunk once, here; each read is
    then a few milliseconds. Not thread-safe: use one reader per thread."""

    def __init__(self, portraits: Mapping[str, Image.Image] | PortraitLoader,
                 layout: TopBarLayout = DEFAULT_LAYOUT, heroes: Iterable[str] | None = None) -> None:
        self.layout = layout
        if isinstance(portraits, Mapping):
            pairs = list(portraits.items())
            self.missing: list[str] = []
        else:
            names = list(known_heroes() if heroes is None else heroes)
            loaded = [(npc, portraits(npc)) for npc in names]
            pairs = [(npc, img) for npc, img in loaded if img is not None]
            self.missing = [npc for npc, img in loaded if img is None]
        if not pairs:
            raise ValueError("TopBarReader needs at least one hero portrait")
        self.heroes = [npc for npc, _ in pairs]
        self._index = {npc: i for i, npc in enumerate(self.heroes)}
        self._t = _features(np.stack([_portrait_thumb(img, layout) for _, img in pairs]))
        self._subsets: dict[frozenset[str], np.ndarray] = {}
        self.last_fit: SlotFit | None = None
        self.last_ms = 0.0

    @classmethod
    def from_cache(cls, layout: TopBarLayout = DEFAULT_LAYOUT, heroes: Iterable[str] | None = None) -> TopBarReader:
        """A reader over the portraits already in Doty's art cache."""
        return cls(cached_portrait, layout, heroes)

    def find_slots(self, frame: Frame, snap: bool = True) -> SlotFit:
        return find_slots(frame, self.layout, snap)

    def read(self, frame: Frame, candidates: Iterable[str] | None = None, snap: bool = True,
             unique: bool = True) -> list[Slot]:
        """The ten slots of this frame (Radiant 0-4, then Dire 0-4). `candidates` limits the heroes
        considered (for example the ten already read with confidence: faster and steadier);
        `unique` lets each hero fill one slot only."""
        w, h = _size(frame)
        box = _band_box(w, h, self.layout)
        return self.read_band(_crop_rgb(frame, box), box, (w, h), candidates, snap, unique)

    def band_box(self, w: int, h: int) -> tuple[int, int, int, int]:
        """The strip of a w x h game window this reader looks at (x0, y0, x1, y1): all that needs grabbing."""
        return _band_box(w, h, self.layout)

    def read_band(self, band: np.ndarray, box: tuple[int, int, int, int], size: tuple[int, int],
                  candidates: Iterable[str] | None = None, snap: bool = True, unique: bool = True) -> list[Slot]:
        """`read` from just the top strip: `band` is the RGB pixels of `box` (from band_box) in a window of
        `size`. What the live reader uses, so it never grabs the whole frame."""
        t0 = time.perf_counter()
        w, h = size
        fit = _fit_slots(_gray(band), box[0], box[1], w, h, self.layout, snap)
        image = Image.fromarray(band)
        cols = self._columns(candidates)
        thumbs = _slot_thumbs(image, box[0], box[1], fit.rects, self.layout.inset)
        slots = self._match(_features(thumbs), fit.rects, cols, unique, fit.found)
        unsure = [i for i, s in enumerate(slots)
                  if s.hero and s.confidence < RECHECK_CONF and fit.found[i // 5] > 0]  # (no bar: nothing to fix)
        if unsure:  # look a pixel or two around those slots, and keep the spot that matches best
            rects = list(fit.rects)
            for i in unsure:
                x, y, rw, rh = rects[i]
                d = max(1.0, RECHECK_STEP * rw)
                tries = [(x + dx * d, y + dy * d, rw, rh) for dx in (-2, -1, 0, 1, 2) for dy in (-1, 0, 1)]
                near = _slot_thumbs(image, box[0], box[1], tries, self.layout.inset)
                k = int(np.argmax(self._scores(_features(near), cols).max(axis=1)))
                thumbs[i], rects[i] = near[k], tries[k]
            slots = self._match(_features(thumbs), rects, cols, unique, fit.found)
        self.last_fit = fit
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return slots

    def _columns(self, candidates: Iterable[str] | None) -> np.ndarray:
        if candidates is None:
            return np.arange(len(self.heroes))
        key = frozenset(candidates)
        cols = self._subsets.get(key)
        if cols is None:
            cols = np.array(sorted(self._index[n] for n in key if n in self._index), dtype=np.int64)
            if cols.size == 0:
                cols = np.arange(len(self.heroes))
            self._subsets[key] = cols
        return cols

    def _scores(self, s: _Features, cols: np.ndarray) -> np.ndarray:
        """(slots, heroes in cols): grey correlation, plus the colour term for coloured slots and heroes."""
        t = self._t
        tg, tm, tc, trms = t.gray[cols], t.masked[cols], t.chroma[cols], t.chroma_rms[cols]
        grey = s.gray @ tg.T  # normalised cross-correlation in grey
        greyed = s.sat < GREY_SAT
        if greyed.any():  # dead: the respawn timer covers the middle, so also try without it
            grey[greyed] = np.maximum(grey[greyed], s.masked[greyed] @ tm.T)
        weight = COLOR_WEIGHT * np.clip(np.minimum(s.chroma_rms[:, None], trms[None, :]) / CHROMA_FULL, 0.0, 1.0)
        return (1.0 - weight) * grey + weight * (s.chroma @ tc.T)

    def _match(self, s: _Features, rects: Sequence[RectF], cols: np.ndarray, unique: bool,
               found: tuple[float, float]) -> list[Slot]:
        t = self._t
        score = self._scores(s, cols)
        empty = s.std < EMPTY_STD
        pick = _assign(score, ~empty, unique)
        out = []
        for i, rect in enumerate(rects):
            side, index = SIDES[i // 5], i % 5
            j = int(pick[i])
            if empty[i] or j < 0:
                out.append(Slot(side, index, "", 0.0, 0.0, False, rect=rect))
                continue
            row = score[i].copy()
            taken = pick[(pick >= 0) & (np.arange(len(pick)) != i)]
            row[taken[taken != j]] = -np.inf  # the runner-up among heroes not placed elsewhere
            row[j] = -np.inf
            k = int(np.argmax(row)) if np.isfinite(row).any() else -1
            best = float(score[i, j])
            second = float(row[k]) if k >= 0 else 0.0
            margin = best - second
            conf = float(np.clip((best - SCORE_FLOOR) / (1.0 - SCORE_FLOOR), 0.0, 1.0) *
                         np.clip(margin / CONF_MARGIN, 0.0, 1.0) * found[i // 5])
            tj = int(cols[j])
            out.append(Slot(side, index, self.heroes[tj], round(conf, 3), round(margin, 3),
                            _is_dead(s, i, t, tj), round(best, 3),
                            self.heroes[int(cols[k])] if k >= 0 else "", rect))
        return out


def _assign(score: np.ndarray, valid: np.ndarray, unique: bool) -> np.ndarray:
    """The column each row (slot) takes: best first, each column once when `unique` (rows left over
    once the columns run out take their best anyway). -1 for invalid rows."""
    rows, cols = score.shape
    pick = np.full(rows, -1, np.int64)
    if not unique:
        pick[valid] = np.argmax(score[valid], axis=1)
        return pick
    want = int(valid.sum())
    used: set[int] = set()
    for flat in np.argsort(-score, axis=None):
        i, j = divmod(int(flat), cols)
        if not valid[i] or pick[i] >= 0 or j in used:
            continue
        pick[i] = j
        used.add(j)
        want -= 1
        if want == 0 or len(used) == cols:
            break
    left = valid & (pick < 0)
    if left.any():
        pick[left] = np.argmax(score[left], axis=1)
    return pick


def _is_dead(s: _Features, i: int, t: _Features, j: int) -> bool:
    """Greyed out: the colour has drained and the slot is darker than the hero's own portrait."""
    lum_ratio = float(s.lum[i]) / max(float(t.lum[j]), 0.02)
    if t.sat[j] < GREY_SAT:  # no colour to lose (no current hero; the least colourful is ~0.08): go by brightness
        return lum_ratio < DEAD_LUM_RATIO_GREY
    return float(s.sat[i]) / float(t.sat[j]) < DEAD_SAT_RATIO and lum_ratio < DEAD_LUM_RATIO


def teams(slots: Iterable[Slot], min_confidence: float = 0.5) -> dict[str, list[str]]:
    """{"radiant": [...], "dire": [...]}: the heroes read with at least this confidence, in slot order.
    The shape lineup.Lineup.add takes."""
    out: dict[str, list[str]] = {side: [] for side in SIDES}
    for s in slots:
        if s.hero and s.confidence >= min_confidence:
            out[s.side].append(s.hero)
    return out


# Minimap ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ColorRange:
    """An HSV colour range: hue in degrees (lo > hi wraps through 0, for reds), minimum saturation and
    value (0..1)."""

    hue: tuple[float, float]
    sat: float = 0.5
    val: float = 0.45

    def matches(self, hue: np.ndarray, sat: np.ndarray, val: np.ndarray) -> np.ndarray:
        lo, hi = self.hue
        in_hue = (hue >= lo) & (hue <= hi) if lo <= hi else (hue >= lo) | (hue <= hi)
        return in_hue & (sat >= self.sat) & (val >= self.val)


# Guesses (MINIMAP_CALIBRATED is False): Radiant's markers green, Dire's red. With Dota's "ally/enemy
# colours" option these are allies and enemies instead: pass {"ally": ..., "enemy": ...}.
TEAM_COLORS: dict[str, ColorRange] = {
    "radiant": ColorRange((80.0, 165.0), sat=0.45, val=0.45),
    "dire": ColorRange((345.0, 20.0), sat=0.55, val=0.45),
}


@dataclass(frozen=True)
class Marker:
    """A hero-sized blob of a team colour on the minimap."""

    x: float  # centre, as a share of the minimap crop (0..1 from its top-left), like marks.Mark
    y: float
    team: str  # a key of the reader's team colours: radiant | dire by default
    size: float  # diameter of a circle of the same area, as a share of the crop width
    confidence: float  # 0..1: how marker-like (size, compactness, colour purity). Uncalibrated
    pixels: int  # its area at the reader's working resolution


def _hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hue (degrees), saturation and value (0..1) of an RGB uint8 image."""
    x = rgb.astype(np.float32) / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx, mn = x.max(axis=-1), x.min(axis=-1)
    c = mx - mn
    cz = np.maximum(c, 1e-6)
    hue = np.where(mx == r, ((g - b) / cz) % 6.0, np.where(mx == g, (b - r) / cz + 2.0, (r - g) / cz + 4.0)) * 60.0
    hue = np.where(c > 1e-6, hue, 0.0)
    return hue, np.where(mx > 1e-6, c / np.maximum(mx, 1e-6), 0.0), mx


def _components(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """4-connected blobs of a boolean mask, without scipy: (ys, xs, label) of its pixels, labels 0..n-1.
    Labels spread to neighbours and then jump along chains (pointer jumping), which settles in a few
    rounds for blobs the size of map markers."""
    ys, xs = np.nonzero(mask)
    n = ys.size
    if n == 0:
        return ys, xs, np.zeros(0, np.int64)
    idx = np.full(mask.shape, -1, np.int64)
    idx[ys, xs] = np.arange(n)
    right = mask[:, :-1] & mask[:, 1:]
    down = mask[:-1, :] & mask[1:, :]
    pairs = [(idx[:, :-1][right], idx[:, 1:][right]), (idx[:-1, :][down], idx[1:, :][down])]
    lab = np.arange(n)
    for _ in range(4 * max(mask.shape)):
        new = lab.copy()
        for a, b in pairs:  # each index appears at most once per list, so plain assignment is safe
            new[a] = np.minimum(new[a], lab[b])
            new[b] = np.minimum(new[b], lab[a])
        new = new[new[new]]
        if np.array_equal(new, lab):
            break
        lab = new
    return ys, xs, np.unique(lab, return_inverse=True)[1].ravel()


class MinimapReader:
    """Finds hero-sized blobs of each team's colour on the minimap (hud.minimap_rect). Conservative and
    UNCALIBRATED (see MINIMAP_CALIBRATED): the colour ranges and sizes are guesses until checked on real
    frames, and two heroes standing together make one blob."""

    calibrated = MINIMAP_CALIBRATED

    def __init__(self, team_colors: Mapping[str, ColorRange] | None = None, work_px: int = MINIMAP_WORK_PX,
                 min_size: float = MARKER_MIN_SIZE, max_size: float = MARKER_MAX_SIZE,
                 min_fill: float = MARKER_MIN_FILL) -> None:
        self.team_colors = dict(team_colors or TEAM_COLORS)
        self.work_px, self.min_size, self.max_size, self.min_fill = work_px, min_size, max_size, min_fill
        self.last: list[Marker] = []
        self.last_ms = 0.0

    def read(self, frame: Frame, minimap_side: str = "left") -> list[Marker]:
        """Markers on this frame's minimap (bottom-left, or bottom-right with Dota's option)."""
        w, h = _size(frame)
        x, y, s, _ = hud.minimap_rect(w, h, minimap_side)
        return self.read_crop(_crop_rgb(frame, (x, y, x + s, y + s)))

    def read_crop(self, crop: Image.Image | np.ndarray) -> list[Marker]:
        """Markers on a minimap crop (as capture and match_capture save it)."""
        t0 = time.perf_counter()
        if not isinstance(crop, Image.Image):
            crop = Image.fromarray(np.ascontiguousarray(np.asarray(crop)[..., :3]))
        img = crop.convert("RGB")
        if img.width != self.work_px:
            img = img.resize((self.work_px, max(1, round(img.height * self.work_px / img.width))), Image.BOX)
        hue, sat, val = _hsv(np.asarray(img))
        markers: list[Marker] = []
        for team, colors in self.team_colors.items():
            markers += self._blobs(colors.matches(hue, sat, val), sat, team, img.size)
        markers.sort(key=lambda m: -m.confidence)
        self.last = markers
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        return markers

    def count(self, team: str, markers: Sequence[Marker] | None = None) -> int:
        """How many markers of a team (the last read's, unless given), at most five."""
        return min(MAX_PER_TEAM, sum(1 for m in (self.last if markers is None else markers) if m.team == team))

    def _blobs(self, mask: np.ndarray, sat: np.ndarray, team: str, size: tuple[int, int]) -> list[Marker]:
        ys, xs, lab = _components(mask)
        if lab.size == 0:
            return []
        n = int(lab.max()) + 1
        area = np.bincount(lab, minlength=n).astype(np.float64)
        cx = np.bincount(lab, xs + 0.5, n) / area
        cy = np.bincount(lab, ys + 0.5, n) / area
        purity = np.bincount(lab, sat[ys, xs], n) / area
        x0 = np.full(n, np.inf)
        x1 = np.full(n, -np.inf)
        y0 = np.full(n, np.inf)
        y1 = np.full(n, -np.inf)
        np.minimum.at(x0, lab, xs)
        np.maximum.at(x1, lab, xs)
        np.minimum.at(y0, lab, ys)
        np.maximum.at(y1, lab, ys)
        fill = area / ((x1 - x0 + 1) * (y1 - y0 + 1))
        w, h = size
        diameter = 2.0 * np.sqrt(area / np.pi) / w
        out = []
        lo, hi = self.min_size, self.max_size
        for i in np.flatnonzero((diameter >= lo) & (diameter <= hi) & (fill >= self.min_fill)):
            d = float(diameter[i])
            fits = 1.0 if lo * 1.25 <= d <= hi / 1.25 else 0.6
            conf = min(1.0, float(fill[i]) / 0.7) * fits * (0.5 + 0.5 * float(purity[i]))
            out.append(Marker(round(float(cx[i]) / w, 4), round(float(cy[i]) / h, 4), team, round(d, 4),
                              round(conf, 3), int(area[i])))
        return out


# Calibration -------------------------------------------------------------------------------------------
def _guess_height(topbar_h: int) -> float:
    """The window height a top-bar crop (hud.topbar_rect) was cut from."""
    for h in COMMON_HEIGHTS:
        if int(h * hud.TOPBAR_H_FRAC) == topbar_h:
            return float(h)
    return topbar_h / hud.TOPBAR_H_FRAC


def _symmetric_boxes(cx: float, sw, g, cg, c) -> tuple[np.ndarray, ...]:
    """Symmetric layouts (arrays broadcast together) as window x edges: (slot lefts, slot rights)
    (..., 10) and (gap lefts, gap rights) (..., 12), the gaps and end margins that should be flat."""
    sw, g, cg, c = (np.asarray(v, dtype=np.float64) for v in (sw, g, cg, c))
    lefts, rights, gap_l, gap_r = _pattern(sw, g, 0.1 * sw)
    centre = (cx + c)[..., None]
    out = []
    for lo, hi in ((lefts, rights), (gap_l, gap_r)):
        lo, hi = lo + cg[..., None], hi + cg[..., None]
        out += [np.concatenate([centre - hi, centre + lo], axis=-1),
                np.concatenate([centre - lo, centre + hi], axis=-1)]
    return tuple(out)


def _cal_columns(tex: np.ndarray, x0: int, u: float, cx: float,
                 around: tuple[float, float, float, float] | None = None) -> tuple[float, float, float, float, float]:
    """The symmetric layout (slot width, gap, clock gap, centre offset; px) that best matches texture in
    its ten slots and flat gaps between them, and its contrast (texture in the slots vs the gaps)."""
    xs = x0 + np.arange(tex.size) + 0.5
    mid = tex[np.abs(xs - cx) < 0.45 * u]
    lo = float(np.percentile(tex, 5))
    hi = float(np.percentile(mid, 60)) if mid.size else lo
    # Sharper than when reading: many portraits fade to dark, nearly flat sides, and with the slot width
    # and gap both free those would pass for gaps. A UI gap is exactly flat in a PNG; a dark side isn't.
    csum = np.concatenate([[0.0], np.cumsum(_texture_score(tex, hi, lo, CAL_FULL, CAL_FLOOR))])

    def score(sw, g, cg, c) -> np.ndarray:  # agreement per column covered, so a bigger pattern gains nothing
        lefts, rights, gap_l, gap_r = _symmetric_boxes(cx, sw, g, cg, c)
        agree = (_box_sum(csum, x0, lefts.ravel(), rights.ravel()).reshape(lefts.shape).sum(axis=-1) -
                 _box_sum(csum, x0, gap_l.ravel(), gap_r.ravel()).reshape(gap_l.shape).sum(axis=-1))
        return agree / ((rights - lefts).sum(axis=-1) + (gap_r - gap_l).sum(axis=-1))

    starts = [around] if around else []
    px = max(1.0, u / 1080)  # coarse steps: 1 px at 1080p, 2 px at 4K
    if around is None:  # coarse: the whole range
        g, cg = np.meshgrid(np.arange(CAL_GAP[0] * u, CAL_GAP[1] * u + 1e-9, px),
                            np.arange(CAL_CLOCK_GAP[0] * u, CAL_CLOCK_GAP[1] * u + 1e-9, px), indexing="ij")
        best = []
        for sw in np.arange(CAL_SLOT_W[0] * u, CAL_SLOT_W[1] * u + 1e-9, px):
            sc = score(np.full(g.shape, sw), g, cg, np.zeros(g.shape))
            i = np.unravel_index(int(np.argmax(sc)), sc.shape)
            best.append((float(sc[i]), (float(sw), float(g[i]), float(cg[i]), 0.0)))
        best.sort(key=lambda b: -b[0])
        starts = [b[1] for b in best[:5]]
    top = (-np.inf, (0.0, 0.0, 0.0, 0.0))
    d = np.arange(-1.5, 1.5 + 1e-9, 0.25) * px
    cs = np.arange(-CAL_CENTER_PX, CAL_CENTER_PX + 1e-9, 0.5) * px
    for sw0, g0, cg0, _ in starts:  # fine: quarter steps around each coarse pick, and the centre
        g, cg, c = np.meshgrid(np.maximum(g0 + d, 0.0), cg0 + d, cs, indexing="ij")
        for sw in sw0 + d:
            sc = score(np.full(g.shape, sw), g, cg, c)
            i = np.unravel_index(int(np.argmax(sc)), sc.shape)
            if sc[i] > top[0]:
                top = (float(sc[i]), (float(sw), float(g[i]), float(cg[i]), float(c[i])))
    sw, g, cg, c = top[1]
    lefts, rights, _, _ = _symmetric_boxes(cx, sw, g, cg, c)
    return sw, g, cg, c, _box_contrast(tex, x0, list(zip(lefts, rights)), 0.15 * sw)


def _spans(cx: float, sw: float, g: float, cg: float, c: float) -> list[tuple[float, float]]:
    p = sw + g
    return ([(cx + c - cg - (4 - j) * p - sw, sw) for j in range(5)] +
            [(cx + c + cg + j * p, sw) for j in range(5)])


def _cal_rows(gray: np.ndarray, ox: int, u: float, spans: list[tuple[float, float]]) -> tuple[float, float]:
    """The slots' top and height (px): the run of rows where the slots have texture."""
    cols = np.concatenate([np.arange(int(math.ceil(x + 0.15 * w)), int(x + 0.85 * w)) for x, w in spans]) - ox
    step = _step(u)
    cols = cols[(cols >= 0) & (cols < gray.shape[1] - step)]
    energy = np.abs(gray[:, cols + step] - gray[:, cols]).mean(axis=1)
    sw = spans[0][1]
    inside = float(np.median(energy[int(0.008 * u):max(int(0.024 * u), int(0.008 * u) + 1)]))
    theta = (inside + float(np.percentile(energy, 10))) / 2
    csum = np.concatenate([[0.0], np.cumsum(energy - theta)])
    tops = np.arange(int(CAL_TOP[0] * u), int(CAL_TOP[1] * u) + 1)
    heights = np.arange(int(CAL_ASPECT[0] * sw), int(CAL_ASPECT[1] * sw) + 1)
    t, hh = np.meshgrid(tops, heights, indexing="ij")
    end = np.minimum(t + hh, energy.size)
    run = csum[end] - csum[np.minimum(t, energy.size)]
    i = np.unravel_index(int(np.argmax(run)), run.shape)
    return float(t[i]), float(end[i] - t[i])


def _calibrate_image(img: Image.Image, u: float, cx: float) -> dict:
    w, h = img.size
    x0 = max(0, int(cx - 0.8 * u))
    x1 = min(w, int(math.ceil(cx + 0.8 * u)))
    gray = _gray(np.asarray(img.crop((x0, 0, x1, min(h, int(math.ceil(0.10 * u)) + 1)))))
    tex = _column_texture(gray, 0.008 * u, 0.024 * u, _step(u))  # rows inside any plausible slot
    sw, g, cg, c, contrast = _cal_columns(tex, x0, u, cx)
    top, sh = _cal_rows(gray, x0, u, _spans(cx, sw, g, cg, c))
    tex = _column_texture(gray, top + 0.2 * sh, top + 0.8 * sh, _step(u))  # again, over the rows just found
    sw, g, cg, c, contrast = _cal_columns(tex, x0, u, cx, around=(sw, g, cg, c))
    top, sh = _cal_rows(gray, x0, u, _spans(cx, sw, g, cg, c))
    return {"slot_w": sw / u, "slot_h": sh / u, "gap": g / u, "clock_gap": cg / u, "top": top / u,
            "center": c / u, "contrast": round(contrast, 2), "unit_px": u,
            "rects": [(round(x, 1), top, round(sw_, 1), sh) for x, sw_ in _spans(cx, sw, g, cg, c)]}


def calibrate_from_frames(paths: Iterable[str | Path], window_height: int | None = None,
                          min_contrast: float = BAR_CONTRAST[1]) -> dict:
    """Measure the top bar's layout on saved frames of the Dota window (full frames, or the top-bar
    crops match_capture saves), so the real geometry can replace DEFAULT_LAYOUT.

    Each frame is searched for the five-and-five pattern of slots that holds the most texture; the
    result is the median over the frames that showed one. `window_height` is the game's height in px,
    for top-bar crops (otherwise guessed from the crop's height). Frames whose slots stand out less than
    `min_contrast` (texture in the slots vs the gaps) are left out: the bar may not have been on screen.
    JPEG frames are only used when no PNG is given, and then "reliable" is False: compression smears
    the flat gaps the search keys on (match_capture saves the top bar as PNG for this).
    Returns the layout fields (HUD units)
    plus "layout" (a TopBarLayout(...) line to paste), "frames", "used", "reliable" and "per_frame" (with each
    frame's slot rectangles in its own pixels, to check by eye)."""
    per_frame = []
    for path in paths:
        try:
            with Image.open(path) as im:
                img = im.convert("RGB")
        except OSError as exc:
            per_frame.append({"file": str(path), "ok": False, "error": str(exc)})
            continue
        w, h = img.size
        if w / h > 6:  # a top-bar crop: centred on the window, from its top edge
            kind, u = "topbar", float(window_height or _guess_height(h))
        else:
            kind, u = "frame", unit(w, h)
        est = _calibrate_image(img, u, w / 2)
        per_frame.append({"file": str(path), "kind": kind, "size": (w, h), **est,
                          "lossless": Path(path).suffix.lower() in LOSSLESS, "ok": est["contrast"] >= min_contrast})
    used = [f for f in per_frame if f.get("ok")]
    lossless = any(f["lossless"] for f in used)
    if lossless:  # JPEG smears the flat gaps the search keys on: PNGs win
        used = [f for f in used if f["lossless"]]
    keys = ("slot_w", "slot_h", "gap", "clock_gap", "top", "center")
    result: dict = {"frames": len(per_frame), "used": len(used), "reliable": bool(used) and lossless,
                    "per_frame": per_frame}
    if used:
        fields = {k: round(float(np.median([f[k] for f in used])), 5) for k in keys}
        result.update(fields)
        result["spread"] = {k: round(float(np.ptp([f[k] for f in used])), 5) for k in keys}
        result["contrast"] = round(float(np.median([f["contrast"] for f in used])), 2)
        layout = replace(DEFAULT_LAYOUT, **fields)
        result["layout"] = "TopBarLayout(" + ", ".join(f"{k}={getattr(layout, k)}" for k in keys) + ")"
    return result
