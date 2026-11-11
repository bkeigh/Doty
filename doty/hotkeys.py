"""System-wide hotkeys via Win32 RegisterHotKey (no keyboard hooks, no admin).

RegisterHotKey is the same mechanism Discord/OBS use: Windows delivers
WM_HOTKEY to our thread and the game never sees the chord.

Linux has no such call that every desktop honours, so there the chords are polled the way the
push-to-talk key is (holdkey.py, through X11): passive, but the game does see the keys too.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
POLL_S = 0.02  # Linux: how often the chords are looked at
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
    "`": 0xC0, "backtick": 0xC0, "/": 0xBF, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def parse_chord(chord: str) -> tuple[int, int]:
    mods, vk = 0, None
    for part in chord.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED_KEYS:
            vk = NAMED_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"unknown key {part!r} in hotkey {chord!r}")
    if vk is None:
        raise ValueError(f"hotkey {chord!r} has no key")
    return mods | MOD_NOREPEAT, vk


def chord_keys(chord: str) -> list[int]:
    """A chord as the virtual-key codes that must all be held: "ctrl+alt+a" -> [ctrl, alt, A]."""
    mods, vk = parse_chord(chord)
    held = [code for bit, code in ((0x0002, 0x11), (0x0001, 0x12), (0x0004, 0x10), (0x0008, 0x5B)) if mods & bit]
    return held + [vk]


class Hotkeys:
    def __init__(self) -> None:
        self._bindings: list[tuple[str, Callable[[], None]]] = []
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self.failed: list[str] = []
        self._poll_stop: threading.Event | None = None  # Linux: stops the polling thread

    def bind(self, chord: str, fn: Callable[[], None]) -> None:
        if chord:
            self._bindings.append((chord, fn))

    def start(self) -> None:
        if sys.platform.startswith("linux") and self._bindings:
            self._start_polling()
            return
        if sys.platform != "win32" or not self._bindings:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="doty-hotkeys", daemon=True)
        self._thread.start()
        ready.wait(2.0)

    def _start_polling(self, held=None) -> None:
        """Linux: one thread looks at the keyboard 50 times a second and fires a chord when it goes down.
        `held(codes)` returns which of those virtual-key codes are held (x11.held; tests pass their own)."""
        from . import x11

        if held is None:
            if not x11.available():
                self.failed = [chord for chord, _ in self._bindings]
                return
            held = x11.held
        chords = []
        for chord, fn in self._bindings:
            try:
                chords.append((chord_keys(chord), fn))
            except ValueError:
                self.failed.append(chord)
        if not chords:
            return
        self._poll_stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, args=(chords, held, self._poll_stop),
                                        name="doty-hotkeys", daemon=True)
        self._thread.start()

    @staticmethod
    def _poll(chords, held, stop: threading.Event) -> None:
        codes = {code for keys, _ in chords for code in keys}
        was_down = [False] * len(chords)
        while not stop.wait(POLL_S):
            try:
                now = held(codes)
            except Exception:
                now = set()
            for i, (keys, fn) in enumerate(chords):
                down = all(code in now for code in keys)
                if down and not was_down[i]:
                    try:
                        fn()
                    except Exception as exc:
                        print(f"[doty] hotkey handler failed: {exc!r}")
                was_down[i] = down

    def stop(self) -> None:
        if self._poll_stop is not None:
            self._poll_stop.set()
        if self._thread_id is not None:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)  # hotkeys are unregistered on exit, so a restart can re-register them

    def _run(self, ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        handlers: dict[int, Callable[[], None]] = {}
        for i, (chord, fn) in enumerate(self._bindings, start=1):
            mods, vk = parse_chord(chord)
            if user32.RegisterHotKey(None, i, mods, vk):
                handlers[i] = fn
            else:
                self.failed.append(chord)  # usually: another app already owns this chord
        ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in handlers:
                    try:
                        handlers[msg.wParam]()
                    except Exception as exc:
                        print(f"[doty] hotkey handler failed: {exc!r}")
        finally:
            for i in handlers:
                user32.UnregisterHotKey(None, i)
