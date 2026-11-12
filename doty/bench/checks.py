"""The checkers: each one judges a single line against the game as it stood when the line was said.

A checker is a function `(line, match) -> None | str`, registered with `@checker(name, severity, about)`:
- None: not applicable (the line isn't about this, or the input doesn't hold what the check needs),
- "": passed,
- any other string: failed, and the string says why (it is quoted in the scorecard, so write it for a person).

Severity weighs a failure in the headline score: 3 is a wrong fact or broken output (the player is told
something the game data says is false, or hears a control tag), 2 is a line at the wrong moment or one that keeps
coming back, 1 is noise (repeats, too many lines). `spoken_only` checkers judge only lines that were heard: what a
line on screen alone says can still be wrong, but it can't be too frequent.

Adding a checker is a few lines:

    @checker("no_shouting", 1, "a line written in capitals")
    def no_shouting(line, match):
        return "written in capitals" if line.text.isupper() else ""

The checks use Doty's own vocabulary where it exists (the coach's buy-word, TP and repeat rules in
coach/llm.py, the item parts and boots logic in Knowledge), so the bench and the coach agree on what a line says;
the bench is the checker of record because it judges what came out, whatever path it took.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .. import speech, triggers
from ..coach import llm
from ..knowledge import build
from ..knowledge import constants as kconst

if TYPE_CHECKING:
    from ..knowledge.constants import Knowledge
    from .model import Line, Match

CheckFn = Callable[["Line", "Match"], "str | None"]


@dataclass(frozen=True)
class Checker:
    name: str
    severity: int  # 1 noise, 2 wrong moment or nagging, 3 wrong fact or broken output
    about: str  # one line for the scorecard's legend
    fn: CheckFn
    spoken_only: bool = False

    def run(self, line: "Line", match: "Match") -> "str | None":
        if self.spoken_only and not line.said:
            return None
        return self.fn(line, match)


CHECKERS: dict[str, Checker] = {}


def checker(name: str, severity: int, about: str, spoken_only: bool = False) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        CHECKERS[name] = Checker(name, severity, about, fn, spoken_only)
        return fn
    return register


# tunables -------------------------------------------------------------------------------------------------------
MAX_ITEM_NAMES = 3  # an item named this many times without a part bought since is enough (llm.MAX_ITEM_PUSHES)
STALE_S = 30  # a clock time this far in the past, said as something to do, is about a moment that's gone
REPEAT_WINDOW_S = 60  # near-identical lines this close together (game seconds) are a repeat...
DANGER_REPEAT_S = 10  # ...but a danger call is new information again after this long (a second burst)
RATE_WINDOW_S = 60
RATE_BUDGET = 4  # lines heard in any minute, danger calls aside (the two real games: 5.5 a minute)
COACH_GAP_S = 30  # at most one unprompted coach line every 30 s outside danger (ACCURACY_PLAN.md section 3)
BOUGHT_GRACE_S = 2  # (log input) an item that arrived this close to a line counts as not owned yet


# shared helpers -------------------------------------------------------------------------------------------------
def _kb(match: "Match") -> "Knowledge | None":
    kb = match.kb
    return kb if kb is not None and kb.items else None


def _names(kb: "Knowledge", items) -> str:
    return ", ".join(sorted(kb.item_name(i) for i in items))


def _owned_items(line: "Line") -> list[str] | None:
    ctx = line.ctx
    if ctx is None:
        return None
    if ctx.items is not None:
        return list(ctx.items)
    return sorted(ctx.owned) if ctx.owned is not None else None


def _danger(line: "Line") -> bool:
    """A call about staying alive: never noise, whenever and however often it comes."""
    return speech.danger(line.priority, line.category, line.text)  # (the speech gate's own test)


STATED_GOLD = re.compile(r"(?<![\d,])(\d{1,2},\d{3}|\d{2,5})\s+gold\b", re.IGNORECASE)


# 1. the line against what the player holds ------------------------------------------------------------------------
@checker("contradicts_inventory", 3, "tells the player to buy what they own, a part of it, or a second pair of boots")
def contradicts_inventory(line: "Line", match: "Match") -> str | None:
    kb = _kb(match)
    ctx = line.ctx
    if kb is None or ctx is None or ctx.owned is None:
        return None
    buys = kb.buy_mentions(line.text, ctx.hero)
    if not buys:
        return None
    # The coach's own rule (knowledge/build.already_have): a part of something built is fine when an item still
    # being built needs it (an Ogre Axe went into Aghanim's Scepter; another one is half of Black King Bar).
    bad = build.already_have(kb, buys, ctx.owned, ctx.hero, line.clock, line.text)
    return f"says to buy what the player already has (or a second pair of boots): {_names(kb, bad)}" if bad else ""


@checker("not_affordable", 3, 'says "buy X now" or "you can afford X" without the gold')
def not_affordable(line: "Line", match: "Match") -> str | None:
    kb = _kb(match)
    ctx = line.ctx
    if kb is None or ctx is None or not llm.NOW_CLAIM.search(line.text):
        return None
    buys = kb.buy_mentions(line.text, ctx.hero)
    if not buys:
        return None
    gold, how = ctx.gold, "gold"
    if gold is None:  # (a session log: only the gold the line itself names, "with your 985 gold")
        m = STATED_GOLD.search(line.text)
        if m is None:
            return None
        gold, how = int(m.group(1).replace(",", "")), "gold (as the line itself says)"
    owned = _owned_items(line) or []
    # As the coach judges it (llm.Coach._cant_afford): broke means the gold covers no real part of the item either;
    # "you have the gold" / "you can afford it" claims the whole of what's left.
    if llm.AFFORD_CLAIM.search(line.text):
        bad = {i for i in buys if 0 < kb.cost_left(i, owned) and kb.cost_left(i, owned) > gold}
    else:
        bad = {i for i in buys if kb.cost_left(i, owned) > 0 and kb.next_buy(i, owned, gold) is None}
    if not bad:
        return ""
    left = ", ".join(f"{kb.item_name(i)} {kb.cost_left(i, owned):,} to go" for i in sorted(bad))
    return f"says to buy now with {gold:,} {how}: {left}"


@checker("skill_order", 2, "tells the player to skill something other than the order Doty says, or to skip it")
def skill_order(line: "Line", match: "Match") -> str | None:
    """"Skip Blade Fury until level 2" with Blade Fury next in the order (a real run). The coach's own test
    (llm.wrong_skill), on the game data's abilities (a replay or a record: the snapshot)."""
    kb = match.kb
    snap = line.ctx.snap if line.ctx is not None else None
    if kb is None or snap is None or not getattr(snap, "abilities", None) or not kb.skill_rows:
        return None
    name = llm.wrong_skill(kb, line.text, snap)
    return f"goes against the skill order ({name})" if name else ""


@checker("claims_owned", 3, "says the player has, or spent their gold on, an item they don't hold")
def claims_owned(line: "Line", match: "Match") -> str | None:
    """"Your gold is spent on Wraith Band now" to a player who never bought it (a real run). The coach's own test
    (llm.OWNED_CLAIM), any item, cheap ones too."""
    kb = _kb(match)
    ctx = line.ctx
    if kb is None or ctx is None or ctx.owned is None or not llm.OWNED_CLAIM.search(line.text):
        return None
    bad = llm.owned_claims(kb, line.text) - kb.owned_with_parts(set(ctx.owned))
    return f"says the player has what they don't: {_names(kb, bad)}" if bad else ""


@checker("wrong_part", 3, 'names an item as a part of another that isn\'t inside it ("X for Y", "X, a part of Y")')
def wrong_part(line: "Line", match: "Match") -> str | None:
    """"Buy Broadsword plus Blades of Attack for Battle Fury" (a real death answer): Battle Fury holds no Blades of
    Attack. The coach's own test (llm.wrong_parts), on the item data's recipes."""
    kb = _kb(match)
    if kb is None:
        return None
    bad = llm.wrong_parts(kb, line.text)
    if not bad and not llm.PART_OF.search(line.text):
        return None
    return ("names a part that isn't in the item: "
            + ", ".join(f"{kb.item_name(p)} for {kb.item_name(w)}" for p, w in bad)) if bad else ""


@checker("asks_player", 3, "asks the player to tell the coach something, or puts a question to them")
def asks_player(line: "Line", match: "Match") -> str | None:
    """"Check the top bar and tell me their lineup." (a real plan): Doty reads the game itself, and the coach can't
    hear an answer. The coach's own test (llm.asks_player)."""
    if line.source != "coach":
        return None
    return "asks the player for something the coach can't hear" if llm.asks_player(line.text) else ""


# The coach's "buy a TP" (llm.TP_BUY), plus the other word order the rules use ("No TP scroll — buy one").
TP_THEN_BUY = re.compile(r"\b(tp|town\s+portal)\b[^.;:!?]{0,30}?\b(buy|get|grab|carry)\b", re.IGNORECASE)


@checker("tp_nag", 3, "tells the player to buy or carry a TP scroll while one is held")
def tp_nag(line: "Line", match: "Match") -> str | None:
    ctx = line.ctx
    if not (llm.TP_BUY.search(line.text) or TP_THEN_BUY.search(line.text)):
        return None
    if ctx is None or ctx.has_tp is None:
        return None
    return "says to buy a TP scroll; one is in the TP slot" if ctx.has_tp else ""


@checker("passed_on", 3, "pushes an item the player has passed on this game")
def passed_on(line: "Line", match: "Match") -> str | None:
    kb = _kb(match)
    ctx = line.ctx
    if kb is None or ctx is None or ctx.skipped is None:
        return None
    buys = kb.buy_mentions(line.text, ctx.hero)
    if not buys:
        return None
    bad = buys & set(ctx.skipped)
    return f"pushes an item the player passed on: {_names(kb, bad)}" if bad else ""


@checker("weak_case", 3, "pushes a counter item that answers little of this enemy lineup")
def weak_case(line: "Line", match: "Match") -> str | None:
    """Knowledge.item_case against the five enemies as they stood (all five known: with one missing, the hero the
    item answers may be that one). A real match: Black King Bar, 57 times, against right-click heroes."""
    kb = _kb(match)
    ctx = line.ctx
    if kb is None or ctx is None or not ctx.enemies or len(ctx.enemies) < 5:
        return None
    buys = kb.buy_mentions(line.text, ctx.hero)
    if not buys:
        return None
    bad = {i for i in buys if (case := kb.item_case(i, list(ctx.enemies))) is not None and case.weak}
    return f"pushes an item that answers little of this lineup: {_names(kb, bad)}" if bad else ""


@checker("foreign_spell", 3, "gives a hero a spell that isn't theirs")
def foreign_spell(line: "Line", match: "Match") -> str | None:
    """"Lion's Fiend's Grip": a hero named with a spell from another hero's list (knowledge/threats.py builds its
    lines only from each hero's own abilities, so it never says this; the AI might)."""
    from ..knowledge import threats

    kb = match.kb
    if kb is None or not kb.ability_info or not kb.hero_abilities:
        return None
    # (the coach's own vetting drops such a line with the same test: knowledge/threats.foreign_spells)
    wrong = threats.foreign_spells(kb, line.text)
    return f"spells that aren't that hero's: {', '.join(wrong)}" if wrong else ""


@checker("item_overuse", 2, f"names one item more than {MAX_ITEM_NAMES} times with no part of it bought since",
         spoken_only=True)
def item_overuse(line: "Line", match: "Match") -> str | None:
    kb = _kb(match)
    if kb is None:
        return None
    verdicts = match.memo.get("item_overuse")
    if verdicts is None:
        verdicts = match.memo["item_overuse"] = _count_item_names(match, kb)
    return verdicts.get(line.index)


WAITING_FOR = re.compile(r"\b(until|till|wait(ing)?\s+for|towards?|head(ing)?\s+for|save\s+(up\s+)?for)\b",
                         re.IGNORECASE)


def _count_item_names(match: "Match", kb: "Knowledge") -> dict[int, str | None]:
    """One pass over the match: each heard line that names an item the player doesn't have counts towards that
    item; buying a part of it starts the count again (they're building it, so it's worth talking about), as the
    coach's own count does (llm.Coach._pushed). A call to get out never counts."""
    counts: dict[str, list[int]] = {}  # item -> [times named, gold left on it then]
    out: dict[int, str | None] = {}
    for line in match.lines:
        if not line.said or line.ctx is None:
            continue
        named = kb.buy_mentions(line.text, line.ctx.hero)
        if WAITING_FOR.search(line.text):  # "farm until Black King Bar" is the same reminder (llm._build_mentions)
            named = named | kb.item_mentions(line.text, line.ctx.hero)
        if not named or llm.RETREAT.search(line.text):
            continue
        owned = _owned_items(line) or []
        have = kb.owned_with_parts(set(owned))
        over = []
        for item in sorted(named - have):
            left = kb.cost_left(item, owned)
            count, was = counts.get(item, (0, left))
            count = 1 if left < was else count + 1
            counts[item] = [count, left]
            if count > MAX_ITEM_NAMES:
                over.append(f"{kb.item_name(item)} ({count} times)")
        out[line.index] = f"names an item it has already named often: {', '.join(over)}" if over else ""
    return out


# 2. the words themselves --------------------------------------------------------------------------------------------
CONTROL_TAG = re.compile(r"(?<![A-Za-z])(TRIGGER|DRAW|BUILD|LINEUP|DRAFT|SKIP|SAY|QUIET):")


@checker("format_leak", 3, "a control tag (TRIGGER:, DRAW:, BUILD:...) or a mid-sentence change of mind in the text")
def format_leak(line: "Line", match: "Match") -> str | None:
    m = CONTROL_TAG.search(line.text)
    if m:
        return f'contains the control tag "{m.group(0)}"'
    # The coach's own patterns: "Wait for Omniknight before fighting" is an instruction, not a second thought.
    m = llm.MID_CORRECTION.search(line.text) or llm.SELF_CORRECTION.search(line.text)
    if m:
        return f'changes its mind mid-line ("{m.group(0).strip()}")'
    # The model writing to itself: a second paragraph, a "Note:", "the player" in the third person (coach/llm.META).
    if re.search(r"\n\s*\n", line.text.strip()):
        return "a second paragraph (the model talking to itself)"
    m = llm.META.search(line.text)
    if m:
        return f'talks about the player instead of to them ("{m.group(0).strip()}")'
    if line.text[:1].islower():
        return "starts mid-sentence (the start was cut away)"
    return ""


# Places, runes and other things a buy-word can point at that aren't items or heroes ("Grab the Bounty rune").
GAME_NOUNS = """Roshan Tormentor Aegis Cheese Refresher Shard Bounty Rune Power Water Wisdom Lotus Shrine Outpost Watcher
Lantern Twin Gate Ancient Tier Tower Barracks Courier Secret Shop Side Shop Turbo Radiant Dire Aghanim Scepter Blessing
Buyback Neutral Talent Mid Top Bottom Bot Safe Lane Offlane Jungle Fountain High Ground TP Town Portal Smoke Dust
Sentry Observer Ward Wards Gem Shard Level Ultimate Ult Necronomicon Stack Camp Tome Knowledge Flask Salve Clarity
Tango Mango Faerie Fire Bottle Glyph Scan""".split()
NAME_PHRASE = re.compile(
    r"\b(?i:buy|get|grab|purchase|rush|build|finish|complete|save\s+for|go\s+for|pick\s+up)\s+"
    r"(?:(?i:a|an|the|your|some|another|more|that|this)\s+)?"
    r"((?:[A-Z][\w'\-]*)(?:\s+(?:of|and|the|[A-Z][\w'\-]*))*)")


def _name_corpus(kb: "Knowledge") -> str:
    """Every name a line may use for a thing in the game, one per line, lowercased: items (and their usual
    short names), heroes (and nicknames), abilities, and the map's places and runes."""
    names = [str(v.get("dname") or "") for v in kb.items.values()]
    names += list(kconst.ITEM_ALIASES)
    names += [str(h.get("localized_name") or "") for h in kb.heroes.values()]
    names += list(kconst.HERO_ALIASES)
    names += [str(a.get("dname") or "") for a in kb.ability_info.values()]
    names += GAME_NOUNS
    return "\n".join(n.lower() for n in names if n)


def _known_phrase(phrase: str, corpus: str) -> bool:
    """Every name in the phrase is a known one ("Town Portal Scroll and Salve", "Black King Bar's Mithril
    Hammer": each part on its own, possessives off)."""
    for part in re.split(r"\s+and\s+|'s?\s+", phrase):
        words = re.sub(r"'s?$", "", part.strip()).split()
        while words and words[-1].lower() in ("of", "and", "the"):
            words.pop()
        if not words:
            continue
        name = " ".join(words).lower()
        if not any(re.search(rf"(?<![a-z']){re.escape(c)}(?![a-z])", corpus) for c in (name, name.rstrip("s"))):
            return False
    return True


@checker("unknown_names", 2, "names a hero who isn't in this match, or an item that doesn't exist")
def unknown_names(line: "Line", match: "Match") -> str | None:
    kb = match.kb
    ctx = line.ctx
    if kb is None or (not kb.items and not kb.heroes):
        return None
    problems = []
    if ctx is not None and ctx.heroes and kb.heroes:
        phantom = kb.hero_mentions(line.text) - set(ctx.heroes) - {ctx.hero}
        if phantom:
            problems.append(f"heroes not in this match: {', '.join(sorted(kb.hero_name(h) for h in phantom))}")
    if kb.items:
        corpus = match.memo.get("name_corpus")
        if corpus is None:
            corpus = match.memo["name_corpus"] = _name_corpus(kb)
        unknown = [m.group(1) for m in NAME_PHRASE.finditer(line.text) if not _known_phrase(m.group(1), corpus)]
        if unknown:
            problems.append(f"names no item, hero or place Doty knows: {', '.join(unknown)}")
    return "; ".join(problems)


# 3. the moment it was said ------------------------------------------------------------------------------------------
CLOCK_TIME = re.compile(r"(?<![\d:.])(\d{1,2}):([0-5]\d)(?![\d:])")
# Words that make a past time a fact about what happened, not a call to act ("Roshan died at 15:00").
PAST_CUE = re.compile(r"\b(died|dead|killed|was|were|ago|since|earlier|last\s+(time|fight|game)|fell|lost|took|"
                      r"got|happened|spawned|picked|bought|finished|already)\b", re.IGNORECASE)


@checker("stale_time", 2, f"names a clock time as something to do after its moment: more than "
                          f"{triggers.EVENT_LATE_S} s after an event at that time (a rune), {STALE_S} s otherwise")
def stale_time(line: "Line", match: "Match") -> str | None:
    """The prepared calls' own test (triggers.late_by): "Grab the power rune at 6:00" is late at 6:06, "Farm until
    Black King Bar by 14:00" only half a minute after 14:00. A real run showed the rune call at 6:26."""
    times = [int(m.group(1)) * 60 + int(m.group(2)) for m in CLOCK_TIME.finditer(line.text)]
    if not times:
        return None
    late = triggers.late_by(line.text, line.clock)
    if late is None or PAST_CUE.search(line.text):
        return ""
    return f"about {_mmss(max(times))}, said at {_mmss(line.clock)} ({late} s late)"


# A call to go and do something on the map, at the start of a sentence: no use to a player who is dead.
MAP_ACTION = re.compile(r"(?:^|[.!?;]\s+)(?:now,?\s+|then,?\s+)?(last[- ]hit|farm|push|gank|stack|pull|ward|fight|"
                        r"take|grab|go|walk|head|rotate|smoke|join|group|defend|contest)\b", re.IGNORECASE)
RESPAWN_CUE = re.compile(r"\b(respawn\w*|once\s+(you'?re\s+|you\s+are\s+)?back|when\s+(you'?re|you\s+are|you\s+get)"
                         r"\s+back|back\s+alive|buy\s*back|when\s+you\s+spawn)\b", re.IGNORECASE)
FIGHT_WORDS = re.compile(r"\b(fight\w*|kill|stun\w*|focus|target|use|press|pop|cast|blink|bkb|black\s+king\s+bar|"
                         r"save|dive|hit|ult\w*|disengage|go\s+in|jump|initiate|chase|run|escape)\b", re.IGNORECASE)


@checker("timing", 2, "said mid-fight or while dead, and not about the fight or the respawn", spoken_only=True)
def timing(line: "Line", match: "Match") -> str | None:
    ctx = line.ctx
    if ctx is None or ctx.alive is None:
        return None
    if ctx.alive is False:
        if line.source == "coach" and line.category != "trigger":
            return ""  # the coach only looks while you're dead to plan the respawn (death answers, pre-respawn look)
        if line.priority <= 1 or RESPAWN_CUE.search(line.text) or not MAP_ACTION.search(line.text):
            return ""
        return "a call to act on the map, said while the player was dead"
    if not ctx.fighting:
        return None if ctx.fighting is None else ""
    if line.priority >= 3:
        return "a routine line heard mid-fight"
    if line.priority <= 1 or _danger(line) or FIGHT_WORDS.search(line.text):
        return ""
    return "said mid-fight, and not about the fight"


# 4. how often ---------------------------------------------------------------------------------------------------------
@checker("repeats", 1, "near-identical to a line heard just before", spoken_only=True)
def repeats(line: "Line", match: "Match") -> str | None:
    words = llm.content_words(line.text)
    if len(words) < 2:
        return None
    window = DANGER_REPEAT_S if _danger(line) else REPEAT_WINDOW_S
    buys = _buy_targets(line.text)
    for prev in reversed(match.lines[:line.index]):
        if line.clock - prev.clock > window:
            break
        if not prev.said:
            continue
        before = _buy_targets(prev.text)
        if buys and before and buys != before:
            continue  # ("Buy Void Stone: a part of Battle Fury", then "Buy Broadsword: ...": the next step, not a repeat)
        if llm.says_again(words, llm.content_words(prev.text)):  # (the coach's own measure, Coach._repeats)
            return f'repeats "{_short(prev.text)}" from {line.clock - prev.clock} s before'
    return ""


@checker("rate", 1, f"more than {RATE_BUDGET} lines heard in a minute (danger calls aside)", spoken_only=True)
def rate(line: "Line", match: "Match") -> str | None:
    if _exempt(line):
        return None
    budget = _talk(match).per_min if match.talk else RATE_BUDGET
    heard = sum(1 for prev in match.lines[:line.index + 1]
                if prev.said and not _exempt(prev) and line.clock - RATE_WINDOW_S < prev.clock <= line.clock)
    return f"line {heard} heard in a minute (budget {budget})" if heard > budget else ""


@checker("coach_budget", 1, f"an unprompted coach line within {COACH_GAP_S} s of the last one", spoken_only=True)
def coach_budget(line: "Line", match: "Match") -> str | None:
    if line.source != "coach" or line.category not in ("coach", "trigger") or _exempt(line):
        return None
    gap = _talk(match).coach_gap_s if match.talk else COACH_GAP_S
    for prev in reversed(match.lines[:line.index]):
        if line.clock - prev.clock >= gap:
            break
        if prev.said and prev.source == "coach" and prev.category in ("coach", "trigger") and not _exempt(prev):
            return f"{line.clock - prev.clock} s after the coach's last line"
    return ""


def _exempt(line: "Line") -> bool:
    """Outside the budget, as the speech gate has it: a danger call, or a line the player asked for (or the start plan
    and a death answer)."""
    return _danger(line) or bool(getattr(line, "asked", False))


def _talk(match: "Match") -> speech.Talk:
    """The budget the match was played with ("How much Doty talks", speech.TALK)."""
    return speech.TALK.get(match.talk or "normal", speech.TALK["normal"])


def _buy_targets(text: str) -> frozenset[str]:
    """The things a line says to buy, as written ("Buy Broadsword (1,000)..." -> {"Broadsword"})."""
    return frozenset(m.group(1).strip() for m in NAME_PHRASE.finditer(text))


def _mmss(clock: int) -> str:
    sign = "-" if clock < 0 else ""
    return f"{sign}{abs(clock) // 60}:{abs(clock) % 60:02d}"


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"
