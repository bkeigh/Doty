"""Leveling and farm pace: skill points, power spikes, benchmark check-ins."""

from __future__ import annotations

from typing import Iterable

from ..advice import Advice, Priority
from ..knowledge import cards
from ..knowledge import mode as game_mode
from . import own_way
from .base import Rule, RuleContext, mmss


def _is_support(ctx: RuleContext) -> bool:
    """Doty has worked out that the player is a support this game (knowledge/roles.py)."""
    role = ctx.mem.get("role") if ctx.store is not None else None
    return bool(role is not None and role.support)


class UnspentSkillPoint(Rule):
    """A skill point held for a few seconds: said once per point (the level it came with), and again only if it's
    still held after a long while. (Every 45 s, as before, was nagging: a replay showed "Skill point: take Healing
    Ward" three times for one point.)"""

    id = "skill_point"
    cooldown_s = 1.0  # (the rule keeps its own once-per-point memory)
    DWELL_S = 6.0
    AGAIN_S = 240.0  # the same point, still not spent: one more reminder after this long
    # Innate abilities and level-17+ point rules make the count noisy late; the
    # reminder matters most in the laning stage anyway.
    MAX_LEVEL = 16

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb = ctx.snap, ctx.kb
        unspent = kb.unspent_points(s) if kb else s.unspent_skill_points
        if not unspent or s.level > self.MAX_LEVEL:
            ctx.mem.pop("skill_since", None)
            return
        since = ctx.mem.setdefault("skill_since", s.received_t)
        said = ctx.mem.get("skill_said")  # (level, when) of the last reminder
        if said is not None and said[0] == s.level and s.received_t - said[1] < self.AGAIN_S:
            return
        if s.received_t - since >= self.DWELL_S:
            ctx.mem["skill_said"] = (s.level, s.received_t)
            nxt = kb.next_skill(s) if kb else None
            if nxt:  # the pro order this patch, from where you are now
                name = kb.ability_name(nxt)
                yield Advice(f"Skill point: take {name}", Priority.NORMAL, "progression",
                             key="skill_point", speech=f"Level {name}")
            else:
                yield Advice("Unspent skill point", Priority.NORMAL, "progression",
                             key="skill_point", speech="Level a skill")


# The basics, said once each for a player new to Dota (level "new"), at a calm moment early in their game.
BASICS = (
    (20, "Stand behind your creeps. Hit enemy creeps only when they're almost dead."),
    (100, "Hit your own creeps when they're low too. That's a deny."),
    (180, "Check the minimap. If enemies are missing, step back."),
    (270, "Your courier brings items. Buy them, then press its deliver button."),
    (390, "Low on health? Walk back to your fountain to heal."),
    (540, "Use your spells on enemy heroes when they come close."),
    (660, "When your team fights, go help. Don't walk alone."),
    (900, "Stay with your team now, and take towers together."),
)


COURIER_BASIC_AT = 270


class Basics(Rule):
    """For a player who's never played (level "new"): one basic at a time, early in the game, while nothing
    else is going on."""

    id = "basics"

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, mem = ctx.snap, ctx.mem
        if ctx.level != "new" or not s.alive or s.paused or not s.in_game:
            return
        said: set[int] = mem.setdefault("basics_said", set())
        for at, line in BASICS:
            if at <= s.clock < at + 60 and at not in said:
                said.add(at)
                if at == COURIER_BASIC_AT and game_mode.of(mem).turbo:
                    return  # (Turbo's courier delivers by itself)
                yield Advice(line, Priority.NORMAL, "progression", key=f"basics@{at}", speech=line, ttl_s=10.0)
                return


class PowerSpike(Rule):
    id = "power_spike"

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, p = ctx.snap, ctx.prev
        if p is None or not p.has_hero:
            return
        if p.level < 6 <= s.level:
            yield Advice("Level 6 — ultimate online, look for a play", Priority.INFO, "progression",
                         key="lvl6", speech="Level 6. Look for a kill.")


class FarmCheck(Rule):
    """At 10/20 minutes compare your last hits with players of your hero, role and rank and the rank above (the
    hero card's pace, knowledge/cards.py: an estimate from public benchmarks, so said with "about"). Without a
    card, with the hero's OpenDota median as before: those are full-match averages and pace climbs as a game goes
    on, so that check only speaks up when the gap is large, and says what it compares. Both are from normal games:
    a Turbo game has no such benchmark yet, so this stays quiet there (the 5:00 lane check still runs: a lane's
    creeps are the same).
    """

    id = "farm_check"
    CHECKPOINTS = (600, 1200)
    SLOW, FAST = 0.7, 1.15
    BEHIND_UP = 0.8  # under this share of what players one rank up have is worth saying

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb = ctx.snap, ctx.kb
        mode = game_mode.of(ctx.mem)
        if kb is None or _is_support(ctx) or mode.turbo:
            return  # (a support's job isn't last hits: the hero's farm benchmark doesn't apply)
        for cp in self.CHECKPOINTS:
            if not cp <= s.clock < cp + 5:
                continue
            lh = int(s.player("last_hits", 0) or 0)
            pace = cards.pace(s.hero_id, ctx.mem.get("role"), getattr(kb, "bracket", 0), mode,
                              getattr(kb, "cache_dir", None)) if s.hero_id else {}
            ref = ((pace.get("ref") or {}).get(cp) or {}).get("lh") if pace.get("available") else None
            if ref:
                yield from self._by_rank(lh, cp, ref, ((pace.get("up") or {}).get(cp) or {}).get("lh"))
                return
            median = kb.benchmark_median(s.hero_id, "last_hits_per_min")
            if not median:
                return
            pace = lh / (s.clock / 60.0)
            hero = kb.hero_name(s.hero_name)
            if pace < median * self.SLOW:
                yield Advice(f"{lh} last hits at {cp // 60}:00 ({pace:.1f}/min). Typical {hero} games average "
                             f"{median:.1f}/min — spend more time farming", Priority.INFO, "progression",
                             key=f"farm@{cp}", speech="Farm is behind. Focus last hits.")
            elif pace > median * self.FAST:
                yield Advice(f"{lh} last hits at {cp // 60}:00 ({pace:.1f}/min) — ahead of the typical {hero} "
                             f"pace of {median:.1f}/min", Priority.INFO, "progression", key=f"farm@{cp}")

    def _by_rank(self, lh: int, cp: int, ref: int, up: int | None) -> Iterable[Advice]:
        """Against the rank above ("Players one rank up have about 48 last hits at 10:00; you have 31"), or your
        own rank's at the top of the ladder or with no rank set."""
        at = f"{cp // 60}:00"
        if up and lh < up * self.BEHIND_UP:
            yield Advice(f"Players one rank up have about {up} last hits at {at}; you have {lh}. Farm between fights",
                         Priority.INFO, "progression", key=f"farm@{cp}", speech="Farm is behind. Focus last hits.")
        elif not up and lh < ref * self.SLOW:
            yield Advice(f"Players of your rank have about {ref} last hits at {at}; you have {lh}. Farm between "
                         "fights", Priority.INFO, "progression", key=f"farm@{cp}",
                         speech="Farm is behind. Focus last hits.")
        elif up and lh >= up:
            yield Advice(f"{lh} last hits at {at}: ahead of players one rank up (about {up})", Priority.INFO,
                         "progression", key=f"farm@{cp}")


class GameStartCard(Rule):
    """At the horn: your game plan from data, on one card. The skill order pro players use on this
    patch and the core build with when it usually lands."""

    id = "game_start"

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb = ctx.snap, ctx.kb
        if kb is None or not -45 <= s.clock <= 15 or ctx.mem.get("game_start_card"):
            return
        if ctx.mem.get("mode_pending") and s.clock < 0:
            return  # (normal game or Turbo is still being looked up: the timings on this card depend on it)
        hero = kb.heroes.get(s.hero_name)
        if not hero:
            return
        hero_id = int(hero["id"])
        skills = [kb.ability_name(a) for a in kb.skill_order(hero_id) if not a.startswith("special_bonus")][:4]
        if "skills" in ctx.topics_off:
            skills = []
        core = []
        timed = [] if "items" in ctx.topics_off else [
            (kb.typical_time(hero_id, item), item) for item in kb.typical_items(hero_id, 4)[:3]]
        for when, item in sorted(timed, key=lambda t: (t[0] is None, t[0] or 0)):  # in the order they land
            core.append(kb.item_name(item) + (f" (~{mmss(when)})" if when else ""))
        if not skills and not core:
            return
        ctx.mem["game_start_card"] = True
        parts = []
        if skills:
            parts.append("Skills: " + " › ".join(skills))
        if core:
            parts.append("Build: " + " › ".join(core))
        yield Advice(" · ".join(parts), Priority.INFO, "plan", key="game_start", ttl_s=20.0, speak=False)


class ItemPace(Rule):
    """While there's still time to act: the next core item usually lands by a time, and you're short."""

    id = "item_pace"
    LEAD_S = 120  # speak up this long before the usual time

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb = ctx.snap, ctx.kb
        mode = game_mode.of(mem := ctx.mem)
        if kb is None or not s.alive or s.clock < mode.at(300) or not s.has_items_section:
            return
        hero = kb.heroes.get(s.hero_name)
        if not hero:
            return
        hero_id = int(hero["id"])
        have = s.all_item_names()
        owned = kb.owned_with_parts(have)
        due = sorted((when, item) for item in kb.typical_items(hero_id, 4)
                     if item not in owned and item not in kb.skipped and not kb.second_boots(item, have)
                     and not kb.weak_here(item)  # (a counter item that answers little of this lineup)
                     and (when := kb.typical_time(hero_id, item)) is not None)
        if own_way.mode_of(getattr(ctx, "prefs", None)) != own_way.REMIND:
            # "Usually lands by" about an item the player isn't building is an argument for it: unless "When I go
            # my own way" is on "remind", the pace is only given for the item they're building now.
            target = kb.next_target(s.hero_name, s.clock, s.item_list())
            due = [(when, item) for when, item in due if item == target]
        for when, item in due:  # in the order they usually land
            if s.clock >= when:
                continue  # its time has passed (skipped, sold or just late): the next one matters now
            if s.clock < when - mode.at(self.LEAD_S) or mem.get(f"pace:{item}"):
                return  # only ever the next item on the way
            # What's still to pay, counting the parts already bought (a Perseverance in the bag is most of the way
            # to Battle Fury, not 3,900 short).
            short = kb.cost_left(item, s.item_list()) - s.gold
            if short > 300:
                mem[f"pace:{item}"] = True
                name = kb.item_name(item)
                yield Advice(f"{name} usually lands by {mmss(when)} — you're {short:,} gold short; "
                             "farm between waves", Priority.INFO, "economy", key=f"pace:{item}")
            return


class LaningTips(Rule):
    """The laning stage, from the game data: a nudge to deny when you haven't, and one when your last
    hits at 5:00 are far under the pace for your hero. Each once a match. (Creep aggro needs to see the
    creeps, which the game data doesn't send; that waits for the screen readers.)"""

    id = "laning"
    DENY_AT = (150, 330)  # 2:30 and 5:30
    MIN_LAST_HITS = 4  # farming a lane (not roaming or jungling)
    PACE_AT = 300  # 5:00
    EARLY_PACE = 0.75  # the early target, as a share of the hero's whole-game median (the overlay gauge's too)
    FAR_BEHIND = 0.6  # under this share of the target is worth saying

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb, mem = ctx.snap, ctx.kb, ctx.mem
        if not s.alive or not 60 <= s.clock < 400:
            return
        lh, dn = int(s.player("last_hits", 0) or 0), int(s.player("denies", 0) or 0)
        for at in self.DENY_AT:
            if at <= s.clock < at + 10 and not mem.get(f"deny@{at}"):
                mem[f"deny@{at}"] = True
                if dn == 0 and lh >= self.MIN_LAST_HITS and not mem.get("deny_said"):
                    mem["deny_said"] = True
                    yield Advice(f"{lh} last hits, no denies yet: hit your own creeps once they drop under half "
                                 "HP. The enemy loses that gold and most of the XP", Priority.INFO, "progression",
                                 key="deny", speech="Deny your creeps too")
        if kb is None or not self.PACE_AT <= s.clock < self.PACE_AT + 10 or mem.get("lane_pace"):
            return
        mem["lane_pace"] = True
        if _is_support(ctx):
            return  # (the lane's last hits are the core's to take)
        median = kb.benchmark_median(s.hero_id, "last_hits_per_min")
        if not median:
            return
        target = median * self.EARLY_PACE * self.PACE_AT / 60
        if lh < target * self.FAR_BEHIND:
            yield Advice(f"{lh} last hits at 5:00; aim for about {target:.0f}. Stay next to the wave and hit "
                         "when a creep is one attack from dying", Priority.INFO, "progression", key="lane_pace",
                         speech="Stay on the wave, last-hit")


def rules() -> list[Rule]:
    return [UnspentSkillPoint(), PowerSpike(), FarmCheck(), GameStartCard(), ItemPace(), LaningTips(), Basics()]
