"""A small flag for each of Doty's languages, drawn here (no image files, and Windows has no flag emoji).

Each is the flag of the country the language's catalog is written for: Brazil for Portuguese, mainland
China for Simplified Chinese, the United States for English. They're simplified to read at 18 to 40 pixels
tall: Spain's has no coat of arms, the United States' has a few stars, Korea's trigrams are plain bars.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

W, H = 300.0, 200.0  # every flag is drawn on this canvas (3:2) and scaled
_cache: dict[tuple[str, int, float], QPixmap] = {}


def _star(cx: float, cy: float, r: float, turn: float = -90.0) -> QPolygonF:
    pts = []
    for i in range(10):
        a = math.radians(turn + i * 36)
        rr = r if i % 2 == 0 else r * 0.382
        pts.append(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return QPolygonF(pts)


def _bands(p: QPainter, colors: list[str], weights: list[float] | None = None, vertical: bool = False) -> None:
    weights = weights or [1.0] * len(colors)
    total, at = sum(weights), 0.0
    for color, w in zip(colors, weights):
        size = w / total * (W if vertical else H)
        p.fillRect(QRectF(at, 0, size + 1, H) if vertical else QRectF(0, at, W, size + 1), QColor(color))
        at += size


def _disc(p: QPainter, cx: float, cy: float, r: float, color: str) -> None:
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(cx, cy), r, r)


def _en(p: QPainter) -> None:
    _bands(p, ["#B22234", "#FFFFFF"] * 6 + ["#B22234"])
    p.fillRect(QRectF(0, 0, 126, H * 7 / 13), QColor("#3C3B6E"))
    p.setBrush(QColor("#FFFFFF"))
    for row in range(3):
        for col in range(4):
            p.drawEllipse(QPointF(18 + col * 30, 20 + row * 34), 6.5, 6.5)


def _zh(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#DE2910"))
    p.setBrush(QColor("#FFDE00"))
    p.drawPolygon(_star(52, 52, 32))
    for x, y, turn in ((104, 20, -60), (126, 42, -35), (126, 74, -5), (104, 96, 20)):
        p.drawPolygon(_star(x, y, 11, turn))


def _pt(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#009B3A"))
    p.setBrush(QColor("#FEDF00"))
    p.drawPolygon(QPolygonF([QPointF(150, 18), QPointF(280, 100), QPointF(150, 182), QPointF(20, 100)]))
    _disc(p, 150, 100, 54, "#002776")
    clip = QPainterPath()
    clip.addEllipse(QPointF(150, 100), 54, 54)
    p.save()
    p.setClipPath(clip, Qt.IntersectClip)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#FFFFFF"), 9))
    p.drawArc(QRectF(60, 84, 220, 220), 40 * 16, 110 * 16)  # the white band across the globe
    p.restore()
    p.setPen(Qt.NoPen)


def _tr(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#E30A17"))
    _disc(p, 112, 100, 50, "#FFFFFF")
    _disc(p, 125, 100, 40, "#E30A17")
    p.setBrush(QColor("#FFFFFF"))
    p.drawPolygon(_star(168, 100, 25, -18))


def _ko(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#FFFFFF"))
    p.save()
    p.translate(150, 100)
    p.rotate(33.7)
    top, bottom = QPainterPath(), QPainterPath()
    top.moveTo(-50, 0)
    top.arcTo(QRectF(-50, -50, 100, 100), 180, -180)
    top.closeSubpath()
    bottom.moveTo(-50, 0)
    bottom.arcTo(QRectF(-50, -50, 100, 100), 180, 180)
    bottom.closeSubpath()
    p.fillPath(top, QColor("#CD2E3A"))
    p.fillPath(bottom, QColor("#0047A0"))
    _disc(p, -25, 0, 25, "#CD2E3A")
    _disc(p, 25, 0, 25, "#0047A0")
    p.restore()
    p.setBrush(QColor("#111111"))
    for cx, cy, angle in ((62, 42, -56.3), (238, 158, -56.3), (238, 42, 56.3), (62, 158, 56.3)):
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        for i in (-1, 0, 1):
            p.drawRect(QRectF(-24, i * 12 - 4, 48, 8))
        p.restore()


def _fil(p: QPainter) -> None:
    _bands(p, ["#0038A8", "#CE1126"])
    p.setBrush(QColor("#FFFFFF"))
    p.drawPolygon(QPolygonF([QPointF(0, 0), QPointF(173, 100), QPointF(0, 200)]))
    _disc(p, 58, 100, 20, "#FCD116")
    p.setBrush(QColor("#FCD116"))
    for x, y in ((16, 24), (16, 176), (138, 100)):
        p.drawPolygon(_star(x, y, 10))


def _vi(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#DA251D"))
    p.setBrush(QColor("#FFFF00"))
    p.drawPolygon(_star(150, 104, 62))


def _ja(p: QPainter) -> None:
    p.fillRect(QRectF(0, 0, W, H), QColor("#FFFFFF"))
    _disc(p, 150, 100, 60, "#BC002D")


_PAINTERS = {
    "en": _en,
    "ru": lambda p: _bands(p, ["#FFFFFF", "#0039A6", "#D52B1E"]),
    "zh": _zh,
    "es": lambda p: _bands(p, ["#AA151B", "#F1BF00", "#AA151B"], [1, 2, 1]),
    "pt": _pt,
    "id": lambda p: _bands(p, ["#CE1126", "#FFFFFF"]),
    "fr": lambda p: _bands(p, ["#0055A4", "#FFFFFF", "#EF4135"], vertical=True),
    "de": lambda p: _bands(p, ["#000000", "#DD0000", "#FFCE00"]),
    "pl": lambda p: _bands(p, ["#FFFFFF", "#DC143C"]),
    "uk": lambda p: _bands(p, ["#0057B7", "#FFD700"]),
    "tr": _tr,
    "ko": _ko,
    "ja": _ja,
    "th": lambda p: _bands(p, ["#A51931", "#F4F5F8", "#2D2A4A", "#F4F5F8", "#A51931"], [1, 1, 2, 1, 1]),
    "vi": _vi,
    "fil": _fil,
}


def has_flag(code: str) -> bool:
    return code in _PAINTERS


def flag_pixmap(code: str, height: int = 20, dpr: float | None = None) -> QPixmap:
    """The flag for a language, `height` pixels tall and half again as wide, with softly rounded corners
    and a hairline edge (so a white flag doesn't bleed into a light row, nor a dark one into the panel)."""
    if dpr is None:
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
    key = (code, height, dpr)
    if key in _cache:
        return _cache[key]
    w, h = round(height * 1.5), height
    pm = QPixmap(round(w * dpr), round(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    shape = QPainterPath()
    radius = max(1.5, h * 0.11)
    shape.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(shape)
    p.save()
    p.scale(w / W, h / H)
    p.setPen(Qt.NoPen)
    painter = _PAINTERS.get(code)
    if painter is None:
        p.fillRect(QRectF(0, 0, W, H), QColor("#3a4350"))
    else:
        painter(p)
    p.restore()
    p.setClipping(False)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 60), 1))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)
    p.end()
    _cache[key] = pm
    return pm


def flag_icon(code: str, height: int = 16) -> QIcon:
    return QIcon(flag_pixmap(code, height))
