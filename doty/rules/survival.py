"""Staying alive: save items, HP danger, TP scroll, death patterns, buyback."""

from __future__ import annotations

import time
from typing import Iterable

from ..advice import Advice, Priority
from ..knowledge import mode as game_mode
from ..state import Snapshot
from .base import Rule, RuleContext

TP_COST = 100  # close enough for "can you afford one"; exact cost isn't needed to nag
TRAVEL_BOOTS = {"item_travel_boots", "item_travel_boots_2"}

# Your items that get you out of a burst, best first, and what Doty says. Blink isn't here: taking damage
# puts it on cooldown. These are said word for word, so they're voiced ahead of time (rules.spoken_lines).
SAVE_CALLS: tuple[tuple[str, str], ...] = (
    ("item_glimmer_cape", "Glimmer yourself"),
    ("item_black_king_bar", "BKB now"),
    ("item_ghost", "Ghost now"),
    ("item_force_staff", "Force Staff out"),
    ("item_hurricane_pike", "Pike out"),
    ("item_cyclone", "Eul's yourself"),
    ("item_wind_waker", "Wind Waker yourself"),
    ("item_invis_sword", "Shadow Blade out"),
    ("item_silver_edge", "Silver Edge out"),
    ("item_shadow_amulet", "Shadow Amulet now"),
    ("item_satanic", "Satanic now"),
    ("item_blade_mail", "Blade Mail now"),
    ("item_lotus_orb", "Lotus yourself"),
    ("item_crimson_guard", "Crimson Guard now"),
    ("item_pipe", "Pipe now"),
    ("item_guardian_greaves", "Greaves now"),
    ("item_mekansm", "Mek now"),
    ("item_magic_wand", "Wand now"),
    ("item_magic_stick", "Stick now"),
)
# Silenced in a fight: the items that remove it (a basic dispel on yourself).
DISPEL_CALLS: tuple[tuple[str, str], ...] = (
    ("item_manta", "Manta the silence"),
    ("item_lotus_orb", "Lotus yourself"),
    ("item_cyclone", "Eul's yourself"),
    ("item_wind_waker", "Wind Waker yourself"),
    ("item_disperser", "Disperser yourself"),
    ("item_black_king_bar", "BKB now"),
    ("item_guardian_greaves", "Greaves now"),
)
WAND_MIN_CHARGES = 8  # a Magic Wand or Stick with fewer is hardly a heal
BURST = 0.25  # this share of your max HP gone in two seconds is a burst
BURST_BELOW = 0.6  # ...when it leaves you under this much HP
SAVE_GAP_S = 20.0  # one save call per fight
SAVE_HOLD_S = 4.0  # "back off" doesn't talk over a save call this fresh


def save_call(snap: Snapshot, silenced_fight: bool = False) -> tuple[str, str] | None:
    """(item, what to say) for the best save item you can press right now, or None. Stunned, hexed or muted
    you can't press anything, so there's nothing to call."""
    if any(snap.hero(f, False) for f in ("stunned", "hexed", "muted")):
        return None
    ready = snap.ready_items()
    if not ready:
        return None
    immune = bool(snap.hero("magicimmune", False))
    charges = {it.name: it.charges for it in snap.inventory}
    for item, line in DISPEL_CALLS if silenced_fight else SAVE_CALLS:
        if item not in ready or (immune and item == "item_black_king_bar"):
            continue
        if item in ("item_magic_wand", "item_magic_stick") and charges.get(item, 0) < WAND_MIN_CHARGES:
            continue
        return item, line
    return None


def save_lines() -> list[str]:
    return list(dict.fromkeys(line for _, line in SAVE_CALLS + DISPEL_CALLS))


class SaveItems(Rule):
    """The moment you're bursted (a quarter of your HP gone in two seconds) or low and still losing HP, or
    silenced mid-fight, with a save item ready: name it. Players die with Glimmer or BKB off cooldown all the
    time; the AI can't see a burst coming in time, so Doty calls this one itself, in milliseconds."""

    id = "save_items"
    cooldown_s = 0.0  # (its own gap below: the keys are per moment)

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, mem = ctx.snap, ctx.mem
        if not s.alive or s.paused or s.in_own_base or s.max_hp == 0 or not s.has_items_section:
            return
        now = time.monotonic()
        if now - mem.get("save_call_t", -1e9) < SAVE_GAP_S:
            return
        lost = -ctx.store.hp_trend(2.0)
        hurt = (lost >= BURST and s.hp_pct <= BURST_BELOW) or (s.hp_pct <= ctx.cfg.low_hp_pct and lost >= 0.05)
        silenced = bool(s.hero("silenced", False)) and ctx.store.in_fight()
        if hurt:
            call = save_call(s)
        elif silenced:
            call = save_call(s, silenced_fight=True)
        else:
            return
        if call is None:
            return
        item, line = call
        mem["save_call_t"] = now
        why = f"{lost:.0%} HP lost in 2 s" if hurt else "silenced"
        name = ctx.kb.item_name(item) if ctx.kb else item.removeprefix("item_").replace("_", " ").title()
        yield Advice(f"{name} is ready: use it ({why})", Priority.CRITICAL, "survival",
                     key=f"save@{s.clock}", speech=line, ttl_s=4)


# Heroes whose own spells spend their health in big steps (Burning Spears, Sunder, Sun Ray, Blast Off): a
# sudden drop is them playing the hero, not being bursted.
SELF_HURT = frozenset({"npc_dota_hero_huskar", "npc_dota_hero_terrorblade", "npc_dota_hero_phoenix",
                       "npc_dota_hero_techies"})


class LowHp(Rule):
    """Low and still dropping: back off. And before that point: a burst (a quarter of your HP in two seconds)
    with no save item to press. At that rate you're dead in a few seconds, and waiting for the low-HP line
    is waiting too long, so Doty says it while there's still HP to walk away with."""

    id = "low_hp"
    cooldown_s = 10.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not s.alive or s.in_own_base or s.max_hp == 0:
            return
        if ctx.store is not None and time.monotonic() - ctx.mem.get("save_call_t", -1e9) < SAVE_HOLD_S:
            return  # a save call was just made: "Glimmer yourself" beats "back off", and isn't cut off
        lost = -ctx.store.hp_trend(2.0)
        if s.hp_pct <= ctx.cfg.low_hp_pct and lost >= 0.05:
            yield Advice(f"Low HP ({s.hp_pct:.0%}) and dropping — back off", Priority.CRITICAL,
                         "survival", key="low_hp", speech="Back off, low HP", ttl_s=4)
        elif lost >= BURST and s.hp_pct <= BURST_BELOW and s.hero_name not in SELF_HURT:
            # Once per fight, like the save calls, and not on top of one ("BKB now", then "Get out now"). Not
            # while you can't act on it (stunned, hexed), nor under spell immunity, where you chose to stay in.
            now = time.monotonic()
            if (now - max(ctx.mem.get("save_call_t", -1e9), ctx.mem.get("burst_call_t", -1e9)) < SAVE_GAP_S
                    or any(s.hero(f, False) for f in ("stunned", "hexed", "magicimmune"))):
                return
            ctx.mem["burst_call_t"] = now
            yield Advice(f"{lost:.0%} of your HP gone in 2 seconds — get out before the next hit", Priority.CRITICAL,
                         "survival", key="low_hp", speech="Get out now", ttl_s=4)


class MissingTp(Rule):
    id = "missing_tp"
    cooldown_s = 90.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not s.has_items_section or not s.alive or s.clock < 150:
            return
        if s.teleport is not None or s.all_item_names() & TRAVEL_BOOTS:
            return
        if s.gold >= TP_COST:
            yield Advice("No TP scroll — buy one", Priority.NORMAL, "survival",
                         key="missing_tp", speech="Buy a TP scroll")


class DeathWatch(Rule):
    """Tracks deaths; flags a feeding streak so the player adjusts."""

    id = "death_watch"
    cooldown_s = 0.0

    WINDOW_S = 360
    THRESHOLD = 3

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not ctx.store.just_died or ctx.store.current is not s:  # a real death (not Aegis), this update
            return
        deaths: list[int] = ctx.mem.setdefault("death_clocks", [])
        deaths.append(s.clock)
        recent = [c for c in deaths if s.clock - c <= self.WINDOW_S]
        if len(recent) >= self.THRESHOLD:
            yield Advice(f"{len(recent)} deaths in {self.WINDOW_S // 60} min — farm safer spots, wait for your team",
                         Priority.HIGH, "survival", key=f"death_streak@{s.clock}",
                         speech="Too many deaths. Stay near your team.")
        respawn = int(s.hero("respawn_seconds", 0) or 0)
        if respawn >= 20 and s.gold >= 1000:
            yield Advice(f"Dead for {respawn}s with {s.gold} gold — buy now so it's ready on respawn",
                         Priority.NORMAL, "economy", key=f"dead_shop@{s.clock}",
                         speech="Spend your gold while dead")


class Buyback(Rule):
    id = "buyback"
    cooldown_s = 300.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if s.clock < game_mode.of(ctx.mem).at(1800) or not s.alive:  # (30:00; a Turbo game's 15:00)
            return
        cost = int(s.hero("buyback_cost", 0) or 0)
        wait = int(float(s.hero("buyback_cooldown", 0) or 0))
        if wait > 0:  # (used recently: gold doesn't help until it's back)
            yield Advice(f"Buyback is on cooldown for {wait // 60}:{wait % 60:02d} — don't overextend", Priority.INFO,
                         "survival", key="buyback", speech="No buyback. Play safe.")
        elif cost and s.gold < cost:
            yield Advice(f"No buyback: {s.gold}/{cost} gold — don't overextend", Priority.INFO,
                         "survival", key="buyback", speech="No buyback. Play safe.")


class GankRisk(Rule):
    """Past the river, exposed: low HP or no TP ready to escape, worse at night (less vision). Warned once,
    as you cross, rather than after the gank has started (the HP rule covers that)."""

    id = "gank_risk"
    cooldown_s = 75.0
    DEEP = 1500  # world units past the river diagonal (x + y), so standing on the river doesn't count

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not s.alive or s.clock < 300 or s.position is None or (s.team or "").lower() not in ("radiant", "dire"):
            return
        x, y = s.position
        depth = (x + y) if s.team.lower() == "radiant" else -(x + y)
        if depth < self.DEEP:
            ctx.mem.pop("gank_risk_armed", None)
            return
        if ctx.mem.get("gank_risk_armed"):
            return  # once per trip across the river
        screen = ctx.mem.get("screen") or {}
        if screen.get("match") == s.match_id and time.monotonic() - screen.get("t", -1e9) <= 3.0:
            enemy = "dire" if s.team.lower() == "radiant" else "radiant"
            if len((screen.get("dead") or {}).get(enemy, [])) >= 2:
                return  # two or more of them are dead (read off the top bar): crossing now is the right call
        tp = s.teleport
        no_escape = (tp is None or float(tp.cooldown or 0) > 0) and not (s.all_item_names() & TRAVEL_BOOTS)
        low = s.hp_pct < 0.5
        if not (low or no_escape):
            return
        ctx.mem["gank_risk_armed"] = True
        why = " and ".join(w for w, on in (("low HP", low), ("no TP ready", no_escape)) if on)
        night = "" if s.daytime else " at night"
        speech = "Head back, no TP ready" if no_escape else "Head back, you're low"
        yield Advice(f"Deep on their side{night} with {why} — ward or head back", Priority.NORMAL, "survival",
                     key="gank_risk", speech=speech)


def rules() -> list[Rule]:
    return [SaveItems(), LowHp(), MissingTp(), DeathWatch(), Buyback(), GankRisk()]
