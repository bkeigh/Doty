from collections import Counter

from doty.knowledge.timings import SPAWNS, Spawn, is_day, roshan_pit
from doty.state import IN_PROGRESS, Snapshot

from .conftest import play_scripted


def test_spawn_times_after():
    power = Spawn("power", "Power rune", first=360, every=120)
    assert power.times_after(340, 15) == []
    assert power.times_after(345, 15) == [360]
    assert power.times_after(360, 15) == []  # strictly after the current clock
    water = Spawn("water", "Water", first=120, every=120, until=240)
    assert water.times_after(350, 15) == []  # past `until`
    once = Spawn("t", "T", first=1200, every=None)
    assert once.times_after(1190, 15) == [1200]


def test_bounty_interval_is_four_minutes():
    bounty = next(s for s in SPAWNS if s.key == "bounty")
    assert bounty.every == 240


def test_roshan_pit_follows_natural_cycle_after_15_minutes():
    assert "starting" in roshan_pit(600)
    assert "bottom" in roshan_pit(1000)  # 15:00-20:00 is night
    assert "top" in roshan_pit(1250)  # 20:00-25:00 is day
    assert is_day(-30) and is_day(0) and not is_day(300) and is_day(600)


def test_tormentor_side_uses_spawn_time_not_now(harness):
    cfg, store, engine, rec = harness
    play_scripted(store, engine, end_clock=1210)
    torm = next(a for a in rec.items if a.key == "tormentor@1200")
    assert "bottom-right" in torm.text  # spawns at 20:00, which is day


def test_scripted_match_triggers_every_rule(harness):
    cfg, store, engine, rec = harness
    play_scripted(store, engine)
    keys = {part for k in rec.keys for part in k.split("+")}  # (spawns at the same time share one line)
    expected = {
        "bounty@0", "water@120", "water@240", "power@360", "wisdom@420", "tormentor@1200",
        "neutral_t2@900", "low_hp", "missing_tp", "stash_items", "skill_point", "lvl6",
        "rosh_dead@900", "rosh_lo@900", "rosh_hi@900", "aegis@905", "unspent_gold",
    }
    missing = expected - keys
    assert not missing, f"rules that never fired: {missing}"


def test_each_timer_fires_exactly_once(harness):
    cfg, store, engine, rec = harness
    play_scripted(store, engine)
    timer_keys = [a.key for a in rec.items if a.category == "timer"]
    dupes = [k for k, n in Counter(timer_keys).items() if n > 1]
    assert not dupes


def test_stacks_are_opt_in(harness):
    cfg, store, engine, rec = harness
    play_scripted(store, engine, end_clock=300)
    assert not any(k.startswith("stack@") for k in rec.keys)
    cfg.rules.announce_stacks = True
    store.reset()
    rec.items.clear()
    play_scripted(store, engine, end_clock=300)
    assert "stack@113" in rec.keys


def test_timers_silent_while_paused(harness):
    cfg, store, engine, rec = harness
    snap = store.ingest({"map": {"clock_time": 350, "game_time": 440, "paused": True, "game_state": IN_PROGRESS}})
    engine.on_snapshot(snap)
    assert rec.items == []


def test_rule_eval_is_fast(harness):
    cfg, store, engine, rec = harness
    play_scripted(store, engine)
    assert engine.max_eval_ms < 25, engine.max_eval_ms


def test_muted_rule_never_fires(harness):
    cfg, store, engine, rec = harness
    cfg.rules.muted_rules = ["low_hp"]
    play_scripted(store, engine, end_clock=400)
    assert "low_hp" not in rec.keys


def test_low_hp_not_in_base():
    from doty.rules.survival import LowHp

    s = Snapshot({"hero": {"name": "npc_dota_hero_axe", "health": 100, "max_health": 1000, "alive": True,
                           "xpos": -7000, "ypos": -6500}, "player": {"team_name": "radiant"}})
    assert s.in_own_base
    assert list(LowHp().evaluate(type("C", (), {"snap": s, "cfg": None, "store": None})())) == []


def test_every_fixed_spoken_line_is_prepared_ahead():
    """Kokoro makes the rules' fixed lines ahead of time; a new `speech="..."` must be listed too."""
    import ast
    from pathlib import Path

    from doty.config import RulesConfig
    from doty.rules import FIXED_LINES, spoken_lines

    found = set()
    for path in (Path(__file__).parent.parent / "doty" / "rules").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "speech" and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                found.add(node.value.value)
    found |= {"Tormentors spawning", "Day", "Night"}  # set through local variables
    assert found <= set(FIXED_LINES), found - set(FIXED_LINES)
    lines = spoken_lines(RulesConfig())
    assert lines[0] == "Back off, low HP" and "Bounty runes in 15" in lines and "Power rune in 15" in lines
    assert len(lines) == len(set(lines))


def _ctx(snap, kb=None):
    from doty.config import RulesConfig
    from doty.rules.base import RuleContext
    from doty.state import GameStateStore

    store = GameStateStore()
    store.current = snap
    return RuleContext(snap, None, store, RulesConfig(), kb, [])


def _hero_snap(clock=600, x=0, y=0, hp=1000, gold=0, items=None, tp=True, day=True):
    inv = dict(items or {})
    if tp:
        inv["teleport0"] = {"name": "item_tpscroll", "cooldown": 0}
    return Snapshot({"map": {"clock_time": clock, "daytime": day},
                     "player": {"team_name": "radiant", "gold": gold},
                     "hero": {"name": "npc_dota_hero_spirit_breaker", "id": 71, "alive": True, "health": hp,
                              "max_health": 1000, "xpos": x, "ypos": y, "level": 8},
                     "items": inv})


def test_gank_risk_warns_once_per_trip_across_the_river():
    from doty.rules.survival import GankRisk

    rule = GankRisk()
    ctx = _ctx(_hero_snap(x=2000, y=1000, tp=False, day=False))
    out = list(rule.evaluate(ctx))
    assert len(out) == 1 and "at night" in out[0].text and "no TP ready" in out[0].text
    assert list(rule.evaluate(ctx)) == []  # same trip: once
    ctx.store.memory.pop("gank_risk_armed")
    assert list(rule.evaluate(_ctx(_hero_snap(x=2000, y=1000)))) == []  # full HP, TP ready: fine


def _kb_with_timings():
    from doty.knowledge.constants import Knowledge

    kb = Knowledge()
    kb.heroes = {"npc_dota_hero_spirit_breaker": {"id": 71, "localized_name": "Spirit Breaker"}}
    kb.items = {"item_invis_sword": {"id": 152, "dname": "Shadow Blade", "cost": 3000, "components": ["x"]}}
    kb.item_popularity[71] = {"mid_game_items": {"152": 40}, "late_game_items": {"152": 37}}
    kb.item_timings[(71, "item_invis_sword")] = [
        {"hero_id": 71, "time": t, "games": g, "wins": 0} for t, g in ((900, 20), (1200, 170), (1500, 545),
                                                                        (1800, 494))]
    kb.ability_ids = {"1": "spirit_breaker_greater_bash", "2": "spirit_breaker_charge_of_darkness"}
    kb.ability_info = {"spirit_breaker_greater_bash": {"dname": "Greater Bash"},
                       "spirit_breaker_charge_of_darkness": {"dname": "Charge of Darkness"}}
    kb.skill_rows[71] = [{"lvl": 1, "ab": 1, "n": 9}, {"lvl": 2, "ab": 2, "n": 9}]
    return kb


def test_item_pace_nudges_before_the_usual_time():
    from doty.rules.progression import ItemPace

    kb = _kb_with_timings()
    assert kb.typical_time(71, "item_invis_sword") == 1500
    out = list(ItemPace().evaluate(_ctx(_hero_snap(clock=1420, gold=1200, items={"slot0": {"name": "item_boots"}}),
                                        kb)))
    assert len(out) == 1 and out[0].text.startswith("Shadow Blade usually lands by 25:00") and "1,800" in out[0].text
    assert list(ItemPace().evaluate(_ctx(_hero_snap(clock=1200, gold=0), kb))) == []  # too early to nag


def test_game_start_card_shows_skills_and_build():
    from doty.rules.progression import GameStartCard

    out = list(GameStartCard().evaluate(_ctx(_hero_snap(clock=-30), _kb_with_timings())))
    assert out[0].text == "Skills: Greater Bash › Charge of Darkness · Build: Shadow Blade (~25:00)"
    assert not out[0].speak


def test_laning_tips_nudge_denies_and_early_last_hits_once():
    from doty.rules.progression import LaningTips

    kb = _kb_with_timings()
    kb.benchmarks = {71: {"result": {"last_hits_per_min": [{"percentile": 0.5, "value": 5.2}]}}}

    def snap(clock, lh, dn):
        s = _hero_snap(clock=clock)
        s.raw["player"].update({"last_hits": lh, "denies": dn})
        return s

    rule = LaningTips()
    ctx = _ctx(snap(152, 9, 0), kb)
    out = list(rule.evaluate(ctx))
    assert [a.key for a in out] == ["deny"] and out[0].speech == "Deny your creeps too"
    assert out[0].text.startswith("9 last hits, no denies yet")
    ctx.snap = snap(331, 9, 0)
    assert list(rule.evaluate(ctx)) == []  # said once a match is enough
    ctx.snap = snap(301, 7, 1)
    out = list(rule.evaluate(ctx))  # target 5.2 * 0.75 * 5 = 19.5; 7 is under 60% of it
    assert [a.key for a in out] == ["lane_pace"] and "aim for about 20" in out[0].text
    ctx.snap = snap(305, 7, 1)
    assert list(rule.evaluate(ctx)) == []
    fresh = _ctx(snap(152, 2, 0), kb)
    assert list(rule.evaluate(fresh)) == []  # 2 last hits: not farming a lane, no deny nudge
    fresh.snap = snap(301, 15, 2)
    assert list(rule.evaluate(fresh)) == []  # on pace: quiet


def test_item_pace_moves_on_past_an_item_you_skipped():
    """Skipping (or selling) an early core item mustn't silence the reminder for the next one."""
    from doty.rules.progression import GameStartCard, ItemPace

    kb = _kb_with_timings()
    kb.items["item_wraith_band"] = {"id": 30, "dname": "Wraith Band", "cost": 1000, "components": ["x"]}
    kb.item_popularity[71]["early_game_items"] = {"30": 90}  # the most bought, usually done by 7:30
    kb.item_timings[(71, "item_wraith_band")] = [{"hero_id": 71, "time": 450, "games": 100, "wins": 0}]
    out = list(ItemPace().evaluate(_ctx(_hero_snap(clock=1420, gold=1200), kb)))
    assert len(out) == 1 and out[0].text.startswith("Shadow Blade usually lands by 25:00")
    card = list(GameStartCard().evaluate(_ctx(_hero_snap(clock=-30), kb)))[0]
    assert "Build: Wraith Band (~7:30) › Shadow Blade (~25:00)" in card.text  # in the order they land


def test_odd_ability_keys_in_the_game_data_dont_break_anything():
    s = _hero_snap()
    s.raw["abilities"] = {"ability1": {"name": "b", "level": 1}, "attributes": {"level": 0},
                          "ability0": {"name": "a", "level": 2}}
    assert [a.name for a in s.abilities] == ["a", "b"]


# the other team -----------------------------------------------------------------------------------------------
def _enemy_ctx(clock, kb):
    from doty.lineup import Lineup

    ctx = _ctx(_hero_snap(clock=clock), kb)
    votes = ctx.store.memory["lineup_votes"] = Lineup()
    for _ in range(2):
        votes.add(["npc_dota_hero_spirit_breaker"], ["npc_dota_hero_juggernaut", "npc_dota_hero_lion"],
                  "npc_dota_hero_spirit_breaker", "radiant")
    return ctx


def _spike_kb():
    kb = _kb_with_timings()
    kb.heroes["npc_dota_hero_juggernaut"] = {"id": 8, "localized_name": "Juggernaut"}
    kb.heroes["npc_dota_hero_lion"] = {"id": 26, "localized_name": "Lion"}
    kb.items["item_bfury"] = {"id": 145, "dname": "Battle Fury", "cost": 4100, "components": ["x"]}
    kb.items["item_blink"] = {"id": 1, "dname": "Blink Dagger", "cost": 2250, "components": []}
    kb.item_popularity[8] = {"early_game_items": {"145": 90}}
    kb.item_popularity[26] = {"early_game_items": {"1": 80}}
    kb.item_timings[(8, "item_bfury")] = [{"hero_id": 8, "time": 900, "games": 100}]
    kb.item_timings[(26, "item_blink")] = [{"hero_id": 26, "time": 720, "games": 100}]
    return kb


def test_enemy_spikes_card_then_a_heads_up_before_each():
    from doty.rules.matchup import EnemySpikes

    kb = _spike_kb()
    rule = EnemySpikes()
    ctx = _enemy_ctx(90, kb)
    out = list(rule.evaluate(ctx))
    assert [a.text for a in out] == ["Their usual spikes: Lion Blink Dagger ~12:00 · Juggernaut Battle Fury ~15:00"]
    assert not out[0].speak
    mem = ctx.store.memory
    later = _enemy_ctx(700, kb)
    later.store.memory.update({k: v for k, v in mem.items() if k != "lineup_votes"})
    call = list(rule.evaluate(later))
    assert [a.spoken for a in call] == ["Lion gets Blink Dagger soon"]
    assert list(rule.evaluate(later)) == []  # once
    soon = _enemy_ctx(870, kb)  # Battle Fury's heads-up, but only 170 s after the last call: allowed (gap 150)
    soon.store.memory.update({k: v for k, v in later.store.memory.items() if k != "lineup_votes"})
    assert [a.spoken for a in rule.evaluate(soon)] == ["Juggernaut gets Battle Fury soon"]


def test_pick_off_calls_once_per_moment_from_a_fresh_screen_reading():
    import time as _t

    from doty.rules.matchup import PickOff

    rule = PickOff()
    ctx = _ctx(_hero_snap(clock=900))
    mem = ctx.store.memory
    mem["screen"] = {"t": _t.monotonic(), "match": "", "dead": {"radiant": [], "dire": ["a", "b"]}}
    out = list(rule.evaluate(ctx))
    assert [a.spoken for a in out] == ["Two enemies dead. Push a tower."]
    mem["screen"]["dead"]["dire"].append("c")
    assert list(rule.evaluate(ctx)) == []  # the same moment, one more down: not again
    mem["screen"]["dead"]["dire"] = ["a"]
    assert list(rule.evaluate(ctx)) == []
    mem["screen"]["dead"]["dire"] = ["a", "b"]
    assert list(rule.evaluate(ctx)) == []  # a flicker in the reading: still the same moment
    mem["screen"]["dead"]["dire"] = []
    assert list(rule.evaluate(ctx)) == []
    mem["pickoff_open_below_t"] -= PickOff.CLOSE_S  # they've been back for a while...
    assert list(rule.evaluate(ctx)) == []
    mem["screen"]["dead"]["dire"] = ["a", "b", "c"]  # ...and the next fight's pick-off is called, seconds later
    assert [a.spoken for a in rule.evaluate(ctx)] == ["Three enemies dead. Take an objective."]
    mem["screen"]["t"] -= 10  # an old reading says nothing
    mem.pop("pickoff_open")
    assert list(rule.evaluate(ctx)) == []


def test_your_team_down_while_deep_says_back_off():
    import time as _t

    from doty.rules.matchup import PickOff

    deep = _ctx(_hero_snap(clock=900, x=1500, y=500))
    deep.store.memory["screen"] = {"t": _t.monotonic(), "match": "",
                                   "dead": {"radiant": ["a", "b"], "dire": []}}
    assert [a.spoken for a in PickOff().evaluate(deep)] == ["Your team is down. Back off."]
    home = _ctx(_hero_snap(clock=900, x=-3000, y=-3000))
    home.store.memory["screen"] = deep.store.memory["screen"]
    assert list(PickOff().evaluate(home)) == []


def test_spoken_alerts_are_dropped_from_the_voice_when_late(harness):
    from doty.advice import Priority
    from doty.rules.engine import STALE_S

    from .conftest import play_scripted

    cfg, store, engine, rec = harness
    play_scripted(store, engine, end_clock=4 * 60)
    assert rec.items
    for adv in rec.items:
        assert adv.stale_s == (None if adv.priority == Priority.CRITICAL else STALE_S)
