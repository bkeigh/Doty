from . import engine as _engine
from .engine import RuleEngine


def _with_focus(base):
    """The default rules plus this game's focus (rules/focus.py). Registered here, around engine.default_rules,
    so the engine module itself is unchanged."""
    def default_rules():
        from .focus import rules as focus_rules

        return [*base(), *focus_rules()]
    default_rules._with_focus = True
    return default_rules


if not getattr(_engine.default_rules, "_with_focus", False):
    _engine.default_rules = _with_focus(_engine.default_rules)
default_rules = _engine.default_rules

__all__ = ["RuleEngine", "default_rules", "spoken_lines", "urgent_lines"]

# Every fixed `speech=` line in the rules (tests/test_rules.py checks this list against the source).
FIXED_LINES = (
    "Back off, low HP",
    "Get out now",
    "Head back, no TP ready",
    "Head back, you're low",
    "Buy a TP scroll",
    "No buyback. Play safe.",
    "Spend your gold",
    "Spend your gold while dead",
    "Too many deaths. Stay near your team.",
    "Roshan can spawn in 20",
    "Roshan is up",
    "Aegis ends in 30",
    "Courier your stash",
    "Level a skill",
    "Level 6. Look for a kill.",
    "Farm is behind. Focus last hits.",
    "Deny your creeps too",
    "Stay on the wave, last-hit",
    "Tormentors spawning",
    "Day",
    "Night",
    "Two enemies dead. Push a tower.",
    "Three enemies dead. Take an objective.",
    "Four enemies dead. Take an objective.",
    "All five dead. Push high ground.",
    "Your team is down. Back off.",
    "They scale better. Win early.",
    "You scale better. Play safe.",
)


def teaching_lines() -> list[str]:
    """The basics said to a player new to Dota (voiced ahead too)."""
    from .progression import BASICS

    return [line for _at, line in BASICS]


def urgent_lines() -> list[str]:
    """The calls about staying alive this second, which the voice says a little faster."""
    from .survival import save_lines

    return [*FIXED_LINES[:2], *save_lines()]


def spoken_lines(cfg) -> list[str]:
    """What the rules say out loud word for word, most urgent first, so a local voice can make
    them ahead of time. `cfg` is the RulesConfig (for the timer lead)."""
    from .survival import save_lines
    from .timers import timer_lines

    timers = timer_lines(cfg)
    # The urgent ones first ("Back off", "Get out", then the save calls): they're the ones that can't wait.
    lines = (list(FIXED_LINES[:2]) + save_lines() + list(FIXED_LINES[2:6]) + timers + list(FIXED_LINES[6:])
             + teaching_lines())
    return list(dict.fromkeys(lines))
