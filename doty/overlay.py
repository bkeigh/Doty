"""What Doty draws over Dota: advice cards, a status badge, and the coach's marks with its pointer (PySide6).

Three always-on-top windows, styled like the app (dark panels with a cut corner, condensed capitals, cyan
for Doty):
- **Cards** (`Overlay`): a column of advice cards, color-coded by what they're about (cyan Doty, gold
  money and items, green levels, amber timers, red danger). Transparent to the mouse, so clicks go to
  the game. Its size follows the game window's height (hud.py), so it reads the same at 1080p, 1440p
  ultrawide or 4K.
- **Status badge** (`StatusBadge`): a small round badge above the cards. An eye means Doty is watching
  your game; turning arrows mean it's waiting for Dota's game data; red means something stopped.
  Hover it for the details. It's also the handle: drag it to move the cards anywhere over the game
  (remembered), double-click to put them back. It's the only part that takes the mouse, and it never
  takes the keyboard focus from the game.
- **Live strip** (`HudStrip`): above the cards, what's next at a glance: the next runes and objectives
  with a countdown, and the item you're building with the gold still to pay and when you'll have it.
- **Marks** (`MarkLayer`): covers the game window only while the coach is pointing at something.
  A cyan pointer darts to the spot, then circles, arrows and boxes draw themselves in with short
  labels; heroes are colored by team (green allies, red enemies). Hidden the rest of the time. Only
  the part of the window that changes is redrawn (a 3440x1440 layer repainted whole sixty times a
  second costs the game frames), and drawings on the game world go away once you've walked on.

While another app's window is in front of Dota (you alt-tabbed to a browser), everything hides.

Works over Dota in borderless window or windowed mode. Exclusive fullscreen bypasses the desktop
compositor, so no normal window can draw over it. Drawing over your own screen is display only: it
reads nothing from the game and sends nothing to it.

Linux (X11, or XWayland on a Wayland desktop): the windows bypass the window manager, so it can't move them
or put the game over them, and the 2 s re-pin raises them again. Qt's own flags make them click-through.
"""

from __future__ import annotations

import html
import math
import re
import sys
import time
from typing import Callable

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPen,
                           QPolygonF, QRegion, QStaticText)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

from . import hud
from .advice import Advice, Priority, tone
from .config import OverlayConfig
from .ui import theme
from .ui.brand import cut_path, display_font, paint_eye, paint_sync, register_fonts, slant_path
from .win import dota_covered, find_dota_rect

COACH = theme.BRAND
TONE = {  # mark colors: a touch brighter than the app's, since they sit on a busy, bright game
    "coach": QColor(theme.BRAND_BRIGHT),
    "danger": QColor("#ff5b45"),
    "good": QColor("#5fe07a"),
    "info": QColor("#7ab8ff"),
}
# What each card is about -> its color and tag. Same family as the app's feed, with money and levels
# picked out, so a glance at the color says what kind of advice it is.
CATEGORY_STYLE = {
    "economy": (theme.GOLD, "GOLD"),
    "items": (theme.GOLD, "ITEMS"),
    "progression": (theme.GREEN, "LEVEL"),
    "timer": (theme.AMBER, "TIMER"),
    "runes": (theme.AMBER, "RUNES"),
    "objectives": (theme.AMBER, "OBJECTIVE"),
    "survival": (theme.RED, "DANGER"),
}
STATUS_STYLE = {  # badge state -> (icon, color)
    "live": ("eye", theme.GREEN),
    "limited": ("eye", theme.AMBER),
    "waiting": ("sync", theme.AMBER),
    "stopped": ("sync", theme.RED),
}

WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20
WDA_EXCLUDEFROMCAPTURE = 0x11
HWND_TOPMOST = -1
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
DRAW_IN_S = 0.35
DRAW_AFTER_S = 0.12  # the first mark starts drawing this soon (the pointer is still arriving)
DRAW_GAP_S = 0.12  # and each next one this much later
POINTER_S = 0.28  # the pointer's dart to the first mark
POINTER_FROM = 420.0  # ...from at most this far (at 1080p), so only that part of the window is redrawn
FADE_S = 0.4
MARK_TTL_S = 6.5  # drawings are about this moment; they shouldn't linger into the next one
ARROW_TTL_S = 4.5  # an arrow's moment passes faster than a circled hero's
MINIMAP_COLOR = QColor("#ffd970")
ANIMATE_S = 2.0  # draw-in plus the minimap pings; after that the layer stops repainting


class OverlayBridge(QObject):
    """Cross-thread entry point: emit from any thread, handled on the GUI thread."""

    advice = Signal(object)
    status = Signal(str, str)  # state, text
    laning = Signal(object)  # LaningInfo or None
    hud = Signal(object)  # the live strip's {timers, item}, or None
    drop_marks = Signal(str)  # a space ("screen"): its drawings have gone stale
    toggle = Signal()


def _screen_at_native(x: int, y: int):
    """The screen holding a point in Windows' (physical) pixels. Qt 6 keeps each screen's top-left at its
    physical position and scales only from there, so a screen's physical rect is its logical top-left
    plus its logical size times its scale."""
    for screen in QGuiApplication.screens():
        g, dpr = screen.geometry(), screen.devicePixelRatio()
        if QRect(g.x(), g.y(), round(g.width() * dpr), round(g.height() * dpr)).contains(x, y):
            return screen
    return QGuiApplication.primaryScreen()


def native_to_logical(x: int, y: int, w: int, h: int) -> tuple[QRect, float]:
    """A rect in physical pixels as Qt's logical pixels on its screen (right on a scaled second monitor,
    where dividing by the scale alone lands it on the wrong spot), and that screen's scale."""
    screen = _screen_at_native(x + w // 2, y + h // 2)
    g, dpr = screen.geometry(), screen.devicePixelRatio()
    return QRect(round(g.x() + (x - g.x()) / dpr), round(g.y() + (y - g.y()) / dpr),
                 round(w / dpr), round(h / dpr)), dpr


def logical_to_native(screen, x: int, y: int) -> tuple[int, int]:
    """The other way: a logical point on `screen` in physical pixels."""
    g, dpr = screen.geometry(), screen.devicePixelRatio()
    return round(g.x() + (x - g.x()) * dpr), round(g.y() + (y - g.y()) * dpr)


def dota_area() -> tuple[QRect, float] | None:
    """The Dota window in logical pixels, and its screen's device-pixel ratio."""
    dota = find_dota_rect()
    if not dota:
        return None
    return native_to_logical(*dota)


def _win_style(widget: QWidget, click_through: bool, exclude_from_capture: bool) -> None:
    """Never take focus from the game; optionally let clicks fall through to it."""
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = int(widget.winId())
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE
    if click_through:
        style |= WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    if exclude_from_capture:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    _pin(widget)


def _click_through(widget: QWidget, exclude_from_capture: bool) -> None:
    _win_style(widget, True, exclude_from_capture)


def _pin(widget: QWidget) -> None:
    if sys.platform == "win32" and widget.isVisible():
        import ctypes

        ctypes.windll.user32.SetWindowPos(int(widget.winId()), HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
    elif sys.platform.startswith("linux") and widget.isVisible():
        widget.raise_()  # (no window manager keeps it on top: see _overlay_flags)


def _overlay_flags(widget: QWidget, click_through: bool = True) -> None:
    flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
    if click_through:
        flags |= Qt.WindowTransparentForInput
    if sys.platform.startswith("linux"):
        # X11: outside the window manager's control, so it neither moves the overlay nor stacks the game
        # (fullscreen or not) above it.
        flags |= Qt.X11BypassWindowManagerHint
    widget.setWindowFlags(flags)
    widget.setAttribute(Qt.WA_TranslucentBackground)
    widget.setAttribute(Qt.WA_ShowWithoutActivating)


def card_tag(source: str, category: str, priority: int) -> str:
    """The short tag on a line: DOTY, YOU, or what it's about (GOLD, LEVEL, TIMER, DANGER...). The app's
    feed shows the same one, so a line reads the same in both places."""
    kind = tone(source, category, int(priority))
    if kind in ("coach", "quiet"):
        return "DOTY"
    if kind == "you":
        return "YOU"
    tag = CATEGORY_STYLE.get(category, ("", ""))[1]
    return tag or ("NOW" if kind == "now" else (category or "info").upper())


def card_style(adv: Advice) -> tuple[str, str]:
    """(color, tag) for a card."""
    kind = tone(adv.source, adv.category, int(adv.priority))
    tag = card_tag(adv.source, adv.category, int(adv.priority))
    if kind in ("coach", "quiet"):
        return COACH, tag
    if kind == "you":
        return theme.NEUTRAL, tag
    if kind == "now":
        return theme.RED, tag
    color = CATEGORY_STYLE.get(adv.category, ("", ""))[0]
    return (color or (theme.AMBER if kind == "soon" else theme.BLUE)), tag


def _rgba(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


# --------------------------------------------------------------------------- cards


class Card(QWidget):
    """One piece of advice: a slanted tag over the text, on a dark panel with a cut corner and a colored
    edge. Urgent cards get a red plate; coach cards lead with the action, big, and the reason below."""

    def __init__(self, adv: Advice, show_latency: bool, scale: float) -> None:
        super().__init__()
        register_fonts()
        self.setAttribute(Qt.WA_TranslucentBackground)  # the cut corner shows the game through it
        self.color, self.tag_text = card_style(adv)
        self.scale, self.coach = scale, adv.source == "coach"
        self.urgent = adv.priority == Priority.CRITICAL and adv.source == "rules"
        s = scale
        self._tag_h = round(19 * s)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(round(16 * s), round(10 * s) + self._tag_h + round(6 * s), round(14 * s),
                               round(11 * s))
        lay.setSpacing(round(3 * s))
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setFont(QFont(theme.FONT))
        self.body.setStyleSheet("background: transparent;")
        lay.addWidget(self.body)
        self.latency = ""
        self._set_text(adv.text)
        self.show_latency = show_latency
        self.set_latency(adv)
        self.effect = QGraphicsOpacityEffect(self)
        self.effect.setOpacity(1.0)
        self.setGraphicsEffect(self.effect)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.anim: QPropertyAnimation | None = None

    def _set_text(self, text: str) -> None:
        s = self.scale
        family = f"font-family:'{theme.FONT}';"
        if self.coach:  # the action first, big; the why after, smaller
            parts = re.split(r"(?<=[.!?;])\s+", text, maxsplit=1)
            first, rest = parts[0], parts[1] if len(parts) > 1 else ""
            body = (f'<div style="{family} font-size:{round(19 * s)}px; font-weight:700; color:{theme.TEXT}">'
                    f'{html.escape(first)}</div>')
            if rest:
                body += (f'<div style="{family} font-size:{round(15 * s)}px; color:{theme.TEXT_2}">'
                         f'{html.escape(rest)}</div>')
        else:
            size = 19 if self.urgent else 17
            body = (f'<div style="{family} font-size:{round(size * s)}px; font-weight:{700 if self.urgent else 600};'
                    f' color:{theme.TEXT}">{html.escape(text)}</div>')
        self.body.setText(body)

    def update_from(self, adv: Advice) -> None:
        self._set_text(adv.text)
        self.set_latency(adv)

    def set_latency(self, adv: Advice) -> None:
        if self.show_latency and adv.latency_ms is not None:
            self.latency = f"{adv.latency_ms:.1f} ms"
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        s = self.scale
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = cut_path(r, 11 * s)
        p.fillPath(path, QColor(56, 12, 9, 242) if self.urgent else QColor(9, 12, 16, 236))
        p.save()
        p.setClipPath(path)
        p.fillRect(QRectF(r.left(), r.top(), 4 * s, r.height()), QColor(self.color))  # the colored edge
        edge = QLinearGradient(r.topLeft(), QPointF(r.left() + r.width() * 0.8, r.top()))
        edge.setColorAt(0.0, QColor(self.color))
        edge.setColorAt(1.0, _rgba(self.color, 0))
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 2 * s), edge)
        p.restore()
        p.setPen(QPen(_rgba(self.color, 90 if self.urgent else 60), 1.0))
        p.drawPath(path)
        # The tag: a slanted plate in the card's color, dark text (the app's tags look the same).
        f = display_font(round(13 * s), QFont.Bold, 1.3 * s)
        p.setFont(f)
        tw = QFontMetricsF(f).horizontalAdvance(self.tag_text) + 18 * s
        tag = QRectF(16 * s, 10 * s, tw, self._tag_h)
        p.fillPath(slant_path(tag, 6 * s), QColor(self.color))
        p.setPen(QColor(theme.ink_on(self.color)))
        p.drawText(tag.adjusted(0, 0, -3 * s, 0), Qt.AlignCenter, self.tag_text)
        if self.latency:
            p.setFont(display_font(round(11 * s), QFont.Medium))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(tag.right(), tag.top(), r.width() - tag.right() - 14 * s, tag.height()),
                       Qt.AlignRight | Qt.AlignVCenter, self.latency)
        p.end()


# --------------------------------------------------------------------------- laning gauge


class LaningGauge(QWidget):
    """The first ten minutes: last hits (and denies) a minute against a target for your hero, as one slim
    bar at the top of the card column. Green on pace, amber a bit behind, red well behind."""

    def __init__(self, scale: float) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.info: dict | None = None
        self.scale = scale
        self.setFixedHeight(round(30 * scale))
        self.hide()

    def set_info(self, info: dict | None) -> None:
        self.info = info
        self.setVisible(info is not None)
        self.update()

    def color(self) -> str:
        ratio = (self.info or {}).get("ratio", 1.0)
        return theme.GREEN if ratio >= 0.95 else theme.AMBER if ratio >= 0.75 else theme.RED

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self.info:
            return
        s, info = self.scale, self.info
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = cut_path(r, 8 * s)
        p.fillPath(path, QColor(9, 12, 16, 225))
        color = self.color()
        p.setFont(display_font(round(13 * s), QFont.Bold, 1.0 * s))
        p.setPen(QColor(theme.TEXT_2))
        left = QRectF(12 * s, 0, r.width() * 0.62, r.height() - 6 * s)
        p.drawText(left, Qt.AlignLeft | Qt.AlignVCenter,
                   f"LAST HITS {info['lh']} · {info['pace']:.1f}/MIN · DENIES {info['dn']}")
        p.setPen(QColor(color))
        p.drawText(QRectF(left.right(), 0, r.width() - left.right() - 12 * s, r.height() - 6 * s),
                   Qt.AlignRight | Qt.AlignVCenter, f"TARGET {info['target']:.1f}")
        track = QRectF(12 * s, r.height() - 7 * s, r.width() - 24 * s, 3 * s)
        p.fillRect(track, QColor(255, 255, 255, 30))
        p.fillRect(QRectF(track.left(), track.top(), track.width() * min(1.0, info["ratio"]), track.height()),
                   QColor(color))
        p.end()


# --------------------------------------------------------------------------- live strip


def _clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _static(text: str) -> QStaticText:
    """Text drawn exactly as given: hero and item names and words already in the player's language
    mustn't go through the app's automatic translation of painted text (i18n.install_qt) again."""
    st = QStaticText(text)
    st.setTextFormat(Qt.PlainText)
    return st


class HudStrip(QWidget):
    """What's next, always in view at the top of the card column. One row for the next spawns with a
    countdown (amber in the last twenty seconds), one for the item you're building: its name, the gold
    still to pay (the parts you hold and the gold in your pocket count) and when you'll have it at your
    gold per minute, over a bar of how far along it is."""

    ROW = 28  # px at 1080p
    SOON_S = 20

    def __init__(self, scale: float) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.info: dict | None = None
        self.scale = scale
        self.hide()

    def rows(self) -> int:
        info = self.info or {}
        return int(bool(info.get("timers"))) + int(bool(info.get("item")))

    def set_info(self, info: dict | None) -> None:
        self.info = info
        self._resize()
        self.setVisible(self.rows() > 0)
        self.update()

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self._resize()

    def _resize(self) -> None:
        if self.rows():
            self.setFixedHeight(round((self.ROW * self.rows() + 8) * self.scale))

    def item_text(self) -> str:
        """The item row's right-hand side: "1,143 to go · ~13:20", or that it's affordable."""
        from . import i18n

        item = (self.info or {}).get("item") or {}
        left = int(item.get("left") or 0)
        if left <= 0:
            return i18n.tr("Affordable now")
        text = i18n.tr("{gold} to go", gold=f"{left:,}")
        return text + (f" · ~{_clock(item['eta'])}" if item.get("eta") is not None else "")

    def paintEvent(self, _event) -> None:  # noqa: N802
        info = self.info or {}
        if not self.rows():
            return
        from . import i18n

        s = self.scale
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.fillPath(cut_path(r, 8 * s), QColor(9, 12, 16, 225))
        pad, row_h = 12 * s, self.ROW * s
        name_font = display_font(round(14 * s), QFont.Bold, 1.0 * s)
        num_font = display_font(round(15 * s), QFont.Bold, 0.6 * s)
        name_fm, num_fm = QFontMetricsF(name_font), QFontMetricsF(num_font)

        def text_at(font: QFont, fm: QFontMetricsF, text: str, x: float, top: float, height: float) -> None:
            p.setFont(font)
            p.drawStaticText(QPointF(x, top + (height - fm.height()) / 2), _static(text))

        y = 4 * s
        timers = list(info.get("timers") or [])[:2]
        if len(timers) > 1 and not isinstance(timers[0][1], str) and len(timers[0][1]) > 1:
            timers = timers[:1]  # two spawns at once need the row's whole width for their names
        if timers:
            gap = 14 * s if len(timers) > 1 else 0.0
            width = (r.width() - 2 * pad - gap) / len(timers)
            time_w = num_fm.horizontalAdvance("00:00")
            for k, (in_s, labels) in enumerate(timers):
                x = pad + k * (width + gap)
                soon = in_s <= self.SOON_S
                clock = _clock(in_s)
                p.setPen(QColor(theme.AMBER if soon else theme.TEXT))
                text_at(num_font, num_fm, clock, x + width - num_fm.horizontalAdvance(clock), y, row_h)
                p.setPen(QColor(theme.AMBER if soon else theme.TEXT_2))
                # (spawns landing together share the cell: "BOUNTY RUNES + POWER RUNE")
                words = " + ".join(str(i18n.tr(lb)) for lb in ([labels] if isinstance(labels, str) else labels))
                name = name_fm.elidedText(words.upper(), Qt.ElideRight, width - time_w - 8 * s)
                text_at(name_font, name_fm, name, x, y, row_h)
            y += row_h
        item = info.get("item")
        if item:
            width = r.width() - 2 * pad
            ready = int(item.get("left") or 0) <= 0
            color = QColor(theme.GREEN if ready else theme.GOLD)
            status = self.item_text().upper()
            status_w = num_fm.horizontalAdvance(status)
            p.setPen(color)
            text_at(num_font, num_fm, status, pad + width - status_w, y, row_h - 5 * s)
            p.setPen(QColor(theme.TEXT_2))
            name = name_fm.elidedText(str(item.get("name") or "").upper(), Qt.ElideRight, width - status_w - 10 * s)
            text_at(name_font, name_fm, name, pad, y, row_h - 5 * s)
            track = QRectF(pad, y + row_h - 6 * s, width, 3 * s)
            p.fillRect(track, QColor(255, 255, 255, 30))
            done = max(0.0, min(1.0, float(item.get("done") or 0.0)))
            p.fillRect(QRectF(track.left(), track.top(), track.width() * done, track.height()), color)
        p.end()


# --------------------------------------------------------------------------- status badge


class StatusBadge(QWidget):
    """The round badge above the cards: what Doty is doing at a glance, the details on hover, and the
    handle to move the cards."""

    def __init__(self, overlay: "Overlay") -> None:
        super().__init__(None)
        self.overlay = overlay
        _overlay_flags(self, click_through=False)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeAllCursor)
        self.state, self.text = "waiting", "Waiting for Dota's game data"
        self.size_px, self.right = 30, True
        self.anchor = QPoint()
        self.hover = False
        self.angle = 0.0
        self._drag: tuple[QPoint, QPoint] | None = None
        self._spin = QTimer(self)
        self._spin.setInterval(33)
        self._spin.timeout.connect(self._turn)
        self._styled = False
        # The details panel opens after a short rest on the circle, not as the cursor passes on its way across the
        # game; and it closes as soon as the cursor leaves the circle, so it never catches a click for Dota.
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.setInterval(400)
        self._open_timer.timeout.connect(self._open_panel)

    # state -----------------------------------------------------------------
    def set_status(self, state: str, text: str) -> None:
        self.state = state if state in STATUS_STYLE else "waiting"
        self.text = text or self.text
        self._sync_spin()
        self.setAccessibleName(f"Doty: {self.text}")
        self._relayout()
        self.update()

    def _sync_spin(self) -> None:
        """The turning arrows turn only while they show (a hidden badge costs nothing)."""
        icon = STATUS_STYLE[self.state][0]
        if icon == "sync" and self.state == "waiting" and self.isVisible():
            self._spin.start()
        else:
            self._spin.stop()

    def _turn(self) -> None:
        self.angle = (self.angle + 9.0) % 360
        self.update()

    # geometry --------------------------------------------------------------
    def place(self, anchor: QPoint, size: int, right: bool) -> None:
        """`anchor`: the badge circle's top-left (global logical px). The hover panel opens inward."""
        if (anchor, size, right) == (self.anchor, self.size_px, self.right):
            return
        self.anchor, self.size_px, self.right = QPoint(anchor), size, right
        self._relayout()

    def _panel_width(self) -> int:
        s = self.size_px / 30
        f1 = display_font(round(13 * s), QFont.Bold, 0.6 * s)
        f2 = display_font(round(11 * s), QFont.Medium, 0.4 * s)
        w = max(QFontMetricsF(f1).horizontalAdvance(self.text.upper()),
                QFontMetricsF(f2).horizontalAdvance(HINT))
        return int(w + 34 * s)

    def _relayout(self) -> None:
        b = self.size_px
        w = b + (self._panel_width() if self.hover else 0)
        h = round(b * 1.5) if self.hover else b  # two lines of text when open
        x = self.anchor.x() - (w - b) if self.right else self.anchor.x()
        self.setGeometry(x, self.anchor.y(), w, h)
        self.setWindowOpacity(1.0 if self.hover or self.state != "live" else 0.6)

    # mouse -----------------------------------------------------------------
    def _circle(self) -> QRect:
        b = self.size_px
        return QRect(self.width() - b if self.right else 0, 0, b, b)

    def _open_panel(self) -> None:
        from PySide6.QtGui import QCursor

        if self._circle().contains(self.mapFromGlobal(QCursor.pos())):
            self.hover = True
            self._relayout()

    def _close_panel(self) -> None:
        self._open_timer.stop()
        if self.hover:
            self.hover = False
            self._relayout()

    def enterEvent(self, _event) -> None:  # noqa: N802
        self._open_timer.start()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._drag is None:
            self._close_panel()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag = (event.globalPosition().toPoint(), self.overlay.column_origin())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None and self.hover and not self._circle().contains(event.position().toPoint()):
            self._close_panel()  # (off the circle: the panel goes, and the click under it reaches Dota)
            return
        if self._drag is not None:
            start, origin = self._drag
            self.overlay.drag_to(origin + (event.globalPosition().toPoint() - start), self.right)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.button() == Qt.LeftButton:
            moved = (event.globalPosition().toPoint() - self._drag[0]).manhattanLength() > 3
            self._drag = None
            if moved:
                self.overlay.drag_done()

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self._drag = None
        self.overlay.reset_position()

    # painting --------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._styled:
            _win_style(self, False, self.overlay.cfg.exclude_from_capture)
            self._styled = True
        self._sync_spin()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._spin.stop()
        self._close_panel()

    def paintEvent(self, _event) -> None:  # noqa: N802
        icon, color = STATUS_STYLE[self.state]
        b = self.size_px
        s = b / 30
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        circle = QRectF(self.width() - b if self.right else 0, 0, b, b).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.hover:  # the details, on a panel that opens toward the middle of the screen
            panel = QRectF(0 if self.right else b * 0.5, 1 * s, self.width() - b * 0.5, self.height() - 2 * s)
            path = cut_path(panel, 7 * s)
            p.fillPath(path, QColor(9, 12, 16, 240))
            p.setPen(QPen(_rgba(color, 120), 1.0))
            p.drawPath(path)
            p.fillRect(QRectF(panel.left(), panel.top(), panel.width() - 7 * s, 2 * s), QColor(color))
            tx = panel.left() + (12 * s if self.right else b * 0.5 + 10 * s)
            tw = panel.width() - b * 0.5 - 22 * s
            mid = panel.top() + panel.height() * 0.54
            p.setFont(display_font(round(13 * s), QFont.Bold, 0.6 * s))
            p.setPen(QColor(theme.TEXT))
            p.drawText(QRectF(tx, panel.top(), tw, mid - panel.top() + 1 * s), Qt.AlignLeft | Qt.AlignBottom,
                       self.text.upper())
            p.setFont(display_font(round(11 * s), QFont.Medium, 0.4 * s))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(tx, mid + 3 * s, tw, panel.bottom() - mid - 3 * s), Qt.AlignLeft | Qt.AlignTop, HINT)
        p.setPen(QPen(QColor(color), 2.2 * s))
        p.setBrush(QColor(9, 12, 16, 235))
        p.drawEllipse(circle)
        c = circle.center()
        if icon == "eye":
            _paint_eye(p, c, b * 0.30, QColor(color), s)
        else:
            _paint_sync(p, c, b * 0.24, QColor(color), s, self.angle)
        p.end()


HINT = "Drag to move · double-click to reset"
_paint_eye, _paint_sync = paint_eye, paint_sync  # shared with the app's top bar and the tray (ui/brand.py)


# --------------------------------------------------------------------------- the card column


class Overlay(QWidget):
    """The card column. Owns the status badge and the mark layer, and follows the Dota window as it
    opens or moves."""

    def __init__(self, cfg: OverlayConfig, minimap_side: str = "left",
                 on_moved: Callable[[], None] | None = None) -> None:
        super().__init__(None)
        self.cfg = cfg
        self.on_moved = on_moved
        self.cards: dict[int, Card] = {}
        self.bridge = OverlayBridge()
        self.bridge.advice.connect(self._on_advice)
        self.bridge.status.connect(self._on_status)
        self.bridge.toggle.connect(self._on_toggle)
        _overlay_flags(self)
        self.setWindowOpacity(1.0)  # transparency lives in the card plates; text stays solid
        # Physical-pixel box around the visible cards, read by the capture thread.
        self.mask_rect_px: tuple[int, int, int, int] | None = None
        self.scale = 1.0
        self.marks = MarkLayer(cfg, minimap_side)
        self.badge = StatusBadge(self)
        self._found = False
        self._column = QRect()  # the column including the badge row, global logical px

        self.stack = QVBoxLayout(self)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(8)
        self.strip = HudStrip(1.0)
        self.stack.addWidget(self.strip)
        self.gauge = LaningGauge(1.0)
        self.stack.addWidget(self.gauge)
        self.stack.addStretch(1)
        self.bridge.laning.connect(self._on_laning)
        self.bridge.hud.connect(self._on_hud)
        self.bridge.drop_marks.connect(self.marks.drop)
        self._covered = False  # another app's window is in front of Dota: nothing of Doty's shows over it

        self._placed: tuple | None = None
        self._dragging = False  # while you drag the badge, the 2 s re-placing leaves it alone
        self._side: bool | None = None  # the side you dragged the badge to (right = True), for this session
        self._place()
        # Some games re-assert z-order on focus changes; keep re-pinning ourselves, and follow
        # the Dota window if it opens, moves or changes size after Doty starts.
        self._pin = QTimer(self)
        self._pin.timeout.connect(lambda: (_pin(self), _pin(self.marks), _pin(self.badge)))
        self._pin.timeout.connect(self._place)
        self._pin.start(2000)
        # Fade out of the way while the mouse is over the cards (e.g. reaching for the shop).
        self._hover = QTimer(self)
        self._hover.timeout.connect(self._fade_under_mouse)
        self._hover.start(100)
        # And out of sight while another app is in front of Dota (you alt-tabbed to a browser).
        self._cover = QTimer(self)
        self._cover.timeout.connect(self._check_covered)
        self._cover.start(500)

    # public, thread-safe -----------------------------------------------------
    def submit(self, adv: Advice) -> None:
        self.bridge.advice.emit(adv)

    def set_status(self, text: str, state: str = "") -> None:
        """`state`: live | limited | waiting | stopped (see STATUS_STYLE); `text` shows on hover."""
        self.bridge.status.emit(state or ("live" if not text else "waiting"), text)

    def toggle_visible(self) -> None:
        self.bridge.toggle.emit()

    def set_laning(self, info: dict | None) -> None:
        """{lh, dn, pace, target, ratio} for the laning gauge, or None to hide it. Thread-safe."""
        self.bridge.laning.emit(info)

    def set_hud(self, info: dict | None) -> None:
        """The live strip: {"timers": [(seconds away, [labels]), ...], "item": {name, left, eta, done}}, or
        None to hide it. Thread-safe."""
        self.bridge.hud.emit(info)

    def drop_marks(self, space: str = "screen") -> None:
        """Take down the drawings in one space ("screen": you've walked on, so they point at the wrong
        spot now). Thread-safe."""
        self.bridge.drop_marks.emit(space)

    # GUI thread --------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        _click_through(self, self.cfg.exclude_from_capture)
        self._show_badge()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.marks.clear()
        self.badge.hide()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.marks.close()
        self.badge.close()
        super().closeEvent(event)

    def _on_laning(self, info) -> None:
        self.gauge.set_info(info)
        QTimer.singleShot(0, self._update_mask)

    def _on_hud(self, info) -> None:
        self.strip.set_info(info if self.cfg.strip else None)
        QTimer.singleShot(0, self._update_mask)

    def _check_covered(self) -> None:
        covered = self._found and dota_covered()
        if covered == self._covered:
            return
        self._covered = covered
        if covered:
            self.marks.clear()
        self.setWindowOpacity(0.0 if covered else 1.0)
        self._show_badge()

    def _show_badge(self) -> None:
        self.badge.setVisible(self.isVisible() and self._found and not self._covered)
        if self.badge.isVisible():
            _pin(self.badge)

    def _place(self) -> None:
        if self._dragging:
            return
        found = dota_area()
        self._found = found is not None
        area = found[0] if found else QGuiApplication.primaryScreen().geometry()
        pos = tuple(self.cfg.position) if len(self.cfg.position) == 2 else ()
        key = (area.x(), area.y(), area.width(), area.height(), self.cfg.anchor, self.cfg.size, pos)
        if key != self._placed:
            self._placed = key
            self._layout(area, right=self._side if pos else None)
        self._show_badge()

    def _layout(self, area: QRect, right: bool | None = None) -> None:
        size = max(0.6, min(1.6, self.cfg.size))
        self.scale = s = hud.scale(area.height()) * size
        x, y, w, h = hud.card_column(area.width(), area.height(), self.cfg.anchor)
        sized = min(round(w * size), area.width() - 8)
        x += {"right": w - sized, "top": (w - sized) // 2}.get(self.cfg.anchor, 0)  # keep the anchored edge
        badge = round(30 * s)
        gap = round(8 * s)
        if len(self.cfg.position) == 2:  # moved by hand: fractions of the game window, so it survives resizes
            fx, fy = self.cfg.position
            x = round(min(max(0.0, fx), 1.0) * area.width())
            y = round(min(max(0.0, fy), 1.0) * area.height())
            x = max(0, min(x, area.width() - sized))
            y = max(0, min(y, area.height() - badge - 2 * gap - int(120 * s)))
            h = max(int(160 * s), int(area.height() * (1 - hud.BOTTOM_HUD_FRAC)) - y)
            h = min(h, area.height() - y)
        else:
            y -= badge + gap  # the badge row sits above the usual card spot
            h += badge + gap
        if right is None:
            right = x + sized / 2 > area.width() / 2
        self._column = QRect(area.x() + x, area.y() + y, sized, h)
        self.setGeometry(area.x() + x, area.y() + y + badge + gap, sized, h - badge - gap)
        self.badge.place(QPoint(area.x() + (x + sized - badge if right else x), area.y() + y), badge, right)
        self.stack.setSpacing(round(8 * s))
        self.gauge.scale = s
        self.gauge.setFixedHeight(round(30 * s))
        self.strip.set_scale(s)
        self.marks.place(area)

    # moving by hand ------------------------------------------------------------
    def column_origin(self) -> QPoint:
        return self._column.topLeft()

    def drag_to(self, top_left: QPoint, right: bool) -> None:
        found = dota_area()
        area = found[0] if found else QGuiApplication.primaryScreen().geometry()
        self.cfg.position = [round((top_left.x() - area.x()) / max(1, area.width()), 4),
                             round((top_left.y() - area.y()) / max(1, area.height()), 4)]
        self._placed = None
        self._dragging, self._side = True, right
        self._layout(area, right=right)  # keep the badge on the same side while dragging

    def drag_done(self) -> None:
        self._dragging = False
        self._placed = None
        self._place()
        if self.on_moved:
            self.on_moved()

    def reset_position(self) -> None:
        self.cfg.position = []
        self._side = None
        self.drag_done()

    def _on_status(self, state: str, text: str) -> None:
        self.badge.set_status(state, text)

    def _fade_under_mouse(self) -> None:
        if not self.isVisible() or self._covered:
            return
        from PySide6.QtGui import QCursor

        pos = QCursor.pos()  # (over a card itself, not anywhere in the column's tall window)
        under = any(c.isVisible() and QRect(c.mapToGlobal(QPoint(0, 0)), c.size()).contains(pos)
                    for c in self.cards.values())
        target = 0.2 if under else 1.0
        if abs(self.windowOpacity() - target) > 0.01:
            self.setWindowOpacity(target)

    def _on_toggle(self) -> None:
        self.setVisible(not self.isVisible())

    def _on_advice(self, adv: Advice) -> None:
        if not adv.display or not adv.overlay:
            return
        card = self.cards.get(adv.id)
        if card is None:
            card = Card(adv, self.cfg.show_latency, self.scale)
            card.timer.timeout.connect(lambda c=card, i=adv.id: self._fade(i, c))
            self.cards[adv.id] = card
            self.stack.insertWidget(2, card)  # newest on top, under the live strip and the laning gauge
            while len(self.cards) > self.cfg.max_cards:
                self._remove(min(self.cards))
        else:
            card.update_from(adv)
        ttl = adv.ttl_s if adv.ttl_s is not None else self.cfg.ttl_s
        ttl = max(ttl, 3.0 + 0.06 * len(adv.text))  # long lines stay up long enough to read
        if adv.source == "coach":
            ttl = max(ttl, 12.0)
        card.timer.start(int(ttl * 1000))
        if adv.final and adv.marks and self.cfg.draw and self.isVisible() and not self._covered:
            self.marks.show_marks(adv.marks, origin=self._card_anchor(card))
        QTimer.singleShot(0, self._update_mask)

    def _card_anchor(self, card: Card) -> QPointF:
        """Where the pointer starts: the card's inner edge, in global logical pixels."""
        g = self.geometry()
        left = self.badge.right
        return QPointF(g.left() if left else g.right(), g.top() + card.geometry().center().y())

    def _update_mask(self) -> None:
        # Everything Doty shows in the column (cards, the live strip, the laning gauge) is blacked out of
        # the coach's screenshots, so it never reads its own words back.
        shown = [w for w in (*self.cards.values(), self.strip, self.gauge) if w.isVisible()]
        if not shown or not self.isVisible():
            self.mask_rect_px = None
            return
        bottom = max(w.geometry().bottom() for w in shown)
        screen = self.screen() or QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio()
        x, y = logical_to_native(screen, self.x(), self.y())
        self.mask_rect_px = (x, y, int(self.width() * dpr), int((bottom + 8) * dpr))

    def _fade(self, advice_id: int, card: Card) -> None:
        anim = QPropertyAnimation(card.effect, b"opacity", card)
        anim.setDuration(500)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InQuad)
        anim.finished.connect(lambda: self._remove(advice_id))
        card.anim = anim
        anim.start()

    def _remove(self, advice_id: int) -> None:
        card = self.cards.pop(advice_id, None)
        if card is not None:
            card.timer.stop()
            self.stack.removeWidget(card)
            card.deleteLater()
            QTimer.singleShot(0, self._update_mask)


# --------------------------------------------------------------------------- marks


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


class MarkLayer(QWidget):
    """Doty's drawings over the whole game window, with a pointer that darts to them. The window is as big
    as the game's, but each frame redraws only the patches the marks and the pointer are in."""

    def __init__(self, cfg: OverlayConfig, minimap_side: str = "left") -> None:
        super().__init__(None)
        self.cfg = cfg
        self.minimap_side = minimap_side
        _overlay_flags(self)
        self.area = QRect()
        self.marks: tuple = ()
        self.shown_at = 0.0
        self.ttl = MARK_TTL_S
        self.origin = QPointF()
        self._dirty = QRegion()  # where the marks are (see _mark_region)
        self._pointer_was = QRect()  # where the pointer was drawn last
        self._tick = QTimer(self)
        self._tick.setInterval(16)
        self._tick.timeout.connect(self._frame)
        self._styled = False

    def place(self, area: QRect) -> None:
        if area != self.area:
            self.area = QRect(area)
            self.setGeometry(area)

    def show_marks(self, marks: tuple, origin: QPointF | None = None) -> None:
        if not marks or self.area.isEmpty():
            return
        self.marks, self.shown_at = tuple(marks), time.monotonic()
        self.ttl = ARROW_TTL_S if all(m.shape == "arrow" for m in self.marks) else MARK_TTL_S
        self.origin = (origin - QPointF(self.area.topLeft())) if origin is not None else QPointF(
            self.area.width() * 0.8, self.area.height() * 0.3)
        # The pointer comes from the card's direction, but only the last stretch of the way: a dart across
        # the whole window would mean redrawing the whole window on every frame of it.
        s = self._scale()
        target = self._pointer_target(s)
        dx, dy = self.origin.x() - target.x(), self.origin.y() - target.y()
        far = math.hypot(dx, dy)
        reach = POINTER_FROM * s
        if far > reach:
            self.origin = QPointF(target.x() + dx / far * reach, target.y() + dy / far * reach)
        self._dirty = self._mark_region(s)
        self._pointer_was = QRect()
        self.show()
        if not self._styled:
            _click_through(self, self.cfg.exclude_from_capture)
            self._styled = True
        _pin(self)
        self._tick.setInterval(16)
        self._tick.start()
        self.update()

    def clear(self) -> None:
        self.marks = ()
        self._tick.stop()
        self.hide()

    def drop(self, space: str) -> None:
        """Take down the drawings in one space. "screen" drawings sit on the game world as it was in the
        screenshot: once you've walked on (or a fight has started) they point at the wrong thing."""
        kept = tuple(m for m in self.marks if m.space != space or m.on_hud)  # (the HUD doesn't move)
        if len(kept) == len(self.marks):
            return
        if not kept:
            self.clear()
            return
        self.marks = kept
        self.update()  # (the whole layer once: what was dropped is wiped wherever it was)
        self._dirty = self._mark_region(self._scale())

    def _age(self) -> float:
        return time.monotonic() - self.shown_at

    def _scale(self) -> float:
        return hud.scale(self.height()) * max(0.6, min(1.6, self.cfg.size))

    def _pointer_target(self, s: float) -> QPointF:
        first = self.marks[0]
        x, y, _x2, _y2, unit = first.to_window(self.width(), self.height(), self.minimap_side)
        off = unit * 0.85 if first.shape in ("circle", "point") else 10 * s
        return QPointF(x + off, y + off)

    def _pointer_at(self, age: float, s: float) -> tuple[QPointF, float]:
        t = _ease_out(age / POINTER_S)
        target = self._pointer_target(s)
        return self.origin + (target - self.origin) * t, t

    def _pointer_rect(self, pos: QPointF, s: float) -> QRect:
        """Around the pointer's diamond and its DOTY tag."""
        return QRectF(pos.x() - 14 * s, pos.y() - 14 * s, 90 * s, 28 * s).toAlignedRect()

    def _mark_region(self, s: float) -> QRegion:
        """Everything the marks can touch while they draw in, ping and fade: each one's shape with its
        glow, its label, and the rings that ripple out of a minimap circle."""
        w, h = self.width(), self.height()
        region = QRegion()
        pad = 12 * s
        for m in self.marks:
            x, y, x2, y2, unit = m.to_window(w, h, self.minimap_side)
            if m.shape in ("circle", "point"):
                r = unit * (2.7 if m.space == "minimap" else 1.0) + pad  # (the pings reach 2.6 radii)
                box = QRectF(x - r, y - r, 2 * r, 2 * r)
            else:
                box = QRectF(QPointF(x, y), QPointF(x2, y2)).normalized().adjusted(-30 * s, -30 * s, 30 * s, 30 * s)
            region += box.toAlignedRect()
            if m.label:  # exactly where _label will put it
                anchor, beyond = self._label_anchor(m, w, h)
                region += self._label_box(m.label, anchor, w, h, s, beyond).adjusted(
                    -4 * s, -4 * s, 4 * s, 4 * s).toAlignedRect()
        return region.intersected(QRegion(self.rect()))

    def _label_anchor(self, mark, w: int, h: int) -> tuple[QPointF, tuple[float, float] | None]:
        """Where a mark's label hangs: up and to the right of a circle or a box; past an arrow's tip, in the
        arrow's own direction (so it never covers the head)."""
        x, y, x2, y2, unit = mark.to_window(w, h, self.minimap_side)
        if mark.shape in ("circle", "point"):
            r = unit if mark.shape == "circle" else unit * 0.35
            return QPointF(x + r * 0.75, y - r * 0.75), None
        if mark.shape == "arrow":
            dx, dy = x2 - x, y2 - y
            length = math.hypot(dx, dy) or 1.0
            return QPointF(x2, y2), (dx / length, dy / length)
        return QRectF(QPointF(x, y), QPointF(x2, y2)).normalized().topRight(), None

    @staticmethod
    def _label_box(text: str, anchor: QPointF, w: int, h: int, s: float,
                   beyond: tuple[float, float] | None = None) -> QRectF:
        fm = QFontMetricsF(display_font(round(15 * s), QFont.Bold, 0.8 * s))
        pad = 8 * s
        box = QRectF(0, 0, fm.horizontalAdvance(text.upper()) + 2 * pad + 3 * s, fm.height() + pad * 0.8)
        if beyond is not None:
            ux, uy = beyond
            reach = abs(ux) * box.width() / 2 + abs(uy) * box.height() / 2 + 10 * s
            box.moveCenter(QPointF(anchor.x() + ux * reach, anchor.y() + uy * reach))
        else:
            box.moveBottomLeft(anchor + QPointF(4 * s, -4 * s))
        box.moveLeft(min(max(4.0, box.left()), w - box.width() - 4))  # keep it on screen
        box.moveTop(min(max(4.0, box.top()), h - box.height() - 4))
        return box

    def _frame(self) -> None:
        age = self._age()
        if age >= self.ttl:
            self.clear()
            return
        # Animate the draw-in and the fade-out; in between nothing moves, so stop repainting.
        if ANIMATE_S < age < self.ttl - FADE_S:
            self._tick.setInterval(max(16, int((self.ttl - FADE_S - age) * 1000)))
        else:
            self._tick.setInterval(16)
        # Only where something changes: the marks, and the pointer where it was and where it is now.
        s = self._scale()
        now = self._pointer_rect(self._pointer_at(age, s)[0], s)
        self.update(self._dirty + self._pointer_was + now)
        self._pointer_was = now

    # painting ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self.marks:
            return
        age = self._age()
        fade = 1.0 if age < self.ttl - FADE_S else max(0.0, (self.ttl - age) / FADE_S)
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setOpacity(fade)
        w, h = self.width(), self.height()
        s = self._scale()
        for i, mark in enumerate(self.marks):
            t = _ease_out((age - DRAW_GAP_S * i - DRAW_AFTER_S) / DRAW_IN_S)  # one by one
            if t > 0:
                self._paint_mark(p, mark, t, w, h, s, age)
        self._paint_pointer(p, age, w, h, s)
        p.end()

    def _pens(self, color: QColor, width: float, s: float) -> list[QPen]:
        """Glow underneath, then a dark outline, then the color: readable on grass, snow or lava."""
        glow = QColor(color)
        glow.setAlpha(70)
        pens = [QPen(glow, width + 9 * s), QPen(QColor(0, 0, 0, 210), width + 4 * s), QPen(color, width)]
        for pen in pens:
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
        return pens

    def _paint_mark(self, p: QPainter, mark, t: float, w: int, h: int, s: float, age: float) -> None:
        x, y, x2, y2, unit = mark.to_window(w, h, self.minimap_side)
        # Enemy icons on the minimap are red: Doty's own minimap marks are bright yellow so they stand
        # apart, unless the mark is about a hero (then its team color says who it is).
        color = MINIMAP_COLOR if mark.space == "minimap" and mark.tone == "coach" else TONE.get(mark.tone,
                                                                                               TONE["coach"])
        width = 5 * s
        a, b = QPointF(x, y), QPointF(x2, y2)
        beyond = None
        if mark.shape in ("circle", "point"):
            r = unit if mark.shape == "circle" else unit * 0.35
            rect = QRectF(x - r, y - r, 2 * r, 2 * r)
            for pen in self._pens(color, width, s):
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawArc(rect, 90 * 16, -int(360 * 16 * t))
            if mark.shape == "point":
                p.setPen(Qt.NoPen)
                p.setBrush(color)
                p.drawEllipse(QPointF(x, y), 4 * s * t, 4 * s * t)
            if mark.space == "minimap" and t >= 1.0:  # two rings ripple out: movement catches the eye
                for k in range(2):
                    ping = (age - DRAW_IN_S - DRAW_AFTER_S - 0.3 * k) / 1.0
                    if 0 < ping < 1:
                        ring = QColor(color)
                        ring.setAlpha(int(220 * (1 - ping)))
                        p.setPen(QPen(ring, 3 * s))
                        rr = r * (1 + 1.6 * ping)
                        p.drawEllipse(QPointF(x, y), rr, rr)
            anchor = QPointF(x + r * 0.75, y - r * 0.75)
        elif mark.shape == "arrow":
            tip = a + (b - a) * t
            self._arrow(p, a, tip, color, width, s, head=t > 0.6, pens=self._pens(color, width, s))
            # Its label goes just past the tip, in the arrow's own direction, so it never covers the head.
            dx, dy = b.x() - a.x(), b.y() - a.y()
            length = math.hypot(dx, dy) or 1.0
            anchor, beyond = b, (dx / length, dy / length)
        else:  # box
            rect = QRectF(a, b).normalized()
            grow = QRectF(rect.center().x() - rect.width() / 2 * t, rect.center().y() - rect.height() / 2 * t,
                          rect.width() * t, rect.height() * t)
            for pen in self._pens(color, width, s):
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(grow)
            anchor = rect.topRight()
        if mark.label and t > 0.5:
            p.save()
            p.setOpacity(p.opacity() * min(1.0, (t - 0.5) * 2))
            self._label(p, mark.label, anchor, color, w, h, s, beyond)
            p.restore()

    @staticmethod
    def _arrow(p: QPainter, a: QPointF, b: QPointF, color: QColor, width: float, s: float, head: bool,
               pens: list[QPen]) -> None:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        size = 24 * s
        shaft_end = QPointF(b.x() - ux * size * 0.7, b.y() - uy * size * 0.7) if head else b
        for pen in pens:
            p.setPen(pen)
            p.drawLine(a, shaft_end)
        if head:
            left = QPointF(b.x() - ux * size - uy * size * 0.55, b.y() - uy * size + ux * size * 0.55)
            right = QPointF(b.x() - ux * size + uy * size * 0.55, b.y() - uy * size - ux * size * 0.55)
            p.setPen(QPen(QColor(0, 0, 0, 210), 3 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(color)
            p.drawPolygon(QPolygonF([b, left, right]))

    @staticmethod
    def _label(p: QPainter, text: str, anchor: QPointF, color: QColor, w: int, h: int, s: float,
               beyond: tuple[float, float] | None = None) -> None:
        """A small panel like the app's: dark, a colored edge on the left, condensed capitals. `beyond`
        (an arrow's direction) puts it past `anchor` that way instead of up and to the right of it."""
        p.setFont(display_font(round(15 * s), QFont.Bold, 0.8 * s))
        box = MarkLayer._label_box(text, anchor, w, h, s, beyond)
        path = cut_path(box, 6 * s)
        p.fillPath(path, QColor(9, 12, 16, 230))
        p.setPen(QPen(color, 1.2 * s))
        p.setBrush(Qt.NoBrush)  # (an arrow's head leaves its fill set: the outline would flood the label with it)
        p.drawPath(path)
        p.fillRect(QRectF(box.left(), box.top(), 3 * s, box.height()), color)
        p.setPen(QColor(theme.TEXT))
        p.drawText(box.adjusted(3 * s, 0, 0, 0), Qt.AlignCenter, text.upper())

    def _paint_pointer(self, p: QPainter, age: float, w: int, h: int, s: float) -> None:
        """Doty's marker: a cyan diamond with a DOTY tag that slides in beside the first mark.
        Deliberately not cursor-shaped, so it's never mistaken for your mouse in a fight."""
        pos, t = self._pointer_at(age, s)
        d = 9 * s
        diamond = QPolygonF([pos + QPointF(0, -d), pos + QPointF(d, 0), pos + QPointF(0, d), pos + QPointF(-d, 0)])
        p.setPen(QPen(QColor(theme.INK), 1.6 * s))
        p.setBrush(TONE["coach"])
        p.drawPolygon(diamond)
        if t >= 1.0:
            f = display_font(round(12 * s), QFont.ExtraBold, 1.2 * s, italic=True)
            p.setFont(f)
            tag = QRectF(pos.x() + d * 1.3, pos.y() - 9 * s, 50 * s, 18 * s)
            p.fillPath(slant_path(tag, 5 * s), QColor(theme.BRAND))
            p.setPen(QColor(theme.INK))
            p.drawText(tag.adjusted(0, 0, -2 * s, 0), Qt.AlignCenter, "DOTY")
