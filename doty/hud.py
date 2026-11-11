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


# Where the map picture itself sits in the minimap's corner, and the bottom bar's fixed buttons, in HUD units
# (unit(): the window height on 16:9 and wider). UNCALIBRATED: a best guess at Dota's default HUD at its
# default minimap size, like vision.TopBarLayout before calibration. Check against a real frame (draw
# "box hud item_slot_1" and see where it lands) and correct the numbers here; nothing else needs to change.
HUD_CALIBRATED = False
MAP_SIDE = 0.262  # the map picture's side (inside the 0.30 crop)
MAP_INSET = 0.008  # its gap to the window's side and bottom edges
ABILITY_SIZE = 0.056  # an ability icon (square)
ABILITY_GAP = 0.006
ABILITY_TOP = 0.112  # from the window's bottom up to the top of the ability icons
ABILITY_CENTER = -0.04  # the ability row's centre, from the window's centre (the row sits a bit left of it)
ITEM_W, ITEM_H = 0.062, 0.046  # an inventory slot (items are wider than tall)
ITEM_GAP = 0.004
ITEM_LEFT = 0.175  # the inventory's left edge, from the window's centre
ITEM_TOP = 0.112  # from the window's bottom up to the top row of the inventory
SHOP_W, SHOP_H = 0.16, 0.05  # the shop button (with the gold on it), in the bottom-right corner
SHOP_INSET = 0.012
ANCHORS = ("ability_1", "ability_2", "ability_3", "ability_4", "ability_5", "ability_6", "ultimate",
           "item_slot_1", "item_slot_2", "item_slot_3", "item_slot_4", "item_slot_5", "item_slot_6",
           "tp_slot", "neutral_slot", "shop_button", "minimap", "top_bar", "clock",
           *(f"radiant_hero_{k}" for k in range(1, 6)), *(f"dire_hero_{k}" for k in range(1, 6)))
RectF = tuple[float, float, float, float]  # x, y, w, h in window pixels


def unit(w: int, h: int) -> float:
    """The HUD unit in pixels (same as vision.unit): Dota scales its HUD with the window height on 16:9 and
    wider, and with the width on narrower windows (16:10, 4:3)."""
    return float(min(h, w * 9 / 16))


def map_rect(w: int, h: int, side: str = "left") -> RectF:
    """The minimap's picture (the map itself, not the crop around it), in window pixels: where a world
    position lands (knowledge/mapgeo.to_minimap gives the fractions of this square)."""
    u = unit(w, h)
    s, inset = MAP_SIDE * u, MAP_INSET * u
    return (inset if side == "left" else w - inset - s, h - inset - s, s, s)


def anchor(name: str, w: int, h: int, side: str = "left", abilities: int = 4) -> RectF | None:
    """A fixed part of Dota's HUD by name, in window pixels: the place stays put whatever the camera does,
    so a drawing on it never goes stale. None for a name Doty doesn't know. `abilities`: how many ability
    slots the hero's bar has (they're centred as a row, so the count moves them)."""
    u = unit(w, h)
    cx = w / 2
    name = (name or "").strip().lower()
    if name == "ultimate":
        name = f"ability_{max(1, min(6, abilities))}"
    if name.startswith("ability_") and name[8:].isdigit() and 1 <= int(name[8:]) <= 6:
        k, n = int(name[8:]) - 1, max(abilities, int(name[8:]))
        step = (ABILITY_SIZE + ABILITY_GAP) * u
        left = cx + ABILITY_CENTER * u - (n * step - ABILITY_GAP * u) / 2
        return (left + k * step, h - ABILITY_TOP * u, ABILITY_SIZE * u, ABILITY_SIZE * u)
    if name.startswith("item_slot_") and name[10:].isdigit() and 1 <= int(name[10:]) <= 6:
        k = int(name[10:]) - 1  # slots 1-3 the top row, 4-6 the bottom row, as Dota numbers them
        col, row = k % 3, k // 3
        return (cx + (ITEM_LEFT + col * (ITEM_W + ITEM_GAP)) * u, h - (ITEM_TOP - row * (ITEM_H + ITEM_GAP)) * u,
                ITEM_W * u, ITEM_H * u)
    if name in ("tp_slot", "neutral_slot"):  # right of the inventory: the TP scroll on top, the neutral item below
        row = 0 if name == "tp_slot" else 1
        return (cx + (ITEM_LEFT + 3 * (ITEM_W + ITEM_GAP)) * u, h - (ITEM_TOP - row * (ITEM_H + ITEM_GAP)) * u,
                ITEM_H * u, ITEM_H * u)
    if name == "shop_button":
        return (w - (SHOP_W + SHOP_INSET) * u, h - (SHOP_H + SHOP_INSET) * u, SHOP_W * u, SHOP_H * u)
    if name == "minimap":
        return map_rect(w, h, side)
    if name == "top_bar":
        return tuple(float(v) for v in topbar_rect(w, h))
    if name == "clock":
        return (cx - 0.04 * u, 0.0, 0.08 * u, TOPBAR_H_FRAC * u * 0.7)
    for team in ("radiant", "dire"):
        prefix = f"{team}_hero_"
        if name.startswith(prefix) and name[len(prefix):] in ("1", "2", "3", "4", "5"):
            from .vision import TopBarLayout  # (the portraits' layout lives with the reader that matches them)

            k = int(name[len(prefix):]) - 1
            return TopBarLayout().rects(w, h)[k + (5 if team == "dire" else 0)]
    return None


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
