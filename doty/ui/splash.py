"""The launch screen: Doty's mark and name on the broadcast backdrop (all drawn; the mark is assets/doty.svg)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPen,
                           QPixmap, QRadialGradient)
from PySide6.QtWidgets import QWidget

from . import theme
from .brand import display_font, paint_backdrop, paint_emblem

WIDTH, HEIGHT = 880, 460
MARK = 172  # the mark's size at full scale
GAP = 46  # between the mark and the name
TAGLINE = "Reads your game. Watches the map. Talks you through it."


def _fonts(s: float) -> tuple[QFont, QFont, QFont]:
    name = display_font(round(122 * s), QFont.ExtraBold, 122 * s * 0.075, italic=True)
    sub = display_font(round(21 * s), QFont.Bold, 5 * s)
    tag = QFont(theme.FONT)
    tag.setPixelSize(round(15 * s))
    return name, sub, tag


def mark_rect(w: float = WIDTH, h: float = HEIGHT) -> QRectF:
    """Where the mark sits: the mark and the words beside it are centered as one group."""
    s = h / HEIGHT
    name, sub, tag = _fonts(s)
    text_w = max(QFontMetricsF(name).horizontalAdvance("DOTY"), QFontMetricsF(sub).horizontalAdvance(
        "A COACH FOR DOTA 2") + 20 * s, QFontMetricsF(tag).horizontalAdvance(TAGLINE))
    group = MARK * s + GAP * s + text_w
    x = (w - group) / 2
    return QRectF(x, h / 2 - 16 * s - MARK * s / 2, MARK * s, MARK * s)


def paint_splash(p: QPainter, w: float = WIDTH, h: float = HEIGHT, tagline: bool = True,
                 status: str = "Starting Doty…") -> None:
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
    rect = QRectF(0, 0, w, h)
    p.fillRect(rect, QColor(theme.BG))
    paint_backdrop(p, rect)
    s = h / HEIGHT
    mark = mark_rect(w, h)
    # a soft cyan glow behind the mark, so it sits in light rather than on a flat panel
    glow = QRadialGradient(mark.center(), mark.width() * 1.05)
    glow.setColorAt(0.0, QColor(63, 212, 255, 46))
    glow.setColorAt(1.0, QColor(63, 212, 255, 0))
    p.fillRect(rect, glow)
    paint_emblem(p, mark)

    # The name beside the mark, its block centered on the mark (the mark carries the cyan slash).
    name, sub, tag = _fonts(s)
    x = mark.right() + GAP * s
    cap = QFontMetricsF(name).capHeight()
    block = cap + 22 * s + 3 * s + 16 * s + 26 * s + (34 * s if tagline else 0)
    top = mark.center().y() - block / 2
    base = top + cap
    p.setFont(name)
    p.setPen(QColor(theme.TEXT))
    p.drawText(QPointF(x - 6 * s, base), "DOTY")
    bar_y = base + 22 * s
    p.fillRect(QRectF(x, bar_y, 64 * s, 3 * s), QColor(theme.BRAND))
    p.setFont(sub)
    p.drawText(QRectF(x, bar_y + 16 * s, w - x - 30 * s, 26 * s), Qt.AlignLeft | Qt.AlignVCenter,
               "A COACH FOR DOTA 2")
    if tagline:
        p.setFont(tag)
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(QRectF(x, bar_y + 48 * s, w - x - 30 * s, 24 * s), Qt.AlignLeft | Qt.AlignVCenter, TAGLINE)

    # bottom: the status centered over a short loading rule, the version at the right
    if status:
        p.setFont(display_font(round(13 * s), QFont.Bold, 3 * s))
        p.setPen(QColor(theme.BRAND))
        p.drawText(QRectF(0, h - 76 * s, w, 22 * s), Qt.AlignHCenter | Qt.AlignVCenter, status.upper())
        track = QRectF(w / 2 - 90 * s, h - 48 * s, 180 * s, 2 * s)
        p.fillRect(track, QColor(63, 212, 255, 40))
        fill = QLinearGradient(track.topLeft(), track.topRight())
        fill.setColorAt(0.0, QColor(63, 212, 255, 0))
        fill.setColorAt(0.5, QColor(theme.BRAND))
        fill.setColorAt(1.0, QColor(63, 212, 255, 0))
        p.fillRect(QRectF(track.left() + track.width() * 0.2, track.top(), track.width() * 0.6, track.height()), fill)
    from .. import __version__
    from ..knowledge.timings import PATCH

    p.setFont(display_font(round(11 * s), QFont.Bold, 1.5 * s))
    p.setPen(QColor(theme.MUTED))
    p.drawText(QRectF(0, h - 52 * s, w - 58 * s, 18 * s), Qt.AlignRight | Qt.AlignVCenter,
               f"V{__version__}  ·  PATCH {PATCH}")
    # broadcast corner brackets
    p.setPen(QPen(QColor(theme.BRAND), 3 * s, Qt.SolidLine, Qt.SquareCap))
    k, n = 18 * s, 30 * s
    for (cx, cy, dx, dy) in ((k, k, 1, 1), (w - k, k, -1, 1), (k, h - k, 1, -1), (w - k, h - k, -1, -1)):
        p.drawLine(QPointF(cx, cy), QPointF(cx + dx * n, cy))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * n))


def render_splash() -> QPixmap:
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen else 1.0
    pm = QPixmap(int(WIDTH * dpr), int(HEIGHT * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.black)
    p = QPainter(pm)
    paint_splash(p)
    p.end()
    return pm


class Splash(QWidget):
    """A plain frameless window showing the launch art. Not QSplashScreen: that one waits up to a second
    for Windows to report the window as exposed before it lets the app go on, which made every launch
    about a second slower."""

    def __init__(self, pixmap: QPixmap) -> None:
        super().__init__(None, Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._pixmap = pixmap
        self.setFixedSize(WIDTH, HEIGHT)
        self.setAccessibleName("Doty is starting")
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pixmap)

    def finish(self, _window: QWidget) -> None:
        """Close once the main window has had its first paint (the next turn of the event loop)."""
        QTimer.singleShot(0, self.close)


def make_splash() -> Splash | None:
    return Splash(render_splash())
