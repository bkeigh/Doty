"""Where Dota's HUD puts things, as fractions of the game window.

The screenshot crops (capture.py) and the overlay (overlay.py) both use these, so a mark the coach
places on the minimap crop lands on the same spot of the real minimap, on any window size or
aspect ratio. Dota anchors its HUD to the window edges and scales it with the height: the minimap
sits in a bottom corner, the hero bar is centred at the top, and the action bar is centred at the
bottom. Ultrawide (21:9, 32:9) just widens the play area in between.
"""

from __future__ import annotations

MINIMAP_FRAC = 0.30  # the minimap crop: a square this fraction of the window height (the map is ~0.25-0.28)
TOPBAR_H_FRAC = 0.075  # the hero bar and clock
TOPBAR_W_PER_H = 1.3  # the hero bar is about 1.3x the window height wide, centred
BOTTOM_HUD_FRAC = 0.20  # action bar, inventory and minimap reach this far up
COLUMN_BOTTOM_FRAC = 0.55  # the shop panel opens below this on the right
Rect = tuple[int, int, int, int]  # x, y, w, h


def scale(h: float) -> float:
    """HUD scale relative to 1080p; Doty's overlay grows and shrinks with it."""
    return max(0.7, min(2.4, h / 1080.0))


def minimap_rect(w: int, h: int, side: str = "left") -> Rect:
    s = int(h * MINIMAP_FRAC)
    return (0 if side == "left" else w - s, h - s, s, s)


def topbar_rect(w: int, h: int) -> Rect:
    tw, th = min(w, int(h * TOPBAR_W_PER_H)), int(h * TOPBAR_H_FRAC)
    return ((w - tw) // 2, 0, tw, th)


def card_column(w: int, h: int, anchor: str = "right") -> Rect:
    """Where advice cards go: below the hero bar, above the bottom HUD (and so clear of the
    minimap), at one side of the play area. Width follows the HUD scale (wider on 1440p and 4K)."""
    s = scale(h)
    cw = min(int(380 * s), max(260, w // 3))
    margin = int(22 * s)
    if anchor == "top":
        cw = min(int(560 * s), w - 2 * margin)
        return ((w - cw) // 2, int(h * (TOPBAR_H_FRAC + 0.03)), cw, int(h * 0.3))
    top = int(h * (TOPBAR_H_FRAC + 0.05))
    bottom = int(h * COLUMN_BOTTOM_FRAC)  # stay above the shop and the inventory
    if anchor == "left":
        x = max(margin, (w - h * 16 // 9) // 2 - cw - margin) if w > h * 3 else margin
    else:  # on 32:9, don't strand the cards far from the action: hug the 16:9 centre's edge
        x = min(w - cw - margin, (w + h * 16 // 9) // 2 + margin) if w > h * 3 else w - cw - margin
    return (x, top, cw, max(120, bottom - top))
