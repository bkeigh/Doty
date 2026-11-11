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

## Set up

```bash
python -m venv .venv
```

```bash
.venv\Scripts\pip install -e .[dev]
```

Run the app with `.venv\Scripts\python -m doty app`, or try it without Dota using **Try a demo** on the Coach
page (a scripted match at 10× speed).

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

The whole suite takes about a minute and needs no network: AI providers, OpenDota and GitHub are faked
locally. Please add a test with each change. UI tests run headless (`QT_QPA_PLATFORM=offscreen`).

## Where things are

docs/ARCHITECTURE.md maps the code. The short version:

- `doty/gsi`: the game-data feed. `doty/rules`: instant, deterministic advice.
- `doty/coach`: the AI coach (prompts, providers, checks on its answers).
- `doty/triggers.py`: instant calls the AI prepares ahead, which Doty fires itself.
- `doty/knowledge`: hero, item and ability data (dotaconstants, OpenDota).
- `doty/overlay.py`: what's drawn over the game. `doty/ui`: the app window.
- `doty/review.py`: the post-game review. `doty/match_capture.py`: capture mode (frames and data for calibration).

## Reporting a wrong tip

In the app, hover the line in the Coach page's feed and click **Wrong tip?**. Doty saves the screenshot, the
game state and the coach's full answer to `Documents\Doty\reports`. Attach that folder to an issue using the
"A tip was wrong" template.

## Style

Plain, readable Python with type hints and docstrings that explain *why*. Text players see: short, plain
words, action first. Run the tests before sending a pull request.
