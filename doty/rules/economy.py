"""Gold: what to buy next (part by part), unspent gold, items stuck in the stash."""

from __future__ import annotations

import time
from typing import Iterable

from ..advice import Advice, Priority
from ..knowledge import build
from ..knowledge import mode as game_mode
from ..state import Snapshot
from .base import Rule, RuleContext

# From here on a core that dies without buyback can lose the game with it, so gold set aside for buyback
# isn't "unspent": the shopping reminders only count what's left on top of it. (A normal game's 30:00; a
# Turbo game gets there in half the time.)
BUYBACK_FROM = 30 * 60


PART_GAP_S = 60.0  # between "buy this part" calls for one item
# "You can afford X" is said this many times for one item. A player who could buy it all that time and didn't
# has decided against it: it leaves the build (knowledge/build.py) instead of being called a third time.
# (A real match: "You can afford Phase Boots", again and again, to a player who never wanted them.)
FINISH_CALLS = 2


def spendable(s: Snapshot, mode: game_mode.Mode | None = None) -> int:
    """Gold free to spend: all of it early; late in the game, what's left after keeping buyback."""
    if s.clock >= (mode or game_mode.NORMAL).at(BUYBACK_FROM):
        return max(0, s.gold - int(s.hero("buyback_cost", 0) or 0))
    return s.gold


class UnspentGold(Rule):
    id = "unspent_gold"
    cooldown_s = 90.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        if not s.alive or s.in_own_base or s.clock < 60:
            return
        free = spendable(s, game_mode.of(ctx.mem))
        if free >= ctx.cfg.unspent_gold_threshold:
            text = (f"{free:,} gold unspent on top of buyback — turn it into items" if free < s.gold
                    else f"{free:,} gold unspent — turn it into items")
            yield Advice(text, Priority.NORMAL, "economy", key="unspent_gold", speech="Spend your gold")


class AffordableBuildItem(Rule):
    """The next item in the build plan (OpenDota popularity or the coach's suggestion), bought the way players
    buy it: part by part. Gold sitting in your pocket is lost when you die and does nothing until then, so the
    moment it covers a real part of the item (Ogre Axe towards Black King Bar), Doty names that part; when it
    covers everything that's left, it says to finish the item."""

    id = "affordable_item"
    cooldown_s = 150.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        kb = ctx.kb
        if kb is None or not s.has_items_section or s.clock < 0:
            return
        mode = game_mode.of(ctx.mem)
        # Only ever the item being built now (kb.shopping: the same answer the overlay's strip, the app's Buy
        # Next panel and the coach get), and of it, what the gold covers.
        plan = kb.shopping(s.hero_name, s.clock, s.item_list(), spendable(s, mode), n=1)
        if not plan or not plan[0]["buy"]:
            return
        now = plan[0]
        item, name = now["item"], now["name"]
        if not now["finish"]:
            # Each part is named once (a player saving for something else isn't nagged about it every few
            # minutes), and two parts of one item not within a minute of each other.
            key = f"afford:{item}:{now['buy']}"
            fired = ctx.mem.get("_fired") or {}
            parts = [t for k, t in fired.items() if k.startswith(f"afford:{item}:")]
            if key in fired or (parts and time.monotonic() - max(parts) < PART_GAP_S):
                return
            part = kb.item_name(now["buy"])
            # (In Turbo every shop sells Secret Shop items.)
            secret = kb.secret_shop(now["buy"]) and not mode.turbo
            price = f"{now['buy_cost']:,}, secret shop" if secret else f"{now['buy_cost']:,}"
            yield Advice(f"Buy {part} ({price}): a part of {name}, {now['left'] - now['buy_cost']:,} "
                         "to go after it", Priority.NORMAL, "economy", key=key, speech=f"Buy {part}")
        else:
            # How often this was really said (the engine notes each call it lets through, in "_fired").
            last = (ctx.mem.get("_fired") or {}).get(f"afford:{item}")
            calls = ctx.mem.setdefault("afford_calls", {})  # item -> (times said, when last)
            said, seen = calls.get(item, (0, None))
            if last is not None and last != seen:
                said, seen = said + 1, last
                calls[item] = (said, seen)
            if said >= FINISH_CALLS:
                if (time.monotonic() - last >= self.cooldown_s and now["left"] >= now["cost"]  # (no part bought)
                        and kb.skip(item, build.COULD_AFFORD)):
                    build.passed_on(ctx.store)
                    yield build.note(kb, item, build.COULD_AFFORD)
                return
            if now["left"] < now["cost"]:
                yield Advice(f"You can finish {name}: {now['left']:,} gold covers the rest", Priority.NORMAL,
                             "economy", key=f"afford:{item}", speech=f"Finish {name}")
            else:
                yield Advice(f"You can afford {name} ({now['left']:,})", Priority.NORMAL, "economy",
                             key=f"afford:{item}", speech=f"Buy {name}")


class StashItems(Rule):
    id = "stash_items"
    cooldown_s = 60.0
    DWELL_S = 8.0

    def evaluate(self, ctx: RuleContext) -> Iterable[Advice]:
        s = ctx.snap
        # (Turbo's couriers bring the stash by themselves.)
        if (not s.has_items_section or not s.stash or not s.alive or s.in_own_base
                or game_mode.of(ctx.mem).turbo):
            ctx.mem.pop("stash_since", None)
            return
        since = ctx.mem.setdefault("stash_since", s.received_t)
        if s.received_t - since >= self.DWELL_S:
            yield Advice("Items waiting in stash — send the courier", Priority.NORMAL, "economy",
                         key="stash_items", speech="Courier your stash")


def rules() -> list[Rule]:
    return [UnspentGold(), AffordableBuildItem(), StashItems()]
