"""Fixes from this round's two reviews, each the way it would have shown up in a real match."""

import asyncio
import time

from doty.advice import Advice, AdviceBus, Priority
from doty.config import RulesConfig
from doty.eleven import PcmPlayer
from doty.knowledge.constants import Knowledge
from doty.review import MatchRecorder
from doty.rules import RuleEngine
from doty.rules.economy import AffordableBuildItem
from doty.rules.base import RuleContext
from doty.rules.matchup import PickOff
from doty.state import GameStateStore

from .conftest import Recorder
from .test_coach_stream import _wait_inflight, coach_env  # noqa: F401  (the fixture)
from .test_review import _kb as _review_kb
from .test_review import feed, payload


def _kb():
    kb = Knowledge()
    kb.heroes = {"npc_dota_hero_juggernaut": {"id": 8, "localized_name": "Juggernaut"}}
    kb.items = {
        "item_yasha": {"id": 170, "dname": "Yasha", "cost": 2050, "components": ["blade_of_alacrity", "band"]},
        "item_manta": {"id": 147, "dname": "Manta Style", "cost": 4650, "components": ["yasha", "ultimate_orb"]},
        "item_bfury": {"id": 145, "dname": "Battle Fury", "cost": 4100, "components": ["quelling_blade", "demon_edge"]},
        "item_boots": {"id": 29, "dname": "Boots of Speed", "cost": 500},
        "item_power_treads": {"id": 63, "dname": "Power Treads", "cost": 1400, "components": ["boots", "gloves"]},
        "item_phase_boots": {"id": 50, "dname": "Phase Boots", "cost": 1500, "components": ["boots", "blades"]},
        "item_travel_boots": {"id": 48, "dname": "Boots of Travel", "cost": 2500, "components": ["boots"]},
    }
    kb.item_popularity[8] = {"mid_game_items": {"170": 90, "50": 80, "147": 70}}
    return kb


def _snap(store, clock, items, gold=2500, paused=False):
    inv = {f"slot{i}": {"name": n} for i, n in enumerate(items)}
    return store.ingest({"map": {"matchid": "1", "clock_time": clock, "paused": paused,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": gold},
                         "hero": {"name": "npc_dota_hero_juggernaut", "id": 8, "alive": True, "health": 1000,
                                  "max_health": 1000, "xpos": -6000, "ypos": -6000, "level": 14},
                         "items": inv}, received_t=float(clock))


def test_no_buy_reminder_for_a_part_of_what_you_built_or_a_second_pair_of_boots():
    kb, store = _kb(), GameStateStore()
    assert "item_yasha" in kb.owned_with_parts({"item_manta"})
    assert kb.second_boots("item_phase_boots", {"item_power_treads"})
    assert not kb.second_boots("item_phase_boots", {"item_boots"})  # upgrading plain boots is the point
    assert not kb.second_boots("item_travel_boots", {"item_power_treads"})
    snap = _snap(store, 1200, ["item_manta", "item_power_treads"], gold=2500)
    ctx = RuleContext(snap, None, store, RulesConfig(), kb, [])
    assert [a.spoken for a in AffordableBuildItem().evaluate(ctx)] == []  # not Yasha, not Phase Boots


def test_nothing_is_said_while_the_game_is_paused():
    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, RulesConfig())
    engine.on_snapshot(_snap(store, 1200, [], gold=5000, paused=True))
    assert rec.items == []  # (no "Spend your 5000 gold" in a pause)


def test_a_failed_download_is_tried_again_and_an_empty_one_isnt_kept(tmp_path):
    kb = Knowledge()
    kb.cache_dir = tmp_path
    asked = []

    async def flaky(name, url, ttl=0):
        asked.append(url)
        return None if "benchmarks" in url else ([] if "matchups" in url else {})

    kb._cached_json = flaky
    asyncio.run(kb.ensure_hero(8))
    assert not kb.has_hero_data(8)  # benchmarks didn't come: not done for the session
    kb._attempted[8] = -1e9  # (the back-off has passed)
    asyncio.run(kb.ensure_hero(8))
    assert sum("benchmarks" in u for u in asked) == 2


def test_a_cache_file_that_cant_be_written_still_gives_the_data(tmp_path, monkeypatch):
    import pathlib

    kb = Knowledge()
    kb.cache_dir = tmp_path

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def json(self, content_type=None):
            return {"blink": {"id": 1}}

    class Session:
        def get(self, url):
            return Resp()

    kb._session = Session()

    def locked(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(pathlib.Path, "replace", locked)
    assert asyncio.run(kb._cached_json("items.json", "https://x/items.json")) == {"blink": {"id": 1}}


def test_another_game_with_the_same_id_isnt_merged_into_the_review(tmp_path):
    kb = _review_kb()
    store, rec = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)
    for clock in range(0, 1500, 30):  # game A (id "0"), left at 25:00 without a result
        feed(rec, store, payload(clock, match="0", items=["item_blink"] if clock >= 900 else []))
    rec.finish()  # (Doty stopping saves what it has)
    store2, rec2 = GameStateStore(), MatchRecorder(kb, out_dir=tmp_path)  # Doty restarted; game B, same id
    for clock in range(0, 300, 30):
        feed(rec2, store2, payload(clock, match="0"))
    assert rec2._duration < 600 and not rec2._items  # B starts clean: A's 25 minutes and Blink aren't B's


def test_the_same_pick_off_isnt_called_twice_when_the_reading_drops_out():
    store = GameStateStore()
    snap = _snap(store, 1500, [])
    mem = store.memory
    rule = PickOff()

    def reading(dead, fresh=True):
        mem["screen"] = {"t": time.monotonic() if fresh else -1e9, "match": snap.match_id,
                         "dead": {"dire": dead, "radiant": []}, "since": {h: 1490 for h in dead}}
        return [a.spoken for a in rule.evaluate(RuleContext(snap, None, store, RulesConfig(), None, []))]

    assert reading(["lion", "lina"]) == ["Two enemies dead. Push a tower."]
    mem.update({"pickoff_open_below_t": -1e9})
    reading([], fresh=False)  # you died: no reading for a while, the moment closes
    assert reading(["lion", "lina"]) == []  # the same two deaths: not called again
    mem["screen"]["since"] = {}
    mem.update({"pickoff_open_below_t": -1e9})
    reading([], fresh=False)
    assert reading(["lion", "zeus"]) == ["Two enemies dead. Push a tower."]  # a new death


def test_a_dead_audio_device_doesnt_make_every_line_too_late():
    player = PcmPlayer()
    player._buf += bytes(24000 * 2 * 15)  # 15 s buffered...
    player._stream = type("S", (), {"active": False, "stop": lambda s: None, "close": lambda s: None})()
    assert player.pending_s == 0.0 and not player._buf  # ...that will never play


async def test_the_death_answer_counts_as_said_so_the_next_look_doesnt_repeat_it(coach_env):
    coach, fake, rec, kb = coach_env
    fake.reply = "Buy Black King Bar: Lion chain-stunned you."
    coach._launch("death", visible=True)
    await _wait_inflight(coach)
    assert "Buy Black King Bar: Lion chain-stunned you." in coach.said
    assert coach._repeats("Buy Black King Bar now, Lion stunned you.")


async def test_a_connection_dropped_mid_reply_ends_the_card_instead_of_hanging(coach_env):
    coach, fake, rec, kb = coach_env

    async def broken(*a, **k):
        yield "Farm the"
        raise ConnectionResetError("dropped")

    coach.backend.stream = broken
    coach._launch("now", visible=True)
    await _wait_inflight(coach)
    card = [a for a in rec.items if a.display][-1]
    assert card.final and "try again" in card.text


async def test_a_shown_only_alert_doesnt_count_as_heard(coach_env):
    coach, fake, rec, kb = coach_env
    coach.note_alert(Advice("Level 6 — ultimate online, look for a play", Priority.INFO, "progression"))
    assert not coach._repeats("Level 6: look for a kill on their mid with your ultimate.")


async def test_with_chatty_coaching_a_repeat_still_isnt_said(coach_env):
    coach, fake, rec, kb = coach_env
    coach.cfg.speak_up = "every"
    adv = coach._publish_watch("Buy a TP scroll.", True, time.perf_counter(), repeat=True)
    assert not adv.speak


async def test_prepared_calls_keep_firing_if_the_coach_turns_off(coach_env):
    from doty import triggers as tl

    coach, fake, rec, kb = coach_env
    coach.on_snapshot(coach.store.current)
    coach.triggers.arm(tl.parse("TRIGGER: hp < 30 => Walk back now.", clock=coach.store.current.clock))
    coach._disable("signed out")
    raw = coach.store.current.raw
    hero = dict(raw["hero"], alive=True)
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": hero["max_health"]}}))
    coach.on_snapshot(coach.store.ingest({**raw, "hero": {**hero, "health": int(hero["max_health"] * 0.2)}}))
    assert [a.text for a in rec.items if a.category == "trigger"] == ["Walk back now."]


async def test_a_calm_moment_waits_for_its_look(coach_env):
    coach, fake, rec, kb = coach_env
    raw = coach.store.current.raw
    hero = dict(raw["hero"], alive=True, health=raw["hero"]["max_health"])  # (alive, out of any fight)
    alive = coach.store.ingest({**raw, "hero": hero})
    coach.on_snapshot(alive)
    await _wait_inflight(coach) if coach._inflight else None
    coach._last_spec_wall = time.monotonic()  # a look just ran: the next can't start for a few seconds
    coach._moment = ("the player just reached level 6", time.monotonic() + 10)
    coach.on_snapshot(coach.store.ingest({**raw, "hero": hero}))
    assert coach._moment is not None  # kept, not lost
    coach._last_spec_wall = 0.0
    coach.on_snapshot(coach.store.ingest({**raw, "hero": hero}))
    assert coach._moment is None and coach._inflight is not None  # the look went, with its moment
    await _wait_inflight(coach)
