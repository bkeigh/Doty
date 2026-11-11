"""Screenshots of the Dota window: for the coach (vision input) and the app's live view.

The coach's screenshots are taken per request (a question, a background look),
never streamed or stored. The live-view frames stay on this PC.
mss uses GDI BitBlt: ~15-30 ms at 1440p, then a downscale + JPEG encode (on Linux, XGetImage over X11;
a Wayland session without XWayland has no screen capture, and the coach works from the game data).
Doty's own cards are blacked out so the model never reads its own advice.
"""

from __future__ import annotations

import base64
import io
import sys
import time

import mss
from PIL import Image, ImageDraw

from . import hud
from .win import Rect, dota_is_foreground, find_dota_rect, find_dota_window, print_window

BLACK_LEVEL = 16  # a frame with no sampled pixel brighter than this (0..255) is all black
BLACK_NOTE = ("no screenshot this time: the Dota window captured as all black (a loading screen, or a display "
              "mode that hides it from screen capture)")
NO_SCREEN_NOTE = ("no screenshot: this Linux desktop doesn't let Doty capture the screen (a Wayland session "
                  "without XWayland); go by the game data")


def screen_capture_available() -> bool:
    """Windows always; Linux only with an X server (X11, or XWayland on a Wayland desktop)."""
    if sys.platform.startswith("linux"):
        from . import x11

        return x11.available()
    return True


def is_black(img: Image.Image) -> bool:
    """Nothing but black: a loading screen, or a capture that couldn't see the game (a real frame, even
    at night, has the HUD and the minimap in it)."""
    small = img.resize((64, 27), Image.NEAREST)
    return max(hi for _lo, hi in small.getextrema()) < BLACK_LEVEL


def _rgb(shot) -> Image.Image:
    """A screen grab as an RGB image, converted in one pass from the BGRA bytes Windows hands over (mss's own
    `.rgb` builds a second copy in Python first: ~18 ms more on a 3440x1440 frame)."""
    raw = getattr(shot, "bgra", None)
    if raw is None:
        return Image.frombytes("RGB", shot.size, shot.rgb)
    return Image.frombuffer("RGB", shot.size, raw, "raw", "BGRX", 0, 1)


def _shrink(img: Image.Image, width: int) -> Image.Image:
    """`img` scaled down to `width`: whole pixels averaged first (fast, and no shimmer from skipped rows),
    then the last stretch smoothly. Quicker than one smooth resize from full size, and sharper text."""
    if img.width <= width:
        return img
    factor = img.width // width
    if factor >= 2:
        img = img.reduce(factor)
    if img.width == width:
        return img
    return img.resize((width, round(img.height * width / img.width)), Image.BILINEAR)


def _grab_dota(sct, mask: Rect | None) -> tuple[Image.Image | None, dict | None, bool]:
    """(image, the screen region, found the Dota window). The screen grab is fast; when Dota isn't the
    window in front (something may cover it) or the grab comes back black, the window's own picture
    (PrintWindow) is used instead."""
    found = find_dota_window()
    if not found:
        mon = next((m for m in sct.monitors[1:] if m.get("is_primary")), sct.monitors[1])
        shot = sct.grab(mon)
        return _rgb(shot), mon, False
    hwnd, (x, y, w, h) = found
    mon = {"left": x, "top": y, "width": w, "height": h}
    img = None
    if dota_is_foreground():
        shot = sct.grab(mon)
        img = _rgb(shot)
        if mask:  # Doty's own cards, blacked out so the coach never reads its own advice
            mx, my, mw, mh = mask
            ImageDraw.Draw(img).rectangle((mx - x, my - y, mx - x + mw, my - y + mh), fill=(0, 0, 0))
    if img is None or is_black(img):
        try:
            alt = print_window(hwnd, w, h)
        except OSError:
            alt = None
        if alt is not None and not is_black(alt):
            img = alt
        elif img is None:
            shot = sct.grab(mon)
            img = _rgb(shot)
    return img, mon, True


class FrameGrabber:
    """Small, frequent grabs of the Dota window for the app's live view (shown
    locally only; nothing is sent anywhere). Use from one thread."""

    def __init__(self) -> None:
        self._sct = None

    def grab(self) -> tuple[int, int, bytes] | None:
        """(width, height, BGRA bytes) of the Dota window, or None if it isn't open."""
        dota = find_dota_rect()
        if not dota or dota[2] < 64 or dota[3] < 64:
            return None
        if self._sct is None:
            self._sct = mss.MSS()
        shot = self._sct.grab({"left": dota[0], "top": dota[1], "width": dota[2], "height": dota[3]})
        return shot.width, shot.height, bytes(shot.bgra)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


class StripGrabber:
    """One part of the Dota window straight off the screen, for Doty's top-bar reader: it reads a strip a
    few dozen pixels tall, so that's all that is grabbed, about a twentieth of the 3440x1440 frame it used
    to take every second. Use from one thread; `grab` gives None when there's nothing usable (no window,
    or a strip that came back black: then the caller falls back to the window's own picture)."""

    def __init__(self) -> None:
        self._sct = None

    def grab(self, box_for) -> "tuple | None":
        """`box_for(width, height)` -> (x0, y0, x1, y1) inside the window. Returns (RGB array of that box,
        the box, (window width, window height))."""
        import numpy as np

        dota = find_dota_rect()
        if not dota or dota[2] < 64 or dota[3] < 64:
            return None
        x, y, w, h = dota
        x0, y0, x1, y1 = box_for(w, h)
        if x1 <= x0 or y1 <= y0:
            return None
        if self._sct is None:
            self._sct = mss.MSS()
        shot = self._sct.grab({"left": x + x0, "top": y + y0, "width": x1 - x0, "height": y1 - y0})
        band = np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)[:, :, 2::-1]
        if int(band.max()) < BLACK_LEVEL:
            return None
        return np.ascontiguousarray(band), (x0, y0, x1, y1), (w, h)

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


def grab_window() -> Image.Image | None:
    """The whole Dota window at full resolution (RGB), or None if it isn't open."""
    dota = find_dota_rect()
    if not dota or dota[2] < 64 or dota[3] < 64:
        return None
    with mss.MSS() as sct:
        img, _mon, _found = _grab_dota(sct, None)
    return img


def _jpeg_b64(img: Image.Image, quality: int) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def grab_views(width: int = 1280, quality: int = 70, mask: Rect | None = None,
               minimap_side: str = "left") -> tuple[dict, float]:
    """The coach's eyes: the Dota window scaled to `width`, plus full-resolution crops of the
    minimap (who's showing where) and the top bar (all ten heroes, dead ones greyed).
    The crops are only taken when the Dota window is found. `views["sizes"]` has each image's
    (width, height) as sent, so marks the coach draws on them can be mapped back (marks.py).
    Returns (views, ms taken)."""
    t0 = time.perf_counter()
    views: dict = {}
    sizes: dict[str, tuple[int, int]] = {}
    if not screen_capture_available():
        return {"unavailable": NO_SCREEN_NOTE, "sizes": sizes}, (time.perf_counter() - t0) * 1000.0
    with mss.MSS() as sct:
        img, _mon, dota = _grab_dota(sct, mask)
    if is_black(img):  # nothing to see: say so instead of sending black pictures
        return {"unavailable": BLACK_NOTE, "sizes": sizes}, (time.perf_counter() - t0) * 1000.0
    W, H = img.size
    if dota:
        mx, my, ms, _ = hud.minimap_rect(W, H, minimap_side)
        minimap = img.crop((mx, my, mx + ms, my + ms))
        if minimap.width > 440:
            minimap = minimap.resize((440, 440), Image.LANCZOS)
        views["minimap"] = _jpeg_b64(minimap, 82)
        sizes["minimap"] = minimap.size
        tx, ty, tw, th = hud.topbar_rect(W, H)
        topbar = img.crop((tx, ty, tx + tw, ty + th))
        if topbar.width > 1200:
            topbar = topbar.resize((1200, round(topbar.height * 1200 / topbar.width)), Image.LANCZOS)
        views["topbar"] = _jpeg_b64(topbar, 82)
    img = _shrink(img, width)
    views["screen"] = _jpeg_b64(img, quality)
    sizes["screen"] = img.size
    views["sizes"] = sizes
    return views, (time.perf_counter() - t0) * 1000.0
