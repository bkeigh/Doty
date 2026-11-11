""""Wrong tip?" reports become test cases the bench runs forever after.

The app's "Wrong tip?" button (coach.llm.Coach.report) saves a folder per line: report.json (what the coach was
told, its whole answer, the line shown) and the screenshots. `convert_report` turns one into a small fixture that
keeps only what judging the line needs, with nothing that says who played: no account, match id, player name,
question, model or timestamp, and no screenshots.

Fixture format (one JSON object per file, format 1):

    {
      "fixture": 1,
      "kind": "watch",                     # the kind of answer: watch, now, death, start, question...
      "clock": 1213,                       # game clock in seconds when it was said
      "hero": "npc_dota_hero_juggernaut",  # the player's hero
      "line": "Buy Phase Boots now.",      # the line as shown (the coach's own words)
      "said": true,                        # heard out loud (false: shown only)
      "state": {                           # the game at that moment; leave out what isn't known
        "alive": true,
        "fighting": false,
        "gold": 3000,                      # gold free to spend
        "items": ["item_boots_of_bearing"],  # held items (inventory, backpack, stash): keys, or names as shown
        "tp": true,                        # a TP scroll in the TP slot
        "passed_on": ["item_black_king_bar"],  # items passed on this game (keys or names)
        "heroes": ["npc_dota_hero_lion"]   # everyone in the match, only when the lineup was confirmed
      },
      "expect": "fail",                    # what the bench must say about the line: "fail" or "pass"
      "checks": ["contradicts_inventory"], # with "fail": these checkers must fail it ([]: none catches it yet)
      "note": "why the player said it was wrong, in a few words"
    }

`run_fixture` judges the line with every checker and says whether the expectation holds: "ok", "regressed" (a
checker that caught it no longer does, or a line that should pass now fails), "uncaught" (a known wrong line no
checker catches yet: a gap to close) or "caught" (it was uncaught and now a checker catches it: update "checks").
It also asks the coach's own vetting whether it would still let the line through ("coach": "drops" / "keeps"),
so a mistake fixed in the coach stays fixed.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..state import IN_PROGRESS, GameStateStore
from .checks import CHECKERS
from .model import Context, Line, Match
from .score import judge

FORMAT = 1
ANNOTATION = re.compile(r"\s*\((cd \d+s|\d+ charges)\)\s*$")


def _item_key(name: str, kb) -> str | None:
    name = ANNOTATION.sub("", str(name)).strip()
    if not name:
        return None
    if name.startswith("item_"):
        return name
    if kb is not None and kb.items:
        for key, data in kb.items.items():
            if str(data.get("dname") or "") == name:
                return key
    return name  # (no item data: kept as shown, resolved when the fixture is run)


def _hero_key(name: str, kb) -> str | None:
    name = str(name).strip()
    if name.startswith("npc_dota_hero_"):
        return name
    found = kb.find_hero(name) if kb is not None else None
    return found or None


def convert_report(folder: Path, kb=None, note: str = "") -> dict[str, Any]:
    """A fixture from a "Wrong tip?" report folder (or its report.json). The player pressed the button, so the
    line is expected to fail; "checks" records which checkers catch it today."""
    path = Path(folder)
    report = json.loads((path / "report.json" if path.is_dir() else path).read_text(encoding="utf-8"))
    state_in = report.get("state") if isinstance(report.get("state"), dict) else {}
    state: dict[str, Any] = {}
    if isinstance(state_in.get("alive"), bool):
        state["alive"] = state_in["alive"]
    state["fighting"] = bool(state_in.get("in_fight", False))
    if isinstance(state_in.get("gold"), int):
        state["gold"] = state_in["gold"]
    held = [*(state_in.get("items") or []), *(state_in.get("backpack") or []), *(state_in.get("stash") or [])]
    if "items" in state_in:
        state["items"] = [k for k in (_item_key(n, kb) for n in held) if k]
    if isinstance(state_in.get("tp"), str):
        state["tp"] = state_in["tp"] != "none"
    if state_in.get("passed_on"):
        state["passed_on"] = [k for k in (_item_key(n, kb) for n in state_in["passed_on"]) if k]
    if state_in.get("lineup_complete", True) and (state_in.get("allies") or state_in.get("enemies")):
        heroes = [_hero_key(n, kb) for n in [*(state_in.get("allies") or []), *(state_in.get("enemies") or [])]]
        if heroes and all(heroes):
            state["heroes"] = sorted(set(heroes))
    fixture = {
        "fixture": FORMAT,
        "kind": str(report.get("kind") or ""),
        "clock": int(report.get("clock") or 0),
        "hero": str(report.get("hero") or ""),
        "line": str(report.get("shown") or "").strip(),
        "said": True,
        "state": state,
        "expect": "fail",
        "checks": [],
        "note": note,
    }
    fixture["checks"] = sorted(failed_checks(fixture, kb))
    return fixture


def _context(fx: dict[str, Any], kb) -> Context:
    st = fx.get("state") or {}
    items = None
    if "items" in st:
        items = tuple(k for k in (_item_key(n, kb) for n in st["items"]) if k)
    passed = st.get("passed_on")
    heroes = st.get("heroes")
    return Context(
        clock=int(fx.get("clock") or 0), hero=str(fx.get("hero") or ""),
        owned=frozenset(items) if items is not None else None, items=items,
        gold=st.get("gold"), has_tp=st.get("tp"), alive=st.get("alive"), fighting=st.get("fighting"),
        skipped=frozenset(k for k in (_item_key(n, kb) for n in passed) if k) if passed is not None else frozenset(),
        heroes=frozenset(h for h in (_hero_key(n, kb) for n in heroes) if h) if heroes else None)


def as_match(fx: dict[str, Any], kb) -> Match:
    category = "trigger" if fx.get("kind") == "trigger" else "coach"
    line = Line(clock=int(fx.get("clock") or 0), text=str(fx.get("line") or ""), source="coach",
                rule=f"coach:{fx.get('kind') or ''}", priority=2, category=category,
                spoken=bool(fx.get("said", True)), ctx=_context(fx, kb))
    return Match(label=str(fx.get("name") or "fixture"), lines=[line], kind="fixture", kb=kb)


def failed_checks(fx: dict[str, Any], kb) -> set[str]:
    match = as_match(fx, kb)
    verdicts = judge(match.lines[0], match, CHECKERS.values())
    return {name for name, v in verdicts.items() if v}


def run_fixture(fx: dict[str, Any], kb) -> dict[str, Any]:
    failed = failed_checks(fx, kb)
    want = set(fx.get("checks") or [])
    if fx.get("expect", "fail") == "pass":
        status = "regressed" if failed else "ok"
    elif want:
        status = "ok" if want <= failed else "regressed"
    else:
        status = "caught" if failed else "uncaught"
    return {"name": fx.get("name", ""), "status": status, "failed": sorted(failed), "expected": sorted(want),
            "coach": coach_verdict(fx, kb)}


def load_fixture(path: Path) -> dict[str, Any]:
    fx = json.loads(Path(path).read_text(encoding="utf-8"))
    fx.setdefault("name", Path(path).stem)
    return fx


# the coach's own vetting, on the same moment -------------------------------------------------------------------------
def payload_from_fixture(fx: dict[str, Any], kb) -> dict[str, Any]:
    """A game-data update that says what the fixture's state says (for the coach's own checks)."""
    st = fx.get("state") or {}
    items = [k for k in (_item_key(n, kb) for n in st.get("items") or []) if k]
    slots = {f"slot{i}": {"name": n} for i, n in enumerate(items[:9])}
    slots.update({f"stash{i}": {"name": n} for i, n in enumerate(items[9:15])})
    slots["teleport0"] = ({"name": "item_tpscroll", "charges": 1, "can_cast": True, "cooldown": 0}
                          if st.get("tp") else {"name": "empty"})
    hero = str(fx.get("hero") or "")
    hero_id = int(((kb.heroes.get(hero) if kb is not None else None) or {}).get("id") or 1)
    alive = st.get("alive", True)
    return {"map": {"matchid": "bench-fixture", "clock_time": int(fx.get("clock") or 0),
                    "game_time": int(fx.get("clock") or 0) + 90, "game_state": IN_PROGRESS},
            "player": {"team_name": "radiant", "gold": int(st.get("gold") or 0)},
            "hero": {"name": hero, "id": hero_id, "alive": alive, "health": 1000 if alive else 0,
                     "max_health": 1000, "level": 10, "xpos": -1800, "ypos": -6400},
            "items": slots}


def coach_verdict(fx: dict[str, Any], kb) -> str | None:
    """"drops" when the coach's own vetting would keep the line from the player today, "keeps" when it would let
    it through, None when it can't be asked (no item data, or the coach's checks have changed shape)."""
    if kb is None or not kb.items:
        return None
    try:
        from ..advice import AdviceBus
        from ..coach import Coach
        from ..config import CoachConfig

        store = GameStateStore()
        snap = store.ingest(payload_from_fixture(fx, kb))
        saved = dict(kb.skipped)
        kb.skipped.clear()
        kb.skipped.update({k: "fixture" for k in (_item_key(n, kb) for n in (fx.get("state") or {}).get("passed_on")
                                                                         or []) if k})
        try:
            coach = Coach(CoachConfig(), store, AdviceBus(), kb)
            line = str(fx.get("line") or "")
            if fx.get("kind") == "watch":
                dropped = bool(coach._vet_tip("watch", line, snap, snap, time.perf_counter(), True))
            else:  # an answer read while it streams loses its bad sentences instead
                dropped = coach._without_bad_sentences(line, snap, complete=True).strip() != line.strip()
        finally:
            kb.skipped.clear()
            kb.skipped.update(saved)
        return "drops" if dropped else "keeps"
    except Exception:
        return None
