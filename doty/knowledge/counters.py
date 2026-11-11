"""What beats what: hero traits and the items that answer them.

A coach's itemization knowledge, written down so the app can show it before and during a game, not
only the AI. Hero names are the in-game English names (OpenDota `localized_name`).
tests/test_draft.py checks that every name resolves to a real hero.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Trait:
    key: str
    title: str  # what the enemy has, in a few words
    items: tuple[str, ...]  # what to buy against it (dotaconstants item keys)
    heroes: frozenset[str]


TRAITS = (
    Trait("lockdown", "Stuns, silences and hexes", ("black_king_bar", "sphere", "lotus_orb", "glimmer_cape", "force_staff"),
          frozenset({"Lion", "Shadow Shaman", "Bane", "Silencer", "Disruptor", "Earthshaker", "Magnus", "Tidehunter",
                     "Enigma", "Faceless Void", "Legion Commander", "Doom", "Batrider", "Puck", "Skywrath Mage",
                     "Rubick", "Dark Willow", "Jakiro", "Sand King", "Centaur Warrunner", "Mars", "Lina"})),
    Trait("magic", "Big magic damage", ("pipe", "black_king_bar", "glimmer_cape", "eternal_shroud", "hood_of_defiance"),
          frozenset({"Lina", "Lion", "Zeus", "Skywrath Mage", "Leshrac", "Tinker", "Queen of Pain", "Invoker", "Pugna",
                     "Nyx Assassin", "Death Prophet", "Shadow Fiend", "Necrophos", "Witch Doctor", "Jakiro", "Ancient Apparition"})),
    Trait("physical", "Heavy right-click damage", ("assault", "shivas_guard", "crimson_guard", "ghost", "heavens_halberd", "blade_mail"),
          frozenset({"Phantom Assassin", "Troll Warlord", "Juggernaut", "Sven", "Ursa", "Slark", "Faceless Void",
                     "Lifestealer", "Monkey King", "Drow Ranger", "Sniper", "Chaos Knight", "Wraith King", "Luna",
                     "Medusa", "Templar Assassin", "Gyrocopter", "Muerta"})),
    Trait("evasion", "Evasion (your attacks miss)", ("monkey_king_bar", "bloodthorn", "javelin"),
          frozenset({"Phantom Assassin", "Windranger", "Brewmaster", "Monkey King"})),
    Trait("healing", "Strong healing or regen", ("spirit_vessel", "skadi", "orb_of_corrosion"),  # (Shiva's: not since 7.41)
          frozenset({"Necrophos", "Alchemist", "Huskar", "Morphling", "Dazzle", "Oracle", "Omniknight", "Abaddon",
                     "Treant Protector", "Witch Doctor", "Lifestealer", "Undying", "Chen", "Io", "Warlock"})),
    Trait("passives", "Passives that carry them", ("silver_edge",),
          frozenset({"Phantom Assassin", "Bristleback", "Spectre", "Huskar", "Mars", "Timbersaw", "Faceless Void",
                     "Slardar", "Anti-Mage", "Ursa", "Viper", "Medusa", "Axe", "Spirit Breaker", "Tiny"})),
    Trait("invisible", "Invisibility", ("dust", "ward_sentry", "gem"),
          frozenset({"Riki", "Clinkz", "Bounty Hunter", "Weaver", "Nyx Assassin", "Templar Assassin", "Slark",
                     "Mirana", "Invoker", "Treant Protector", "Sand King", "Hoodwink"})),
    Trait("slippery", "Slippery: blinks, jumps and escapes", ("orchid", "sheepstick", "abyssal_blade", "nullifier", "rod_of_atos", "gungir"),
          frozenset({"Anti-Mage", "Queen of Pain", "Storm Spirit", "Ember Spirit", "Puck", "Void Spirit", "Weaver",
                     "Slark", "Morphling", "Mirana", "Earth Spirit", "Pangolier", "Phoenix", "Faceless Void", "Riki"})),
    Trait("illusions", "Illusions and summons", ("mjollnir", "maelstrom", "bfury", "shivas_guard", "radiance", "crimson_guard"),
          frozenset({"Phantom Lancer", "Naga Siren", "Terrorblade", "Chaos Knight", "Lone Druid", "Broodmother", "Meepo",
                     "Arc Warden", "Nature's Prophet", "Lycan", "Visage", "Enigma", "Beastmaster", "Invoker"})),
)


def traits_of(hero_name: str) -> list[Trait]:
    return [t for t in TRAITS if hero_name in t.heroes]


def counter_items(enemy_names: list[str]) -> list[tuple[str, list[str], list[str]]]:
    """[(item key, why (trait titles), which enemies)], most-needed first: an item that answers
    several enemies at once comes before a niche one."""
    score: dict[str, list] = {}
    for name in enemy_names:
        for t in traits_of(name):
            for rank, item in enumerate(t.items):
                why, who, weight = score.setdefault(item, [[], [], 0.0])
                if t.title not in why:
                    why.append(t.title)
                if name not in who:
                    who.append(name)
                score[item][2] = weight + 1.0 / (1 + rank)
    ordered = sorted(score.items(), key=lambda kv: -kv[1][2])
    return [(item, why, who) for item, (why, who, _w) in ordered]
