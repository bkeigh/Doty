"""The coach's language, and this patch's changes in the coach's match brief."""

import asyncio

from doty.coach import prompts
from doty.knowledge.constants import Knowledge


def test_the_coach_writes_in_your_language_and_keeps_its_tags_in_english():
    content = prompts.user_content("watch", {"clock": "5:00"}, 14, None, language="es")
    note = content[-1]["text"]
    assert "Spanish" in note and "TRIGGER:" in note and "in English" in note
    assert "Spanish" not in prompts.user_content("watch", {}, 14, None)[-1]["text"]  # English: nothing added
    assert prompts.LANGUAGES["zh"][1] == "简体中文" and "ru" in prompts.LANGUAGES


def test_patch_notes_reach_the_brief(tmp_path):
    kb = Knowledge()
    kb.cache_dir = tmp_path
    kb.patch = "7.41"
    kb.heroes = {"npc_dota_hero_lion": {"id": 26, "localized_name": "Lion"},
                 "npc_dota_hero_juggernaut": {"id": 8, "localized_name": "Juggernaut"}}
    kb.ability_ids = {"5047": "lion_finger_of_death"}
    kb.ability_info = {"lion_finger_of_death": {"dname": "Finger of Death"}}
    feeds = {
        "patchnoteslist.json": {"patches": [{"patch_number": "7.40"}, {"patch_number": "7.41"},
                                            {"patch_number": "7.41f"}]},
        "patchnotes_7.41f.json": {"heroes": [{"hero_id": 26, "abilities": [{"ability_id": 5047, "ability_notes": [
            {"note": "Damage per kill decreased from 30 to 25"}, {"note": "<br>"}]}]}]},
        "patchnotes_7.41.json": {"heroes": [{"hero_id": 8, "hero_notes": [{"note": "Base Armor increased by 1"}]}]},
    }

    async def cached(name, url, ttl=0):
        return feeds.get(name)

    kb._cached_json = cached
    asyncio.run(kb.ensure_patch_notes())
    assert kb.hero_patch_notes(26) == ["7.41f: Finger of Death: Damage per kill decreased from 30 to 25"]
    brief = prompts.match_brief({"radiant": ["Juggernaut"], "dire": ["Lion"]}, "radiant",
                                "npc_dota_hero_juggernaut", kb)
    assert "Changed lately: 7.41f: Finger of Death" in brief
    assert "Your hero (Juggernaut) changed lately: 7.41: Base Armor increased by 1" in brief
