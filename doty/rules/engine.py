"""Runs every rule on every snapshot, dedupes, and publishes advice."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ..advice import Advice, AdviceBus, Priority
from ..config import RulesConfig
from ..state import POST_GAME, GameStateStore, Snapshot
from .base import Rule, RuleContext

if TYPE_CHECKING:
    from ..knowledge.constants import Knowledge

# What each player level hears (CoachConfig.level). New and learning players also hear the teaching lines that
# are shown-only for everyone else (level 6, laning and farm checks, item pace); experienced and top players hear
# fewer shopping and skill reminders (still shown).
SPEAK_MORE = {
    "new": frozenset({"power_spike", "farm_check", "laning_tips", "item_pace", "basics"}),
    "learning": frozenset({"power_spike", "farm_check", "laning_tips", "item_pace"}),
}
SPEAK_LESS = {
    "experienced": frozenset({"missing_tp", "stash_items", "affordable_item", "unspent_gold", "skill_point"}),
    "pro": frozenset({"missing_tp", "stash_items", "affordable_item", "unspent_gold", "skill_point", "item_pace",
                      "gank_risk", "buyback", "death_watch"}),
}

# Which calls belong to which subject of Settings > Alerts > "What Doty suggests" (config.SuggestConfig). A rule
# that isn't listed (low HP, the save calls, gank risk) is about staying alive and has no switch here.
TOPIC_RULES = {
    "items": frozenset({"affordable_item", "item_pace"}),
    "skills": frozenset({"skill_point", "power_spike"}),
    "farm": frozenset({"farm_check", "laning"}),
    "timers": frozenset({"spawn_timer", "roshan", "day_night"}),
    "enemy": frozenset({"enemy_spikes", "game_plan", "pick_off"}),
    "gold": frozenset({"unspent_gold", "stash_items", "buyback", "missing_tp"}),
    "deaths": frozenset({"death_watch"}),
}


def topics_off(suggest) -> frozenset[str]:
    """The subjects turned off (a config.SuggestConfig, or None for all on)."""
    if suggest is None:
        return frozenset()
    return frozenset(t for t in TOPIC_RULES if not getattr(suggest, t, True))


# An alert that can't be heard within this many seconds of its moment (the voice is busy with a long
# line) is dropped from the voice: "Bounty runes in 15" said ten seconds late is wrong. It stays on screen.
STALE_S = 8.0


def default_rules() -> list[Rule]:
    from . import economy, matchup, progression, survival, timers

    return [
        *timers.rules(),
        *survival.rules(),
        *economy.rules(),
        *progression.rules(),
        *matchup.rules(),
    ]


class RuleEngine:
    def __init__(self, store: GameStateStore, bus: AdviceBus, cfg: RulesConfig,
                 kb: "Knowledge | None" = None, rules: list[Rule] | None = None) -> None:
        self.store = store
        self.bus = bus
        self.cfg = cfg
        self.kb = kb
        self.rules = rules if rules is not None else default_rules()
        self.last_eval_ms = 0.0
        self.max_eval_ms = 0.0
        self.level_of = lambda: "some"  # the player's level, read live (the app points it at its config)
        self.suggest = None  # config.SuggestConfig: which subjects Doty speaks about (the app sets it)
        # config.CoachConfig: "When I go my own way" and the player's standing build preferences (rules/own_way.py;
        # the app sets it, read live). None = the defaults.
        self.prefs = None

    @property
    def _fired(self) -> dict[str, float]:
        # Lives in match memory so a new match clears it.
        return self.store.memory.setdefault("_fired", {})

    def on_snapshot(self, snap: Snapshot) -> list[Advice]:
        if not self.cfg.enabled or snap.custom_game:  # (arcade games have their own rules and timings)
            return []
        if snap.in_draft or snap.game_state == POST_GAME:
            return []  # (not on the victory screen: "unspent gold" after the Ancient falls helps no one)
        if snap.paused:
            return []  # (nothing can be bought or levelled in a pause; Dota still sends an update every 10 s)
        start = time.perf_counter()
        from . import own_way

        own_way.honour(self.kb, self.prefs, snap, self.store)  # (items never to suggest, the player's hero build)
        ctx = RuleContext(
            snap=snap,
            prev=self.store.previous,
            store=self.store,
            cfg=self.cfg,
            kb=self.kb,
            new_events=self.store.new_events(snap),
            level=self.level_of(),
            topics_off=topics_off(self.suggest),
            prefs=self.prefs,
        )
        silenced = frozenset().union(*(TOPIC_RULES[t] for t in ctx.topics_off)) if ctx.topics_off else frozenset()
        more, less = SPEAK_MORE.get(ctx.level, frozenset()), SPEAK_LESS.get(ctx.level, frozenset())
        emitted: list[Advice] = []
        now = time.monotonic()
        fired = self._fired
        # Mid-fight, only what's about the fight is said out loud: "Bounty runes in 15" or "Buy a TP scroll"
        # while you're being hit is noise. Those still show on screen.
        fighting = snap.alive and self.store.in_fight()
        for rule in self.rules:
            if rule.id in self.cfg.muted_rules or rule.id in silenced:
                continue
            if rule.needs_hero and not snap.has_hero:
                continue
            try:
                produced = list(rule.evaluate(ctx))
            except Exception as exc:  # a broken rule must never take down the loop
                print(f"[doty] rule {rule.id} failed: {exc!r}")
                continue
            for adv in produced:
                key = adv.key or f"{rule.id}:{adv.text}"
                last = fired.get(key)
                if last is not None and (rule.cooldown_s == 0 or now - last < rule.cooldown_s):
                    continue
                fired[key] = now
                adv.key = key
                adv.trigger_t = snap.received_t
                adv.created_t = time.perf_counter()
                if adv.stale_s is None and adv.priority > Priority.CRITICAL:
                    adv.stale_s = STALE_S  # (critical lines cut in, so they're never late)
                if rule.id in more and adv.priority > Priority.NORMAL:
                    adv.priority = Priority.NORMAL  # (said out loud for this player)
                if rule.id in less and adv.priority >= Priority.NORMAL:
                    adv.speak = False
                if fighting and adv.priority >= Priority.NORMAL:
                    adv.speak = False
                emitted.append(adv)
        self.last_eval_ms = (time.perf_counter() - start) * 1000.0
        self.max_eval_ms = max(self.max_eval_ms, self.last_eval_ms)
        from .. import i18n

        if i18n.language() != "en":  # the player's language: the spoken line, and the card where it's known
            for adv in emitted:
                spoken = i18n.tr(adv.speech) if adv.speech else None
                text = i18n.tr(adv.text)
                if text == adv.text and spoken and spoken != adv.speech:
                    text = spoken  # (a card built with numbers isn't in the catalog: its spoken line is)
                adv.text, adv.speech = text, spoken if adv.speech else adv.speech
        for adv in sorted(emitted, key=lambda a: a.priority):
            self.bus.publish(adv)
        return emitted
