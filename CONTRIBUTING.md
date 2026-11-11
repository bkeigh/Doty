# Contributing to Doty

Thanks for helping. Doty is a free, open-source Dota 2 coach for Windows, written in Python (PySide6).

## Ground rules

- **Fair play first.** Doty only uses what the player could see or know: Valve's Game State Integration,
  screenshots of the player's own screen, and public hero statistics. Never read game memory, inject
  anything, send input to the game, or look up the players in a lobby. Pull requests that cross this line
  won't be merged. See README "How it works" and docs/RESEARCH.md.
- **No Valve art in Doty's branding.** Hero and item images inside pages come from Valve's public CDN at
  runtime and are never bundled.
- **Free and light.** No paid services required, no telemetry, and think twice before adding a dependency.

## License

Doty is licensed under the GNU General Public License, version 3 or later (see [LICENSE](LICENSE)). By
sending a pull request you agree that your contribution is released under the same license.

## Set up

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -e .[dev]
```

Run the app with `.venv\Scripts\python -m doty app`, or try it without Dota using **Try a demo** on the Coach
page (a scripted match at 6× speed).

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

The whole suite takes about 10 minutes and needs no network: AI providers, OpenDota and GitHub are faked
locally, and `tests/conftest.py` turns off the background hero study and points match records at a temporary
folder. While you work, run the files for what you changed (`-m pytest tests/test_bench.py -q`). Please add a
test with each change. UI tests run headless (`QT_QPA_PLATFORM=offscreen`).

## The bench

For anything that changes what Doty says, the bench gives a number instead of a feeling. It replays a match
through the real rules (and the AI coach with scripted answers) and checks every line against the game as it
stood: items you own, gold you don't have, a TP scroll you hold, an item you passed on, a stale time, a repeat,
too much talk.

```bash
.venv\Scripts\python -m doty.bench --scripted                 # the demo match, rules only
.venv\Scripts\python -m doty.bench --latest 3                 # your three latest match records
.venv\Scripts\python -m doty.bench --compare before.json after.json
.venv\Scripts\python -m doty.bench --fixtures tests/data/bench/*.json
```

`--json out.json` saves a scorecard for `--compare`. A "Wrong tip?" report becomes a small anonymised fixture
with `--from-report REPORT_FOLDER --out fixture.json`; fixtures in `tests/data/bench/` run with the test suite.
`python -m doty.bench --help` lists the rest.

## Where things are

docs/ARCHITECTURE.md maps the code. The short version:

- `doty/gsi`: the game-data feed. `doty/rules`: instant, deterministic advice.
- `doty/coach`: the AI coach (prompts, providers, checks on its answers).
- `doty/triggers.py`: instant calls the AI prepares ahead, which Doty fires itself.
- `doty/knowledge`: hero, item and ability data (dotaconstants, OpenDota), the item judgement
  (`judgement.py`), hero cards and the study that builds them (`cards.py`, `study.py`), map places (`mapgeo.py`).
- `doty/overlay.py` and `doty/marks.py`: what's drawn over the game. `doty/ui`: the app window.
- `doty/review.py`: the post-game review. `doty/match_record.py`: the record of each match (on by default).
  `doty/match_capture.py`: capture mode (frames and data for calibration). `doty/bench`: the bench.

The rule for new work: the AI supplies judgement, code supplies the facts. If the coach needs a fact (what an
item answers, where a tower is), compute it in code and hand it over; don't ask the AI to know it.

## Reporting a wrong tip

In the app, hover the line in the Coach page's feed and click **Wrong tip?**. Doty saves the screenshot, the
game state and the coach's full answer to `Documents\Doty\reports`. Attach that folder to an issue using the
"A tip was wrong" template.

## Style

Plain, readable Python with type hints and docstrings that explain *why*. Text players see: short, plain
words, action first. Run the tests before sending a pull request.
