"""The live part of the Coach page, laid out like a broadcast: what Doty sees of your screen (with Doty's
latest line captioned over it), your hero, the next timers and what to buy next."""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QBoxLayout, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..i18n import tr
from ..rules.base import mmss
from . import theme
from .motion import CountLabel
from .visuals import HeroPortrait, ItemIcon
from .widgets import Card, Tag, caps_font, label

FRAME_WIDTH = 960  # live-view frames are scaled to this width before display
FRAME_EVERY_S = 0.4
LINE_HOLD_MS = 20000  # how long Doty's latest line stays captioned over the picture
NO_DOTA_TEXT = "Dota 2 isn't open. Launch it and you'll see what Doty is watching here."
COVER_TEXT = "Doty is in front of Dota, so it can't see the game. Put Doty on your other screen to watch live."
DEMO_TEXT = "Demo match: there's no Dota window to show. Doty's lines are captioned here as it talks."


def _font(px: float, weight: int = 600, family: str | None = None) -> QFont:
    f = QFont(family or theme.FONT)
    f.setPixelSize(max(1, round(px)))
    f.setWeight(QFont.Weight(weight))
    return f


class ScreenView(QWidget):
    """A live picture of the Dota window, a couple of frames a second, framed like a broadcast feed: corner
    brackets, a LIVE tag, and Doty's latest line captioned along the bottom. Local only.

    It films the screen where Dota is, so it holds still while Doty itself covers Dota (it would only
    film Doty) and while nobody can see this window."""

    frame = Signal(QImage)

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(150)
        self._pixmap: QPixmap | None = None
        self._scaled: tuple[QSize, QPixmap] | None = None
        self.message = "Open Dota 2 and Doty shows what it's watching here."
        self.badge = ""  # LIVE, PAUSED, or nothing (no picture)
        self.note = ""  # why it's paused, shown in the corner
        self.covering = False  # Doty is on top of Dota: say so instead of filming itself
        self.demo = False  # a demo match: there's no Dota window, and that's expected
        self._blink = False
        # the caption: Doty's latest line, like a broadcast lower third
        self.line: dict | None = None
        self._lt = 0.0
        self._lt_anim = QVariantAnimation(self)
        self._lt_anim.setDuration(260)
        self._lt_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._lt_anim.valueChanged.connect(self._set_lt)
        self._lt_timer = QTimer(self)
        self._lt_timer.setSingleShot(True)
        self._lt_timer.timeout.connect(lambda: self._fade_line(0.0))
        self.setAccessibleName("What Doty sees")
        self._describe()
        self.frame.connect(self._on_frame)
        self._active = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="doty-liveview", daemon=True).start()

    # size: never taller than the picture (16:9 of the width), so a tall window gives the room to the feed
    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, w: int) -> int:  # noqa: N802
        return round(w * 9 / 16)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, 360)

    def _cap(self) -> None:
        self.setMaximumHeight(max(self.minimumHeight(), round(self.width() * 9 / 16)))  # never taller than 16:9

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._cap()

    def set_active(self, on: bool, covering: bool = False) -> None:
        """Grab frames only while someone can see them. `covering`: Doty's window is in front of Dota."""
        was = self._active.is_set()
        if on:
            self._active.set()
        else:
            self._active.clear()
        if covering != self.covering:
            self.covering = covering
            if covering and self._pixmap is None:
                self._placeholder(COVER_TEXT)
        if self._pixmap is not None and (was != on or covering):
            if on:
                self.note = ""
            else:  # keep the last picture, marked as not live
                self.badge = "PAUSED"
                self.note = "Doty is in front of Dota" if covering else ""
            self.setToolTip(COVER_TEXT if covering else "")
            self._describe()
            self.update()

    def stop(self) -> None:
        self._stop.set()
        self._active.set()  # wake the loop so it can exit

    def _placeholder(self, text: str) -> None:
        if self._pixmap is None and self.message == text:
            return
        self._pixmap, self._scaled = None, None
        self.message = text
        self.badge = ""
        self._describe()
        self.update()

    # the caption ---------------------------------------------------------------------
    def show_line(self, e: dict) -> None:
        """Doty's latest line (not its quiet "still watching" notes), captioned over the picture as it
        streams in; it fades a while after the last word."""
        if e.get("source") != "coach" or e.get("category") == "watching":
            return
        self.line = {"id": e["id"], "text": e["text"] + ("" if e.get("final", True) else " …"),
                     "clock": e.get("clock", "")}
        self._fade_line(1.0)
        self._lt_timer.start(LINE_HOLD_MS)
        self._describe()
        self.update()

    def _fade_line(self, to: float) -> None:
        running = self._lt_anim.state() == QVariantAnimation.Running
        if abs(self._lt - to) < 0.01 and not running:
            return
        if running and abs(float(self._lt_anim.endValue()) - to) < 0.01:
            return  # already heading there
        self._lt_anim.stop()
        self._lt_anim.setStartValue(self._lt)
        self._lt_anim.setEndValue(to)
        self._lt_anim.start()

    def _set_lt(self, v) -> None:
        self._lt = float(v)
        self.update()

    def _describe(self) -> None:
        """What screen readers hear: the picture's state, why it's paused, and Doty's latest line."""
        bits = [self.badge.title() if self.badge else self.message, self.note]
        if self.line:
            bits.append(f"Doty at {self.line['clock']}: {self.line['text']}")
        self.setAccessibleDescription(". ".join(b for b in bits if b))

    # worker thread ---------------------------------------------------------------
    def _loop(self) -> None:
        from ..capture import FrameGrabber

        grabber = FrameGrabber()
        try:
            while not self._stop.is_set():
                if not self._active.wait(0.5):
                    continue
                if self._stop.is_set():
                    break
                try:
                    got = grabber.grab()
                except Exception:
                    got = None
                if got is None:
                    img = QImage()
                else:
                    w, h, data = got
                    img = QImage(data, w, h, w * 4, QImage.Format_RGB32).scaledToWidth(
                        min(FRAME_WIDTH, w), Qt.SmoothTransformation)
                try:
                    self.frame.emit(img)
                except RuntimeError:
                    break  # the window is gone
                time.sleep(FRAME_EVERY_S if got else 1.0)
        finally:
            grabber.close()

    # GUI thread ---------------------------------------------------------------------
    def _on_frame(self, img: QImage) -> None:
        if not self._active.is_set():
            return  # a late frame after Doty stopped or got covered: keep what's showing
        if img.isNull():
            self._placeholder(DEMO_TEXT if self.demo else NO_DOTA_TEXT)
            return
        self._pixmap, self._scaled = QPixmap.fromImage(img), None
        self._blink = not self._blink
        if self.badge != "LIVE" or self.note:
            self.badge, self.note = "LIVE", ""
            self._describe()
        self.update()

    def _picture(self, r: QRectF) -> tuple[QRectF, QPixmap] | None:
        if self._pixmap is None:
            return None
        size = self._pixmap.size().scaled(r.size().toSize(), Qt.KeepAspectRatio)
        if self._scaled is None or self._scaled[0] != size:
            self._scaled = (size, self._pixmap.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        x = r.left() + (r.width() - size.width()) / 2
        y = r.top() + (r.height() - size.height()) / 2
        return QRectF(x, y, size.width(), size.height()), self._scaled[1]

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import slant_path

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        whole = QRectF(self.rect())
        pic = self._picture(whole)
        # the frame hugs the picture (dark bars beside it belong to the page); with no picture, the whole box
        r = pic[0] if pic is not None else whole
        p.fillRect(r, QColor("#040507"))
        if pic is not None:
            p.drawPixmap(pic[0].topLeft(), pic[1])
        else:
            p.setFont(_font(14, 500))
            p.setPen(QColor(theme.MUTED))
            p.drawText(r.adjusted(60, 0, -60, 0), Qt.AlignCenter | Qt.TextWordWrap, self.message)
        p.translate(r.topLeft())
        r = QRectF(0, 0, r.width(), r.height())

        # corner brackets
        p.setPen(QPen(QColor(theme.BRAND), 3, Qt.SolidLine, Qt.SquareCap))
        k, n = 10.0, min(24.0, r.height() / 6)
        for (x, y, dx, dy) in ((k, k, 1, 1), (r.width() - k, k, -1, 1), (k, r.height() - k, 1, -1),
                               (r.width() - k, r.height() - k, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * n, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * n))

        # tags, top left
        tag_font = caps_font(9.5, 800, 1.4)
        fm = QFontMetrics(tag_font)
        p.setFont(tag_font)
        x, y, th = 24.0, 20.0, fm.height() + 6.0
        tags = [("What Doty sees", QColor(6, 8, 11, 215), QColor(theme.TEXT_2))]
        if self.badge == "LIVE":
            tags.append(("Live", QColor(theme.LIVE if self._blink else "#bf2617"), QColor("#ffffff")))
        elif self.badge == "PAUSED":
            tags.append(("Paused", QColor(theme.GREY), QColor("#ffffff")))
        for i, (text, fill, ink) in enumerate(tags):
            w = fm.horizontalAdvance(text.upper()) + 24.0
            box = QRectF(x, y, w, th)
            p.fillPath(slant_path(box, 6.0, left=i > 0), fill)
            p.setPen(ink)
            p.drawText(box, Qt.AlignCenter, text)
            x += w + 4
        if self.note:
            f = _font(12, 600)
            p.setFont(f)
            w = QFontMetrics(f).horizontalAdvance(self.note) + 20.0
            box = QRectF(r.width() - 24 - w, y, w, th)
            p.fillRect(box, QColor(6, 8, 11, 215))
            p.setPen(QColor(theme.AMBER))
            p.drawText(box, Qt.AlignCenter, self.note)

        if self.line and self._lt > 0.01:
            self._paint_caption(p, r)
        p.end()

    def _paint_caption(self, p: QPainter, r: QRectF) -> None:
        from .brand import slant_path

        p.save()
        p.setOpacity(self._lt)
        left = 24.0
        block_w = 70.0
        width = min(r.width() - 2 * left, 820.0)
        text_w = width - block_w - 28
        f = _font(16 if r.width() > 700 else 14, 600)
        fm = QFontMetrics(f)
        text = self.line["text"]
        lines = 3 if r.height() > 260 else 2
        bound = fm.boundingRect(QRect(0, 0, int(text_w), 2000), Qt.TextWordWrap, text)
        max_h = fm.lineSpacing() * lines
        while bound.height() > max_h and " " in text:  # long lines: keep the latest words that fit
            text = text.split(" ", 1)[1]
            bound = fm.boundingRect(QRect(0, 0, int(text_w), 2000), Qt.TextWordWrap, "… " + text)
        if text != self.line["text"]:
            text = "… " + text
        h = max(48.0, bound.height() + 22.0)
        bottom = r.height() - 26.0
        block = QRectF(left, bottom - h, block_w, h)
        panel = QRectF(left + block_w - 10, bottom - h, width - block_w + 10, h)
        p.fillRect(panel.adjusted(0, 0, 0, 0), QColor(8, 11, 15, 238))
        p.fillRect(QRectF(panel.left(), panel.top(), panel.width(), 2), QColor(theme.BRAND))
        p.fillPath(slant_path(block, 10.0), QColor(theme.BRAND))
        p.setPen(QColor(theme.INK))
        name = caps_font(12.5, 800, 1.4)
        name.setItalic(True)
        p.setFont(name)
        p.drawText(QRectF(block.left(), block.top(), block_w - 10, h * 0.62), Qt.AlignHCenter | Qt.AlignBottom,
                   "Doty")
        p.setFont(caps_font(9, 700, 0.5))
        p.drawText(QRectF(block.left(), block.top() + h * 0.62, block_w - 10, h * 0.38),
                   Qt.AlignHCenter | Qt.AlignTop, self.line.get("clock", ""))
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        p.drawText(QRectF(panel.left() + 22, panel.top() + 11, text_w, h - 18), Qt.TextWordWrap | Qt.AlignLeft,
                   text)
        p.restore()


class Bar(QWidget):
    """An HP or mana bar with its numbers on it; the fill eases to each new value, and a hit leaves a pale
    trail that drains after a beat."""

    def __init__(self, color: str, height: int = 20, pct: bool = False) -> None:
        super().__init__()
        self.setFixedHeight(height)
        self._color = QColor(color)
        self._pct = pct
        self._frac = 0.0
        self._target = 0.0
        self._text = ""
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self._trail = 0.0  # where the bar was before a drop
        self._trail_anim = QVariantAnimation(self)
        self._trail_anim.setDuration(500)
        self._trail_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._trail_anim.valueChanged.connect(self._step_trail)

    def _step(self, v) -> None:
        self._frac = float(v)
        self.update()

    def _step_trail(self, v) -> None:
        self._trail = float(v)
        self.update()

    def set(self, frac: float, text: str, color: str | None = None) -> None:
        frac = max(0.0, min(1.0, frac))
        self._text = text
        self._target = frac
        if color:
            self._color = QColor(color)
        if abs(frac - self._frac) > 0.002 and self.isVisible():
            if frac < self._frac - 0.02:  # a hit: leave a pale trail that drains after a beat
                self._trail_anim.stop()
                start = max(self._trail, self._frac)
                self._trail = start
                self._trail_anim.setStartValue(start)
                self._trail_anim.setKeyValueAt(0.45, start)
                self._trail_anim.setEndValue(frac)
                self._trail_anim.start()
            self._anim.stop()
            self._anim.setStartValue(self._frac)
            self._anim.setEndValue(frac)
            self._anim.start()
        else:
            self._frac = frac
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        r = QRectF(self.rect())
        p.fillRect(r, QColor("#19202a"))
        if self._trail > self._frac + 0.005:
            p.fillRect(QRectF(r.x(), r.y(), r.width() * self._trail, r.height()), QColor(255, 128, 104, 150))
        if self._frac > 0:
            fill = QRectF(r.x(), r.y(), max(3.0, r.width() * self._frac), r.height())
            g = QLinearGradient(fill.topLeft(), fill.bottomLeft())
            g.setColorAt(0, self._color.lighter(118))
            g.setColorAt(1, self._color.darker(112))
            p.fillRect(fill, g)
        if self._text or self._pct:
            p.setRenderHint(QPainter.TextAntialiasing)
            f = _font(r.height() * 0.72, 800, theme.DISPLAY)
            p.setFont(f)
            inner = r.adjusted(10, 0, -10, 0)
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(inner.translated(0, 1), Qt.AlignLeft | Qt.AlignVCenter, self._text)
            p.setPen(QColor("white"))
            p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, self._text)
            if self._pct and self._text:
                p.setFont(_font(r.height() * 0.62, 700, theme.DISPLAY))
                p.setPen(QColor(theme.TEXT_2))
                p.drawText(inner, Qt.AlignRight | Qt.AlignVCenter, f"{round(self._target * 100)}%")
        p.end()


def _stat(caption: str, tip: str = "", counting: bool = True, snap: float = 0,
          color: str = theme.TEXT) -> tuple[QWidget, QLabel]:
    """A big condensed number under a small caption, like a broadcast stat."""
    w = QWidget()
    w.setToolTip(tip)
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    cap = QLabel(caption)
    cap.setFont(caps_font(9.5, 700, 1.8))
    cap.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 9.5pt; color: {theme.MUTED};')
    v.addWidget(cap)
    value = CountLabel(snap_below=snap) if counting else label("—")
    value.setObjectName("num")
    value.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 23pt; color: {color};')
    v.addWidget(value)
    v.addStretch(1)
    return w, value


class GamePanel(Card):
    """Your hero as Doty sees it, from the game's own data feed (about 10 updates a second): portrait,
    HP and mana, items, the enemy lineup and your numbers, with your team's color down the left. The
    all-set line (or what needs a look, with its fix) sits under the items."""

    open_hero = Signal(str)  # an enemy portrait was clicked

    def __init__(self, stats_service=None) -> None:
        super().__init__(margins=(22, 14, 18, 14), spacing=10)
        self.svc = stats_service
        self.team_color = QColor(theme.GREY)
        self.empty = label("Waiting for a match. Your hero, items and numbers show up here once you're in game.",
                           "faint", wrap=True)
        self.body.addWidget(self.empty)

        self.content = QWidget()
        cols = self.cols = QBoxLayout(QBoxLayout.LeftToRight, self.content)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(20)
        left = QHBoxLayout()
        left.setSpacing(16)
        self.portrait = HeroPortrait(160, 90, radius=0)
        left.addWidget(self.portrait, 0, Qt.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(7)
        self.name_row = QBoxLayout(QBoxLayout.LeftToRight)  # the enemy lineup moves under the name when narrow
        self.name_row.setSpacing(8)
        name = QHBoxLayout()
        name.setSpacing(8)
        self.hero = QLabel("")
        self.hero.setFont(caps_font(19, 800, 1.0))
        self.hero.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 19pt;')
        name.addWidget(self.hero, 0, Qt.AlignVCenter)
        self.level = Tag("", "#c8d0d8", solid=True, pt=9.5)
        name.addWidget(self.level, 0, Qt.AlignVCenter)
        name.addStretch(1)
        self.name_row.addLayout(name, 1)
        vs_row = QHBoxLayout()
        vs_row.setSpacing(5)
        self.vs_caption = QLabel("vs")
        self.vs_caption.setFont(caps_font(9.5, 700, 1.4))
        self.vs_caption.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 9.5pt; color: {theme.MUTED};')
        vs_row.addWidget(self.vs_caption, 0, Qt.AlignVCenter)
        self.enemy_pics = [HeroPortrait(44, 25, hover=True, radius=0) for _ in range(5)]
        for pic in self.enemy_pics:
            pic.clicked.connect(lambda p=pic: self.open_hero.emit(p.property("hero_name") or ""))
            vs_row.addWidget(pic, 0, Qt.AlignVCenter)
        vs_row.addStretch(1)
        self.name_row.addLayout(vs_row, 0)
        info.addLayout(self.name_row)
        self.vs = label("", "faint", wrap=True)
        self.vs.setStyleSheet("font-size: 8.5pt;")
        info.addWidget(self.vs)
        self.hp = Bar(theme.HP, 22, pct=True)
        self.mana = Bar(theme.MANA, 13)
        info.addWidget(self.hp)
        info.addWidget(self.mana)
        items_row = QHBoxLayout()
        items_row.setSpacing(5)
        self.item_icons = [ItemIcon(44, 32) for _ in range(6)]
        for icon in self.item_icons:
            items_row.addWidget(icon)
        items_row.addSpacing(6)
        self.neutral_icon = ItemIcon(32, 32)
        items_row.addWidget(self.neutral_icon)
        items_row.addSpacing(6)
        self.tp = Tag("", theme.RED, pt=9.5)
        self.tp.hide()
        items_row.addWidget(self.tp, 0, Qt.AlignVCenter)
        items_row.addStretch(1)
        info.addLayout(items_row)
        self.ready = label("", "muted")
        self.ready.setTextFormat(Qt.RichText)
        self.ready.setTextInteractionFlags(Qt.LinksAccessibleByMouse)  # a problem's fix is a link here
        self.ready.setStyleSheet("font-size: 9pt;")
        self.ready.setWordWrap(True)  # several problems wrap instead of pushing the panel wider
        self.ready.hide()
        info.addWidget(self.ready)
        left.addLayout(info, 1)
        cols.addLayout(left, 1)

        self.stats_box = QWidget()
        self.stats = QGridLayout(self.stats_box)
        self.stats.setContentsMargins(0, 0, 0, 0)
        self.stats.setHorizontalSpacing(18)
        self.stats.setVerticalSpacing(8)
        self.cells = [_stat("Gold", "Your gold", snap=150, color=theme.GOLD),  # passive gold snaps; a kill counts
                      _stat("K / D / A", "Your kills, deaths and assists", counting=False),
                      _stat("LH / DN", "Last hits (creeps you finished off) and denies (your own creeps)",
                            counting=False),
                      _stat("GPM", "Gold per minute: how fast you're getting richer", snap=60)]
        (_, self.gold), (_, self.kda), (_, self.cs), (_, self.gpm) = self.cells
        cols.addWidget(self.stats_box, 0)
        self.body.addWidget(self.content)
        self.body.addStretch(1)  # a taller panel keeps its content at the top
        self.content.hide()
        self.setMaximumHeight(330)  # takes spare room under the picture, within reason
        self._narrow: bool | None = None
        self._place_stats(False)

    def _place_stats(self, narrow: bool) -> None:
        for w, _v in self.cells:
            self.stats.removeWidget(w)
        for i, (w, _v) in enumerate(self.cells):
            self.stats.addWidget(w, 0 if narrow else i // 2, i if narrow else i % 2, Qt.AlignTop)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        w = self.width()
        narrow = w < 820  # the numbers go under your hero, four across
        if narrow != self._narrow:
            self._narrow = narrow
            self.cols.setDirection(QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight)
            self.cols.setSpacing(12 if narrow else 20)
            self._place_stats(narrow)
        # tight: the enemy lineup gets its own line; tighter still, the portrait steps aside for the items
        self.name_row.setDirection(QBoxLayout.TopToBottom if w < 700 else QBoxLayout.LeftToRight)
        self.portrait.setVisible(w >= 600)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.fillRect(QRectF(0, 0, 4, self.height()), self.team_color)
        p.end()

    def _stats(self):
        s = getattr(self.svc, "stats", None)
        return s if s is not None and s.loaded else None

    def set_ready(self, html: str = "", tip: str = "") -> None:
        """The quiet all-set line, or what needs a look with its fix as a link."""
        self.ready.setText(html)
        self.ready.setToolTip(tip)
        self.ready.setVisible(bool(html))

    def set(self, g: dict | None) -> None:
        if g is None:
            self.content.hide()
            self.empty.show()
            self.team_color = QColor(theme.GREY)
            self.update()
            return
        self.empty.hide()
        self.content.show()
        s = self._stats()
        team = (g.get("team") or "").lower()
        color = QColor({"radiant": theme.GREEN, "dire": theme.RED}.get(team, theme.GREY))
        if color != self.team_color:
            self.team_color = color
            self.update()
        self.hero.setText(g["hero"])
        self.level.set(tr("Lvl {n}", n=g["level"]))
        hid = s.find(g["hero"]) if s is not None else None
        if self.portrait.hero_id != hid or hid is None:
            self.portrait.set_hero(s, hid, g["hero"])

        enemies = g.get("enemies") or []
        for i, pic in enumerate(self.enemy_pics):
            if i < len(enemies):
                pic.show()
                eid = s.find(enemies[i]) if s is not None else None
                if pic.hero_id != eid or eid is None:
                    pic.set_hero(s, eid, enemies[i])
                    pic.setProperty("hero_name", enemies[i])
                    pic.setToolTip(tr("{hero}: click for its spells and what to buy against it", hero=enemies[i]))
            else:
                pic.hide()
        self.vs_caption.setVisible(bool(enemies))
        self.vs.setText("" if enemies else "Enemy lineup: the coach reads it from the top bar on its next look")
        self.vs.setVisible(not enemies)

        if g["alive"]:
            frac = g["hp"] / g["max_hp"] if g["max_hp"] else 0
            color = theme.HP if frac > 0.5 else (theme.AMBER if frac > 0.25 else theme.RED)
            self.hp.set(frac, f"{g['hp']:,} / {g['max_hp']:,}", color)
            mana, max_mana = g.get("mana"), g.get("max_mana")
            if max_mana:
                self.mana.set(mana / max_mana, f"{mana:,} / {max_mana:,}")
            else:
                self.mana.set(g["mana_pct"], "")
        else:
            self.hp.set(0, f"Dead · back in {int(g['respawn_s'])} s", theme.RED)
            self.mana.set(0, "")

        keys = g.get("item_keys") or []
        names = g.get("items") or []
        for i, icon in enumerate(self.item_icons):
            if i < len(keys):
                icon.set_item(s, keys[i], names[i] if i < len(names) else "")
            else:
                icon.set_empty()
        if g.get("neutral_key"):
            self.neutral_icon.set_item(s, g["neutral_key"], g.get("neutral", ""))
        else:
            self.neutral_icon.set_empty()
        no_tp = not g["has_tp"] and g["clock"] > 0
        self.tp.set("No TP" if no_tp else "")
        self.tp.setToolTip("No Town Portal Scroll: buy one so you can join fights and save towers." if no_tp else "")

        self.gold.set_value(g["gold"])
        k, d, a = g["kda"]
        self.kda.setText(f"{k}/{d}/{a}")
        self.cs.setText(f"{g['last_hits']}/{g['denies']}")
        self.gpm.set_value(g["gpm"])


class _Countdown(QWidget):
    """A thin bar that drains as a timer runs out (for the next minute)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(64, 4)
        self.frac = 0.0

    def set(self, frac: float) -> None:
        self.frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1f2833"))
        p.fillRect(QRectF(0, 0, self.width() * self.frac, self.height()), QColor(theme.AMBER))
        p.end()


class TimersPanel(Card):
    """What's coming up (runes, day and night, Tormentor): the next one in amber with a draining bar once
    it's under a minute away."""

    ROWS = 4

    def __init__(self) -> None:
        super().__init__(margins=(16, 12, 16, 12), spacing=2, accent=theme.AMBER)
        self.body.addWidget(label("NEXT UP", "section"))
        self.rows: list[tuple[QWidget, QLabel, _Countdown, QLabel]] = []
        for i in range(self.ROWS):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 3, 0, 3)
            h.setSpacing(10)
            name = label("")
            bar = _Countdown()
            when = QLabel("")
            when.setObjectName("num")
            when.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            when.setMinimumWidth(44)
            h.addWidget(name, 1)
            h.addWidget(bar, 0, Qt.AlignVCenter)
            h.addWidget(when)
            if i:
                line = QWidget()
                line.setFixedHeight(1)
                line.setStyleSheet(f"background: {theme.BORDER};")
                self.body.addWidget(line)
                row._line = line
            self.body.addWidget(row)
            self.rows.append((row, name, bar, when))
        self.empty = label("Nothing coming up.", "faint")
        self.body.addWidget(self.empty)

    def set(self, timers: list[dict]) -> None:
        for i, (row, name, bar, when) in enumerate(self.rows):
            show = i < len(timers)
            row.setVisible(show)
            if hasattr(row, "_line"):
                row._line.setVisible(show)
            if not show:
                continue
            t = timers[i]
            soon = t["in_s"] <= 30
            name.setText(t["label"])
            name.setStyleSheet(f"font-size: 10.5pt; color: {theme.AMBER if soon else theme.TEXT};"
                               f" font-weight: {700 if soon else 500};")
            when.setText(mmss(t["in_s"]))
            when.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 15pt;'
                               f" color: {theme.AMBER if soon else '#c8d0d8'};")
            bar.setVisible(i == 0 and t["in_s"] <= 60)
            bar.set(t["in_s"] / 60)
        self.empty.setVisible(not timers)


class _ItemLine(QWidget):
    """One item to buy: icon and name, a gold bar filling toward what's left of its cost, and what's still to
    pay (or BUY NOW). Only the item being built now takes your gold; the ones after it show their price."""

    def __init__(self, stats, key: str, name: str = "") -> None:
        super().__init__()
        self.key = key
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        icon = ItemIcon(40, 29)
        icon.set_item(stats, key, name)
        h.addWidget(icon)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        self.name = label(name or (stats.item_name(key) if stats is not None else key))
        self.name.setStyleSheet("font-weight: 600; font-size: 10.5pt;")
        texts.addWidget(self.name)
        self.part = label("", "faint")  # "Buy Ogre Axe now": the part of it your gold covers
        self.part.setStyleSheet(f"font-size: 9.5pt; font-weight: 600; color: {theme.GREEN};")
        self.part.hide()
        texts.addWidget(self.part)
        self.bar = _Progress()
        texts.addWidget(self.bar)
        h.addLayout(texts, 1)
        self.togo = QLabel("")
        self.togo.setObjectName("num")
        h.addWidget(self.togo, 0, Qt.AlignVCenter)
        self.ready = Tag("Buy now", theme.GREEN, solid=True, pt=9.5)
        self.ready.hide()
        h.addWidget(self.ready, 0, Qt.AlignVCenter)
        self.setToolTip(icon.toolTip())

    def set_entry(self, e: dict, first: bool) -> None:
        """`e` is one entry of the engine's shopping list (Knowledge.shopping): the first is the item being
        built now, with what your gold covers; the rest come after it."""
        cost, left = int(e.get("cost") or 0), int(e.get("left") or 0)
        short = int(e.get("short") or 0) if first else left
        can = first and bool(e.get("finish"))
        part = e.get("buy_name") if first and e.get("buy") and not can else ""
        self.ready.setVisible(can)
        self.part.setVisible(bool(part))
        if part:
            self.part.setText(tr("Buy {part} now", part=part))
        self.togo.setVisible(not can)
        self.bar.setVisible(first and not can and cost > 0)
        if can:
            return
        if first and cost > 0:
            self.bar.set(1.0 - short / cost)  # (the parts you hold and the gold in your pocket both count)
        self.togo.setText(f"−{short:,}" if first else f"{left:,}")
        near = first and short <= 500
        color = theme.GOLD if near else "#c8d0d8" if first else theme.MUTED
        self.togo.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 13pt; color: {color};')
        self.togo.setToolTip(tr("{left} gold to go · it costs {cost}", left=f"{short:,}", cost=f"{cost:,}"))


class _Progress(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(3)
        self.frac = 0.0

    def set(self, frac: float) -> None:
        self.frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1f2833"))
        p.fillRect(QRectF(0, 0, self.width() * self.frac, self.height()), QColor(theme.GOLD))
        p.end()


class BuildPanel(Card):
    """What to buy next, as the engine decided it (Knowledge.shopping: the same answer the overlay's strip,
    the spoken "Buy ..." calls and the coach get): the item you're building now with a gold bar toward what's
    left of it, what comes after it, and the items that answer their lineup."""

    def __init__(self, stats_service=None) -> None:
        super().__init__(margins=(16, 12, 16, 14), spacing=9, accent=theme.GOLD)
        self.svc = stats_service
        self.body.addWidget(label("BUY NEXT", "section"))
        self.next_lay = QVBoxLayout()
        self.next_lay.setSpacing(9)
        self.next_lay.addWidget(label("Loading item data…", "faint"))
        self.body.addLayout(self.next_lay)
        self.vs_row = QWidget()  # the answers to their lineup, under a hairline
        rows = QVBoxLayout(self.vs_row)
        rows.setContentsMargins(0, 2, 0, 0)
        rows.setSpacing(9)
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        rows.addWidget(line)
        vs = QHBoxLayout()
        rows.addLayout(vs)
        vs.setSpacing(6)
        cap = QLabel("vs their lineup")
        cap.setFont(caps_font(9.5, 700, 1.4))
        cap.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 9.5pt; color: {theme.MUTED};')
        vs.addWidget(cap)
        vs.addSpacing(4)
        self.vs_lay = QHBoxLayout()
        self.vs_lay.setSpacing(6)
        vs.addLayout(self.vs_lay)
        vs.addStretch(1)
        self.body.addWidget(self.vs_row)
        self.vs_row.hide()
        self._shown: tuple | None = None
        self._lines: dict[str, _ItemLine] = {}

    def _stats(self):
        s = getattr(self.svc, "stats", None)
        return s if s is not None and s.loaded else None

    @staticmethod
    def _clear(lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()

    def set(self, g: dict | None) -> None:
        if g is None:
            return
        from ..knowledge import counters

        s = self._stats()
        build = g.get("build")  # None: the engine has no item data yet
        nxt = tuple(e["key"] for e in build or ())
        owned = set(g.get("item_keys") or []) | {g.get("neutral_key") or ""}
        vs = tuple((k, tuple(who)) for k, _why, who in counters.counter_items(g.get("enemies") or [])
                   if k not in owned and k not in nxt)[:3] if s is not None else ()
        shown = (build is None, nxt, vs, s is not None)
        if shown != self._shown:
            self._shown = shown
            self._lines = {}
            self._clear(self.next_lay)
            if not nxt:
                self.next_lay.addWidget(label("Doty is loading your hero's build." if build is None else
                                              "You have the usual build. Buy what the game needs.", "faint",
                                              wrap=True))
            for e in build or ():
                line = _ItemLine(s, e["key"], e.get("name") or "")
                self.next_lay.addWidget(line)
                self._lines[e["key"]] = line
            self._clear(self.vs_lay)
            for k, who in vs:
                icon = ItemIcon(36, 26, hover=True)
                icon.set_item(s, k)
                icon.setToolTip(tr("{item}: against {who}", item=s.item_name(k), who=", ".join(who[:2])))
                self.vs_lay.addWidget(icon)
            self.vs_row.setVisible(bool(vs))
        for i, e in enumerate(build or ()):
            line = self._lines.get(e["key"])
            if line is not None:
                line.set_entry(e, first=i == 0)
