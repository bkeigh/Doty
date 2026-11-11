"""Knowing the heroes: which side a long game favours (public win rate by game length), which enemy
spells a dispel removes, and what really killed you (your own last seconds, from the game data)."""

from doty.coach import prompts
from doty.knowledge import curves
from doty.knowledge.constants import Knowledge
from doty.lineup import Lineup
from doty.state import GameStateStore

RADIANT = ["npc_dota_hero_a1", "npc_dota_hero_a2", "npc_dota_hero_a3", "npc_dota_hero_a4", "npc_dota_hero_a5"]
DIRE = ["npc_dota_hero_e1", "npc_dota_hero_e2", "npc_dota_hero_e3", "npc_dota_hero_e4", "npc_dota_hero_e5"]


def _rows(early: float, late: float, games: int = 1000) -> list[dict]:
    """OpenDota's /heroes/{id}/durations shape: 5-minute bins, unsorted."""
    return [{"duration_bin": 1200, "games_played": games, "wins": int(games * early)},
            {"duration_bin": 1800, "games_played": games, "wins": games // 2},  # (mid-length: not counted)
            {"duration_bin": 2700, "games_played": games, "wins": int(games * late)},
            {"duration_bin": 600, "games_played": 3, "wins": 3}]


def _kb(ours=(0.55, 0.45), theirs=(0.45, 0.56)) -> Knowledge:
    kb = Knowledge()
    for i, npc in enumerate(RADIANT + DIRE, start=1):
        kb.heroes[npc] = {"id": i, "localized_name": npc.rsplit("_", 1)[1].upper()}
        kb.durations[i] = _rows(*(ours if npc in RADIANT else theirs))
    return kb


def test_a_hero_curve_is_its_win_rate_in_short_and_long_games():
    c = curves.curve(_rows(0.4, 0.6))
    assert abs(c.early - 0.4) < 0.01 and c.late == 0.6  # (every bin under 25:00 is "short")
    assert curves.curve(_rows(0.4, 0.6, games=50)) is None  # too few games to say
    assert curves.curve("not a list") is None


def test_the_plan_says_who_the_clock_favours():
    plan = _kb().game_plan(RADIANT, DIRE)
    assert plan.side == "them" and plan.lean < 0
    assert "their lineup gets stronger the longer the game goes" in plan.card()
    assert "Fight, push and take objectives early" in plan.card()
    assert _kb().game_plan(DIRE, RADIANT).side == "you"  # the other way round
    even = _kb(ours=(0.5, 0.5), theirs=(0.5, 0.51)).game_plan(RADIANT, DIRE)
    assert even.side == "" and "Neither lineup" in even.card()
    kb = _kb()
    kb.durations.clear()
    assert kb.game_plan(RADIANT, DIRE) is None  # still loading


def _rules_ctx(kb, clock=-60):
    from doty.config import RulesConfig
    from doty.rules.base import RuleContext
    from doty.state import Snapshot

    snap = Snapshot({"map": {"clock_time": clock, "matchid": "7"}, "player": {"team_name": "radiant"},
                     "hero": {"name": RADIANT[0], "id": 1, "alive": True}})
    store = GameStateStore()
    store.current = snap
    votes = store.memory["lineup_votes"] = Lineup()
    for _ in range(2):
        votes.add(RADIANT, DIRE, RADIANT[0], "radiant")
    return RuleContext(snap, None, store, RulesConfig(), kb, [])


def test_the_game_plan_card_comes_once_the_lineup_is_known():
    from doty.rules.matchup import GamePlan

    rule, ctx = GamePlan(), _rules_ctx(_kb())
    out = list(rule.evaluate(ctx))
    assert [a.spoken for a in out] == ["They scale better. Win early."]
    assert ctx.store.memory["game_plan_brief"].startswith("Timing: their heroes")  # the brief says the same
    assert "%" in out[0].text  # the numbers behind it are on the card
    assert list(rule.evaluate(ctx)) == []  # once
    late = _rules_ctx(_kb(), clock=900)
    assert list(rule.evaluate(late)) == []  # past 10:00 it's not a plan anymore
    even = _rules_ctx(_kb(ours=(0.5, 0.5), theirs=(0.5, 0.5)))
    assert list(rule.evaluate(even)) == [] and even.store.memory["game_plan"] == "even"


def test_the_brief_has_the_timing_and_what_each_spell_a_dispel_removes():
    kb = _kb()
    kb.hero_abilities = {DIRE[0]: {"abilities": ["e1_hex", "e1_ult"]}}
    kb.ability_info = {"e1_hex": {"dname": "Hex", "desc": "Turns an enemy into a critter.", "cd": 13.0,
                                  "dispel": "strong only"},
                       "e1_ult": {"dname": "Doom", "desc": "Silences and burns.", "cd": 120.0, "bkb": True,
                                  "dispel": "none"}}
    lineup = {"radiant": [kb.hero_name(n) for n in RADIANT], "dire": [kb.hero_name(n) for n in DIRE]}
    plan = kb.game_plan(RADIANT, DIRE)
    brief = prompts.match_brief(lineup, "radiant", RADIANT[0], kb, complete=True, timing=plan.brief())
    assert "Timing: their heroes win more of the long games" in brief
    assert "- Hex: Turns an enemy into a critter. (13 s cooldown, dispel: strong only)" in brief
    assert "(120 s cooldown, goes through BKB, dispel: none)" in brief
    assert "Timing" not in prompts.match_brief(lineup, "radiant", RADIANT[0], kb)  # until GamePlan decides


def test_dispel_words_come_from_dotaconstants():
    from doty.knowledge.constants import DISPEL_WORDS

    assert DISPEL_WORDS == {"Yes": "any", "Strong Dispels Only": "strong only", "No": "none"}


# what killed you ----------------------------------------------------------------------------------------------
def _feed(store, t, alive=True, hp=1000, deaths=0, hero=None, items=None, pos=(3000, 2500), count=True):
    player = {"team_name": "radiant", "gold": 600, **({"deaths": deaths} if count else {})}
    return store.ingest({"map": {"matchid": "9", "clock_time": int(t) + 600,
                                 "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                         "player": player,
                         "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": alive, "health": hp if alive else 0,
                                  "max_health": 1000, "xpos": pos[0], "ypos": pos[1], **(hero or {})},
                         "items": items or {}}, received_t=t)


def test_aegis_is_not_a_death_but_a_real_one_is():
    store = GameStateStore()
    _feed(store, 1.0)
    _feed(store, 1.1, alive=False)  # Aegis: "alive" flips, the death count doesn't
    assert not store.just_died
    _feed(store, 6.0)
    _feed(store, 9.0, alive=False, deaths=1)
    assert store.just_died
    _feed(store, 9.1, alive=False, deaths=1)
    assert not store.just_died  # once per death


def test_a_death_counts_when_the_counter_arrives_an_update_apart():
    store = GameStateStore()
    _feed(store, 1.0)
    _feed(store, 1.1, alive=False)  # the flag first...
    assert not store.just_died
    _feed(store, 1.2, alive=False, deaths=1)  # ...then the count
    assert store.just_died
    other = GameStateStore()
    _feed(other, 1.0)
    _feed(other, 1.1, deaths=1)  # the count first...
    _feed(other, 1.2, alive=False, deaths=1)  # ...then the flag
    assert other.just_died
    nocount = GameStateStore()
    _feed(nocount, 1.0, count=False)
    _feed(nocount, 1.1, alive=False, count=False)
    assert nocount.just_died  # no counter in the feed: the flag is all there is


def test_the_death_summary_says_how_fast_what_was_on_you_and_what_you_never_pressed():
    store = GameStateStore()
    items = {"slot0": {"name": "item_black_king_bar", "cooldown": 0, "can_cast": True},
             "slot1": {"name": "item_magic_wand", "cooldown": 0, "can_cast": True},
             "slot2": {"name": "item_blade_mail", "cooldown": 15, "can_cast": False},
             "teleport0": {"name": "item_tpscroll", "cooldown": 0, "can_cast": True}}
    t = 100.0
    for hp in (1000, 1000, 1000):
        _feed(store, t, hp=hp, items=items)
        t += 0.5
    for hp in (800, 500, 300, 100):  # stunned through the burst
        _feed(store, t, hp=hp, items=items, hero={"stunned": True})
        t += 0.5
    _feed(store, t, alive=False, deaths=1, items=items)
    death = store.memory["last_death"]
    assert store.just_died and death["clock"] == int(t) + 600
    assert death["hp_before"] == "100% HP 2.5 s before dying"
    assert death["disabled_s"] == {"stunned": 2.0}
    assert death["ready_but_unused"] == ["black_king_bar"]  # (the wand and the TP don't count)
    assert death["where"] == "deep on the enemy's side" and death["tp_ready"] is True

    snap = store.current
    summary = prompts.state_summary(snap, store, None, [])
    assert summary["how_you_died"]["ready_but_unused"] == ["black_king_bar"]
