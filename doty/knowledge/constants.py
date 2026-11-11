"""Hero/item/ability data (odota/dotaconstants, MIT) and per-hero stats (OpenDota API).

What the coach knows about your hero comes from here, so it follows each patch instead of the AI's
memory of older ones:
- **Abilities:** what each of your hero's abilities does (dotaconstants), so the coach understands how
  they work together.
- **Skill order:** the order pro players level this hero's abilities on the current patch, from
  OpenDota's public match explorer (one query per hero, cached for a week). Doty also uses it to tell
  which abilities take skill points, so innate and Shard abilities don't throw off the count.
- **Items:** what this hero's players buy at each stage (OpenDota itemPopularity), so suggestions stay
  within builds that work for the hero.

Everything is fetched lazily, cached on disk, and optional: if the network is down Doty still runs,
it just loses item names/costs and build plans.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp

from ..config import CACHE_DIR
from . import curves
from . import mode as game_mode

CONSTANTS_BASE = "https://raw.githubusercontent.com/odota/dotaconstants/master/build"
OPENDOTA = "https://api.opendota.com/api"
CACHE_TTL_S = 24 * 3600
# OpenDota now and then answers 500 or 429 (seen in real logs): it's asked again after these waits.
RETRY_DELAYS_S = (1.0, 3.0)
REQUESTS_PER_MIN = 50  # OpenDota's free limit is 60 a minute without a key: Doty stays under it
# Valve's own patch notes, as data: what changed for each hero, ability and item in each patch.
PATCH_FEED = "https://www.dota2.com/datafeed"
PATCH_NOTES_TTL_S = 7 * 24 * 3600  # (a patch's notes don't change; the list of patches is checked daily)
PATCH_NOTES_KEPT = 4  # the latest letter patches of the current patch (7.41c, d, e, f)
# dotaconstants' "dispellable", as the match brief puts it.
DISPEL_WORDS = {"Yes": "any", "Strong Dispels Only": "strong only", "No": "none"}
SKILLS_TTL_S = 7 * 24 * 3600
# The most common ability taken at each upgrade, in pro matches on this patch.
SKILL_SQL = ("SELECT u.lvl, u.ab, count(*) n FROM player_matches pm JOIN match_patch mp USING (match_id) "
             "CROSS JOIN LATERAL unnest(pm.ability_upgrades_arr) WITH ORDINALITY u(ab,lvl) "
             "WHERE pm.hero_id={hero_id} AND mp.patch='{patch}' AND u.lvl <= 25 GROUP BY 1,2 ORDER BY 1,3 DESC")
# Items worth buying against particular enemies, whatever the hero's usual build (see the coach prompt).
COUNTER_ITEMS = {
    "black_king_bar", "monkey_king_bar", "bloodthorn", "orchid", "spirit_vessel", "urn_of_shadows",
    "shivas_guard", "skadi", "silver_edge", "dust", "ward_sentry", "gem", "sphere", "lotus_orb", "force_staff",
    "sheepstick", "abyssal_blade", "nullifier", "mjollnir", "maelstrom", "bfury", "radiance", "assault",
    "crimson_guard", "ghost", "pipe", "eternal_shroud", "glimmer_cape", "aeon_disk", "cyclone", "blade_mail",
    "heavens_halberd", "solar_crest", "pavise", "hurricane_pike", "manta", "diffusal_blade", "disperser",
    "satanic", "butterfly", "wind_waker", "guardian_greaves", "mekansm", "holy_locket", "smoke_of_deceit",
    "ward_observer", "tpscroll", "travel_boots", "infused_raindrop", "magic_wand", "cloak", "hood_of_defiance",
}
ITEM_ALIASES = {  # how players (and the AI) shorten item names
    "BKB": "black_king_bar", "Blink": "blink", "Aghs": "ultimate_scepter", "Aghanim's": "ultimate_scepter",
    "Shard": "aghanims_shard", "Drums": "ancient_janggo", "Euls": "cyclone", "Eul's": "cyclone", "MKB": "monkey_king_bar",
    "Mjolnir": "mjollnir", "Basher": "basher", "Abyssal": "abyssal_blade", "Halberd": "heavens_halberd",
    "Pike": "hurricane_pike", "Vyse": "sheepstick", "Hex": "sheepstick", "Shiva's": "shivas_guard",
    "Skadi": "skadi", "Linken's": "sphere", "Lotus": "lotus_orb", "Battle Fury": "bfury", "Treads": "power_treads",
}

# Phases of OpenDota's itemPopularity, and when (game seconds) each becomes the plan.
PHASES = [("start_game_items", -90), ("early_game_items", 0), ("mid_game_items", 900), ("late_game_items", 1800)]
# Consumables and components we never want to "remind" someone to buy.
NOT_BUILD_TARGETS = {
    "tango", "flask", "clarity", "enchanted_mango", "faerie_fire", "ward_observer", "ward_sentry",
    "smoke_of_deceit", "dust", "tpscroll", "branches", "blood_grenade", "circlet", "gauntlets",
    "slippers", "mantle", "ward_dispenser", "quelling_blade", "magic_stick", "boots",
}
# Big buys with no components that are still worth a reminder.
STANDALONE_TARGETS = {"blink", "aghanims_shard"}


HERO_ALIASES = {  # normalized nickname -> npc name, for names read off the screen or said out loud
    "am": "npc_dota_hero_antimage", "np": "npc_dota_hero_furion", "prophet": "npc_dota_hero_furion",
    "sf": "npc_dota_hero_nevermore", "od": "npc_dota_hero_obsidian_destroyer", "wk": "npc_dota_hero_skeleton_king",
    "pa": "npc_dota_hero_phantom_assassin", "pl": "npc_dota_hero_phantom_lancer", "qop": "npc_dota_hero_queenofpain",
    "cm": "npc_dota_hero_crystal_maiden", "es": "npc_dota_hero_earthshaker", "ss": "npc_dota_hero_shadow_shaman",
    "wr": "npc_dota_hero_windrunner", "ta": "npc_dota_hero_templar_assassin", "ck": "npc_dota_hero_chaos_knight",
    "dk": "npc_dota_hero_dragon_knight", "bh": "npc_dota_hero_bounty_hunter", "bb": "npc_dota_hero_bristleback",
    "lc": "npc_dota_hero_legion_commander", "ns": "npc_dota_hero_night_stalker", "sk": "npc_dota_hero_sand_king",
    "wd": "npc_dota_hero_witch_doctor", "ds": "npc_dota_hero_dark_seer", "ld": "npc_dota_hero_lone_druid",
    "tb": "npc_dota_hero_terrorblade", "void": "npc_dota_hero_faceless_void",
    "naix": "npc_dota_hero_life_stealer", "lifestealer": "npc_dota_hero_life_stealer",
    "underlord": "npc_dota_hero_abyssal_underlord", "timbersaw": "npc_dota_hero_shredder",
    "clockwerk": "npc_dota_hero_rattletrap", "io": "npc_dota_hero_wisp", "magnus": "npc_dota_hero_magnataur",
    "zeus": "npc_dota_hero_zuus", "treant": "npc_dota_hero_treant", "wraithking": "npc_dota_hero_skeleton_king",
    "outworlddevourer": "npc_dota_hero_obsidian_destroyer", "doom": "npc_dota_hero_doom_bringer",
    "centaur": "npc_dota_hero_centaur", "necro": "npc_dota_hero_necrolyte", "necrophos": "npc_dota_hero_necrolyte",
    "vengefulspirit": "npc_dota_hero_vengefulspirit", "venge": "npc_dota_hero_vengefulspirit",
    "windranger": "npc_dota_hero_windrunner", "shadowfiend": "npc_dota_hero_nevermore",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _last_number(value: Any) -> float:
    """dotaconstants' per-level values ("120", ["120", "110", "100"]) -> the max-level one (0 if none)."""
    if isinstance(value, list):
        value = value[-1] if value else ""
    try:
        return float(str(value).split("/")[-1].strip() or 0)
    except ValueError:
        return 0.0


# An enemy's item that changes how you fight them: these, or any of their usual items this expensive.
SPIKE_ITEMS = {"item_black_king_bar", "item_blink", "item_ultimate_scepter", "item_bfury", "item_radiance",
               "item_manta", "item_desolator", "item_skadi", "item_butterfly", "item_satanic", "item_mjollnir",
               "item_heart", "item_assault", "item_refresher", "item_sheepstick"}
SPIKE_MIN_COST = 3800
ENEMY_ITEMS = 3  # an enemy's usual items Doty fetches timings for
# A part of a bigger item (Yasha in Manta Style) drops out of a hero's usual items when the bigger item is bought
# at least this often compared with the part: then the bigger one is what its players are building.
FOLD_SHARE = 0.4
PART_MIN_COST = 400  # a part cheaper than this isn't worth a "buy this now" (branches, a recipe scroll)
# The build plan is this phase's most-bought items after the phase before's, so a player who is behind is
# still pointed at the core they skipped (Battle Fury before Swift Blink). Of the phase before, only items worth
# buying late carry over: this dear, or boots.
PLAN_PER_PHASE = 4
CARRY_OVER_MIN_COST = 1500
# Telling the player to buy something ("Buy X", "finish your X", "save for X")...
BUY_VERB = re.compile(r"\b(buy|get|grab|purchase|pick up|rush|build|finish|complete|afford|save for|go for|"
                      r"work towards?)\b", re.IGNORECASE)
# ...unless one of these stands between the buy word and the item: then the item is the setting, or something
# to press, not the thing to buy ("Buy Manta Style after Battle Fury", "Get out and use Black King Bar").
NOT_THE_OBJECT = re.compile(r"\b(after|once|when|with|before|until|because|since|if|than|instead of|on|from|into|"
                            r"value|use|using|pop|press|activate|cast|toggle)\b", re.IGNORECASE)
# ...or the item is what something else is made from ("Build your Yasha into Manta Style").
MADE_INTO = re.compile(r"\s+(into|to make|to finish)\b", re.IGNORECASE)
BUY_REACH = 4  # words between the buy word and the item ("buy Yasha and then" Manta Style)


def _name_pattern(names) -> "re.Pattern | None":
    """One regex for whole-word matches of any of `names`, longest first."""
    names = sorted({n for n in names if n}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(rf"(?<![A-Za-z'])({'|'.join(re.escape(n) for n in names)})(?![A-Za-z])")


class Knowledge:
    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self.heroes: dict[str, dict[str, Any]] = {}  # by npc name
        self.items: dict[str, dict[str, Any]] = {}  # by "item_x" name
        self.item_popularity: dict[int, dict[str, Any]] = {}
        self.benchmarks: dict[int, dict[str, Any]] = {}
        self.matchups: dict[int, list[dict[str, Any]]] = {}  # hero id -> results vs every other hero
        self.custom_plan: dict[str, list[str]] = {}  # hero npc name -> item names, set by coach
        self.hero_abilities: dict[str, dict[str, Any]] = {}  # npc name -> {abilities, talents, facets}
        self.ability_info: dict[str, dict[str, Any]] = {}  # ability name -> {dname, desc, innate}
        self.ability_ids: dict[str, str] = {}  # "5003" -> "antimage_mana_break"
        self.patch = ""  # e.g. "7.41"
        # The match being played: a normal game or Turbo (knowledge/mode.py; the app sets it as it finds out).
        # Timings learned from normal games (when an item usually lands, the phases of a build) are given on
        # this game's clock.
        self.mode = game_mode.NORMAL
        self.patch_notes: dict[int, list[str]] = {}  # hero id -> this patch's changes, newest first
        self._patch_notes_asked = False
        self.skill_rows: dict[int, list[dict[str, Any]]] = {}  # hero id -> explorer rows (lvl, ab, n)
        self.item_timings: dict[tuple[int, str], list[dict[str, Any]]] = {}  # (hero id, item) -> time buckets
        self._hero_index: dict[str, str] = {}
        self._mention_re = None
        self._mention_names: dict[str, str] = {}
        self._item_re = None
        self._item_names: dict[str, str] = {}
        self._item_mask = None  # item names, hidden before looking for heroes ("Monkey King Bar")
        self._ability_mask = None  # ability names, hidden before looking for items ("Blink Strike")
        self._own_masks: dict[str, Any] = {}  # npc -> its own abilities ("Blink" for Anti-Mage)
        self._loading = False  # load() is running: hero data waits for the patch and item names
        self._session: aiohttp.ClientSession | None = None
        self._pending: set[int] = set()  # hero ids being fetched (an enemy's lighter fetch: -id)
        self._attempted: dict[int, float] = {}
        self._full: set[int] = set()  # heroes ensure_hero has fetched everything for
        self._by_id: dict[int, str] = {}
        self._by_id_key: tuple = ()
        self._spikes: dict[int, tuple[tuple, list]] = {}
        self._typical: dict[int, tuple[tuple, list[str]]] = {}  # hero id -> (what it was built from, usual items)
        self._plans: dict[tuple[int, int], tuple[tuple, list[str]]] = {}  # (hero id, phase) -> (same, the plan)
        self._enemy_done: set[int] = set()  # (-id) enemies whose spike timings are in
        self.durations: dict[int, Any] = {}  # hero id -> OpenDota win rate by game length (5-minute bins)
        self._durations_asked: dict[int, float] = {}
        self._refreshing: set[str] = set()  # cache files being downloaded again in the background
        self._sent: list[float] = []  # when the last minute's requests went out (pacing)
        self._pace_lock: tuple[Any, asyncio.Lock] | None = None  # (its event loop, the lock)
        self.fetch_failures = 0  # downloads that failed after their retries (for the app's status)

    # loading ---------------------------------------------------------------
    async def load(self) -> None:
        self._loading = True
        try:
            await self._load()
        finally:
            self._loading = False
            self._mention_re = self._item_re = self._item_mask = self._ability_mask = None
            self._own_masks.clear()
            self._hero_index.clear()

    async def _load(self) -> None:
        heroes, items, hero_abilities, abilities, ability_ids, patches = (
            None if isinstance(r, Exception) else r for r in await asyncio.gather(*(
                self._cached_json(f"{name}.json", f"{CONSTANTS_BASE}/{name}.json")
                for name in ("heroes", "items", "hero_abilities", "abilities", "ability_ids", "patch")),
                return_exceptions=True))  # (one file failing mustn't lose the other five)
        if isinstance(heroes, dict):
            self.heroes = {h["name"]: h for h in heroes.values() if isinstance(h, dict) and "name" in h}
        if isinstance(items, dict):
            self.items = {f"item_{k}": v for k, v in items.items() if isinstance(v, dict)}
        if isinstance(hero_abilities, dict):
            self.hero_abilities = {k: v for k, v in hero_abilities.items() if isinstance(v, dict)}
        if isinstance(abilities, dict):  # keep only what the coach reads (the file is ~2 MB)
            wanted = {a for h in self.hero_abilities.values() for a in h.get("abilities", [])}
            self.ability_info = {
                name: {"dname": str(v.get("dname") or ""), "desc": str(v.get("desc") or ""),
                       "innate": bool(v.get("is_innate")),
                       "hidden": "Hidden" in (v.get("behavior") if isinstance(v.get("behavior"), list)
                                              else [v.get("behavior")]),
                       "cd": _last_number(v.get("cd")), "bkb": str(v.get("bkbpierce") or "") == "Yes",
                       "dispel": DISPEL_WORDS.get(str(v.get("dispellable") or ""), ""),
                       "passive": "Passive" in (v.get("behavior") if isinstance(v.get("behavior"), list)
                                                else [v.get("behavior")])}
                for name, v in abilities.items() if isinstance(v, dict) and (name in wanted or not wanted)}
        if isinstance(ability_ids, dict):
            self.ability_ids = {str(k): str(v) for k, v in ability_ids.items()}
        if isinstance(patches, list) and patches and isinstance(patches[-1], dict):
            self.patch = str(patches[-1].get("name") or "")

    async def ensure_patch_notes(self) -> None:
        """What changed for each hero in the latest patches of the current one (Valve's patch notes), once a
        session. Only the coach reads it: a hero changed last week is easy to misjudge from older habits."""
        if self._patch_notes_asked or self._loading:
            return
        self._patch_notes_asked = True
        listing = await self._cached_json("patchnoteslist.json", f"{PATCH_FEED}/patchnoteslist?language=english")
        patches = listing.get("patches") if isinstance(listing, dict) else None
        if not isinstance(patches, list):
            self._patch_notes_asked = False  # (try again later)
            return
        names = [str(p.get("patch_number") or "") for p in patches if isinstance(p, dict)]
        major = (self.patch or (names[-1] if names else "")).rstrip("abcdefghijklmnopqrstuvwxyz")
        versions = [n for n in names if n.rstrip("abcdefghijklmnopqrstuvwxyz") == major][-PATCH_NOTES_KEPT:]
        notes: dict[int, list[str]] = {}
        for version in reversed(versions):  # newest first
            data = await self._cached_json(f"patchnotes_{version}.json",
                                           f"{PATCH_FEED}/patchnotes?version={version}&language=english",
                                           ttl=PATCH_NOTES_TTL_S)
            if not isinstance(data, dict):
                continue
            for hero in data.get("heroes") or []:
                hid = int(hero.get("hero_id") or 0)
                if hid:
                    notes.setdefault(hid, []).extend(f"{version}: {line}" for line in self._hero_changes(hero))
        if notes:
            self.patch_notes = notes

    def _hero_changes(self, hero: dict) -> list[str]:
        """One hero's entry in a patch's notes as plain lines ("Blade Fury: Cooldown decreased from 34 to 30")."""
        def clean(n: dict) -> str:
            return re.sub(r"<[^>]+>", "", str(n.get("note") or "")).strip()

        out = [clean(n) for n in hero.get("hero_notes") or []]
        for ab in hero.get("abilities") or []:
            name = self.ability_ids.get(str(ab.get("ability_id")), "")
            label = (self.ability_info.get(name) or {}).get("dname") or name.replace("_", " ").title()
            out += [f"{label}: {clean(n)}" if label else clean(n) for n in ab.get("ability_notes") or []]
        out += [f"Talent: {clean(n)}" for n in hero.get("talent_notes") or []]
        return [line for line in out if line and not line.endswith(": ") and line != "Talent:"]

    def hero_patch_notes(self, hero_id: int, n: int = 4) -> list[str]:
        return (self.patch_notes.get(int(hero_id or 0)) or [])[:n]

    def has_hero_data(self, hero_id: int) -> bool:
        """ensure_hero has run for this hero (an enemy's lighter fetch doesn't count)."""
        return hero_id in self._full

    async def ensure_hero(self, hero_id: int) -> None:
        """Fetch build popularity, benchmarks, matchups and the skill order for a hero, once, in the
        background."""
        if not hero_id or hero_id in self._full or hero_id in self._pending or self._loading:
            return  # (while load() runs, the patch and item names aren't known yet: ask again next update)
        if time.monotonic() - self._attempted.get(hero_id, -1e9) < 300:
            return  # failed recently (offline?); don't hammer OpenDota at 10 Hz
        self._attempted[hero_id] = time.monotonic()
        self._pending.add(hero_id)
        self._full.add(hero_id)
        try:
            complete = await self._ensure_hero(hero_id)
        except BaseException:
            self._full.discard(hero_id)
            raise
        finally:
            self._pending.discard(hero_id)
        if not complete:
            self._full.discard(hero_id)  # some of it didn't come back (offline, OpenDota busy): again after the back-off

    async def ensure_enemy(self, hero_id: int) -> None:
        """An enemy hero's usual items and when they usually finish the big ones (their power spikes),
        for the match brief and the heads-up before them. Lighter than ensure_hero: four requests."""
        key = -hero_id  # its own entry in the back-off and in-flight sets
        if not hero_id or self._loading or key in self._pending or key in self._enemy_done:
            return
        if time.monotonic() - self._attempted.get(key, -1e9) < 300:
            return
        self._attempted[key] = time.monotonic()
        self._pending.add(key)
        try:
            if hero_id not in self.item_popularity:
                pop = await self._cached_json(f"itempop_{hero_id}.json", f"{OPENDOTA}/heroes/{hero_id}/itemPopularity")
                if not isinstance(pop, dict):
                    return
                self.item_popularity[hero_id] = pop
            wanted = [i for i in self.spike_items(hero_id) if (hero_id, i) not in self.item_timings]
            if await self._fetch_timings(hero_id, wanted):
                self._enemy_done.add(key)  # (else asked again after the back-off)
        finally:
            self._pending.discard(key)

    async def ensure_durations(self, hero_id: int) -> None:
        """This hero's public win rate by game length (for the game plan: who the clock favours), once."""
        if not hero_id or hero_id in self.durations:
            return
        if time.monotonic() - self._durations_asked.get(hero_id, -1e9) < 300:
            return  # in flight, or failed recently
        self._durations_asked[hero_id] = time.monotonic()
        rows = await self._cached_json(f"durations_{hero_id}.json", f"{OPENDOTA}/heroes/{int(hero_id)}/durations")
        if isinstance(rows, list) and rows:
            self.durations[hero_id] = rows

    def game_plan(self, allies: list[str], enemies: list[str]) -> "curves.Plan | None":
        """Who the clock favours, from the heroes on each side (npc names); None until there's data."""
        def side(names: list[str]) -> list:
            ids = [int((self.heroes.get(n) or {}).get("id") or 0) for n in names]
            return [curves.curve(self.durations.get(i)) for i in ids if i]
        return curves.plan(side(allies), side(enemies))

    async def _fetch_timings(self, hero_id: int, items: list[str]) -> bool:
        """When this hero's players finish each item. True if every one came back."""
        rows_by_item = await asyncio.gather(*(self._cached_json(
            f"timing_{hero_id}_{i.removeprefix('item_')}.json",
            f"{OPENDOTA}/scenarios/itemTimings?item={i.removeprefix('item_')}&hero_id={int(hero_id)}")
            for i in items))
        complete = True
        for item, rows in zip(items, rows_by_item):
            if isinstance(rows, list):
                self.item_timings[(hero_id, item)] = [r for r in rows if int(r.get("hero_id") or 0) == hero_id]
            else:
                complete = False
        return complete

    def spike_items(self, hero_id: int) -> list[str]:
        """The items that change how a hero fights (Black King Bar, Blink, Battle Fury...), among those
        its players usually buy, most popular first."""
        return [i for i in self.typical_items(hero_id, 8)
                if i in SPIKE_ITEMS or self.item_cost(i) >= SPIKE_MIN_COST][:ENEMY_ITEMS]

    def spikes(self, hero_id: int) -> list[tuple[int, str]]:
        """(game second by which half of this hero's players have it, item) for its spike items, soonest
        first. (Asked on every update: kept until more data arrives.)"""
        key = (len(self.item_popularity), len(self.item_timings), len(self.items), self.mode.pace)
        hit = self._spikes.get(hero_id)
        if hit is not None and hit[0] == key:
            return hit[1]
        out = sorted((t, i) for i in self.spike_items(hero_id) if (t := self.typical_time(hero_id, i)) is not None)
        self._spikes[hero_id] = (key, out)
        return out

    def enemy_notes(self, hero_npc: str) -> dict[str, Any]:
        """What an enemy hero does and buys, briefly: each ability with its cooldown (and whether it goes
        through Black King Bar), and when its players usually finish their big items."""
        hero = self.heroes.get(hero_npc) or {}
        abilities, seen = [], set()
        for name in (self.hero_abilities.get(hero_npc) or {}).get("abilities", []):
            info = self.ability_info.get(name)
            if name == "generic_hidden" or not info or not info.get("dname") or info.get("hidden"):
                continue
            if info["dname"] in seen:  # Shadow Fiend's three Shadowrazes are one entry
                continue
            seen.add(info["dname"])
            desc = re.sub(r"\s+", " ", info.get("desc", "")).strip()
            desc = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0]  # the first sentence says what it does
            if len(desc) > 130:
                desc = desc[:130].rsplit(" ", 1)[0] + "…"
            line = f"{info['dname']}: {desc}"
            extra = [f"{info['cd']:g} s cooldown"] if info.get("cd") else []
            if info.get("bkb"):
                extra.append("goes through BKB")
            if info.get("passive"):
                extra.append("passive")
            if info.get("dispel"):
                extra.append(f"dispel: {info['dispel']}")
            abilities.append(line + (f" ({', '.join(extra)})" if extra else ""))
        out: dict[str, Any] = {"hero": self.hero_name(hero_npc)}
        if hero.get("roles"):
            out["roles"] = ", ".join(hero["roles"][:3])
        if abilities:
            out["abilities"] = abilities
        hid = int(hero.get("id") or 0)
        timed = self.spikes(hid) if hid else []
        if timed:
            out["usual_big_items"] = [f"{self.item_name(i)} by ~{t // 60}:{t % 60:02d}" for t, i in timed]
        return out

    async def _ensure_hero(self, hero_id: int) -> bool:
        """True when every part came back."""
        jobs = [
            self._cached_json(f"itempop_{hero_id}.json", f"{OPENDOTA}/heroes/{hero_id}/itemPopularity"),
            self._cached_json(f"bench_{hero_id}.json", f"{OPENDOTA}/benchmarks?hero_id={hero_id}"),
            self._cached_json(f"matchups_{hero_id}.json", f"{OPENDOTA}/heroes/{hero_id}/matchups"),
        ]
        if self.patch and re.fullmatch(r"\d+\.\d+[a-z]?", self.patch):
            sql = urllib.parse.quote(SKILL_SQL.format(hero_id=int(hero_id), patch=self.patch))
            jobs.append(self._cached_json(f"skills_{hero_id}_{self.patch}.json", f"{OPENDOTA}/explorer?sql={sql}",
                                          ttl=SKILLS_TTL_S))
        pop, bench, vs, *skills = await asyncio.gather(*jobs)
        if isinstance(pop, dict):
            self.item_popularity[hero_id] = pop
        if isinstance(bench, dict):
            self.benchmarks[hero_id] = bench
        if isinstance(vs, list):
            self.matchups[hero_id] = vs
        if skills and isinstance(skills[0], dict) and skills[0].get("rows"):
            self.skill_rows[hero_id] = skills[0]["rows"]
        elif skills:  # an explorer error or an empty answer: don't keep it for a week
            (self.cache_dir / f"skills_{hero_id}_{self.patch}.json").unlink(missing_ok=True)
        # When players of this hero usually finish its core items (for pace reminders and the review).
        timed = await self._fetch_timings(hero_id, self.typical_items(hero_id, 4))
        return (isinstance(pop, dict) and isinstance(bench, dict) and isinstance(vs, list) and timed
                and (not skills or hero_id in self.skill_rows))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _cached_json(self, name: str, url: str, ttl: float = CACHE_TTL_S) -> Any:
        """The answer from the cache folder while it's fresh. Once it's old, the saved copy is used at once and a
        new one downloads in the background, so nothing in a match waits on OpenDota or goes blank when it's
        busy. Only with no saved copy at all does the caller wait for the download."""
        path = self.cache_dir / name
        saved = None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - path.stat().st_mtime < ttl:
                return saved
        except (OSError, ValueError):
            saved = None
        if saved is not None:
            self._refresh_later(name, url)
            return saved
        return await self._download(name, url)

    def _refresh_later(self, name: str, url: str) -> None:
        if name in self._refreshing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refreshing.add(name)

        async def refresh() -> None:
            try:
                await self._download(name, url)
            finally:
                self._refreshing.discard(name)
        loop.create_task(refresh())

    async def _pace(self) -> None:
        """Keep under OpenDota's free limit: at most REQUESTS_PER_MIN in any minute."""
        loop = asyncio.get_running_loop()
        if self._pace_lock is None or self._pace_lock[0] is not loop:
            self._pace_lock = (loop, asyncio.Lock())
        async with self._pace_lock[1]:
            now = time.monotonic()
            self._sent = [t for t in self._sent if now - t < 60.0]
            if len(self._sent) >= REQUESTS_PER_MIN:
                await asyncio.sleep(60.0 - (now - self._sent[0]))
            self._sent.append(time.monotonic())

    async def _download(self, name: str, url: str) -> Any:
        """Fetch and save one answer, asking again when OpenDota is busy (HTTP 5xx or 429, a timeout).
        None if it never came."""
        why = ""
        for delay in (0.0, *RETRY_DELAYS_S):
            if delay:
                await asyncio.sleep(delay)
            await self._pace()
            try:
                if self._session is None:
                    self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
                async with self._session.get(url) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        why = f"HTTP {resp.status}"
                        continue  # busy: try again
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except aiohttp.ClientResponseError as exc:  # (a 4xx: asking again won't change it)
                why = f"HTTP {exc.status}"
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                why = exc.__class__.__name__
                continue
            if data in ([], {}, None):
                return data  # (an empty answer is often OpenDota having a bad moment: not kept for a day)
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                part = self.cache_dir / (name + ".part")  # never leave a half-written file behind
                part.write_text(json.dumps(data), encoding="utf-8")
                part.replace(self.cache_dir / name)
            except OSError as exc:  # (the file is open elsewhere, the disk is full): use it all the same
                print(f"[doty] couldn't save {name} to the cache ({exc.__class__.__name__})")
            return data
        self.fetch_failures += 1
        print(f"[doty] couldn't fetch {url} ({why}); trying again later")
        return None

    # lookups -----------------------------------------------------------------
    def hero_name(self, npc_name: str) -> str:
        h = self.heroes.get(npc_name)
        if h:
            return h.get("localized_name", npc_name)
        return npc_name.removeprefix("npc_dota_hero_").replace("_", " ").title()

    def item_name(self, item: str) -> str:
        it = self.items.get(item)
        if it and it.get("dname"):
            return it["dname"]
        return item.removeprefix("item_").replace("_", " ").title()

    def item_cost(self, item: str) -> int:
        it = self.items.get(item)
        return int(it.get("cost") or 0) if it else 0

    def _id_to_item(self) -> dict[int, str]:
        key = (id(self.items), len(self.items))  # (rules ask on every update: built once per item list)
        if self._by_id_key != key:
            self._by_id = {int(v["id"]): k for k, v in self.items.items() if "id" in v}
            self._by_id_key = key
        return self._by_id

    def build_plan(self, hero_npc: str, clock: int) -> list[str]:
        """Ordered item names to aim for at this point in the game: the coach's own suggestion for this match
        when it made one, else what the hero's players buy most in this phase of the game, after the big items
        of the phase before (so a player who is behind is pointed at the core they skipped, not at late-game
        items on top of nothing). A stepping stone (Perseverance) stands in the plan as the item its players
        build it into (Battle Fury), like `typical_items`. In Turbo the phases come twice as early. Callers
        leave out what the player owns. (Rules ask on every update: kept until the data changes.)"""
        if hero_npc in self.custom_plan:
            return self.custom_plan[hero_npc]
        hero = self.heroes.get(hero_npc)
        hero_id = int(hero["id"]) if hero else 0
        pop = self.item_popularity.get(hero_id)
        if not pop:
            return []
        now = self.mode.as_normal(clock)
        phase = max(i for i, (_name, start) in enumerate(PHASES) if i == 0 or now >= start)
        whole = self.typical_items(hero_id, 99)
        key = self._typical[hero_id][0]  # (what typical_items was built from: the same data as the plan)
        hit = self._plans.get((hero_id, phase))
        if hit is not None and hit[0] == key:
            return hit[1]
        by_id = self._id_to_item()

        def most_bought(name: str) -> list[str]:
            found: list[str] = []
            for item_id, _count in sorted((pop.get(name) or {}).items(), key=lambda kv: -kv[1]):
                item = by_id.get(int(item_id))
                if not item or not self._is_build_target(item):
                    continue
                if whole and item not in whole:
                    item = next((big for big in whole if item in self.owned_with_parts({big})), item)
                if item not in found:
                    found.append(item)
            return found[:PLAN_PER_PHASE]

        if phase == 0:  # before the horn: the starting items are mostly small ones, so the early game's follow
            plan = most_bought(PHASES[0][0])
            plan += [i for i in most_bought(PHASES[1][0]) if i not in plan]
        else:
            plan = [i for i in most_bought(PHASES[phase - 1][0])
                    if self.item_cost(i) >= CARRY_OVER_MIN_COST or self._boots_upgrade(i)]
            plan += [i for i in most_bought(PHASES[phase][0]) if i not in plan]
        self._plans[(hero_id, phase)] = (key, plan)
        return plan

    def shopping(self, hero_npc: str, clock: int, owned, gold: int, n: int = 3) -> list[dict[str, Any]]:
        """What to buy, in order: the one answer every part of Doty gives (the rules' "Buy ..." calls, the
        overlay's strip, the app's Buy Next panel, the coach's state). First the item you're building now,
        then what comes after it in the plan, then the hero's usual items. `owned` is every item you hold
        (with repeats), `gold` what's free to spend.

        Each entry is {"item", "name", "cost", "left"} (`left`: gold still to pay, counting the parts you
        hold). The first also has "short" (what your gold doesn't cover yet), "buy" (the item, or the part of
        it your gold covers now; None while you save), "buy_cost" and "finish". Gold goes to one thing at a
        time, so only the first can ever be "buy now"."""
        target = self.next_target(hero_npc, clock, owned)
        if target is None:
            return []
        names = set(owned)
        have = self.owned_with_parts(names)
        hero = self.heroes.get(hero_npc)
        later = self.build_plan(hero_npc, clock) + (self.typical_items(int(hero["id"]), 8) if hero else [])
        boots = self._boots_upgrade(target) or any(  # (Travel Boots count as having a pair)
            i != "item_boots" and "boots" in ((self.items.get(i) or {}).get("components") or []) for i in have)
        out: list[dict[str, Any]] = []
        for item in dict.fromkeys([target] + later):
            if len(out) >= n:
                break
            if item != target:
                if item in have or self.cost_left(item, owned) <= 0:
                    continue
                if self._boots_upgrade(item):
                    if boots:
                        continue  # (one pair of boots is enough)
                    boots = True
            out.append({"item": item, "name": self.item_name(item), "cost": self.item_cost(item),
                        "left": self.cost_left(item, owned)})
        first = out[0]
        buy = self.next_buy(target, owned, gold)
        first.update(short=max(0, first["left"] - gold), buy=buy["buy"] if buy else None,
                     buy_cost=buy["cost"] if buy else 0, finish=bool(buy and buy["finish"]))
        return out

    def _boots_upgrade(self, item: str) -> bool:
        """A pair of boots made from Boots of Speed (Travel Boots aside: they go on top of another pair)."""
        return (item != "item_boots" and not item.startswith("item_travel_boots")
                and "boots" in ((self.items.get(item) or {}).get("components") or []))

    def _is_build_target(self, item: str) -> bool:
        """Finished items only: popularity lists include components (Broadsword
        for Battle Fury), which make bad "you can afford" reminders."""
        short = item.removeprefix("item_")
        data = self.items.get(item) or {}
        if short in NOT_BUILD_TARGETS or int(data.get("cost") or 0) < 500:
            return False
        return bool(data.get("components")) or short in STANDALONE_TARGETS

    def benchmark_median(self, hero_id: int, metric: str = "last_hits_per_min") -> float | None:
        """Median full-match value of an OpenDota benchmark metric for this hero."""
        rows = (self.benchmarks.get(hero_id) or {}).get("result", {}).get(metric)
        if not rows:
            return None
        return min(rows, key=lambda r: abs(r.get("percentile", 0) - 0.5)).get("value")

    def find_hero(self, name: str) -> str | None:
        """npc name for a hero name as a person (or a model reading the screen) writes it:
        'Anti-Mage', "Nature's Prophet", 'shadow fiend', 'OD'... None if unknown."""
        if not self._hero_index and self.heroes:
            for npc, h in self.heroes.items():
                self._hero_index[_norm(h.get("localized_name", ""))] = npc
                self._hero_index[_norm(npc.removeprefix("npc_dota_hero_"))] = npc
            for alias, npc in HERO_ALIASES.items():
                if npc in self.heroes:
                    self._hero_index[alias] = npc
        return self._hero_index.get(_norm(name))

    def hero_mentions(self, text: str) -> set[str]:
        """npc names of the heroes a piece of text names ("Back off, Lion and PA are missing" -> Lion, PA).
        Names match as capitalized words ('Tiny', not 'tiny'), and short nicknames only in capitals ('PA'),
        so ordinary words don't count as heroes."""
        if not text or not self.heroes:
            return set()
        text = self._masked(text, "item")  # "Monkey King Bar", "Ogre Axe" and "Void Stone" aren't heroes
        if self._mention_re is None:
            import re

            names: dict[str, str] = {}
            for npc, h in self.heroes.items():
                loc = str(h.get("localized_name") or "")
                if loc:
                    names[loc] = npc
            for alias, npc in HERO_ALIASES.items():
                if npc in self.heroes:
                    names[alias.upper() if len(alias) <= 3 else alias.title()] = npc
            self._mention_names = names
            pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            self._mention_re = re.compile(rf"(?<![A-Za-z'])({pattern})(?![A-Za-z])")
        return {self._mention_names[m] for m in self._mention_re.findall(text)}

    def matchups_vs(self, hero_id: int, enemy_names: list[str], min_games: int = 20) -> list[dict[str, Any]]:
        """This hero's historical win rate against each named enemy (OpenDota, public aggregate)."""
        rows = {int(r["hero_id"]): r for r in self.matchups.get(hero_id, []) if "hero_id" in r}
        out = []
        for name in enemy_names:
            npc = self.find_hero(name)
            enemy = self.heroes.get(npc or "")
            row = rows.get(int(enemy["id"])) if enemy else None
            if row and int(row.get("games_played") or 0) >= min_games:
                games = int(row["games_played"])
                out.append({"vs": self.hero_name(npc), "win_rate": f"{int(row.get('wins') or 0) / games:.0%}",
                            "games": games})
        return out

    # abilities and skill points -----------------------------------------------------
    def ability_name(self, name: str) -> str:
        info = self.ability_info.get(name)
        return info["dname"] if info and info.get("dname") else name.split("_", 1)[-1].replace("_", " ").title()

    def skill_order(self, hero_id: int) -> list[str]:
        """The ability most often taken at each upgrade (1st, 2nd...) by pro players this patch."""
        best: dict[int, tuple[int, str]] = {}
        for row in self.skill_rows.get(hero_id, []):
            try:
                lvl, n, name = int(row["lvl"]), int(row["n"]), self.ability_ids.get(str(row["ab"]), "")
            except (KeyError, TypeError, ValueError):
                continue
            if name and (lvl not in best or n > best[lvl][0]):
                best[lvl] = (n, name)
        return [best[lvl][1] for lvl in sorted(best)]

    def skillable(self, hero_id: int) -> set[str]:
        """Abilities that take skill points for this hero (those players actually put points into)."""
        totals: Counter = Counter()
        for row in self.skill_rows.get(hero_id, []):
            name = self.ability_ids.get(str(row.get("ab")), "")
            if name and not name.startswith("special_bonus"):
                totals[name] += int(row.get("n") or 0)
        return {a for a, n in totals.items() if n >= 5}

    POINTS_KNOWN_TO = 16  # every level up to here gives one point; past it some levels give none

    def unspent_points(self, snap) -> int | None:
        """Skill points the player can spend now, or None when Doty can't tell (no abilities reported, or past
        level 16, where some levels give no point). Counts the abilities this hero's players put points into
        (innate and Shard/Scepter abilities report levels too, which would hide a waiting point). A learnable
        ability outside that list with a level counts as well, unless a Shard or Scepter could explain it, so an
        ability Doty didn't expect can't make up a point either."""
        abilities = [a for a in snap.abilities if not a.is_talent_or_attr]
        if not abilities or not snap.level or snap.level > self.POINTS_KNOWN_TO:
            return None

        def learnable(name: str) -> bool:
            info = self.ability_info.get(name, {})
            return not (info.get("innate") or info.get("hidden"))

        skillable = self.skillable(snap.hero_id)
        if skillable and any(a.name in skillable for a in abilities):  # (Ability Draft: none of the hero's own)
            counted = sum(a.level for a in abilities if a.name in skillable)
            if not (snap.hero("aghanims_shard", False) or snap.hero("aghanims_scepter", False)):
                counted += sum(a.level for a in abilities if a.name not in skillable and learnable(a.name))
        else:
            counted = sum(a.level for a in abilities if learnable(a.name))
        spent = counted + snap.talents_taken + int(snap.hero("attributes_level", 0) or 0)
        earned = snap.level
        if snap.hero_name == "npc_dota_hero_invoker":  # since 7.41b: a bonus point at 6, 12 and 18
            earned += sum(1 for lvl in (6, 12, 18) if snap.level >= lvl)
        return max(0, earned - spent)

    @staticmethod
    def open_talents(snap) -> int:
        """Talent tiers reached (10, 15, 20, 25) that the player hasn't picked a talent from yet."""
        return max(0, sum(1 for lvl in (10, 15, 20, 25) if snap.level >= lvl) - snap.talents_taken)

    def next_skill(self, snap) -> str | None:
        """The ability to level next, following the pro order this patch from where the player is now
        (an early detour doesn't throw it off)."""
        have = {a.name: a.level for a in snap.abilities if not a.is_talent_or_attr}
        need: Counter = Counter()
        for ab in self.skill_order(snap.hero_id):
            if ab not in have:
                continue  # talents, attribute points, abilities renamed since
            need[ab] += 1
            if need[ab] > have[ab]:
                return ab
        return None

    def ability_notes(self, hero_npc: str) -> list[dict[str, Any]]:
        """What each of the hero's abilities does, briefly, for the coach."""
        out = []
        for name in (self.hero_abilities.get(hero_npc) or {}).get("abilities", []):
            info = self.ability_info.get(name)
            if name == "generic_hidden" or not info or not info.get("dname"):
                continue
            desc = re.sub(r"\s+", " ", info.get("desc", "")).strip()
            if len(desc) > 220:
                desc = desc[:220].rsplit(" ", 1)[0] + "…"
            note: dict[str, Any] = {"name": info["dname"], "does": desc}
            if info.get("innate"):
                note["innate"] = True
            out.append(note)
        return out

    # items -----------------------------------------------------------------------
    def typical_items(self, hero_id: int, n: int = 10) -> list[str]:
        """The finished items this hero's players buy most, over the whole game. A stepping stone (Perseverance on
        the way to Battle Fury, Yasha on the way to Manta Style) is left out when its players usually go on to
        the bigger item: the bigger one is the build. (Rules ask on every update: kept until the data changes.)"""
        pop = self.item_popularity.get(hero_id) or {}
        key = (id(pop), id(self.items), len(self.items),
               tuple((len(d), sum(int(n) for n in d.values())) for d in ((pop.get(ph) or {}) for ph, _ in PHASES)))
        hit = self._typical.get(hero_id)
        if hit is None or hit[0] != key:
            by_id = self._id_to_item()
            totals: Counter = Counter()
            for phase, _start in PHASES[1:]:
                for item_id, count in (pop.get(phase) or {}).items():
                    item = by_id.get(int(item_id))
                    if item and self._is_build_target(item):
                        totals[item] += int(count)
            ranked = [i for i, _ in totals.most_common()]
            inside = {i: self.owned_with_parts({i}) - {i} for i in ranked}
            hit = (key, [i for i in ranked if not any(
                i in inside[big] and totals[big] >= FOLD_SHARE * totals[i] for big in ranked)])
            self._typical[hero_id] = hit
        return hit[1][:n]

    # what an item is made of, and what's left to pay -----------------------------------------------
    def parts_of(self, item: str) -> list[str]:
        """What `item` is combined from, as item keys (a part can be there twice), with its recipe when that
        costs gold. [] for an item bought whole (Blink Dagger) or one Doty has no data for."""
        parts = [p if str(p).startswith("item_") else f"item_{p}"
                 for p in ((self.items.get(item) or {}).get("components") or [])]
        if not parts or any(p not in self.items for p in parts):
            return []  # (a part Doty doesn't know: treat the item as bought whole rather than guess its price)
        recipe = f"item_recipe_{item.removeprefix('item_')}"
        if self.item_cost(recipe) > 0:
            parts.append(recipe)
        return parts

    def _walk_parts(self, item: str, have: Counter, missing: list[str], depth: int = 0) -> int:
        """Gold already paid towards `item` (the parts of it in `have`, which are used up); the parts still to
        buy are added to `missing`."""
        paid = 0
        for part in self.parts_of(item) if depth < 6 else []:
            if have[part] > 0:
                have[part] -= 1
                paid += self.item_cost(part)
            elif self.parts_of(part):
                paid += self._walk_parts(part, have, missing, depth + 1)
            else:
                missing.append(part)
        return paid

    def cost_left(self, item: str, owned) -> int:
        """Gold still to pay for `item`, counting the parts of it you already hold (`owned`: every item you have,
        inventory, backpack and stash, with repeats). 0 if you have it. An Ogre Axe and a Mithril Hammer in the
        bag leave 1,450 of Black King Bar's 4,050."""
        have = Counter(owned)
        if have[item] > 0:
            return 0
        return max(0, self.item_cost(item) - self._walk_parts(item, have, []))

    def missing_parts(self, item: str, owned) -> list[str]:
        """The parts of `item` still to buy (recipes included), [] for an item bought whole."""
        missing: list[str] = []
        self._walk_parts(item, Counter(owned), missing)
        return missing

    def secret_shop(self, item: str) -> bool:
        return (self.items.get(item) or {}).get("qual") == "secret_shop"

    def next_buy(self, item: str, owned, gold: int, min_part: int = PART_MIN_COST) -> dict[str, Any] | None:
        """What to buy now on the way to `item`: the whole of what's left when the gold covers it ("finish"),
        else the dearest part the gold does cover (never a recipe on its own, nor small change). None when
        there's nothing worth buying yet, or you have it. {"item", "buy", "cost", "left", "finish"}."""
        left = self.cost_left(item, owned)
        if left <= 0:
            return None
        if gold >= left:
            return {"item": item, "buy": item, "cost": left, "left": left, "finish": True}
        parts = [(self.item_cost(p), p) for p in self.missing_parts(item, owned) if "item_recipe_" not in p]
        can = [(c, p) for c, p in parts if min_part <= c <= gold]
        if not can:
            return None
        cost, part = max(can)
        return {"item": item, "buy": part, "cost": cost, "left": left, "finish": False}

    def next_target(self, hero_npc: str, clock: int, owned) -> str | None:
        """The item you're building now: the one in the plan you've started paying for (you hold a part of
        it), else the first one you don't have. A second pair of boots is never it. (So a cheap early item you
        skipped doesn't stay "next" while you're three parts into Battle Fury.)"""
        names = set(owned)
        have = self.owned_with_parts(names)
        todo = [i for i in self.build_plan(hero_npc, clock) if i not in have and not self.second_boots(i, names)]
        # (Plain Boots of Speed are a part of every pair of boots: holding them isn't a start on any one.)
        held = [i for i in owned if i != "item_boots"]
        for item in todo:
            if 0 < self.cost_left(item, held) < self.item_cost(item):
                return item
        return todo[0] if todo else None

    def typical_time(self, hero_id: int, item: str, min_games: int = 30) -> int | None:
        """Game second by which half of this hero's players have finished `item` (OpenDota buckets:
        7:30, 10, 12, 15, 20, 25 and 30+ minutes). None without enough games. The data is from normal
        games; in Turbo the time is given on Turbo's clock (about half)."""
        rows = sorted(self.item_timings.get((hero_id, item), []), key=lambda r: int(r.get("time") or 0))
        total = sum(int(r.get("games") or 0) for r in rows)
        if total < min_games:
            return None
        seen = 0
        for r in rows:
            seen += int(r.get("games") or 0)
            if seen >= total / 2:
                return self.mode.at(int(r["time"]))
        return None

    def owned_with_parts(self, owned: set[str]) -> set[str]:
        """What you own plus every part inside it (Battle Fury holds a Ring of Health, Manta a Yasha), so a
        part of something you built is never suggested."""
        out, todo = set(owned), list(owned)
        while todo:
            for part in (self.items.get(todo.pop()) or {}).get("components") or []:
                key = part if str(part).startswith("item_") else f"item_{part}"
                if key not in out:
                    out.add(key)
                    todo.append(key)
        return out

    def second_boots(self, item: str, owned: set[str]) -> bool:
        """`item` is a boots upgrade and you already have a different one (Phase Boots after Power Treads).
        Plain Boots of Speed don't count: upgrading them is the point. Travel Boots are an upgrade on top."""
        def upgrade(i: str) -> bool:
            return i != "item_boots" and "boots" in ((self.items.get(i) or {}).get("components") or [])
        return self._boots_upgrade(item) and any(upgrade(i) and i != item for i in owned)

    def win_rate_by(self, hero_id: int, item: str, clock: int, min_games: int = 30) -> float | None:
        """How often this hero's players win when `item` is finished by about `clock` (the OpenDota bucket it
        falls in: 7:30, 10, 12, 15, 20, 25, 30+ minutes). None without enough games in that bucket. Public
        statistics, and a skewed sample (parsed matches), so only gaps of several points mean anything.
        Normal games only: what a late item costs in Turbo, these numbers don't say."""
        rows = sorted(self.item_timings.get((hero_id, item), []), key=lambda r: int(r.get("time") or 0))
        if not rows or self.mode.turbo:
            return None
        row = next((r for r in rows if clock <= int(r.get("time") or 0)), rows[-1])
        games = int(row.get("games") or 0)
        return int(row.get("wins") or 0) / games if games >= min_games else None

    def item_odds(self, hero_id: int, item: str, after: int = 0, min_games: int = 30) -> list[tuple[int, float]]:
        """(by game second, win rate) for each bucket from `after` on with enough games: how the item's value
        falls off the later it lands. (Normal games only, like `win_rate_by`.)"""
        if self.mode.turbo:
            return []
        rows = sorted(self.item_timings.get((hero_id, item), []), key=lambda r: int(r.get("time") or 0))
        return [(int(r["time"]), int(r.get("wins") or 0) / int(r["games"])) for r in rows
                if int(r.get("time") or 0) >= after and int(r.get("games") or 0) >= min_games]

    def bought_items(self, hero_id: int) -> set[str] | None:
        """What this hero's players really buy (a real part of the builds in some phase of the game, not a
        one-off). None when Doty has no data for the hero."""
        pop = self.item_popularity.get(hero_id)
        if not pop:
            return None
        by_id = self._id_to_item()
        seen: set[str] = set()
        for phase, _start in PHASES:
            counts = {int(i): int(n) for i, n in (pop.get(phase) or {}).items()}
            if not counts:
                continue
            floor = max(3, 0.08 * max(counts.values()))
            seen |= {by_id[i] for i, n in counts.items() if n >= floor and i in by_id}
        return seen

    def allowed_items(self, hero_id: int) -> set[str] | None:
        """Items it makes sense to suggest for this hero: anything its players buy, plus items that
        answer particular enemies. None when Doty has no data for the hero (then nothing is filtered)."""
        seen = self.bought_items(hero_id)
        if seen is None:
            return None
        return seen | {f"item_{i}" for i in COUNTER_ITEMS}

    def _masked(self, text: str, what: str, own_hero: str = "") -> str:
        """`text` with item names ("item") or ability names ("ability") blanked out. Abilities: every
        hero's abilities of two words or more ("Blink Strike", "Drums of Slom"), and all of `own_hero`'s
        (Anti-Mage's "Blink" is his spell, not Blink Dagger)."""
        if what == "item":
            if self._item_mask is None:
                heroes = {str(h.get("localized_name") or "") for h in self.heroes.values()}
                names = {str(v.get("dname") or "") for v in self.items.values()}
                self._item_mask = _name_pattern(n for n in names if len(n) >= 4 and n not in heroes)
            return self._item_mask.sub(" ", text) if self._item_mask is not None else text
        items = {str(v.get("dname") or "") for v in self.items.values()}
        if self._ability_mask is None:
            names = {str(v.get("dname") or "") for v in self.ability_info.values()}
            self._ability_mask = _name_pattern(n for n in names if " " in n.strip() and n not in items)
        if self._ability_mask is not None:
            text = self._ability_mask.sub(" ", text)
        if own_hero:
            if own_hero not in self._own_masks:
                own = [str(self.ability_info.get(a, {}).get("dname") or "")
                       for a in (self.hero_abilities.get(own_hero) or {}).get("abilities", [])]
                self._own_masks[own_hero] = _name_pattern(n for n in own if len(n) >= 3 and n not in items)
            if self._own_masks[own_hero] is not None:
                text = self._own_masks[own_hero].sub(" ", text)
        return text

    def item_mentions(self, text: str, own_hero: str = "") -> set[str]:
        """Big items (1000+ gold) a piece of text names: 'Get Blink Dagger' -> {item_blink}. Ability names
        don't count (see `_masked`); pass the player's hero as `own_hero` so its spells don't either."""
        if not text or not self.items:
            return set()
        text = self._masked(text, "ability", own_hero)
        if self._item_re is None:
            names = {str(v.get("dname")): k for k, v in self.items.items()
                     if v.get("dname") and int(v.get("cost") or 0) >= 1000}
            names.update({alias: f"item_{key}" for alias, key in ITEM_ALIASES.items() if f"item_{key}" in self.items})
            self._item_names = names
            pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            self._item_re = re.compile(rf"(?<![A-Za-z])({pattern})(?![A-Za-z])")
        return {self._item_names[m] for m in self._item_re.findall(text)}

    def buy_mentions(self, text: str, own_hero: str = "") -> set[str]:
        """Big items a line tells the player to buy: "Buy Yasha and Manta Style" -> both; "Buy Manta Style
        after Battle Fury" -> Manta Style only; "Use Black King Bar before you jump" and "Build your Yasha into
        Manta Style" -> none. Careful on purpose: a tip is dropped on this, so when in doubt it isn't a buy."""
        if not text or not self.items:
            return set()
        if not self.item_mentions(text, own_hero) or self._item_re is None:  # (also builds the name pattern)
            return set()
        text = self._masked(text, "ability", own_hero)
        out = set()
        for m in self._item_re.finditer(text):
            before = re.split(r"[.,;:!?]", text[max(0, m.start() - 80):m.start()])[-1]  # its own clause
            verb = None
            for verb in BUY_VERB.finditer(before):
                pass  # the last buy word before the item
            if verb is None or MADE_INTO.match(text, m.end()):
                continue
            between = before[verb.end():]
            if len(between.split()) <= BUY_REACH and not NOT_THE_OBJECT.search(between):
                out.add(self._item_names[m.group(1)])
        return out

    def summary_for_prompt(self, hero_npc: str, clock: int, owned: set[str] | None = None) -> dict[str, Any]:
        # What the player has (or has built into something) and a second pair of boots are left out: listed,
        # the AI suggests them ("Buy Power Treads" to a player in Phase Boots, in a real demo run).
        names = set(owned or ())
        have = self.owned_with_parts(names)

        def open_(item: str) -> bool:
            return item not in have and not self.second_boots(item, names)

        plan = [self.item_name(i) for i in self.build_plan(hero_npc, clock) if open_(i)][:PLAN_PER_PHASE]
        out: dict[str, Any] = {"popular_next_items": plan}
        hero = self.heroes.get(hero_npc)
        if hero:
            typical = [self.item_name(i) for i in self.typical_items(int(hero["id"])) if open_(i)]
            if typical:
                out["typical_items"] = typical
            odds = {}
            for item in self.typical_items(int(hero["id"]), 4):
                if not open_(item):
                    continue
                buckets = self.item_odds(int(hero["id"]), item, after=clock)
                if len(buckets) >= 2:
                    odds[self.item_name(item)] = " · ".join(f"by {t // 60}:00 {wr:.0%}" for t, wr in buckets[:4])
                if len(odds) == 2:
                    break
            if odds:
                out["next_item_odds"] = odds
            order = [self.ability_name(a) for a in self.skill_order(int(hero["id"]))
                     if not a.startswith("special_bonus")][:10]
            if order:
                out["pro_skill_order"] = order
        notes = self.ability_notes(hero_npc)
        if notes:
            out["your_abilities"] = notes
        return out
