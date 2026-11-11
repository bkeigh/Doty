"""Live skills: prepared calls about items to use and what's on your hero right now, and the look that
starts before you respawn so its tip is ready when you're back."""


from doty import triggers as tl
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN
from .test_live_speed import _wait_inflight, live  # noqa: F401  (the fixture)


def _snap(store, t, hp=1000, gold=1000, alive=True, items=None, hero=None, respawn=0):
    inv = items if items is not None else {}
    raw_hero = {"name": "npc_dota_hero_axe", "id": 2, "alive": alive, "health": hp, "max_health": 1000,
                "xpos": -3000, "ypos": -3000, "level": 12, "respawn_seconds": respawn, **(hero or {})}
    return store.ingest({"map": {"matchid": "1", "clock_time": 900,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": {"team_name": "radiant", "gold": gold},
                         "hero": raw_hero, "items": inv}, received_t=t)


def test_the_new_words_parse_and_unknown_ones_still_drop():
    armed = tl.parse("TRIGGER: hp_lost >= 30 and ready item_glimmer_cape => Glimmer yourself now.\n"
                     "TRIGGER: silenced and ready manta => Manta now to purge the silence.\n"
                     "TRIGGER: dead and buyback => Buy back now and defend.\n"
                     "TRIGGER: smoked and magic_immune and broken and hexed => fine\n"
                     "TRIGGER: ready => not an item, dropped\n"
                     "TRIGGER: confused => not a word Doty knows, dropped\n", clock=900)
    assert [t.say for t in armed] == ["Glimmer yourself now.", "Manta now to purge the silence.",
                                      "Buy back now and defend.", "fine"]
    assert armed[1].clauses[1] == tl.Clause("ready", "item_manta")
    assert armed[2].while_dead  # a call about being dead may fire on the death screen


def test_ready_means_in_the_inventory_off_cooldown_and_usable():
    store = GameStateStore()
    items = {"slot0": {"name": "item_manta", "cooldown": 0, "can_cast": True},
             "slot1": {"name": "item_black_king_bar", "cooldown": 40, "can_cast": False},
             "slot6": {"name": "item_glimmer_cape", "cooldown": 0, "can_cast": True},  # backpack: can't use
             "neutral0": {"name": "item_mysterious_hat", "cooldown": 0, "can_cast": True},
             "teleport0": {"name": "item_tpscroll", "cooldown": 0, "can_cast": True}}
    snap = _snap(store, 1.0, items=items)
    assert snap.ready_items() == {"item_manta", "item_mysterious_hat", "item_tpscroll"}


def test_a_dispel_call_fires_the_moment_you_are_silenced():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: silenced and ready item_manta => Manta now to purge the silence.", clock=900))
    manta = {"slot0": {"name": "item_manta", "cooldown": 0, "can_cast": True}}
    assert book.check(_snap(store, 1.0, items=manta), store) == []
    fired = book.check(_snap(store, 1.1, items=manta, hero={"silenced": True}), store)
    assert [a.text for a in fired] == ["Manta now to purge the silence."]
    assert fired[0].cut_in  # it's about this very moment


def test_a_save_call_waits_while_the_item_is_on_cooldown():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: silenced and ready item_manta => Manta now.", clock=900))
    on_cd = {"slot0": {"name": "item_manta", "cooldown": 20, "can_cast": False}}
    book.check(_snap(store, 1.0, items=on_cd), store)
    assert book.check(_snap(store, 1.1, items=on_cd, hero={"silenced": True}), store) == []


def test_burst_damage_is_hp_lost_in_the_last_two_seconds():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: hp_lost >= 30 => Glimmer yourself now.", clock=900))
    assert book.check(_snap(store, 10.0, hp=1000), store) == []
    assert book.check(_snap(store, 11.0, hp=900), store) == []  # chip damage
    fired = book.check(_snap(store, 11.8, hp=550), store)  # 45% gone within two seconds
    assert [a.text for a in fired] == ["Glimmer yourself now."]
    facts = tl.facts_for(_snap(store, 30.0, hp=550), store)
    assert facts["hp_lost"] == 0  # (nothing lost lately: the slow walk back isn't burst)


def test_buyback_is_known_only_when_you_can_afford_it_and_it_is_off_cooldown():
    store = GameStateStore()
    bb = {"buyback_cost": 1500, "buyback_cooldown": 0}
    assert tl.facts_for(_snap(store, 1.0, gold=2000, alive=False, hero=bb))["buyback"]
    assert not tl.facts_for(_snap(store, 1.1, gold=2000, hero=bb))["buyback"]  # alive: nothing to buy back
    assert not tl.facts_for(_snap(store, 1.2, gold=1000, alive=False, hero=bb))["buyback"]
    assert not tl.facts_for(_snap(store, 1.3, gold=2000, alive=False,
                                  hero={"buyback_cost": 1500, "buyback_cooldown": 300}))["buyback"]
    assert "buyback" not in tl.facts_for(_snap(store, 1.4))  # not reported: unknown, so a call waits


def test_a_buyback_call_fires_on_the_death_screen_not_after_respawning():
    store, book = GameStateStore(), tl.TriggerBook()
    book.arm(tl.parse("TRIGGER: buyback => Buy back now and defend high ground.", clock=900))
    bb = {"buyback_cost": 1500, "buyback_cooldown": 0}
    assert book.check(_snap(store, 1.0, gold=1000, hero=bb), store) == []
    assert book.check(_snap(store, 1.1, gold=1000, alive=False, hero=bb), store) == []  # dead, can't afford it
    fired = book.check(_snap(store, 1.2, gold=1600, alive=False, hero=bb), store)
    assert [a.text for a in fired] == ["Buy back now and defend high ground."]


# the look before you respawn -------------------------------------------------------------------------------
def _payload(m, alive=True, respawn=0):
    """The scripted match, after one more death than the fixture's last update."""
    p = m.payload(TOKEN)
    p["hero"]["alive"], p["hero"]["respawn_seconds"] = alive, respawn
    p["player"]["deaths"] += 1
    return p


async def test_a_look_starts_three_seconds_before_respawn_and_its_tip_lands_after(live):
    coach, fake, rec, kb = live
    m = ScriptedMatch()
    for _ in range(700):
        m.step(1)
    fake.reply = "You died to a gank."
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=12)))  # just died
    assert coach._inflight[0] == "death"
    await _wait_inflight(coach)
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=8)))
    assert coach._inflight[0] != "watch"  # (the regular rhythm may look while you're dead: cached only)
    await _wait_inflight(coach)

    fake.reply = "SAY: Buy Black King Bar, then farm the bottom jungle."
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=3)))
    assert coach._inflight[0] == "watch"
    look = coach._inflight[2]
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=2)))  # still the same death
    assert coach._inflight[2] is look  # one look per death
    snap = coach.store.ingest(_payload(m, alive=True))  # back in the game before the answer arrives
    coach.on_snapshot(snap)  # (and the respawn itself doesn't start a second look)
    assert coach._inflight[2] is look
    await _wait_inflight(coach)
    assert "respawns in 3 seconds" in str(fake.requests[-1]["messages"])
    tip = [a for a in rec.items if a.display and a.final and a.text.startswith("Buy Black King Bar")]
    assert tip and tip[-1].speak  # not dropped as "you respawned meanwhile": that was the point


async def test_the_look_before_respawn_never_cuts_off_the_death_answer(live):
    coach, fake, rec, kb = live
    m = ScriptedMatch()
    for _ in range(700):
        m.step(1)
    fake.reply = "You died because you were alone in their jungle with three of them missing."
    fake.chunk_s = 0.05
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=4)))  # a short respawn
    death = coach._inflight[2]
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=3)))
    assert coach._inflight[2] is death and not death.cancelled()  # waits for the answer
    await _wait_inflight(coach)
    fake.chunk_s = 0.005
    coach.on_snapshot(coach.store.ingest(_payload(m, alive=False, respawn=2)))
    assert coach._inflight[0] == "watch"  # then it looks
    await _wait_inflight(coach)
