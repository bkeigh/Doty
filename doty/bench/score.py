"""The scorecard: every line through every checker, summed up per match and overall.

The headline number is the score: 100 minus the share of lines that failed a check, each weighted by its worst
failure's severity (a severity-3 failure costs a whole line, a severity-1 failure a third of one). Next to it,
the plan's own first bar (docs/ACCURACY_PLAN.md, A3): the share of lines failing any check (target under 2%) and
of lines heard that repeat (target under 10%).

The JSON form is for machines and for `compare`: matches sorted by label, failures by clock, numbers rounded, no
ids or timestamps, so two runs over the same input are identical and a diff shows only what changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .checks import CHECKERS, Checker, _mmss
from .model import Line, Match

FORMAT = 1
WORST_N = 12


def judge(line: Line, match: Match, checkers: Iterable[Checker]) -> dict[str, str | None]:
    """Each checker's verdict on one line: None (not applicable), "" (passed) or the reason it failed."""
    out: dict[str, str | None] = {}
    for c in checkers:
        try:
            out[c.name] = c.run(line, match)
        except Exception as exc:  # a broken checker is reported, never takes the bench down
            out[c.name] = f"checker error: {exc.__class__.__name__}: {exc}"
    return out


def _empty_counts(checkers: list[Checker]) -> dict[str, dict[str, Any]]:
    return {c.name: {"severity": c.severity, "checked": 0, "failed": 0} for c in checkers}


def score_match(match: Match, checkers: list[Checker] | None = None) -> dict[str, Any]:
    checkers = checkers if checkers is not None else list(CHECKERS.values())
    match.renumber()
    match.memo.clear()
    counts = _empty_counts(checkers)
    fails: list[dict[str, Any]] = []
    scored = failing = 0
    penalty = 0.0
    for line in match.lines:
        verdicts = judge(line, match, checkers)
        applicable = {k: v for k, v in verdicts.items() if v is not None}
        if not applicable:
            continue
        scored += 1
        worst = 0
        for c in checkers:
            v = verdicts[c.name]
            if v is None:
                continue
            counts[c.name]["checked"] += 1
            if v:
                counts[c.name]["failed"] += 1
                worst = max(worst, c.severity)
                fails.append({"clock": line.clock, "at": _mmss(line.clock), "checker": c.name,
                              "severity": c.severity, "source": line.source, "rule": line.rule,
                              "said": line.said, "text": line.text, "reason": v})
        if worst:
            failing += 1
            penalty += worst / 3.0
    said = sum(1 for line in match.lines if line.said)
    fails.sort(key=lambda f: (f["clock"], f["checker"], f["text"]))
    card = {
        "label": match.label,
        "kind": match.kind,
        "minutes": round(match.minutes, 1),
        "lines": len(match.lines),
        "said": said,
        "per_minute": round(said / match.minutes, 2) if match.minutes > 0 else None,
        "scored": scored,
        "failing": failing,
        "penalty": round(penalty, 3),
        "notes": list(dict.fromkeys(match.notes)),
        "checks": counts,
        "fails": fails,
    }
    _finish(card)
    return card


def _finish(card: dict[str, Any]) -> None:
    """The rates and the headline, from the counts (the same for a match and for the total)."""
    scored = card["scored"]
    card["fail_rate"] = round(100.0 * card["failing"] / scored, 1) if scored else None
    card["score"] = round(100.0 * (1 - card["penalty"] / scored), 1) if scored else None
    for counts in card["checks"].values():
        checked = counts["checked"]
        counts["pass_rate"] = round(100.0 * (checked - counts["failed"]) / checked, 1) if checked else None
    rep = card["checks"].get("repeats")
    card["repeat_rate"] = (round(100.0 * rep["failed"] / card["said"], 1)
                           if rep is not None and card["said"] else None)
    card["worst"] = sorted(card["fails"], key=lambda f: (-f["severity"], f["clock"], f["checker"]))[:WORST_N]


def scorecard(matches: Iterable[Match], checkers: list[Checker] | None = None) -> dict[str, Any]:
    checkers = checkers if checkers is not None else list(CHECKERS.values())
    cards = sorted((score_match(m, checkers) for m in matches), key=lambda c: c["label"])
    total: dict[str, Any] = {"label": "overall", "kind": "all", "lines": 0, "said": 0, "scored": 0, "failing": 0,
                             "penalty": 0.0, "minutes": 0.0, "checks": _empty_counts(checkers), "fails": []}
    for card in cards:
        for key in ("lines", "said", "scored", "failing", "penalty", "minutes"):
            total[key] += card[key]
        for name, counts in card["checks"].items():
            total["checks"][name]["checked"] += counts["checked"]
            total["checks"][name]["failed"] += counts["failed"]
        total["fails"] += [dict(f, match=card["label"]) for f in card["fails"]]
    total["minutes"] = round(total["minutes"], 1)
    total["penalty"] = round(total["penalty"], 3)
    total["per_minute"] = round(total["said"] / total["minutes"], 2) if total["minutes"] > 0 else None
    _finish(total)
    del total["fails"]  # (each match lists its own)
    return {"bench": FORMAT,
            "checkers": {c.name: {"severity": c.severity, "about": c.about, "spoken_only": c.spoken_only}
                         for c in checkers},
            "overall": total, "matches": cards}


# reading it --------------------------------------------------------------------------------------------------------
def _pct(v) -> str:
    return "  -  " if v is None else f"{v:5.1f}"


def render(card: dict[str, Any], worst: int = 8) -> str:
    """The scorecard as a compact table for a person."""
    o = card["overall"]
    out = [f"Doty bench: {len(card['matches'])} match(es), {o['said']} lines said of {o['lines']} shown, "
           f"{o['minutes']} min, {o['per_minute'] if o['per_minute'] is not None else '-'} a minute",
           f"SCORE {o['score'] if o['score'] is not None else '-'}   lines failing a check {_pct(o['fail_rate'])}%"
           f" (target < 2%)   repeats {_pct(o['repeat_rate'])}% of lines said (target < 10%)", ""]
    out.append(f"{'checker':<22}{'sev':>4}{'checked':>9}{'failed':>8}{'pass %':>8}")
    for name, c in o["checks"].items():
        out.append(f"{name:<22}{c['severity']:>4}{c['checked']:>9}{c['failed']:>8}{_pct(c['pass_rate']):>8}")
    for m in card["matches"]:
        out += ["", f"{m['label']} ({m['kind']}): score {m['score'] if m['score'] is not None else '-'}, "
                    f"{m['said']} said / {m['lines']} shown in {m['minutes']} min "
                    f"({m['per_minute'] if m['per_minute'] is not None else '-'} a minute), "
                    f"{m['failing']} lines failing"]
        for note in m["notes"]:
            out.append(f"  note: {note}")
        bad = [f"{n} {c['failed']}" for n, c in m["checks"].items() if c["failed"]]
        if bad:
            out.append("  fails: " + ", ".join(bad))
        for f in m["worst"][:worst]:
            text = f["text"] if len(f["text"]) <= 110 else f["text"][:109] + "…"
            out.append(f"  {f['at']:>6} [{f['checker']}] {f['source']}: \"{text}\"")
            out.append(f"         {f['reason']}")
    return "\n".join(out)


def save(card: dict[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(card, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def compare(old: dict[str, Any], new: dict[str, Any], show: int = 10) -> str:
    """What got better or worse between two scorecards (e.g. before and after a prompt change)."""
    def arrow(a, b, higher_is_better=True) -> str:
        if a is None or b is None:
            return f"{a} -> {b}"
        d = round(b - a, 2)
        tag = "same" if d == 0 else ("better" if (d > 0) == higher_is_better else "worse")
        return f"{a} -> {b} ({'+' if d > 0 else ''}{d}, {tag})"

    o, n = old["overall"], new["overall"]
    out = ["overall",
           f"  score        {arrow(o['score'], n['score'])}",
           f"  failing %    {arrow(o['fail_rate'], n['fail_rate'], False)}",
           f"  repeats %    {arrow(o.get('repeat_rate'), n.get('repeat_rate'), False)}",
           f"  said         {o['said']} -> {n['said']}",
           f"  a minute     {arrow(o['per_minute'], n['per_minute'], False)}",
           "checkers (failed lines)"]
    for name in sorted(set(o["checks"]) | set(n["checks"])):
        a, b = o["checks"].get(name, {}).get("failed"), n["checks"].get(name, {}).get("failed")
        if a != b:
            out.append(f"  {name:<22}{arrow(a, b, False)}")
    old_m = {m["label"]: m for m in old["matches"]}
    new_m = {m["label"]: m for m in new["matches"]}
    for label in sorted(set(old_m) | set(new_m)):
        a, b = old_m.get(label), new_m.get(label)
        if a is None or b is None:
            out.append(f"{label}: only in the {'new' if a is None else 'old'} run")
            continue
        if a["score"] != b["score"] or a["failing"] != b["failing"]:
            out.append(f"{label}: score {arrow(a['score'], b['score'])}")

    def keys(card) -> dict[tuple, dict]:
        return {(m["label"], f["checker"], f["at"], f["text"]): f for m in card["matches"] for f in m["fails"]}
    before, after = keys(old), keys(new)
    fixed = [before[k] | {"match": k[0]} for k in sorted(set(before) - set(after))]
    broke = [after[k] | {"match": k[0]} for k in sorted(set(after) - set(before))]
    for title, rows in (("no longer failing", fixed), ("newly failing", broke)):
        out.append(f"{title}: {len(rows)}")
        for f in rows[:show]:
            text = f["text"] if len(f["text"]) <= 100 else f["text"][:99] + "…"
            out.append(f"  {f['match']} {f['at']:>6} [{f['checker']}] \"{text}\"")
    return "\n".join(out)
