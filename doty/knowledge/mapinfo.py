"""Where on the map you are, roughly, from your hero's world coordinates (the game data sends them).

Dota's map is a square with Radiant's base in the bottom-left corner and Dire's in the top-right; the river
runs between them from the top-left to the bottom-right, and the middle lane joins the bases along the other
diagonal. So two sums place a hero well enough for coaching:

- x + y: how far along the base-to-base diagonal (0 on the river; Radiant's side below it, Dire's above).
- y - x: which side of the middle lane (the top side above it, the bottom side below).

Everything here is coarse on purpose (which side of the river, which lane's side, in a base). Exact landmarks
move between patches; these don't.
"""

from __future__ import annotations

SIDES = ("radiant", "dire")
RIVER = 1000  # |x + y| within this is on or beside the river (the rules' "past the river" starts here too)
DEEP = 4500  # this far past the river is deep in their half
MID_BAND = 1800  # |y - x| within this is the middle lane's strip
SIDE_BAND = 3500  # |y - x| beyond this is a side lane's part of the map; in between is jungle
Pos = tuple[int, int]


def other(side: str) -> str:
    return "dire" if side == "radiant" else "radiant"


def in_base(pos: Pos | None, side: str) -> bool:
    """Inside `side`'s base (the fountain and the area around it). Same test as Snapshot.in_own_base."""
    if pos is None:
        return False
    x, y = pos
    if side == "radiant":
        return x < -5200 and y < -4700
    if side == "dire":
        return x > 5000 and y > 4500
    return False


def depth(pos: Pos | None, team: str) -> float | None:
    """How far towards the enemy base you are, in world units: 0 on the river, positive on their side,
    negative on yours. None without a position or a team."""
    team = (team or "").lower()
    if pos is None or team not in SIDES:
        return None
    return float(pos[0] + pos[1]) if team == "radiant" else float(-(pos[0] + pos[1]))


def band(pos: Pos) -> str:
    """'top', 'mid' or 'bottom': the lane strip a point is in, or 'jungle' between two of them."""
    s = pos[1] - pos[0]
    if abs(s) <= MID_BAND:
        return "mid"
    if s > SIDE_BAND:
        return "top"
    if s < -SIDE_BAND:
        return "bottom"
    return "jungle"


def lane_role(lane: str, team: str) -> str:
    """A lane as the team calls it: 'safe', 'mid' or 'off' ('' for anything else). Radiant's safe lane is
    the bottom one and Dire's the top one."""
    team = (team or "").lower()
    if lane == "mid":
        return "mid"
    if lane not in ("top", "bottom") or team not in SIDES:
        return ""
    return "safe" if (lane == "bottom") == (team == "radiant") else "off"


def describe(pos: Pos | None, team: str) -> str:
    """Where the player is, in words for the coach: "your side of the map, bottom (your safe lane's side)".
    '' without a position or a team."""
    team = (team or "").lower()
    d = depth(pos, team)
    if d is None:
        return ""
    if in_base(pos, team):
        return "in your base"
    if in_base(pos, other(team)):
        return "inside the enemy base"
    if d < -RIVER:
        where = "your side of the map"
    elif d <= RIVER:
        where = "at the river"
    elif d <= DEEP:
        where = "the enemy's side of the map"
    else:
        where = "deep on the enemy's side"
    part = band(pos)
    if part == "mid":
        return f"{where}, middle lane"
    half = "top" if pos[1] - pos[0] > 0 else "bottom"
    lane = {"safe": "your safe lane's side", "off": "your offlane's side"}[lane_role(half, team)]
    return f"{where}, {half} {'jungle' if part == 'jungle' else 'lane'} ({lane})"
