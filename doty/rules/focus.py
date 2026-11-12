"""This game's focus (knowledge/focus.py): chosen at the start of the match, said once at the horn with its target,
checked once or twice where the number can be seen mid-game, and nothing more. It must not become one more thing
that nags: at most three lines a game, each said once, at NORMAL priority so the speech budget and the level's
settings treat it like any other alert.

The choice is kept in match memory: `store.memory["focus"]` is the topic's key (other parts read it), and
`store.memory["focus_plan"]` holds its target and how it was chosen (the post-game review grades against it).
"""

from __future__ import annotations

import statistics
from typing import Iterable

from ..advice import Advice, Priority
from ..i18n import tr
from ..knowledge import focus as lib
from ..knowledge import mode as game_mode
from .base import Rule, RuleContext, mmss

# The topics' titles as the player reads them (the same as knowledge/focus.py's, here so they're translated).
TITLES = {
    "lh10": "Last hits at 10:00",
    "deaths_no_tp": "Deaths without a TP scroll",
    "skill_points": "Minutes with a skill point unspent",
    "deaths_with_gold": "Deaths holding 2,000+ gold",
    "farm_stalls": "Slow farming minutes after the lanes",
    "key_item": "Your first key item",
    "buyback": "Late deaths without buyback",
}
CHOOSE_FROM = -90  # the focus is chosen at the first update of the match (the strategy screen's end)
SAY_AT = (0, 20)  # said at the horn, within these seconds of it
LH_CHECKS = (300, 600)  # last hits: a word at 5:00 (halfway) and at 10:00 (the result)
CHECK_WINDOW = 10


class Focus(Rule):
    id = "focus"

    def __init__(self, history_path=None) -> None:
        self.history_path = history_path  # (None: the real history; tests give their own)

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s, mem = ctx.snap, ctx.mem
        if not s.in_game or s.clock < CHOOSE_FROM:
            return
        if "focus" not in mem:
            self._choose(ctx)
        plan = mem.get("focus_plan")
        if not plan or not plan.get("key"):
            return
        topic = lib.TOPICS[plan["key"]]
        if topic.switch in ctx.topics_off:
            return  # (switched off mid-game: it's still graded after, but not talked about)
        if SAY_AT[0] <= s.clock <= SAY_AT[1] and not mem.get("focus_said"):
            mem["focus_said"] = True
            line = self._goal(plan)
            if line:
                yield Advice(line, Priority.NORMAL, "focus", key="focus_start")
        role = mem.get("role")
        if not lib.any_role(topic) and lib.settled(role) and lib.role_group(role) not in topic.roles:
            return  # (chosen from the player's usual role on the hero, and they're playing another one today)
        if plan["key"] == "lh10":
            yield from self._lh_check(ctx, plan)
        elif plan["key"] == "key_item":
            yield from self._item_check(ctx, plan)

    # choosing --------------------------------------------------------------------------------------------------
    def _choose(self, ctx: RuleContext) -> None:
        mem, s, kb = ctx.mem, ctx.snap, ctx.kb
        mode = game_mode.of(mem).key
        history = lib.load_history(self.history_path)
        # The role isn't settled until about 4:30 (roles.py). Until then only a topic that suits every role is set,
        # unless the role is already sure, or this player nearly always plays this hero in one role: telling a
        # support to get 40 last hits is worse than saying nothing. Once said, the focus stays for the game (one
        # goal, never a second one a few minutes in).
        role = mem.get("role")
        if not lib.settled(role):
            role = lib.usual_role(history, s.hero_name, mode) or None
        choice = lib.choose(history, ctx.level, role, mode, ctx.topics_off, any_role_only=role is None)
        mem["focus"] = choice.key if choice else ""
        if choice is None:
            mem["focus_plan"] = {}
            return
        target = None
        try:
            target = lib.TOPICS[choice.key].target({
                "hero_id": s.hero_id, "role": role, "bracket": getattr(kb, "bracket", 0) if kb else 0,
                "mode": mode, "level": ctx.level, "cache_dir": getattr(kb, "cache_dir", None) if kb else None})
        except Exception as exc:  # (no card, odd data: a focus without a number is still a focus)
            print(f"[doty] focus target: {exc!r}")
        if target is None and choice.key == "key_item":  # (no card for this hero: the level's next topic)
            fallback = lib.choose(history, ctx.level, role, mode, ctx.topics_off | {"items"},
                                  any_role_only=role is None)
            mem["focus"] = fallback.key if fallback else ""
            if fallback is None:
                mem["focus_plan"] = {}
                return
            choice = fallback
            target = lib.TOPICS[choice.key].target({"level": ctx.level, "mode": mode})
        mem["focus_plan"] = {
            "key": choice.key, "why": choice.why, "recent": list(choice.recent), "level": ctx.level, "mode": mode,
            "target": target.value if target else None, "source": target.source if target else "",
            "item": target.item if target else "", "item_name": target.item_name if target else "",
            "at": target.at if target else 0}

    # what it says ------------------------------------------------------------------------------------------------
    @staticmethod
    def _goal(plan: dict) -> str:
        key, target, recent = plan["key"], plan.get("target"), plan.get("recent") or []
        avg = statistics.mean(recent) if recent else None
        if key == "lh10" and target:
            line = tr("This game: {n} last hits by 10:00.", n=int(target))
            return line + (" " + tr("You've averaged {n}.", n=int(round(avg))) if avg is not None else "")
        if key == "key_item" and plan.get("item_name"):
            line = tr("This game: {item} by {time}.", item=plan["item_name"], time=mmss(int(plan.get("at") or 0)))
            if avg is not None and avg > 30:
                line += " " + tr("It's been about {n} minutes late.", n=max(1, int(round(avg / 60))))
            return line
        lines = {
            "deaths_no_tp": "This game: never die without a TP scroll.",
            "deaths_with_gold": "This game: spend your gold before you fight. No deaths holding 2,000 gold.",
            "skill_points": "This game: spend every skill point as soon as you level up.",
            "buyback": "This game: from 30 minutes on, keep gold for buyback.",
            "farm_stalls": "This game: after the lanes, never go a minute without farming.",
        }
        line = tr(lines[key]) if key in lines else ""
        if line and avg is not None and avg > 0 and key != "farm_stalls":
            line += " " + tr("Last games: {n} a game.", n=_num(avg))
        return line

    def _lh_check(self, ctx: RuleContext, plan: dict) -> Iterable[Advice]:
        s, target = ctx.snap, plan.get("target")
        if not target:
            return
        lh = int(s.player("last_hits", 0) or 0)
        for at in LH_CHECKS:
            if not at <= s.clock < at + CHECK_WINDOW or ctx.mem.get(f"focus_lh@{at}"):
                continue
            ctx.mem[f"focus_lh@{at}"] = True
            if at < 600:
                half = int(round(target * at / 600))
                text = (tr("{n} last hits at 5:00: on pace for your {target}.", n=lh, target=int(target))
                        if lh >= half else
                        tr("{n} last hits at 5:00. Your {target} needs {half} by now: stay on the wave.",
                           n=lh, target=int(target), half=half))
            elif lh >= target:
                text = tr("{n} last hits at 10:00: target hit.", n=lh)
            else:
                text = tr("{n} last hits at 10:00: {short} short of {target}.", n=lh, short=int(target) - lh,
                          target=int(target))
            yield Advice(text, Priority.NORMAL, "focus", key=f"focus_lh_{at}")

    def _item_check(self, ctx: RuleContext, plan: dict) -> Iterable[Advice]:
        """One word at the item's target time if it isn't done: how much is left."""
        s, kb, at, item = ctx.snap, ctx.kb, int(plan.get("at") or 0), plan.get("item")
        if not item or not at or not at <= s.clock < at + CHECK_WINDOW or ctx.mem.get("focus_item_check"):
            return
        ctx.mem["focus_item_check"] = True
        if item in s.all_item_names():
            return
        left = kb.cost_left(item, s.item_list()) if kb is not None and kb.items else 0
        text = (tr("{item} target time: {left} gold to go.", item=plan.get("item_name") or item, left=f"{left:,}")
                if left else tr("{item} target time.", item=plan.get("item_name") or item))
        yield Advice(text, Priority.NORMAL, "focus", key="focus_item")


def _num(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:.1f}"


def rules() -> list[Rule]:
    return [Focus()]
