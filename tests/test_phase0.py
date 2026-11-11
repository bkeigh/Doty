"""Phase 0 of docs/NEXT_PLAN.md: Turbo (detected, and its numbers), one answer to "what to buy", demo games
not counted as yours, LIVE going out when a match stops, and one tag per kind of line."""

import time

import pytest

from doty import app as appmod
from doty.config import Config
from doty.knowledge import mode as game_mode
from doty.knowledge import timings
from doty.knowledge.mode import Mode
from doty.knowledge.profile import Profile
from doty.knowledge.stats import Stats
from doty.review import MatchRecorder, Review, latest_review
from doty.rules.economy import BUYBACK_FROM, AffordableBuildItem, StashItems, spendable
from doty.rules.progression import FarmCheck, ItemPace
from doty.rules.survival import Buyback
from doty.rules.timers import RoshanAegis, SpawnTimer
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN
from .test_game_sense import GAME, JUGG, _ctx, _feed, _kb

TURBO = Mode("turbo", "setting")


def _snap(store, clock, xpm=0, match="7", custom=""):
    return store.ingest({"map": {"matchid": match, "clock_time": clock, "game_state": GAME, "customgamename": custom},
                         "player": {"team_name": "radiant", "xpm": xpm},
                         "hero": {"name": JUGG, "id": 8, "alive": True, "health": 500, "max_health": 1000}},
                        received_t=float(clock))


# which mode ------------------------------------------------------------------------------------------------
def test_what_you_usually_play_comes_from_your_recent_matches():
    turbo, normal = {"game_mode": 23}, {"game_mode": 22}
    assert game_mode.usual([turbo] * 20) == "turbo"
    assert game_mode.usual([turbo] * 10 + [normal] * 10) == "turbo"  # half and half: what you played is Turbo too
    assert game_mode.usual([turbo] * 3 + [normal] * 17) == "normal"
    assert game_mode.usual([turbo] * 4) is None  # too few to say
    assert game_mode.usual(None) is None and game_mode.usual({"error": "x"}) is None


def test_the_match_itself_shows_turbo_by_how_fast_experience_comes_in():
    store = GameStateStore()
    assert game_mode.evidence(_snap(store, 60, xpm=1500)) is None  # too early: one kill swings it
    assert game_mode.evidence(_snap(store, 200, xpm=900)) == "turbo"  # no normal lane gives that
    assert game_mode.evidence(_snap(store, 200, xpm=250)) is None  # a slow start says nothing yet
    assert game_mode.evidence(_snap(store, 330, xpm=250)) == "normal"  # even a shared Turbo lane gives more
    assert game_mode.evidence(_snap(store, 330, xpm=520)) is None  # a normal solo lane or a Turbo support
    # After the lanes (Doty started mid-match) the bars are higher: everyone's rate has climbed.
    assert game_mode.evidence(_snap(store, 900, xpm=900)) is None  # a normal game's mid, or a Turbo support
    assert game_mode.evidence(_snap(store, 900, xpm=1300)) == "turbo"
    assert game_mode.evidence(_snap(store, 900, xpm=380)) == "normal"


def test_your_setting_wins_then_the_match_then_your_history():
    store = GameStateStore()
    assert game_mode.observe(_snap(store, 30), store) == game_mode.NORMAL
    assert game_mode.observe(_snap(store, 31), store, "auto", "turbo") == Mode("turbo", "history")
    assert game_mode.of(store.memory).turbo
    assert game_mode.observe(_snap(store, 32), store, "normal", "turbo") == Mode("normal", "setting")
    # The match shows a normal game though your history says Turbo: the match is right, and stays right.
    assert game_mode.observe(_snap(store, 330, xpm=240), store, "auto", "turbo") == Mode("normal", "match")
    assert game_mode.observe(_snap(store, 400, xpm=600), store, "auto", "turbo") == Mode("normal", "match")
    assert game_mode.observe(_snap(store, 5, match="8"), store, "auto", "turbo").turbo  # a new match starts over


def test_the_demo_is_always_a_normal_game():
    store = GameStateStore()
    snap = _snap(store, 200, xpm=2000, match="doty-sim-1")
    assert snap.is_demo and game_mode.observe(snap, store, "turbo", "turbo") == Mode("normal", "demo")
    assert not _snap(store, 200, match="8123456789").is_demo


def test_a_mode_puts_normal_game_times_on_its_own_clock():
    assert game_mode.NORMAL.at(1800) == 1800 and TURBO.at(1800) == 900
    assert TURBO.as_normal(450) == 900 and TURBO.as_normal(-60) == -60 and game_mode.NORMAL.as_normal(450) == 450
    assert TURBO.label == "Turbo" and "TURBO" in game_mode.prompt_note(TURBO) and game_mode.prompt_note(Mode()) == ""


# Turbo's numbers --------------------------------------------------------------------------------------------
def test_turbo_has_its_own_spawn_table_and_roshan_timer():
    keys = {sp.key: sp for sp in timings.spawns(turbo=True)}
    assert "tormentor" not in keys  # (its Turbo time isn't known: not announced rather than wrong)
    assert keys["neutral_t2"].first == 450 and keys["neutral_t5"].first == 1800  # twice as fast
    assert keys["power"].first == 360 and keys["bounty"].every == 240  # runes keep their times
    assert {sp.key for sp in timings.spawns()} - set(keys) == {"tormentor"}
    assert timings.roshan_window(600) == (1080, 1260) and timings.roshan_window(600, turbo=True) == (840, 930)
    assert timings.aegis_s() == 300 and timings.aegis_s(turbo=True) == 240


def test_the_timer_rules_follow_the_mode():
    kb, store = _kb(), GameStateStore()
    _feed(store, 1.0, clock=1190)  # 19:50: the Tormentors are 10 s away in a normal game
    said = [a.text for a in SpawnTimer().evaluate(_ctx(store, kb))]
    assert any("Tormentors" in t for t in said)
    store.memory["mode"] = TURBO
    assert not any("Tormentors" in a.text for a in SpawnTimer().evaluate(_ctx(store, kb)))
    # Roshan died at 20:00: in Turbo the window opens at 24:00, not 28:00.
    _feed(store, 2.0, clock=1425)
    store.memory["roshan_killed_clock"] = 1200
    assert [a.speech for a in RoshanAegis().evaluate(_ctx(store, kb))] == ["Roshan can spawn in 20"]
    store.memory["mode"] = game_mode.NORMAL
    assert not list(RoshanAegis().evaluate(_ctx(store, kb)))


def test_item_timings_and_build_phases_run_twice_as_fast_in_turbo():
    kb = _kb()
    kb.item_timings[(8, "item_manta")] = [{"hero_id": 8, "time": t, "games": g, "wins": g // 2}
                                          for t, g in ((900, 20), (1200, 170), (1500, 545), (1800, 494))]
    assert kb.typical_time(8, "item_manta") == 1500 and kb.item_odds(8, "item_manta")
    assert "item_manta" not in kb.build_plan(JUGG, 500)  # 8:20 of a normal game: early items
    kb.mode = TURBO
    assert kb.typical_time(8, "item_manta") == 750  # 12:30 on a Turbo clock
    assert kb.spikes(8) == [(750, "item_manta")]
    assert kb.item_odds(8, "item_manta") == [] and kb.win_rate_by(8, "item_manta", 700) is None  # normal games only
    assert "item_manta" in kb.build_plan(JUGG, 500)  # 8:20 in Turbo is a normal game's 16:40: mid-game items
    assert "item_manta" not in kb.build_plan(JUGG, 300)
    kb.mode = game_mode.NORMAL
    assert kb.spikes(8) == [(1500, "item_manta")]  # (not a stale answer from the other mode)


def test_buyback_gold_is_kept_from_fifteen_minutes_in_turbo():
    kb, store = _kb(), GameStateStore()
    snap = _feed(store, 1.0, clock=1000, gold=3000, extra={"buyback_cost": 1200, "buyback_cooldown": 0})
    assert BUYBACK_FROM == 1800 and spendable(snap) == 3000 and spendable(snap, TURBO) == 1800
    _feed(store, 2.0, clock=1000, gold=500, extra={"buyback_cost": 1200, "buyback_cooldown": 0})
    assert not list(Buyback().evaluate(_ctx(store, kb)))  # 16:40 of a normal game: too early to matter
    store.memory["mode"] = TURBO
    assert [a.speech for a in Buyback().evaluate(_ctx(store, kb))] == ["No buyback. Play safe."]


def test_turbo_drops_what_its_rules_make_wrong():
    kb, store = _kb(), GameStateStore()
    # The courier brings the stash by itself.
    _feed(store, 1.0, clock=400, stash=["item_ogre_axe"], pos=(0, 0))
    assert not list(StashItems().evaluate(_ctx(store, kb)))  # (it has to sit there a few seconds first)
    _feed(store, 20.0, clock=419, stash=["item_ogre_axe"], pos=(0, 0))
    assert [a.speech for a in StashItems().evaluate(_ctx(store, kb))] == ["Courier your stash"]
    store.memory["mode"] = TURBO
    assert not list(StashItems().evaluate(_ctx(store, kb)))
    # Every shop sells Secret Shop items.
    kb.custom_plan[JUGG] = ["item_manta"]
    _feed(store, 30.0, clock=430, gold=2900, items=["item_yasha"], pos=(0, 0))
    card = list(AffordableBuildItem().evaluate(_ctx(store, kb)))[0]
    assert card.speech == "Finish Manta Style" and "secret shop" not in card.text
    # The farm benchmark is from normal games: no verdict on a Turbo game.
    _feed(store, 40.0, clock=601, lh=10, pos=(0, 0))
    assert not list(FarmCheck().evaluate(_ctx(store, kb)))
    store.memory["mode"] = game_mode.NORMAL
    assert [a.speech for a in FarmCheck().evaluate(_ctx(store, kb))] == ["Farm is behind. Focus last hits."]


def test_the_start_card_waits_for_the_mode_before_the_horn():
    from doty.rules.progression import GameStartCard

    kb, store = _kb(), GameStateStore()
    kb.item_timings[(8, "item_power_treads")] = [{"hero_id": 8, "time": 600, "games": 100, "wins": 50}]
    _feed(store, 1.0, clock=-30)
    store.memory["mode_pending"] = True  # your recent matches are still being fetched
    assert not list(GameStartCard().evaluate(_ctx(store, kb)))
    store.memory.pop("mode_pending")
    kb.mode = store.memory["mode"] = TURBO
    card = list(GameStartCard().evaluate(_ctx(store, kb)))[0]
    assert "Power Treads (~5:00)" in card.text  # 10:00 in a normal game


def test_item_pace_speaks_up_on_the_turbo_clock():
    kb, store = _kb(), GameStateStore()
    kb.item_timings[(8, "item_power_treads")] = [{"hero_id": 8, "time": 600, "games": 100, "wins": 50}]
    kb.mode = store.memory["mode"] = TURBO  # (usually done by 10:00; in Turbo, 5:00)
    _feed(store, 1.0, clock=200, gold=100)
    assert not list(ItemPace().evaluate(_ctx(store, kb)))  # 3:20: more than a (Turbo) minute to go
    store.memory["mode"] = TURBO
    _feed(store, 2.0, clock=250, gold=100)
    out = list(ItemPace().evaluate(_ctx(store, kb)))
    assert len(out) == 1 and out[0].text.startswith("Power Treads usually lands by 5:00")


# one answer to "what to buy" -------------------------------------------------------------------------------
def test_shopping_gives_one_thing_to_buy_now_and_what_comes_after():
    kb = _kb()
    kb.custom_plan[JUGG] = ["item_black_king_bar", "item_manta"]
    plan = kb.shopping(JUGG, 700, ["item_phase_boots", "item_ogre_axe"], 1700)
    assert [p["item"] for p in plan] == ["item_black_king_bar", "item_manta"]
    first = plan[0]
    assert first["left"] == 3050 and first["short"] == 1350 and first["cost"] == 4050
    assert first["buy"] == "item_mithril_hammer" and first["buy_cost"] == 1600 and not first["finish"]
    assert all("buy" not in later for later in plan[1:])  # your gold goes to one item at a time
    # Power Treads never joins the list: you have boots.
    assert "item_power_treads" not in [p["item"] for p in kb.shopping(JUGG, 700, ["item_phase_boots"], 0, n=9)]
    done = kb.shopping(JUGG, 700, ["item_phase_boots", "item_ogre_axe", "item_mithril_hammer"], 1500, n=1)
    assert done[0]["finish"] and done[0]["buy"] == "item_black_king_bar" and done[0]["short"] == 0
    saving = kb.shopping(JUGG, 700, ["item_phase_boots"], 300, n=1)
    assert saving[0]["buy"] is None and saving[0]["short"] == 3750
    assert kb.shopping("npc_dota_hero_unknown", 700, [], 9999) == []


def test_only_one_pair_of_boots_is_ever_in_the_list():
    kb = _kb()
    plan = kb.shopping(JUGG, 100, [], 0, n=9)  # early: Power Treads and Phase Boots are both popular
    boots = [p["item"] for p in plan if p["item"] in ("item_power_treads", "item_phase_boots")]
    assert boots == ["item_power_treads"]


def test_a_stepping_stone_stands_in_the_plan_as_what_it_builds_into():
    kb = _kb()
    kb.item_popularity[8]["mid_game_items"] = {"170": 80}  # mid game: Yasha is the most bought...
    kb.item_popularity[8]["late_game_items"] = {"147": 60}  # ...on the way to Manta Style
    assert kb.build_plan(JUGG, 1000) == ["item_power_treads", "item_phase_boots", "item_manta"]


def test_a_player_who_is_behind_is_pointed_at_the_core_they_skipped():
    """The plan is this phase's items after the big ones of the phase before: late in the game with nothing
    built, Manta Style and Black King Bar come before anything that only late-game inventories buy."""
    kb = _kb()
    kb.items["item_wraith_band"] = {"id": 75, "dname": "Wraith Band", "cost": 505, "components": ["slippers"]}
    kb.items["item_butterfly"] = {"id": 139, "dname": "Butterfly", "cost": 5450, "components": ["eaglesong"]}
    kb.item_popularity[8]["early_game_items"]["75"] = 70
    kb.item_popularity[8]["late_game_items"] = {"139": 90, "147": 60}
    assert kb.build_plan(JUGG, 500) == ["item_power_treads", "item_wraith_band", "item_phase_boots"]
    mid = kb.build_plan(JUGG, 1000)
    assert mid[:2] == ["item_power_treads", "item_phase_boots"]  # boots carry over; the cheap early item doesn't
    assert "item_wraith_band" not in mid and mid[2:] == ["item_manta", "item_black_king_bar"]
    late = kb.build_plan(JUGG, 2000)
    assert late == ["item_manta", "item_black_king_bar", "item_butterfly"]  # (early boots are two phases back)
    assert kb.next_target(JUGG, 2000, ["item_phase_boots"]) == "item_manta"
    assert kb.next_target(JUGG, 2000, ["item_phase_boots", "item_manta"]) == "item_black_king_bar"
    assert kb.build_plan(JUGG, 2000) is late  # (kept until the data changes: the rules ask ten times a second)
    kb.item_popularity[8]["late_game_items"]["116"] = 95
    assert kb.build_plan(JUGG, 2000) is not late


def test_before_the_horn_the_plan_is_the_early_games(tmp_path, monkeypatch):
    kb = _kb()
    assert kb.build_plan(JUGG, -60) == ["item_power_treads", "item_phase_boots"]  # (no starting-item data here)
    assert kb.shopping(JUGG, -60, [], 600)[0]["item"] == "item_power_treads"
    # No build data for the hero yet is "still loading", not "you have everything".
    doty = _doty(tmp_path, monkeypatch)
    snap = doty.store.ingest(_payload(-60))
    assert doty._shopping(snap)
    doty.kb.item_popularity.clear()
    assert doty._shopping(snap) is None and doty._game_view(snap)["build"] is None


def test_travel_boots_and_a_consumed_scepter_count_as_owned():
    kb, store = _kb(), GameStateStore()
    kb.items["item_travel_boots"] = {"id": 48, "dname": "Boots of Travel", "cost": 2500, "components": ["boots"]}
    kb.items["item_ultimate_scepter"] = {"id": 108, "dname": "Aghanim's Scepter", "cost": 4200,
                                         "components": ["ogre_axe", "blade_of_alacrity"]}
    kb.items["item_recipe_ultimate_scepter_2"] = {"id": 270, "dname": "Blessing Recipe", "cost": 1600}
    kb.items["item_ultimate_scepter_2"] = {"id": 271, "dname": "Aghanim's Blessing", "cost": 5800,
                                           "components": ["ultimate_scepter"]}
    late = kb.shopping(JUGG, 1000, ["item_travel_boots"], 0, n=9)
    assert not {"item_power_treads", "item_phase_boots"} & {p["item"] for p in late}
    # The Scepter's effect with no Scepter in a slot is a consumed Blessing: nothing left to finish.
    kb.custom_plan[JUGG] = ["item_ultimate_scepter_2", "item_manta"]
    snap = _feed(store, 1.0, clock=2400, gold=2000, extra={"aghanims_scepter": True})
    assert kb.next_target(JUGG, 2400, snap.item_list()) == "item_manta"
    held = _feed(store, 2.0, clock=2401, gold=2000, items=["item_ultimate_scepter"], extra={"aghanims_scepter": True})
    assert kb.next_target(JUGG, 2401, held.item_list()) == "item_ultimate_scepter_2"  # holding it: the Blessing is next


def test_plain_boots_arent_a_start_on_any_one_pair():
    kb = _kb()
    kb.custom_plan[JUGG] = ["item_black_king_bar", "item_power_treads"]
    assert kb.next_target(JUGG, 600, ["item_boots"]) == "item_black_king_bar"  # (not Treads because you hold Boots)
    assert kb.next_target(JUGG, 600, ["item_boots", "item_gloves"]) == "item_power_treads"  # Gloves are a start


def test_the_strip_the_rules_the_coach_and_the_app_agree(tmp_path, monkeypatch):
    from doty.coach import prompts

    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb = doty.engine.kb = _kb()
    doty.kb._full.add(8)
    doty.kb.custom_plan[JUGG] = ["item_black_king_bar", "item_manta"]
    m = ScriptedMatch(match_id="555")
    m.step(90 + 700)
    payload = m.payload(TOKEN)
    payload["items"]["slot5"] = {"name": "item_ogre_axe"}
    payload["player"]["gold"] = 1700
    snap = doty.store.ingest(payload)
    build = doty._game_view(snap)["build"]
    assert [e["key"] for e in build] == ["black_king_bar", "manta"]
    assert build[0]["buy"] == "mithril_hammer" and build[0]["buy_name"] == "Mithril Hammer"
    assert doty._hud(snap)["item"]["name"] == "Black King Bar" and doty._hud(snap)["item"]["left"] == build[0]["short"]
    told = prompts.next_item(snap, doty.kb)
    assert told["item"] == "Black King Bar" and told["gold_left"] == build[0]["short"]
    assert told["buy_now"].startswith("Mithril Hammer") and told["after_it"] == ["Manta Style"]
    from doty.rules.base import RuleContext

    ctx = RuleContext(snap, None, doty.store, doty.cfg.rules, doty.kb, [])
    assert [a.speech for a in AffordableBuildItem().evaluate(ctx)] == ["Buy Mithril Hammer"]


# the app follows the mode, and knows when a match stopped -------------------------------------------------
def _doty(tmp_path, monkeypatch, **rules):
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    cfg = Config()
    for key, value in rules.items():
        setattr(cfg.rules, key, value)
    doty = appmod.Doty(cfg, overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb = doty.engine.kb = _kb()
    doty.kb._full.add(8)
    return doty


def _payload(clock, match="555", **player):
    m = ScriptedMatch(match_id=match)
    m.step(90 + clock)
    payload = m.payload(TOKEN)
    payload["player"].update(player)
    return payload


def test_the_engine_coaches_a_turbo_game_as_turbo(tmp_path, monkeypatch):
    from doty.coach import prompts

    doty = _doty(tmp_path, monkeypatch, game_mode="turbo")
    doty._on_payload(_payload(700), 1.0)
    snap = doty.store.current
    assert doty.kb.mode == Mode("turbo", "setting") and doty._game_view(snap)["mode"] == "turbo"
    state = prompts.state_summary(snap, doty.store, doty.kb, [])
    assert state["phase"] == "mid game" and "TURBO" in state["game_mode"]  # (11:40 is past a Turbo lane)
    assert not any("Tormentor" in line for line in prompts.coming_up(1100, turbo=True))
    assert any("Tormentor" in line for line in prompts.coming_up(1100))
    assert doty._laning() is None  # the laning gauge is over by 5:00 in Turbo
    # The demo is coached as the normal game it is, whatever the setting.
    doty._on_payload(_payload(100, match="doty-sim-1"), 2.0)
    assert doty.kb.mode == Mode("normal", "demo") and "game_mode" not in prompts.state_summary(
        doty.store.current, doty.store, doty.kb, [])


def test_your_history_is_only_looked_up_for_a_real_account_while_running(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(Profile, "usual_mode", lambda self, account: asked.append(account) or "turbo")
    doty._on_payload(_payload(10, accountid="47"), 1.0)  # (not started: a test, or the engine shutting down)
    assert not asked and doty.kb.mode == game_mode.NORMAL
    doty.running = True
    doty._on_payload(_payload(11, accountid="0"), 2.0)  # the demo's account: nobody
    doty._on_payload(_payload(12, match="doty-sim-1", accountid="47"), 3.0)
    assert not asked
    doty._on_payload(_payload(13, accountid="47"), 4.0)
    deadline = time.time() + 5
    while doty._usual_mode is None and time.time() < deadline:
        time.sleep(0.01)
    doty._on_payload(_payload(14, accountid="47"), 5.0)
    assert asked == [47] and doty.kb.mode == Mode("turbo", "history")  # asked once, used from then on
    assert "mode_pending" not in doty.store.memory
    # Another account (a friend on this PC): its own history, never the last one's; and when OpenDota has
    # nothing, it's asked again later, not every update.
    monkeypatch.setattr(Profile, "usual_mode", lambda self, account: asked.append(account) and None)
    doty._on_payload(_payload(15, accountid="48"), 6.0)
    deadline = time.time() + 5
    while doty._usual_pending and time.time() < deadline:
        time.sleep(0.01)
    doty._on_payload(_payload(16, accountid="48"), 7.0)
    assert asked == [47, 48] and doty.kb.mode == game_mode.NORMAL
    doty._usual_asked -= doty.USUAL_RETRY_S + 1
    doty._on_payload(_payload(17, accountid="48"), 8.0)
    deadline = time.time() + 5
    while doty._usual_pending and time.time() < deadline:
        time.sleep(0.01)
    assert asked == [47, 48, 48]
    doty.running = False


def test_live_goes_out_seconds_after_a_match_stops_sending(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch)
    payload = _payload(300)
    doty._on_payload(payload, 1.0)
    doty.server.received = 1
    doty.server.last_payload_t = time.perf_counter()
    assert doty.status()["phase"] == "live" and doty._match_running()
    doty.server.last_payload_t = time.perf_counter() - 6  # a hitch or a reconnect: still your match
    assert doty.status()["phase"] == "live"
    doty.server.last_payload_t = time.perf_counter() - 13  # past Dota's own heartbeat: it stopped sending
    assert doty.status()["phase"] == "menu" and not doty._match_running() and doty._laning() is None
    payload["map"]["paused"] = True  # a paused match only sends its heartbeat: still your match
    doty._on_payload(payload, 2.0)
    doty.server.last_payload_t = time.perf_counter() - 20
    assert doty.status()["phase"] == "live"
    doty.server.last_payload_t = time.perf_counter() - 40
    assert doty.status()["phase"] == "stale"
    # Stopping the demo is known the moment it happens; the next real update is live again.
    payload["map"]["paused"] = False
    doty._on_payload(payload, 3.0)
    doty.server.last_payload_t = time.perf_counter()
    doty.match_gone()
    assert doty.status()["phase"] == "menu"
    doty._on_payload(payload, 4.0)
    assert doty.status()["phase"] == "live"


# demo games aren't yours -----------------------------------------------------------------------------------
def test_the_demo_review_never_counts_as_your_last_game(tmp_path):
    for match in ("8123", "doty-sim-1"):  # your game first, then the demo (the newest file)
        rec, store = MatchRecorder(_kb(), out_dir=tmp_path), GameStateStore()
        for clock in (10, 70, 130):
            rec.on_snapshot(_snap(store, clock, match=match), store)
        review = rec.finish()
        assert review.demo == (match == "doty-sim-1") and review.mode == "normal"
        time.sleep(0.05)
    assert latest_review(tmp_path).match_id == "8123"
    assert latest_review(tmp_path, demos=True).match_id == "doty-sim-1"  # (its own recap still shows it)
    # A demo review saved before the flag existed is told by its match id.
    assert Review.from_dict({"match_id": "doty-sim-1"}).demo and not Review.from_dict({"match_id": "8123"}).demo


def test_a_review_says_which_mode_it_was(tmp_path):
    from doty.review import focus_points, review_prompt

    kb = _kb()
    kb.mode = TURBO
    rec, store = MatchRecorder(kb, out_dir=tmp_path), GameStateStore()
    rec.on_snapshot(_snap(store, 10), store)
    assert rec.finish().mode == "turbo" and latest_review(tmp_path).mode == "turbo"
    # A Turbo game's farm and item pace aren't judged by a normal game's numbers, and the AI is told the mode.
    slow = Review(hero_name="Juggernaut", lh_10=20, lh_per_min_10=2.0, benchmark_lh_per_min=8.0,
                  item_pace=[("Battle Fury", 1500, 900)])
    assert any("last hits at 10:00" in p for p in focus_points(slow))
    assert any("Battle Fury came at" in p for p in focus_points(slow))
    slow.mode = "turbo"
    assert focus_points(slow) == [] and "TURBO" in review_prompt(slow) and "TURBO" not in review_prompt(Review())


def test_the_review_closes_a_match_under_its_own_mode(tmp_path, monkeypatch):
    doty = _doty(tmp_path, monkeypatch, game_mode="turbo")
    doty.review = MatchRecorder(doty.kb, out_dir=tmp_path)
    doty._on_payload(_payload(100, match="doty-sim-1"), 1.0)  # the demo: a normal game whatever the setting
    doty._on_payload(_payload(160, match="doty-sim-1"), 2.0)
    doty._on_payload(_payload(10, match="901"), 3.0)  # a real (Turbo) match starts: the demo's review is written
    assert doty.review.last_review.match_id == "doty-sim-1" and doty.review.last_review.mode == "normal"
    doty._on_payload(_payload(70, match="901"), 4.0)
    assert doty.review.finish().mode == "turbo"


# the profile -------------------------------------------------------------------------------------------------
def test_turbo_games_get_no_score_and_say_what_you_usually_play(tmp_path):
    def row(i, mode):
        return {"match_id": i, "hero_id": 71, "player_slot": 0, "radiant_win": True, "gold_per_min": 900,
                "xp_per_min": 1400, "last_hits": 120, "duration": 1500, "start_time": 1000 - i, "game_mode": mode}

    def fetch(url):
        if url.endswith("/recentMatches"):
            return [row(i, 23) for i in range(6)] + [row(9, 22)]
        if "benchmarks" in url:
            return {"result": {"gold_per_min": [{"percentile": 0.5, "value": 450}, {"percentile": 0.9, "value": 590}]}}
        pytest.fail(url)

    profile = Profile(Stats(cache_dir=tmp_path, fetch=fetch))
    games = profile.recent(47)
    assert [g.turbo for g in games] == [True] * 6 + [False]
    assert all(g.score is None for g in games[:6]) and games[6].score == 90  # 900 GPM would flatter a Turbo game
    assert profile.usual_mode(47) == "turbo"


# one tag per kind of line -----------------------------------------------------------------------------------
def test_the_feed_and_the_overlay_tag_a_line_the_same():
    from doty.advice import Advice, Priority
    from doty.overlay import card_style, card_tag

    for category, tag in (("progression", "LEVEL"), ("economy", "GOLD"), ("survival", "DANGER"), ("timer", "TIMER"),
                          ("plan", "PLAN")):
        assert card_tag("rules", category, int(Priority.NORMAL)) == tag
        assert card_style(Advice("x", Priority.NORMAL, category))[1] == tag
    assert card_tag("coach", "coach", 2) == "DOTY" and card_tag("player", "general", 3) == "YOU"
    assert card_tag("rules", "general", int(Priority.CRITICAL)) == "NOW"
