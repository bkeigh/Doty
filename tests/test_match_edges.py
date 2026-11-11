"""Real-match edge cases: back-to-back bot games with the same match id, reconnects, the victory screen,
arcade games and Ability Draft."""

from doty.advice import AdviceBus
from doty.config import RulesConfig
from doty.rules import RuleEngine
from doty.sim import ScriptedMatch
from doty.state import HERO_SELECTION, POST_GAME, GameStateStore, Snapshot

from .conftest import TOKEN, Recorder
from .test_live_speed import _wait_inflight, live  # noqa: F401  (the fixture)


def _game(m: ScriptedMatch, match_id="0", **map_over):
    p = m.payload(TOKEN)
    p["map"]["matchid"] = match_id
    p["map"].update(map_over)
    return p


def test_a_new_game_with_the_same_match_id_starts_fresh():
    store = GameStateStore()
    m = ScriptedMatch()
    m.step(700)
    store.ingest(_game(m))
    store.memory["_fired"] = {"lvl6": 1.0}
    gen = store.generation
    store.ingest(_game(m, game_state=POST_GAME))
    store.ingest({"provider": {}})  # menus between games: no map at all
    assert store.memory  # (nothing reset yet)
    store.ingest({"map": {"matchid": "0", "game_state": HERO_SELECTION, "clock_time": 0}})
    assert store.generation == gen + 1 and not store.memory  # a new draft after a game: a new match
    store.ingest({"map": {"matchid": "0", "game_state": HERO_SELECTION, "clock_time": 0}})
    assert store.generation == gen + 1  # (once)


def test_the_clock_jumping_far_back_is_a_new_game():
    store = GameStateStore()
    m = ScriptedMatch()
    m.step(900)
    store.ingest(_game(m))
    gen = store.generation
    fresh = ScriptedMatch()
    fresh.step(95)  # a restarted demo or bot game: 0:05 again
    store.ingest(_game(fresh))
    assert store.generation == gen + 1
    fresh.step(1)
    store.ingest(_game(fresh))
    assert store.generation == gen + 1


async def test_the_coach_starts_over_too(live):
    coach, fake, rec, kb = live
    snap = coach.store.current
    coach.on_snapshot(snap)
    coach.said.append("Farm the bottom triangle.")
    kb.custom_plan[snap.hero_name] = ["item_bfury"]
    await _wait_inflight(coach)
    coach.store.ingest({**snap.raw, "map": {**snap.raw["map"], "game_state": POST_GAME}})
    draft = coach.store.ingest({"map": {"matchid": snap.match_id, "game_state": HERO_SELECTION,
                                        "clock_time": 0}})
    coach.on_snapshot(draft)
    assert not coach.said and not kb.custom_plan  # last game's talk and build stay with last game


async def test_back_from_a_disconnect_is_not_a_calm_moment(live):
    coach, fake, rec, kb = live
    snap = coach.store.current
    coach.on_snapshot(snap)
    await _wait_inflight(coach)
    coach.store.ingest({"provider": {}, "map": {"matchid": snap.match_id}})  # a menu update: no hero
    calls = []
    coach._calm_moment = lambda s, prev, f: calls.append(prev) or ""
    coach.on_snapshot(coach.store.ingest(snap.raw))
    assert calls == [None]  # not "just reached level 6" from a level-0 menu update


def _engine(store):
    bus, rec = AdviceBus(), Recorder()
    bus.subscribe(rec)
    return RuleEngine(store, bus, RulesConfig(), kb=None), rec


def test_no_rule_talks_on_the_victory_screen():
    store = GameStateStore()
    engine, rec = _engine(store)
    m = ScriptedMatch()
    m.step(1500)
    p = _game(m, game_state=POST_GAME)
    p["player"]["gold"] = 9000  # would be "unspent gold" in a match
    p["items"] = {}  # and "no TP scroll"
    assert engine.on_snapshot(store.ingest(p)) == []


def test_arcade_games_get_no_dota_rules_or_coaching():
    store = GameStateStore()
    engine, rec = _engine(store)
    m = ScriptedMatch()
    m.step(400)
    snap = store.ingest(_game(m, customgamename="overthrow_3"))
    assert snap.custom_game and engine.on_snapshot(snap) == []
    assert not Snapshot({"map": {"customgamename": ""}}).custom_game


def test_ability_draft_doesnt_nag_about_skill_points():
    from .test_hero_knowledge import SB, _kb

    kb = _kb()
    abilities = {"ability0": {"name": "lion_impale", "level": 2}, "ability1": {"name": "zuus_arc_lightning", "level": 2},
                 "ability2": {"name": "axe_counter_helix", "level": 1}, "ability3": {"name": "lina_laguna_blade", "level": 0}}
    snap = Snapshot({"hero": {"name": SB, "id": 71, "level": 5}, "abilities": abilities})
    assert kb.unspent_points(snap) == 0  # drafted abilities, none of Spirit Breaker's own: count them all


def test_aegis_does_not_start_a_death_look():
    store = GameStateStore()
    m = ScriptedMatch()
    m.step(900)
    p = _game(m)
    store.ingest(p)
    p2 = _game(m)
    p2["hero"]["alive"] = False  # reincarnating: "alive" flips, the death count doesn't
    store.ingest(p2)
    assert not store.just_died


# from the second review ------------------------------------------------------------------------------------
async def test_a_disconnect_keeps_the_coach_memory_of_the_match(live):
    coach, fake, rec, kb = live
    snap = coach.store.current
    coach.on_snapshot(snap)
    await _wait_inflight(coach)
    coach.said.append("Farm the bottom triangle.")
    kb.custom_plan[snap.hero_name] = ["item_bfury"]
    coach.on_snapshot(coach.store.ingest({"provider": {"name": "Dota 2"}}))  # a menu update: no map at all
    coach.on_snapshot(coach.store.ingest(snap.raw))  # back in the game
    assert coach.said and kb.custom_plan  # the same match: nothing forgotten


def _revive_store():
    """A hero mid-fight, then an Aegis revive: "alive" goes false without the death count moving."""
    store = GameStateStore()
    m = ScriptedMatch()
    m.step(900)
    store.ingest(_game(m))
    p = _game(m)
    p["hero"].update(alive=False, respawn_seconds=3)
    return store, p


def test_an_aegis_revive_is_not_dead_for_calls():
    from doty import triggers as tl

    store, p = _revive_store()
    book = tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: buyback => Buy back now.\nTRIGGER: dead and respawn_s <= 3 => Buy a TP.", 900))
    book.check(store.current, store)
    snap = store.ingest(p)
    assert book.check(snap, store) == []
    facts = tl.facts_for(snap, store)
    assert facts["dead"] is False and "respawn_s" not in facts and facts["buyback"] is False


def test_respawn_calls_wait_while_you_are_alive():
    from doty import triggers as tl

    store, book = GameStateStore(), tl.TriggerBook()
    m = ScriptedMatch()
    m.step(900)
    p = _game(m)
    p["player"]["gold"] = 3000
    book.arm(tl.parse("TRIGGER: respawn_s <= 5 and gold >= 1400 => Buy Blink before you respawn.", 900))
    assert book.check(store.ingest(p), store) == []  # alive: no respawn timer to speak of
    assert "respawn_s" not in tl.facts_for(store.current, store)


async def test_an_aegis_revive_starts_no_respawn_look_and_no_respawned_moment(live):
    coach, fake, rec, kb = live
    store = coach.store
    snap = store.current
    raw = dict(snap.raw)
    dead = {**raw, "hero": {**raw["hero"], "alive": False, "respawn_seconds": 3}}
    coach.on_snapshot(store.ingest(dead))  # the revive (no death counted)
    assert not store.just_died and not coach._pre_respawn_done
    assert coach._inflight is None or coach._inflight[0] != "death"
    store.ingest(raw)
    assert not store.just_respawned


def test_the_review_ignores_aegis_and_keeps_same_id_games_apart(tmp_path):
    from doty.review import MatchRecorder

    from .test_review import feed, payload

    store, rec = GameStateStore(), MatchRecorder(None, out_dir=tmp_path)
    feed(rec, store, payload(600, match="0"))
    feed(rec, store, payload(601, match="0", alive=False))  # Aegis: alive flips, deaths don't
    feed(rec, store, payload(606, match="0"))
    assert rec._deaths == []
    feed(rec, store, payload(900, match="0", state=POST_GAME, win="radiant"))
    first = rec.last_review
    assert first is not None
    feed(rec, store, {"map": {"matchid": "0", "game_state": HERO_SELECTION, "clock_time": 0}})
    feed(rec, store, payload(300, match="0"))
    feed(rec, store, payload(310, match="0", alive=False, deaths=1))
    feed(rec, store, payload(700, match="0", state=POST_GAME, win="dire", deaths=1))
    assert rec.last_review is not first and [d.clock for d in rec.last_review.deaths] == [310]


def test_the_next_pick_off_is_called_after_you_respawn():
    import time as _t

    from doty.rules.matchup import PickOff

    from .test_rules import _ctx, _hero_snap

    rule = PickOff()
    ctx = _ctx(_hero_snap(clock=900))
    mem = ctx.store.memory
    mem["screen"] = {"t": _t.monotonic(), "match": "", "dead": {"radiant": [], "dire": ["a", "b"]}}
    assert len(list(rule.evaluate(ctx))) == 1
    mem["screen"]["t"] -= 10  # you died: no fresh reading for a while
    list(rule.evaluate(ctx))
    mem["pickoff_open_below_t"] -= PickOff.CLOSE_S
    list(rule.evaluate(ctx))
    mem["screen"] = {"t": _t.monotonic(), "match": "", "dead": {"radiant": [], "dire": ["c", "d"]}}
    assert [a.spoken for a in rule.evaluate(ctx)] == ["Two enemies dead. Push a tower."]


def test_toggles_and_farming_items_are_not_unused_saves():
    from doty.state import death_summary

    store = GameStateStore()
    m = ScriptedMatch()
    m.step(900)
    p = _game(m)
    p["items"] = {"slot0": {"name": "item_armlet", "cooldown": 0, "can_cast": True},
                  "slot1": {"name": "item_hand_of_midas", "cooldown": 0, "can_cast": True},
                  "slot2": {"name": "item_magic_wand", "cooldown": 0, "can_cast": True, "charges": 12},
                  "slot3": {"name": "item_glimmer_cape", "cooldown": 0, "can_cast": True}}
    snap = store.ingest(p)
    out = death_summary(list(store.history), snap, snap.received_t)
    assert out["ready_but_unused"] == ["glimmer_cape", "magic_wand"]


def test_demo_hero_mode_is_not_an_arcade_game():
    assert not Snapshot({"map": {"customgamename": "hero_demo"}}).custom_game
    assert Snapshot({"map": {"customgamename": "overthrow"}}).custom_game
