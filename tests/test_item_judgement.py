"""Item judgement for the match being played. From a real Turbo match on Spirit Breaker: Black King Bar was pushed
57 times against Juggernaut and Bristleback, whose damage is physical and goes through it (Omnislash, Quill
Spray). Now each counter item has a case against the lineup, computed from the enemies' spells, and the build
follows it; and the parts the player holds decide what they're building, in Doty's plan or not."""

from doty.coach import prompts
from doty.knowledge import build, judgement
from doty.knowledge.constants import ability_entry
from doty.lineup import Lineup
from doty.state import GameStateStore

from .test_game_sense import JUGG, LION, _feed
from .test_own_build import _kb as _own_build_kb

BKB, BEARING = "item_black_king_bar", "item_boots_of_bearing"
BB, LINA, BANE = "npc_dota_hero_bristleback", "npc_dota_hero_lina", "npc_dota_hero_bane"
TROLL, SNIPER, URSA, PA = ("npc_dota_hero_troll_warlord", "npc_dota_hero_sniper", "npc_dota_hero_ursa",
                           "npc_dota_hero_phantom_assassin")


def _ab(dname, behavior, team=None, dmg=None, bkb="No", dispel=None, desc="", headers=()):
    """An ability as dotaconstants' abilities.json has it (synthetic, shaped like the real entries)."""
    return {"dname": dname, "behavior": behavior, "target_team": team, "dmg_type": dmg, "bkbpierce": bkb,
            "dispellable": dispel, "desc": desc, "attrib": [{"header": h} for h in headers]}


ABILITIES = {
    LION: {
        "lion_impale": _ab("Earth Spike", ["Point Target", "Unit Target"], "Enemy", "Magical", dispel="Strong Dispels Only",
                           desc="Enemy units are hurled into the air, then are stunned and take damage.",
                           headers=["DAMAGE:", "STUN DURATION:"]),
        "lion_voodoo": _ab("Hex", "Unit Target", "Enemy", dispel="Strong Dispels Only",
                           desc="Transforms an enemy unit into a harmless beast, with all special abilities disabled."),
        "lion_finger_of_death": _ab("Finger of Death", ["Unit Target", "AOE"], "Enemy", "Magical",
                                    desc="Rips at an enemy unit, dealing massive damage."),
    },
    LINA: {
        "lina_dragon_slave": _ab("Dragon Slave", ["Unit Target", "Point Target"], "Enemy", "Magical",
                                 desc="Sends out a wave of fire that scorches every enemy in its path."),
        "lina_light_strike_array": _ab("Light Strike Array", ["Point Target", "AOE"], None, "Magical",
                                       dispel="Strong Dispels Only", desc="Summons a column of flames that damages and "
                                       "stuns enemies.", headers=["STUN DURATION:"]),
        "lina_laguna_blade": _ab("Laguna Blade", "Unit Target", "Enemy", "Magical",
                                 desc="Fires off a bolt of lightning at a single enemy unit, dealing massive damage."),
    },
    BANE: {
        "bane_enfeeble": _ab("Enfeeble", "Unit Target", "Enemy", "Pure", dispel="Yes",
                             desc="Deals damage every second and lowers the enemy's attack damage."),
        "bane_nightmare": _ab("Nightmare", "Unit Target", "Both", dispel="Yes",
                              desc="Puts the target enemy or friendly Hero to sleep."),
        "bane_fiends_grip": _ab("Fiend's Grip", ["Unit Target", "Channeled"], "Enemy", "Pure", bkb="Yes",
                                dispel="Strong Dispels Only", desc="Grips an enemy unit, disabling it and causing heavy "
                                "damage over time."),
    },
    JUGG: {
        "juggernaut_blade_fury": _ab("Blade Fury", ["No Target", "Instant Cast"], None, "Magical", dispel="No",
                                     desc="Causes a bladed whirlwind of destruction around Juggernaut."),
        "juggernaut_healing_ward": _ab("Healing Ward", ["AOE", "Point Target"], bkb="Yes",
                                       desc="Summons a Healing Ward which heals all nearby allied units."),
        "juggernaut_blade_dance": _ab("Blade Dance", "Passive", bkb=None, desc="Gives Juggernaut a chance to deal "
                                      "critical damage on each attack.", headers=["BLADE DANCE LIFESTEAL:"]),
        "juggernaut_swift_slash": _ab("Swiftslash", "Unit Target", "Enemy", "Physical", bkb="Yes", dispel="No",
                                      desc="Juggernaut performs a short Omnislash."),
        "juggernaut_omni_slash": _ab("Omnislash", "Unit Target", "Enemy", "Physical", bkb="Yes", dispel="No",
                                     desc="Juggernaut leaps towards the target enemy unit, then slashes it repeatedly."),
    },
    BB: {
        "bristleback_viscous_nasal_goo": _ab("Viscous Nasal Goo", "Unit Target", "Enemy", dispel="Yes",
                                             desc="Covers a target in snot, reducing armor and slowing it."),
        "bristleback_quill_spray": _ab("Quill Spray", ["No Target", "Instant Cast"], None, "Physical", bkb="Yes",
                                       dispel="No", desc="Sprays enemy units with quills dealing damage."),
        "bristleback_bristleback": _ab("Bristleback", "Passive", bkb=None, desc="Bristleback takes less damage "
                                       "from the side and back."),
        "bristleback_warpath": _ab("Warpath", "Passive", bkb=None, dispel="No",
                                   desc="Bristleback works himself up into a fury every time he casts a spell."),
    },
    TROLL: {
        "troll_warlord_fervor": _ab("Fervor", "Passive", bkb=None, desc="Troll gains attack speed on each attack."),
        "troll_warlord_battle_trance": _ab("Battle Trance", "No Target", "Friendly", dispel="No",
                                           desc="Troll lets out a battle cry that grants attack speed."),
    },
    SNIPER: {
        "sniper_take_aim": _ab("Take Aim", "Passive", bkb=None, desc="Increases attack range."),
        "sniper_assassinate": _ab("Assassinate", "Unit Target", "Enemy", "Magical",
                                  desc="Locks onto a target enemy unit and fires a devastating shot."),
    },
    URSA: {
        "ursa_overpower": _ab("Overpower", "No Target", None, dispel="No", desc="Ursa gains bonus attack speed."),
        "ursa_fury_swipes": _ab("Fury Swipes", "Passive", "Enemy", "Physical", bkb="Yes",
                                desc="Each attack against the same target deals more damage."),
    },
    PA: {
        "phantom_assassin_stifling_dagger": _ab("Stifling Dagger", "Unit Target", "Enemy", "Physical", bkb=None,
                                                dispel="Yes", desc="Throws a dagger slowing the enemy unit."),
        "phantom_assassin_blur": _ab("Blur", "No Target", "Friendly", dispel="No",
                                     desc="Phantom Assassin blurs her body."),
        "phantom_assassin_immaterial": _ab("Immaterial", "Passive", bkb=None,
                                           desc="Phantom Assassin focuses inward, increasing her ability to evade "
                                           "enemy attacks."),
        "phantom_assassin_coup_de_grace": _ab("Coup de Grace", "Passive", bkb="Yes",
                                              desc="A chance to deal a critical hit."),
    },
}

RIGHT_CLICK = [JUGG, BB, TROLL, SNIPER, URSA]  # the match's kind of lineup: almost nothing Black King Bar blocks
SPELLS = [LION, LINA, BANE, TROLL, SNIPER]  # stuns, a hex, a sleep


def _kb():
    kb = _own_build_kb()
    kb.heroes.update({
        BB: {"id": 99, "localized_name": "Bristleback", "roles": ["Carry", "Durable"], "attack_type": "Melee",
             "primary_attr": "str"},
        LINA: {"id": 25, "localized_name": "Lina", "roles": ["Support", "Carry", "Nuker"], "attack_type": "Ranged"},
        BANE: {"id": 3, "localized_name": "Bane", "roles": ["Support", "Disabler"], "attack_type": "Ranged"},
        TROLL: {"id": 95, "localized_name": "Troll Warlord", "roles": ["Carry"], "attack_type": "Ranged"},
        SNIPER: {"id": 35, "localized_name": "Sniper", "roles": ["Carry", "Nuker"], "attack_type": "Ranged"},
        URSA: {"id": 70, "localized_name": "Ursa", "roles": ["Carry", "Durable"], "attack_type": "Melee"},
    })
    kb.heroes[JUGG]["attack_type"] = "Melee"
    kb.hero_abilities = {npc: {"abilities": list(abs_)} for npc, abs_ in ABILITIES.items()}
    kb.ability_info = {name: ability_entry(raw) for abs_ in ABILITIES.values() for name, raw in abs_.items()}

    def item(i, name, cost, parts=None):
        return {"id": i, "dname": name, "cost": cost, "components": parts}

    kb.items.update({f"item_{k}": v for k, v in {
        "sphere": item(123, "Linken's Sphere", 4800), "lotus_orb": item(226, "Lotus Orb", 3850),
        "ghost": item(37, "Ghost Scepter", 1500), "heavens_halberd": item(210, "Heaven's Halberd", 3550),
        "cyclone": item(100, "Eul's Scepter of Divinity", 2625), "pipe": item(90, "Pipe of Insight", 3725),
        "glimmer_cape": item(254, "Glimmer Cape", 2150), "spirit_vessel": item(267, "Spirit Vessel", 2780),
        "force_staff": item(102, "Force Staff", 2200), "nullifier": item(225, "Nullifier", 4375),
        "belt_of_strength": item(14, "Belt of Strength", 450),
        "sange": item(162, "Sange", 2050, ["ogre_axe", "belt_of_strength"]),  # (an Ogre Axe goes into two items)
    }.items()})
    return kb


# the case for an item, from the enemies' spells ----------------------------------------------------------------
def test_black_king_bar_is_weak_against_right_clickers_whose_spells_go_through_it():
    kb = _kb()
    case = kb.item_case(BKB, RIGHT_CLICK)
    assert case is not None and case.score < judgement.WEAK and case.weak
    # The reason is factual: what it blocks, and what goes through it, by name.
    assert "Omnislash" in case.reason and "Quill Spray" in case.reason and "go through it" in case.reason
    assert ("Juggernaut", "Blade Fury") in case.answers and ("Juggernaut", "Omnislash") not in case.answers


def test_black_king_bar_is_strong_against_stuns_hexes_and_sleeps():
    kb = _kb()
    case = kb.item_case("black_king_bar", ["Lion", "Lina", "Bane", "Troll Warlord", "Sniper"])  # (names work too)
    assert case.score >= judgement.STRONG and case.strong
    assert case.reason.startswith("it blocks Lion's Earth Spike and Hex")
    assert "Lina's Light Strike Array" in case.reason and "Bane's Nightmare" in case.reason
    assert "Fiend's Grip goes through it" in case.reason  # (it does: the data says so)
    assert ("Lion", "Hex") in case.answers and ("Bane", "Fiend's Grip") not in case.answers


def test_the_other_counter_items_follow_the_data():
    kb = _kb()
    spells, right_click = SPELLS, RIGHT_CLICK
    # Single-target spells (Linken's, Lotus) count even when they go through Black King Bar (Fiend's Grip).
    linkens = kb.item_case("item_sphere", spells)
    assert linkens.strong and ("Bane", "Fiend's Grip") in linkens.answers and ("Lion", "Earth Spike") not in linkens.answers
    assert kb.item_case("item_sphere", right_click).score < kb.item_case("item_sphere", spells).score
    # A dispel on yourself removes Nightmare, not Hex (strong dispels only).
    euls = kb.item_case("item_cyclone", spells)
    assert ("Bane", "Nightmare") in euls.answers and ("Lion", "Hex") not in euls.answers
    # Magic damage: Pipe and Glimmer against the nukers, nothing against the right-clickers.
    assert kb.item_case("item_pipe", spells).score > 0.6 > kb.item_case("item_pipe", right_click).score
    # Against right-click carries, Ghost Scepter and Halberd are what's worth it.
    ghost = kb.item_case("item_ghost", right_click)
    assert ghost.strong and ("Juggernaut", "") in ghost.answers and "Juggernaut" in ghost.reason
    assert kb.item_case("item_heavens_halberd", right_click).strong
    # Break: Bristleback's passives, named from the data.
    silver = kb.item_case("item_silver_edge", right_click)
    assert ("Bristleback", "Bristleback") in silver.answers and "breaks Bristleback's" in silver.reason
    # Evasion: Phantom Assassin's Immaterial, from its description.
    mkb = kb.item_case("item_monkey_king_bar", [PA, BB])
    assert ("Phantom Assassin", "Immaterial") in mkb.answers and mkb.score >= judgement.STRONG
    assert kb.item_case("item_monkey_king_bar", right_click).score == 0
    assert kb.item_case("item_monkey_king_bar", right_click).reason == "none of them has evasion"
    # Items nobody buys for the lineup have no case.
    assert kb.item_case("item_power_treads", spells) is None and kb.item_case("item_manta", spells) is None


def test_ability_facts_read_the_raw_data():
    facts = judgement.ability_facts(ABILITIES[LION]["lion_voodoo"])
    assert facts["disable"] == "hex" and facts["unit"] and facts["hostile"]
    assert judgement.ability_facts(ABILITIES[LINA]["lina_light_strike_array"])["disable"] == "stun"
    assert not judgement.ability_facts(ABILITIES[LINA]["lina_light_strike_array"])["unit"]
    assert judgement.ability_facts(ABILITIES[BANE]["bane_nightmare"])["disable"] == "sleep"
    # A save cast on a friend isn't a disable ("Removes debuffs and disables").
    save = _ab("Press The Attack", "Unit Target", "Friendly", dispel="Yes", desc="Removes debuffs and disables "
               "from the target friendly unit.")
    assert judgement.ability_facts(save)["disable"] == "" and judgement.ability_facts(save)["self_buff"]
    assert judgement.ability_facts(ABILITIES[PA]["phantom_assassin_immaterial"])["evasion"]


# the build follows the case -----------------------------------------------------------------------------------
def test_a_weak_counter_item_isnt_suggested_once_the_lineup_is_known():
    """The match: Black King Bar again and again against Juggernaut and Bristleback."""
    kb = _kb()
    kb.custom_plan[JUGG] = [BKB, "item_manta"]  # (the coach's plan said it too)
    assert kb.next_target(JUGG, 1000, ["item_power_treads"]) == BKB  # (lineup unknown: as before)
    kb.set_enemies(RIGHT_CLICK[1:], complete=False)
    assert kb.next_target(JUGG, 1000, ["item_power_treads"]) == BKB  # (four of five: still as before)
    kb.set_enemies(RIGHT_CLICK[1:] + [LION], complete=True)
    assert kb.lineup_complete
    kb.set_enemies([BB, TROLL, SNIPER, URSA, "npc_dota_hero_phantom_assassin"], complete=True)
    assert BKB not in kb.build_plan(JUGG, 1000)
    assert kb.next_target(JUGG, 1000, ["item_power_treads"]) != BKB
    assert BKB not in [e["item"] for e in kb.shopping(JUGG, 1000, ["item_power_treads"], 5000, n=6)]
    said = kb.summary_for_prompt(JUGG, 1000, {"item_power_treads"})
    assert "Black King Bar" not in str(said["popular_next_items"]) + str(said.get("typical_items"))
    assert said["counter_item_cases"]["Black King Bar"].startswith("weak (")


def test_a_strong_counter_item_the_plan_lacks_is_offered_with_its_reason():
    kb = _kb()
    kb.item_popularity[8]["late_game_items"] = {"147": 60, "249": 12}  # (some Juggernauts buy Silver Edge)
    plan = kb.build_plan(JUGG, 1000)
    assert "item_silver_edge" not in plan
    kb.set_enemies([BB, PA, TROLL, SNIPER, URSA], complete=True)  # two heroes whose passives carry them
    plan = kb.build_plan(JUGG, 1000)
    assert plan[1] == "item_silver_edge"  # after the item at the head of the plan
    entries = kb.shopping(JUGG, 1000, ["item_power_treads"], 0, n=4)
    edge = next(e for e in entries if e["item"] == "item_silver_edge")
    assert edge["why"].startswith("it breaks ") and "Bristleback" in edge["why"]
    said = kb.summary_for_prompt(JUGG, 1000, {"item_power_treads"})
    assert said["counter_item_cases"]["Silver Edge"].startswith("strong (1.0): it breaks")


def test_the_coach_is_told_the_cases_and_the_system_prompt_says_how_to_use_them():
    kb, store = _kb(), GameStateStore()
    kb.set_enemies([LION, LINA], complete=False)  # (strong cases are facts about the known heroes)
    snap = _feed(store, 1.0, clock=1000, items=["item_power_treads"])
    said = prompts.state_summary(snap, store, kb, [])
    assert said["counter_item_cases"]["Black King Bar"].startswith("strong")
    assert "Lion's Earth Spike and Hex" in said["counter_item_cases"]["Black King Bar"]
    kb.set_enemies(RIGHT_CLICK[1:] + [PA], complete=True)
    assert prompts.state_summary(snap, store, kb, [])["counter_item_cases"]["Black King Bar"].startswith("weak")
    assert '"counter_item_cases"' in prompts.SYSTEM_PROMPT and "Never argue for an item whose case is" in prompts.SYSTEM_PROMPT


def test_the_lineup_comes_from_the_top_bar_votes():
    kb, store = _kb(), GameStateStore()
    build.observe(_feed(store, 1.0, clock=10, items=[]), store, kb)
    assert kb.enemies == () and not kb.lineup_complete
    votes = store.memory["lineup_votes"] = Lineup()
    for _ in range(2):  # (a hero counts once two reads agree)
        votes.add([JUGG, LION], [BB, TROLL, SNIPER, URSA, PA], own=JUGG, team="radiant")
    build.observe(_feed(store, 2.0, clock=20, items=[]), store, kb)
    assert set(kb.enemies) == {BB, TROLL, SNIPER, URSA, PA} and kb.lineup_complete
    store.reset("9")  # a new match: nobody known yet
    build.observe(_feed(store, 3.0, clock=5, items=[]), store, kb)
    assert kb.enemies == () and not kb.lineup_complete


# what the player is already building --------------------------------------------------------------------------
def test_the_parts_you_hold_decide_what_youre_building():
    """Tranquil Boots and a Drum of Endurance: Boots of Bearing, though no plan had it."""
    kb = _kb()
    owned = ["item_tranquil_boots", "item_ancient_janggo"]
    assert BEARING not in kb.build_plan(JUGG, 1500)
    assert kb.next_target(JUGG, 1500, owned) == BEARING
    first = kb.shopping(JUGG, 1500, owned, 1700)[0]
    assert first["item"] == BEARING and first["left"] == 1700 and first["finish"]  # "1,700 to finish"
    # Following it isn't passing on anything else.
    store = GameStateStore()
    build.observe(_feed(store, 1.0, clock=1500, items=owned), store, kb)
    assert build.observe(_feed(store, 2.0, clock=1600, items=owned + ["item_invis_sword"]), store, kb) == []


def test_weak_evidence_and_ties():
    kb = _kb()
    assert kb.held_target(["item_ogre_axe"]) is None  # (it goes into Black King Bar and Sange: no evidence alone)
    assert kb.held_target(["item_ogre_axe", "item_mithril_hammer"]) == BKB  # two of its parts
    assert kb.held_target(["item_recipe_yasha"]) == "item_yasha"  # a recipe goes into one item
    assert kb.held_target(["item_tranquil_boots"]) is None  # (an item in its own right, not a start on Bearing)
    # Two items started: the one with more gold in it.
    both = ["item_ogre_axe", "item_mithril_hammer", "item_blade_of_alacrity", "item_boots_of_elves"]
    assert kb.held_target(both) == BKB
    # Never an item you passed on, nor a second pair of boots.
    kb.skip(BKB)
    assert kb.held_target(both) == "item_yasha"
    assert kb.held_target(["item_phase_boots", "item_tranquil_boots", "item_ancient_janggo"]) is None


def test_held_parts_beat_a_weak_case():
    """The player is building it: Doty talks about finishing it, whatever its case."""
    kb = _kb()
    kb.set_enemies(RIGHT_CLICK, complete=True)
    owned = ["item_power_treads", "item_ogre_axe", "item_mithril_hammer"]
    assert kb.next_target(JUGG, 1000, owned) == BKB
    assert kb.shopping(JUGG, 1000, owned, 0)[0]["left"] == 1450
