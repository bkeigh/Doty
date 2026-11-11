"""The other team, from who Doty knows is in the game (the lineup it reads off the top bar): when the
enemy's big items usually land, and the moments their heroes, or yours, are dead.

Fair play: the spikes are public statistics about those heroes (what their players usually have by
then), not this match's items; the dead heroes are the greyed-out portraits on the player's own screen.
"""

from __future__ import annotations

import time
from typing import Iterable

from ..advice import Advice, Priority
from ..knowledge import mode as game_mode
from ..lineup import SIDES, other
from .base import Rule, RuleContext, mmss


class EnemySpikes(Rule):
    """A card with the enemy's usual power spikes once they're known, and a heads-up shortly before the
    first few (Black King Bar, Blink, Battle Fury... by when half of that hero's players have it)."""

    id = "enemy_spikes"
    LEAD_S = 40  # the heads-up comes this long before the usual time
    MAX_CALLS = 3  # spoken heads-ups per match
    # (Game times below are a normal game's; a Turbo game reaches each in half the time.)
    GAP_S = 150  # game seconds between two of them
    MIN_TIME = 480  # items usually done before 8:00 aren't a spike worth a call
    CARD_BY = 720  # the card only makes sense in the first minutes

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb, mem = ctx.snap, ctx.kb, ctx.mem
        tracker = mem.get("lineup_votes")
        team = (s.team or "").lower()
        if kb is None or tracker is None or team not in SIDES:
            return
        mode = game_mode.of(mem)
        spikes = []
        for npc in tracker.confirmed(other(team)):
            hero = kb.heroes.get(npc)
            if hero:
                spikes += [(when, npc, item) for when, item in kb.spikes(int(hero["id"]))
                           if when >= mode.at(self.MIN_TIME)]
        if not spikes:
            return
        spikes.sort()
        if not mem.get("spike_card") and (tracker.complete or s.clock >= 60) and s.clock < mode.at(self.CARD_BY):
            ahead = [f"{kb.hero_name(npc)} {kb.item_name(item)} ~{mmss(when)}" for when, npc, item in spikes
                     if when > s.clock][:5]
            if ahead:
                mem["spike_card"] = True
                yield Advice("Their usual spikes: " + " · ".join(ahead), Priority.INFO, "plan", key="spike_card",
                             ttl_s=20.0, speak=False)
        if not s.alive:
            return
        calls: list[int] = mem.setdefault("spike_calls", [])
        if len(calls) >= self.MAX_CALLS or (calls and s.clock - calls[-1] < mode.at(self.GAP_S)):
            return
        said: set[str] = mem.setdefault("spikes_said", set())
        for when, npc, item in spikes:
            key = f"spike:{npc}:{item}"
            if when - self.LEAD_S <= s.clock < when and key not in said:
                said.add(key)
                calls.append(s.clock)
                hero, name = kb.hero_name(npc), kb.item_name(item)
                yield Advice(f"{hero}'s {name} usually lands by {mmss(when)}: fight before it, or play safer "
                             "once it's done", Priority.NORMAL, "objectives", key=key,
                             speech=f"{hero} gets {name} soon")
                return


class GamePlan(Rule):
    """Once all ten heroes are known: which side a long game favours, from the public win rate of each
    hero by game length, as the game plan ("their lineup is stronger late: win the game early")."""

    id = "game_plan"
    FROM, UNTIL = -90, 600  # the plan matters while there's still time to play for it

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, kb, mem = ctx.snap, ctx.kb, ctx.mem
        tracker = mem.get("lineup_votes")
        team = (s.team or "").lower()
        mode = game_mode.of(mem)
        if (kb is None or tracker is None or not tracker.complete or team not in SIDES or mem.get("game_plan")
                or not self.FROM <= s.clock <= mode.at(self.UNTIL)):
            return
        plan = kb.game_plan(tracker.confirmed(team), tracker.confirmed(other(team)))
        if plan is None:
            return  # (the win rates are still loading: asked again on the next update)
        mem["game_plan"] = plan.side or "even"
        mem["game_plan_brief"] = plan.brief()  # (the coach's match brief says the same, all match)
        if plan.side == "them":
            yield Advice(plan.card(), Priority.NORMAL, "plan", key="game_plan", ttl_s=25.0,
                         speech="They scale better. Win early.")
        elif plan.side == "you":
            yield Advice(plan.card(), Priority.NORMAL, "plan", key="game_plan", ttl_s=25.0,
                         speech="You scale better. Play safe.")


# Spoken word for word, so the local voice makes them ahead of time (listed in rules.FIXED_LINES).
PICKOFF_LINES = {
    2: "Two enemies dead. Push a tower.",
    3: "Three enemies dead. Take an objective.",
    4: "Four enemies dead. Take an objective.",
    5: "All five dead. Push high ground.",
}
TEAM_DOWN_LINE = "Your team is down. Back off."


class PickOff(Rule):
    """Greyed-out portraits on the top bar (read by Doty, once it knows all ten): two or more of them
    dead is the time to push or take an objective; two or more of your team dead while you're on their
    side of the river is the time to back off. Each once per such moment: a moment ends once fewer are
    dead for a few seconds (so a flicker in the reading doesn't call it twice, and the next fight's
    pick-off is called even seconds later)."""

    id = "pick_off"
    FRESH_S = 3.0  # the reading must be this recent
    MIN_CLOCK = 300  # in the first minutes a death or two doesn't open the map
    DEEP = 1000  # world units past the river (x + y), as the prepared calls' enemy_side
    CLOSE_S = 5.0  # fewer dead for this long ends the moment

    @classmethod
    def _moment(cls, mem: dict, name: str, now_open: bool) -> bool:
        """True when a moment opens; keeps it open through short dips."""
        below = f"{name}_below_t"
        if now_open:
            mem.pop(below, None)
            if mem.get(name):
                return False
            mem[name] = True
            return True
        if mem.get(name):
            since = mem.setdefault(below, time.monotonic())
            if time.monotonic() - since >= cls.CLOSE_S:
                mem.pop(name, None)
                mem.pop(below, None)
        return False

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, mem = ctx.snap, ctx.mem
        screen = mem.get("screen")
        team = (s.team or "").lower()
        if (not screen or not s.alive or s.clock < self.MIN_CLOCK or team not in SIDES
                or screen.get("match") != s.match_id or time.monotonic() - screen.get("t", -1e9) > self.FRESH_S):
            # No reading to go on (you're dead, or the bar isn't readable): a moment open from before ends
            # after a while, so the next pick-off after you respawn is called.
            self._moment(mem, "pickoff_open", False)
            self._moment(mem, "team_down_open", False)
            return
        dead = screen.get("dead") or {}
        enemies, allies = len(dead.get(other(team), [])), len(dead.get(team, []))
        # Each death is one (hero, when they went down): a reading that drops out and comes back with the same
        # heroes still dead is the same pick-off, not a new one.
        since = screen.get("since") or {}
        deaths = {(h, since.get(h)) for h in dead.get(other(team), [])}
        called: set = mem.setdefault("pickoff_called", set())
        if self._moment(mem, "pickoff_open", enemies >= 2) and not deaths <= called:
            called |= deaths
            yield Advice(f"{enemies} of them are dead: push a tower or take Roshan while they're down",
                         Priority.HIGH, "objectives", key=f"pickoff@{s.clock}", speech=PICKOFF_LINES[min(enemies, 5)])
        pos = s.position
        deep = pos is not None and ((pos[0] + pos[1]) if team == "radiant" else -(pos[0] + pos[1])) > self.DEEP
        if self._moment(mem, "team_down_open", allies >= 2 and deep):
            yield Advice(f"{allies} of your team are dead and you're on their side: back off", Priority.HIGH,
                         "survival", key=f"team_down@{s.clock}", speech=TEAM_DOWN_LINE)


def rules() -> list[Rule]:
    return [GamePlan(), EnemySpikes(), PickOff()]
