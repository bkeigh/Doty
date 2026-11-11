"""Advice: the single message type every producer emits and every output consumes."""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable


class Priority(IntEnum):
    CRITICAL = 1  # you're about to die
    HIGH = 2  # act in the next few seconds
    NORMAL = 3  # timers, purchases
    INFO = 4  # nice to know; overlay only by default


_ids = itertools.count(1)


def tone(source: str, category: str, priority: int) -> str:
    """What a line's color means, the same in the app's feed and on the overlay:
    "coach" (gold), "quiet" (the coach's dim observations), "you" (what you asked), "now" (red: act now),
    "soon" (amber: something coming up) or "info" (plain information)."""
    if source == "coach":
        return "quiet" if category == "watching" else "coach"
    if source == "player":
        return "you"
    if priority <= Priority.CRITICAL:
        return "now"
    if priority <= Priority.HIGH or category in ("timer", "runes", "objectives"):
        return "soon"
    return "info"


@dataclass
class Advice:
    text: str
    priority: Priority = Priority.NORMAL
    category: str = "general"  # timer | survival | economy | progression | coach
    key: str = ""  # dedupe key; same key won't repeat within the rule's cooldown
    speech: str | None = None  # shorter spoken form; defaults to text
    ttl_s: float | None = None
    source: str = "rules"  # rules | coach
    # Wall-clock perf_counter() of the GSI payload that triggered this, for latency.
    trigger_t: float | None = None
    created_t: float = field(default_factory=time.perf_counter)
    id: int = field(default_factory=lambda: next(_ids))
    # Streaming coach advice updates in place: same id, growing text, final=True at the end.
    final: bool = True
    # Sinks filter on these: the coach streams card updates with speak=False and
    # sends each finished sentence as display=False so voice starts early.
    display: bool = True
    speak: bool = True
    # False = the app's feed only, not the in-game overlay (e.g. the coach's quiet observations).
    overlay: bool = True
    # Circles, arrows and boxes the coach drew to point things out (marks.Mark), shown with the card.
    marks: tuple = ()
    # Voice: a line about this moment is dropped if it can't start within this many seconds (it would be
    # about the past by then); None = always said. And cut_in: stop whatever is being said for this one.
    stale_s: float | None = None
    cut_in: bool = False

    @property
    def latency_ms(self) -> float | None:
        if self.trigger_t is None:
            return None
        return (self.created_t - self.trigger_t) * 1000.0

    @property
    def spoken(self) -> str:
        return self.speech if self.speech is not None else self.text


Subscriber = Callable[[Advice], None]


class AdviceBus:
    """Fan-out to subscribers. Thread-safe; subscribers must not block."""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> None:
        with self._lock:
            self._subs.append(fn)

    def publish(self, advice: Advice) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(advice)
            except Exception as exc:  # one bad sink must not silence the rest
                print(f"[doty] advice subscriber {fn!r} failed: {exc}")
