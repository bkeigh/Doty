"""A threat card per enemy hero: the one or two things of theirs most likely to kill YOUR hero, and what answers
each, worked out from their abilities (knowledge/judgement.py's reading of the ability data).

"Know the enemy": Lion's Hex, single-target, BKB blocks it; Bane's Fiend's Grip goes through BKB, but Linken's
takes it; Riki goes invisible. What's most dangerous depends on who you are: a hero that lives on its spells fears
a silence first, a right-clicker a disarm or evasion, a support an invisible hero. So the card is ranked for the
player's own hero and role.

Every spell a card names comes from that enemy's own ability list (by construction: the card is built from
`judgement._enemy`, which reads only that hero's abilities), and an answer is either an item whose case against
that enemy is strong (`Knowledge.item_case`, among the items the player's hero is bought with), or a habit the
spell's data shows (single-target: stay out of its range). Nothing comes from memory of the game.

Fair play: the enemy heroes on the player's own top bar and public facts about their spells; never their items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import judgement

if TYPE_CHECKING:
    from .constants import Knowledge

LINE_WORDS = 14  # a card's line is said out loud: as short as an ItemCase reason
SHOWN = 2  # threats per enemy on a card

# What each kind of threat does to you, as a few words after the spell's name.
WHAT = {"hex": "hexes you", "stun": "stuns you", "disable": "disables you", "sleep": "puts you to sleep",
        "taunt": "taunts you", "fear": "makes you flee", "silence": "silences you", "root": "roots you",
        "cyclone": "lifts you up", "disarm": "disarms you", "burst": "bursts you", "invis": "goes invisible",
        "right_click": "kills with attacks", "evasion": "dodges your attacks"}
# Items that answer a kind of threat, in the order they're tried (each must also have a strong case against that
# enemy and be bought on the player's hero). A disable: what blocks or sends it back, then what removes it.
ANSWERS = {
    "disable": ("item_black_king_bar", "item_sphere", "item_lotus_orb", "item_cyclone", "item_force_staff"),
    "burst": ("item_black_king_bar", "item_pipe", "item_glimmer_cape", "item_eternal_shroud", "item_sphere"),
    "right_click": ("item_ghost", "item_heavens_halberd", "item_shivas_guard"),
    "evasion": ("item_monkey_king_bar", "item_bloodthorn"),
    "invis": ("item_dust", "item_ward_sentry", "item_gem"),
}
SUPPORT_SAVES = ("item_lotus_orb", "item_glimmer_cape", "item_force_staff", "item_cyclone", "item_sphere")
BIG_CD = 60.0  # a damage spell with a cooldown this long is an ultimate-sized burst
HARD = {"hex", "stun", "disable", "sleep", "taunt", "fear", "cyclone"}


@dataclass
class Threat:
    """One dangerous thing an enemy has: `kind` (a disable kind, "burst", "invis", "right_click", "evasion"),
    `spell` (its ability's name; '' for the hero as a whole), `through_bkb`, `single_target`, the `answer` in
    words with `answer_item` when it's an item ('' for a habit), and `weight` (how likely it kills this player's
    hero: the ranking)."""

    kind: str
    spell: str
    weight: float
    through_bkb: bool = False
    single_target: bool = False
    answer: str = ""
    answer_item: str = ""

    def what(self) -> str:
        return WHAT.get(self.kind, self.kind) + (" through BKB" if self.through_bkb else "")


@dataclass
class ThreatCard:
    """An enemy hero's card: `threats` (the top SHOWN, most dangerous first), `line` (at most LINE_WORDS words,
    to say or show: "Lion's Hex hexes you; answer: Linken's Sphere"), and `facts` (every (hero, ability) the card
    rests on)."""

    hero: str  # display name
    npc: str
    threats: list[Threat]
    line: str
    facts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def top(self) -> Threat | None:
        return self.threats[0] if self.threats else None

    def as_dict(self) -> dict[str, Any]:
        return {"hero": self.hero, "line": self.line,
                "threats": [{"kind": t.kind, "spell": t.spell, "what": t.what(), "answer": t.answer,
                             **({"answer_item": t.answer_item} if t.answer_item else {})} for t in self.threats]}


@dataclass
class _You:
    """The player's hero, as far as it decides what's dangerous to it."""

    spells: bool  # it lives on its spells (an Intelligence hero, or a Nuker): a silence is the worst
    right_click: bool  # it lives on its attacks: a disarm or evasion hurts most
    support: bool  # position 4 or 5: invisible gankers and lockdown on a hero with no escape
    bought: set[str] | None  # what players of the hero buy (None: no data, any item may be named)


def _you(kb: "Knowledge", own_npc: str, role: Any) -> _You:
    hero = kb.heroes.get(own_npc) or {}
    roles = list(hero.get("roles") or [])
    me = judgement._enemy(kb, own_npc) if own_npc in kb.heroes else None
    position = int(getattr(role, "position", 0) or 0) if role is not None else 0
    core = getattr(role, "core", None) if role is not None else None
    support = position in (4, 5) or core is False
    bought = kb.bought_items(int(hero["id"])) if hero.get("id") else None
    spells = hero.get("primary_attr") == "int" or ("Nuker" in roles and roles.index("Nuker") <= 1)
    return _You(spells=spells, right_click=bool(me and me.right_click) and not support, support=support,
                bought=bought)


def _answer(kb: "Knowledge", kind: str, lineup: list[str], hero: str, spell: str, you: _You) -> tuple[str, str]:
    """(words, item) answering one threat: an item with a strong case against the lineup that answers exactly
    this (the case's own facts name the spell or the hero), and that the player's hero is bought with; else a
    habit the data supports; else ('', '')."""
    group = "disable" if kind in HARD or kind in ("silence", "root", "disarm") else kind
    order = ANSWERS.get(group, ())
    if you.support and group in ("disable", "burst"):
        # A support saves itself with a save item; Black King Bar on one is rare, whatever a few games show.
        order = SUPPORT_SAVES + tuple(i for i in order if i not in SUPPORT_SAVES and i != "item_black_king_bar")
    for item in order:
        if you.bought is not None and item not in you.bought:
            continue
        if item in ("item_dust", "item_ward_sentry", "item_gem"):  # (true sight: no case, the spell says it)
            return f"carry {kb.item_name(item)}", item
        case = kb.item_case(item, lineup)
        # (Exactly this: Force Staff answering Riki as a melee carry doesn't answer his Smoke Screen.)
        if case is not None and case.strong and (hero, spell) in case.answers:
            return kb.item_name(item), item
    return "", ""


def _threats(kb: "Knowledge", e: "judgement._Enemy", npc: str, you: _You) -> list[Threat]:
    found: list[Threat] = []
    for s in e.spells:
        f = s.facts
        if not f.get("hostile"):
            continue
        kind = f.get("disable") or ""
        bkb = bool(f.get("bkb"))
        unit = bool(f.get("unit"))
        if kind:
            w = judgement.DISABLE_WEIGHT.get(kind, 0.5) + (0.3 if unit else 0.0) + (0.4 if bkb else 0.0)
            if kind == "silence" and you.spells:
                w *= 1.6
            if kind == "disarm":
                w *= 3.0 if you.right_click else 0.5
            if you.support and kind in HARD and unit:
                w += 0.2  # (a support has no escape item for a point-and-click disable)
            if f.get("passive"):
                w *= 0.6  # (a bash or a chance on attack: real, but not a spell they aim at you)
            found.append(Threat(kind, s.name, w, bkb, unit))
        elif f.get("dmg") in ("magical", "pure") and float(f.get("cd") or 0) >= BIG_CD and not f.get("passive"):
            found.append(Threat("burst", s.name, 0.9 + (0.4 if bkb else 0.0), bkb, unit))
    if e.right_click:
        found.append(Threat("right_click", "", 0.7 if "physical" in e.traits else 0.5))
    evasion = [s.name for s in e.spells if s.facts.get("evasion")]
    if evasion or "evasion" in e.traits:
        found.append(Threat("evasion", evasion[0] if evasion else "", 1.1 if you.right_click else 0.2))
    invis = [s.name for s in e.spells if s.facts.get("invis")]
    if invis:
        found.append(Threat("invis", invis[0], 0.9 if you.support else 0.6))
    found.sort(key=lambda t: -t.weight)
    # One threat per spell, and at most one of each hero-wide kind.
    out, seen = [], set()
    for t in found:
        key = t.spell or t.kind
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _line(hero: str, t: Threat) -> str:
    """'Lion's Hex hexes you; answer: Linken's Sphere', at most LINE_WORDS words."""
    subject = f"{hero}'s {t.spell}" if t.spell else hero
    line = f"{subject} {t.what()}"
    if t.answer:
        longer = f"{line}; answer: {t.answer}"
        if len(longer.split()) <= LINE_WORDS:
            return longer
    elif t.single_target:
        longer = f"{line}; it's single-target: stay out of its range"
        if len(longer.split()) <= LINE_WORDS:
            return longer
    return line


def card(kb: "Knowledge", enemy: str, own_npc: str = "", role: Any = None,
         lineup: list[str] | None = None) -> ThreatCard | None:
    """The threat card for one enemy hero (npc or hero name) against the player's hero (`own_npc`) in their
    `role` (a roles.Role, or anything with .position / .core; None when not known). None when Doty has no ability
    data for that enemy: no data, no card."""
    npc = enemy if enemy in kb.heroes or enemy.startswith("npc_dota_hero_") else (kb.find_hero(enemy) or "")
    if not npc or not kb._has_spells(npc):
        return None
    e = judgement._enemy(kb, npc)
    you = _you(kb, own_npc, role)
    threats = _threats(kb, e, npc, you)[:SHOWN]
    for t in threats:
        t.answer, t.answer_item = _answer(kb, t.kind, list(lineup or [npc]), e.name, t.spell, you)
        if not t.answer and t.single_target and t.kind != "burst":
            t.answer = "stay out of its range"
    line = _line(e.name, threats[0]) if threats else f"{e.name}: nothing in the data stands out"
    facts = [(e.name, t.spell) for t in threats if t.spell]
    return ThreatCard(e.name, npc, threats, line, facts)


def cards(kb: "Knowledge", enemies: list[str], own_npc: str = "", role: Any = None) -> list[ThreatCard]:
    """A card for each enemy Doty has data for, the most dangerous to the player first."""
    out = [c for name in enemies if (c := card(kb, name, own_npc, role, enemies)) is not None]
    out.sort(key=lambda c: -(c.top.weight if c.top else 0.0))
    return out


def hero_spells(kb: "Knowledge", npc: str) -> set[str]:
    """The spell names a card may use for this hero: its own visible abilities, as the cards name them."""
    return {s.name for s in judgement._enemy(kb, npc).spells}


# What the game data says was on the player when they died (state.death_summary "disabled_s") -> the threat kinds
# that do it.
DEATH_FLAGS = {"stunned": ("stun", "disable", "sleep", "cyclone"), "hexed": ("hex",), "silenced": ("silence",),
               "disarmed": ("disarm",)}


def death_causes(kb: "Knowledge", enemies: list[str], disabled_s: dict[str, float],
                 own_npc: str = "", role: Any = None) -> list[str]:
    """For a death: which enemy spells could have done what the game data says was on the player ("hexed 1.6
    s": Lion's Hex, Shadow Shaman's Hex), each with its answer from the card. [] when nothing matches or Doty has
    no data. The game data doesn't say who cast it: these are the candidates in this lineup, not a verdict."""
    out = []
    for flag, seconds in sorted((disabled_s or {}).items(), key=lambda kv: -kv[1]):
        kinds = DEATH_FLAGS.get(flag)
        if not kinds:
            continue
        found = []
        for name in enemies:
            npc = name if name in kb.heroes or name.startswith("npc_dota_hero_") else (kb.find_hero(name) or "")
            if not npc or not kb._has_spells(npc):
                continue
            e = judgement._enemy(kb, npc)
            you = _you(kb, own_npc, role)
            for s in e.spells:
                if s.facts.get("hostile") and s.facts.get("disable") in kinds:
                    answer, _item = _answer(kb, s.facts["disable"], enemies, e.name, s.name, you)
                    found.append(f"{e.name}'s {s.name}" + (f" (answer: {answer})" if answer else ""))
        if found:
            out.append(f"{flag} {seconds:g} s: could be " + ", ".join(found[:4]))
    return out
