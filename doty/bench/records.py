"""Scoring real games from Doty's match records (match_record.py): what the coach really said, judged against the
game as it really stood, with every checker.

A record folder holds the game data (gsi.jsonl.gz), every line Doty showed (advice.jsonl: clock, source, rule,
priority, category, whether it was said) and match.json (hero, team, mode, the lineup once known). The game data
is replayed through the store, the build tracker and the game mode, without the rules (the lines are the ones
already said), and each line gets the state of the moment it was said, joined on wall time. With the lineup
confirmed, a hero who isn't in the match can be caught too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..match_record import ADVICE_FILE, INFO_FILE, list_records, record_folder
from .logs import line_from_row
from .model import Match
from .replay import Replay, attach_states, read_recording


def is_record(path: Path) -> bool:
    return Path(path).is_dir() and (Path(path) / INFO_FILE).exists()


def read_info(folder: Path) -> dict[str, Any]:
    try:
        info = json.loads((Path(folder) / INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def lineup_of(info: dict[str, Any]) -> list[str] | None:
    """Everyone in the match (npc names), when the lineup was confirmed; else None."""
    lineup = info.get("lineup") or {}
    if not lineup.get("complete"):
        return None
    heroes = list(lineup.get("radiant") or []) + list(lineup.get("dire") or [])
    return heroes or None


def record_match(folder: Path, kb=None) -> Match | None:
    """The match in a record folder, its lines carrying the recorded state. None if it holds no lines."""
    folder = Path(folder)
    info = read_info(folder)
    rows = []
    try:
        with (folder / ADVICE_FILE).open(encoding="utf-8") as fh:
            for raw in fh:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("clock"), int) and row.get("text"):
                    rows.append(row)
    except OSError:
        return None
    heroes = lineup_of(info)
    replay = Replay(kb, rules=False, label=folder.name, heroes=heroes)
    try:
        replay.run(read_recording(folder))
    except OSError:
        return None
    lines = [line_from_row(row, None, i) for i, row in enumerate(rows)]
    got = attach_states(lines, replay.timeline)
    clocks = [line.clock for line in lines] + [ctx.clock for _t, _g, ctx in replay.timeline]
    if not clocks:
        return None
    m = Match(label=folder.name, lines=lines, kind="record", kb=kb, start_clock=min(clocks),
              end_clock=max([int(info.get("duration") or 0)] + clocks))
    m.notes.append(f"match record: {got} of {len(lines)} lines matched to the game state")
    if heroes is None:
        m.notes.append("lineup not confirmed: heroes not in the match can't be checked")
    return m


def latest(root: Path | None = None, n: int = 1) -> list[Path]:
    """The `n` most recent record folders, newest last."""
    found = list_records(Path(root) if root is not None else record_folder())
    return found[-max(1, n):]


def last_game_summary(root: Path | None = None, kb=None, folder: Path | None = None) -> str:
    """"How was the coach last game", in two or three plain sentences, from the latest match record (or `folder`).
    For the app to show after a match; '' when there's no record to judge."""
    from . import score

    folder = folder if folder is not None else next(iter(latest(root, 1)), None)
    match = record_match(folder, kb) if folder is not None else None
    if match is None or not match.lines:
        return ""
    card = score.score_match(match)
    said, minutes = card["said"], card["minutes"]
    out = [f"Last game Doty said {said} line{'s' if said != 1 else ''} in {minutes:g} minutes"
           + (f" ({card['per_minute']:g} a minute)" if card["per_minute"] else "")
           + (f", and scored {card['score']:g} out of 100 on its own checks." if card["score"] is not None else ".")]
    wrong = {name: c["failed"] for name, c in card["checks"].items() if c["failed"] and c["severity"] >= 3}
    if wrong:
        words = {"contradicts_inventory": "told you to buy something you had",
                 "not_affordable": "said buy now without the gold", "tp_nag": "asked for a TP scroll you held",
                 "passed_on": "pushed an item you passed on", "format_leak": "showed garbled text"}
        parts = [f"{words.get(name, name)} ({n} time{'s' if n != 1 else ''})" for name, n in sorted(wrong.items())]
        out.append("Wrong: " + "; ".join(parts) + ".")
    else:
        out.append("Nothing it said contradicted the game data.")
    noise = card["checks"].get("repeats", {}).get("failed", 0)
    if noise:
        out.append(f"It repeated itself {noise} time{'s' if noise != 1 else ''}.")
    return " ".join(out)
