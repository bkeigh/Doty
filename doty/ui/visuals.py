"""Pictures: hero portraits, item and ability icons, and cards that light up under the mouse.

Images load in the background (art.py). Until one arrives a portrait shows the hero's initials on
a dark plate, so nothing jumps around and the page works offline.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from . import theme
from .art import Art, art
from .widgets import label

TONES = {"good": theme.GREEN, "bad": theme.RED, "gold": theme.BRAND, "warn": theme.AMBER, "": None, None: None}


def _initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


class _Hover:
    """A 0..1 value that eases up while the mouse is over the widget and back down when it leaves."""

    def __init__(self, widget: QWidget, ms: int = 160) -> None:
        self.value = 0.0
        self._w = widget
        self._a = QVariantAnimation(widget)
        self._a.setDuration(ms)
        self._a.setEasingCurve(QEasingCurve.OutCubic)
        self._a.valueChanged.connect(self._set)

    def _set(self, v) -> None:
        self.value = float(v)
        self._w.update()

    def go(self, on: bool) -> None:
        self._a.stop()
        self._a.setStartValue(self.value)
        self._a.setEndValue(1.0 if on else 0.0)
        self._a.start()


HERO_MIME = "application/x-doty-hero"  # a hero's name, dragged from a portrait onto a draft slot
_drag_origin = None  # what a drag in progress started from (a draft slot, or None for a plain portrait)


def drag_origin():
    """The widget the current drag started from, so a drop target can swap or clear it."""
    return _drag_origin


def start_hero_drag(source: QWidget, name: str, origin=None) -> None:
    """Drag a hero (its name) from `source`, with a faded copy of the portrait under the cursor."""
    global _drag_origin
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDrag

    mime = QMimeData()
    mime.setData(HERO_MIME, name.encode("utf-8"))  # (no plain text: a text box mustn't take it as typing)
    drag = QDrag(source)
    drag.setMimeData(mime)
    pix = source.grab()
    if not pix.isNull():
        pix = pix.scaledToWidth(min(pix.width(), 110), Qt.SmoothTransformation)
        drag.setPixmap(pix)
        drag.setHotSpot(QPoint(pix.width() // 2, pix.height() // 2))
    _drag_origin = origin
    try:
        drag.exec(Qt.CopyAction | Qt.MoveAction)
    finally:
        _drag_origin = None


class Picture(QWidget):
    """A rounded picture from the art cache, with an optional badge, tone ring and hover lift. Give it a
    hero's name in `drag_text` and it can be dragged (onto a draft slot)."""

    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, w: int, h: int, radius: float = 2, hover: bool = False) -> None:
        super().__init__()
        self.setFixedSize(w, h)
        self._radius = radius
        self._key = ""
        self._img: QImage | None = None
        self._gray: QImage | None = None
        self._name = ""
        self._badge = ""
        self._badge_color = theme.TEXT
        self._tone: str | None = None
        self._dim = False
        self._empty = ""
        self.hover_caption = False  # fade the name in at the bottom while the mouse is over it
        self.caption = False  # always show the name at the bottom
        self.strike = False  # a red line through it (a banned hero)
        self._hoverable = hover  # the mouse drives the hover glow (a HeroTile drives its own portrait's)
        self._clickable = hover
        self.drag_text = ""  # a hero's name: this portrait can be dragged
        self.drag_origin = None  # passed to the drop target (a draft slot dragging its own hero)
        self._press: QPoint | None = None
        self._dragged = False
        self._hover = _Hover(self)
        if hover:
            self.setCursor(Qt.PointingHandCursor)
        art().arrived.connect(self._arrived)

    # content ---------------------------------------------------------------------------
    def _load(self, key: str, path: str, name: str) -> None:
        self._key, self._name, self._empty = key, name, ""
        self._img = art().image(key, path) if key else None
        self._gray = None
        self.update()

    def set_empty(self, text: str = "") -> None:
        self._key, self._img, self._gray, self._name, self._badge = "", None, None, "", ""
        self._empty = text or " "
        self._tone = None
        self.update()

    def set_badge(self, text: str, color: str = theme.TEXT) -> None:
        self._badge, self._badge_color = text, color
        self.update()

    def set_tone(self, tone: str | None) -> None:
        self._tone = TONES.get(tone, tone)
        self.update()

    def set_dim(self, on: bool) -> None:
        self._dim = on
        self.update()

    def _arrived(self, key: str) -> None:
        if key and key == self._key and self._img is None:
            self._img = art().image(key, "")
            self.update()

    # mouse -------------------------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        if self._hoverable:
            self._hover.go(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hoverable:
            self._hover.go(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press, self._dragged = event.position().toPoint(), False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtWidgets import QApplication

        if (self.drag_text and self._press is not None and event.buttons() & Qt.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance()):
            self._press, self._dragged = None, True
            start_hero_drag(self, self.drag_text, self.drag_origin)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._press = None
        if self._dragged:  # the end of a drag isn't a click
            self._dragged = False
            event.accept()
            return
        if self._clickable and event.button() == Qt.LeftButton:
            event.accept()  # handled here; don't let a parent tile count the same click again
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._clickable and event.button() == Qt.LeftButton:
            event.accept()
            self.double_clicked.emit()
            return
        super().mouseDoubleClickEvent(event)

    # paint -------------------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        h = self._hover.value
        clip = QPainterPath()
        clip.addRoundedRect(r, self._radius, self._radius)
        if self._empty:
            pen = QPen(QColor(theme.BORDER_STRONG if not h else theme.BRAND), 1.2, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(255, 255, 255, int(6 + 10 * h)))
            p.drawRoundedRect(r, self._radius, self._radius)
            if self._empty.strip():
                f = QFont(theme.FONT)
                f.setPointSizeF(9.5)
                p.setFont(f)
                p.setPen(QColor(theme.TEXT_2 if not h else theme.BRAND_BRIGHT))
                p.drawText(r, Qt.AlignCenter, self._empty)
            p.end()
            return
        p.save()
        p.setClipPath(clip)
        img = self._img
        if img is not None:
            if self._dim:
                if self._gray is None:
                    self._gray = img.convertToFormat(QImage.Format_Grayscale8)
                img = self._gray
            zoom = 1.0 + 0.06 * h  # a gentle push-in under the mouse
            iw, ih = img.width(), img.height()
            scale = max(r.width() / iw, r.height() / ih) * zoom
            tw, th = iw * scale, ih * scale
            p.drawImage(QRectF(r.center().x() - tw / 2, r.center().y() - th / 2, tw, th), img)
        else:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor("#262b33"))
            g.setColorAt(1, QColor("#14171c"))
            p.fillRect(r, g)
            f = QFont(theme.FONT)
            f.setPixelSize(max(9, int(r.height() * 0.32)))
            f.setWeight(QFont.Bold)
            p.setFont(f)
            p.setPen(QColor(theme.MUTED))
            p.drawText(r, Qt.AlignCenter, _initials(self._name))
        if self._dim:
            p.fillRect(r, QColor(8, 9, 11, 120))
        cap = 1.0 if self.caption else h if self.hover_caption else 0.0
        if cap > 0.01 and self._name:
            h_cap = cap
            shade = QLinearGradient(r.left(), r.bottom() - r.height() * 0.6, r.left(), r.bottom())
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, int(220 * h_cap)))
            p.fillRect(r, shade)
            f = QFont(theme.FONT)
            f.setPixelSize(max(10, int(r.height() * 0.19)))
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            c = QColor(theme.TEXT)
            c.setAlphaF(h_cap)
            p.setPen(c)
            name = p.fontMetrics().elidedText(self._name, Qt.ElideRight, int(r.width() - 8))
            p.drawText(r.adjusted(4, 0, -4, -3), Qt.AlignHCenter | Qt.AlignBottom, name)
        if self._badge and not self.caption:
            shade = QLinearGradient(r.left(), r.bottom() - r.height() * 0.55, r.left(), r.bottom())
            shade.setColorAt(0, QColor(0, 0, 0, 0))
            shade.setColorAt(1, QColor(0, 0, 0, 205))
            p.fillRect(r, shade)
        p.restore()
        if self._badge:
            f = QFont(theme.FONT)
            f.setPixelSize(max(10, int(r.height() * (0.17 if self.caption else 0.2))))
            f.setWeight(QFont.Bold)
            p.setFont(f)
            if self.caption:  # a small dark pill in the top corner, clear of the name
                tw = p.fontMetrics().horizontalAdvance(self._badge) + 10
                th = p.fontMetrics().height() + 2
                pill = QRectF(r.right() - tw - 3, r.top() + 3, tw, th)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 175))
                p.drawRoundedRect(pill, 3, 3)
                p.setPen(QColor(self._badge_color))
                p.drawText(pill, Qt.AlignCenter, self._badge)
            else:
                p.setPen(QColor(self._badge_color))
                p.drawText(r.adjusted(4, 2, -5, -2), Qt.AlignRight | Qt.AlignBottom, self._badge)
        if self.strike:
            p.setPen(QPen(QColor(theme.RED), 2.0))
            p.drawLine(r.topRight() + QPointF(-4, 4), r.bottomLeft() + QPointF(4, -4))
        ring = QColor(self._tone) if self._tone else QColor(0, 0, 0, 160)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(ring, 1.6 if self._tone else 1.0))
        p.drawRoundedRect(r, self._radius, self._radius)
        if h > 0.01:
            glow = QColor(theme.BRAND_BRIGHT)
            glow.setAlphaF(0.85 * h)
            p.setPen(QPen(glow, 1.8))
            p.drawRoundedRect(r, self._radius, self._radius)
        p.end()


class HeroPortrait(Picture):
    """A hero's portrait (Valve's 16:9 art)."""

    def __init__(self, w: int = 128, h: int = 72, hover: bool = False, radius: float = 2) -> None:
        super().__init__(w, h, radius, hover)
        self.hero_id: int | None = None

    def set_hero(self, stats, hero_id: int | None, name: str = "") -> None:
        self.hero_id = hero_id
        key, path = Art.hero_key(stats, hero_id)
        if not name and stats is not None and hero_id:
            name = stats.name(hero_id)
        self._load(key, path, name)
        self.setToolTip(name)


class ItemIcon(Picture):
    """An item's icon; the tooltip has its name and cost."""

    def __init__(self, w: int = 44, h: int = 32, hover: bool = False) -> None:
        super().__init__(w, h, 3, hover)

    def set_item(self, stats, key: str, name: str = "", tip: str = "") -> None:
        k, path = Art.item_key(stats, key)
        name = (stats.item_name(key) if stats is not None and key else "") or name or key
        self._load(k, path, name)
        cost = stats.item_cost(key) if stats is not None and key else 0
        self.setToolTip(tip or (f"{name} · {cost:,} gold" if cost else name))


class AbilityIcon(Picture):
    def __init__(self, size: int = 40) -> None:
        super().__init__(size, size, 4)

    def set_ability(self, stats, key: str, name: str = "") -> None:
        k, path = Art.ability_key(stats, key)
        self._load(k, path, name)


class HeroTile(QWidget):
    """A clickable portrait with the hero's name and a line under it."""

    clicked = Signal()

    def __init__(self, w: int = 128, h: int = 72) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.portrait = HeroPortrait(w, h, hover=True)
        self.portrait._hoverable = False  # the whole tile lights the portrait up
        self.portrait.clicked.connect(self.clicked)
        lay.addWidget(self.portrait)
        self.name = label("")
        self.name.setStyleSheet("font-weight: 600; font-size: 9.5pt;")
        self.name.setFixedWidth(w)
        lay.addWidget(self.name)
        self.sub = label("", "faint")
        self.sub.setStyleSheet("font-size: 8.5pt;")
        self.sub.setFixedWidth(w)
        lay.addWidget(self.sub)
        self._name_text = self._sub_text = ""
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set(self, stats, hero_id: int, sub: str = "", badge: str = "", badge_color: str = theme.TEXT,
            tip: str = "") -> None:
        self.portrait.set_hero(stats, hero_id)
        self.portrait.set_badge(badge, badge_color)
        self._name_text, self._sub_text = stats.name(hero_id), sub
        self._elide()
        self.sub.setVisible(bool(sub))
        self.setToolTip(tip or self._name_text)
        self.portrait.setToolTip(tip or self._name_text)

    def _elide(self) -> None:
        w = self.portrait.width()
        self.name.setText(self.name.fontMetrics().elidedText(self._name_text, Qt.ElideRight, w))
        self.sub.setText(self.sub.fontMetrics().elidedText(self._sub_text, Qt.ElideRight, w))

    def set_width(self, w: int) -> None:
        h = round(w * self.portrait.height() / max(1, self.portrait.width()))
        self.portrait.setFixedSize(w, h)
        self.name.setFixedWidth(w)
        self.sub.setFixedWidth(w)
        self._elide()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.portrait._hover.go(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.portrait._hover.go(False)
        super().leaveEvent(event)
