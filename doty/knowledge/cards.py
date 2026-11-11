"""Hero cards: what the data says about playing one hero right now, small enough to read in every prompt.

The coach's own memory of Dota is out of date and unreliable about numbers, so everything it says about builds,
timings, pace and matchups has to come from data Doty gives it, with the patch named. A card is that data for
one hero, studied from public statistics by `study.py` and saved in the cache (`cards/card_<hero id>.json`):

- **Builds by role** (pro matches on this patch, each player's position worked out from their lane and their
  team's farm order): the items each position finishes, by phase, how many of them do, and by when; the
  starting items; the boots.
- **Item timings that matter** (OpenDota's public item timings): the win rate when the hero's key items land
  early or late, as one fact per item with a target time. Turbo halves the clock (an estimate: `mode.py`).
- **Pace by rank** (OpenDota's benchmarks, placed on the ranked ladder by how many public matches each rank
  plays, shaped by the pro games' minute-by-minute curves): last hits, GPM and XPM at 10:00 and 20:00 for your
  rank and the one above, and what pros of this position have.
- **Power spikes:** items whose early timing wins far more than the hero does; the ultimate's levels and the
  talents nearly everyone takes.
- **Skill order** of pro players of the position.
- **Matchups by rank and mode** (public matches of the last few days): the enemies it does worst and best
  against, by how much, and, where the ability data shows it in one clause, why.
- **Game length:** how it does in short and long games at your rank.
- **Patch changes** to the hero and its usual items.

Every number keeps its sample size; a fact with too few games behind it is left out. A card names the patch
it describes and is stale once a newer one lands (`is_stale`). Card facts are data: the coach and the rules
phrase them (so this module adds no translatable strings). Hero-level public data only: nothing here is
about a player.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import CACHE_DIR
from .constants import NOT_BUILD_TARGETS, STANDALONE_TARGETS, _norm
from .roles import NAMES as ROLE_NAMES

CARD_VERSION = 1
CARDS_DIR = "cards"
CARD_TTL_S = 7 * 24 * 3600  # a card is studied again after a week even without a new patch
BRACKET_NAMES = {0: "All ranks", 1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon", 5: "Legend", 6: "Ancient",
                 7: "Divine", 8: "Immortal"}  # (as in stats.BRACKETS; repeated so this module stays light)
NORMAL_MODE, TURBO_MODE = 22, 23  # OpenDota game_mode: ranked All Pick, Turbo

# Sample-size floors: below these a fact is left out rather than shown.
MIN_ROLE_GAMES = 30  # pro games of a position before its build is that position's own
MIN_ITEM_PLAYERS = 8  # players of the role who finished the item
MIN_ITEM_SHARE = 0.15  # ...and this share of the role's players
MIN_START_SHARE = 0.3
MIN_BOOTS_SHARE = 0.1
FOLD_SHARE = 0.4  # a part (Yasha) drops out when the item it's built into (Manta) is this common next to it
MIN_BUCKET_GAMES = 30  # each side of an item-timing fact
MIN_TIMING_GAMES = 150  # all buckets of an item together
KEY_ITEM_COST = 1800  # an item this dear is one whose timing matters
KEY_ITEMS = 3
SPIKE_GAIN = 0.04  # an item finished early that wins this much more than the hero does is a power spike
MIN_SPIKE_GAMES = 50
MIN_BRACKET_GAMES = 2000  # hero games at a rank (in a mode) before that rank's own numbers are used
MIN_MATCHUP_GAMES = 400  # games against one enemy
MIN_MATCHUP_EFFECT = 0.02  # and at least two standard errors
MATCHUPS_SHOWN = 3
MIN_CURVE_GAMES = 300  # games in the short (or the long) span
EARLY_END_S, LATE_START_S = 25 * 60, 40 * 60  # (as curves.py; Turbo halves both)
CURVE_LEAN = 0.03
TALENT_SHARE = 0.6  # a talent this share of the tier's pickers take is "the" talent
# The explorer numbers skill points in the order they were spent, not hero levels (pro data puts the level-10
# talent at point 11), so a card speaks of points; the skill order is read up to this point.
SKILL_POINTS_SHOWN = 18
PACE_MINUTES = (10, 20)
PATCH_NOTES_SHOWN = 4
PROMPT_WORDS = 180  # prompt_block's budget (it goes in every prompt)

PACE_METRICS = {"lh": "last_hits_per_min", "gpm": "gold_per_min", "xpm": "xp_per_min"}


# --- reading cards ----------------------------------------------------------------------------------------
_loaded: dict[str, tuple[float, dict]] = {}  # path -> (mtime, card): rules ask on every update


def card_path(hero_id: int, cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or CACHE_DIR) / CARDS_DIR / f"card_{int(hero_id)}.json"


def _heroes(cache_dir: Path | None) -> dict[str, dict]:
    """dotaconstants heroes.json (shared with Knowledge), by id string; {} without it."""
    path = Path(cache_dir or CACHE_DIR) / "heroes.json"
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _loaded.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    _loaded[key] = (mtime, data)
    return data


def hero_id_of(hero: Any, cache_dir: Path | None = None) -> int:
    """A hero id from an id, an npc name ("npc_dota_hero_spirit_breaker") or a name ("Spirit Breaker", "sb"
    is not one: nicknames are Knowledge's job). 0 if unknown."""
    if isinstance(hero, int):
        return hero
    text = str(hero or "")
    if text.isdigit():
        return int(text)
    want = _norm(text.removeprefix("npc_dota_hero_"))
    for hid, h in _heroes(cache_dir).items():
        if isinstance(h, dict) and want in (_norm(str(h.get("localized_name") or "")),
                                            _norm(str(h.get("name") or "").removeprefix("npc_dota_hero_"))):
            return int(hid)
    return 0


def load(hero: Any, cache_dir: Path | None = None) -> dict | None:
    """The saved card for a hero (id, npc name or name), or None."""
    hid = hero_id_of(hero, cache_dir)
    if not hid:
        return None
    path = card_path(hid, cache_dir)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _loaded.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict) or card.get("version") != CARD_VERSION:
        return None
    _loaded[str(path)] = (mtime, card)
    return card


def save(card: dict, cache_dir: Path | None = None) -> Path:
    path = card_path(int(card["hero_id"]), cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")  # never a half-written card
    part.write_text(json.dumps(card, separators=(",", ":")), encoding="utf-8")
    part.replace(path)
    return path


def current_patch(cache_dir: Path | None = None) -> tuple[str, int]:
    """(the latest patch, e.g. "7.41f", and when it came out, unix seconds; 0 if unknown), from Valve's patch
    list (`patchnoteslist.json`, which Knowledge keeps), else dotaconstants' `patch.json` (major patches only)."""
    base = Path(cache_dir or CACHE_DIR)
    try:
        listing = json.loads((base / "patchnoteslist.json").read_text(encoding="utf-8"))
        patches = [p for p in listing.get("patches") or [] if isinstance(p, dict) and p.get("patch_number")]
        if patches:
            last = max(patches, key=lambda p: int(p.get("patch_timestamp") or 0))
            return str(last["patch_number"]), int(last.get("patch_timestamp") or 0)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        rows = json.loads((base / "patch.json").read_text(encoding="utf-8"))
        if isinstance(rows, list) and rows and isinstance(rows[-1], dict):
            return str(rows[-1].get("name") or ""), 0
    except (OSError, ValueError):
        pass
    return "", 0


def major(patch: str) -> str:
    """'7.41f' -> '7.41'."""
    return str(patch or "").rstrip("abcdefghijklmnopqrstuvwxyz")


def is_stale(card: dict | None, patch: str = "", now: float | None = None) -> bool:
    """True when the card should be studied again: none yet, an older card format, a newer patch than the one it
    describes (a letter patch counts: 7.41f after 7.41e), a week old, or built on a thin, blended sample (the
    first days of a patch) more than a day ago."""
    if not card or card.get("version") != CARD_VERSION:
        return True
    if patch and card.get("patch") != patch:
        return True
    age = (time.time() if now is None else now) - float(card.get("built_at") or 0)
    if age > CARD_TTL_S:
        return True
    return bool((card.get("basis") or {}).get("blended")) and age > 24 * 3600


# --- turning study data into a card ----------------------------------------------------------------------
def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parts(key: str, items: dict, depth: int = 0) -> set[str]:
    """Every part inside an item, however deep (keys without the item_ prefix)."""
    out: set[str] = set()
    for part in ((items.get(key) or {}).get("components") or []) if depth < 6 else []:
        part = str(part).removeprefix("item_")
        out.add(part)
        out |= _parts(part, items, depth + 1)
    return out


def _finished(key: str, items: dict) -> bool:
    """A finished item worth naming in a build (not a component, a consumable or a recipe)."""
    data = items.get(key) or {}
    if not data or key in NOT_BUILD_TARGETS or key.startswith("recipe_") or _num(data.get("cost")) < 500:
        return False
    return bool(data.get("components")) or key in STANDALONE_TARGETS


def _is_boots(key: str, items: dict) -> bool:
    return key != "boots" and not key.startswith("travel_boots") and "boots" in _parts(key, items)


def position_of(farm_rank: int, lane_role: int) -> int:
    """A player's position (1-5) from their team's farm order (1 = most gold a minute) and their lane
    (OpenDota lane_role: 1 safe, 2 mid, 3 off, 4 jungle). The three best-farmed are the cores, by lane; the
    two others support, the one in the safe lane being the hard support. (The same rule the pro query uses.)"""
    if farm_rank <= 3:
        return {1: 1, 2: 2}.get(lane_role, 3)
    return 5 if lane_role == 1 else 4


ROLE_GROUPS = {"1": (1,), "2": (2,), "3": (3,), "4": (4,), "5": (5,), "core": (1, 2, 3), "support": (4, 5),
               "all": (1, 2, 3, 4, 5)}


def _roles_from_pro(rows: list[dict], items: dict, ability_ids: dict, abilities: dict,
                    talent_tiers: dict[str, int]) -> dict[str, dict]:
    """The pro query's rows (study.PRO_SQL: kind g = games and pace, i = items, s = skills) -> per role key
    ("1".."5", "core", "support", "all") its games, build, skills and pro pace."""
    games: dict[int, dict] = {}
    item_rows: dict[int, dict[str, tuple[int, int, float]]] = {}
    skill_rows: dict[int, dict[tuple[int, str], int]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pos = int(_num(r.get("pos")))
        if pos not in ROLE_NAMES:
            continue
        kind = r.get("k")
        if kind == "g":
            games[pos] = {"n": int(_num(r.get("n"))), "w": int(_num(r.get("w"))),
                          "lh10": _num(r.get("v1")), "g10": _num(r.get("v2")), "x10": _num(r.get("v3")),
                          "n20": int(_num(r.get("v4"))), "lh20": _num(r.get("v5")), "g20": _num(r.get("v6")),
                          "x20": _num(r.get("v7")), "lhpm": _num(r.get("v8")), "gpm": _num(r.get("v9")),
                          "xpm": _num(r.get("v10"))}
        elif kind == "i":
            item_rows.setdefault(pos, {})[str(r.get("x") or "")] = (int(_num(r.get("n"))), int(_num(r.get("w"))),
                                                                    _num(r.get("v1")))
        elif kind == "s":
            lvl, _, ab = str(r.get("x") or "").partition(":")
            name = ability_ids.get(ab, "")
            if lvl.isdigit() and name:
                key = (int(lvl), name)
                skill_rows.setdefault(pos, {})[key] = skill_rows.get(pos, {}).get(key, 0) + int(_num(r.get("n")))
    total = sum(g["n"] for g in games.values())
    out: dict[str, dict] = {}
    for key, positions in ROLE_GROUPS.items():
        have = [p for p in positions if p in games]
        n = sum(games[p]["n"] for p in have)
        if not n:
            continue
        role: dict[str, Any] = {"games": n, "wins": sum(games[p]["w"] for p in have),
                                "share": round(n / total, 3) if total else 0.0}
        role["pace"] = _merge_pace([games[p] for p in have])
        merged: dict[str, list[float]] = {}
        for p in have:
            for item, (cnt, wins, med) in item_rows.get(p, {}).items():
                m = merged.setdefault(item, [0, 0, 0.0])
                m[0] += cnt
                m[1] += wins
                m[2] += med * cnt  # (a games-weighted mean of the positions' medians: close enough for "by when")
        role["build"] = _build(n, {k: (int(v[0]), int(v[1]), v[2] / v[0]) for k, v in merged.items() if v[0]}, items)
        counts: dict[tuple[int, str], int] = {}
        for p in have:
            for k, cnt in skill_rows.get(p, {}).items():
                counts[k] = counts.get(k, 0) + cnt
        role["skills"] = _skills(counts, n, abilities, talent_tiers)
        out[key] = role
    return out


def _merge_pace(rows: list[dict]) -> dict:
    n = sum(r["n"] for r in rows)
    n20 = sum(r["n20"] for r in rows)
    if not n:
        return {}

    def avg(key: str, weight: str = "n") -> float:
        w = sum(r[weight] for r in rows)
        return sum(r[key] * r[weight] for r in rows) / w if w else 0.0
    return {"n": n, "n20": n20, "lh10": round(avg("lh10"), 1), "g10": round(avg("g10")), "x10": round(avg("x10")),
            "lh20": round(avg("lh20", "n20"), 1), "g20": round(avg("g20", "n20")), "x20": round(avg("x20", "n20")),
            "lhpm": round(avg("lhpm"), 3), "gpm": round(avg("gpm"), 1), "xpm": round(avg("xpm"), 1)}


def _build(games: int, rows: dict[str, tuple[int, int, float]], items: dict) -> dict:
    """What a role's players buy: starting items, boots, and finished items by phase (each with how many of
    the role's players finish it, the share, their win rate and the usual time)."""
    def entry(key: str) -> dict:
        n, w, med = rows[key]
        return {"item": key, "n": n, "share": round(n / games, 3), "wr": round(w / n, 3) if n else 0.0,
                "by_s": int(round(med))}

    start = sorted((k for k, (n, _w, med) in rows.items()
                    if med <= 0 and n / games >= MIN_START_SHARE and k in items and not k.startswith("recipe_")),
                   key=lambda k: -rows[k][0])
    boots = sorted((k for k, (n, _w, _m) in rows.items()
                    if _is_boots(k, items) and n >= MIN_ITEM_PLAYERS and n / games >= MIN_BOOTS_SHARE),
                   key=lambda k: -rows[k][0])
    core = [k for k, (n, _w, med) in rows.items()
            if _finished(k, items) and not _is_boots(k, items) and med > 0
            and n >= MIN_ITEM_PLAYERS and n / games >= MIN_ITEM_SHARE]
    inside = {k: _parts(k, items) for k in core}
    core = [k for k in core if not any(k in inside[big] and rows[big][0] >= FOLD_SHARE * rows[k][0]
                                       for big in core if big != k)]
    phases: dict[str, list[dict]] = {"early": [], "mid": [], "late": []}
    for k in sorted(core, key=lambda k: rows[k][2]):
        med = rows[k][2]
        phases["early" if med < 900 else "mid" if med < 1800 else "late"].append(entry(k))
    return {"start": [entry(k) for k in start[:6]], "boots": [entry(k) for k in boots[:3]], **phases}


def _skills(counts: dict[tuple[int, str], int], games: int, abilities: dict, talent_tiers: dict[str, int]) -> dict:
    """The most common pick at each upgrade, which ability is maxed first, the ultimate's levels and the talents
    most players of the role take."""
    if not counts:
        return {}
    best: dict[int, tuple[int, str]] = {}  # skill point -> (players, ability), talents and attributes included
    talents: dict[str, int] = {}
    for (lvl, name), n in counts.items():
        if name.startswith("special_bonus"):
            talents[name] = talents.get(name, 0) + n
        if lvl <= SKILL_POINTS_SHOWN and (lvl not in best or n > best[lvl][0]):
            best[lvl] = (n, name)
    picks = [(lvl, best[lvl][1]) for lvl in sorted(best)]
    # The ultimate: what most take at point 6, if it was never taken before (Invoker's Quas at 6 is not one).
    ult = best[6][1] if 6 in best and not best[6][1].startswith("special_bonus") else ""
    if ult and any(name == ult for lvl, name in picks if lvl < 6):
        ult = ""
    order = [name for _lvl, name in picks if not name.startswith("special_bonus")]
    max_order: list[str] = []
    seen: dict[str, int] = {}
    for name in order:
        seen[name] = seen.get(name, 0) + 1
        if name != ult and seen[name] == 4 and name not in max_order:
            max_order.append(name)
    picked: list[dict] = []
    by_tier: dict[int, list[tuple[int, str]]] = {}
    for name, n in talents.items():
        tier = talent_tiers.get(name)
        if tier:
            by_tier.setdefault(tier, []).append((n, name))
    for tier, rows in sorted(by_tier.items()):
        total = sum(n for n, _ in rows)
        n, name = max(rows)
        if total >= MIN_ROLE_GAMES // 2 and n / total >= TALENT_SHARE:
            picked.append({"level": 5 + 5 * tier, "talent": name, "name": _dname(abilities, name),
                           "share": round(n / total, 2), "n": total})
    dn = {name: _dname(abilities, name) for name in set(order)}
    return {"order": [dn[n] for n in order], "max_order": [dn[n] for n in max_order], "ult": dn.get(ult, ""),
            "ult_points": [lvl for lvl, n in picks if n == ult and ult], "talents": picked, "n": games}


def _dname(abilities: dict, name: str) -> str:
    return str((abilities.get(name) or {}).get("dname") or name.split("_", 1)[-1].replace("_", " ").title())


def _foe_rate(row: dict, mode: str, bracket: str) -> float | None:
    """An enemy's own win rate at a rank (OpenDota heroStats; Turbo has no ranks there)."""
    if mode == str(TURBO_MODE):
        n, w = row.get("turbo_picks"), row.get("turbo_wins")
    elif bracket == "0":
        n, w = row.get("pub_pick"), row.get("pub_win")
    else:
        n, w = row.get(f"{bracket}_pick"), row.get(f"{bracket}_win")
    n, w = _num(n), _num(w)
    return round(w / n, 4) if n >= MIN_MATCHUP_GAMES else None


def _pub_tables(rows: list[dict], hero_stats: Any = None) -> dict[str, dict]:
    """The public query's rows (study.PUB_SQL: kind v = against each enemy, d = by game length; per rank
    bracket and mode) -> {mode: {bracket: {"n", "w", "vs": {enemy: [n, w, enemy's own win rate or None]},
    "dur": {bin: [n, w]}}}}, with bracket "0" for every rank together. The hero's own games and wins are the
    sum over game lengths. `hero_stats`: OpenDota's heroStats rows, for each enemy's own win rate."""
    stats = {str(r.get("id")): r for r in hero_stats or [] if isinstance(r, dict)}
    out: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        mode, b = str(int(_num(r.get("g")))), int(_num(r.get("b")))
        n, w = int(_num(r.get("n"))), int(_num(r.get("w")))
        for bracket in {"0", str(b)} if 1 <= b <= 8 else {"0"}:
            t = out.setdefault(mode, {}).setdefault(bracket, {"vs": {}, "dur": {}})
            key = "vs" if r.get("k") == "v" else "dur"
            cell = t[key].setdefault(str(int(_num(r.get("x")))), [0, 0])
            cell[0] += n
            cell[1] += w
    for mode in out.values():
        for t in mode.values():
            t["n"] = sum(c[0] for c in t["dur"].values())
            t["w"] = sum(c[1] for c in t["dur"].values())
            t["vs"] = {e: c for e, c in t["vs"].items() if c[0] >= MIN_MATCHUP_GAMES}
    for mode, tables in out.items():
        for bracket, t in tables.items():
            for e, c in t["vs"].items():
                c.append(_foe_rate(stats.get(e) or {}, mode, bracket))
    return out


def _timing_table(rows: list[dict]) -> dict[str, dict]:
    """OpenDota's /scenarios/itemTimings for a hero (every item at once) -> {item: {"buckets": [[t, n, w]]}}.
    A bucket's time is its upper end: finished by 7:30, 10, 12, 15, 20, 25 or 30 minutes."""
    out: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("item"):
            continue
        out.setdefault(str(r["item"]), {"buckets": []})["buckets"].append(
            [int(_num(r.get("time"))), int(_num(r.get("games"))), int(_num(r.get("wins")))])
    for v in out.values():
        v["buckets"].sort()
        v["n"] = sum(b[1] for b in v["buckets"])
    return {k: v for k, v in out.items() if v["n"] >= MIN_TIMING_GAMES}


def _bench_table(bench: Any) -> dict[str, list[list[float]]]:
    result = (bench or {}).get("result") if isinstance(bench, dict) else None
    out = {}
    for metric in PACE_METRICS.values():
        rows = (result or {}).get(metric) or []
        table = sorted([_num(r.get("percentile")), _num(r.get("value"))] for r in rows if isinstance(r, dict))
        if len(table) >= 3:
            out[metric] = table
    return out


def rank_percentiles(rows: Any) -> dict[str, float]:
    """Where the middle of each rank sits on the ranked ladder (0-1), from how many public ranked matches each
    rank's average falls in (study.RANK_SQL). Used to read a rank's place off OpenDota's benchmarks, which are
    percentiles of all of a hero's players. An approximation, said so on the card."""
    counts: dict[int, int] = {}
    for r in rows or []:
        if isinstance(r, dict):
            b = int(_num(r.get("b")))
            if 1 <= b <= 8:
                counts[b] = counts.get(b, 0) + int(_num(r.get("n")))
    total = sum(counts.values())
    if not total:
        return {}
    out, below = {}, 0
    for b in range(1, 9):
        share = counts.get(b, 0) / total
        out[str(b)] = round(below + share / 2, 3)
        below += share
    return out


# matchup reasons, from the ability data -------------------------------------------------------------------
_SILENCE = re.compile(r"\bsilenc", re.I)
_DISPEL = re.compile(r"(applies a (strong |basic )?dispel|\bdispels\b|\bpurges?\b)", re.I)
_DISABLE = re.compile(r"\b(stuns?|stunned|stunning|roots?|rooted|hex(es)?|sleep|taunts?|fears?|cyclone|"
                      r"disarms?)\b", re.I)
_INVIS = re.compile(r"\binvisib", re.I)


def hero_traits(npc: str, heroes_by_npc: dict, hero_abilities: dict, abilities: dict) -> dict:
    """What the ability data says about a hero, for one-clause matchup reasons: its silences, dispels,
    disables a basic dispel removes, disables that go through Black King Bar, invisibility, and whether its
    damage leans on spells (an Intelligence hero, or tagged Nuker)."""
    traits: dict[str, Any] = {"silence": "", "dispel": "", "dispellable_disable": "", "bkb_disable": "",
                              "invis": ""}
    for name in (hero_abilities.get(npc) or {}).get("abilities", []):
        a = abilities.get(name) or {}
        desc, dname = str(a.get("desc") or ""), str(a.get("dname") or "")
        if not dname or name == "generic_hidden" or "Hidden" in str(a.get("behavior")):
            continue
        if _SILENCE.search(desc) and not traits["silence"]:
            traits["silence"] = dname
        if _DISPEL.search(desc) and not traits["dispel"]:
            traits["dispel"] = dname
        if _DISABLE.search(desc):
            if str(a.get("dispellable") or "") == "Yes" and not traits["dispellable_disable"]:
                traits["dispellable_disable"] = dname
            if str(a.get("bkbpierce") or "") == "Yes" and not traits["bkb_disable"]:
                traits["bkb_disable"] = dname
        if _INVIS.search(desc) and not traits["invis"]:
            traits["invis"] = dname
    h = heroes_by_npc.get(npc) or {}
    roles = list(h.get("roles") or [])
    traits["spells"] = h.get("primary_attr") == "int" or ("Nuker" in roles and roles.index("Nuker") <= 1)
    return traits


def matchup_reason(me: dict, foe: dict, delta: float) -> list[str]:
    """Why a matchup goes the way the numbers say, in data ([code, ability, ...]), or [] when the ability data
    doesn't show it. Only a reason that points the same way as the numbers is given."""
    if delta < 0:
        if foe.get("silence") and me.get("spells"):
            return ["their_silence", foe["silence"]]
        if foe.get("dispel") and me.get("dispellable_disable"):
            return ["their_dispel", foe["dispel"], me["dispellable_disable"]]
        if foe.get("bkb_disable"):
            return ["their_bkb_disable", foe["bkb_disable"]]
        if foe.get("invis"):
            return ["their_invis", foe["invis"]]
    elif delta > 0:
        if me.get("silence") and foe.get("spells"):
            return ["your_silence", me["silence"]]
        if me.get("dispel") and foe.get("dispellable_disable"):
            return ["your_dispel", me["dispel"], foe["dispellable_disable"]]
    return []


# patch notes -----------------------------------------------------------------------------------------------
def _clean(note: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str((note or {}).get("note") or ""))).strip()


def patch_facts(hero_id: int, notes: dict[str, Any], ability_ids: dict, abilities: dict, items: dict,
                item_keys: Iterable[str] = ()) -> dict[str, list[str]]:
    """What the patch notes (Valve's, one per letter patch: {"7.41f": {...}}) changed for this hero and for the
    items it usually buys, newest first: {"hero": ["7.41f: Greater Bash: Cooldown increased from 1.2 to
    1.5"], "items": [...]}."""
    by_id = {int(_num(v.get("id"))): k for k, v in items.items() if isinstance(v, dict) and v.get("id")}
    wanted = set(item_keys)
    hero_lines: list[str] = []
    item_lines: list[str] = []
    for version in sorted(notes, key=_patch_sort, reverse=True):
        data = notes[version]
        if not isinstance(data, dict):
            continue
        for hero in data.get("heroes") or []:
            if int(_num(hero.get("hero_id"))) != int(hero_id):
                continue
            lines = [_clean(n) for n in hero.get("hero_notes") or []]
            for ab in hero.get("abilities") or []:
                name = ability_ids.get(str(ab.get("ability_id")), "")
                label = _dname(abilities, name) if name else ""
                lines += [f"{label}: {_clean(n)}" if label else _clean(n) for n in ab.get("ability_notes") or []]
            lines += [f"Talent: {_clean(n)}" for n in hero.get("talent_notes") or []]
            hero_lines += [f"{version}: {line}" for line in lines if line and not line.endswith(":")]
        for group in ("items", "neutral_items"):
            for it in data.get(group) or []:
                key = by_id.get(int(_num(it.get("ability_id"))))
                if key not in wanted:
                    continue
                label = str((items.get(key) or {}).get("dname") or key)
                item_lines += [f"{version}: {label}: {line}" for line in map(_clean, it.get("ability_notes") or [])
                               if line]
    return {"hero": hero_lines, "items": item_lines}


def _patch_sort(version: str) -> tuple:
    m = re.fullmatch(r"(\d+)\.(\d+)([a-z]?)", str(version))
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (0, 0, str(version))


def build_card(hero_id: int, raw: dict[str, Any], ctx: dict[str, Any], now: float | None = None) -> dict:
    """A hero's card from what `study.py` fetched. `raw`: {"pro": explorer rows, "pub": explorer rows,
    "timings": itemTimings rows, "bench": benchmarks answer}. `ctx` (shared by every hero): {"patch", "major",
    "basis", "heroes" (dotaconstants, by id string), "items", "abilities", "hero_abilities", "ability_ids",
    "patch_notes" ({version: notes}), "rank_pct" (rank_percentiles), "hero_stats" (OpenDota heroStats)}."""
    heroes = ctx.get("heroes") or {}
    items = ctx.get("items") or {}
    abilities = ctx.get("abilities") or {}
    hero_abilities = ctx.get("hero_abilities") or {}
    ability_ids = {str(k): str(v) for k, v in (ctx.get("ability_ids") or {}).items()}
    hero = heroes.get(str(hero_id)) or {}
    npc = str(hero.get("name") or "")
    tiers = {t.get("name"): int(_num(t.get("level"))) for t in (hero_abilities.get(npc) or {}).get("talents", [])
             if isinstance(t, dict)}
    roles = _roles_from_pro(raw.get("pro") or [], items, ability_ids, abilities, tiers)
    pub = _pub_tables(raw.get("pub") or [], ctx.get("hero_stats"))
    timings = _timing_table(raw.get("timings") or [])
    # Reasons for every enemy with a matchup row, both ways (which one applies depends on the numbers).
    by_npc = {str(h.get("name")): h for h in heroes.values() if isinstance(h, dict)}
    traits = ctx.setdefault("_traits", {})  # (every hero's traits, worked out once per study run)

    def trait(n: str) -> dict:
        if n not in traits:
            traits[n] = hero_traits(n, by_npc, hero_abilities, abilities)
        return traits[n]
    enemies = {e for mode in pub.values() for t in mode.values() for e in t["vs"]}
    reasons: dict[str, dict] = {}
    names: dict[str, str] = {}
    npcs: dict[str, str] = {}
    for e in enemies:
        h = heroes.get(e) or {}
        names[e] = str(h.get("localized_name") or f"Hero {e}")
        npcs[e] = str(h.get("name") or "")
        if npc and h.get("name"):
            me, foe = trait(npc), trait(str(h["name"]))
            bad, good = matchup_reason(me, foe, -1), matchup_reason(me, foe, 1)
            if bad or good:
                reasons[e] = {"bad": bad, "good": good}
    bought = {e["item"] for r in roles.values() for ph in ("boots", "early", "mid", "late") for e in r["build"][ph]}
    used = bought | set(timings) | {e["item"] for r in roles.values() for e in r["build"]["start"]}
    notes = patch_facts(hero_id, ctx.get("patch_notes") or {}, ability_ids, abilities, items, bought)
    return {
        "version": CARD_VERSION, "hero_id": int(hero_id), "npc": npc,
        "name": str(hero.get("localized_name") or f"Hero {hero_id}"),
        "patch": str(ctx.get("patch") or ""), "major": str(ctx.get("major") or major(ctx.get("patch") or "")),
        "built_at": time.time() if now is None else now, "basis": dict(ctx.get("basis") or {}),
        "roles": roles, "pub": pub, "timings": timings, "bench": _bench_table(raw.get("bench")),
        "rank_pct": dict(ctx.get("rank_pct") or {}), "reasons": reasons, "names": names, "npcs": npcs,
        "item_names": {k: str((items.get(k) or {}).get("dname") or k.replace("_", " ").title()) for k in used},
        "item_costs": {k: int(_num((items.get(k) or {}).get("cost"))) for k in used},
        "patch_notes": notes,
    }


# --- views for the coach and the rules ---------------------------------------------------------------------
def _mode_key(mode: Any) -> str:
    """'turbo' or 'normal' from a mode.Mode, a string, or OpenDota's number."""
    if getattr(mode, "turbo", False) or str(mode).lower() in ("turbo", str(TURBO_MODE)):
        return "turbo"
    return "normal"


def _role_key(card: dict, role: Any) -> tuple[str, str]:
    """(the card's role entry to use, why: "" if it's exactly what was asked) for a role given as a position
    (1-5), a roles.Role, "core"/"support", or None (the hero's most played position)."""
    roles = card.get("roles") or {}
    if isinstance(role, str) and role.isdigit():
        role = int(role)
    if role == "all":
        return ("all", "") if (roles.get("all") or {}).get("games", 0) >= MIN_ROLE_GAMES else ("", "no_data")
    pos = getattr(role, "position", role)
    core = getattr(role, "core", None)
    if pos == 0 and core is None:
        role = None  # (a roles.Role not known yet)
    if isinstance(pos, int) and pos in ROLE_NAMES:
        key = str(pos)
        if (roles.get(key) or {}).get("games", 0) >= MIN_ROLE_GAMES:
            return key, ""
        group = "core" if pos <= 3 else "support"
        if (roles.get(group) or {}).get("games", 0) >= MIN_ROLE_GAMES:
            return group, "thin_position"
    elif core is not None or role in ("core", "support"):
        group = "core" if (core if core is not None else role == "core") else "support"
        if (roles.get(group) or {}).get("games", 0) >= MIN_ROLE_GAMES:
            return group, ""
    played = [k for k in "12345" if (roles.get(k) or {}).get("games", 0) >= MIN_ROLE_GAMES]
    if role in (None, 0, "") and played:
        return max(played, key=lambda k: roles[k]["games"]), "most_played"
    return ("all", "all_roles") if (roles.get("all") or {}).get("games", 0) >= MIN_ROLE_GAMES else ("", "no_data")


def role_label(key: str) -> str:
    if key.isdigit():
        return f"{ROLE_NAMES[int(key)]} (position {key})"
    return {"core": "core (positions 1-3)", "support": "support (positions 4-5)", "all": "all positions"}.get(key, "")


def _bracket_table(card: dict, bracket: int, mode: str) -> tuple[int, dict]:
    """(the bracket whose numbers are used, its table): the asked one with enough games, else all ranks."""
    tables = (card.get("pub") or {}).get(str(TURBO_MODE if mode == "turbo" else NORMAL_MODE)) or {}
    t = tables.get(str(int(bracket or 0)))
    if bracket and t and t.get("n", 0) >= MIN_BRACKET_GAMES:
        return int(bracket), t
    t = tables.get("0") or {}
    return 0, t if t.get("n", 0) >= MIN_BRACKET_GAMES else {}


def _enemy_id(card: dict, hero: Any, cache_dir: Path | None) -> int:
    """An enemy's id from an id, npc name or name, by the card's own list first (no file to read)."""
    if isinstance(hero, int) or str(hero).isdigit():
        return int(hero)
    want = _norm(str(hero))
    for e, name in (card.get("names") or {}).items():
        if want in (_norm(name), _norm((card.get("npcs") or {}).get(e, ""))):
            return int(e)
    return hero_id_of(hero, cache_dir)


def matchups(card: dict, bracket: int = 0, mode: Any = "normal", enemies: Iterable[Any] = (),
             cache_dir: Path | None = None) -> dict:
    """The enemies this hero does worst and best against at this rank and mode (win-rate points against its
    own win rate there, with games), each with a reason where the ability data shows one; and `enemies` (ids,
    npc names or names), the lineup in this match, whichever way they go. Only effects of at least two
    standard errors (and MIN_MATCHUP_EFFECT) count."""
    used, t = _bracket_table(card, bracket, _mode_key(mode))
    if not t:
        return {}
    base = t["w"] / t["n"]
    rows = []
    for e, cell in (t.get("vs") or {}).items():
        n, w = cell[0], cell[1]
        foe = cell[2] if len(cell) > 2 and cell[2] is not None else 0.5
        delta = w / n - (base - foe + 0.5)  # (beyond what both heroes' own strength says)
        if n >= MIN_MATCHUP_GAMES and abs(delta) >= max(MIN_MATCHUP_EFFECT, 2 * math.sqrt(0.25 / n)):
            why = (card.get("reasons") or {}).get(e, {}).get("bad" if delta < 0 else "good") or []
            rows.append({"hero_id": int(e), "name": (card.get("names") or {}).get(e, f"Hero {e}"),
                         "delta": round(delta, 3), "wr": round(w / n, 3), "n": n, "why": why})
    rows.sort(key=lambda r: r["delta"])
    lineup = []
    want = {_enemy_id(card, e, cache_dir) for e in enemies}
    for r in rows:
        if r["hero_id"] in want:
            lineup.append(r)
    return {"bracket": used, "base": round(base, 3), "n": t["n"],
            "worst": [r for r in rows if r["delta"] < 0][:MATCHUPS_SHOWN],
            "best": [r for r in rows[::-1] if r["delta"] > 0][:MATCHUPS_SHOWN], "lineup": lineup}


def game_length(card: dict, bracket: int = 0, mode: Any = "normal") -> dict:
    """Win rate in short and long games at this rank (Turbo: both spans halved), the median game length, and
    which way it leans ("late": it wins more of the long games, "early", or "")."""
    m = _mode_key(mode)
    used, t = _bracket_table(card, bracket, m)
    pace = 2.0 if m == "turbo" else 1.0
    early_end, late_start = EARLY_END_S / pace, LATE_START_S / pace
    spans = {"early": [0, 0], "late": [0, 0]}
    bins = sorted((int(k) * 300, c) for k, c in (t.get("dur") or {}).items())
    for start, (n, w) in bins:
        span = "early" if start + 300 <= early_end else "late" if start >= late_start else None
        if span:
            spans[span][0] += n
            spans[span][1] += w
    if not bins or any(n < MIN_CURVE_GAMES for n, _ in spans.values()):
        return {}
    total, seen, median = sum(c[0] for _, c in bins), 0, 0
    for start, (n, _w) in bins:
        seen += n
        if seen >= total / 2:
            median = start + 150
            break
    early, late = spans["early"][1] / spans["early"][0], spans["late"][1] / spans["late"][0]
    return {"bracket": used, "early": [round(early, 3), spans["early"][0]], "late": [round(late, 3), spans["late"][0]],
            "early_end_s": int(early_end), "late_start_s": int(late_start), "median_s": median,
            "lean": "late" if late - early >= CURVE_LEAN else "early" if early - late >= CURVE_LEAN else ""}


def item_timing(buckets: list[list[int]], mode: Any = "normal") -> dict:
    """One item's timing fact from its buckets ([[by second, games, wins], ...]): the target time (when half of
    its buyers have it) and the win rate of those who had it by then against those who got it later. Turbo:
    the target is halved (an estimate, from normal games: `turbo_estimate`). {} without enough games either
    side."""
    rows = sorted(b for b in buckets if len(b) == 3)
    total = sum(b[1] for b in rows)
    if total < MIN_TIMING_GAMES:
        return {}
    seen, target = 0, 0
    for t, n, _w in rows:
        seen += n
        if seen >= total / 2:
            target = t
            break
    by = [sum(b[1] for b in rows if b[0] <= target), sum(b[2] for b in rows if b[0] <= target)]
    after = [sum(b[1] for b in rows if b[0] > target), sum(b[2] for b in rows if b[0] > target)]
    if by[0] < MIN_BUCKET_GAMES or after[0] < MIN_BUCKET_GAMES:
        return {}
    turbo = _mode_key(mode) == "turbo"
    return {"target_s": int(round(target / 2)) if turbo else target, "normal_target_s": target,
            "by": [round(by[1] / by[0], 3), by[0]], "after": [round(after[1] / after[0], 3), after[0]],
            "n": total, "turbo_estimate": turbo}


def _spike(buckets: list[list[int]], base: float) -> dict:
    """The item finished in its earliest quarter of buyers, against the hero's win rate: a power spike when it
    wins SPIKE_GAIN more."""
    rows = sorted(buckets)
    total = sum(b[1] for b in rows)
    n = w = by = 0
    for t, bn, bw in rows:
        n, w, by = n + bn, w + bw, t
        if n >= total / 4:
            break
    if n < MIN_SPIKE_GAMES or not total or w / n - base < SPIKE_GAIN:
        return {}
    return {"by_s": by, "wr": round(w / n, 3), "n": n}


def card(hero: Any, role: Any = None, bracket: int = 0, mode: Any = "normal", enemies: Iterable[Any] = (),
         cache_dir: Path | None = None, data: dict | None = None) -> dict | None:
    """One hero's card for one role, rank and mode: a small structured record (see the module docstring), or
    None when the hero hasn't been studied. `data`: a card already loaded (tests, the study tool)."""
    c = data if data is not None else load(hero, cache_dir)
    if not c:
        return None
    m = _mode_key(mode)
    key, why = _role_key(c, role)
    r = (c.get("roles") or {}).get(key) or {}
    names = c.get("item_names") or {}

    def named(entries: list[dict]) -> list[dict]:
        return [{**e, "name": names.get(e["item"], e["item"]),
                 "by_s": int(round(e["by_s"] / 2)) if m == "turbo" and e["by_s"] > 0 else e["by_s"]}
                for e in entries]
    build = {ph: named((r.get("build") or {}).get(ph) or []) for ph in ("start", "boots", "early", "mid", "late")}
    # Key items: the role's dearest common items with public timing data.
    finished = [e for ph in ("early", "mid", "late") for e in (r.get("build") or {}).get(ph) or []]
    costs = c.get("item_costs") or {}
    key_items = [e["item"] for e in sorted(finished, key=lambda e: -e["share"])
                 if costs.get(e["item"], 0) >= KEY_ITEM_COST and e["item"] in (c.get("timings") or {})][:KEY_ITEMS]
    timings = []
    for item in key_items:
        fact = item_timing(c["timings"][item]["buckets"], m)
        if fact:
            timings.append({"item": item, "name": names.get(item, item), **fact})
    base_used, base_t = _bracket_table(c, 0, "normal")
    base = base_t["w"] / base_t["n"] if base_t else 0.5
    spikes = []
    if m == "normal":  # (the public timings are normal games')
        for item in key_items:
            s = _spike(c["timings"][item]["buckets"], base)
            if s:
                spikes.append({"kind": "item", "item": item, "name": names.get(item, item), "base": round(base, 3),
                               **s})
    skills = r.get("skills") or {}
    if skills.get("ult"):
        spikes.insert(0, {"kind": "ult", "points": skills.get("ult_points") or [], "ability": skills["ult"],
                          "n": skills.get("n", 0)})
    for t in skills.get("talents") or []:
        spikes.append({"kind": "talent", "level": t["level"], "name": t["name"], "share": t["share"], "n": t["n"]})
    used, t = _bracket_table(c, bracket, m)
    notes = c.get("patch_notes") or {}
    item_set = {e["item"] for ph in ("boots", "early", "mid", "late") for e in build[ph]}
    item_notes = [line for line in notes.get("items") or []
                  if any(f": {names.get(i, i)}:" in line for i in item_set)]
    return {
        "hero_id": c["hero_id"], "hero": c.get("name"), "patch": c.get("patch"), "major": c.get("major"),
        "basis": c.get("basis") or {}, "mode": m, "role": key, "role_label": role_label(key), "role_note": why,
        "role_games": r.get("games", 0), "role_share": r.get("share", 0.0),
        "role_wr": round(r["wins"] / r["games"], 3) if r.get("games") else None,
        "bracket": int(bracket or 0), "bracket_used": used,
        "win_rate": [round(t["w"] / t["n"], 3), t["n"]] if t else None,
        "build": build, "timings": timings, "spikes": spikes, "skills": skills,
        "matchups": matchups(c, bracket, m, enemies, cache_dir), "game_length": game_length(c, bracket, m),
        "pace": pace(c["hero_id"], key or role, bracket, m, cache_dir, data=c),
        "patch_notes": (notes.get("hero") or [])[:PATCH_NOTES_SHOWN], "item_notes": item_notes[:2],
    }


def _at(table: list[list[float]], p: float) -> float:
    """A benchmark value at percentile p, read between the table's rows."""
    if p <= table[0][0]:
        return table[0][1]
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if p <= p1:
            return v0 + (v1 - v0) * (p - p0) / (p1 - p0) if p1 > p0 else v1
    return table[-1][1]


def _pct_of(table: list[list[float]], value: float) -> float:
    """The percentile at which a benchmark reaches `value` (the inverse of `_at`)."""
    if value <= table[0][1]:
        return table[0][0]
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if value <= v1:
            return p0 + (p1 - p0) * (value - v0) / (v1 - v0) if v1 > v0 else p1
    return table[-1][0]


def support_share(c: dict) -> float | None:
    """The share of the hero's public players who are supports, read off its last-hit benchmark: players below
    the rate halfway between the hero's pro supports and pro cores (whole game) count as supports. 0 or 1 when
    pros play it only as a core or only as a support; None without the data."""
    roles = c.get("roles") or {}
    table = (c.get("bench") or {}).get(PACE_METRICS["lh"])
    core, sup = ((roles.get(k) or {}).get("pace") or {} for k in ("core", "support"))
    has_core, has_sup = core.get("n", 0) >= MIN_ROLE_GAMES, sup.get("n", 0) >= MIN_ROLE_GAMES
    if not table or not (has_core or has_sup):
        return None
    if not has_sup:
        return 0.0
    if not has_core:
        return 1.0
    return round(_pct_of(table, (core["lhpm"] + sup["lhpm"]) / 2), 3)


def pace(hero: Any, role: Any = None, bracket: int = 0, mode: Any = "normal", cache_dir: Path | None = None,
         data: dict | None = None) -> dict:
    """Last hits, GPM and XPM at 10:00 and 20:00: for players of this hero and role at your rank ("ref"), at the
    rank above ("up", "up_bracket"), and for pro players of the position ("pro", with games). {"available": False,
    "why": ...} when the data can't say (a Turbo game: there are no Turbo benchmarks; a hero not studied yet).
    Keys: {600: {"lh", "gpm", "xpm"}, 1200: {...}}.

    How (an estimate, and `basis` says so): OpenDota's benchmarks are whole-game per-minute percentiles of all of
    a hero's public players, cores and supports together. The supports are the bottom `support_share` of them
    (see there), so a core of a rank is read at its rank's place on the ladder (`rank_percentiles`) within the
    top part, a support within the bottom part. The whole-game rate is then turned into the 10:00 and 20:00 value
    with the pro games' own curve for the position (pace climbs as a game goes)."""
    c = data if data is not None else load(hero, cache_dir)
    if not c:
        return {"available": False, "why": "no_card"}
    if _mode_key(mode) == "turbo":
        return {"available": False, "why": "turbo"}
    key, _why = _role_key(c, role)
    roles = c.get("roles") or {}
    pro = (roles.get(key) or {}).get("pace") or {}
    bench = c.get("bench") or {}
    out: dict[str, Any] = {"available": True, "patch": c.get("patch"), "role": key, "bracket": int(bracket or 0)}
    if pro.get("n", 0) >= MIN_ROLE_GAMES and pro.get("n20", 0) >= MIN_ROLE_GAMES:
        out["pro"] = {600: {"lh": round(pro["lh10"]), "gpm": round(pro["g10"] / 10), "xpm": round(pro["x10"] / 10)},
                      1200: {"lh": round(pro["lh20"]), "gpm": round(pro["g20"] / 20), "xpm": round(pro["x20"] / 20)},
                      "n": pro["n"]}
    support = key in ("4", "5", "support")
    share = support_share(c)
    part = share if support else (1.0 - share if share is not None else 0.0)
    why = ("no_benchmarks" if not bench or share is None
           else "thin_pro_data" if not (out.get("pro") and pro.get("lhpm") and pro.get("gpm") and pro.get("xpm"))
           else "too_few_public_players_in_role" if part < 0.1 or key == "all" else "")
    if why:
        out["why"] = why
        return out if out.get("pro") else {**out, "available": False}

    def at(p: float) -> dict:
        p = p * share if support else share + p * (1.0 - share)  # (the rank's place within the role's part)
        res = {}
        for minute in PACE_MINUTES:
            lh_shape = pro[f"lh{minute}"] / (minute * pro["lhpm"])  # (the share of the whole-game rate by then)
            g_shape = (pro[f"g{minute}"] / minute) / pro["gpm"]
            x_shape = (pro[f"x{minute}"] / minute) / pro["xpm"]
            # (Last hits are capped at the pros' own: the top of a whole-game benchmark is stomps, not skill.)
            res[minute * 60] = {"lh": round(min(_at(bench[PACE_METRICS["lh"]], p) * minute * lh_shape,
                                                pro[f"lh{minute}"])),
                                "gpm": round(_at(bench[PACE_METRICS["gpm"]], p) * g_shape),
                                "xpm": round(_at(bench[PACE_METRICS["xpm"]], p) * x_shape)}
        return res
    pct = c.get("rank_pct") or {}
    b = int(bracket or 0)
    if b and str(b) in pct:
        out["ref"], out["ref_pct"] = at(pct[str(b)]), pct[str(b)]
        if b < 8 and str(b + 1) in pct:
            out["up"], out["up_pct"], out["up_bracket"] = at(pct[str(b + 1)]), pct[str(b + 1)], b + 1
    else:
        out["ref"], out["ref_pct"] = at(0.5), 0.5  # all ranks: the role's middle player
    out["support_share"] = share
    out["basis"] = "benchmarks_by_rank_place"  # (an estimate: see the docstring)
    return out


# --- the compact block for the AI -----------------------------------------------------------------------------
def _clock(seconds: int) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _games(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 10000 else f"{n:,}"


_REASON_TEXT = {
    "their_silence": "their {0} silences a spell hero",
    "their_dispel": "their {0} dispels your {1}",
    "their_bkb_disable": "their {0} goes through BKB",
    "their_invis": "{0}: invisible, carry detection",
    "your_silence": "your {0} silences a spell hero",
    "your_dispel": "your {0} dispels their {1}",
}


def _reason(why: list[str]) -> str:
    if not why or why[0] not in _REASON_TEXT:
        return ""
    return " (" + _REASON_TEXT[why[0]].format(*why[1:], "", "") + ")"


def prompt_block(hero: Any, role: Any = None, bracket: int = 0, mode: Any = "normal", enemies: Iterable[Any] = (),
                 cache_dir: Path | None = None, data: dict | None = None, budget: int = PROMPT_WORDS) -> str:
    """The card as a few tight lines for the coach's prompt (about 120-180 words), the most decision-relevant
    facts first: this lineup's matchups, the build with its timings, pace, spikes, skills, game length, patch
    changes. Names the patch and what the numbers are based on. '' when the hero hasn't been studied."""
    v = card(hero, role, bracket, mode, enemies, cache_dir, data)
    if not v or not v.get("role"):
        return ""
    turbo = v["mode"] == "turbo"
    basis = v.get("basis") or {}
    rank = BRACKET_NAMES.get(v["bracket_used"], "All ranks")
    head = f"HERO CARD {v['hero']}, patch {v['patch']}"
    src = [f"{rank} public games ({basis['public']})" if basis.get("public") else "",
           f"pro builds {basis['pro']}" if basis.get("pro") else ""]
    head += " (" + "; ".join(s for s in src if s) + ")" if any(src) else ""
    if turbo:
        head += ". TURBO: times halved from normal games (estimate)"
    lines = [head + "."]
    mu = v.get("matchups") or {}
    if mu.get("lineup"):
        lines.append("Vs this lineup: " + "; ".join(
            f"{r['name']} {r['delta'] * 100:+.1f} pts ({_games(r['n'])} g){_reason(r['why'])}"
            for r in mu["lineup"]) + ".")
    who = (f"{ROLE_NAMES[int(v['role'])]}, pos {v['role']}" if v["role"].isdigit()
           else v["role_label"].split(" (")[0])
    role_line = f"As {who} ({v['role_games']} pro g, {_pct(v['role_share'])} of its pro games)"
    b = v["build"]
    parts = []
    if b["boots"]:
        parts.append("/".join(f"{e['name']} {_pct(e['share'])}" for e in b["boots"][:2]))
    for ph in ("early", "mid", "late"):
        parts += [f"{e['name']} {_pct(e['share'])} by {_clock(e['by_s'])}" for e in b[ph][:3]]
    if parts:
        lines.append(role_line + ": " + ", ".join(parts) + ".")
    for t in v.get("timings") or []:
        lines.append(f"{t['name']}: by {_clock(t['target_s'])} wins {_pct(t['by'][0])} ({_games(t['by'][1])} g), "
                     f"later {_pct(t['after'][0])} ({_games(t['after'][1])} g).")
    p = v.get("pace") or {}
    if p.get("available") and p.get("ref"):
        ref, up = p["ref"], p.get("up")
        text = f"Pace {rank}: 10:00 {ref[600]['lh']} LH/{ref[600]['gpm']} GPM, 20:00 {ref[1200]['lh']} LH"
        if up:
            text += f"; {BRACKET_NAMES[p['up_bracket']]} 10:00 {up[600]['lh']} LH/{up[600]['gpm']} GPM"
        if p.get("pro"):
            text += f"; pros 10:00 {p['pro'][600]['lh']} LH ({p['pro']['n']} g)"
        lines.append(text + " (estimate).")
    elif p.get("available") and p.get("pro"):
        pro = p["pro"]
        lines.append(f"Pace of pros in this role: 10:00 {pro[600]['lh']} LH/{pro[600]['gpm']} GPM/"
                     f"{pro[600]['xpm']} XPM, "
                     f"20:00 {pro[1200]['lh']} LH/{pro[1200]['gpm']} GPM ({pro['n']} g).")
    spikes = []
    for s in v.get("spikes") or []:
        if s["kind"] == "ult" and s["points"]:
            spikes.append(f"ult {s['ability']} (skill points {'/'.join(map(str, s['points']))})")
        elif s["kind"] == "item":
            spikes.append(f"{s['name']} by {_clock(s['by_s'])} wins {_pct(s['wr'])} vs {_pct(s['base'])}")
        elif s["kind"] == "talent":
            spikes.append(f"L{s['level']} {s['name']} ({_pct(s['share'])})")
    if spikes:
        lines.append("Spikes: " + "; ".join(spikes[:4]) + ".")
    sk = v.get("skills") or {}
    if sk.get("max_order"):
        lines.append("Skills: max " + " > ".join(sk["max_order"]) + ".")
    worst, best = mu.get("worst") or [], mu.get("best") or []
    if worst or best:
        lines.append("Matchups " + rank + ": worst " + ", ".join(
            f"{r['name']} {r['delta'] * 100:+.1f}{_reason(r['why'])}" for r in worst[:2])
            + ("; best " + ", ".join(f"{r['name']} {r['delta'] * 100:+.1f}" for r in best[:2]) if best else "") + ".")
    gl = v.get("game_length") or {}
    if gl:
        lean = {"late": "scales: wins more long games", "early": "wants it short"}.get(gl["lean"], "no clear lean")
        lines.append(f"Length: {lean} (<{gl['early_end_s'] // 60}m {_pct(gl['early'][0])}, "
                     f">{gl['late_start_s'] // 60}m {_pct(gl['late'][0])}).")
    for note in (v.get("patch_notes") or [])[:2] + (v.get("item_notes") or [])[:1]:
        lines.append("Patch " + (note if len(note) <= 90 else note[:88].rsplit(" ", 1)[0] + "…"))
    out: list[str] = []
    words = 0
    for line in lines:
        n = len(line.split())
        if words + n > budget:
            continue  # (a later, shorter line may still fit)
        out.append(line)
        words += n
    return "\n".join(out)
