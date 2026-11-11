"""The bench: replays matches through Doty offline and scores every line it says.

"The coach got better" should be a number per release, not a feeling. The bench gives that number:

- `replay.Replay` feeds a recorded match (a capture folder's gsi.jsonl, a gsi.record_dir recording, or the
  scripted match) through the real game-state store, rules and knowledge, optionally the AI coach with scripted
  answers, at full speed on a simulated clock. Same input, same lines.
- `checks` judges each line against the game as it stood when it was said: an item the player owns, gold they
  don't have, a TP scroll they hold, an item they passed on, a control tag read out, a time long past, the same
  line again, a line at the wrong moment, too many lines. The AI never supplies facts, only judgement; every
  claim it speaks should check out against the game data, and the bench is the checker of record.
- `score` sums it up per match and overall, as JSON for machines and a table for people, and compares two runs.
- `logs` scores what was really said in past games from the session logs and match reviews Doty already keeps.
- `fixtures` turns "Wrong tip?" reports into small anonymised test cases.

Run it with `python -m doty.bench --help`. Everything here is developer output in plain English: the bench isn't
part of the app's interface, so its strings aren't translated.
"""

from .checks import CHECKERS, Checker, checker
from .model import Context, Line, Match
from .replay import Replay, ScriptedBackend, read_recording, scripted_payloads
from .score import compare, render, scorecard

__all__ = ["CHECKERS", "Checker", "checker", "Context", "Line", "Match", "Replay", "ScriptedBackend",
           "read_recording", "scripted_payloads", "compare", "render", "scorecard"]
