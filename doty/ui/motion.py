"""Motion for the Doty window: short, quiet animations that show what changed.

- Pages cross-fade and the new one rises a few pixels.
- The cyan bar under the tabs slides to the page you picked.
- Sections fold open and shut, numbers count to their new value, loading areas shimmer.

Everything here runs for under half a second and never blocks a click.
"""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import theme

FAST, NORMAL = 160, 240
QWIDGETSIZE_MAX = 16777215


def _anim(target, prop: bytes, start, end, ms: int = NORMAL, curve=QEasingCurve.OutCubic) -> QPropertyAnimation:
    a = QPropertyAnimation(target, prop, target)
    a.setDuration(ms)
    a.setStartValue(start)
    a.setEndValue(end)
    a.setEasingCurve(curve)
    a.start(QPropertyAnimation.DeleteWhenStopped)
    return a


class FadeStack(QStackedWidget):
    """A stack of pages that cross-fades when you switch, with the new page rising into place."""

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        old = self.currentWidget()
        if index == self.currentIndex() or old is None or not self.isVisible():
            super().setCurrentIndex(index)
            return
        shot = old.grab()
        super().setCurrentIndex(index)
        cover = QLabel(self)
        cover.setAttribute(Qt.WA_TransparentForMouseEvents)
        cover.setPixmap(shot)
        cover.setGeometry(old.geometry())
        cover.show()
        cover.raise_()
        eff = QGraphicsOpacityEffect(cover)
        cover.setGraphicsEffect(eff)
        fade = _anim(eff, b"opacity", 1.0, 0.0, FAST)
        fade.finished.connect(cover.deleteLater)
        new = self.currentWidget()
        home = new.pos()
        rise = _anim(new, b"pos", home + QPoint(0, 10), home, NORMAL)
        rise.finished.connect(lambda: new.move(home) if new.parent() is self else None)


class NavIndicator(QFrame):
    """The cyan bar under the selected tab, which slides to the page you pick."""

    def __init__(self, parent: QWidget, thickness: int = 3) -> None:
        super().__init__(parent)
        self.setObjectName("navIndicator")
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.thickness = thickness
        self._anim: QPropertyAnimation | None = None

    def move_to(self, button: QWidget, animate: bool = True) -> None:
        at = button.mapTo(self.parentWidget(), QPoint(0, 0))
        r = QRect(at.x() + 6, at.y() + button.height() - self.thickness, max(8, button.width() - 12), self.thickness)
        self.raise_()
        if self._anim is not None:
            self._anim.stop()
        if not animate or not self.isVisible() or self.geometry().isEmpty():
            self.setGeometry(r)
            self.show()
            return
        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(NORMAL)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(r)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()


class _ClickRow(QWidget):
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class Collapsible(QWidget):
    """A header you click to fold a section open or shut."""

    toggled = Signal(bool)

    def __init__(self, title: str, subtitle: str = "", expanded: bool = False) -> None:
        super().__init__()
        from .widgets import icon_label, label

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = _ClickRow()
        row = QHBoxLayout(self.header)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(10)
        self.chevron = icon_label(theme.Glyph.CHEVRON_RIGHT, 11, theme.BRAND)
        self.chevron.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(self.chevron, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.title = label(title.upper(), "section")
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        texts.addWidget(self.title)
        self.subtitle = label(subtitle, "faint", wrap=True)
        self.subtitle.setAttribute(Qt.WA_TransparentForMouseEvents)
        if not subtitle:
            self.subtitle.hide()  # (never show() a widget before it has a parent: it flashes up as a window)
        texts.addWidget(self.subtitle)
        row.addLayout(texts, 1)
        outer.addWidget(self.header)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 10, 0, 0)
        self.body_lay.setSpacing(10)
        outer.addWidget(self.body)
        self._open = expanded
        self.body.setMaximumHeight(QWIDGETSIZE_MAX if expanded else 0)
        self.body.setVisible(expanded)
        self._set_chevron()
        self.header.clicked.connect(lambda: self.set_open(not self._open))
        self._anim: QPropertyAnimation | None = None

    @property
    def is_open(self) -> bool:
        return self._open

    def _set_chevron(self) -> None:
        self.chevron.setText(theme.Glyph.CHEVRON_DOWN if self._open else theme.Glyph.CHEVRON_RIGHT)

    def set_open(self, on: bool, animate: bool = True) -> None:
        if on == self._open:
            return
        self._open = on
        self._set_chevron()
        if self._anim is not None:
            self._anim.stop()
        self.body.setVisible(True)
        full = max(1, self.body.sizeHint().height())
        if not animate or not self.isVisible():
            self.body.setMaximumHeight(QWIDGETSIZE_MAX if on else 0)
            self.body.setVisible(on)
        else:
            self._anim = QPropertyAnimation(self.body, b"maximumHeight", self)
            self._anim.setDuration(NORMAL)
            self._anim.setStartValue(self.body.height() if not on else 0)
            self._anim.setEndValue(full if on else 0)
            self._anim.setEasingCurve(QEasingCurve.OutCubic)
            self._anim.finished.connect(self._settle)
            self._anim.start()
        self.toggled.emit(on)

    def _settle(self) -> None:
        if self._open:
            self.body.setMaximumHeight(QWIDGETSIZE_MAX)  # let the content grow afterwards
        else:
            self.body.setVisible(False)


class Shimmer(QWidget):
    """A placeholder that shimmers while something loads: `rows` bars, or one block."""

    def __init__(self, rows: int = 3, height: int = 14, block: tuple[int, int] | None = None) -> None:
        super().__init__()
        self._rows, self._h, self._block = rows, height, block
        self._t = 0.0
        if block:
            self.setFixedSize(*block)
        else:
            self.setFixedHeight(rows * (height + 10))
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._timer.start(33)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._t = (self._t + 0.018) % 1.4
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        g = QLinearGradient(w * (self._t - 0.4), 0, w * self._t, 0)
        base, hi = QColor("#151c25"), QColor("#223040")
        g.setColorAt(0, base)
        g.setColorAt(0.5, hi)
        g.setColorAt(1, base)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        if self._block:
            p.drawRect(QRectF(self.rect()))
        else:
            for i in range(self._rows):
                frac = (1.0, 0.82, 0.64, 0.9, 0.7)[i % 5]
                p.drawRect(QRectF(0, i * (self._h + 10), w * frac, self._h))
        p.end()


class CountLabel(QLabel):
    """A number that counts to its new value instead of jumping."""

    def __init__(self, fmt=lambda v: f"{v:,}", name: str | None = None, snap_below: float = 0) -> None:
        super().__init__("—")
        self._snap = snap_below  # changes smaller than this jump straight there (no endless counting)
        if name:
            self.setObjectName(name)
        self._fmt = fmt
        self._value: float | None = None
        self._target: float | None = None
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(420)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self._show(float(v)))

    def _show(self, v: float) -> None:
        self._value = v
        self.setText(self._fmt(int(round(v))))

    def set_value(self, v: int | None) -> None:
        if v is None:
            self._anim.stop()
            self._value = self._target = None
            self.setText("—")
            return
        if v == self._target:
            return
        self._target = v
        self._anim.stop()
        if self._value is None or abs(v - self._value) < max(1, self._snap):
            self._show(v)
            return
        self._anim.setStartValue(float(self._value))
        self._anim.setEndValue(float(v))
        self._anim.start()


def fade_in(w: QWidget, ms: int = NORMAL, rise: int = 6) -> None:
    """Fade a freshly added widget in (and let it rise a touch)."""
    eff = QGraphicsOpacityEffect(w)
    w.setGraphicsEffect(eff)
    a = _anim(eff, b"opacity", 0.0, 1.0, ms)
    a.finished.connect(lambda: w.setGraphicsEffect(None))
    if rise and w.parent() is not None and w.isVisible():
        home = w.pos()
        _anim(w, b"pos", home + QPoint(0, rise), home, ms)
