"""Where the map's fixed things are, in world coordinates, so the coach can name a place and Doty draws it.

The AI picks WHAT to point at ("radiant_bot_t1", "roshan_top", "me"); code knows WHERE it is. A position
read off a shrunken, seconds-old screenshot is a guess; these are not.

Source: the entity dump of patch 7.41's map in leamare/dota-interactive-map
(assets/data/741/mapdata.json, made with the dota-map-coordinates custom game, which reads every entity's
origin in the game itself), and that project's worlddata.json for the world's bounds (GetWorldMinX() and
friends: -10464 to 10400 on both axes since the 7.33 map). Each tower's side, lane and tier is worked out
here from where it stands (Radiant's half is below the river diagonal, x + y < 0); the dump only says
"tower1".."tower4". The lanes' midpoints are derived (the middle of the two tier-1 towers), not dumped.

Left out, because the dump has nothing for them on 7.41 (rather no mark than a guessed one):
- water runes (no rune spawner of that kind on this map), shrines (gone since 7.33), side shops;
- the watchers (ten "lantern" entities: no reliable short names for them);
- which Roshan pit is in use: the pit moves between day and night, so both are named and the AI picks.

Coordinates move a little with each map patch: re-check against the next mapdata.json when one comes out.
World x grows to the right (east, towards Dire's bottom lane) and y grows upwards (north), as the game's
data sends a hero's position.
"""

from __future__ import annotations

MAP_PATCH = "7.41"
# The world's bounds; the minimap shows this square (the dota-map-coordinates world data). If the real
# minimap turns out to frame it differently, this is the one place to correct.
WORLD_MIN = -10464.0
WORLD_MAX = 10400.0

Pos = tuple[float, float]

PLACES: dict[str, Pos] = {
    # Towers: side, lane, tier.
    "radiant_top_t1": (-6336, 1856),
    "radiant_top_t2": (-6501, -872),
    "radiant_top_t3": (-6592, -3408),
    "radiant_mid_t1": (-1544, -1408),
    "radiant_mid_t2": (-3190, -2926),
    "radiant_mid_t3": (-4640, -4144),
    "radiant_bot_t1": (4860, -6379),
    "radiant_bot_t2": (-360, -6256),
    "radiant_bot_t3": (-3952, -6112),
    "radiant_t4_top": (-5712, -4864),
    "radiant_t4_bot": (-5392, -5192),
    "dire_top_t1": (-5275, 6036),
    "dire_top_t2": (-128, 6016),
    "dire_top_t3": (3552, 5776),
    "dire_mid_t1": (524, 652),
    "dire_mid_t2": (2496, 2112),
    "dire_mid_t3": (4272, 3759),
    "dire_bot_t1": (6269, -2240),
    "dire_bot_t2": (6400, 384),
    "dire_bot_t3": (6336, 3032),
    "dire_t4_top": (4944, 4776),
    "dire_t4_bot": (5280, 4432),
    # Barracks (melee and ranged) in each lane.
    "radiant_top_melee_rax": (-6336, -3758),
    "radiant_top_ranged_rax": (-6844, -3759),
    "radiant_mid_melee_rax": (-4672, -4552),
    "radiant_mid_ranged_rax": (-5060, -4199),
    "radiant_bot_melee_rax": (-4280, -6360),
    "radiant_bot_ranged_rax": (-4279, -5853),
    "dire_top_melee_rax": (3898, 5496),
    "dire_top_ranged_rax": (3894, 6025),
    "dire_mid_melee_rax": (4702, 3824),
    "dire_mid_ranged_rax": (4336, 4183),
    "dire_bot_melee_rax": (6592, 3392),
    "dire_bot_ranged_rax": (6064, 3376),
    # Ancients, fountains, shops.
    "radiant_ancient": (-5920, -5352),
    "dire_ancient": (5528, 5000),
    "radiant_fountain": (-7456, -6938),
    "dire_fountain": (7408, 6848),
    "radiant_secret_shop": (-5080, 1948),
    "dire_secret_shop": (4886, -1208),
    # Roshan's two pits (he moves between them with day and night).
    "roshan_top": (-3194, 2395),
    "roshan_bottom": (2831, -2740),
    # Runes.
    "power_rune_top": (-1640, 1112),
    "power_rune_bottom": (1180, -1216),
    "bounty_rune_radiant": (595, -4660),
    "bounty_rune_dire": (-996, 4431),
    "wisdom_rune_radiant": (-8088, 768),
    "wisdom_rune_dire": (8167, -1142),
    # Neutral objectives.
    "lotus_pool_top": (-7548, 4209),
    "lotus_pool_bottom": (7504, -4405),
    "tormentor_top": (-7680, 6336),
    "tormentor_bottom": (7744, -6208),
    "outpost_top": (-4096, -448),
    "outpost_bottom": (3392, -448),
    "twin_gate_top": (-6458, 7599),
    "twin_gate_bottom": (6426, -7314),
    # The lanes, roughly: halfway between the two tier-1 towers.
    "top_lane": (-5806, 3946),
    "mid_lane": (-510, -378),
    "bot_lane": (5565, -4310),
}

# Other spellings the AI is likely to write for the same place.
ALIASES: dict[str, str] = {
    "radiant_bottom_t1": "radiant_bot_t1", "radiant_bottom_t2": "radiant_bot_t2",
    "radiant_bottom_t3": "radiant_bot_t3", "dire_bottom_t1": "dire_bot_t1", "dire_bottom_t2": "dire_bot_t2",
    "dire_bottom_t3": "dire_bot_t3", "bottom_lane": "bot_lane", "middle_lane": "mid_lane",
    "radiant_base": "radiant_ancient", "dire_base": "dire_ancient",
    "power_rune_bot": "power_rune_bottom", "roshan_bot": "roshan_bottom",
    "lotus_pool_bot": "lotus_pool_bottom", "tormentor_bot": "tormentor_bottom", "outpost_bot": "outpost_bottom",
    "twin_gate_bot": "twin_gate_bottom",
}


def canonical(name: str) -> str:
    """The table's own name for a place ('' if Doty doesn't know it). Case and dashes don't matter."""
    key = (name or "").strip().lower().replace("-", "_")
    key = ALIASES.get(key, key)
    return key if key in PLACES else ""


def place(name: str) -> Pos | None:
    """A named place's world position, or None if it isn't in the table (never a guess)."""
    key = canonical(name)
    return PLACES[key] if key else None


def to_minimap(x: float, y: float) -> tuple[float, float]:
    """World position -> (fx, fy): fractions of the minimap picture from its top-left corner (world y grows
    upwards, the picture's downwards). Clamped to the picture."""
    span = WORLD_MAX - WORLD_MIN
    fx = (x - WORLD_MIN) / span
    fy = (WORLD_MAX - y) / span
    return min(1.0, max(0.0, fx)), min(1.0, max(0.0, fy))
