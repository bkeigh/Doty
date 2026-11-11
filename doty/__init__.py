"""Doty: a real-time Dota 2 coach.

Data flows one way:

    Dota 2 (GSI HTTP POST) -> gsi.server -> state.GameStateStore
        -> rules.engine (deterministic, <10 ms)  -> AdviceBus -> overlay / voice / log
        -> coach.llm (Claude, streamed, async)   -> AdviceBus
"""

__version__ = "0.1.1"
