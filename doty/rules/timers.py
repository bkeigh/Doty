"""Predictive objective timers: fire *before* the spawn, so latency is negative.

Each spawn time gets a unique key ("power@480"), so it's announced exactly once
per match even across reconnects.
"""

from __future__ import annotations

from typing import Iterable

from .. import i18n
from ..advice import Advice, Priority
from ..knowledge import mode as game_mode
from ..knowledge import timings
from ..knowledge.timings import Spawn
from .base import Rule, RuleContext, mmss


RUNE = (" rune", " runes")


def joined(labels: list[str]) -> str:
    """Spawns at the same moment, as one name: "Bounty and power runes", "Power rune and wisdom shrines"."""
    runes = [lb for lb in labels if lb.endswith(RUNE)]
    phrases = [lb for lb in labels if not lb.endswith(RUNE)]
    if len(runes) == 1:
        phrases.insert(0, runes[0])
    elif runes:
        kinds = [runes[0].split()[0]] + [r.split()[0].lower() for r in runes[1:]]
        phrases.insert(0, f"{', '.join(kinds[:-1])} and {kinds[-1]} runes")
    phrases = [phrases[0]] + [p[0].lower() + p[1:] for p in phrases[1:]]
    if len(phrases) == 1:
        return phrases[0]
    if len(phrases) == 2 and " and " not in phrases[0]:
        return f"{phrases[0]} and {phrases[1]}"
    return ", ".join(phrases)


def timer_lines(cfg, until: int = 4800) -> list[str]:
    """Every spoken timer line a match can have (spawns that coincide share one), so the voice makes them
    ahead of time."""
    out: dict[str, None] = {}
    if getattr(cfg, "announce_stacks", False):
        out[f"Stack in {int(getattr(cfg, 'stack_lead_s', 7))}"] = None
    for table in (timings.SPAWNS, timings.TURBO_SPAWNS):  # (spawns that share a second differ between the modes)
        for _, group in _groups(table, cfg, -90, until + 90):
            lead = int(getattr(cfg, group[0].lead_setting, 15))
            out[f"{joined([sp.label for sp in group])} in {lead}"] = None
    return list(out)


def _groups(spawns: list[Spawn], cfg, clock: int, horizon: int | None = None) -> list[tuple[int, list[Spawn]]]:
    """(time, spawns) coming up within `horizon` seconds of `clock` (default: each spawn's warning lead), for
    the spawns said the usual way ("<label> in N"), grouped when they land at the same second with the same
    priority and lead."""
    found: dict[tuple[int, int, str], list[Spawn]] = {}
    for sp in spawns:
        if sp.key in ("stack", "tormentor") or (sp.toggle and not getattr(cfg, sp.toggle, True)):
            continue
        ahead = horizon if horizon is not None else int(getattr(cfg, sp.lead_setting, 15))
        for t in sp.times_after(clock, ahead):
            found.setdefault((t, sp.priority, sp.lead_setting), []).append(sp)
    return [(t, group) for (t, _, _), group in sorted(found.items(), key=lambda kv: kv[0][0])]


class SpawnTimer(Rule):
    id = "spawn_timer"
    needs_hero = False

    def __init__(self, spawns: list[Spawn] | None = None) -> None:
        self.spawns = spawns  # None: the table for this game's mode (a normal game, or Turbo)

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not s.in_game or s.paused:
            return
        spawns = self.spawns if self.spawns is not None else timings.spawns(game_mode.of(ctx.mem).turbo)
        for sp in spawns:
            if sp.key not in ("stack", "tormentor") or (sp.toggle and not getattr(ctx.cfg, sp.toggle, True)):
                continue
            lead = int(getattr(ctx.cfg, sp.lead_setting, 15))
            for t in sp.times_after(s.clock, lead):
                secs = t - s.clock
                if sp.key == "stack":
                    text, speech = f"Stack in {secs}s (pull at {mmss(t)})", f"Stack in {secs}"
                else:
                    side = timings.tormentor_side(t)
                    text, speech = f"Tormentors spawn in {secs}s — {side}", "Tormentors spawning"
                yield Advice(text, Priority(sp.priority), "timer", key=f"{sp.key}@{t}", speech=speech,
                             ttl_s=lead + 2)
        # Everything else: spawns landing together are one line ("Bounty and power runes in 15"), not two.
        for t, group in _groups(spawns, ctx.cfg, s.clock):
            lead = int(getattr(ctx.cfg, group[0].lead_setting, 15))
            secs, name = t - s.clock, joined([sp.label for sp in group])
            spoken = f"{name} in {secs}"
            if i18n.language() != "en":
                name = ", ".join(i18n.tr(sp.label) for sp in group)
                spoken = i18n.tr("{name} in {secs}", name=name, secs=secs)
            yield Advice(f"{name} in {secs}s ({mmss(t)})" if i18n.language() == "en" else spoken,
                         Priority(min(sp.priority for sp in group)), "timer",
                         key="+".join(f"{sp.key}@{t}" for sp in group), speech=spoken, ttl_s=lead + 2)


class RoshanAegis(Rule):
    """Roshan respawn window from the public kill event, and Aegis expiry if you hold it."""

    id = "roshan"
    needs_hero = False

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, mem = ctx.snap, ctx.mem
        turbo = game_mode.of(mem).turbo
        for ev in ctx.new_events:
            if ev.get("event_type") == "roshan_killed":
                killed = s.event_clock(ev)
                mem["roshan_killed_clock"] = killed
                if s.clock - killed < 30:  # don't announce history when Doty starts mid-game
                    lo, hi = timings.roshan_window(killed, turbo)
                    yield Advice(f"Roshan down — respawns {mmss(lo)}–{mmss(hi)}", Priority.INFO, "timer",
                                 key=f"rosh_dead@{killed}", speech=None)
        killed = mem.get("roshan_killed_clock")
        if killed is not None and not s.paused:
            lo, hi = timings.roshan_window(killed, turbo)
            if lo - 20 <= s.clock < lo:
                yield Advice(f"Roshan window opens at {mmss(lo)}", Priority.NORMAL, "timer",
                             key=f"rosh_lo@{killed}", speech="Roshan can spawn in 20")
            if hi <= s.clock < hi + 5:
                yield Advice("Roshan is definitely up", Priority.NORMAL, "timer",
                             key=f"rosh_hi@{killed}", speech="Roshan is up")

        if not s.has_hero:
            return
        holding = s.has_item("item_aegis")
        if holding and "aegis_clock" not in mem:
            mem["aegis_clock"] = s.clock
        elif not holding:
            mem.pop("aegis_clock", None)
        got = mem.get("aegis_clock")
        if got is not None:
            expires = got + timings.aegis_s(turbo)
            if expires - 30 <= s.clock < expires:
                yield Advice("Aegis expires in 30s — use it or lose it", Priority.HIGH, "timer",
                             key=f"aegis@{got}", speech="Aegis ends in 30")


class DayNight(Rule):
    """Natural day/night flips; from 15:00 each one also moves Roshan (7.41)."""

    id = "day_night"
    needs_hero = False

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, p = ctx.snap, ctx.prev
        if p is None or not s.in_game or s.clock <= 0:
            return
        day = timings.is_day(s.clock)
        if timings.is_day(p.clock) == day:
            return
        half = timings.DAY_NIGHT_CYCLE_S // 2
        flip = s.clock - s.clock % half
        if s.clock >= timings.ROSHAN_ROAMS_FROM:
            yield Advice(f"{'Day' if day else 'Night'}: Roshan moves to the {timings.roshan_pit(s.clock)}",
                         Priority.INFO, "timer", key=f"roshpit@{flip}", speech=None)
        elif ctx.cfg.announce_day_night:
            label = "Day" if day else "Night"
            yield Advice(f"{label} — vision {'returns' if day else 'shrinks'}", Priority.INFO, "timer",
                         key=f"daynight@{flip}", speech=label)


def rules() -> list[Rule]:
    return [SpawnTimer(), RoshanAegis(), DayNight()]
