"""Small building blocks for the Doty window."""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLayout,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..advice import Priority
from . import theme

# What each tone (advice.tone) looks like in the window. The overlay has a brighter set for the game.
TONE_COLORS = {"coach": theme.BRAND, "quiet": theme.BRAND_DIM, "you": theme.NEUTRAL, "now": theme.RED,
               "soon": theme.AMBER, "info": theme.TEXT_2}
# The word a feed line is tagged with, by tone (rules lines use their category).
TONE_TAGS = {"coach": "DOTY", "quiet": "DOTY", "you": "YOU"}


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    if name in ("eyebrow", "gold", "section", "h1", "heroTitle", "display", "num"):
        f = lab.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.8 if name in ("eyebrow", "gold", "section") else 0.4)
        lab.setFont(f)
    return lab


def caps_font(pt: float, weight: int = 700, spacing: float = 1.2) -> QFont:
    """Condensed capitals for buttons, tags and tabs (the text keeps its case; it's drawn in capitals)."""
    f = QFont(theme.DISPLAY)
    f.setPointSizeF(pt)
    f.setWeight(QFont.Weight(weight))
    f.setCapitalization(QFont.AllUppercase)
    f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


def icon_label(glyph: str, size: int = 14, color: str = theme.TEXT_2) -> QLabel:
    lab = QLabel(glyph)
    lab._icon_size = size
    set_icon_color(lab, color)
    lab.setFixedWidth(int(size * 1.5))
    lab.setAlignment(Qt.AlignCenter)
    return lab


def set_icon_color(lab: QLabel, color: str, extra: str = "") -> None:
    # The app stylesheet sets font-family on every widget, so the icon font has to
    # be named in the label's own stylesheet; setFont() alone would be overridden.
    lab.setStyleSheet(f'font-family: "{theme.ICON_FONT}"; font-size: {lab._icon_size}px; color: {color};'
                      f" background: transparent; {extra}")


SOLID_KINDS = ("primary", "accent", "start")  # cyan, with a leaning right edge


class IconButton(QPushButton):
    """A button that fades as a whole when disabled (its child labels carry their own colors). The main
    kinds paint their own cyan shape: a stylesheet can't lean an edge."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setAttribute(Qt.WA_Hover)  # repaint on hover for the painted kinds

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            if hasattr(self, "_text"):
                _style_parts(self)  # cyan buttons swap to readable text when disabled
            if self.isEnabled():
                self.setGraphicsEffect(None)
            else:
                eff = QGraphicsOpacityEffect(self)
                eff.setOpacity(0.5)
                self.setGraphicsEffect(eff)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.objectName() not in SOLID_KINDS:
            super().paintEvent(event)
            return
        from .brand import slant_path

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            fill = QColor(theme.SURFACE_HOVER)
        elif self.isDown():
            fill = QColor("#22a9cf")
        elif self.underMouse():
            fill = QColor(theme.BRAND_BRIGHT)
        else:
            fill = QColor(theme.BRAND)
        p.fillPath(slant_path(QRectF(self.rect()), 8.0), fill)
        if not hasattr(self, "_text") and self.text():  # no icon: the button draws its own words
            p.setFont(caps_font(11.5, 800, 1.4))
            p.setPen(QColor(theme.INK if self.isEnabled() else theme.TEXT))
            p.drawText(QRectF(self.rect()).adjusted(0, 0, -8, 0), Qt.AlignCenter, self.text())
        if self.hasFocus():  # reached with Tab: a light outline inside the shape
            p.setPen(QPen(QColor(theme.INK), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(slant_path(QRectF(self.rect()).adjusted(3, 3, -4, -3), 6.0))
        p.end()


def button(text: str, kind: str = "ghost", glyph: str | None = None, tooltip: str = "") -> QPushButton:
    btn = IconButton(f"{text}")
    btn.setObjectName(kind)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFocusPolicy(Qt.TabFocus)  # the keyboard reaches it (and shows where it is); a click doesn't
    if glyph:
        btn.setProperty("glyph", glyph)
        _decorate(btn, glyph, text)
    elif kind in SOLID_KINDS:
        f = caps_font(11.5, 800, 1.4)
        btn.setMinimumSize(QFontMetrics(f).horizontalAdvance(text) + 44, 38)
    elif kind != "link":
        btn.setFont(caps_font(11, 700))
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def _big(kind: str) -> bool:
    return kind in ("primary", "danger", "start", "stop")


def _style_parts(btn: QPushButton) -> None:
    kind = btn.objectName()
    big = _big(kind)
    solid = kind in SOLID_KINDS
    color = {"danger": theme.BRAND_BRIGHT, "stop": "#ff9a8f"}.get(kind, theme.INK if solid else theme.TEXT)
    if solid and not btn.isEnabled():
        color = theme.TEXT  # dark ink on the grey disabled fill would vanish
    pt = 12.5 if big else 11.5 if solid else 11
    weight = 800 if solid or big else 700
    btn._text.setFont(caps_font(pt, weight, 1.4 if solid else 1.0))
    btn._text.setStyleSheet(f'background: transparent; font-family: "{theme.DISPLAY}"; font-weight: {weight};'
                            f" font-size: {pt}pt; color: {color};")
    icon = {"danger": theme.BRAND, "stop": "#ff9a8f"}.get(kind, theme.INK if solid else theme.TEXT_2)
    if solid and not btn.isEnabled():
        icon = theme.TEXT_2
    set_icon_color(btn._icon, icon)
    if kind == "icon":
        btn._text.hide()
        btn.setFixedSize(40, 40)
        return
    btn._text.setVisible(bool(btn._text.text()))
    # QPushButton's size hint ignores child layouts, so size it from the text ourselves.
    text_w = QFontMetrics(btn._text.font()).horizontalAdvance(btn._text.text())
    m = btn.layout().contentsMargins()
    btn.setMinimumWidth(m.left() + btn._icon.width() + btn.layout().spacing() + text_w + m.right() + 8 +
                        (8 if solid else 0))
    btn.setMinimumHeight(46 if big else 38)


def _decorate(btn: QPushButton, glyph: str, text: str) -> None:
    """Glyph + text inside one button (two fonts), via a child layout."""
    btn.setText("")
    lay = QHBoxLayout(btn)
    icon_only = btn.objectName() == "icon"
    lay.setContentsMargins(*((0, 0, 0, 0) if icon_only else (22 if _big(btn.objectName()) else 14, 0, 18, 0)))
    lay.setSpacing(8)
    btn._icon = icon_label(glyph, 14 if icon_only else 13)
    btn._icon.setAttribute(Qt.WA_TransparentForMouseEvents)
    btn._text = QLabel(text)
    btn._text.setAttribute(Qt.WA_TransparentForMouseEvents)
    btn.setAccessibleName(text)  # the words live in a child label, so name the button for screen readers
    if icon_only:
        lay.addStretch(1)
    lay.addWidget(btn._icon)
    lay.addWidget(btn._text)
    lay.addStretch(1)
    _style_parts(btn)


def set_button(btn: QPushButton, text: str, glyph: str | None = None, kind: str | None = None) -> None:
    if kind and kind != btn.objectName():
        btn.setObjectName(kind)
        btn.style().unpolish(btn)
        btn.style().polish(btn)
    if not hasattr(btn, "_text"):
        btn.setText(text)
        return
    btn._text.setText(text)
    btn.setAccessibleName(text)  # e.g. "Unmute" once muted
    if glyph:
        btn._icon.setText(glyph)
    _style_parts(btn)


class Card(QFrame):
    """A panel: dark, with its top-right corner cut and a thin top edge (colored with `set_accent`)."""

    def __init__(self, name: str = "card", margins: tuple[int, int, int, int] = (20, 18, 20, 18),
                 spacing: int = 10, accent: str | None = None) -> None:
        super().__init__()
        self.setObjectName(name)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(spacing)
        self.accent = accent

    def set_accent(self, color: str | None) -> None:
        if color != self.accent:
            self.accent = color
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        if self.objectName() != "card":
            super().paintEvent(event)
            return
        from .brand import paint_panel

        p = QPainter(self)
        paint_panel(p, QRectF(self.rect()), self.accent, theme.SURFACE, theme.CUT)
        p.end()


class ArtBanner(Card):
    """The status panel: dark steel with faint broadcast stripes and a bar down the left in the state's color."""

    def __init__(self, margins=(30, 20, 26, 20)) -> None:
        super().__init__("heroArt", margins=margins, spacing=0)
        self.bar = QColor(theme.GREY)

    def set_bar(self, color: str) -> None:
        if QColor(color) != self.bar:
            self.bar = QColor(color)
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import cut_path, paint_backdrop

        p = QPainter(self)
        r = QRectF(self.rect())
        paint_backdrop(p, r)
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipPath(cut_path(r, theme.CUT))
        p.fillRect(QRectF(r.left(), r.top(), 4, r.height()), self.bar)
        p.end()


class Tag(QWidget):
    """A broadcast tag: condensed capitals on a leaning bar (LIVE, RADIANT, NO TP, BUY NOW)."""

    def __init__(self, text: str = "", color: str = theme.TEXT_2, solid: bool = False, pt: float = 9.5) -> None:
        super().__init__()
        from ..i18n import tr

        text = tr(text)
        self._text, self._color, self._solid = text, QColor(color), solid
        self._font = caps_font(pt, 800, 1.2)
        self.setFixedHeight(int(QFontMetrics(self._font).height() + 5))
        self.setAccessibleName(text)
        self._resize()

    def set(self, text: str, color: str | None = None, solid: bool | None = None) -> None:
        from ..i18n import tr

        text = tr(text)
        self._text = text
        self.setAccessibleName(text)
        if color is not None:
            self._color = QColor(color)
        if solid is not None:
            self._solid = solid
        self._resize()
        self.setVisible(bool(text))
        self.update()

    def text(self) -> str:
        return self._text

    def _resize(self) -> None:
        self.setFixedWidth(QFontMetrics(self._font).horizontalAdvance(self._text) + 24)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import slant_path

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill = QColor(self._color)
        if not self._solid:
            fill.setAlphaF(0.17)
        p.fillPath(slant_path(QRectF(self.rect()), 6.0, left=True), fill)
        p.setFont(self._font)
        ink = QColor(theme.ink_on(self._color)) if self._solid else self._color.lighter(118)
        p.setPen(ink)
        p.drawText(QRectF(self.rect()).adjusted(1, 0, 0, 0), Qt.AlignCenter, self._text)
        p.end()


class Switch(QAbstractButton):
    """An animated on/off toggle."""

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 24)

    def _set_pos(self, v: float) -> None:
        self._pos = float(v)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 - jump without animating when set from code
        super().setChecked(on)
        self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        off, on = QColor("#283240"), QColor("#1d8fb3")  # cyan when on
        t = self._pos
        track = QColor(int(off.red() + (on.red() - off.red()) * t), int(off.green() + (on.green() - off.green()) * t),
                       int(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 44, 24), 12, 12)
        p.setBrush(QColor(theme.BRAND_BRIGHT if t > 0.5 else "#d5dbe2"))
        x = 3 + t * 20
        p.drawEllipse(QRectF(x, 3, 18, 18))
        p.end()


class StatusIcon(QWidget):
    """Doty's status at a glance, drawn the same in the top bar, on the overlay's badge and in the tray: an
    eye while it watches (green; amber when something needs a look), turning arrows while it waits (amber),
    still red arrows when something stopped, a grey eye when paused."""

    def __init__(self, size: int = 22) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self.icon, self.color, self.spin, self.angle = "eye", QColor(theme.GREY), False, 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._turn)

    def set_status(self, icon: str, color: str, spin: bool = False) -> None:
        self.icon, self.color, self.spin = icon, QColor(color), spin and icon == "sync"
        self._sync_timer()
        self.update()

    def _sync_timer(self) -> None:
        if self.spin and self.isVisible():  # (turns only while it can be seen: none in the tray)
            self._timer.start()
        else:
            self._timer.stop()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _turn(self) -> None:
        self.angle = (self.angle + 9.0) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import paint_eye, paint_sync

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(self.rect()).center()
        s = self.width() / 22
        if self.icon == "eye":
            paint_eye(p, c, self.width() * 0.38, self.color, s)
        else:
            paint_sync(p, c, self.width() * 0.3, self.color, s, self.angle)
        p.end()


class Dot(QWidget):
    """A status dot that can pulse (a soft ring breathing around it)."""

    def __init__(self, size: int = 12) -> None:
        super().__init__()
        self._size = size
        self.setFixedSize(size * 2 + 4, size * 2 + 4)
        self._color = QColor(theme.GREY)
        self._phase = 0.0
        self._want = False
        self._rings = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def set_state(self, color: str, pulse: bool) -> None:
        """A pulsing dot rings three times when its state changes, then holds still (constant motion at
        the edge of your eye is a distraction)."""
        changed = QColor(color) != self._color or pulse != self._want
        self._color, self._want = QColor(color), pulse
        if pulse and changed:
            self._rings, self._phase = 3, 0.0
            self._timer.start(33)
        elif not pulse:
            self._timer.stop()
            self._phase = 0.0
        self.update()

    def _tick(self) -> None:
        self._phase += 0.025
        if self._phase >= 1.0:
            self._phase = 0.0
            self._rings -= 1
            if self._rings <= 0:
                self._timer.stop()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self.rect().center()
        r = self._size / 2
        if self._timer.isActive():
            ring = QColor(self._color)
            ring.setAlphaF(0.45 * (1 - self._phase))
            p.setPen(Qt.NoPen)
            p.setBrush(ring)
            rr = r + self._phase * r * 1.1
            p.drawEllipse(QRectF(c.x() - rr + 0.5, c.y() - rr + 0.5, rr * 2, rr * 2))
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QRectF(c.x() - r + 0.5, c.y() - r + 0.5, r * 2, r * 2))
        p.end()


class Wordmark(QWidget):
    """Doty's name in the top bar: a cyan slash and DOTY in condensed italic capitals."""

    def __init__(self, px: int = 26) -> None:
        super().__init__()
        self._px = px
        self.setFixedSize(int(px * 3.6), 56)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import paint_wordmark

        p = QPainter(self)
        paint_wordmark(p, 0, self.height() / 2 + self._px * 0.36, self._px)
        p.end()


def fit_width(layout, width: int, max_width: int = 1560, side: int = 34) -> None:
    """Center a page's content once the window is wider than `max_width` (ultrawide screens)."""
    m = layout.contentsMargins()
    pad = max(side, (width - max_width) // 2)
    if m.left() != pad:
        layout.setContentsMargins(pad, m.top(), pad, m.bottom())


class FeedItem(QFrame):
    """One line in the feed: the game time, then the line with a colored tag in front (DOTY, YOU, or the
    alert's kind)."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("feeditem")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 4, 8)
        lay.setSpacing(10)
        self.time = QLabel("")
        self.time.setFixedWidth(36)
        self.time.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 600; font-size: 10pt;'
                                f" color: {theme.MUTED};")
        lay.addWidget(self.time, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.text = label("", wrap=True)
        self.text.setTextFormat(Qt.RichText)
        self.text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.text)
        self.meta = label("", "faint")
        self.meta.setStyleSheet("font-size: 8.5pt;")
        self.meta.hide()
        col.addWidget(self.meta)
        lay.addLayout(col, 1)
        # On coach lines: save what the coach saw, to report a wrong tip. Shown on hover (and to the keyboard).
        self.wrong = button("Wrong tip?", "link")
        self.wrong.setToolTip("Saves this line's screenshot, game state and the coach's full answer to "
                              "Documents\\Doty\\reports, to attach to a bug report")
        self.wrong.setStyleSheet("font-size: 8.5pt;")
        self.wrong.setVisible(False)
        lay.addWidget(self.wrong, 0, Qt.AlignTop)
        self.reportable = False
        self.entry_id = 0
        self.tag = ""
        self.plain = ""  # the line itself, without the tag's markup
        self._flash = 0.0
        self._color = QColor(theme.GREY)
        self._flash_anim = QVariantAnimation(self)
        self._flash_anim.setDuration(900)
        self._flash_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._flash_anim.valueChanged.connect(self._set_flash)

    def _set_flash(self, v) -> None:
        self._flash = float(v)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.wrong.setVisible(self.reportable)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        if not (self.wrong.hasFocus() or self.hasFocus()):
            self.wrong.setVisible(False)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)  # tabbing onto a coach line shows its button, next in the tab order
        self.wrong.setVisible(self.reportable)

    def flash(self) -> None:
        """Urgent lines light up red for a moment, then settle."""
        self._flash_anim.stop()
        self._flash_anim.setStartValue(1.0)
        self._flash_anim.setEndValue(0.0)
        self._flash_anim.start()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._flash > 0.01:
            p = QPainter(self)
            c = QColor(self._color)
            c.setAlphaF(0.22 * self._flash)
            p.fillRect(self.rect(), c)
            p.end()

    def show_entry(self, e: dict, details: bool = False) -> None:
        from ..advice import tone

        kind = tone(e["source"], e["category"], int(e["priority"]))
        quiet = kind == "quiet"
        color = TONE_COLORS[kind]
        self.entry_id = int(e.get("id") or 0)
        self.reportable = e["source"] == "coach" and bool(e["final"])
        self.wrong.setFocusPolicy(Qt.TabFocus if self.reportable else Qt.NoFocus)
        self.setFocusPolicy(Qt.TabFocus if self.reportable else Qt.NoFocus)
        try:  # the same tag the overlay's card has (GOLD, LEVEL, TIMER...), not the rule's internal category
            from ..i18n import tr
            from ..overlay import card_tag

            self.tag = tr(card_tag(e["source"], e["category"], int(e["priority"])))[:11]
        except Exception:
            self.tag = TONE_TAGS.get(kind, e["category"].upper())[:11]
        self._color = QColor(color)
        self.time.setText(e["clock"])
        self.plain = e["text"] + ("" if e["final"] else " …")
        self.setAccessibleName(f"{self.tag} {e['clock']}: {self.plain}")
        body = self.plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        dim = quiet or not e["final"]
        self.text.setText(f'<span style="font-family:\'{theme.DISPLAY}\'; font-weight:800; font-size:9.5pt;'
                          f' color:{color}">{self.tag}</span>&nbsp;&nbsp;'
                          f'<span style="color:{theme.TEXT_2 if dim else theme.TEXT}">{body}</span>')
        self.text.setStyleSheet("font-size: 10.5pt;")
        ms = e.get("latency_ms")
        self.meta.setText(f"{ms:.0f} ms" if details and ms is not None else "")
        self.meta.setVisible(bool(self.meta.text()))


class Feed(QScrollArea):
    """Everything Doty said, newest first; streaming entries update in place."""

    MAX_ITEMS = 200
    report = Signal(int, str)  # "Wrong tip?" on a coach line: (advice id, the line)

    def __init__(self, empty_text: str) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(0, 0, 8, 0)
        self.list.setSpacing(0)
        self.empty = label(empty_text, "faint", wrap=True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(120)
        self.list.addWidget(self.empty)
        self.list.addStretch(1)
        self.setWidget(inner)
        self.items: dict[int, FeedItem] = {}
        self.details = False  # show timings (Settings > Advanced > Show technical details)

    def upsert(self, entry: dict) -> None:
        item = self.items.get(entry["id"])
        if item is None:
            self.empty.hide()
            item = FeedItem()
            item.wrong.clicked.connect(lambda _c=False, it=item: self.report.emit(it.entry_id, it.plain))
            self.items[entry["id"]] = item
            self.list.insertWidget(1, item)
            self._fade_in(item)
            if entry.get("priority") == Priority.CRITICAL:
                item.flash()
            while len(self.items) > self.MAX_ITEMS:
                oldest = min(self.items)
                old = self.items.pop(oldest)
                self.list.removeWidget(old)
                old.deleteLater()
        item.show_entry(entry, self.details)

    def clear(self) -> None:
        for item in self.items.values():
            self.list.removeWidget(item)
            item.deleteLater()
        self.items.clear()
        self.empty.show()

    @staticmethod
    def _fade_in(item: QWidget) -> None:
        eff = QGraphicsOpacityEffect(item)
        item.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", item)
        anim.setDuration(260)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(lambda: item.setGraphicsEffect(None))
        anim.start()
        item._anim = anim


class SettingRow(QWidget):
    """Title + description on the left, a control on the right."""

    def __init__(self, title: str, description: str, control: QWidget) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(24)
        text = QVBoxLayout()
        text.setSpacing(2)
        t = label(title)
        t.setStyleSheet("font-size: 10.5pt; font-weight: 600;")
        text.addWidget(t)
        if description:
            d = label(description, "muted", wrap=True)
            d.setStyleSheet("font-size: 9.5pt;")
            text.addWidget(d)
        lay.addLayout(text, 1)
        lay.addWidget(control, 0, Qt.AlignVCenter | Qt.AlignRight)
        self.control = control


class Toast(QLabel):
    """A short message that fades in at the bottom of the window, then away."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setStyleSheet("font-weight: 600;")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # a message, never in the way of a click
        self.hide()
        self._eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._eff)
        self._anim = QPropertyAnimation(self._eff, b"opacity", self)
        self._anim.finished.connect(self._faded)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)

    def show_message(self, text: str, ms: int = 3200) -> None:
        was_visible = self.isVisible()
        par = self.parentWidget()
        self.setWordWrap(True)
        self.setMaximumWidth(max(240, min(720, par.width() - 80)))  # (a long message wraps, never off the window)
        self.setText(text)
        self.adjustSize()
        self.move((par.width() - self.width()) // 2, par.height() - self.height() - 28)
        self.raise_()
        self.show()
        self._anim.stop()
        self._anim.setDuration(180)
        self._anim.setStartValue(self._eff.opacity() if was_visible else 0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._anim.setDuration(400)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _faded(self) -> None:
        if self._eff.opacity() <= 0.01:
            self.hide()  # (gone, not an invisible box over the page)


class TabButton(QPushButton):
    """A page tab in the top bar: condensed capitals, with an optional amber dot (something to set up)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("navtab")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(caps_font(13.5, 700, 1.5))  # (bigger targets: the tabs are how you get around)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(f"{text} page")
        self.badge = False

    def set_badge(self, on: bool) -> None:
        if on != self.badge:
            self.badge = on
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        w = QFontMetrics(self.font()).horizontalAdvance(self.text()) + 32 + (12 if self.badge else 0)
        return QSize(w, 56)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.badge:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.AMBER))
            text_w = QFontMetrics(self.font()).horizontalAdvance(self.text())
            x = (self.width() + text_w) / 2 + 4
            p.drawEllipse(QRectF(x, self.height() / 2 - 6, 6, 6))
            p.end()



class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps to the next line, like words in a paragraph."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._spacing, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y()
