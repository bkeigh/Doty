"""Marks: the circles, arrows and boxes the coach draws over the game to show you things.

The AI says WHAT to point at; Doty knows WHERE it is. So the coach names things, and code places them:

    DRAW: circle radiant_bot_t1 "defend"           a fixed place on the map (knowledge/mapgeo.py), on the minimap
    DRAW: arrow me radiant_bot_t1 "back to tower"  from your own hero (the game data's position) to a place
    DRAW: box hud item_slot_3 "use this"           a fixed part of Dota's HUD (hud.anchor)

Pixel positions on the images it was given still work, as a fallback:

    DRAW: circle minimap 312 140 "3 missing" enemy
    DRAW: arrow screen 640 300 980 180 "back to tower" danger

Doty turns pixels into fractions of that image (the whole game window, or the minimap crop), and the overlay
maps them back onto the real window at whatever size it is. A minimap point that names a team (enemy, ally,
radiant, dire) snaps onto the nearest hero marker of that team Doty itself sees there, when it has fresh
ones. A name Doty doesn't know drops the mark: it is never guessed. Marks are only ever placed on things the
player can see or fixed public map knowledge (fair play).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Iterable

from . import hud
from .knowledge import mapgeo

SHAPES = ("circle", "arrow", "box", "point")
SPACES = ("screen", "minimap", "hud")
SPACE_WORDS = {"screen": "screen", "minimap": "minimap", "map": "minimap", "hud": "hud"}
TONES = ("coach", "danger", "good", "info")
TEAM_WORDS = ("enemy", "ally", "radiant", "dire")
ME = "me"
MAX_MARKS = 3
SNAP_RADIUS = 0.05  # a minimap point within this share of the crop of a seen hero marker lands on it
MARKERS_FRESH_S = 1.5  # markers older than this say nothing about where a hero is now
DRAW_LINE = re.compile(r"^\s*DRAW:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_TOKEN = re.compile(r'"[^"]*"|\S+')
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass(frozen=True)
class Mark:
    shape: str  # circle | arrow | box | point
    space: str  # screen (the whole game window) | minimap | hud (a named, fixed part of Dota's HUD)
    x: float  # fractions of that space, 0..1, from the top-left (unused when `ref` names the point)
    y: float
    x2: float = 0.0  # arrow tip / box corner
    y2: float = 0.0
    label: str = ""
    tone: str = "coach"  # coach (cyan) | danger (red) | good (green) | info (blue)
    ref: str = ""  # a named thing instead of x, y: a map place, "me", or a HUD anchor (space "hud")
    ref2: str = ""  # the arrow's named tip

    @property
    def on_hud(self) -> bool:
        """Drawn on Dota's own HUD (the top bar, or the bottom bar with the abilities, items and shop
        buttons), which stays put when the camera moves: such a drawing doesn't go stale as you walk."""
        if self.space == "hud":
            return True
        if self.space != "screen":
            return False
        ys = (self.y, self.y2) if self.shape in ("arrow", "box") else (self.y,)
        return all(y >= 1.0 - hud.BOTTOM_HUD_FRAC for y in ys) or all(y <= hud.TOPBAR_H_FRAC for y in ys)

    @property
    def follows_me(self) -> bool:
        """Placed on your own hero: it moves with you, and comes down when Doty no longer knows where you are."""
        return ME in (self.ref, self.ref2)

    def to_window(self, w: int, h: int, minimap_side: str = "left",
                  me: tuple[float, float] | None = None) -> tuple[float, float, float, float, float]:
        """(x, y, x2, y2, unit) in window pixels; `unit` sizes circles and labels for this space. `me`: your
        hero's world position now, for marks on "me"."""
        if self.space == "hud":
            return self._hud_window(w, h, minimap_side)
        if self.space == "minimap":
            mx, my, ms, _ = hud.minimap_rect(w, h, minimap_side)
            unit = ms * 0.07
            if self.ref:
                x, y = _on_map(self.ref, w, h, minimap_side, me)
                if self.ref2:
                    x2, y2 = _on_map(self.ref2, w, h, minimap_side, me)
                elif self.shape == "box":
                    x, y, x2, y2 = x - unit, y - unit, x + unit, y + unit
                else:
                    x2, y2 = x, y
                return x, y, x2, y2, unit
            return mx + self.x * ms, my + self.y * ms, mx + self.x2 * ms, my + self.y2 * ms, unit
        return self.x * w, self.y * h, self.x2 * w, self.y2 * h, h * 0.045

    def _hud_window(self, w: int, h: int, side: str) -> tuple[float, float, float, float, float]:
        rx, ry, rw, rh = hud.anchor(self.ref, w, h, side) or (0.0, 0.0, 0.0, 0.0)
        pad = hud.unit(w, h) * 0.004
        if self.shape == "box":
            return rx - pad, ry - pad, rx + rw + pad, ry + rh + pad, h * 0.045
        cx, cy = rx + rw / 2, ry + rh / 2
        if self.shape == "arrow" and self.ref2:
            tx, ty, tw, th = hud.anchor(self.ref2, w, h, side) or (0.0, 0.0, 0.0, 0.0)
            return cx, cy, tx + tw / 2, ty + th / 2, h * 0.045
        return cx, cy, cx, cy, max(rw, rh) * 0.62 + pad


def _on_map(name: str, w: int, h: int, side: str, me: tuple[float, float] | None) -> tuple[float, float]:
    """A named place (or "me") -> window pixels on the minimap's picture."""
    pos = me if name == ME else mapgeo.place(name)
    if pos is None:  # (callers drop such marks before they get here; this is only a safe default)
        pos = (0.0, 0.0)
    fx, fy = mapgeo.to_minimap(*pos)
    x, y, s, _ = hud.map_rect(w, h, side)
    return x + fx * s, y + fy * s


def _kind(name: str) -> str:
    """'map' for a map place or "me", 'hud' for a HUD anchor, '' for a name Doty doesn't know."""
    if name == ME or mapgeo.canonical(name):
        return "map"
    if name in hud.ANCHORS:
        return "hud"
    return ""


def snap_point(x: float, y: float, team: str, markers: Iterable | None,
               radius: float = SNAP_RADIUS) -> tuple[float, float]:
    """A minimap point the AI gave (fractions of the crop) -> the nearest hero marker of `team` that Doty
    itself sees within `radius`, or the point unchanged (no markers, none of that team, or none near).
    `markers`: vision.Marker-like objects (x, y, team), already known to be fresh."""
    best, best_d = None, radius
    for m in markers or ():
        if getattr(m, "team", None) != team:
            continue
        d = math.hypot(m.x - x, m.y - y)
        if d <= best_d:
            best, best_d = m, d
    return (best.x, best.y) if best is not None else (x, y)


def fresh_markers(entry, now: float | None = None, max_age: float = MARKERS_FRESH_S) -> list:
    """`entry`: (monotonic time read, markers) as the minimap reader would leave it, or None. The markers
    if they're recent enough to say where heroes are, else []."""
    if not entry:
        return []
    when, markers = entry
    now = time.monotonic() if now is None else now
    return list(markers) if now - when <= max_age else []


def parse(text: str, sizes: dict[str, tuple[int, int]], have_me: bool = True, team: str = "",
          markers: Iterable | None = None) -> list[Mark]:
    """DRAW lines -> marks. `sizes` holds each image's (width, height) as sent to the model; lines naming an
    image that wasn't sent, coordinates outside it, or a thing Doty doesn't know are dropped. `have_me`:
    Doty knows where your hero is (else a mark on "me" is dropped). `team`: your side, so "enemy"/"ally"
    can snap to the right markers. `markers`: fresh minimap hero markers (see snap_point), or None."""
    out: list[Mark] = []
    for m in DRAW_LINE.finditer(text):
        mark = _parse_one(m.group(1), sizes, have_me, team, markers)
        if mark is not None:
            out.append(mark)
            if len(out) >= MAX_MARKS:
                break
    return out


def _parse_one(body: str, sizes, have_me: bool, team: str, markers) -> Mark | None:
    tokens = _TOKEN.findall(body)
    label = next((t.strip('"') for t in tokens if t.startswith('"')), "")[:40]
    words = [t.lower().strip(",.;:") for t in tokens if not t.startswith('"')]
    shape, space, tone, side, rest = None, "", "coach", "", []
    for w in words:  # the first word of each kind is that setting; anything after is a name (hud minimap)
        if shape is None and w in SHAPES:
            shape = w
        elif not space and w in SPACE_WORDS:
            space = SPACE_WORDS[w]
        elif tone == "coach" and w in TONES:
            tone = w
        elif not side and w in TEAM_WORDS:
            side = w
        elif w:
            rest.append(w)
    nums = [float(w) for w in rest if _NUMBER.match(w)]
    names = [w for w in rest if not _NUMBER.match(w)]
    if shape is None:
        return None
    if names:
        return _named(shape, space, names, nums, label, tone, have_me)
    space = space or "screen"
    if space not in sizes or space == "hud":
        return None
    need = 4 if shape in ("arrow", "box") else 2
    if len(nums) < need:
        return None
    iw, ih = sizes[space]
    fx = [n / iw for n in nums[:need:2]]
    fy = [n / ih for n in nums[1:need:2]]
    if not all(-0.02 <= v <= 1.02 for v in fx + fy):
        return None
    fx, fy = [min(1.0, max(0.0, v)) for v in fx], [min(1.0, max(0.0, v)) for v in fy]
    if space == "minimap" and side and markers:
        want = {"enemy": _other(team), "ally": team}.get(side, side)
        if want:
            fx[0], fy[0] = snap_point(fx[0], fy[0], want, markers)
            if need == 4:
                fx[-1], fy[-1] = snap_point(fx[-1], fy[-1], want, markers)
    return Mark(shape, space, fx[0], fy[0], fx[-1] if need == 4 else 0.0, fy[-1] if need == 4 else 0.0,
                label, tone)


def _named(shape: str, space: str, names: list[str], nums: list[float], label: str, tone: str,
           have_me: bool) -> Mark | None:
    """A mark on named things: every name must be known and of one kind (all on the map, or all on the HUD),
    and names and pixels don't mix."""
    need = 2 if shape == "arrow" else 1
    if nums or len(names) != need:
        return None
    kinds = {_kind(n) for n in names}
    if len(kinds) != 1 or "" in kinds:
        return None
    kind = kinds.pop()
    if kind == "map":
        if space not in ("", "minimap") or (ME in names and not have_me):
            return None
        refs = [n if n == ME else mapgeo.canonical(n) for n in names]
        return Mark(shape, "minimap", 0.0, 0.0, label=label, tone=tone, ref=refs[0],
                    ref2=refs[1] if need == 2 else "")
    if space not in ("", "hud", "screen"):
        return None
    return Mark(shape, "hud", 0.0, 0.0, label=label, tone=tone, ref=names[0], ref2=names[1] if need == 2 else "")


def _other(team: str) -> str:
    team = (team or "").lower()
    return {"radiant": "dire", "dire": "radiant"}.get(team, "")
