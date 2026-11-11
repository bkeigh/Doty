"""Hold-to-talk key: detects press *and* release, for keys or mouse buttons.

RegisterHotKey (used for the tap hotkeys) only reports presses and can't see
mouse buttons, so this polls GetAsyncKeyState at 100 Hz instead: passive (no
input hook, nothing injected), ~0% CPU. The game still receives the key, so
pick one Dota doesn't use. A mouse side button is the usual choice.

Linux: the same polling through X11 (x11.py: XQueryKeymap, and XInput2 for the
mouse's side buttons).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from .hotkeys import NAMED_KEYS

VK_MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
VK_MOUSE = {"mouse3": 0x04, "middle": 0x04, "mouse4": 0x05, "xbutton1": 0x05, "mouse5": 0x06, "xbutton2": 0x06}
POLL_S = 0.01


def parse_hold(spec: str) -> list[int]:
    """'mouse5' -> [0x06]; 'ctrl+alt+t' -> [0x11, 0x12, 0x54]. All must be held."""
    vks: list[int] = []
    for part in spec.lower().replace(" ", "").split("+"):
        if part in VK_MODS:
            vks.append(VK_MODS[part])
        elif part in VK_MOUSE:
            vks.append(VK_MOUSE[part])
        elif part in NAMED_KEYS:
            vks.append(NAMED_KEYS[part])
        elif len(part) == 1 and part.isalnum():
            vks.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key {part!r} in push-to-talk key {spec!r}")
    if not vks:
        raise ValueError("empty push-to-talk key")
    return vks


class HoldKey:
    def __init__(self, spec: str, on_press: Callable[[], None], on_release: Callable[[], None],
                 is_down: Callable[[int], bool] | None = None) -> None:
        self.spec = spec
        self.vks = parse_hold(spec)
        self.on_press = on_press
        self.on_release = on_release
        self._is_down = is_down or _async_key_down
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.held = False

    def start(self) -> None:
        if sys.platform != "win32" and self._is_down is _async_key_down and not _can_poll():
            return
        self._thread = threading.Thread(target=self._run, name="doty-ptt", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1.0)

    def poll_once(self) -> None:
        down = all(self._is_down(vk) for vk in self.vks)
        if down and not self.held:
            self.held = True
            self._safe(self.on_press)
        elif not down and self.held:
            self.held = False
            self._safe(self.on_release)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            time.sleep(POLL_S)

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[doty] push-to-talk handler failed: {exc!r}")


def _can_poll() -> bool:
    """Outside Windows: Linux with an X server (X11, or XWayland)."""
    if not sys.platform.startswith("linux"):
        return False
    from . import x11

    return x11.available()


def _async_key_down(vk: int) -> bool:
    if sys.platform.startswith("linux"):
        from . import x11

        return x11.key_down(vk)
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
