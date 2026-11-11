"""Is a counter item worth it in THIS match? A case for each situational item, computed from what the enemy
heroes' spells actually do.

`counters.py` says which items answer which heroes, as a coach would list them; it can't weigh them. A real
Turbo match on Spirit Breaker: Black King Bar was pushed 57 times against Juggernaut and Bristleback, whose
damage is physical and goes through it (Omnislash, Quill Spray). So here each item gets a score from 0 to 1 and
a reason a player can check ("it blocks Lion's Earth Spike and Hex, Lina's Light Strike Array"), built from the
ability data (dotaconstants: damage type, whether it goes through Black King Bar, whether a dispel removes it,
whether it targets one unit) and, for what that data can't say (who has evasion, which passives carry a hero,
who heals), the hand-written table in `counters.py`.

Fair play: only the enemy heroes (on the player's own top bar) and public facts about their spells; never their
items or anything else the player couldn't know.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from . import counters

if TYPE_CHECKING:
    from .constants import Knowledge

# Below WEAK an item isn't suggested against this lineup (even when it's popular on the hero); from STRONG on it
# may be offered when the plan lacks it, with its reason.
WEAK = 0.3
STRONG = 0.6

# What a disable is, from its description and its numbers' labels ("STUN DURATION:"). Strongest first: an ability
# is the first kind it matches.
DISABLE_WORDS = (
    ("hex", re.compile(r"\bhex\b|transforms? (an|the) enemy|harmless (beast|creature)")),
    ("stun", re.compile(r"\bstun")),
    ("sleep", re.compile(r"\bto sleep\b|\basleep\b")),
    ("taunt", re.compile(r"\bforced to attack|\btaunt")),
    ("silence", re.compile(r"\bsilenc|cannot (use|cast) (abilities|spells)|prevents? them from casting")),
    ("fear", re.compile(r"\bfear")),
    ("root", re.compile(r"\broot|cannot move|prohibiting movement|entangl|ensnar")),
    ("cyclone", re.compile(r"\bcyclone")),
    ("disable", re.compile(r"\bdisabling\b|\btrapping\b|\block (an|the) enemy|\bdisables? (the )?(target|enemy)")),
)
# How much each kind of disable costs you: a stun or hex loses a fight, a root less so.
DISABLE_WEIGHT = {"hex": 1.0, "stun": 1.0, "disable": 1.0, "silence": 0.9, "sleep": 0.8, "taunt": 0.8,
                  "fear": 0.8, "root": 0.6, "cyclone": 0.6}
EVASION = re.compile(r"\bevasion|\bevade")
# Healing that keeps them alive: heals, health regen, lifesteal (not mana regen, not a heal they take away).
HEAL = re.compile(r"\bheal(s|ing|ed)?\b|\bhealth regen|lifesteal")
NOT_HEAL = re.compile(r"heal(ing)? reduction|reduc\w* (their |the target's )?heal|prevents?[^.]*heal|negative heal")


def _listed(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []


def ability_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """What one ability (a dotaconstants abilities.json entry) does to the other team, as the item judgement
    reads it: "dmg" (magical, physical, pure or ''), "hostile" (it hits or disables enemies), "unit" (it
    targets one unit, so Linken's Sphere or Lotus Orb can take it), "disable" (the kind: stun, hex, silence,
    root...; '' for none), "evasion", "heal" and "self_buff" (a buff on their own side a basic dispel removes)."""
    behavior = _listed(raw.get("behavior"))
    team = _listed(raw.get("target_team"))
    dmg = raw.get("dmg_type")
    dmg = dmg.lower() if isinstance(dmg, str) and dmg.lower() in ("magical", "physical", "pure") else ""
    # (Labels of yes/no switches, "DOES ROOT:", say what a variant could do, not what the spell does.)
    headers = " ".join(h for a in raw.get("attrib") or [] if isinstance(a, dict)
                       and "DOES" not in (h := str(a.get("header") or "")))
    desc = str(raw.get("desc") or "")
    text = f"{raw.get('dname') or ''} {desc} {headers}".lower()
    kind = next((k for k, pattern in DISABLE_WORDS if pattern.search(text)), "")
    if "Enemy" in team or "Both" in team:
        hostile = True
    elif "Friendly" in team:
        hostile = False
    else:  # (no target team listed: an area spell or a passive; it's hostile when it hurts or holds someone)
        hostile = bool(dmg or kind)
    if not hostile:
        kind = ""  # ("Removes debuffs and disables": a save, not a disable)
    passive = "Passive" in behavior
    dispel = str(raw.get("dispellable") or "")
    return {
        "dmg": dmg if hostile else "",
        "hostile": hostile,
        "unit": hostile and "Unit Target" in behavior and "Point Target" not in behavior,
        "disable": kind,
        "evasion": not hostile and bool(EVASION.search(text)),
        "heal": bool(HEAL.search(text)) and not NOT_HEAL.search(text),
        "self_buff": (not hostile and not passive and not dmg and dispel == "Yes"
                      and "Hidden" not in behavior and ("Friendly" in team or not team)),
    }


@dataclass
class ItemCase:
    """Whether an item is worth buying against this lineup: `score` from 0 (answers nothing of theirs) to 1,
    `reason` a short clause to use in a sentence ("it blocks Lion's Hex and Lina's Light Strike Array"), and
    `answers` the (enemy hero, ability) pairs it answers (ability '' when it answers the hero as a whole: a
    right-click carry, say)."""

    item: str
    score: float
    reason: str
    answers: list[tuple[str, str]] = field(default_factory=list)

    @property
    def weak(self) -> bool:
        return self.score < WEAK

    @property
    def strong(self) -> bool:
        return self.score >= STRONG


@dataclass
class _Spell:
    name: str
    facts: dict[str, Any]  # Knowledge.ability_info's entry: ability_facts plus "bkb", "dispel", "passive"...


@dataclass
class _Enemy:
    name: str  # display name
    roles: list[str]
    melee: bool
    right_click: bool  # a carry who kills with attacks (the table, or the Carry role on a strength or agility hero)
    traits: set[str]  # counters.py trait keys
    spells: list[_Spell]  # visible abilities (no hidden Shard or Scepter ones), one per name


def _enemy(kb: "Knowledge", npc: str) -> _Enemy:
    hero = kb.heroes.get(npc) or {}
    name = kb.hero_name(npc)
    spells, seen = [], set()
    for ability in (kb.hero_abilities.get(npc) or {}).get("abilities", []):
        info = kb.ability_info.get(ability)
        if ability == "generic_hidden" or not info or not info.get("dname") or info.get("hidden"):
            continue
        # One spell per name: Shadowraze three times, Whirling Axes (Ranged) and (Melee) once.
        label = re.sub(r"\s*\([^)]*\)$", "", info["dname"])
        if label in seen:
            continue
        seen.add(label)
        spells.append(_Spell(label, info))
    roles = list(hero.get("roles") or [])
    traits = {t.key for t in counters.traits_of(name)}
    right_click = "physical" in traits or ("Carry" in roles and hero.get("primary_attr") in ("str", "agi", "all"))
    return _Enemy(name, roles, hero.get("attack_type") == "Melee", right_click, traits, spells)


# Each judge: the enemies -> [(hero, ability, weight)] and, for Black King Bar, what goes through it.
Found = list[tuple[str, str, float]]


def _spells_bkb(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Disables and spell damage Black King Bar blocks; and the ones that go through it."""
    found, through = [], []
    for e in enemies:
        for s in e.spells:
            if not s.facts.get("hostile"):
                continue
            # Disables are what Black King Bar is for; spell damage counts a little.
            w = DISABLE_WEIGHT.get(s.facts.get("disable"), 0.0)
            dmg = s.facts.get("dmg")
            w += 0.25 if dmg in ("magical", "pure") else 0.1 if dmg == "physical" else 0.0
            if w <= 0:
                continue
            (through if s.facts.get("bkb") else found).append((e.name, s.name, w))
    return found, through


def _targeted(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Spells cast on one unit (Linken's Sphere blocks them, Lotus Orb sends them back), Black King Bar or not."""
    found = []
    for e in enemies:
        for s in e.spells:
            if s.facts.get("unit"):
                w = DISABLE_WEIGHT.get(s.facts.get("disable"), 0.0)
                w += 0.5 if s.facts.get("dmg") in ("magical", "pure") else 0.0
                if w > 0:
                    found.append((e.name, s.name, w))
    return found, []


def _dispellable(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Their disables (and lesser debuffs) a basic dispel removes: Eul's on yourself, Wind Waker."""
    found = []
    for e in enemies:
        for s in e.spells:
            if not s.facts.get("hostile") or s.facts.get("dispel") != "any":
                continue
            if s.facts.get("disable"):
                found.append((e.name, s.name, DISABLE_WEIGHT.get(s.facts["disable"], 0.5)))
            elif not s.facts.get("passive"):
                found.append((e.name, s.name, 0.2))  # (a slow, a damage over time)
    return found, []


def _magic(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Magic damage spells, counted in full on a Nuker (the hero's role) and half on others: a carry's Blade
    Fury isn't Lina's Laguna Blade."""
    found = []
    for e in enemies:
        w = 1.0 if "Nuker" in e.roles else 0.5
        for s in e.spells:
            if s.facts.get("dmg") == "magical":
                found.append((e.name, s.name, w / 2 if s.facts.get("passive") else w))
    return found, []


def _evasion(enemies: list[_Enemy]) -> tuple[Found, Found]:
    found = []
    for e in enemies:
        spells = [s.name for s in e.spells if s.facts.get("evasion")]
        if spells or "evasion" in e.traits:
            found += [(e.name, n, 0.7 / len(spells)) for n in spells] or [(e.name, "", 0.7)]
    return found, []


def _passives(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Heroes whose passives carry them (the table: the data can't tell a key passive from a minor one), with
    their passives named from the data."""
    found = []
    for e in enemies:
        if "passives" not in e.traits:
            continue
        spells = [s.name for s in e.spells if s.facts.get("passive") and not s.facts.get("innate")][:2]
        found += [(e.name, n, 0.5 / len(spells)) for n in spells] or [(e.name, "", 0.5)]
    return found, []


def _healing(enemies: list[_Enemy]) -> tuple[Found, Found]:
    found = []
    for e in enemies:
        spells = [s.name for s in e.spells if s.facts.get("heal")]
        total = 1.0 if "healing" in e.traits else min(0.6, 0.3 * len(spells))
        if total > 0:
            found += [(e.name, n, total / len(spells)) for n in spells[:2]] if spells else [(e.name, "", total)]
    return found, []


def _physical(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Right-click carries (the table's heavy hitters count most) and physical spells."""
    found = []
    for e in enemies:
        base = 1.0 if "physical" in e.traits else 0.6 if e.right_click else 0.0
        if base > 0:
            found.append((e.name, "", base))
        found += [(e.name, s.name, 0.3) for s in e.spells if s.facts.get("dmg") == "physical"][:2]
    return found, []


def _positional(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """What Force Staff gets you out of: roots, and melee carries chasing you."""
    found = []
    for e in enemies:
        found += [(e.name, s.name, 0.8) for s in e.spells if s.facts.get("disable") == "root"]
        if e.melee and e.right_click:
            found.append((e.name, "", 0.5))
    return found, []


def _buffs(enemies: list[_Enemy]) -> tuple[Found, Found]:
    """Their own buffs a basic dispel on them removes (Nullifier): Ghost Shroud, Windrun, Bloodlust..."""
    return [(e.name, s.name, 0.5) for e in enemies for s in e.spells if s.facts.get("self_buff")], []


@dataclass(frozen=True)
class _Item:
    judge: Callable[[list[_Enemy]], tuple[Found, Found]]
    full: float  # the weight that makes the case certain (score 1)
    verb: str  # "it blocks" + what it answers
    none: str  # the reason when it answers nothing
    heroes_only: bool = False  # the reason names the heroes, not their spells ("it disarms Juggernaut")


# The situational items: what each answers, and how much of it makes the item worth buying. (Item keys without
# "item_".) Any other item has no case: it isn't bought for the lineup.
ITEMS = {
    "black_king_bar": _Item(_spells_bkb, 4.5, "it blocks", "nothing of theirs to block"),
    "sphere": _Item(_targeted, 2.5, "it blocks", "no single-target spell of theirs to block"),
    "lotus_orb": _Item(_targeted, 3.0, "it sends back", "no single-target spell of theirs to send back"),
    "cyclone": _Item(_dispellable, 3.0, "a dispel on yourself removes", "none of their disables can be dispelled"),
    "wind_waker": _Item(_dispellable, 3.0, "a dispel on yourself removes", "none of their disables can be dispelled"),
    "pipe": _Item(_magic, 5.0, "it soaks the magic damage of", "they have little magic damage"),
    "glimmer_cape": _Item(_magic, 4.5, "it soaks the magic damage of", "they have little magic damage"),
    "eternal_shroud": _Item(_magic, 4.5, "it soaks the magic damage of", "they have little magic damage"),
    "hood_of_defiance": _Item(_magic, 4.5, "it soaks the magic damage of", "they have little magic damage"),
    "monkey_king_bar": _Item(_evasion, 1.0, "it hits through", "none of them has evasion"),
    "bloodthorn": _Item(_evasion, 1.0, "it hits through", "none of them has evasion"),
    "silver_edge": _Item(_passives, 1.0, "it breaks", "no passives of theirs worth breaking"),
    "spirit_vessel": _Item(_healing, 2.0, "it cuts the healing of", "they have little healing"),
    "skadi": _Item(_healing, 2.0, "it cuts the healing of", "they have little healing"),
    "ghost": _Item(_physical, 2.0, "it stops the attacks of", "they have no right-click carry", True),
    "heavens_halberd": _Item(_physical, 2.0, "it disarms", "they have no right-click carry", True),
    "shivas_guard": _Item(_physical, 2.5, "its armor and attack slow blunt", "they have no right-click carry", True),
    "force_staff": _Item(_positional, 2.0, "it gets you out of", "no roots or melee carries to get away from"),
    "nullifier": _Item(_buffs, 1.5, "it dispels", "no buffs of theirs to dispel"),
}

MAX_HEROES = 3  # in a reason: it has to fit in a spoken sentence
MAX_SPELLS = 2


def _say(found: Found) -> str:
    """'Lion's Earth Spike and Hex, and Lina's Light Strike Array' (most weight first)."""
    by_hero: dict[str, list[tuple[str, float]]] = {}
    for hero, spell, w in found:
        by_hero.setdefault(hero, []).append((spell, w))
    ranked = sorted(by_hero.items(), key=lambda kv: -sum(w for _s, w in kv[1]))[:MAX_HEROES]
    parts = []
    for hero, spells in ranked:
        names = [s for s, _w in sorted(spells, key=lambda sw: -sw[1]) if s]
        names = list(dict.fromkeys(names))[:MAX_SPELLS]
        parts.append(f"{hero}'s {' and '.join(names)}" if names else hero)
    if len(parts) == 1:
        return parts[0]
    # (A comma before the last hero when a hero has two spells: "X's a and b, and Y's c".)
    last = ", and " if any(" and " in p for p in parts) or len(parts) > 2 else " and "
    return ", ".join(parts[:-1]) + last + parts[-1]


def cases(kb: "Knowledge", enemy_npcs: list[str]) -> dict[str, ItemCase]:
    """Every situational item's case against these enemies (npc names), by item key ("item_x"). Items Doty
    has no data for (an empty item list aside) are left out."""
    enemies = [_enemy(kb, npc) for npc in enemy_npcs]
    out: dict[str, ItemCase] = {}
    for short, spec in ITEMS.items():
        item = f"item_{short}"
        if kb.items and item not in kb.items:
            continue
        found, through = spec.judge(enemies)
        score = round(min(1.0, sum(w for _h, _s, w in found) / spec.full), 2)
        named = [(h, "" if spec.heroes_only else s, w) for h, s, w in found]
        reason = f"{spec.verb} {_say(named)}" if found else spec.none
        if through:
            gone = _say(through)
            reason += f"; {gone} {'go' if ' and ' in gone else 'goes'} through it"
        answers = list(dict.fromkeys((h, s) for h, s, _w in found))
        out[item] = ItemCase(item, score, reason, answers)
    return out
