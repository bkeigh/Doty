"""Find the Dota 2 window, so the overlay and screenshots follow the game
across multi-monitor setups instead of assuming the primary screen. (Linux: through X11, doty/x11.py.)"""

from __future__ import annotations

import sys

Rect = tuple[int, int, int, int]  # x, y, w, h in physical screen pixels

DOTA_TITLE = "Dota 2"


def dota_is_foreground() -> bool:
    """True while the Dota 2 window is the one you're using."""
    if sys.platform.startswith("linux"):
        from . import x11

        return x11.active_title() == DOTA_TITLE
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value == DOTA_TITLE


COVERED_SHARE = 0.5  # the window in front hides this much of Dota's: Doty's overlay stays out of its way
SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd")  # the desktop and the taskbar cover nothing


def dota_covered() -> bool:
    """Another app's window is in front of Dota and covers most of it (you alt-tabbed to a browser on the
    same screen). Doty's own windows, the desktop and the taskbar don't count, nor does a window on
    another monitor. Windows only: elsewhere the overlay always shows."""
    if sys.platform != "win32":
        return False
    import ctypes
    import os
    from ctypes import wintypes

    found = find_dota_window()
    if not found:
        return False
    _hwnd, (x, y, w, h) = found
    user32 = ctypes.windll.user32
    front = user32.GetForegroundWindow()
    if not front or dota_is_foreground():
        return False
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(front, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(front, name, 64)
    if name.value in SHELL_CLASSES:
        return False
    rect = wintypes.RECT()
    if not user32.GetWindowRect(front, ctypes.byref(rect)):
        return False
    across = max(0, min(rect.right, x + w) - max(rect.left, x))
    down = max(0, min(rect.bottom, y + h) - max(rect.top, y))
    return across * down >= COVERED_SHARE * w * h


def find_dota_rect() -> Rect | None:
    """Client-area rectangle of the visible Dota 2 window, or None."""
    found = find_dota_window()
    return found[1] if found else None


def find_dota_window() -> tuple[int, Rect] | None:
    """(window handle, client-area rectangle) of the visible Dota 2 window, or None."""
    if sys.platform.startswith("linux"):
        from . import x11

        found = x11.dota_window(DOTA_TITLE)
        if found is None or found[1][2] < 200 or found[1][3] < 200:
            return None
        return found
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length != len(DOTA_TITLE):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == DOTA_TITLE:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(on_window, 0)
    if not found:
        return None
    rect = wintypes.RECT()
    user32.GetClientRect(found[0], ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(found[0], ctypes.byref(origin))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 200 or h < 200:
        return None
    return found[0], (origin.x, origin.y, w, h)


def print_window(hwnd: int, width: int, height: int):
    """The window's own picture of its client area (PrintWindow with PW_RENDERFULLCONTENT), as a PIL
    RGB image, or None. Slower than a screen grab, but it can see a game the screen grab gets as black
    (some fullscreen display modes bypass the desktop) and isn't fooled by a window in front.
    Windows only: on Linux there's no such call, and the screen grab is all Doty has."""
    if sys.platform != "win32" or width <= 0 or height <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    # Own instances, so these signatures don't change ctypes.windll's for the rest of Doty.
    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.GetDC.argtypes, user32.GetDC.restype = (wintypes.HWND,), wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes, gdi32.CreateCompatibleDC.restype = (wintypes.HDC,), wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hwnd_dc = user32.GetDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    old = gdi32.SelectObject(mem_dc, bitmap)
    try:
        # PW_CLIENTONLY (1) | PW_RENDERFULLCONTENT (2)
        if not user32.PrintWindow(hwnd, mem_dc, 3):
            return None
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(header), 0) != height:
            return None
        return Image.frombuffer("RGB", (width, height), buf.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
