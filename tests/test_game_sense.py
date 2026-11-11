"""Game sense from the game data: what an item still costs (its parts count), which role you're playing,
where you are, a burst before you're low, and the coach being told all of it."""

import time

from doty import triggers as tl
from doty.advice import AdviceBus, Priority
from doty.coach import Coach, prompts
from doty.config import CoachConfig, RulesConfig
from doty.knowledge import mapinfo, roles
from doty.knowledge.constants import Knowledge
from doty.rules.base import RuleContext
from doty.rules.economy import BUYBACK_FROM, AffordableBuildItem, UnspentGold
from doty.rules.progression import FarmCheck, ItemPace, LaningTips
from doty.rules.survival import GankRisk, LowHp, SaveItems
from doty.state import GameStateStore

JUGG, LION = "npc_dota_hero_juggernaut", "npc_dota_hero_lion"
GAME = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"


def _kb():
    kb = Knowledge()
    kb.heroes = {JUGG: {"id": 8, "localized_name": "Juggernaut", "roles": ["Carry", "Pusher", "Escape"]},
                 LION: {"id": 26, "localized_name": "Lion", "roles": ["Support", "Disabler", "Nuker"]},
                 "npc_dota_hero_tidehunter": {"id": 29, "localized_name": "Tidehunter", "roles": ["Initiator"]},
                 "npc_dota_hero_phantom_assassin": {"id": 44, "localized_name": "Phantom Assassin",
                                                    "roles": ["Carry", "Escape"]}}

    def item(i, name, cost, parts=None, **more):
        return {"id": i, "dname": name, "cost": cost, "components": parts, **more}

    kb.items = {f"item_{k}": v for k, v in {
        "boots": item(29, "Boots of Speed", 500), "gloves": item(25, "Gloves of Haste", 450),
        "belt_of_strength": item(14, "Belt of Strength", 450),
        "power_treads": item(63, "Power Treads", 1400, ["boots", "gloves", "belt_of_strength"]),
        "chainmail": item(4, "Chainmail", 550), "blades_of_attack": item(2, "Blades of Attack", 450),
        "phase_boots": item(50, "Phase Boots", 1500, ["boots", "chainmail", "blades_of_attack"]),
        "ogre_axe": item(21, "Ogre Axe", 1000), "mithril_hammer": item(10, "Mithril Hammer", 1600),
        "recipe_black_king_bar": item(115, "Black King Bar Recipe", 1450),
        "black_king_bar": item(116, "Black King Bar", 4050, ["mithril_hammer", "ogre_axe"]),
        "blade_of_alacrity": item(22, "Blade of Alacrity", 1000), "boots_of_elves": item(18, "Band of Elvenskin", 450),
        "recipe_yasha": item(169, "Yasha Recipe", 600),
        "yasha": item(170, "Yasha", 2050, ["blade_of_alacrity", "boots_of_elves"]),
        "ultimate_orb": item(24, "Ultimate Orb", 2800, qual="secret_shop"),
        "manta": item(147, "Manta Style", 4850, ["yasha", "ultimate_orb"]),
        "monkey_king_bar": item(135, "Monkey King Bar", 4700), "shivas_guard": item(119, "Shiva's Guard", 5175),
        "blink": item(1, "Blink Dagger", 2250),
    }.items()}
    kb.item_popularity[8] = {"early_game_items": {"63": 90, "50": 40},
                             "mid_game_items": {"170": 80, "116": 60, "135": 30}, "late_game_items": {"147": 60}}
    kb.benchmarks = {8: {"result": {"last_hits_per_min": [{"percentile": 0.5, "value": 8.0}]}},
                     26: {"result": {"last_hits_per_min": [{"percentile": 0.5, "value": 2.0}]}}}
    return kb


def _feed(store, t, clock=600, hero=JUGG, hid=8, hp=1000, gold=0, items=(), pos=(-1800, -6400), lh=0,
          extra=None, gpm=450, tp=True, stash=()):
    inv = {f"slot{i}": ({"name": n} if isinstance(n, str) else n) for i, n in enumerate(items)}
    inv.update({f"stash{i}": {"name": n} for i, n in enumerate(stash)})
    if tp:
        inv["teleport0"] = {"name": "item_tpscroll", "cooldown": 0, "can_cast": True}
    return store.ingest({"map": {"matchid": "7", "clock_time": clock, "game_state": GAME},
                         "player": {"team_name": "radiant", "gold": gold, "last_hits": lh, "gpm": gpm},
                         "hero": {"name": hero, "id": hid, "alive": True, "health": hp, "max_health": 1000,
                                  "xpos": pos[0], "ypos": pos[1], "level": 8, **(extra or {})},
                         "items": inv}, received_t=t)


def _ctx(store, kb=None, level="some"):
    return RuleContext(store.current, store.previous, store, RulesConfig(), kb, [], level)


# items: parts, what's left, the build ----------------------------------------------------------------------
def test_an_items_cost_counts_the_parts_you_already_hold():
    kb = _kb()
    assert kb.parts_of("item_black_king_bar") == ["item_mithril_hammer", "item_ogre_axe", "item_recipe_black_king_bar"]
    assert kb.cost_left("item_black_king_bar", []) == 4050
    assert kb.cost_left("item_black_king_bar", ["item_ogre_axe"]) == 3050
    assert kb.cost_left("item_black_king_bar", ["item_ogre_axe", "item_mithril_hammer"]) == 1450  # the recipe
    assert kb.cost_left("item_black_king_bar", ["item_black_king_bar"]) == 0
    assert kb.cost_left("item_manta", ["item_blade_of_alacrity"]) == 3850  # a part of a part counts too
    assert kb.missing_parts("item_manta", ["item_yasha"]) == ["item_ultimate_orb"]
    assert kb.cost_left("item_blink", []) == 2250 and kb.missing_parts("item_blink", []) == []  # bought whole
    assert kb.cost_left("item_power_treads", ["item_boots", "item_boots"]) == 900  # one pair is used, not two


def test_the_next_buy_is_the_dearest_part_you_can_afford_then_the_finish():
    kb = _kb()
    assert kb.next_buy("item_black_king_bar", [], 600) is None  # nothing worth a call yet
    assert kb.next_buy("item_black_king_bar", [], 1200)["buy"] == "item_ogre_axe"
    assert kb.next_buy("item_black_king_bar", [], 2000)["buy"] == "item_mithril_hammer"
    done = kb.next_buy("item_black_king_bar", ["item_ogre_axe", "item_mithril_hammer"], 1500)
    assert done["finish"] and done["cost"] == 1450  # (the recipe alone is never suggested before that)
    assert kb.next_buy("item_black_king_bar", ["item_ogre_axe", "item_mithril_hammer"], 1000) is None
    assert kb.secret_shop("item_ultimate_orb") and not kb.secret_shop("item_ogre_axe")


def test_a_stepping_stone_isnt_listed_as_a_core_item():
    """A real demo run: "Build: Perseverance > Battle Fury > Yasha". Yasha is on the way to Manta Style."""
    kb = _kb()
    usual = kb.typical_items(8)
    assert "item_manta" in usual and "item_yasha" not in usual
    assert usual[0] == "item_power_treads"
    kb.item_popularity[8]["late_game_items"] = {"147": 5}  # hardly anyone goes on to Manta: Yasha is the item
    assert "item_yasha" in kb.typical_items(8)


def test_shopping_calls_name_the_part_then_the_finish():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = ["item_black_king_bar"]
    _feed(store, 1.0, gold=1100)
    out = list(AffordableBuildItem().evaluate(_ctx(store, kb)))
    assert [a.spoken for a in out] == ["Buy Ogre Axe"] and "a part of Black King Bar, 3,050 to go" in out[0].text
    _feed(store, 2.0, gold=1500, items=["item_ogre_axe", "item_mithril_hammer"])
    out = list(AffordableBuildItem().evaluate(_ctx(store, kb)))
    assert [a.spoken for a in out] == ["Finish Black King Bar"] and "1,450 gold covers the rest" in out[0].text
    _feed(store, 3.0, gold=4100)
    assert [a.text for a in AffordableBuildItem().evaluate(_ctx(store, kb))] == ["You can afford Black King Bar (4,050)"]
    _feed(store, 4.0, gold=2900, items=["item_yasha"])
    kb.custom_plan[JUGG] = ["item_manta"]
    out = list(AffordableBuildItem().evaluate(_ctx(store, kb)))
    assert out[0].spoken == "Finish Manta Style"  # (the orb is all that's left, and the gold covers it)
    _feed(store, 5.0, gold=700, stash=["item_yasha", "item_ultimate_orb"])
    assert list(AffordableBuildItem().evaluate(_ctx(store, kb))) == []  # all bought: the courier's job now


def test_a_shard_you_ate_is_owned_and_the_item_you_started_is_the_one_you_are_building():
    kb, store = _kb(), GameStateStore()
    kb.items["item_aghanims_shard"] = {"id": 609, "dname": "Aghanim's Shard", "cost": 1400, "components": None}
    kb.custom_plan[JUGG] = ["item_aghanims_shard", "item_black_king_bar", "item_manta"]
    snap = _feed(store, 1.0, gold=1500)
    assert kb.next_target(JUGG, 600, snap.item_list()) == "item_aghanims_shard"
    snap = _feed(store, 2.0, gold=1500, extra={"aghanims_shard": True})  # bought: it's in no slot, but it's yours
    assert "item_aghanims_shard" in snap.all_item_names()
    assert kb.next_target(JUGG, 600, snap.item_list()) == "item_black_king_bar"
    # A cheap item you skipped doesn't stay "next" once you're paying for a later one.
    snap = _feed(store, 3.0, gold=100, items=["item_blade_of_alacrity"])
    assert kb.next_target(JUGG, 600, snap.item_list()) == "item_manta"


def test_item_pace_counts_the_parts_in_your_bag():
    kb, store = _kb(), GameStateStore()
    kb.item_popularity[8] = {"mid_game_items": {"116": 60}}
    kb.item_timings[(8, "item_black_king_bar")] = [{"hero_id": 8, "time": 1200, "games": 100, "wins": 50}]
    _feed(store, 1.0, clock=1100, gold=1000, items=["item_ogre_axe", "item_mithril_hammer"])
    out = list(ItemPace().evaluate(_ctx(store, kb)))
    assert len(out) == 1 and "450 gold short" in out[0].text  # 1,450 left to pay, not 4,050


def test_late_game_shopping_calls_leave_buyback_alone():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = ["item_black_king_bar"]
    late = {"buyback_cost": 2000}
    _feed(store, 1.0, clock=BUYBACK_FROM + 60, gold=2800, extra=late)
    assert list(AffordableBuildItem().evaluate(_ctx(store, kb))) == []  # 800 over buyback: no part worth it
    assert list(UnspentGold().evaluate(_ctx(store, kb))) == []
    _feed(store, 2.0, clock=BUYBACK_FROM - 60, gold=2800, extra=late)
    assert [a.spoken for a in UnspentGold().evaluate(_ctx(store, kb))] == ["Spend your gold"]
    assert [a.spoken for a in AffordableBuildItem().evaluate(_ctx(store, kb))] == ["Buy Mithril Hammer"]


# the coach isn't allowed to sell you what you have -----------------------------------------------------------
def test_buy_mentions_tell_buying_from_using():
    kb = _kb()
    assert kb.buy_mentions("Buy Power Treads now, then head to lane.") == {"item_power_treads"}
    assert kb.buy_mentions("Buy Manta Style after Black King Bar.") == {"item_manta"}
    assert kb.buy_mentions("Get out and use Black King Bar.") == set()
    for not_a_buy in ("Finish Manta Style from your Yasha.", "Build your Yasha into Manta Style.",
                      "Finish the tower, Black King Bar keeps you alive.", "Get value from Black King Bar: fight now."):
        assert "item_yasha" not in kb.buy_mentions(not_a_buy) and "item_black_king_bar" not in kb.buy_mentions(not_a_buy)
    assert kb.buy_mentions("Farm until Black King Bar is done, then buy Yasha and Manta Style.") == {
        "item_yasha", "item_manta"}


def test_a_tip_to_buy_what_you_have_or_second_boots_is_dropped():
    kb, store = _kb(), GameStateStore()
    coach = Coach(CoachConfig(), store, AdviceBus(), kb)
    snap = _feed(store, 1.0, items=["item_phase_boots", "item_manta"])
    assert coach._already_have("Buy Power Treads now, then head to lane.", snap) == {"item_power_treads"}
    assert coach._already_have("Finish Yasha first.", snap) == {"item_yasha"}  # (it's inside the Manta)
    assert coach._already_have("Use Manta Style to dodge the stun.", snap) == set()
    assert "already have" in coach._vet_tip("watch", "Buy Power Treads now.", snap, snap, time.perf_counter(), True)
    s = kb.summary_for_prompt(JUGG, 300, snap.all_item_names())
    assert "Power Treads" not in s["popular_next_items"] and "Manta Style" not in s.get("typical_items", [])


# prepared calls ----------------------------------------------------------------------------------------------------
def test_afford_counts_parts_and_the_same_words_arent_said_twice_in_a_row():
    kb, store = _kb(), GameStateStore()
    coach = Coach(CoachConfig(), store, AdviceBus(), kb)
    book = tl.TriggerBook()
    line = "TRIGGER: afford black_king_bar => Finish Black King Bar now."
    assert tl.parse(line)[0].clauses[0].kind == "afford"
    held = ["item_ogre_axe", "item_mithril_hammer"]
    book.arm(tl.parse(line, clock=600))
    assert book.check(_feed(store, 1.0, gold=1000, items=held), store, afford=coach._afford(store.current)) == []
    snap = _feed(store, 2.0, gold=1500, items=held)
    assert [a.text for a in book.check(snap, store, afford=coach._afford(snap))] == ["Finish Black King Bar now."]
    # The AI arms the same call again; the gold dips and comes back: not said a second time so soon.
    tl_gap = tl.MIN_GAP_S
    tl.MIN_GAP_S = 0.0
    try:
        book.arm(tl.parse(line, clock=620))
        book.check(_feed(store, 3.0, clock=620, gold=900, items=held), store, afford=coach._afford(store.current))
        again = _feed(store, 4.0, clock=640, gold=1600, items=held)
        assert book.check(again, store, afford=coach._afford(again)) == [] and book.active == []
        book.arm(tl.parse(line, clock=900))
        book.check(_feed(store, 5.0, clock=900, gold=900, items=held), store, afford=coach._afford(store.current))
        later = _feed(store, 6.0, clock=920, gold=1600, items=held)
        assert len(book.check(later, store, afford=coach._afford(later))) == 1  # minutes later it may
    finally:
        tl.MIN_GAP_S = tl_gap
    assert book.check(_feed(store, 7.0, gold=5000), store) == []  # (no item data given: an afford call waits)


def test_a_prepared_call_for_something_bought_since_is_dropped_when_it_fires():
    kb, store, bus = _kb(), GameStateStore(), AdviceBus()
    said = []
    bus.subscribe(said.append)
    coach = Coach(CoachConfig(), store, bus, kb)
    coach.enabled = False  # (prepared calls need no AI)
    snap = _feed(store, 1.0, gold=500)
    coach.on_snapshot(snap)
    coach.triggers.arm(tl.parse("TRIGGER: gold >= 1400 => Buy Power Treads now.", clock=600))
    coach.on_snapshot(_feed(store, 2.0, gold=600))
    coach.on_snapshot(_feed(store, 3.0, gold=1500, items=["item_phase_boots"]))  # bought Phase Boots meanwhile
    assert said == []


# a burst, before you're low ------------------------------------------------------------------------------------
def test_a_burst_with_nothing_to_press_says_get_out():
    store = GameStateStore()
    _feed(store, 10.0, hp=1000, pos=(2000, 1500))
    _feed(store, 11.0, hp=900, pos=(2000, 1500))
    _feed(store, 12.0, hp=560, pos=(2000, 1500))  # 44% gone in two seconds, 56% left: no save item
    assert list(SaveItems().evaluate(_ctx(store))) == []
    out = list(LowHp().evaluate(_ctx(store)))
    assert [a.spoken for a in out] == ["Get out now"] and out[0].priority == Priority.CRITICAL
    assert out[0].key == "low_hp"  # one call per fight with "Back off, low HP"

    huskar = GameStateStore()
    for t, hp in ((10.0, 1000), (12.0, 560)):
        _feed(huskar, t, hp=hp, hero="npc_dota_hero_huskar", hid=59, pos=(2000, 1500))
    assert list(LowHp().evaluate(_ctx(huskar))) == []  # he spends his own HP

    slow = GameStateStore()
    for t, hp in ((10.0, 1000), (12.0, 850)):
        _feed(slow, t, hp=hp, pos=(2000, 1500))
    assert list(LowHp().evaluate(_ctx(slow))) == []  # a trade, not a burst


def test_get_out_is_said_once_a_fight_and_only_when_you_can_act_on_it():
    def burst(store, t, extra=None):
        _feed(store, t, hp=1000, pos=(2000, 1500))
        _feed(store, t + 2.0, hp=560, pos=(2000, 1500), extra=extra)
        return [a.spoken for a in LowHp().evaluate(_ctx(store))]

    store = GameStateStore()
    assert burst(store, 10.0) == ["Get out now"]
    assert burst(store, 14.0) == []  # the same fight: not again
    assert burst(GameStateStore(), 10.0, {"stunned": True}) == []  # nothing you can do about it
    assert burst(GameStateStore(), 10.0, {"magicimmune": True}) == []  # BKB is on: you chose to stay in
    saved = GameStateStore()
    _feed(saved, 5.0, pos=(2000, 1500))
    saved.memory["save_call_t"] = time.monotonic() - 8  # "BKB now" was just said this fight
    assert burst(saved, 10.0) == []


def test_crossing_the_river_is_fine_while_two_of_them_are_dead():
    store = GameStateStore()
    _feed(store, 1.0, pos=(2500, 1500), tp=False)
    store.memory["screen"] = {"t": time.monotonic(), "match": "7", "dead": {"radiant": [], "dire": ["a", "b"]}}
    assert list(GankRisk().evaluate(_ctx(store))) == []
    store.memory["screen"]["dead"]["dire"] = ["a"]
    assert [a.spoken for a in GankRisk().evaluate(_ctx(store))] == ["Head back, no TP ready"]


# where you are, and which role you play -------------------------------------------------------------------------
def test_where_you_are_in_words():
    assert mapinfo.describe((-1800, -6400), "radiant") == "your side of the map, bottom lane (your safe lane's side)"
    assert mapinfo.describe((0, 0), "radiant") == "at the river, middle lane"
    assert mapinfo.describe((-6000, 3000), "radiant") == "your side of the map, top lane (your offlane's side)"
    assert mapinfo.describe((-4700, 5900), "dire") == "your side of the map, top lane (your safe lane's side)"
    assert mapinfo.describe((6000, 5500), "radiant") == "inside the enemy base"
    assert mapinfo.describe((-7000, -6500), "radiant") == "in your base"
    assert mapinfo.describe(None, "radiant") == "" and mapinfo.describe((0, 0), "") == ""
    assert mapinfo.depth((2000, 1000), "radiant") == 3000 and mapinfo.depth((2000, 1000), "dire") == -3000


def _lane(store, kb, hero, hid, pos, lh_per_min, until=300, items=()):
    for clock in range(0, until + 1):
        snap = _feed(store, float(clock), clock=clock, hero=hero, hid=hid, pos=pos, lh=int(clock / 60 * lh_per_min),
                     items=items)
        roles.observe(snap, store, kb)
    return store.memory.get("role")


def test_the_role_comes_from_the_lane_and_the_last_hits():
    kb = _kb()
    carry = _lane(GameStateStore(), kb, JUGG, 8, (-1800, -6400), 5.0)
    assert (carry.position, carry.lane, carry.core, carry.sure) == (1, "safe", True, True)
    assert carry.label == "safe-lane carry (position 1)"
    support = _lane(GameStateStore(), kb, LION, 26, (-1800, -6400), 0.8, items=["item_ward_sentry"])
    assert (support.position, support.core, support.support) == (5, False, True)
    mid = _lane(GameStateStore(), kb, "npc_dota_hero_tidehunter", 29, (-600, -500), 4.0)
    assert mid.position == 2
    off = _lane(GameStateStore(), kb, "npc_dota_hero_tidehunter", 29, (-6200, 2500), 3.0)
    assert off.position == 3 and off.lane == "off"
    soft = _lane(GameStateStore(), kb, LION, 26, (-6200, 2500), 0.5)
    assert soft.position == 4
    early = _lane(GameStateStore(), kb, LION, 26, (-6900, -6400), 0, until=20)
    assert early.core is False and not early.sure and early.label == "a support, a first guess"
    assert not early.support  # a first guess silences nothing


def test_a_core_losing_the_lane_isnt_taken_for_a_support():
    """Few last hits alone don't make a support: it takes support purchases or a support hero."""
    kb = _kb()
    carry = _lane(GameStateStore(), kb, JUGG, 8, (-1800, -6400), 1.1)  # 5 last hits at 5:00: a rough lane
    assert carry.position == 1 and carry.core
    tide = _lane(GameStateStore(), kb, "npc_dota_hero_tidehunter", 29, (-6200, 2500), 1.6)
    assert tide.position == 4 and not tide.sure and not tide.support  # maybe a support: a guess, nothing silenced
    assert tide.label == "soft support (position 4), a first guess"
    store = GameStateStore()
    _lane(store, kb, "npc_dota_hero_tidehunter", 29, (-6200, 2500), 1.6, until=301)
    kb.benchmarks[29] = {"result": {"last_hits_per_min": [{"percentile": 0.5, "value": 6.0}]}}
    assert [a.key for a in LaningTips().evaluate(_ctx(store, kb))] == ["lane_pace"]  # still told about the farm


def test_farm_reminders_arent_for_supports():
    kb = _kb()
    store = GameStateStore()
    _lane(store, kb, LION, 26, (-1800, -6400), 0.4, until=301, items=["item_ward_sentry"])
    assert store.memory["role"].support
    assert [a.key for a in LaningTips().evaluate(_ctx(store, kb))] == []  # 2 last hits at 5:00, and that's fine
    _feed(store, 601.0, clock=601, hero=LION, hid=26, lh=4)
    assert list(FarmCheck().evaluate(_ctx(store, kb))) == []
    core = GameStateStore()
    _lane(core, kb, JUGG, 8, (-1800, -6400), 2.2, until=301)
    assert [a.key for a in LaningTips().evaluate(_ctx(core, kb))] == ["lane_pace"]  # a carry far behind is told


# what the coach is told ------------------------------------------------------------------------------------------------
def test_the_coach_is_told_where_what_role_what_is_coming_and_what_the_next_item_costs():
    kb, store = _kb(), GameStateStore()
    kb.custom_plan[JUGG] = ["item_black_king_bar"]
    _lane(store, kb, JUGG, 8, (-1800, -6400), 5.0)
    bkb = {"name": "item_phase_boots", "cooldown": 6.2}
    wand = {"name": "item_magic_wand", "charges": 14}
    snap = _feed(store, 400.0, clock=330, gold=550, gpm=600, items=[bkb, wand, "item_ogre_axe", "item_mithril_hammer"])
    s = prompts.state_summary(snap, store, kb, [])
    assert s["where"].startswith("your side of the map, bottom lane") and s["role"] == "safe-lane carry (position 1)"
    assert s["tp"] == "ready" and s["items"][:2] == ["Phase Boots (cd 6s)", "Magic Wand (14 charges)"]
    assert s["coming_up"][0].startswith("Power rune") and "Wisdom shrines 7:00" in s["coming_up"]
    assert s["next_item"] == {"item": "Black King Bar", "gold_left": 900, "eta": "about 7:00 at this gold per minute",
                              "after_it": ["Manta Style"]}
    snap = _feed(store, 401.0, clock=331, gold=2000, items=["item_ogre_axe", "item_mithril_hammer"], tp=False)
    s = prompts.state_summary(snap, store, kb, [])
    assert s["next_item"]["eta"] == "affordable now" and s["tp"] == "none"
    late = _feed(store, 402.0, clock=BUYBACK_FROM + 5, gold=2000, items=["item_ogre_axe", "item_mithril_hammer"],
                 extra={"buyback_cost": 1500})
    assert prompts.next_item(late, kb)["gold_left"] == 950  # 1,450 to pay, and only 500 on top of buyback
    assert prompts.coming_up(-20) == ["Bounty runes 0:00", "Water runes 2:00"]


def test_the_match_brief_names_the_answers_and_the_allies_big_spells():
    kb = _kb()
    kb.hero_abilities = {"npc_dota_hero_tidehunter": {"abilities": ["tidehunter_gush", "tidehunter_ravage"]}}
    kb.ability_info = {"tidehunter_gush": {"dname": "Gush", "cd": 12.0}, "tidehunter_ravage": {"dname": "Ravage", "cd": 150.0}}
    lineup = {"radiant": ["Juggernaut", "Tidehunter"], "dire": ["Phantom Assassin", "Lion"]}
    brief = prompts.match_brief(lineup, "radiant", JUGG, kb, complete=False)
    answers = next(line for line in brief.splitlines() if line.startswith("Answers to their lineup"))
    # Monkey King Bar (which Juggernaut's players buy) is there; Shiva's Guard (which they don't) isn't.
    assert "Monkey King Bar (evasion (your attacks miss): Phantom Assassin)" in answers
    assert "Shiva's Guard" not in answers
    assert "Allies' big spells: Tidehunter: Ravage (150 s)." in brief
    assert "afford black_king_bar" in prompts.SYSTEM_PROMPT and "item_name" not in prompts.SYSTEM_PROMPT
