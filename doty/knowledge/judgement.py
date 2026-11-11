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
# (Reviewed against every hero's descriptions with `--audit`, patch 7.41: see the test that pins the result.)
DISABLE_WORDS = (
    ("hex", re.compile(r"\bhex(es|ed)?\b|transforms? (an|the) enemy|harmless (beast|creature)")),
    # A full disable: they can't move, attack or cast (Black Hole, Flaming Lasso, Shackles, Song of the Siren),
    # are banished (Disruption, Astral Imprisonment), trapped by Chronosphere, gripped (Fiend's Grip), locked in
    # time (Time Lock) or pulled in (Electric Vortex, Aether Remnant).
    ("disable", re.compile(r"cannot (move|act)\b[^.]{0,12}\b(attack|cast)|cannot act\b|\bbanishes\b|"
                           r"invulnerable and disabled|\btrapping all\b|\bdisabling (it|them|the target)\b(?! as)|"
                           r"\block (an|the) enemy|\bdisables? (the )?(target|enemy)|"
                           r"\bpull(s|ing)? (an enemy|it in|all units)")),
    ("sleep", re.compile(r"\bto sleep\b|\basleep\b|\bunconscious\b|\bsleep duration")),
    # (A bash is a stun; so is being launched into the air: Walrus PUNCH!, Tornado.)
    ("stun", re.compile(r"\bstun|\bbash|(launch|lift|hurl)\w* [^.]{0,30}into the air|\bbe (briefly )?frozen\b")),
    ("taunt", re.compile(r"\bforced to attack|\bcauses? them to attack|\btaunt")),
    ("silence", re.compile(r"\bsilenc|cannot (use|cast) (abilities|spells)|prevents? them from casting")),
    ("fear", re.compile(r"\bfear(?!some)|\bflee|\bhypnoti")),
    ("root", re.compile(r"\broot|cannot move|prohibiting movement|prevents? them from moving|entangl|ensnar|"
                        r"\b(anchoring|trapping|freezes?|holding) (them |it |an enemy |the target )?in place|"
                        r"\btrapping (any|it|units)\b|\bleash|can'?t pass|cannot pass")),
    ("cyclone", re.compile(r"\bcyclone|(suspend|hold)\w* [^.]{0,30}in the air")),
    ("disarm", re.compile(r"\bdisarm|unable to attack")),
)
# How much each kind of disable costs you: a stun or hex loses a fight, a root less so, a disarm only a carry.
DISABLE_WEIGHT = {"hex": 1.0, "stun": 1.0, "disable": 1.0, "silence": 0.9, "sleep": 0.8, "taunt": 0.8,
                  "fear": 0.8, "root": 0.6, "cyclone": 0.6, "disarm": 0.3}
# A label naming a kind ("FEAR DURATION:"): when every such label is 0, the word in the description is flavour
# (Howl "strikes fear", with FEAR DURATION 0).
KIND_LABEL = {"stun": "STUN", "silence": "SILENCE", "fear": "FEAR", "root": "ROOT", "sleep": "SLEEP",
              "hex": "HEX", "disarm": "DISARM"}
SELF_SENTENCE = re.compile(r"\b(he|she) (is|becomes|gets|falls|loses)\b|\b(himself|herself)\b|"
                           r"be attacked, (he|she)\b|\bis unable to attack\b|loses the ability|"
                           r"for the duration of the ability|\bdo(es)? not\b", re.I)
# Heroes whose real spells are listed as hidden (Invoker's invoked spells): those are read too.
HIDDEN_SPELLS_COUNT = frozenset({"npc_dota_hero_invoker"})
EVASION = re.compile(r"\bevasion|\bevade")
# Healing that keeps them alive: heals, health regen, lifesteal (not mana regen, not a heal they take away).
HEAL = re.compile(r"\bheal(s|ing|ed)?\b|\bhealth regen|lifesteal")
NOT_HEAL = re.compile(r"heal(ing)? reduction|reduc\w* (their |the target's )?heal|prevents?[^.]*heal|negative heal|"
                      r"lifesteal penalty|golem health")
INVIS = re.compile(r"\binvisib|\bturns? invisible|\bfades? (away|from view)")

# What the data gets wrong or can't say, reviewed by hand against the ability's own description: ability key ->
# (facts to set, why, the patch it was checked on). Keep it small: a fix to the reading above beats an entry here.
OVERRIDES: dict[str, tuple[dict[str, Any], str, str]] = {
    # The stun in its numbers ("UNPERCHED STUNNED DURATION") is Monkey King's own, when his tree is cut down.
    "monkey_king_tree_dance": ({"hostile": False, "disable": "", "unit": False, "dmg": ""},
                               "a jump onto a tree; the stun it lists is his own when the tree falls", "7.41"),
    # "Strikes fear into enemies" with FEAR DURATION 0, and no target team: read as a buff on Lycan's side, which
    # would make Nullifier "dispel" it. It's a debuff on the enemies (armor and attack damage), no disable.
    "lycan_howl": ({"hostile": True, "self_buff": False, "disable": "", "unit": False},
                   "a debuff on all enemies (armor, attack damage), not a buff and not a fear", "7.41"),
}


def _listed(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []


def _zero(value: Any) -> bool:
    """An attribute with no effect of its own: every level 0 ("STUN DURATION: 0", "DOES ROOT: 0"), or given as
    "+2.5" (what a talent or facet adds, not what the spell does)."""
    values = value if isinstance(value, list) else [value]
    if any(str(v).strip().startswith("+") for v in values):
        return True
    try:
        return all(float(str(v).split("/")[0].strip().rstrip("%") or 0) == 0 for v in values)
    except ValueError:
        return False


def _labels(raw: dict[str, Any]) -> tuple[str, str]:
    """The labels of the ability's numbers that have a value ("STUN DURATION:" when it stuns), and those that are
    0. Not the yes/no switches ("DOES ROOT:", "PIERCES IMMUNITY:"), which say what a variant or a talent could
    do, nor what it does to its own caster ("SELF STUN DURATION PERCENT:" on Meat Hook)."""
    live, dead = [], []
    for a in raw.get("attrib") or []:
        if not isinstance(a, dict):
            continue
        header = str(a.get("header") or "")
        if "DOES" in header or "PIERCE" in header or "SELF" in header:
            continue
        (dead if _zero(a.get("value")) else live).append(header)
    return " ".join(live), " ".join(dead)


def ability_facts(raw: dict[str, Any], name: str = "") -> dict[str, Any]:
    """What one ability (a dotaconstants abilities.json entry, `name` its key) does to the other team, as the
    item judgement reads it: "dmg" (magical, physical, pure or ''), "hostile" (it hits or disables enemies),
    "unit" (it targets one unit, so Linken's Sphere or Lotus Orb can take it), "disable" (the kind: stun, hex,
    silence, root...; '' for none) with "why" (the words that showed it), "evasion", "heal", "self_buff" (a buff
    on their own side a basic dispel removes) and "invis". Then OVERRIDES, for what the data gets wrong."""
    behavior = _listed(raw.get("behavior"))
    team = _listed(raw.get("target_team"))
    dmg = raw.get("dmg_type")
    dmg = dmg.lower() if isinstance(dmg, str) and dmg.lower() in ("magical", "physical", "pure") else ""
    desc = str(raw.get("desc") or "")
    live, dead = _labels(raw)
    # Sentences about the caster ("he falls and is stunned", "Largo... He is disarmed", "Timbersaw loses the
    # ability to attack") or saying what it doesn't do ("These beams do not stun") aren't about the enemy.
    told = " ".join(s for s in re.split(r"(?<=[.!?])\s+", desc) if not SELF_SENTENCE.search(s))
    text = f"{raw.get('dname') or ''} {told} {live}".lower()
    kind, why = "", ""
    for k, pattern in DISABLE_WORDS:
        m = pattern.search(text)
        if not m:
            continue
        label = KIND_LABEL.get(k)
        if label and label in dead and label not in live.upper():
            continue  # (its duration is 0: the word is flavour)
        kind, why = k, m.group(0)
        break
    enemies = bool(re.search(r"\benem", desc, re.I))
    if "Enemy" in team or "Both" in team:
        hostile = True
    elif "Friendly" in team:
        # Cast on a friend, but it hits enemies where it lands (Firesnap Cookie, Rebound, Supernova).
        hostile = bool(dmg and kind and enemies and why in told.lower())
    else:  # (no target team listed: an area spell or a passive; it's hostile when it hurts or holds someone)
        hostile = bool(dmg or kind)
    if not hostile:
        kind = why = ""  # ("Removes debuffs and disables": a save, not a disable)
    passive = "Passive" in behavior
    dispel = str(raw.get("dispellable") or "")
    facts = {
        "dmg": dmg if hostile else "",
        "hostile": hostile,
        "unit": hostile and "Unit Target" in behavior and "Point Target" not in behavior,
        "disable": kind,
        "why": why,
        "evasion": not hostile and bool(EVASION.search(text)),
        "heal": bool(HEAL.search(text)) and not NOT_HEAL.search(text),
        "self_buff": (not hostile and not passive and not dmg and dispel == "Yes"
                      and "Hidden" not in behavior and ("Friendly" in team or not team)),
        "invis": bool(INVIS.search(desc)) and "Enemy" not in team,
    }
    fix = OVERRIDES.get(name)
    if fix:
        facts.update(fix[0])
        facts["why"] = "override: " + fix[1]
    return facts


@dataclass
class ItemCase:
    """Whether an item is worth buying against this lineup: `score` from 0 (answers nothing of theirs) to 1,
    `reason` a short clause to say out loud (at most REASON_WORDS words: its two or three strongest answers,
    "it blocks Lion's Earth Spike and Hex and Lina's Light Strike Array"), `answers` the (enemy hero, ability)
    pairs it answers (ability '' when it answers the hero as a whole: a right-click carry, say), and `detail`
    the long form for the coach's prompt and the app (more answers, and what goes through Black King Bar)."""

    item: str
    score: float
    reason: str
    answers: list[tuple[str, str]] = field(default_factory=list)
    detail: str = ""

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
        if ability == "generic_hidden" or not info or not info.get("dname"):
            continue
        if info.get("hidden") and npc not in HIDDEN_SPELLS_COUNT:
            continue  # (a Shard or Scepter spell, or a sub-spell)
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
    """What Force Staff gets you out of: roots someone casts on you (not an attack's chance to ensnare), and
    melee carries chasing you."""
    found = []
    for e in enemies:
        found += [(e.name, s.name, 0.8) for s in e.spells
                  if s.facts.get("disable") == "root" and not s.facts.get("passive")]
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
    verb: str  # "it blocks" + the spells it answers
    none: str  # the reason when it answers nothing
    heroes_only: bool = False  # the reason names the heroes, not their spells ("it disarms Juggernaut")
    hero_verb: str = ""  # for a hero answered as a whole, when that reads differently ("it gets you away from")


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
    "monkey_king_bar": _Item(_evasion, 1.0, "it hits through", "none of them has evasion",
                             hero_verb="it hits through the evasion of"),
    "bloodthorn": _Item(_evasion, 1.0, "it hits through", "none of them has evasion",
                        hero_verb="it hits through the evasion of"),
    "silver_edge": _Item(_passives, 1.0, "it breaks", "no passives of theirs worth breaking",
                         hero_verb="it breaks the passives of"),
    "spirit_vessel": _Item(_healing, 2.0, "it cuts the healing of", "they have little healing"),
    "skadi": _Item(_healing, 2.0, "it cuts the healing of", "they have little healing"),
    "ghost": _Item(_physical, 2.0, "it stops the attacks of", "they have no right-click carry", True),
    "heavens_halberd": _Item(_physical, 2.0, "it disarms", "they have no right-click carry", True),
    "shivas_guard": _Item(_physical, 2.5, "its armor and attack slow blunt", "they have no right-click carry", True),
    "force_staff": _Item(_positional, 2.0, "it gets you out of", "no roots or melee carries to get away from",
                         hero_verb="it gets you away from"),
    "nullifier": _Item(_buffs, 1.5, "it dispels", "no buffs of theirs to dispel"),
}

# The items Doty may put into the player's build by itself (after the phase's core items), when
# the case clears INSERT_MIN and the hero's players buy the item. Only those whose case rests on hard data about
# the enemies' spells: Black King Bar against disables it blocks, Linken's against single-target spells, Monkey
# King Bar against evasion, Silver Edge against passives that carry a hero, Pipe against heavy magic damage. The
# others (Ghost Scepter, Halberd, Force Staff, Nullifier, Spirit Vessel, Eul's...) depend on the player's role and
# the fight as much as on the lineup ("the enemies are melee" doesn't make Ghost Scepter a core's second item):
# the coach is told their cases to weigh, but they're never added to the build.
AUTO_INSERT = frozenset({"item_black_king_bar", "item_sphere", "item_monkey_king_bar", "item_silver_edge",
                         "item_pipe"})
INSERT_MIN = 0.8

REASON_WORDS = 14  # the spoken reason: two or three answers, never a list
DETAIL_ANSWERS = 6  # the long form, for the prompt and the app
MAX_HEROES = 3  # (what goes through Black King Bar, in the long form)
MAX_SPELLS = 2


def _join(parts: list[str], comma: bool = True) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    # (Written, a comma before the last one when one of them has two spells: "X's a and b, and Y's c". Said out
    # loud, two are just "and".)
    last = ", and " if len(parts) > 2 or (comma and any(" and " in p for p in parts)) else " and "
    return ", ".join(parts[:-1]) + last + parts[-1]


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
    return _join(parts)


def _phrase(spec: _Item, found: Found, most: int) -> str:
    """What the item answers, its `most` strongest answers (heaviest first: a stun or hex before a nuke), as one
    clause: "it blocks Lion's Earth Spike and Hex and Lina's Light Strike Array". Spells and heroes answered as
    a whole aren't mixed in one list: "it gets you out of Crystal Maiden's Frostbite and gets you away from
    Juggernaut"."""
    weights: dict[tuple[str, str], float] = {}
    for hero, spell, w in found:
        key = (hero, "" if spec.heroes_only else spell)
        weights[key] = weights.get(key, 0.0) + w
    ranked = sorted(weights.items(), key=lambda kv: -kv[1])[:most]
    spells: dict[str, list[str]] = {}
    heroes: list[str] = []
    for (hero, spell), _w in ranked:
        if spell:
            spells.setdefault(hero, []).append(spell)
        elif hero not in heroes:
            heroes.append(hero)
    groups = [f"{hero}'s {' and '.join(names)}" for hero, names in spells.items()]
    spoken = most < DETAIL_ANSWERS
    out = f"{spec.verb} {_join(groups, comma=not spoken)}" if groups else ""
    if heroes:
        verb = spec.hero_verb or spec.verb
        out = f"{out} and {verb.removeprefix('it ')} {_join(heroes)}" if out else f"{verb} {_join(heroes)}"
    return out


def cases(kb: "Knowledge", enemy_npcs: list[str]) -> dict[str, ItemCase]:
    """Every situational item's case against these enemies (npc names, each with ability data: the caller,
    Knowledge.item_case, sees to that), by item key ("item_x"). Items Doty has no data for (an empty item list
    aside) are left out."""
    enemies = [_enemy(kb, npc) for npc in enemy_npcs]
    out: dict[str, ItemCase] = {}
    for short, spec in ITEMS.items():
        item = f"item_{short}"
        if kb.items and item not in kb.items:
            continue
        found, through = spec.judge(enemies)
        score = round(min(1.0, sum(w for _h, _s, w in found) / spec.full), 2)
        if found:
            for most in (3, 2, 1):
                reason = _phrase(spec, found, most)
                if len(reason.split()) <= REASON_WORDS:
                    break
            detail = _phrase(spec, found, DETAIL_ANSWERS)
        else:
            reason = detail = spec.none
        if through:
            gone = _say(through)
            detail += f"; {gone} {'go' if ' and ' in gone else 'goes'} through it"
        answers = list(dict.fromkeys((h, s) for h, s, _w in found))
        out[item] = ItemCase(item, score, reason, answers, detail)
    return out


# the audit: what Doty believes each hero's spells do ----------------------------------------------------------
def load_cached(cache_dir: Any = None) -> "Knowledge":
    """A Knowledge with the hero, item and ability data from the cache folder only (no download): for the audit
    and offline checks."""
    import json
    from pathlib import Path

    from ..config import CACHE_DIR
    from .constants import Knowledge, ability_entry

    folder = Path(cache_dir or CACHE_DIR)
    kb = Knowledge(folder)

    def read(name: str) -> Any:
        try:
            return json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    heroes, items, hero_abilities, abilities = (read(f"{n}.json") for n in
                                                ("heroes", "items", "hero_abilities", "abilities"))
    if isinstance(heroes, dict):
        kb.heroes = {h["name"]: h for h in heroes.values() if isinstance(h, dict) and "name" in h}
    if isinstance(items, dict):
        kb.items = {f"item_{k}": v for k, v in items.items() if isinstance(v, dict)}
    if isinstance(hero_abilities, dict):
        kb.hero_abilities = {k: v for k, v in hero_abilities.items() if isinstance(v, dict)}
    if isinstance(abilities, dict):
        wanted = {a for h in kb.hero_abilities.values() for a in h.get("abilities", [])}
        kb.ability_info = {n: ability_entry(v, n) for n, v in abilities.items()
                           if isinstance(v, dict) and (n in wanted or not wanted)}
    return kb


def _flag_text(facts: dict[str, Any]) -> str:
    if not facts.get("hostile"):
        flags = [f for f in ("evasion", "heal", "self_buff", "invis") if facts.get(f)]
        return "own side" + (f" [{', '.join(flags)}]" if flags else "")
    parts = [facts.get("dmg") or "no damage", "single target" if facts.get("unit") else "area/other",
             facts.get("disable") or "no disable",
             "goes through BKB" if facts.get("bkb") else "BKB blocks it",
             f"dispel: {facts.get('dispel') or '?'}"]
    flags = [f for f in ("heal",) if facts.get(f)]
    return ", ".join(parts) + (f" [{', '.join(flags)}]" if flags else "")


def audit(kb: "Knowledge", heroes: list[str] | None = None, why: bool = False) -> list[str]:
    """What the engine believes each hero's visible spells do, one line per spell (the heroes named, or all):
    for checking the readings against the descriptions by eye."""
    npcs = []
    for name in heroes or sorted(kb.hero_abilities, key=kb.hero_name):
        npc = name if name in kb.heroes or name.startswith("npc_dota_hero_") else kb.find_hero(name)
        if npc and npc in kb.hero_abilities:
            npcs.append(npc)
    lines = []
    for npc in npcs:
        e = _enemy(kb, npc)
        lines.append(f"{e.name} ({npc.removeprefix('npc_dota_hero_')}): traits {sorted(e.traits) or '-'}"
                     + (", right-click" if e.right_click else ""))
        for s in e.spells:
            line = f"  {s.name:28s} {_flag_text(s.facts)}"
            if why and s.facts.get("why"):
                line += f'   <- "{s.facts["why"]}"'
            lines.append(line)
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m doty.knowledge.judgement",
                                     description="Print what Doty believes each hero's spells do, from the cached "
                                                 "ability data (no download).")
    parser.add_argument("--audit", action="store_true", help="list each hero's spells as Doty reads them")
    parser.add_argument("--why", action="store_true", help="show the words that decided each disable")
    parser.add_argument("--cache", default=None, help="the cache folder (default: Doty's own)")
    parser.add_argument("heroes", nargs="*", help="hero names (default: every hero)")
    args = parser.parse_args(argv)
    if not args.audit:
        parser.print_help()
        return 2
    kb = load_cached(args.cache)
    if not kb.hero_abilities or not kb.ability_info:
        print("No cached ability data: start Doty once with a network connection.")
        return 1
    for line in audit(kb, args.heroes or None, args.why):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
