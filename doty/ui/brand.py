"""Doty's look, in one place: the mark (assets/doty.svg, the app icon: a dark tile with the panels' cut corner
in cyan, holding a leaning D whose stem is the wordmark's cyan slash), the wordmark (a cyan slash and DOTY in
condensed italic capitals), the fonts (Barlow and Barlow Condensed, SIL OFL) and the shapes every panel shares.

The style is a broadcast HUD: near-black panels with a cut top-right corner and a thin colored top edge,
condensed capitals for labels and numbers, cyan for Doty itself. No Valve logos or art.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
                           QRadialGradient)

ASSETS = Path(__file__).resolve().parent / "assets"
EMBLEM = ASSETS / "doty.svg"
FONT_FILES = sorted((ASSETS / "fonts").glob("Barlow*.ttf"))
DISPLAY = "Barlow Condensed"  # labels, numbers, titles and buttons; body text is Barlow
BRAND = "#3fd4ff"

_fonts_loaded = False
_renderer = None


def register_fonts() -> str:
    """Load the bundled fonts once. Returns the display family (Segoe UI if they failed)."""
    global _fonts_loaded, DISPLAY
    if not _fonts_loaded:
        _fonts_loaded = True
        families: set[str] = set()
        for f in FONT_FILES:
            if f.is_file():
                fid = QFontDatabase.addApplicationFont(str(f))
                families |= set(QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else [])
        DISPLAY = "Barlow Condensed" if "Barlow Condensed" in families else "Segoe UI"
    return DISPLAY


def display_font(px: int, weight: int = QFont.ExtraBold, spacing: float = 0.0, italic: bool = False) -> QFont:
    f = QFont(register_fonts())
    f.setPixelSize(max(1, px))
    f.setWeight(QFont.Weight(weight))
    f.setItalic(italic)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


def paint_emblem(p: QPainter, rect: QRectF) -> None:
    global _renderer
    if _renderer is None:
        from PySide6.QtSvg import QSvgRenderer

        _renderer = QSvgRenderer(str(EMBLEM))
    _renderer.render(p, rect)


def cut_path(rect: QRectF, cut: float = 12.0, bottom_left: float = 0.0) -> QPainterPath:
    """A rectangle with its top-right corner cut off (and optionally its bottom-left)."""
    r = rect
    pts = [QPointF(r.left(), r.top()), QPointF(r.right() - cut, r.top()), QPointF(r.right(), r.top() + cut),
           QPointF(r.right(), r.bottom())]
    if bottom_left:
        pts += [QPointF(r.left() + bottom_left, r.bottom()), QPointF(r.left(), r.bottom() - bottom_left)]
    else:
        pts.append(QPointF(r.left(), r.bottom()))
    path = QPainterPath()
    path.addPolygon(QPolygonF(pts))
    path.closeSubpath()
    return path


def slant_path(rect: QRectF, slant: float = 8.0, left: bool = False) -> QPainterPath:
    """A button or tag shape: the right edge leans (and the left too, for a parallelogram)."""
    r = rect
    a = slant if left else 0.0
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(r.left() + a, r.top()), QPointF(r.right(), r.top()),
                               QPointF(r.right() - slant, r.bottom()), QPointF(r.left(), r.bottom())]))
    path.closeSubpath()
    return path


def paint_panel(p: QPainter, rect: QRectF, accent: str | None = None, fill: str = "#0e1217",
                cut: float = 12.0) -> None:
    """A panel: dark fill, cut top-right corner, and a thin top edge that fades out (in `accent` when given,
    else a quiet steel)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    path = cut_path(rect, cut)
    p.fillPath(path, QColor(fill))
    p.setClipPath(path)
    edge = QLinearGradient(rect.topLeft(), QPointF(rect.left() + rect.width() * 0.85, rect.top()))
    c = QColor(accent or "#3a4656")
    edge.setColorAt(0.0, c)
    c2 = QColor(c)
    c2.setAlpha(0)
    edge.setColorAt(1.0, c2)
    p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 2.0 if accent else 1.0), edge)
    p.restore()


def paint_backdrop(p: QPainter, rect: QRectF, radius: float = 0.0) -> None:
    """Dark steel with faint diagonal broadcast stripes on the right and a cool vignette. Cheap enough to
    paint on resize. (`radius` is kept for callers; panels are square now.)"""
    p.save()
    clip = cut_path(rect, 12.0)
    p.setClipPath(clip)
    base = QLinearGradient(rect.topLeft(), rect.bottomRight())
    base.setColorAt(0.0, QColor("#111821"))
    base.setColorAt(1.0, QColor("#07090c"))
    p.fillRect(rect, base)
    glow = QRadialGradient(QPointF(rect.right() - rect.width() * 0.18, rect.top()), rect.height() * 1.6)
    glow.setColorAt(0.0, QColor(63, 212, 255, 34))
    glow.setColorAt(1.0, QColor(63, 212, 255, 0))
    p.fillRect(rect, glow)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(63, 212, 255, 16), 6.0))
    w, h = rect.width(), rect.height()
    x = rect.left() + w * 0.58
    while x < rect.right() + h:
        p.drawLine(QPointF(x, rect.bottom()), QPointF(x + h * 0.6, rect.top()))
        x += 22.0
    shade = QRadialGradient(rect.center(), max(w, h) * 0.8)
    shade.setColorAt(0.65, QColor(0, 0, 0, 0))
    shade.setColorAt(1.0, QColor(0, 0, 0, 110))
    p.fillRect(rect, shade)
    p.restore()


def hairline(p: QPainter, rect: QRectF, radius: float = 0.0, alpha: int = 70) -> None:
    """A thin cyan border along a panel's cut outline."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(63, 212, 255, alpha), 1.0))
    p.setBrush(Qt.NoBrush)
    p.drawPath(cut_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), 12.0))
    p.restore()


def paint_eye(p: QPainter, c: QPointF, r: float, color: QColor, s: float = 1.0) -> None:
    """Doty's "watching" icon: an eye. (Status everywhere: the overlay badge, the top bar, the tray.)"""
    eye = QPainterPath()
    eye.moveTo(c.x() - r, c.y())
    eye.quadTo(c.x(), c.y() - r * 1.15, c.x() + r, c.y())
    eye.quadTo(c.x(), c.y() + r * 1.15, c.x() - r, c.y())
    p.setPen(QPen(color, 1.8 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPath(eye)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(c, r * 0.36, r * 0.36)


def paint_sync(p: QPainter, c: QPointF, r: float, color: QColor, s: float = 1.0, angle: float = 0.0) -> None:
    """Doty's "waiting" icon: two arrows chasing each other round a circle (turned by `angle`)."""
    import math

    p.save()
    p.translate(c)
    p.rotate(angle)
    pen = QPen(color, 2.0 * s, Qt.SolidLine, Qt.FlatCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    box = QRectF(-r, -r, 2 * r, 2 * r)
    head = 3.4 * s
    for start in (20, 200):
        p.drawArc(box, start * 16, 130 * 16)
        a = math.radians(start + 130)
        tip = QPointF(r * math.cos(a), -r * math.sin(a))
        tangent = QPointF(-math.sin(a), -math.cos(a))  # the direction the arc travels at its end
        normal = QPointF(math.cos(a), -math.sin(a))
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPolygon(QPolygonF([tip + tangent * head * 1.3, tip + normal * head, tip - normal * head]))
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
    p.restore()


def paint_wordmark(p: QPainter, x: float, baseline: float, px: int, color: str = "#f2f4f7") -> float:
    """The cyan slash and DOTY. Returns the width drawn."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    h = px * 0.92
    w = px * 0.36
    top = baseline - h * 0.86
    slash = QPolygonF([QPointF(x + w * 0.4, top), QPointF(x + w, top), QPointF(x + w * 0.6, top + h),
                       QPointF(x, top + h)])
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(BRAND))
    p.drawPolygon(slash)
    f = display_font(px, QFont.ExtraBold, px * 0.075, italic=True)
    p.setFont(f)
    p.setPen(QColor(color))
    tx = x + w + px * 0.28
    p.drawText(QPointF(tx, baseline), "DOTY")
    from PySide6.QtGui import QFontMetricsF

    width = tx - x + QFontMetricsF(f).horizontalAdvance("DOTY")
    p.restore()
    return width
