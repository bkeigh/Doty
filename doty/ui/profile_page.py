"""The Profile page: who you are in Dota (name, avatar, rank medal), your numbers over the last 20 games,
your form as a bar chart, the heroes you've played most, and every game with a performance score.

The account comes from Dota's game data the first time Doty sees a match, or you type it. It's your
own account only: Doty doesn't look up other players.

Layout: one column, centered and at most MAX_W wide. When the window resizes the page works out the
column's width and hands it down, so the stat tiles pick how many fit in a row, the Form and Most played
cards sit side by side or stack, and each game folds its numbers under the hero's name when narrow.
"""

from __future__ import annotations

import math
import time
import urllib.parse
import urllib.request

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QIcon, QImage, QLinearGradient, QPainter, QPen, QPixmap,
                           QPolygonF, QRadialGradient)
from PySide6.QtWidgets import (QBoxLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy,
                               QToolTip, QVBoxLayout, QWidget)

from ..i18n import tr
from ..config import CACHE_DIR
from ..knowledge.profile import Game, Profile, ago, parse_account, rank_name
from . import theme
from .art import enabled as art_enabled
from .brand import cut_path, paint_panel
from .motion import Shimmer, fade_in
from .stats_pages import StatsService
from .visuals import HeroPortrait
from .widgets import ArtBanner, Card, Tag, button, icon_label, label, set_button

MAX_W = 1200  # the column never gets wider than this; wider windows center it
SIDE = 34  # the page's side margin on narrower windows
GAP = 16  # between cards
SPLIT_AT = 1000  # Form and Most played sit side by side from this column width
TILE_MIN = 116  # a stat tile's narrowest
ROW_WIDE = 790  # a game row needs this much width to show every number in its own column
AVATAR_TTL_S = 7 * 86400
AVATAR_HOSTS = (".steamstatic.com", ".akamaihd.net")  # where OpenDota's avatar links point

NEEDS_PUBLIC_DATA = ("Play a match with Doty running, or type your account ID above. Your games show up once "
                     "Dota shares them: in Dota, Settings > Options > Social > turn on Expose Public Match Data.")


class _G:
    """Segoe MDL2 Assets glyphs this page uses beyond theme.Glyph."""

    UP = ""
    DOWN = ""
    STAR = ""
    CHART = ""
    GAMES = ""
    CLOCK = ""


# Medal colors, by tier (rank_tier // 10). They name a medal, nothing more: no good or bad meaning, so they
# stay clear of the theme's green, red, amber and gold.
RANK_COLORS = {1: "#a38c74", 2: "#8db897", 3: "#5cbfae", 4: "#6ea6ee", 5: "#cdc46c", 6: "#ad91f2", 7: "#d4dfeb",
               8: "#f0905f"}


def rank_color(rank_tier: int | None) -> str:
    return RANK_COLORS.get(int(rank_tier or 0) // 10, theme.GREY)


def score_color(score: float | None) -> str:
    """A game's performance score: 60+ good, 40-60 ordinary, under 40 a rough one."""
    if score is None:
        return theme.MUTED
    return theme.GREEN if score >= 60 else theme.AMBER if score >= 40 else theme.RED


def kda_ratio(g: Game) -> float:
    return (g.kills + g.assists) / max(1, g.deaths)


def kda_color(ratio: float) -> str:
    return theme.GREEN if ratio >= 4 else theme.RED if ratio < 1.5 else theme.TEXT


def win_color(rate: float) -> str:
    return theme.GREEN if rate >= 0.53 else theme.RED if rate <= 0.47 else theme.TEXT


def mmss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _span(text, color: str, weight: int | None = None) -> str:
    w = f" font-weight:{weight};" if weight else ""
    return f'<span style="color:{color};{w}">{text}</span>'


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def _out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def _font(pt: float, weight: int = 700, family: str = theme.DISPLAY) -> QFont:
    f = QFont(family)
    f.setPointSizeF(pt)
    f.setWeight(QFont.Weight(weight))
    return f


def _text(text: str, pt: float, color: str = theme.TEXT, weight: int = 700, display: bool = True,
          rich: bool = False) -> QLabel:
    """A label with its own font (the app stylesheet names a font on every widget, so it goes in the label's
    stylesheet too)."""
    lab = QLabel(text)
    lab.setTextFormat(Qt.RichText if rich else Qt.PlainText)
    family = theme.DISPLAY if display else theme.FONT
    lab.setStyleSheet(f'font-family: "{family}"; font-size: {pt}pt; font-weight: {weight}; color: {color};')
    return lab


def _glyph_icon(glyph: str, color: str, px: int = 16) -> QIcon:
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    f = QFont(theme.ICON_FONT)
    f.setPixelSize(int(px * 0.8))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, px, px), Qt.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


def _title_row(title: str, hint: str = "", glyph: str = "") -> QHBoxLayout:
    """A card's heading: a small icon, the title in condensed capitals, and a quiet hint on the right."""
    row = QHBoxLayout()
    row.setSpacing(8)
    if glyph:
        row.addWidget(icon_label(glyph, 13, theme.MUTED))
    row.addWidget(label(title.upper(), "section"))
    row.addStretch(1)
    if hint:
        h = label(hint, "faint")
        h.setStyleSheet("font-size: 8.5pt;")
        row.addWidget(h)
    return row


# --------------------------------------------------------------------------- motion


class _Ease:
    """A 0..1 value that eases in (OutCubic) after an optional delay, for counting numbers and growing bars.
    It rests at 1, so a page that never animates (hidden, or in tests) shows the final values."""

    def __init__(self, owner: QWidget, on_change, ms: int = 800) -> None:
        self.raw = self.t = 1.0
        self._ms, self._delay, self._cb = ms, 0, on_change
        self._a = QVariantAnimation(owner)
        self._a.valueChanged.connect(self._tick)

    def reset(self) -> None:
        """Back to the start (until `play`), so nothing flashes at its final value first."""
        self._a.stop()
        self._delay = 0
        self._tick(0.0)

    def play(self, delay: int = 0) -> None:
        self._a.stop()
        self._delay = max(0, int(delay))
        self._a.setDuration(self._delay + self._ms)
        self._a.setStartValue(0.0)
        self._a.setEndValue(float(self._delay + self._ms))
        self._tick(0.0)
        self._a.start()

    def _tick(self, v) -> None:
        self.raw = _clamp((float(v) - self._delay) / self._ms)
        self.t = _out_cubic(self.raw)
        self._cb(self.t)


class _HoverGlow:
    """Eases a widget's hover highlight in and out."""

    def __init__(self, owner: QWidget, ms: int = 150) -> None:
        self.value = 0.0
        self._a = QVariantAnimation(owner)
        self._a.setDuration(ms)
        self._a.valueChanged.connect(lambda v: (setattr(self, "value", float(v)), owner.update()))

    def go(self, on: bool) -> None:
        self._a.stop()
        self._a.setStartValue(self.value)
        self._a.setEndValue(1.0 if on else 0.0)
        self._a.start()


class _Tally(QLabel):
    """A number that counts up from zero: `render(t)` gives the label's text at t (0..1)."""

    def __init__(self, render, pt: float = 23, color: str = theme.TEXT) -> None:
        super().__init__()
        self.setTextFormat(Qt.RichText)
        self._render = render
        self.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-size: {pt}pt; font-weight: 800; color: {color};')
        self.ease = _Ease(self, lambda t: self.setText(self._render(t)), 900)
        self.setText(render(1.0))


# --------------------------------------------------------------------------- small pictures


class _Elided(QLabel):
    """One line of text that shortens with "…" instead of pushing its neighbours."""

    def __init__(self, text: str, style: str = "") -> None:
        super().__init__(text)
        self._full = text
        self.setTextFormat(Qt.PlainText)
        if style:
            self.setStyleSheet(style)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)

    def full_text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(30, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, self.width())
        if shown != self.text():
            self.setText(shown)


class _Avatar(QWidget):
    """Your Steam avatar in a cut-corner frame ringed in your medal's color (your initials until it loads)."""

    def __init__(self, name: str, ring: str, size: int = 88) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._img: QImage | None = None
        self._ring = QColor(ring)
        words = [w for w in name.replace("_", " ").split() if w[:1].isalnum()]
        self._initials = ("".join(w[0] for w in words[:2]) if len(words) > 1 else name[:2]).upper() or "?"

    def set_image(self, img: QImage | None) -> None:
        self._img = img if img is not None and not img.isNull() else None
        self.update()

    def has_image(self) -> bool:
        return self._img is not None

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = cut_path(r, 14)
        p.save()
        p.setClipPath(path)
        if self._img is not None:
            iw, ih = self._img.width(), self._img.height()
            s = max(r.width() / iw, r.height() / ih)
            p.drawImage(QRectF(r.center().x() - iw * s / 2, r.center().y() - ih * s / 2, iw * s, ih * s), self._img)
        else:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor("#1f2833"))
            g.setColorAt(1, QColor("#0d1116"))
            p.fillRect(r, g)
            tint = QColor(self._ring)
            tint.setAlpha(40)
            p.fillRect(r, tint)
            f = _font(1, 800)
            f.setPixelSize(int(r.height() * 0.4))
            p.setFont(f)
            p.setPen(QColor(theme.TEXT_2))
            p.drawText(r, Qt.AlignCenter, self._initials)
        p.restore()
        p.setPen(QPen(self._ring, 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


def _star(cx: float, cy: float, outer: float, inner: float) -> QPolygonF:
    pts = []
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = math.radians(-90 + i * 36)
        pts.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    return QPolygonF(pts)


# The game's own rank medals (Valve's art, as OpenDota serves it): a medal per tier and its stars on top. Fetched
# when first needed and kept in the art cache, like hero portraits; never bundled with Doty.
RANK_ART = "https://www.opendota.com/assets/images/dota2/rank_icons/"
_rank_images: dict[str, QImage] = {}


def _rank_files(tier: int) -> list[str]:
    medal, stars = divmod(int(tier or 0), 10)
    if not 1 <= medal <= 8:
        return []
    files = [f"rank_icon_{medal}.png"]
    if medal < 8 and 1 <= stars <= 5:
        files.append(f"rank_star_{stars}.png")
    return files


def _rank_file(name: str):
    return CACHE_DIR / "art" / "rank" / name


def fetch_rank_art(tier: int) -> int:
    """Download the medal and star pictures for this rank (on a worker thread); the tier, for the page."""
    if not art_enabled():
        return tier
    for name in _rank_files(tier):
        f = _rank_file(name)
        if f.exists():
            continue
        req = urllib.request.Request(RANK_ART + name, headers={"User-Agent": "Doty (open-source Dota 2 coach)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(1_000_000)
        if QImage.fromData(data).isNull():
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        part = f.with_suffix(".part")
        part.write_bytes(data)
        part.replace(f)
    return tier


def _rank_art(tier: int) -> list[QImage] | None:
    """The medal's pictures from the cache, or None until they're downloaded."""
    names = _rank_files(tier)
    if not names:
        return None
    out = []
    for name in names:
        img = _rank_images.get(name)
        if img is None:
            f = _rank_file(name)
            if not f.exists():
                return None
            img = QImage(str(f))
            if img.isNull():
                return None
            _rank_images[name] = img
        out.append(img)
    return out


class _Medal(QWidget):
    """Your rank medal: the game's own art once it's downloaded (the medal with its stars on top); until then, or
    offline, a drawn stand-in, a hexagon in the medal's color with its stars as pips below. Unranked is a dashed
    outline with a question mark."""

    def __init__(self, rank_tier: int | None, size: int = 62) -> None:
        super().__init__()
        self._tier = int(rank_tier or 0)
        self.setFixedSize(size, int(size * 1.14))
        self.setToolTip(rank_name(rank_tier))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w = self.width()
        art = _rank_art(self._tier)
        if art:
            side = min(w, self.height())
            target = QRectF((w - side) / 2, (self.height() - side) / 2, side, side)
            for img in art:  # the medal, then its stars over it
                p.drawImage(target, img)
            p.end()
            return
        medal, stars = divmod(self._tier, 10)
        cx, cy, r = w / 2, w * 0.47, w * 0.43
        hexagon = QPolygonF([QPointF(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
                             for a in range(-90, 270, 60)])
        if not medal:
            p.setPen(QPen(QColor(theme.GREY), 1.6, Qt.DashLine))
            p.setBrush(QColor(255, 255, 255, 8))
            p.drawPolygon(hexagon)
            f = _font(1, 800)
            f.setPixelSize(int(r))
            p.setFont(f)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignCenter, "?")
            p.end()
            return
        c = QColor(rank_color(self._tier))
        glow = QRadialGradient(QPointF(cx, cy), r * 1.25)
        halo = QColor(c)
        halo.setAlpha(60)
        glow.setColorAt(0.7, halo)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r * 1.25, r * 1.25)
        fill = QRadialGradient(QPointF(cx, cy - r * 0.4), r * 1.3)
        fill.setColorAt(0.0, c.lighter(145))
        fill.setColorAt(0.55, c)
        fill.setColorAt(1.0, c.darker(230))
        p.setPen(QPen(c.lighter(160), 1.5))
        p.setBrush(fill)
        p.drawPolygon(hexagon)
        inner = QPolygonF([QPointF(cx + (pt.x() - cx) * 0.72, cy + (pt.y() - cy) * 0.72) for pt in hexagon])
        p.setPen(QPen(QColor(0, 0, 0, 80), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawPolygon(inner)
        ink = QColor(theme.ink_on(c.name()))
        ink.setAlpha(215)
        p.setPen(Qt.NoPen)
        p.setBrush(ink)
        p.drawPolygon(_star(cx, cy, r * 0.4, r * 0.17))
        if medal < 8:  # Immortal has no stars
            pr, step = w * 0.05, w * 0.16
            y = self.height() - pr - 2
            for i in range(5):
                p.setBrush(c.lighter(125) if i < stars else QColor("#28313d"))
                p.drawEllipse(QPointF(cx + (i - 2) * step, y), pr, pr)
        p.end()


class _GlyphBadge(QWidget):
    """An icon on a tinted cut-corner plate, for the empty, error and help notices."""

    def __init__(self, glyph: str, color: str, size: int = 56) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self._glyph, self._color = glyph, QColor(color)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = cut_path(r, 10)
        fill = QColor(self._color)
        fill.setAlphaF(0.12)
        p.fillPath(path, fill)
        edge = QColor(self._color)
        edge.setAlphaF(0.55)
        p.setPen(QPen(edge, 1.2))
        p.drawPath(path)
        f = QFont(theme.ICON_FONT)
        f.setPixelSize(int(r.height() * 0.42))
        p.setFont(f)
        p.setPen(self._color)
        p.drawText(r, Qt.AlignCenter, self._glyph)
        p.end()


class _SplitBar(QWidget):
    """A thin won/lost bar: green grows from the left, red from the right, meeting at the split."""

    def __init__(self, good: int, bad: int, height: int = 4) -> None:
        super().__init__()
        self._good, self._bad = good, bad
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.ease = _Ease(self, lambda _t: self.update(), 800)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        r = QRectF(self.rect())
        p.fillRect(r, QColor("#1b232d"))
        total = self._good + self._bad
        if total:
            t = self.ease.t
            split = r.width() * self._good / total
            p.fillRect(QRectF(0, 0, split * t, r.height()), QColor(theme.GREEN))
            red = (r.width() - split) * t
            p.fillRect(QRectF(r.width() - red, 0, red, r.height()), QColor(theme.RED))
            if self._good and self._bad and t > 0.98:
                p.fillRect(QRectF(split - 1, 0, 2, r.height()), QColor(theme.SURFACE))
        p.end()


class _ScoreBar(QWidget):
    """A game's 0-100 score as a short bar (a tick marks 50, an average game) and the number beside it."""

    def __init__(self, score: float | None, width: int = 120, turbo: bool = False) -> None:
        super().__init__()
        self._score = score
        self.setFixedSize(width, 30)
        self.ease = _Ease(self, lambda _t: self.update(), 800)
        none = ("A Turbo game: not scored. The benchmarks are from normal games, where gold and experience "
                "come half as fast." if turbo else "No benchmark for this hero yet")
        self.setToolTip(none if score is None else
                        f"Performance score {score:.0f} of 100 (50 = an average game on this hero)")

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        num_w = 34
        bar = QRectF(0, h / 2 - 3, w - num_w - 8, 6)
        p.fillRect(bar, QColor("#1b232d"))
        color = QColor(score_color(self._score))
        t = self.ease.t
        if self._score is not None:
            p.fillRect(QRectF(bar.left(), bar.top(), bar.width() * self._score / 100 * t, bar.height()), color)
        p.setPen(QPen(QColor("#4a5768"), 1))
        x = bar.left() + bar.width() / 2
        p.drawLine(QPointF(x, bar.top() - 3), QPointF(x, bar.bottom() + 3))
        p.setFont(_font(14.5, 800))
        p.setPen(color)
        text = "–" if self._score is None else f"{round(self._score * t)}"
        p.drawText(QRectF(w - num_w, 0, num_w, h), Qt.AlignRight | Qt.AlignVCenter, text)
        p.end()


# --------------------------------------------------------------------------- the header


class _StatTile(QWidget):
    """One of your numbers: a small caption, the number counting up, and a line under it."""

    def __init__(self, caption: str, render, color: str = theme.TEXT, sub: str = "", accent: str | None = None,
                 tip: str = "", extra: QWidget | None = None) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 11, 12, 12)
        v.setSpacing(2)
        cap = label(caption.upper(), "eyebrow")
        cap.setStyleSheet(f"font-size: 9pt; color: {theme.MUTED};")
        v.addWidget(cap)
        self.value = _Tally(render, 23, color)
        v.addWidget(self.value)
        if extra is not None:
            v.addSpacing(3)
            v.addWidget(extra)
        if sub:
            s = label(sub, "faint", wrap=True)
            s.setStyleSheet("font-size: 8.5pt;")
            v.addWidget(s)
        v.addStretch(1)
        self._accent = accent
        self.setToolTip(tip)
        self.setMinimumWidth(TILE_MIN)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        paint_panel(p, QRectF(self.rect()), self._accent, "#131a22", 8)
        p.end()


class _TileGrid(QWidget):
    """Tiles in equal columns: all in one row when they fit, else two or three rows (never a lonely one)."""

    def __init__(self, tiles: list[QWidget], gap: int = 12) -> None:
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(gap)
        self._gap, self._tiles, self._cols = gap, tiles, 0
        self._place(len(tiles))

    def columns(self) -> int:
        return self._cols

    def set_width(self, w: int) -> None:
        n = len(self._tiles)
        for cols in (n, (n + 1) // 2, (n + 2) // 3, 1):
            if cols * TILE_MIN + (cols - 1) * self._gap <= w:
                break
        self._place(max(1, cols))

    def _place(self, cols: int) -> None:
        if cols == self._cols:
            return
        for t in self._tiles:
            self._grid.removeWidget(t)
        for c in range(len(self._tiles)):
            self._grid.setColumnStretch(c, 1 if c < cols else 0)
        for i, t in enumerate(self._tiles):
            self._grid.addWidget(t, i // cols, i % cols)
        self._cols = cols


def _stat_tiles(games: list[Game]) -> list[_StatTile]:
    n = len(games)
    wins = sum(g.won for g in games)
    rate = wins / n
    kda = _avg([kda_ratio(g) for g in games]) or 0.0
    score = _avg([g.score for g in games if g.score is not None])
    # (Turbo games have no score: the benchmarks are from normal games.)
    score_sub = "Turbo isn't scored" if score is None and any(g.turbo for g in games) else "50 = average"
    gpm = _avg([g.gpm for g in games]) or 0
    xpm = _avg([g.xpm for g in games]) or 0
    dash = _span("–", theme.MUTED)

    def record(t: float) -> str:
        return f"{_span(round(wins * t), theme.GREEN)}{dash}{_span(round((n - wins) * t), theme.RED)}"

    tiles = [
        _StatTile("Record", record, sub="won – lost", tip=f"{wins} won, {n - wins} lost in your last {n} games"),
        _StatTile("Win rate", lambda t: f"{round(rate * 100 * t)}%", win_color(rate),
                  accent=win_color(rate) if win_color(rate) != theme.TEXT else None,
                  extra=_SplitBar(wins, n - wins), tip="How many of these games you won"),
        _StatTile("Avg KDA", lambda t: f"{kda * t:.1f}", kda_color(kda), sub="per game",
                  tip="(kills + assists) / deaths, averaged over the games"),
        _StatTile("Avg score", lambda t: "–" if score is None else f"{round(score * t)}", score_color(score),
                  sub=score_sub, accent=score_color(score) if score is not None else None,
                  tip="Your performance score, averaged: each game's GPM, XPM, last hits, kills, damage and "
                      "healing against everyone else's games on the same hero. 50 is an average game."),
        _StatTile("Avg GPM", lambda t: f"{round(gpm * t)}", theme.GOLD, sub="gold / minute",
                  tip="Gold per minute, averaged"),
        _StatTile("Avg XPM", lambda t: f"{round(xpm * t)}", sub="xp / minute", tip="Experience per minute, averaged"),
    ]
    return tiles


class _PlayerHeader(ArtBanner):
    """Your avatar, name and rank medal over dark steel with the medal's color down the left, and your
    numbers (record, win rate, KDA, score, GPM, XPM) as tiles under them."""

    def __init__(self, acc: int, player: dict, games: list[Game]) -> None:
        super().__init__(margins=(30, 22, 26, 22))
        self.body.setSpacing(18)
        tier = player.get("rank_tier")
        color = rank_color(tier)
        self.set_bar(color)
        prof = player.get("profile") or {}
        name = str(prof.get("personaname") or f"Account {acc}")
        top = QHBoxLayout()
        top.setSpacing(18)
        self.avatar = _Avatar(name, color)
        top.addWidget(self.avatar, 0, Qt.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        names.addStretch(1)
        eyebrow = label("YOUR PROFILE", "eyebrow")
        eyebrow.setStyleSheet(f"font-size: 9pt; color: {theme.MUTED};")
        names.addWidget(eyebrow)
        self.name = _Elided(name, f'font-family: "{theme.DISPLAY}"; font-size: 27pt; font-weight: 800;'
                                  f" color: {theme.TEXT};")
        names.addWidget(self.name)
        shown = f"last {len(games)} games" if games else "no public games yet"
        meta = _text(f"Account {acc} &nbsp;·&nbsp; {shown} &nbsp;·&nbsp; from OpenDota", 9.5, theme.MUTED, 500,
                     display=False, rich=True)
        names.addWidget(meta)
        names.addStretch(1)
        top.addLayout(names, 1)
        rank = QHBoxLayout()
        rank.setSpacing(12)
        rank.addWidget(_Medal(tier, 72), 0, Qt.AlignVCenter)
        rank_texts = QVBoxLayout()
        rank_texts.setSpacing(0)
        rank_texts.addStretch(1)
        cap = label("RANK", "eyebrow")
        cap.setStyleSheet(f"font-size: 9pt; color: {theme.MUTED};")
        rank_texts.addWidget(cap)
        rank_text = rank_name(tier, player.get("leaderboard_rank")) if tier else "Unranked"
        self.rank = _text(rank_text, 19, color if tier else theme.TEXT_2, 800)
        rank_texts.addWidget(self.rank)
        medal, stars = divmod(int(tier or 0), 10)
        sub = ("or hidden on your Dota profile" if not tier else "the top medal" if medal == 8 else
               f"{stars} of 5 stars")
        rank_texts.addWidget(_text(sub, 9, theme.MUTED, 500, display=False))
        rank_texts.addStretch(1)
        rank.addLayout(rank_texts)
        top.addLayout(rank)
        self.body.addLayout(top)
        self.tiles: _TileGrid | None = None
        if games:
            self.tiles = _TileGrid(_stat_tiles(games))
            self.body.addWidget(self.tiles)

    def set_width(self, w: int) -> None:
        if self.tiles is not None:
            m = self.body.contentsMargins()
            self.tiles.set_width(w - m.left() - m.right())


# --------------------------------------------------------------------------- form


def _streak(games: list[Game]) -> tuple[bool, int]:
    """(won?, how many in a row) counting back from your newest game."""
    if not games:
        return True, 0
    won, n = games[0].won, 0
    for g in games:
        if g.won != won:
            break
        n += 1
    return won, n


def _trend(games: list[Game]) -> str:
    """How your last five scores compare with the games before them, as rich text ("" if too few)."""
    scored = [g.score for g in games if g.score is not None]
    recent, earlier = scored[:5], scored[5:]
    if len(recent) < 5 or len(earlier) < 3:
        return ""
    r, e = sum(recent) / 5, sum(earlier) / len(earlier)
    diff = r - e
    num = _span(f"{r:.0f}", score_color(r), 800)
    if abs(diff) < 3:
        return f"Steady: your last 5 games average a score of {num}, about the same as the {len(earlier)} before."
    color = theme.GREEN if diff > 0 else theme.RED
    arrow = (f'<span style="font-family:\'{theme.ICON_FONT}\'; color:{color};">'
             f"{_G.UP if diff > 0 else _G.DOWN}</span>")
    way = "up" if diff > 0 else "down"
    return (f"{arrow}&nbsp; Your last 5 games average a score of {num}, "
            f"{_span(f'{abs(diff):.0f} {way}', color, 800)} on the {len(earlier)} before.")


class _FormChart(QWidget):
    """Your last games as bars, oldest on the left: the height is the game's score, green for a win and red
    for a loss, with W or L under each. Hover a bar for the game."""

    GUTTER, FOOT = 30, 22

    def __init__(self, stats, games: list[Game]) -> None:  # oldest first
        super().__init__()
        self._s, self._games, self._hover = stats, games, -1
        self.setFixedHeight(156)
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName(f"Form chart: last {len(games)} games, "
                               f"{sum(g.won for g in games)} won, {sum(not g.won for g in games)} lost")
        self.ease = _Ease(self, lambda _t: self.update(), 1100)

    def _plot(self) -> QRectF:
        return QRectF(self.GUTTER, 8, self.width() - self.GUTTER - 2, self.height() - 8 - self.FOOT)

    def _index(self, x: float) -> int:
        plot = self._plot()
        if not self._games or x < plot.left():
            return -1
        i = int((x - plot.left()) / (plot.width() / len(self._games)))
        return i if 0 <= i < len(self._games) else -1

    def _tip(self, g: Game) -> str:
        score = ("Turbo isn't scored" if g.turbo else "no score") if g.score is None else f"score {g.score:.0f}"
        return (f"{self._s.name(g.hero_id)}: {'won' if g.won else 'lost'}, {ago(g.start_time)}\n"
                f"{g.kills} / {g.deaths} / {g.assists} · {score} · {g.gpm} GPM · {mmss(g.duration_s)}")

    def event(self, e) -> bool:
        if e.type() == QEvent.ToolTip:
            i = self._index(e.pos().x())
            if i >= 0:
                QToolTip.showText(e.globalPos(), self._tip(self._games[i]), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        i = self._index(e.position().x())
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        plot = self._plot()
        n = len(self._games)
        if not n:
            p.end()
            return
        slot = plot.width() / n
        bw = min(26.0, slot * 0.66)
        p.setFont(_font(8.5, 700))
        for v in (100, 50):
            y = plot.bottom() - plot.height() * v / 100
            p.setPen(QPen(QColor(theme.BORDER_STRONG if v == 50 else theme.BORDER), 1,
                          Qt.DashLine if v == 50 else Qt.SolidLine))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(0, y - 8, self.GUTTER - 7, 16), Qt.AlignRight | Qt.AlignVCenter, str(v))
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 1))
        p.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        raw = self.ease.raw
        for i, g in enumerate(self._games):
            left = plot.left() + slot * i
            x = left + (slot - bw) / 2
            grow = _out_cubic(_clamp((raw - 0.5 * i / max(1, n - 1)) / 0.5))
            col = QColor(theme.GREEN if g.won else theme.RED)
            if i == self._hover:
                p.fillRect(QRectF(left, plot.top() - 4, slot, plot.height() + self.FOOT + 4), QColor(255, 255, 255, 14))
                col = col.lighter(118)
            if g.score is None:
                h = plot.height() * 0.16 * grow
                dim = QColor(col)
                dim.setAlpha(170)
                p.setPen(QPen(dim, 1.2, Qt.DashLine))
                p.setBrush(Qt.NoBrush)
                p.drawRect(QRectF(x + 0.5, plot.bottom() - h, bw - 1, h))
            else:
                h = max(3.0, plot.height() * g.score / 100) * grow
                top = QRectF(x, plot.bottom() - h, bw, h)
                grad = QLinearGradient(0, top.top(), 0, top.bottom())
                grad.setColorAt(0.0, col)
                fade = QColor(col)
                fade.setAlpha(80)
                grad.setColorAt(1.0, fade)
                p.fillRect(top, grad)
                p.fillRect(QRectF(x, top.top(), bw, min(2.0, h)), col.lighter(135))
            letter = QColor(col)
            letter.setAlphaF(0.35 + 0.65 * grow)
            p.setPen(letter)
            p.setFont(_font(9, 800))
            p.drawText(QRectF(left, plot.bottom() + 4, slot, self.FOOT - 4), Qt.AlignHCenter | Qt.AlignTop,
                       "W" if g.won else "L")
        p.end()


class _FormCard(Card):
    def __init__(self, stats, games: list[Game]) -> None:  # newest first
        super().__init__(margins=(20, 16, 20, 14), spacing=12)
        self.body.addLayout(_title_row("Form", "oldest to newest · bar height = score", _G.CHART))
        row = QHBoxLayout()
        row.setSpacing(12)
        won, n = _streak(games)
        text = (f"{n} {'win' if won else 'loss'} streak" if n > 1 else "won last game" if won else "lost last game")
        self.streak = Tag(text, theme.GREEN if won else theme.RED, pt=10)
        row.addWidget(self.streak, 0, Qt.AlignVCenter)
        trend = _trend(games)
        self.trend = _text(trend, 9.5, theme.TEXT_2, 500, display=False, rich=True)
        self.trend.setWordWrap(True)
        self.trend.setVisible(bool(trend))
        row.addWidget(self.trend, 1)
        if not trend:
            row.addStretch(1)
        self.body.addLayout(row)
        self.chart = _FormChart(stats, list(reversed(games)))
        self.body.addWidget(self.chart)
        self.body.addStretch(1)


# --------------------------------------------------------------------------- most played


class _HeroCard(QWidget):
    """One of your most-played heroes: a big portrait, your record on it as numbers and a bar, your KDA
    and average score."""

    def __init__(self, stats, hero_id: int, games: list[Game], total: int) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        n, wins = len(games), sum(g.won for g in games)
        name = stats.name(hero_id)
        self.portrait = HeroPortrait(150, 84, hover=True)
        self.portrait.set_hero(stats, hero_id)
        self.portrait.setCursor(Qt.ArrowCursor)
        v.addWidget(self.portrait)
        v.addSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = _Elided(name, f"font-size: 10.5pt; font-weight: 600; color: {theme.TEXT};")
        top.addWidget(self.name, 1)
        top.addWidget(_text(f"{n} game{'s' if n != 1 else ''}", 9, theme.MUTED, 500, display=False))
        v.addLayout(top)
        rec = QHBoxLayout()
        rec.setSpacing(6)
        rec.addWidget(_text(f"{_span(f'{wins}W', theme.GREEN)} {_span('–', theme.MUTED)} "
                            f"{_span(f'{n - wins}L', theme.RED)}", 12.5, theme.TEXT, 800, rich=True))
        rec.addStretch(1)
        rate = wins / n
        rec.addWidget(_text(f"{rate * 100:.0f}%", 12.5, win_color(rate), 800))
        v.addLayout(rec)
        self.bar = _SplitBar(wins, n - wins)
        v.addWidget(self.bar)
        kda = _avg([kda_ratio(g) for g in games]) or 0.0
        score = _avg([g.score for g in games if g.score is not None])
        parts = [f"KDA {_span(f'{kda:.1f}', kda_color(kda), 700)}"]
        if score is not None:
            parts.append(f"score {_span(f'{score:.0f}', score_color(score), 700)}")
        v.addWidget(_text(" &nbsp;·&nbsp; ".join(parts), 9, theme.MUTED, 500, display=False, rich=True))
        v.addStretch(1)
        self.setToolTip(tr("{name}: {wins} won, {lost} lost of your last {total} games", name=name, wins=wins,
                           lost=n - wins, total=total))
        self.set_width(150)

    def set_width(self, w: int) -> None:
        w = max(96, int(w))
        self.setFixedWidth(w)
        self.portrait.setFixedSize(w, round(w * 9 / 16))


class _MostPlayedCard(Card):
    """Your top three heroes lately (four when the card spans the column), as big portrait tiles."""

    MIN_TILE, MAX_TILE, SPACING = 180, 300, 14

    def __init__(self, stats, games: list[Game]) -> None:
        super().__init__(margins=(20, 16, 20, 16), spacing=12)
        self.body.addLayout(_title_row("Most played", "your top heroes lately", _G.STAR))
        by_hero: dict[int, list[Game]] = {}
        for g in games:
            by_hero.setdefault(g.hero_id, []).append(g)
        top = sorted(by_hero.items(), key=lambda kv: (-len(kv[1]), -sum(g.won for g in kv[1])))[:4]
        row = QHBoxLayout()
        row.setSpacing(self.SPACING)
        self.tiles = [_HeroCard(stats, hid, gs, len(games)) for hid, gs in top]
        for t in self.tiles:
            row.addWidget(t, 0, Qt.AlignTop)
        row.addStretch(1)
        self.body.addLayout(row)
        self.body.addStretch(1)
        self.count = 3

    def set_width(self, w: int) -> None:
        m = self.body.contentsMargins()
        inner = w - m.left() - m.right()
        self.count = max(3, min(4, (inner + self.SPACING) // (self.MIN_TILE + self.SPACING)))
        tile = min(self.MAX_TILE, (inner - self.SPACING * (self.count - 1)) // self.count)
        for i, t in enumerate(self.tiles):
            t.set_width(tile)
            t.setVisible(i < self.count)


class _Split(QWidget):
    """Two cards side by side on a wide column, one above the other on a narrow one."""

    def __init__(self, a: QWidget, b: QWidget, ratio: float = 0.56) -> None:
        super().__init__()
        self.a, self.b, self._ratio = a, b, ratio
        self.box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(GAP)
        self.box.addWidget(a)
        self.box.addWidget(b)

    def side_by_side(self) -> bool:
        return self.box.direction() == QBoxLayout.LeftToRight

    def set_width(self, w: int) -> None:
        side = w >= SPLIT_AT
        self.box.setDirection(QBoxLayout.LeftToRight if side else QBoxLayout.TopToBottom)
        aw = int((w - GAP) * self._ratio) if side else w
        bw = (w - GAP - aw) if side else w
        for card, cw in ((self.a, aw), (self.b, bw)):
            card.setFixedWidth(cw)
            if hasattr(card, "set_width"):
                card.set_width(cw)


# --------------------------------------------------------------------------- recent games


GAME_COLS = (("K / D / A", 104, "kills / deaths / assists"), ("GPM", 54, "gold per minute"),
             ("XPM", 54, "experience per minute"), ("LH", 44, "last hits"), ("LENGTH", 54, "game length"),
             ("SCORE", 120, "performance score: 50 = an average game on that hero, 100 = the best"))
WIDE_ONLY = {"GPM", "XPM", "LH", "LENGTH"}
ROW_MARGINS = (20, 8, 18, 8)


class _GamesHeader(QWidget):
    def __init__(self) -> None:
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(ROW_MARGINS[0], 0, ROW_MARGINS[2], 2)
        h.setSpacing(12)
        pad = QWidget()  # (a widget, not addSpacing: Qt adds no spacing next to a spacer)
        pad.setFixedWidth(88)
        h.addWidget(pad)
        h.addWidget(self._cap("HERO", "the hero, whether you won, and when"), 1)
        self.wide: list[QLabel] = []
        for caption, width, tip in GAME_COLS:
            cap = self._cap(caption, tip)
            cap.setFixedWidth(width)
            h.addWidget(cap)
            if caption in WIDE_ONLY:
                self.wide.append(cap)

    @staticmethod
    def _cap(text: str, tip: str) -> QLabel:
        cap = label(text, "eyebrow")
        cap.setStyleSheet(f"font-size: 9pt; color: {theme.MUTED};")
        cap.setToolTip(tip)
        return cap

    def set_compact(self, on: bool) -> None:
        for c in self.wide:
            c.setVisible(not on)


class _GameRow(QWidget):
    """One game: a stripe in the result's color, the hero's portrait and name, won or lost and when, K/D/A
    (deaths in red), GPM, XPM, last hits, length and the score. Click it to open the match on OpenDota."""

    def __init__(self, stats, g: Game) -> None:
        super().__init__()
        self.game = g
        name = stats.name(g.hero_id)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Open this match on OpenDota")
        self.setAccessibleName(f"{'Won' if g.won else 'Lost'} as {name}, {g.kills} / {g.deaths} / {g.assists}, "
                               f"{ago(g.start_time)}")
        self._glow = _HoverGlow(self)
        h = QHBoxLayout(self)
        h.setContentsMargins(*ROW_MARGINS)
        h.setSpacing(12)
        self.portrait = HeroPortrait(88, 50)
        self.portrait.set_hero(stats, g.hero_id)
        self.portrait.setAttribute(Qt.WA_TransparentForMouseEvents)
        h.addWidget(self.portrait)
        names = QVBoxLayout()
        names.setSpacing(1)
        names.addStretch(1)
        names.addWidget(_Elided(name, f"font-size: 10.5pt; font-weight: 600; color: {theme.TEXT};"))
        result = _span("WON" if g.won else "LOST", theme.GREEN if g.won else theme.RED, 800)
        names.addWidget(_text(f"{result}{_span(f' &nbsp;·&nbsp; {ago(g.start_time)}', theme.MUTED)}", 9,
                              theme.MUTED, 600, rich=True))
        self.extra = _text(f"{_span(g.gpm, theme.GOLD, 700)} GPM &nbsp;·&nbsp; {g.xpm} XPM &nbsp;·&nbsp; "
                           f"{g.last_hits} LH &nbsp;·&nbsp; {mmss(g.duration_s)}", 9, theme.MUTED, 500,
                           display=False, rich=True)
        self.extra.hide()
        names.addWidget(self.extra)
        names.addStretch(1)
        h.addLayout(names, 1)

        kda = QWidget()
        kda.setFixedWidth(GAME_COLS[0][1])
        kv = QVBoxLayout(kda)
        kv.setContentsMargins(0, 0, 0, 0)
        kv.setSpacing(0)
        sep = _span(" / ", theme.MUTED)
        kv.addWidget(_text(f"{g.kills}{sep}{_span(g.deaths, theme.RED)}{sep}{g.assists}", 13.5, theme.TEXT, 700,
                           rich=True))
        ratio = kda_ratio(g)
        kv.addWidget(_text(f"KDA {_span(f'{ratio:.1f}', kda_color(ratio), 700)}", 8.5, theme.MUTED, 500,
                           display=False, rich=True))
        h.addWidget(kda)
        self.wide: list[QWidget] = []
        cells = ((g.gpm, theme.GOLD), (g.xpm, theme.TEXT), (g.last_hits, theme.TEXT),
                 (mmss(g.duration_s), theme.TEXT_2))
        for (value, color), (_caption, width, _tip) in zip(cells, GAME_COLS[1:5]):
            cell = _text(str(value), 12.5, color, 700)
            cell.setFixedWidth(width)
            h.addWidget(cell)
            self.wide.append(cell)
        self.score = _ScoreBar(g.score, GAME_COLS[5][1], turbo=g.turbo)
        h.addWidget(self.score)

    def set_compact(self, on: bool) -> None:
        self.extra.setVisible(on)
        for c in self.wide:
            c.setVisible(not on)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._glow.go(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._glow.go(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()) and self.game.match_id:
            QDesktopServices.openUrl(QUrl(f"https://www.opendota.com/matches/{int(self.game.match_id)}"))
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        r = QRectF(self.rect())
        col = QColor(theme.GREEN if self.game.won else theme.RED)
        tint = QLinearGradient(0, 0, r.width() * 0.4, 0)
        start = QColor(col)
        start.setAlphaF(0.09)
        end = QColor(col)
        end.setAlphaF(0.0)
        tint.setColorAt(0, start)
        tint.setColorAt(1, end)
        p.fillRect(r, tint)
        if self._glow.value > 0.01:
            p.fillRect(r, QColor(255, 255, 255, int(12 * self._glow.value)))
        p.fillRect(QRectF(0, 0, 3, r.height()), col)
        p.fillRect(QRectF(0, r.height() - 1, r.width(), 1), QColor(theme.BORDER))
        p.end()


class _GamesCard(Card):
    def __init__(self, stats, games: list[Game]) -> None:
        super().__init__(margins=(0, 16, 0, 12), spacing=4)
        title = _title_row("Recent games", "score 50 = an average game on that hero · click a game to open it "
                                           "on OpenDota", _G.GAMES)
        title.setContentsMargins(20, 0, 20, 6)
        self.body.addLayout(title)
        self.head = _GamesHeader()
        self.body.addWidget(self.head)
        self.rows = [_GameRow(stats, g) for g in games]
        list_box = QVBoxLayout()
        list_box.setSpacing(0)
        for r in self.rows:
            list_box.addWidget(r)
        self.body.addLayout(list_box)
        self.compact: bool | None = None

    def set_width(self, w: int) -> None:
        compact = w < ROW_WIDE
        if compact != self.compact:
            self.compact = compact
            self.head.set_compact(compact)
            for r in self.rows:
                r.set_compact(compact)


# --------------------------------------------------------------------------- states


class _Notice(Card):
    """A designed message: an icon plate, a title, a line or two, optional numbered steps and a button."""

    def __init__(self, glyph: str, color: str, title: str, text: str, steps: tuple = (), action=None,
                 foot: str = "") -> None:
        super().__init__(margins=(28, 24, 28, 24), spacing=14, accent=color)
        top = QHBoxLayout()
        top.setSpacing(18)
        top.addWidget(_GlyphBadge(glyph, color), 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        self.title = _text(title, 17, theme.TEXT, 800)
        texts.addWidget(self.title)
        self.text = label(text, "muted", wrap=True)
        self.text.setTextFormat(Qt.PlainText)
        texts.addWidget(self.text)
        top.addLayout(texts, 1)
        self.body.addLayout(top)
        for i, (g, line) in enumerate(steps, 1):
            row = QHBoxLayout()
            row.setContentsMargins(74, 0, 0, 0)
            row.setSpacing(10)
            num = _text(str(i), 11, theme.INK, 800)
            num.setAlignment(Qt.AlignCenter)
            num.setFixedSize(22, 22)
            num.setStyleSheet(num.styleSheet() + f" background: {theme.BRAND}; border-radius: 11px;")
            row.addWidget(num, 0, Qt.AlignTop)
            row.addWidget(icon_label(g, 15, theme.TEXT_2), 0, Qt.AlignTop)
            step = label(line, wrap=True)
            step.setTextFormat(Qt.PlainText)
            row.addWidget(step, 1)
            self.body.addLayout(row)
        if action is not None or foot:
            row = QHBoxLayout()
            row.setContentsMargins(74, 4, 0, 0)
            row.setSpacing(14)
            if action is not None:
                row.addWidget(action)
            if foot:
                f = label(foot, "faint", wrap=True)
                f.setStyleSheet("font-size: 9pt;")
                row.addWidget(f, 1, Qt.AlignVCenter)
            else:
                row.addStretch(1)
            self.body.addLayout(row)
        self.setMaximumWidth(780)


def _centered(w: QWidget) -> QWidget:
    holder = QWidget()
    h = QHBoxLayout(holder)
    h.setContentsMargins(0, 0, 0, 0)
    h.addStretch(1)
    h.addWidget(w, 100)
    h.addStretch(1)
    return holder


class _View(QWidget):
    """What the page shows (the empty, loading, error or data state): a stack of parts that get the column's
    width when it changes."""

    def __init__(self, parts: list[QWidget], kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.parts = parts
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(GAP)
        for p in parts:
            v.addWidget(p)
        v.addStretch(1)

    def set_width(self, w: int) -> None:
        for p in self.parts:
            if hasattr(p, "set_width"):
                p.set_width(w)

    def eases(self) -> list[tuple[_Ease, QWidget]]:
        return [(w.ease, w) for w in self.findChildren(QWidget) if isinstance(getattr(w, "ease", None), _Ease)]


def _loading_view() -> _View:
    """The page's shape in shimmering placeholders (avatar, name, medal, tiles, game rows) while it loads."""
    head = ArtBanner(margins=(30, 22, 26, 22))
    head.body.setSpacing(18)
    head.set_bar(theme.BRAND_DIM)
    row = QHBoxLayout()
    row.setSpacing(18)
    row.addWidget(Shimmer(block=(88, 88)))
    lines = QVBoxLayout()
    lines.setSpacing(9)
    lines.addStretch(1)
    for w, h in ((84, 10), (250, 26), (300, 11)):
        lines.addWidget(Shimmer(block=(w, h)), 0, Qt.AlignLeft)
    lines.addStretch(1)
    row.addLayout(lines, 1)
    row.addWidget(Shimmer(block=(62, 70)), 0, Qt.AlignVCenter)
    head.body.addLayout(row)
    tiles = QHBoxLayout()
    tiles.setSpacing(12)
    for _ in range(6):
        tiles.addWidget(Shimmer(rows=1, height=64))
    head.body.addLayout(tiles)
    status = QHBoxLayout()
    status.setSpacing(8)
    status.addWidget(icon_label(_G.CLOCK, 13, theme.BRAND))
    status.addWidget(_text("Loading your last 20 games from OpenDota…", 9.5, theme.TEXT_2, 500, display=False))
    status.addStretch(1)
    head.body.addLayout(status)
    games = Card(margins=(20, 16, 20, 16), spacing=12)
    games.body.addLayout(_title_row("Recent games", "", _G.GAMES))
    for _ in range(4):
        r = QHBoxLayout()
        r.setSpacing(12)
        r.addWidget(Shimmer(block=(88, 50)))
        texts = QVBoxLayout()
        texts.setSpacing(8)
        texts.addStretch(1)
        texts.addWidget(Shimmer(block=(150, 12)), 0, Qt.AlignLeft)
        texts.addWidget(Shimmer(block=(100, 10)), 0, Qt.AlignLeft)
        texts.addStretch(1)
        r.addLayout(texts, 1)
        r.addWidget(Shimmer(block=(90, 14)), 0, Qt.AlignVCenter)
        r.addSpacing(24)
        r.addWidget(Shimmer(block=(120, 8)), 0, Qt.AlignVCenter)
        games.body.addLayout(r)
    return _View([head, games], "loading")


# --------------------------------------------------------------------------- avatar


def _avatar_url_ok(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url or "")
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "").endswith(AVATAR_HOSTS)


def _avatar_file(acc: int):
    return CACHE_DIR / "art" / "avatar" / f"{int(acc)}.img"


def _cached_avatar(acc: int) -> QImage | None:
    f = _avatar_file(acc)
    try:
        if f.is_file() and time.time() - f.stat().st_mtime < AVATAR_TTL_S:
            img = QImage(str(f))
            return None if img.isNull() else img
    except OSError:
        pass
    return None


def _fetch_avatar(acc: int, url: str) -> tuple[int, QImage | None]:
    """Download your avatar (on a worker thread) and keep a copy for next time."""
    req = urllib.request.Request(url, headers={"User-Agent": "Doty (open-source Dota 2 coach)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read(2_000_000)
    img = QImage.fromData(data)  # (QImage is safe off the GUI thread)
    if img.isNull():
        return acc, None
    f = _avatar_file(acc)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        part = f.with_suffix(".part")
        part.write_bytes(data)
        part.replace(f)
    except OSError:
        pass
    return acc, img


# --------------------------------------------------------------------------- the page


class ProfilePage(QWidget):
    def __init__(self, ctl, service: StatsService) -> None:
        super().__init__()
        self.ctl, self.svc = ctl, service
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 28, 0, 0)
        lay.setSpacing(16)

        self.head = QWidget()
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(SIDE, 0, SIDE, 0)
        hl.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Profile", "h1"))
        titles.addWidget(label("Your rank, your form and your recent games from OpenDota, with a performance score "
                               "for each.", "muted", wrap=True))
        self.status = label("", "faint", wrap=True)
        self.status.hide()
        titles.addWidget(self.status)
        hl.addLayout(titles, 1)
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.account = QLineEdit()
        self.account.setPlaceholderText("Your Dota account ID or profile link")
        self.account.setMinimumWidth(280)
        self.account.setFixedHeight(38)
        self.account.addAction(_glyph_icon(theme.Glyph.PROFILE, theme.MUTED), QLineEdit.ActionPosition.LeadingPosition)
        self.account.setToolTip("Your Friend ID from Dota, a 64-bit Steam ID, or a Dotabuff / OpenDota / STRATZ "
                                "profile link. Press Enter to load it.")
        if ctl.cfg.app.account_id:
            self.account.setText(str(ctl.cfg.app.account_id))
        self.account.returnPressed.connect(self.load)
        self.refresh_btn = button("Refresh", "ghost", theme.Glyph.REFRESH)
        self.refresh_btn.clicked.connect(self.load)
        controls.addWidget(self.account)
        controls.addWidget(self.refresh_btn)
        hl.addLayout(controls)
        hl.setAlignment(controls, Qt.AlignTop)
        lay.addWidget(self.head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self._row = QHBoxLayout(inner)
        self._row.setContentsMargins(SIDE, 0, 0, 24)
        self._row.setSpacing(0)
        self.column = QWidget()
        self.col = QVBoxLayout(self.column)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(0)
        self._row.addWidget(self.column, 0, Qt.AlignTop)
        self._row.addStretch(1)
        self.scroll.setWidget(inner)
        lay.addWidget(self.scroll, 1)

        self._view: _View | None = None
        self._data: tuple | None = None  # the (account, player, games) on show
        self._avatars: dict[int, QImage] = {}
        self._avatar_asked: set[int] = set()
        self._drawn_with_stats = False
        self._width = MAX_W
        self._pending_play = False
        self._shown = 0
        service.result.connect(self._on_result)
        service.ready.connect(self._on_stats_ready)
        if parse_account(self.account.text()):
            self._set_view(_loading_view())
        else:
            self._show_link()

    # layout ---------------------------------------------------------------------------------------------
    def _fit(self) -> None:
        w = self.width()
        content = max(300, min(MAX_W, w - 2 * SIDE))
        left = max(SIDE, (w - content) // 2)
        self.head.layout().setContentsMargins(left, 0, max(SIDE, w - left - content), 0)
        self._row.setContentsMargins(left, 0, 0, 24)
        self.column.setFixedWidth(content)
        self._width = content
        if self._view is not None:
            self._view.set_width(content)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _set_view(self, view: _View, animate: bool = False) -> None:
        if self._view is not None:
            self.col.removeWidget(self._view)
            self._view.hide()
            self._view.deleteLater()
        self._view = view
        self.col.addWidget(view)
        view.set_width(self._width)
        if self.isVisible():
            if animate:
                self._play()
            fade_in(view, rise=0)
        elif animate:
            self._pending_play = True

    def _play(self) -> None:
        """Count the numbers up and grow the bars, top to bottom and left to right."""
        self._pending_play = False
        view = self._view
        if view is None:
            return
        eases = view.eases()
        for ease, _w in eases:
            ease.reset()

        def go(v=view) -> None:
            if v is not self._view:
                return
            for ease, w in eases:
                at = w.mapTo(v, QPoint(0, 0))
                ease.play(int(min(700, max(0, at.y()) * 0.3 + max(0, at.x()) * 0.1)))

        QTimer.singleShot(30, go)

    # states ---------------------------------------------------------------------------------------------
    def _show_link(self, problem: str = "") -> None:
        steps = ((theme.Glyph.CONNECT, "Play a match with Doty running. It reads your account from the game by "
                                       "itself."),
                 (theme.Glyph.PROFILE, "Or paste your account ID (your Friend ID in Dota) or your Dotabuff or "
                                       "OpenDota profile link in the box above, then press Enter."),
                 (theme.Glyph.SETTINGS, "In Dota, turn on Settings > Options > Social > Expose Public Match Data, "
                                        "so OpenDota can see your games."))
        if problem:
            notice = _Notice(theme.Glyph.WARN, theme.AMBER, "That's not an account ID", problem, steps[1:],
                             foot="Doty only ever looks up your own account.")
        else:
            notice = _Notice(theme.Glyph.PROFILE, theme.BRAND, "Link your Dota account",
                             "See your rank, your form and every recent game with a performance score.", steps,
                             foot="Doty only ever looks up your own account.")
        self._data = None
        self._set_view(_View([_centered(notice)], "link"))

    def _show_loading(self) -> None:
        self._set_view(_loading_view())

    def _busy(self, on: bool) -> None:
        set_button(self.refresh_btn, "Loading" if on else "Refresh", theme.Glyph.REFRESH)
        self.refresh_btn.setEnabled(not on)

    def _note(self, text: str = "", color: str = theme.MUTED) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; font-size: 9.5pt;")
        self.status.setVisible(bool(text))

    # what the window calls ------------------------------------------------------------------------------
    def refresh(self, st: dict | None) -> None:
        """Learn your account from the game data, once."""
        acc = (st or {}).get("account_id") or 0
        if acc and acc != self.ctl.cfg.app.account_id:
            from ..config import save_config

            self.ctl.cfg.app.account_id = acc
            try:
                save_config(self.ctl.cfg)
            except OSError:
                pass
            self.account.setText(str(acc))
            if self.isVisible():
                self.load()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit()
        self.svc.ensure_loaded()
        acc = parse_account(self.account.text())
        if acc and acc != self._shown:
            self.load()
        elif self._pending_play:
            self._play()

    def load(self) -> None:
        acc = parse_account(self.account.text())
        if not acc:
            self._shown = 0
            self._busy(False)
            self._note()
            self._show_link("That doesn't look like a Dota account ID. Paste your Dotabuff or OpenDota profile "
                            "link, or the number in it.")
            return
        self._shown = acc
        if acc != self.ctl.cfg.app.account_id:  # (yours: remembered, and the Learn tab reads your rank from it)
            from ..config import save_config

            self.ctl.cfg.app.account_id = acc
            try:
                save_config(self.ctl.cfg)
            except OSError:
                pass
        self._busy(True)
        if self._data is None or self._data[0] != acc:
            self._note()
            self._show_loading()
        else:
            self._note("Checking OpenDota for new games…")
        def work(s, acc=acc):
            try:
                return acc, Profile(s).player(acc), Profile(s).recent(acc)
            except Exception as exc:  # (with its account: a slow old request's error mustn't hide the new one)
                return acc, exc, None

        self.svc.run("profile", work)

    # results --------------------------------------------------------------------------------------------
    def _on_result(self, name: str, value) -> None:
        if name == "profile_avatar":
            self._on_avatar(value)
            return
        if name == "rank_art":
            for medal in self.findChildren(_Medal):
                medal.update()  # (the real medal is in the cache now)
            return
        if name != "profile":
            return
        if isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], Exception):
            if value[0] != self._shown:
                return  # (an earlier account's request failing: not this one)
            value = value[1]
        if isinstance(value, Exception):
            self._busy(False)
            if self._data is not None and self._data[0] == self._shown:
                self._note(f"Couldn't refresh from OpenDota ({value}). These are the games Doty loaded last.",
                           theme.AMBER)
                return
            self._note()
            retry = button("Try again", "ghost", theme.Glyph.REFRESH)
            retry.clicked.connect(self.load)
            self._set_view(_View([_centered(_Notice(
                theme.Glyph.WARN, theme.AMBER, "Couldn't reach OpenDota",
                f"Your games didn't load ({value}). OpenDota may be busy, or you're offline.", action=retry))],
                "error"))
            return
        acc, player, games = value
        if acc != self._shown:
            return  # an older request, for an account you've since changed
        self._busy(False)
        new = (acc, player or {}, list(games or []))
        if self._data == new and self._view is not None and self._view.kind in ("data", "empty"):
            self._note("No new games since the last check.")  # (nothing to redraw)
            return
        self._note()
        self._data = new
        self._render(animate=True)

    def _on_stats_ready(self) -> None:
        """Hero names and portraits arrived after your games did: draw them again (no animation)."""
        if self._data is not None and self.svc.stats.loaded and not self._drawn_with_stats:
            self._render(animate=False)

    def _render(self, animate: bool) -> None:
        acc, player, games = self._data
        s = self.svc.stats
        self._drawn_with_stats = bool(s.loaded)
        header = _PlayerHeader(acc, player, games)
        tier = int(player.get("rank_tier") or 0)
        if tier and _rank_art(tier) is None and art_enabled():
            self.svc.run("rank_art", lambda _s, t=tier: fetch_rank_art(t))  # (the game's medal, then a repaint)
        if not games:
            parts = [header, _centered(_Notice(
                theme.Glyph.INFO, theme.BLUE, "No public games yet",
                "OpenDota has no games for this account. Turn on Expose Public Match Data in Dota "
                "(Settings > Options > Social), play a game, and refresh in a few minutes."))]
            self._set_view(_View(parts, "empty"), animate=False)
        else:
            parts = [header, _Split(_FormCard(s, games), _MostPlayedCard(s, games)), _GamesCard(s, games)]
            self._set_view(_View(parts, "data"), animate=animate)
        self._apply_avatar(acc, player, header)

    def _apply_avatar(self, acc: int, player: dict, header: _PlayerHeader) -> None:
        img = self._avatars.get(acc) or _cached_avatar(acc)
        if img is not None:
            self._avatars[acc] = img
            header.avatar.set_image(img)
            return
        prof = player.get("profile") or {}
        url = str(prof.get("avatarfull") or prof.get("avatarmedium") or "")
        if _avatar_url_ok(url) and art_enabled() and acc not in self._avatar_asked:
            self._avatar_asked.add(acc)  # (once a session: offline, your initials stay)
            self.svc.run("profile_avatar", lambda _s, a=acc, u=url: _fetch_avatar(a, u))

    def _on_avatar(self, value) -> None:
        if isinstance(value, Exception) or not isinstance(value, tuple):
            return  # your initials stay
        acc, img = value
        if img is None:
            return
        self._avatars[acc] = img
        view = self._view
        if view is not None and self._data is not None and self._data[0] == acc:
            for part in view.parts:
                if isinstance(part, _PlayerHeader):
                    part.avatar.set_image(img)
