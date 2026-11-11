"""Hero knowledge from data, not the AI's memory: skill order, skill points, items (Spirit Breaker, 7.41)."""

from doty.knowledge.constants import Knowledge
from doty.state import Snapshot

SB = "npc_dota_hero_spirit_breaker"
IDS = {"5353": "spirit_breaker_charge_of_darkness", "5354": "spirit_breaker_bulldoze",
       "5355": "spirit_breaker_greater_bash", "5356": "spirit_breaker_nether_strike",
       "6000": "special_bonus_armor_4", "5357": "spirit_breaker_planar_pocket", "5358": "spirit_breaker_bull_rush"}
# What the OpenDota explorer returned for Spirit Breaker on 7.41 (trimmed): Greater Bash first, then Charge.
ROWS = [
    {"lvl": 1, "ab": 5355, "n": 907}, {"lvl": 1, "ab": 5353, "n": 129},
    {"lvl": 2, "ab": 5353, "n": 904}, {"lvl": 2, "ab": 5355, "n": 130},
    {"lvl": 3, "ab": 5355, "n": 756}, {"lvl": 4, "ab": 5353, "n": 538}, {"lvl": 5, "ab": 5355, "n": 665},
    {"lvl": 6, "ab": 5356, "n": 1031}, {"lvl": 7, "ab": 5355, "n": 677}, {"lvl": 8, "ab": 5353, "n": 552},
    {"lvl": 9, "ab": 5353, "n": 638}, {"lvl": 10, "ab": 5353, "n": 394}, {"lvl": 11, "ab": 6000, "n": 379},
    {"lvl": 12, "ab": 5354, "n": 866},
]


def _kb():
    kb = Knowledge()
    kb.heroes = {SB: {"id": 71, "localized_name": "Spirit Breaker"}}
    kb.ability_ids = IDS
    kb.skill_rows[71] = ROWS
    kb.ability_info = {
        "spirit_breaker_charge_of_darkness": {"dname": "Charge of Darkness", "desc": "Charges at an enemy.",
                                              "innate": False, "hidden": False},
        "spirit_breaker_greater_bash": {"dname": "Greater Bash", "desc": "A chance to stun on attack.",
                                        "innate": False, "hidden": False},
        "spirit_breaker_bulldoze": {"dname": "Bulldoze", "desc": "", "innate": False, "hidden": False},
        "spirit_breaker_nether_strike": {"dname": "Nether Strike", "desc": "", "innate": False, "hidden": False},
        "spirit_breaker_bull_rush": {"dname": "Empowering Haste", "desc": "", "innate": True, "hidden": True},
        "spirit_breaker_planar_pocket": {"dname": "Planar Pocket", "desc": "", "innate": False, "hidden": False},
    }
    kb.hero_abilities = {SB: {"abilities": list(kb.ability_info)}}
    kb.items = {
        "item_phase_boots": {"id": 50, "dname": "Phase Boots", "cost": 1500, "components": ["boots"]},
        "item_ancient_janggo": {"id": 185, "dname": "Drum of Endurance", "cost": 1650, "components": ["x"]},
        "item_invis_sword": {"id": 152, "dname": "Shadow Blade", "cost": 3000, "components": ["x"]},
        "item_blink": {"id": 1, "dname": "Blink Dagger", "cost": 2250},
        "item_black_king_bar": {"id": 116, "dname": "Black King Bar", "cost": 4050, "components": ["x"]},
    }
    kb.item_popularity[71] = {"start_game_items": {}, "early_game_items": {"50": 38},
                              "mid_game_items": {"50": 59, "185": 40, "152": 19},
                              "late_game_items": {"152": 37, "116": 26}}
    return kb


def _snap(level, levels, extra=None):
    abilities = {f"ability{i}": {"name": n, "level": lv} for i, (n, lv) in enumerate(levels.items())}
    return Snapshot({"hero": {"name": SB, "id": 71, "level": level, **(extra or {})}, "abilities": abilities})


def test_first_point_goes_to_greater_bash_not_charge():
    kb = _kb()
    snap = _snap(1, {"spirit_breaker_charge_of_darkness": 0, "spirit_breaker_greater_bash": 0,
                     "spirit_breaker_bulldoze": 0, "spirit_breaker_nether_strike": 0})
    assert kb.unspent_points(snap) == 1
    assert kb.ability_name(kb.next_skill(snap)) == "Greater Bash"


def test_next_skill_follows_the_order_from_where_you_are():
    kb = _kb()
    # Took Charge first (a detour): Doty still asks for Greater Bash next, then carries on.
    snap = _snap(2, {"spirit_breaker_charge_of_darkness": 1, "spirit_breaker_greater_bash": 0,
                     "spirit_breaker_bulldoze": 0, "spirit_breaker_nether_strike": 0})
    assert kb.next_skill(snap) == "spirit_breaker_greater_bash"
    snap = _snap(6, {"spirit_breaker_charge_of_darkness": 2, "spirit_breaker_greater_bash": 3,
                     "spirit_breaker_bulldoze": 0, "spirit_breaker_nether_strike": 0})
    assert kb.next_skill(snap) == "spirit_breaker_nether_strike"


def test_innate_and_shard_abilities_dont_hide_a_skill_point():
    kb = _kb()
    # Level 3 with 2 points spent; the innate and the Shard ability report levels too.
    snap = _snap(3, {"spirit_breaker_charge_of_darkness": 1, "spirit_breaker_greater_bash": 1,
                     "spirit_breaker_bulldoze": 0, "spirit_breaker_nether_strike": 0,
                     "spirit_breaker_bull_rush": 1, "spirit_breaker_planar_pocket": 1},
                 extra={"aghanims_shard": True})  # (Planar Pocket has a level once you own the Shard)
    assert snap.unspent_skill_points == 0  # the old count missed it
    assert kb.unspent_points(snap) == 1


def test_no_point_is_made_up_from_an_ability_doty_didnt_expect_or_past_level_16():
    kb = _kb()
    # A learnable ability outside the pros' list with a point in it, no Shard or Scepter to explain it: spent.
    snap = _snap(3, {"spirit_breaker_charge_of_darkness": 1, "spirit_breaker_greater_bash": 1,
                     "spirit_breaker_planar_pocket": 1})
    assert kb.unspent_points(snap) == 0
    assert kb.unspent_points(_snap(17, {"spirit_breaker_greater_bash": 4})) is None  # (17+: some levels give none)


def test_items_this_hero_doesnt_buy_are_caught():
    kb = _kb()
    allowed = kb.allowed_items(71)
    assert "item_blink" not in allowed and "item_invis_sword" in allowed
    assert "item_black_king_bar" in allowed  # a counter item is always fair game
    assert kb.item_mentions("Buy Blink Dagger to catch them") == {"item_blink"}
    assert kb.item_mentions("Get a BKB before fighting") == {"item_black_king_bar"}
    assert kb.typical_items(71)[:2] == ["item_phase_boots", "item_invis_sword"]


def test_the_coach_is_told_what_the_hero_does_and_what_to_skill():
    kb = _kb()
    s = kb.summary_for_prompt(SB, 300)
    assert s["pro_skill_order"][:3] == ["Greater Bash", "Charge of Darkness", "Greater Bash"]
    assert {"name": "Greater Bash", "does": "A chance to stun on attack."} in s["your_abilities"]
    assert "Phase Boots" in s["typical_items"] and "Blink Dagger" not in s["typical_items"]


def test_item_names_are_not_heroes_and_spells_are_not_items():
    """"Monkey King Bar" doesn't name Monkey King; Riki's "Blink Strike" and Anti-Mage's own "Blink"
    aren't Blink Dagger; Blink alone, for Spirit Breaker, is."""
    kb = _kb()
    am = "npc_dota_hero_antimage"
    kb.heroes.update({"npc_dota_hero_monkey_king": {"id": 114, "localized_name": "Monkey King"},
                      "npc_dota_hero_axe": {"id": 2, "localized_name": "Axe"}, am: {"id": 1, "localized_name": "Anti-Mage"}})
    kb.items.update({"item_monkey_king_bar": {"dname": "Monkey King Bar", "cost": 4700},
                     "item_ogre_axe": {"dname": "Ogre Axe", "cost": 1000},
                     "item_blink": {"dname": "Blink Dagger", "cost": 2250}})
    kb.ability_info.update({"antimage_blink": {"dname": "Blink"}, "riki_blink_strike": {"dname": "Blink Strike"}})
    kb.hero_abilities[am] = {"abilities": ["antimage_blink"]}
    assert kb.hero_mentions("Buy Monkey King Bar and an Ogre Axe") == set()
    assert kb.hero_mentions("Monkey King is in the trees; Axe is missing") == {"npc_dota_hero_monkey_king",
                                                                               "npc_dota_hero_axe"}
    assert kb.item_mentions("Watch for Riki's Blink Strike") == set()
    assert kb.item_mentions("Blink to the jungle and farm", own_hero=am) == set()
    assert kb.item_mentions("Get Blink before Charge", own_hero=SB) == {"item_blink"}


def test_a_damaged_cache_file_is_ignored_not_fatal(tmp_path, monkeypatch):
    import asyncio

    kb = Knowledge(cache_dir=tmp_path)
    (tmp_path / "items.json").write_text('{"blink": {"dname": "Bli', encoding="utf-8")  # cut off mid-write
    import os

    os.utime(tmp_path / "items.json", (1, 1))  # stale, so Doty fetches, and the fetch fails (offline)

    class Offline:
        def get(self, url):
            raise __import__("aiohttp").ClientError("offline")

        async def close(self):
            pass

    kb._session = Offline()
    assert asyncio.run(kb._cached_json("items.json", "https://example.invalid/items.json")) is None


def test_hero_data_waits_until_the_patch_is_known():
    import asyncio

    kb = _kb()
    kb._loading = True
    asyncio.run(kb.ensure_hero(71))
    assert 71 not in kb._attempted  # not marked as tried: the next update asks again, after load()


def test_enemy_data_is_lighter_and_doesnt_stop_the_full_fetch_later():
    import asyncio

    from doty.knowledge import constants

    kb = constants.Knowledge()
    kb.items = {"item_bfury": {"id": 145, "dname": "Battle Fury", "cost": 4100, "components": ["x"]},
                "item_tango": {"id": 44, "dname": "Tango", "cost": 90}}
    urls = []

    async def cached(name, url, ttl=0):
        urls.append(url)
        if "itemPopularity" in url:
            return {"early_game_items": {"145": 90, "44": 99}}
        if "itemTimings" in url:
            return [{"hero_id": 8, "time": 900, "games": 100}]
        if "matchups" in url:
            return []  # (a list, as OpenDota sends)
        return {}

    kb._cached_json = cached
    asyncio.run(kb.ensure_enemy(8))
    assert len(urls) == 2 and "itemTimings?item=bfury&hero_id=8" in urls[1]
    assert kb.spikes(8) == [(900, "item_bfury")]
    assert not kb.has_hero_data(8)  # the enemy fetch isn't the full one
    asyncio.run(kb.ensure_enemy(8))
    assert len(urls) == 2  # once
    asyncio.run(kb.ensure_hero(8))  # now you play Juggernaut: benchmarks, matchups and the rest still come
    assert any("benchmarks" in u for u in urls) and kb.has_hero_data(8)


def test_a_live_tip_that_invents_a_skill_point_is_dropped():
    """A real match: the AI read the level-up arrows off the screen wrong and said "Level 4 with Charge 1 and
    Bash 2 means a point is unspent" every few seconds. Doty's own count from the game data settles it."""
    from doty.advice import AdviceBus
    from doty.coach import Coach
    from doty.config import CoachConfig
    from doty.state import GameStateStore

    kb = _kb()
    coach = Coach(CoachConfig(), GameStateStore(), AdviceBus(), kb)
    all_spent = _snap(4, {"spirit_breaker_charge_of_darkness": 1, "spirit_breaker_greater_bash": 2,
                          "spirit_breaker_bulldoze": 1, "spirit_breaker_nether_strike": 0})
    claim = "Level Bulldoze now: Level 4 with Charge 1 and Bash 2 means a point is unspent."
    assert coach._false_skill_claim(claim, all_spent)
    assert not coach._false_skill_claim("Farm the bottom triangle until Black King Bar.", all_spent)
    one_left = _snap(4, {"spirit_breaker_charge_of_darkness": 1, "spirit_breaker_greater_bash": 2,
                         "spirit_breaker_bulldoze": 0, "spirit_breaker_nether_strike": 0})
    assert not coach._false_skill_claim(claim, one_left)  # there really is one: the tip stands
    late = _snap(25, {"spirit_breaker_greater_bash": 4}, extra={f"talent_{i}": True for i in (1, 3, 5, 7)})
    assert coach._false_skill_claim("Pick the level 20 talent now.", late)  # all four tiers picked already
