"""Linux desktop plumbing over X11 (libX11 and libXi through ctypes, no extra packages).

What doty/win.py, hotkeys.py and holdkey.py do with Win32 on Windows, this does on Linux:
- **The Dota window:** its position and whether it's the active window, from the window manager's own
  lists (_NET_CLIENT_LIST, _NET_ACTIVE_WINDOW): the same thing a taskbar reads.
- **Keys and mouse buttons:** is a key or button held right now (XQueryKeymap, XIQueryPointer)? Polled, like
  GetAsyncKeyState on Windows: passive, nothing hooked, nothing sent to the game.

Wayland: Dota runs under XWayland, so all of this works there when the session has XWayland (DISPLAY set),
and Doty asks Qt for its X11 backend so its overlay can sit over the game (`prefer_xcb`). A Wayland session
without XWayland gets none of it: Doty then coaches from the game data alone.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import threading

Rect = tuple[int, int, int, int]

# Windows virtual-key codes (what hotkeys.py and holdkey.py parse keys into) -> X keysyms that count as that key.
_VK_KEYSYMS: dict[int, tuple[int, ...]] = {
    0x10: (0xFFE1, 0xFFE2),  # shift: Shift_L, Shift_R
    0x11: (0xFFE3, 0xFFE4),  # ctrl: Control_L, Control_R
    0x12: (0xFFE9, 0xFFEA, 0xFFE7, 0xFFE8),  # alt: Alt_L, Alt_R, and Meta_L/R where layouts put Alt there
    0x5B: (0xFFEB, 0xFFEC),  # win: Super_L, Super_R
    0x20: (0x0020,), 0x0D: (0xFF0D,), 0x09: (0xFF09,), 0x1B: (0xFF1B,), 0x08: (0xFF08,),
    0x24: (0xFF50,), 0x23: (0xFF57,), 0x2D: (0xFF63,), 0x2E: (0xFFFF,), 0x21: (0xFF55,), 0x22: (0xFF56,),
    0xC0: (0x0060,), 0xBF: (0x002F,), 0xBA: (0x003B,), 0xDE: (0x0027,), 0xDB: (0x005B,), 0xDD: (0x005D,),
    0xDC: (0x005C,),
}
# Mouse virtual-key codes -> X button numbers (8 = back, 9 = forward: the side buttons).
VK_BUTTONS = {0x01: 1, 0x02: 3, 0x04: 2, 0x05: 8, 0x06: 9}

_IS_VIEWABLE = 2
_ANY_PROPERTY_TYPE = 0


def keysyms_for_vk(vk: int) -> tuple[int, ...]:
    """The X keysyms for a Windows virtual-key code: letters, digits, F1-F24 and the named keys."""
    if 0x41 <= vk <= 0x5A:  # A-Z: X names the lowercase keysym
        return (vk + 0x20,)
    if 0x30 <= vk <= 0x39:  # 0-9
        return (vk,)
    if 0x70 <= vk <= 0x87:  # F1-F24
        return (0xFFBE + vk - 0x70,)
    return _VK_KEYSYMS.get(vk, ())


def keycode_down(keymap: bytes, keycode: int) -> bool:
    """A key's bit in XQueryKeymap's 32-byte answer."""
    return 0 < keycode < 256 and bool(keymap[keycode >> 3] & (1 << (keycode & 7)))


def button_in_mask(mask: bytes, button: int) -> bool:
    """A button's bit in an XInput2 button mask."""
    return 0 <= button < len(mask) * 8 and bool(mask[button >> 3] & (1 << (button & 7)))


def session_kind(env=os.environ) -> str:
    """"x11", "xwayland" (a Wayland session that can still run X11 apps), "wayland" (no X11 at all) or "none"."""
    wayland = env.get("XDG_SESSION_TYPE", "").lower() == "wayland" or bool(env.get("WAYLAND_DISPLAY"))
    if env.get("DISPLAY"):
        return "xwayland" if wayland else "x11"
    return "wayland" if wayland else "none"


def prefer_xcb(platform: str = sys.platform, env=os.environ) -> bool:
    """Before the QApplication is made: on a Wayland session with XWayland, run Doty's windows through X11
    too. Wayland doesn't let a window place itself or stay above the game, which the overlay needs. Returns
    True if it changed Qt's platform (the player's own QT_QPA_PLATFORM always wins)."""
    if not platform.startswith("linux") or env.get("QT_QPA_PLATFORM"):
        return False
    if session_kind(env) != "xwayland":
        return False
    env["QT_QPA_PLATFORM"] = "xcb"
    return True


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("border_width", ctypes.c_int), ("depth", ctypes.c_int), ("visual", ctypes.c_void_p),
                ("root", ctypes.c_ulong), ("class_", ctypes.c_int), ("bit_gravity", ctypes.c_int),
                ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int), ("backing_planes", ctypes.c_ulong),
                ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int), ("colormap", ctypes.c_ulong),
                ("map_installed", ctypes.c_int), ("map_state", ctypes.c_int), ("all_event_masks", ctypes.c_long),
                ("your_event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
                ("override_redirect", ctypes.c_int), ("screen", ctypes.c_void_p)]


class _XIButtonState(ctypes.Structure):
    _fields_ = [("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class _XIModifierState(ctypes.Structure):
    _fields_ = [("base", ctypes.c_int), ("latched", ctypes.c_int), ("locked", ctypes.c_int),
                ("effective", ctypes.c_int)]


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


@_ERROR_HANDLER
def _ignore_x_error(_display, _event):
    # Xlib's default handler ends the process on any error, e.g. a window that closed while Doty looked at it.
    return 0


class X11:
    """One connection to the X server. Xlib isn't safe across threads, so each thread gets its own (`get`)."""

    _libs: tuple | None = None
    _local = threading.local()

    @classmethod
    def _load(cls):
        if cls._libs is None:
            x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
            x.XOpenDisplay.restype = ctypes.c_void_p
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x.XDefaultRootWindow.restype = ctypes.c_ulong
            x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x.XInternAtom.restype = ctypes.c_ulong
            x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int,
                ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
            x.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWindowAttributes)]
            x.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong)]
            x.XFree.argtypes = [ctypes.c_void_p]
            x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            x.XKeysymToKeycode.restype = ctypes.c_ubyte
            x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x.XQueryPointer.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]
            x.XSetErrorHandler.restype = ctypes.c_void_p
            x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]
            x.XSetErrorHandler(_ignore_x_error)
            xi = None
            try:  # XInput2: the only way to see the mouse's side buttons
                xi = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xi") or "libXi.so.6")
                xi.XIQueryVersion.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                              ctypes.POINTER(ctypes.c_int)]
                xi.XIGetClientPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_int)]
                xi.XIQueryPointer.argtypes = [
                    ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
                    ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
                    ctypes.POINTER(_XIButtonState), ctypes.POINTER(_XIModifierState),
                    ctypes.POINTER(_XIModifierState)]
            except (OSError, AttributeError):
                xi = None
            cls._libs = (x, xi)
        return cls._libs

    @classmethod
    def get(cls) -> "X11 | None":
        """This thread's connection, or None where there's no X server (or not Linux)."""
        if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
            return None
        conn = getattr(cls._local, "conn", False)
        if conn is False:
            try:
                conn = cls(*cls._load())
            except (OSError, AttributeError, RuntimeError):
                conn = None
            cls._local.conn = conn
        return conn

    def __init__(self, x, xi) -> None:
        self.x, self.xi = x, xi
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("can't open the X display")
        self.root = x.XDefaultRootWindow(self.dpy)
        self._atoms: dict[str, int] = {}
        self._keycodes: dict[int, tuple[int, ...]] = {}
        self._pointer: int | None = None
        if xi is not None:
            major, minor = ctypes.c_int(2), ctypes.c_int(0)
            dev = ctypes.c_int(0)
            if xi.XIQueryVersion(self.dpy, ctypes.byref(major), ctypes.byref(minor)) == 0 and \
                    xi.XIGetClientPointer(self.dpy, 0, ctypes.byref(dev)):
                self._pointer = dev.value

    # windows -----------------------------------------------------------------------------
    def _atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.x.XInternAtom(self.dpy, name.encode(), False)
        return self._atoms[name]

    def _property(self, window: int, name: str, items: int = 4096) -> tuple[int, bytes, int]:
        """(item size in bits, raw data, item count) of a window property, or (0, b"", 0)."""
        actual, fmt = ctypes.c_ulong(), ctypes.c_int()
        count, after = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x.XGetWindowProperty(self.dpy, window, self._atom(name), 0, items, False, _ANY_PROPERTY_TYPE,
                                           ctypes.byref(actual), ctypes.byref(fmt), ctypes.byref(count),
                                           ctypes.byref(after), ctypes.byref(data))
        if status != 0 or not data:
            return 0, b"", 0
        try:
            # 32-bit items come back as C longs (8 bytes on 64-bit Linux).
            size = {8: 1, 16: 2, 32: ctypes.sizeof(ctypes.c_long)}.get(fmt.value, 0)
            return fmt.value, ctypes.string_at(data, size * count.value), count.value
        finally:
            self.x.XFree(data)

    def _windows(self, name: str) -> list[int]:
        fmt, raw, n = self._property(self.root, name)
        if fmt != 32 or not n:
            return []
        return list((ctypes.c_ulong * n).from_buffer_copy(raw))

    def title(self, window: int) -> str:
        fmt, raw, _ = self._property(window, "_NET_WM_NAME")
        if fmt != 8 or not raw:
            fmt, raw, _ = self._property(window, "WM_NAME")
        return raw.decode("utf-8", errors="replace") if fmt == 8 else ""

    def active_window(self) -> int:
        found = self._windows("_NET_ACTIVE_WINDOW")
        return found[0] if found else 0

    def client_windows(self) -> list[int]:
        """Top-level windows, bottom to top, as the window manager lists them."""
        return self._windows("_NET_CLIENT_LIST_STACKING") or self._windows("_NET_CLIENT_LIST")

    def client_rect(self, window: int) -> Rect | None:
        """A shown window's inside area on the screen (x, y, w, h), or None if it's hidden or minimized."""
        attrs = _XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, window, ctypes.byref(attrs)):
            return None
        if attrs.map_state != _IS_VIEWABLE:
            return None
        x, y, child = ctypes.c_int(), ctypes.c_int(), ctypes.c_ulong()
        if not self.x.XTranslateCoordinates(self.dpy, window, self.root, 0, 0, ctypes.byref(x), ctypes.byref(y),
                                            ctypes.byref(child)):
            return None
        return x.value, y.value, attrs.width, attrs.height

    # keys and buttons ---------------------------------------------------------------------
    def keymap(self) -> bytes:
        buf = ctypes.create_string_buffer(32)
        self.x.XQueryKeymap(self.dpy, buf)
        return buf.raw

    def keycodes(self, vk: int) -> tuple[int, ...]:
        if vk not in self._keycodes:
            codes = (self.x.XKeysymToKeycode(self.dpy, sym) for sym in keysyms_for_vk(vk))
            self._keycodes[vk] = tuple(c for c in codes if c)
        return self._keycodes[vk]

    def buttons(self) -> bytes:
        """The mouse buttons held now, as an XInput2 button mask (bit n = button n). b"" if it can't tell."""
        if self.xi is not None and self._pointer is not None:
            root, child = ctypes.c_ulong(), ctypes.c_ulong()
            rx, ry, wx, wy = ctypes.c_double(), ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
            state, mods, group = _XIButtonState(), _XIModifierState(), _XIModifierState()
            if self.xi.XIQueryPointer(self.dpy, self._pointer, self.root, ctypes.byref(root), ctypes.byref(child),
                                      ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(wx), ctypes.byref(wy),
                                      ctypes.byref(state), ctypes.byref(mods), ctypes.byref(group)):
                try:
                    return ctypes.string_at(state.mask, state.mask_len) if state.mask else b""
                finally:
                    if state.mask:
                        self.x.XFree(state.mask)
        # No XInput2: the core pointer only knows buttons 1-5 (Button1Mask is bit 8 of the state).
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        ints = [ctypes.c_int() for _ in range(4)]
        mask = ctypes.c_uint()
        if not self.x.XQueryPointer(self.dpy, self.root, ctypes.byref(root), ctypes.byref(child),
                                    *(ctypes.byref(i) for i in ints), ctypes.byref(mask)):
            return b""
        return bytes([(mask.value >> 8 & 0x1F) << 1, 0])

    def side_buttons_visible(self) -> bool:
        return self.xi is not None and self._pointer is not None


def dota_window(title: str) -> tuple[int, Rect] | None:
    """(window id, inside area) of the shown window with this title, the topmost if there are several."""
    conn = X11.get()
    if conn is None:
        return None
    try:
        for window in reversed(conn.client_windows()):
            if conn.title(window) == title:
                rect = conn.client_rect(window)
                if rect is not None:
                    return window, rect
    except (OSError, ValueError):
        return None
    return None


def active_title() -> str:
    conn = X11.get()
    if conn is None:
        return ""
    try:
        window = conn.active_window()
        return conn.title(window) if window else ""
    except (OSError, ValueError):
        return ""


def key_down(vk: int) -> bool:
    """Is this key or mouse button (a Windows virtual-key code) held right now?"""
    conn = X11.get()
    if conn is None:
        return False
    try:
        if vk in VK_BUTTONS:
            return button_in_mask(conn.buttons(), VK_BUTTONS[vk])
        codes = conn.keycodes(vk)
        if not codes:
            return False
        keymap = conn.keymap()
        return any(keycode_down(keymap, c) for c in codes)
    except (OSError, ValueError):
        return False


def held(codes) -> set[int]:
    """Which of these keys and mouse buttons (Windows virtual-key codes) are held right now: one look at the
    keyboard (and one at the mouse, if a button is asked about) for all of them."""
    conn = X11.get()
    if conn is None:
        return set()
    out: set[int] = set()
    try:
        keys = [vk for vk in codes if vk not in VK_BUTTONS]
        if keys:
            keymap = conn.keymap()
            out.update(vk for vk in keys if any(keycode_down(keymap, c) for c in conn.keycodes(vk)))
        buttons = [vk for vk in codes if vk in VK_BUTTONS]
        if buttons:
            mask = conn.buttons()
            out.update(vk for vk in buttons if button_in_mask(mask, VK_BUTTONS[vk]))
    except (OSError, ValueError):
        return set()
    return out


def available() -> bool:
    return X11.get() is not None
