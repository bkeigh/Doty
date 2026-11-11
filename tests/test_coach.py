import json

from doty.advice import AdviceBus
from doty.coach import prompts
from doty.coach.llm import Coach, _strip_tags
from doty.config import CoachConfig
from doty.knowledge.constants import Knowledge
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN


def test_strip_tags_hides_full_and_partial_tags():
    assert _strip_tags("Farm bottom.\nBUILD: item_bfury, item_manta") == "Farm bottom."
    assert _strip_tags("Farm bottom.\nBUI") == "Farm bottom."
    assert _strip_tags("Farm bottom.\nBuy a Blink.") == "Farm bottom.\nBuy a Blink."
    assert _strip_tags("Buy BKB.\nLINEUP: radiant=Lion, Lina | dire=Axe") == "Buy BKB."
    assert _strip_tags("Buy BKB.\nLINE") == "Buy BKB."


def test_build_line_sets_custom_plan():
    kb = Knowledge()
    kb.items = {"item_bfury": {"cost": 3900}, "item_manta": {"cost": 4650}}
    store = GameStateStore()
    coach = Coach(CoachConfig(), store, AdviceBus(), kb)
    snap = store.ingest({"hero": {"name": "npc_dota_hero_juggernaut"}})
    coach._apply_build("Plan.\nBUILD: item_bfury, manta, item_not_real", snap)
    assert kb.custom_plan["npc_dota_hero_juggernaut"] == ["item_bfury", "item_manta"]


def test_state_summary_is_compact_json():
    m = ScriptedMatch()
    m.step(400)
    store = GameStateStore()
    snap = store.ingest(m.payload(TOKEN))
    summary = prompts.state_summary(snap, store, None, ["Power rune in 15s"])
    text = json.dumps(summary)
    assert summary["hero"].lower().startswith("juggernaut")
    assert summary["recent_alerts"] == ["Power rune in 15s"]
    assert len(text) < 2500  # keeps the per-request prompt small


def test_user_content_puts_image_first():
    content = prompts.user_content("now", {"clock": "1:00"}, 25, "QUJD")
    assert content[0]["type"] == "image" and content[-1]["type"] == "text"
    assert "25 words" in content[-1]["text"]


def test_request_uses_fallbacks_only_where_supported():
    store, bus = GameStateStore(), AdviceBus()
    opus = Coach(CoachConfig(model="claude-opus-5-5"), store, bus)._request([], "now")
    haiku = Coach(CoachConfig(model="claude-haiku-5-5"), store, bus)._request([], "now")
    assert opus["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in opus["betas"]
    assert "fallbacks" not in haiku and "betas" not in haiku
    assert opus["output_config"] == {"effort": "low"}
    assert opus["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_is_stable():
    # Cache prefix must not change between requests.
    import importlib

    first = prompts.SYSTEM_PROMPT
    importlib.reload(prompts)
    assert prompts.SYSTEM_PROMPT == first


def test_experience_level_changes_the_instruction():
    from doty.coach import prompts

    new = prompts.user_content("now", {}, 20, None, level="new")[-1]["text"]
    some = prompts.user_content("now", {}, 20, None, level="some")[-1]["text"]
    assert "new to Dota" in new and "DRAW" in new and "new to Dota" not in some
