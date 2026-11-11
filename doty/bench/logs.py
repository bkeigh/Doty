"""Scoring what Doty really said in past games, from the files every session already leaves behind.

A session log (`%LOCALAPPDATA%\\Doty\\logs\\session-*.jsonl`, written by app.Doty._session_log) has one line per
finished card shown: {"t": unix time, "clock": game clock or null, "source", "category", "priority", "text",
"latency_ms"}. One file can hold several matches; they're told apart by the clock going back (a new game starts
before the horn) or a long gap in wall time.

The match's review (`reviews/match-*.json`, review.MatchReview) adds what the log doesn't have: the hero, when
each big item arrived ("bought": [[item key, clock], ...]) and each death with whether a TP scroll was held
("deaths": [{"clock", "had_tp", ...}]). A review is matched to a log segment by the time it was saved and the
match's length.

What this can judge: repeats, stale clock times, control tags and changes of mind, an item named too often,
being told to buy an item already bought (big items only: the review doesn't list parts), "buy a TP" in the
seconds after a death with a TP held, "buy X now" against the gold the line itself names, and lines per minute.
What it can't: anything needing the gold, the inventory or HP at the moment (no game data was recorded), whether
a line was voiced or only shown (the log doesn't say: lines at NORMAL priority or above count as said), heroes
in the match.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Context, Line, Match

NEW_MATCH_BACK_S = 60  # the clock going back this far means a new game (GameStateStore.BACKWARDS_S)
NEW_MATCH_GAP_S = 15 * 60  # or this long with nothing said
REVIEW_AFTER_S = 45 * 60  # a review is saved when its match ends: at most this long after the last line
DEATH_TP_S = 20  # after a death, the TP slot is as it was at the death for this long (the death answer's window)


def read_session_log(path: Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except ValueError:
                continue  # (a line cut off when the app closed)
            if isinstance(row, dict) and isinstance(row.get("text"), str):
                rows.append(row)
    return rows


def split_matches(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The log's lines, one list per match. Lines with no clock (said outside a game) are left out."""
    out: list[list[dict[str, Any]]] = []
    last_clock: int | None = None
    last_t: float | None = None
    for row in rows:
        clock = row.get("clock")
        if not isinstance(clock, int):
            continue
        t = float(row.get("t") or 0.0)
        new = (last_clock is None or clock < last_clock - NEW_MATCH_BACK_S
               or (last_t is not None and t - last_t > NEW_MATCH_GAP_S)
               # (newer logs number the session's matches: a change is a new match, whatever the clock does)
               or ("match" in row and out and out[-1] and row["match"] != out[-1][-1].get("match", row["match"])))
        if new:
            out.append([])
        out[-1].append(row)
        # (against the latest clock seen: a line stamped a little early, the coach's late answer, isn't a new game)
        last_clock = clock if new or last_clock is None else max(clock, last_clock)
        last_t = t
    return out


def load_reviews(folder: Path | None) -> list[dict[str, Any]]:
    if folder is None or not Path(folder).is_dir():
        return []
    found = []
    for path in sorted(Path(folder).glob("match-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and not data.get("demo"):
            found.append(data)
    return found


def review_for(segment: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The review saved when this stretch of the log ended, if there is one: saved after its first line and
    soon after its last, for a match about as long as the log says."""
    first_t, last_t = float(segment[0].get("t") or 0), float(segment[-1].get("t") or 0)
    last_clock = max(int(r["clock"]) for r in segment)
    best, gap = None, None
    for r in reviews:
        saved = float(r.get("saved_at") or 0)
        if not first_t <= saved <= last_t + REVIEW_AFTER_S:
            continue
        if abs(int(r.get("duration") or 0) - last_clock) > 240:
            continue
        g = abs(saved - last_t)
        if gap is None or g < gap:
            best, gap = r, g
    return best


def _contexts(review: dict[str, Any] | None, kb):
    """A function clock -> Context from what the review knows."""
    if review is None:
        return lambda clock: Context(clock=clock)
    hero = str(review.get("hero") or "")
    bought = []
    # Every purchase, parts included (newer reviews), else the big items only.
    for entry in review.get("purchases") or review.get("bought") or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            bought.append((str(entry[0]), int(entry[1])))
    if not bought and kb is not None:  # (older reviews: only display names)
        by_name = {str(v.get("dname")): k for k, v in kb.items.items() if v.get("dname")}
        bought = [(by_name[n], int(c)) for n, c in review.get("items") or [] if n in by_name]
    deaths = [(int(d.get("clock") or 0), d.get("had_tp")) for d in review.get("deaths") or [] if isinstance(d, dict)]
    from .checks import BOUGHT_GRACE_S

    def at(clock: int) -> Context:
        have = [item for item, c in bought if c <= clock - BOUGHT_GRACE_S]
        tp = None
        for died, had in deaths:
            if died <= clock <= died + DEATH_TP_S and had is not None:
                tp = bool(had)
        return Context(clock=clock, hero=hero, owned=frozenset(have), items=tuple(have), has_tp=tp)
    return at


def log_matches(path: Path, kb=None, reviews_dir: Path | None = None, min_minutes: float = 2.0) -> list[Match]:
    """One Match per game in a session log, with what its review adds. Stretches shorter than `min_minutes` of
    game time (a launch, a menu, a demo started and stopped) are left out."""
    path = Path(path)
    if reviews_dir is None:
        reviews_dir = path.parent.parent / "reviews"  # (logs and reviews sit side by side in Doty's data folder)
    reviews = load_reviews(reviews_dir)
    out: list[Match] = []
    for n, segment in enumerate(split_matches(read_session_log(path)), 1):
        clocks = [int(r["clock"]) for r in segment]
        review = review_for(segment, reviews)
        start = int(review.get("recorded_from", min(clocks))) if review else min(clocks)
        end = max(int(review.get("duration") or 0), max(clocks)) if review else max(clocks)
        if (end - max(start, 0)) / 60.0 < min_minutes:
            continue
        ctx_at = _contexts(review, kb)
        m = Match(label=f"{path.stem}#{n}", kind="log", kb=kb, start_clock=start, end_clock=end)
        m.notes.append("session log: no game state" + ("" if any("speak" in r for r in segment) else
                                                       "; whether a line was voiced is estimated from its priority"))
        m.notes.append("with its review: purchase times and TP at deaths" if review else
                       "no review found: inventory and TP checks skipped")
        for row in segment:
            m.lines.append(line_from_row(row, ctx_at(int(row["clock"])), len(m.lines)))
        out.append(m)
    return out


def line_from_row(row: dict[str, Any], ctx: Context | None, index: int, voiced_max: int = 3) -> Line:
    """A line as a session log or a match record wrote it. Newer files say which rule said it and whether it was
    meant to be heard ("spoken" in a record, "speak" in a session log); older ones don't, and then a line at
    NORMAL priority or above counts as said."""
    priority = int(row.get("priority") or 3)
    source, category = str(row.get("source") or ""), str(row.get("category") or "general")
    if isinstance(row.get("spoken"), bool):
        spoken = row["spoken"]
    elif isinstance(row.get("speak"), bool):
        coach_card = source == "coach" and category == "coach" and priority <= 2  # (voiced a sentence at a time)
        spoken = coach_card or (row["speak"] and priority <= voiced_max)
    else:
        spoken = None
    return Line(clock=int(row.get("clock") or 0), text=str(row.get("text") or "").strip(), source=source,
                rule=str(row.get("rule") or ""), priority=priority, category=category, spoken=spoken, ctx=ctx,
                index=index, t=float(row.get("t") or 0.0))


def is_session_log(path: Path) -> bool:
    """A session log, rather than a game-data recording (whose lines are {"t", "payload"})."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            for raw in fh:
                if raw.strip():
                    row = json.loads(raw)
                    return isinstance(row, dict) and "text" in row and "payload" not in row
    except (OSError, ValueError):
        return False
    return False
