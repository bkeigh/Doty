"""Marks: the circles, arrows and boxes the coach draws over the game to show you things.

The coach looks at the screenshots and, when pointing helps, adds lines like

    DRAW: circle minimap 312 140 "3 missing"
    DRAW: arrow screen 640 300 980 180 "back to tower" danger

in the pixel coordinates of the image it was given. Doty turns those into fractions of that image
(the whole game window, or the minimap crop), and the overlay maps them back onto the real
window, at whatever size and aspect ratio it is. Marks are only ever placed on things visible in
the screenshot: the coach is told never to invent positions (fair play).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import hud

SHAPES = ("circle", "arrow", "box", "point")
SPACES = ("screen", "minimap")
TONES = ("coach", "danger", "good", "info")
MAX_MARKS = 3
DRAW_LINE = re.compile(r"^\s*DRAW:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_TOKEN = re.compile(r'"[^"]*"|\S+')


@dataclass(frozen=True)
class Mark:
    shape: str  # circle | arrow | box | point
    space: str  # screen (the whole game window) | minimap
    x: float  # fractions of that space, 0..1, from the top-left
    y: float
    x2: float = 0.0  # arrow tip / box corner
    y2: float = 0.0
    label: str = ""
    tone: str = "coach"  # coach (cyan) | danger (red) | good (green) | info (blue)

    @property
    def on_hud(self) -> bool:
        """Drawn on Dota's own HUD (the top bar, or the bottom bar with the abilities, items and shop
        buttons), which stays put when the camera moves: such a drawing doesn't go stale as you walk."""
        if self.space != "screen":
            return False
        ys = (self.y, self.y2) if self.shape in ("arrow", "box") else (self.y,)
        return all(y >= 1.0 - hud.BOTTOM_HUD_FRAC for y in ys) or all(y <= hud.TOPBAR_H_FRAC for y in ys)

    def to_window(self, w: int, h: int, minimap_side: str = "left") -> tuple[float, float, float, float, float]:
        """(x, y, x2, y2, unit) in window pixels; `unit` sizes circles and labels for this space."""
        if self.space == "minimap":
            mx, my, ms, _ = hud.minimap_rect(w, h, minimap_side)
            return mx + self.x * ms, my + self.y * ms, mx + self.x2 * ms, my + self.y2 * ms, ms * 0.07
        return self.x * w, self.y * h, self.x2 * w, self.y2 * h, h * 0.045


def parse(text: str, sizes: dict[str, tuple[int, int]]) -> list[Mark]:
    """DRAW lines -> marks. `sizes` holds each image's (width, height) as sent to the model;
    lines naming an image that wasn't sent, or coordinates outside it, are dropped."""
    out: list[Mark] = []
    for m in DRAW_LINE.finditer(text):
        tokens = _TOKEN.findall(m.group(1))
        label = next((t.strip('"') for t in tokens if t.startswith('"')), "")
        words = [t.lower() for t in tokens if not t.startswith('"')]
        shape = next((w for w in words if w in SHAPES), None)
        space = next((w for w in words if w in SPACES), "screen")
        tone = next((w for w in words if w in TONES), "coach")
        nums = []
        for w in words:
            try:
                nums.append(float(w.rstrip(",")))
            except ValueError:
                continue
        if shape is None or space not in sizes:
            continue
        need = 4 if shape in ("arrow", "box") else 2
        if len(nums) < need:
            continue
        iw, ih = sizes[space]
        fx = [n / iw for n in nums[:need:2]]
        fy = [n / ih for n in nums[1:need:2]]
        if not all(-0.02 <= v <= 1.02 for v in fx + fy):
            continue
        fx, fy = [min(1.0, max(0.0, v)) for v in fx], [min(1.0, max(0.0, v)) for v in fy]
        out.append(Mark(shape, space, fx[0], fy[0], fx[-1] if need == 4 else 0.0, fy[-1] if need == 4 else 0.0,
                        label[:40], tone))
        if len(out) >= MAX_MARKS:
            break
    return out
