"""python -m doty.bench: score Doty's lines on recorded matches, past session logs, or the scripted match.

    python -m doty.bench --latest [N]                     # the N most recent match records (match_record.py)
    python -m doty.bench RECORD_FOLDER                    # one match record
    python -m doty.bench CAPTURE_FOLDER_OR_RECORDING.jsonl [--answers answers.json] [--json out.json]
    python -m doty.bench session-*.jsonl                  # what was really said in past games
    python -m doty.bench RECORDING --said session.jsonl   # what was said, judged against the recorded game
    python -m doty.bench --scripted [--minutes 26]
    python -m doty.bench --compare old.json new.json
    python -m doty.bench --from-report REPORT_FOLDER --out fixture.json
    python -m doty.bench --fixtures tests/data/bench/*.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path


def _expand(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        hits = sorted(glob.glob(p)) if any(ch in p for ch in "*?[") else [p]
        out += [Path(h) for h in hits]
    return out


def _kb(args, hero_ids=()):
    if args.no_kb:
        return None
    from .kb import load_kb

    kb = load_kb(Path(args.cache) if args.cache else None, hero_ids)
    if not kb.items:
        print("[bench] no item data in the cache: item checks are skipped (run Doty once online to fill it)",
              file=sys.stderr)
    return kb


def _hero_ids_from_reviews(folder: Path | None) -> set[int]:
    from .logs import load_reviews

    return {int(r.get("hero_id") or 0) for r in load_reviews(folder)} - {0}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m doty.bench",
                                description="Score every line Doty says against the game at that moment.")
    p.add_argument("inputs", nargs="*", help="capture folders, game-data recordings (.jsonl) or session logs")
    p.add_argument("--scripted", action="store_true", help="replay the scripted demo match")
    p.add_argument("--latest", nargs="?", type=int, const=1, metavar="N",
                   help="score the N most recent match records (default 1)")
    p.add_argument("--records", help="folder of match records (default: Doty's own)")
    p.add_argument("--minutes", type=float, default=26.5, help="length of the scripted match")
    p.add_argument("--answers", help="replay the AI coach with these recorded answers (.json list or .jsonl)")
    p.add_argument("--said", help="a session log: score its lines against the recording's game state")
    p.add_argument("--reviews", help="folder of match reviews (default: next to the session logs)")
    p.add_argument("--cache", help="Doty's knowledge cache folder (default: Doty's own)")
    p.add_argument("--no-kb", action="store_true", help="don't load item and hero data")
    p.add_argument("--min-minutes", type=float, default=2.0, help="skip log stretches shorter than this")
    p.add_argument("--json", metavar="OUT", help="write the scorecard as JSON")
    p.add_argument("--quiet", action="store_true", help="don't print the table")
    p.add_argument("--worst", type=int, default=8, help="worst lines to quote per match")
    p.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="what changed between two scorecards")
    p.add_argument("--from-report", metavar="FOLDER", help='turn a "Wrong tip?" report into a fixture')
    p.add_argument("--out", help="where --from-report writes the fixture (default: print it)")
    p.add_argument("--fixtures", nargs="+", metavar="FILE", help="run fixtures and say which still hold")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    from . import score

    if args.compare:
        old, new = (json.loads(Path(f).read_text(encoding="utf-8")) for f in args.compare)
        print(score.compare(old, new))
        return 0
    if args.from_report:
        from .fixtures import convert_report

        fx = convert_report(Path(args.from_report), _kb(args))
        text = json.dumps(fx, indent=1, ensure_ascii=False) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"[bench] wrote {args.out}: expect {fx['expect']}, caught by {fx['checks'] or 'no checker yet'}")
        else:
            print(text)
        return 0
    if args.fixtures:
        from .fixtures import load_fixture, run_fixture

        kb = _kb(args)
        results = [run_fixture(load_fixture(f), kb) for f in _expand(args.fixtures)]
        for r in results:
            print(f"{r['status']:<10} {r['name']:<40} failed={','.join(r['failed']) or '-'}"
                  f" expected={','.join(r['expected']) or '-'} coach={r['coach'] or '-'}")
        return 1 if any(r["status"] == "regressed" for r in results) else 0

    from .logs import is_session_log, log_matches
    from .replay import Replay, ScriptedBackend, attach_states, load_answers, read_recording, scripted_payloads

    matches = []
    backend = ScriptedBackend(load_answers(Path(args.answers))) if args.answers else None
    if args.scripted:
        kb = _kb(args, [8])  # (the scripted match plays Juggernaut)
        payloads = scripted_payloads(end_clock=int(args.minutes * 60))
        matches += Replay(kb, backend=backend, label="scripted").run(payloads)
    from .records import is_record, latest, read_info, record_match

    inputs = _expand(args.inputs)
    if args.latest:
        found = latest(Path(args.records) if args.records else None, args.latest)
        if not found:
            print("[bench] no match records yet: play a match with Doty running (Settings > Advanced > "
                  "Keep a record of each match)", file=sys.stderr)
        inputs += found
    records = [f for f in inputs if is_record(f)]
    logs = [f for f in inputs if f.is_file() and is_session_log(f)]
    recordings = [f for f in inputs if f not in logs and f not in records]
    if records:
        kb = _kb(args, {int(read_info(f).get("hero_id") or 0) for f in records} - {0})
        for folder in records:
            m = record_match(folder, kb)
            if m is not None:
                matches.append(m)
    if logs:
        reviews = Path(args.reviews) if args.reviews else logs[0].parent.parent / "reviews"
        kb = _kb(args, _hero_ids_from_reviews(reviews))
        for log in logs:
            matches += log_matches(log, kb, reviews, args.min_minutes)
    for rec in recordings:
        from .kb import hero_ids_in

        kb = _kb(args, hero_ids_in(read_recording(rec)))
        label = rec.name if rec.is_dir() else rec.stem
        if args.said:
            replay = Replay(kb, rules=False, label=label)
            replay.run(read_recording(rec))
            said = log_matches(Path(args.said), kb, Path(args.reviews) if args.reviews else None, 0)
            for m in said:
                got = attach_states(m.lines, replay.timeline)
                m.kind = "log+recording"
                m.notes = [f"what was said, judged against the recorded game ({got} of {len(m.lines)} lines "
                           f"matched to a game state)"]
            matches += [m for m in said if any(line.ctx is not None and line.ctx.snap is not None for line in m.lines)]
        else:
            matches += Replay(kb, backend=backend, label=label).run(read_recording(rec))
    if not matches:
        p.error("nothing to score: give a recording, a session log or --scripted")
    card = score.scorecard(matches)
    if args.json:
        score.save(card, Path(args.json))
    if not args.quiet:
        print(score.render(card, worst=args.worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
