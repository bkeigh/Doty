"""The Draft and Heroes pages: public hero statistics, usable with or without the AI coach.

- **Draft:** both teams as portraits, counter picks (cores and supports), bans worth making, items to
  plan for, and a matchup grid. Picks come from what the coach reads off the hero-selection screen,
  or you type them.
- **Heroes:** every hero in a grid that fills the window, with live search and filter chips (attribute,
  melee or ranged, roles) and each hero's win rate at your rank; pick one for its build by game phase, its
  spells (with BKB and dispel notes), talents, matchups, win rate at every rank and what to buy against it.

Everything is hero-level public data (OpenDota, dotaconstants), loaded once in the background.
Nothing here looks up the players in your lobby.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QRadialGradient, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QCompleter, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..i18n import tr
from ..knowledge import counters, draft
from ..knowledge.stats import ATTR_NAMES, BRACKETS, Stats
from . import theme
from .art import Art, art
from .motion import NORMAL, Shimmer, fade_in
from .visuals import HERO_MIME, AbilityIcon, HeroPortrait, HeroTile, ItemIcon, drag_origin
from .widgets import Card, FlowLayout, Tag, button, caps_font, fit_width, label

PHASES = [("start_game_items", "Starting items"), ("early_game_items", "Early game"),
          ("mid_game_items", "Mid game"), ("late_game_items", "Late game")]
SKIP_ITEMS = {"tango", "flask", "clarity", "enchanted_mango", "faerie_fire", "ward_observer", "ward_sentry",
              "smoke_of_deceit", "dust", "tpscroll", "blood_grenade", "ward_dispenser"}
ATTR_ORDER = ["str", "agi", "int", "all"]


class StatsService(QObject):
    """Loads the statistics once, off the GUI thread, and runs lookups in the background."""

    ready = Signal()
    result = Signal(str, object)  # (request name, value)

    RETRY_S = 30.0  # a load that brought nothing (offline, OpenDota busy) is tried again after this long

    def __init__(self) -> None:
        super().__init__()
        self.stats = Stats()
        self._started = False
        self._loading = False
        self._tried = -1e9

    def ensure_loaded(self) -> None:
        """Load the statistics once, in the background; if that brought nothing, try again (not more often
        than every RETRY_S) the next time a page asks."""
        if self._loading or (self._started and (self.stats.loaded or time.monotonic() - self._tried < self.RETRY_S)):
            return
        self._started, self._loading, self._tried = True, True, time.monotonic()

        def work() -> None:
            try:
                self.stats.load()
            finally:
                self._loading = False
                self.ready.emit()

        threading.Thread(target=work, name="doty-stats", daemon=True).start()

    def run(self, name: str, fn) -> None:
        def work() -> None:
            try:
                value = fn(self.stats)
            except Exception as exc:  # a lookup must never take the window down
                value = exc
            self.result.emit(name, value)

        threading.Thread(target=work, name=f"doty-stats-{name}", daemon=True).start()


# --------------------------------------------------------------------------- shared pieces


class _Page(QWidget):
    """A page whose content stays a readable width on very wide windows (`max_width`: a page of pictures can
    use more)."""

    max_width = 1560

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        fit_width(self.layout(), self.width(), self.max_width)


class PageStack(QWidget):
    """Pages on top of each other, as tall as the one showing. (QStackedWidget measures every page, even
    hidden ones, which stretched a hero's page to the height of the whole hero grid.)"""

    currentChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._pages: list[QWidget] = []
        self._index = -1

    def addWidget(self, w: QWidget) -> int:  # noqa: N802
        self._pages.append(w)
        self._lay.addWidget(w)
        if self._index < 0:
            self._index = 0
        else:
            w.hide()  # hidden widgets take no room in the layout
        return len(self._pages) - 1

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if index == self._index or not 0 <= index < len(self._pages):
            return
        for i, page in enumerate(self._pages):
            page.setVisible(i == index)
        self._index = index
        self.currentChanged.emit(index)

    def currentIndex(self) -> int:  # noqa: N802
        return self._index

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        return self._pages[self._index] if 0 <= self._index < len(self._pages) else None

    def count(self) -> int:
        return len(self._pages)

    def widget(self, index: int) -> QWidget:
        return self._pages[index]


def hero_completer(names: list[str], parent: QWidget) -> QCompleter:
    """Autocomplete for hero names, in the app's dark style (matches anywhere in the name)."""
    c = QCompleter(names, parent)
    c.setCaseSensitivity(Qt.CaseInsensitive)
    c.setFilterMode(Qt.MatchContains)
    c.popup().setObjectName("completer")
    return c


def _labelled(caption: str, widget: QWidget) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    h.addWidget(label(caption, "muted"))
    h.addWidget(widget)
    return w


def _page_shell(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout, QVBoxLayout, QHBoxLayout]:
    """(page, outer layout, scrolling body, header controls row)."""
    page = _Page()
    page.setObjectName("page")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(34, 28, 34, 20)
    outer.setSpacing(14)
    top = QHBoxLayout()
    head = QVBoxLayout()
    head.setSpacing(2)
    head.addWidget(label(title, "h1"))
    head.addWidget(label(subtitle, "muted", wrap=True))
    top.addLayout(head, 1)
    controls = QHBoxLayout()
    controls.setSpacing(8)
    top.addLayout(controls)
    top.setAlignment(controls, Qt.AlignTop)
    outer.addLayout(top)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    inner = QWidget()
    body = QVBoxLayout(inner)
    body.setContentsMargins(0, 0, 10, 10)
    body.setSpacing(14)
    scroll.setWidget(inner)
    outer.addWidget(scroll, 1)
    return page, outer, body, controls


def _section(title: str, hint: str = "") -> tuple[Card, QVBoxLayout]:
    card = Card(margins=(20, 16, 20, 16), spacing=10)
    row = QHBoxLayout()
    row.addWidget(label(title.upper(), "section"))
    row.addStretch(1)
    if hint:
        h = label(hint, "faint")
        h.setStyleSheet("font-size: 8.5pt;")
        row.addWidget(h)
    card.body.addLayout(row)
    return card, card.body


def _pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "–"
    return f"{v * 100:+.1f}" if signed else f"{v * 100:.1f}%"


def _wr_color(wr: float) -> str:
    """A hero's win rate: 48-52% is ordinary (neutral); only real gaps get green or red."""
    return theme.GREEN if wr >= 0.52 else theme.RED if wr <= 0.48 else theme.TEXT_2


def _edge_color(v: float | None) -> str:
    if v is None:
        return theme.MUTED
    if v > 0.004:
        return theme.GREEN
    if v < -0.004:
        return theme.RED
    return theme.TEXT_2


def _rich(text: str, name: str | None = None) -> QLabel:
    lab = label(text, name)
    lab.setTextFormat(Qt.RichText)
    return lab


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()  # gone at once, even before Qt gets round to deleting it
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


def _fill(lay: QVBoxLayout, widgets: list[QWidget]) -> None:
    """Replace a section's content (everything under its title row)."""
    while lay.count() > 1:
        item = lay.takeAt(1)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())
    for w in widgets:
        lay.addWidget(w)
    lay.addStretch(1)


def _flow(widgets: list[QWidget], spacing: int = 12) -> QWidget:
    holder = QWidget()
    fl = FlowLayout(holder, spacing)
    for w in widgets:
        fl.addWidget(w)
    return holder


def _pill(text: str, color: str) -> QLabel:
    c = QColor(color)
    lab = QLabel(text)
    lab.setStyleSheet(f"color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},0.13); border-radius: 4px;"
                      " padding: 2px 8px; font-size: 8.5pt; font-weight: 700;")
    return lab


def _item_row(stats: Stats, key: str, detail: str = "", detail_color: str = theme.MUTED) -> QWidget:
    """An item's icon, name and a short note (its cost, or who it's for)."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(9)
    icon = ItemIcon(40, 29)
    icon.set_item(stats, key)
    h.addWidget(icon)
    texts = QVBoxLayout()
    texts.setSpacing(0)
    name = label(stats.item_name(key))
    name.setStyleSheet("font-weight: 600; font-size: 9.5pt;")
    texts.addWidget(name)
    if detail:
        d = label(detail)
        d.setStyleSheet(f"color: {detail_color}; font-size: 8.5pt;")
        texts.addWidget(d)
    h.addLayout(texts, 1)
    w.setToolTip(icon.toolTip())
    return w


def _loading(rows: int = 3) -> list[QWidget]:
    return [Shimmer(rows)]


def _bracket_combo(ctl) -> QComboBox:
    box = QComboBox()
    box.setCursor(Qt.PointingHandCursor)
    box.setMinimumWidth(150)
    for k, v in BRACKETS.items():
        box.addItem(v, k)
    box.setCurrentIndex(max(0, box.findData(ctl.cfg.coach.bracket)))
    box.setToolTip("Your rank: statistics come from games at this rank")
    return box


def follow_rank(box: QComboBox, ctl) -> None:
    """Show the rank saved in Settings (changing it there must reach the Draft and Heroes pages)."""
    want = box.findData(ctl.cfg.coach.bracket)
    if want >= 0 and want != box.currentIndex():
        box.setCurrentIndex(want)  # its own handler recomputes the page


# --------------------------------------------------------------------------- Draft


class DraftSlot(QWidget):
    """One pick: the hero's portrait with its name on it. Click to type a hero, or drop one here (from the
    hero pool, a suggestion, or another slot to swap them); right-click to clear, or drag it back to the pool."""

    changed = Signal()
    dropped = Signal(object)  # a hero was dropped here (the page then takes it out of any other slot)

    def __init__(self, placeholder: str, tone: str, w: int = 136, h: int = 76, banned: bool = False) -> None:
        super().__init__()
        self.tone, self.placeholder, self.banned = tone, placeholder, banned
        self.setFixedSize(w, h)
        self.setAcceptDrops(True)
        self.portrait = HeroPortrait(w, h, hover=True)
        self.portrait.setParent(self)
        self.portrait.caption = True
        self.portrait.strike = banned
        self.portrait.set_dim(banned)
        self.portrait.set_empty("+  " + placeholder)
        self.portrait.clicked.connect(self.edit)
        self.portrait.drag_origin = self  # dragging a filled slot moves its hero
        self.portrait.setToolTip("Click to type a hero, or drop one here · right-click to clear")
        self.box = QLineEdit(self)
        self.box.setPlaceholderText(placeholder)
        self.box.setGeometry(4, h - 34, w - 8, 30)
        self.box.hide()
        self.box.editingFinished.connect(self._finish)
        self.box.installEventFilter(self)
        self._before = ""

    def set_width(self, w: int) -> None:
        """Narrower on a narrow window (the five slots of a team keep one row)."""
        h = round(self.height() * w / max(1, self.width()))
        if w == self.width():
            return
        self.setFixedSize(w, h)
        self.portrait.setFixedSize(w, h)
        self.box.setGeometry(4, h - 34, w - 8, 30)

    def edit(self) -> None:
        self._before = self.box.text()
        if self.portrait._empty:
            self.portrait.set_empty("")  # the "+ Ally" text would sit right above the box
        self.box.show()
        self.box.raise_()
        self.box.setFocus()
        self.box.selectAll()

    def _finish(self) -> None:
        if not self.box.isHidden():
            self.box.hide()
            c = self.box.completer()
            text = self.box.text().strip()
            if c is not None and text and text.lower() not in (n.lower() for n in c.model().stringList()):
                c.setCompletionPrefix(text)
                if c.completionCount():
                    self.box.setText(c.currentCompletion())  # Enter on "Ax" means Axe
            if self.box.text() != self._before:
                self.changed.emit()
            elif not self.box.text().strip():
                self.portrait.set_empty("+  " + self.placeholder)

    def set_you(self, on: bool) -> None:
        self.portrait.set_badge("YOU" if on else "", theme.NEUTRAL)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.box and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.box.setText(self._before)
            self.box.hide()
            if not self._before.strip():
                self.portrait.set_empty("+  " + self.placeholder)
            return True
        return super().eventFilter(obj, event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self.box.text():
            self.box.clear()
            self.changed.emit()

    # drag and drop ---------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(HERO_MIME) and drag_origin() is not self:
            event.acceptProposedAction()
            self.portrait.set_badge("DROP", theme.BRAND)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.portrait.set_badge("")

    def dropEvent(self, event) -> None:  # noqa: N802
        self.portrait.set_badge("")
        name = bytes(event.mimeData().data(HERO_MIME)).decode("utf-8").strip()
        if not name:
            return
        origin = drag_origin()
        if isinstance(origin, DraftSlot) and origin is not self:
            origin.box.setText(self.box.text())  # dropped from another slot: the two swap
            origin.box.setCursorPosition(0)
        self.box.setText(name)
        self.box.setCursorPosition(0)
        event.acceptProposedAction()
        self.dropped.emit(self)
        self.changed.emit()

    def show_hero(self, stats: Stats) -> None:
        text = self.box.text().strip()
        hid = stats.find(text) if stats.loaded and text else None
        self.portrait.drag_text = stats.name(hid) if hid else ""
        if hid:
            if self.portrait.hero_id != hid or self.portrait._empty:
                self.portrait.set_hero(stats, hid)
            self.portrait.set_tone(None if self.banned else self.tone)
            self.portrait.set_dim(self.banned)
        else:
            self.portrait.hero_id = None
            self.portrait.set_empty(f"“{text}”?" if text else "+  " + self.placeholder)


class ModeSwitch(QWidget):
    """Draft | All heroes: the two halves of the Heroes tab."""

    changed = Signal(bool)  # True = Draft

    def __init__(self, draft: bool) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self.b_draft, self.b_all = QPushButton("Draft"), QPushButton("All heroes")
        for b, tip in ((self.b_draft, "Counter picks and bans for the game you're drafting"),
                       (self.b_all, "Every hero: builds, abilities and what beats it")):
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            self._group.addButton(b)
            lay.addWidget(b)
        (self.b_draft if draft else self.b_all).setChecked(True)
        self.b_draft.clicked.connect(lambda: self.changed.emit(True))
        self.b_all.clicked.connect(lambda: self.changed.emit(False))

    def set_mode(self, draft: bool) -> None:
        (self.b_draft if draft else self.b_all).setChecked(True)


ATTACK_TYPES = (("", "Melee and ranged"), ("Melee", "Melee"), ("Ranged", "Ranged"))
ROLES = ("Carry", "Support", "Nuker", "Disabler", "Initiator", "Durable", "Escape", "Pusher")


def hero_matches(s: Stats, hid: int, query: str = "", attr: str = "", role: str = "", attack: str = "") -> bool:
    """The Heroes tab's filters: part of the name, primary attribute, role, melee or ranged."""
    h = s.heroes.get(hid, {})
    return ((not query or query in s.name(hid).lower()) and (not attr or h.get("primary_attr") == attr)
            and (not role or role in h.get("roles", [])) and (not attack or h.get("attack_type") == attack))


class HeroPool(QWidget):
    """Every hero, small, for drafting by hand: drag one onto a slot (double-click adds it to your team).
    Filter by name, attribute, role and melee/ranged; sort by name, win rate at your rank, or how each does
    against the enemy picks so far. Heroes already picked or banned are dimmed. Drag a hero off a slot and
    drop it here to clear the slot."""

    picked = Signal(str)  # double-click: add to your team
    cleared = Signal()  # a slot was dragged back here and emptied

    SIZE = (84, 47)

    def __init__(self, svc: "StatsService", bracket, enemies, taken) -> None:
        super().__init__()
        self.svc = svc
        self._bracket, self._enemies, self._taken = bracket, enemies, taken
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        bar_w = QWidget()  # wraps to a second line on a narrow window
        bar = FlowLayout(bar_w, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter heroes…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(170)
        self.search.setAcceptDrops(False)  # a slot dragged back over it clears the slot (the pool takes it)
        self.search.textEdited.connect(lambda _t: self.refresh())
        bar.addWidget(self.search)
        self.attr = QComboBox()
        self.attr.addItem("Any attribute", "")
        for a in ATTR_ORDER:
            self.attr.addItem(ATTR_NAMES.get(a, a), a)
        self.role = QComboBox()
        self.role.addItem("Any role", "")
        for r in ROLES:
            self.role.addItem(r, r)
        self.attack = QComboBox()
        for value, text in ATTACK_TYPES:
            self.attack.addItem(text, value)
        self.sort = QComboBox()
        for value, text in (("vs", "Best vs their picks"), ("wr", "Win rate"), ("name", "A to Z")):
            self.sort.addItem(text, value)
        for box in (self.attr, self.role, self.attack, self.sort):
            box.setCursor(Qt.PointingHandCursor)
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)  # shrinks on a narrow window
            box.setMinimumContentsLength(10)
            box.currentIndexChanged.connect(lambda _i: self.refresh())
            bar.addWidget(box)
        lay.addWidget(bar_w)
        self.holder = QWidget()
        self.holder_lay = QVBoxLayout(self.holder)
        self.holder_lay.setContentsMargins(0, 0, 8, 0)
        self.holder_lay.addWidget(Shimmer(2))
        # A window onto the pool, about four rows tall, so the slots above stay in view while you drag
        # (and the suggestions below aren't pushed off the page).
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(self.SIZE[1] * 4 + 6 * 3 + 4)
        self.scroll.setWidget(self.holder)
        lay.addWidget(self.scroll)
        self.pics: dict[int, HeroPortrait] = {}
        self._fetching: set[int] = set()  # enemies whose matchups are loading in the background
        svc.result.connect(self._on_result)

    def _build(self) -> None:
        s = self.svc.stats
        for hid in s.heroes:
            pic = HeroPortrait(*self.SIZE, hover=True)
            pic.set_hero(s, hid)
            pic.caption = True
            pic.drag_text = s.name(hid)
            pic.double_clicked.connect(lambda h=hid: self._double_clicked(h))
            self.pics[hid] = pic

    def _double_clicked(self, hid: int) -> None:
        if hid not in set(self._taken()):  # a banned or picked hero stays where it is
            self.picked.emit(self.svc.stats.name(hid))

    def _on_result(self, name: str, _value) -> None:
        if name == "pool-matchups":
            self._fetching.clear()
            self.refresh()

    def refresh(self) -> None:
        """Filter, sort and dim (call when the picks, the rank or a filter change)."""
        s = self.svc.stats
        if not s.loaded:
            return
        if not self.pics:
            self._build()
        bracket, enemies, taken = int(self._bracket() or 0), self._enemies(), set(self._taken())
        query = self.search.text().strip().lower()
        shown = [h for h in self.pics if hero_matches(s, h, query, self.attr.currentData(), self.role.currentData(),
                                                      self.attack.currentData())]
        mode = self.sort.currentData()
        # Matchups not loaded yet come from the network: fetch them in the background (never here, on
        # the window's thread) and sort by win rate until they arrive.
        ready = [e for e in enemies if s.has_matchups(e)]
        missing = [e for e in enemies if e not in ready and e not in self._fetching]
        if mode == "vs" and missing:
            self._fetching |= set(missing)
            self.svc.run("pool-matchups", lambda st, ids=tuple(missing): [st.matchups(e) for e in ids])
        if mode == "vs" and ready:
            edge = {h: sum(draft.advantage(s, h, e) or 0.0 for e in ready) for h in shown}
            shown.sort(key=lambda h: -edge[h])
        elif mode in ("vs", "wr"):  # no enemy picks yet: best at your rank instead
            edge = {}
            shown.sort(key=lambda h: -s.win_rate(h, bracket)[0])
        else:
            edge = {}
            shown.sort(key=s.name)
        for hid, pic in self.pics.items():
            pic.setParent(None)
            done = hid in taken
            pic.set_dim(done)
            pic.drag_text = "" if done else s.name(hid)
            if hid in edge:
                pic.set_badge(f"{edge[hid] * 100:+.1f}", _edge_color(edge[hid]))
            elif mode in ("vs", "wr"):
                wr = s.win_rate(hid, bracket)[0]
                pic.set_badge(f"{wr * 100:.0f}%", _wr_color(wr))
            else:
                pic.set_badge("")
            pic.setToolTip(f"{s.name(hid)}" + (tr(" · already in this draft") if done else
                                               tr(" · drag onto a slot, or double-click to add to your team")))
        _clear(self.holder_lay)
        self.holder_lay.addWidget(_flow([self.pics[h] for h in shown], 6) if shown else
                                  label("No hero matches those filters.", "faint"))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(HERO_MIME) and isinstance(drag_origin(), DraftSlot):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        origin = drag_origin()
        if isinstance(origin, DraftSlot):  # a slot dragged back to the pool: empty it
            origin.box.clear()
            event.acceptProposedAction()
            origin.changed.emit()
            self.cleared.emit()


class DraftPage(QWidget):
    open_hero = Signal(str)  # a hero's name: show its build
    summary = Signal(object)  # the top picks and the verdict, for the Coach page during hero selection

    def __init__(self, ctl, service: StatsService) -> None:
        super().__init__()
        self.ctl, self.svc = ctl, service
        self.setObjectName("page")
        page, outer, body, controls = _page_shell(
            "Draft", "Counter picks, bans and matchups from millions of public games at your rank. Picks fill in "
                     "from your screen while Doty coaches, or type them.")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)
        self.mode = ModeSwitch(True)
        outer.insertWidget(0, self.mode, 0, Qt.AlignLeft)
        self.rank = _bracket_combo(ctl)
        self.rank.currentIndexChanged.connect(self._rank_changed)
        self.role = QComboBox()
        self.role.setCursor(Qt.PointingHandCursor)
        self.role.setMinimumWidth(140)
        for value, text in (("any", "Cores and supports"), ("core", "Cores"), ("support", "Supports")):
            self.role.addItem(text, value)
        self.role.currentIndexChanged.connect(lambda _i: self._recompute())
        clear = button("Clear", "ghost")
        clear.clicked.connect(self._clear_picks)
        controls.addWidget(_labelled("Rank", self.rank))
        controls.addWidget(_labelled("Show", self.role))
        controls.addWidget(clear)

        teams, tlay = _section("Teams", "")
        self.teams_card = teams
        page.findChild(QScrollArea).viewport().installEventFilter(self)  # refit the slots as the width changes
        self.source = label("", "faint")
        tlay.itemAt(0).layout().insertWidget(1, self.source)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(12)
        self.ally_slots = [DraftSlot("You" if i == 0 else "Ally", "good") for i in range(5)]
        self.enemy_slots = [DraftSlot("Enemy", "bad") for _ in range(5)]
        self.ally_inputs = [s.box for s in self.ally_slots]
        self.enemy_inputs = [s.box for s in self.enemy_slots]
        for slot in self.ally_slots + self.enemy_slots:
            slot.changed.connect(self._typed_change)
            slot.dropped.connect(self._one_slot_each)
        for r, (title, color, slots) in enumerate((("YOUR TEAM", theme.GREEN, self.ally_slots),
                                                   ("ENEMY TEAM", theme.RED, self.enemy_slots))):
            cap = label(title, "gold")
            cap.setStyleSheet(f"color: {color};")
            cap.setMinimumWidth(92)
            grid.addWidget(cap, r, 0, Qt.AlignVCenter)
            for c, w in enumerate(slots):
                grid.addWidget(w, r, c + 1)
        grid.addWidget(label("BANS", "gold"), 2, 0, Qt.AlignVCenter)
        bans_row = QHBoxLayout()
        bans_row.setSpacing(10)  # in line with the pick columns
        self.ban_slots = [DraftSlot("Ban", "", 136, 52, banned=True) for _ in range(5)]
        self.ban_inputs = [b.box for b in self.ban_slots]
        for slot in self.ban_slots:
            slot.changed.connect(self._typed_change)
            slot.dropped.connect(self._one_slot_each)
            bans_row.addWidget(slot)
        bans_row.addStretch(1)
        grid.addLayout(bans_row, 2, 1, 1, 5)
        grid.setColumnStretch(6, 1)
        tlay.addLayout(grid)
        body.addWidget(teams)

        pool_card, pool_lay = _section("Hero pool", "drag a hero onto a slot (or between slots to swap them) · "
                                                    "double-click adds it to your team")
        self.pool = HeroPool(service, lambda: self.rank.currentData(), lambda: self._ids(self.enemy_inputs),
                             lambda: self._ids(self.ally_inputs + self.enemy_inputs + self.ban_inputs))
        self.pool.picked.connect(self._add_to_team)
        pool_lay.addWidget(self.pool)
        body.addWidget(pool_card)

        picks, self.picks_lay = _section("Suggested picks", "+7.5 ≈ 7 more wins per 100 games against this lineup"
                                                            " · click one for its build")
        body.addWidget(picks)
        picks.installEventFilter(self)
        row = QHBoxLayout()
        row.setSpacing(14)
        bans, self.bans_lay = _section("Ban these", "strongest at your rank")
        row.addWidget(bans, 1)
        bans.installEventFilter(self)
        buy, self.buy_lay = _section("Plan to buy", "against their lineup")
        row.addWidget(buy, 1)
        body.addLayout(row)
        grid_card, self.grid_lay = _section("Matchups", "how each of your heroes does against each of theirs")
        body.addWidget(grid_card)
        note = label("Hero statistics only: Doty never looks up the players in your lobby.", "faint")
        note.setStyleSheet("font-size: 8.5pt;")
        body.addWidget(note)
        body.addStretch(1)

        self.pick_tiles: list[HeroTile] = []
        self.ban_tiles: list[HeroTile] = []
        self._you_known = False  # is the first ally slot really you?
        self._auto: dict | None = None  # the last picks read from the screen
        self._typed = False
        self._pending = 0
        service.ready.connect(self._on_ready)
        service.result.connect(self._on_result)
        self._show_loading()

    # inputs ---------------------------------------------------------------------
    def _fit_slots(self) -> None:
        """The draft slots shrink a little on a narrow window, so a team's five stay on one row."""
        scroll = self.findChild(QScrollArea)
        if scroll is None or getattr(self, "teams_card", None) is None:
            return
        # Measure what's visible, not the card (the slots themselves would hold the card wide).
        avail = scroll.viewport().width() - 10 - 40 - 92 - 5 * 10  # body margin, card padding, caption, gaps
        w = max(100, min(136, avail // 5))
        for slot in self.ally_slots + self.enemy_slots + self.ban_slots:
            slot.set_width(w)

    def _fit_tiles(self) -> None:
        """Five suggestion tiles fill their card's width (up to a comfortable size), and the
        suggested bans sit three to a row."""
        self._fit_slots()
        for lay, tiles, per_row, lo, hi in ((self.picks_lay, self.pick_tiles, 5, 110, 220),
                                            (self.bans_lay, self.ban_tiles, 3, 96, 190)):
            card = lay.parentWidget()
            if card is None or not tiles:
                continue
            m = lay.contentsMargins()
            avail = card.contentsRect().width() - m.left() - m.right() - 2
            w = max(lo, min(hi, (avail - (per_row - 1) * 12) // per_row))
            for t in tiles:
                if t.portrait.width() != w:
                    t.set_width(w)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Resize:
            self._fit_tiles()  # e.g. the scroll bar appeared and the cards got narrower
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_tiles()

    def _attach_completers(self) -> None:
        names = self.svc.stats.hero_names()
        for slot in self.ally_slots + self.enemy_slots + self.ban_slots:
            c = hero_completer(names, slot.box)
            c.activated.connect(lambda _text, sl=slot: sl._finish())
            slot.box.setCompleter(c)

    def _typed_change(self) -> None:
        self._typed = True
        self._you_known = True  # you typed the first slot yourself
        self._recompute()

    def _one_slot_each(self, slot: "DraftSlot") -> None:
        """A hero dropped on a slot leaves any other slot it was in (a hero is in a draft once)."""
        hid = self.svc.stats.find(slot.box.text().strip())
        if not hid:
            return
        for other in self.ally_slots + self.enemy_slots + self.ban_slots:
            if other is not slot and self.svc.stats.find(other.box.text().strip()) == hid:
                other.box.clear()

    def _add_to_team(self, name: str) -> None:
        """Double-click in the hero pool: the hero goes into your team's first empty slot."""
        if self.svc.stats.find(name) in self._ids(self.ally_inputs + self.enemy_inputs + self.ban_inputs):
            return  # already in this draft
        box = next((b for b in self.ally_inputs if not b.text().strip()), None)
        if box is None:
            return
        box.setText(name)
        box.setCursorPosition(0)
        self._typed_change()

    def new_draft(self) -> None:
        """A new hero selection started: last game's picks (and your corrections) are done with."""
        self._clear_picks()

    def _clear_picks(self) -> None:
        for box in self.ally_inputs + self.enemy_inputs + self.ban_inputs:
            box.clear()
        self._typed, self._auto, self._you_known = False, None, False
        self.source.setText("")
        self._recompute()

    def _rank_changed(self) -> None:
        from ..config import save_config

        self.ctl.cfg.coach.bracket = int(self.rank.currentData())
        try:
            save_config(self.ctl.cfg)
        except OSError:
            pass
        self._recompute()

    def _ids(self, boxes) -> list[int]:
        return [h for h in (self.svc.stats.find(b.text().strip()) for b in boxes) if h]

    # status from the engine ------------------------------------------------------
    def refresh(self, st: dict | None) -> None:
        """Fill the picks from what the coach read off the screen, unless you typed your own."""
        follow_rank(self.rank, self.ctl)
        picks = (st or {}).get("draft_read")
        if not picks or picks == self._auto or self._typed or picks.get("you") not in ("radiant", "dire"):
            return
        self._auto = picks
        you = picks["you"]
        them = "dire" if you == "radiant" else "radiant"
        allies = list(picks.get(you, []))
        mine = (st or {}).get("hero") or ""
        self._you_known = bool(mine) and mine in allies
        if self._you_known:
            allies.remove(mine)
            allies.insert(0, mine)  # your own pick goes in the first slot
        for boxes, names in ((self.ally_inputs, allies), (self.enemy_inputs, picks.get(them, []))):
            for box, name in zip(boxes, list(names) + [""] * 5):
                box.setText(name)
                box.setCursorPosition(0)
        for box, name in zip(self.ban_inputs, list(picks.get("bans", [])) + [""] * 5):
            box.setText(name)
        self.source.setText("· read from your screen")
        self._recompute()

    # compute ------------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.svc.ensure_loaded()

    def _on_ready(self) -> None:
        if self.svc.stats.loaded:
            self._attach_completers()
        self._recompute()

    def _show_loading(self) -> None:
        for lay, rows in ((self.picks_lay, 3), (self.bans_lay, 2), (self.buy_lay, 3), (self.grid_lay, 2)):
            _fill(lay, _loading(rows))

    _body = staticmethod(_fill)  # (the Profile page fills its sections the same way)

    def _recompute(self) -> None:
        stats = self.svc.stats
        if not stats.loaded:
            if self.svc._started and not stats.heroes and stats.error:
                _fill(self.picks_lay, [label(stats.error, "faint")])
            return
        for slot in self.ally_slots + self.enemy_slots + self.ban_slots:
            slot.show_hero(stats)
        self.pool.refresh()
        first = self.ally_slots[0]
        first.set_you(self._you_known and first.portrait.hero_id is not None)
        allies, enemies = self._ids(self.ally_inputs), self._ids(self.enemy_inputs)
        banned = self._ids(self.ban_inputs)
        bracket, role = int(self.rank.currentData()), self.role.currentData()
        self._pending += 1
        token = self._pending

        def work(s: Stats):
            if role == "any":
                cores = draft.suggest(s, allies, enemies, banned, bracket, "core", top=5)
                taken = banned + [p.hero_id for p in cores]
                groups = [("Cores", cores), ("Supports", draft.suggest(s, allies, enemies, taken, bracket,
                                                                       "support", top=5))]
            else:
                groups = [("", draft.suggest(s, allies, enemies, banned, bracket, role, top=10))]
            return token, (groups, draft.matrix(s, allies, enemies),
                           draft.ban_suggestions(s, bracket, allies + enemies + banned),
                           counters.counter_items([s.name(e) for e in enemies]), allies, enemies, banned)

        self.svc.run("draft", work)

    def _on_result(self, name: str, value) -> None:
        if name != "draft" or isinstance(value, Exception):
            return
        token, (groups, grid, bans, buys, allies, enemies, banned) = value
        if token != self._pending:
            return  # a newer request is on its way
        s = self.svc.stats
        widgets: list[QWidget] = []
        self.pick_tiles: list[HeroTile] = []
        for title, picks in groups:
            if title:
                widgets.append(label(title.upper(), "eyebrow"))
            tiles = []
            for p in picks:
                strong = [n for n, a in p.vs[:2] if a > 0]
                sub = ("vs " + ", ".join(strong)) if strong else ("" if not enemies else "no strong edge")
                edge = p.score * 100
                tip = "\n".join([s.name(p.hero_id),
                                 f"Counters them: {edge:+.1f} (about {abs(round(edge))} "
                                 f"{'more' if edge >= 0 else 'fewer'} wins per 100 games)",
                                 f"Win rate at your rank: {p.win_rate * 100:.1f}%"]
                                + [f"vs {n}: {a * 100:+.1f}" for n, a in p.vs] + ["Click for its build and counters"])
                t = HeroTile(136, 76)
                t.set(s, p.hero_id, sub, f"{edge:+.1f}", _edge_color(p.score), tip + "\nDrag onto a slot to pick it")
                t.clicked.connect(lambda n=s.name(p.hero_id): self.open_hero.emit(n))
                t.portrait.drag_text = s.name(p.hero_id)
                tiles.append(t)
            self.pick_tiles += tiles
            widgets.append(_flow(tiles))
        if not enemies:  # said once, not under every tile
            widgets.insert(0, label("Strongest at your rank right now. Add enemy picks to see counters.", "faint"))
        _fill(self.picks_lay, widgets)
        self._fit_tiles()

        tiles = []
        bans_over = len(banned) >= 5 or len(allies) + len(enemies) >= 6
        for h, w, pr in ([] if bans_over else bans):  # once the bans are done, no more ban advice
            t = HeroTile(104, 58)
            t.set(s, h, f"{pr * 100:.0f}% pick rate", f"{w * 100:.1f}%", theme.AMBER,
                  f"{s.name(h)}: wins {w * 100:.1f}% of games at your rank. Click for its build; drag onto a "
                  "ban slot to ban it.")
            t.clicked.connect(lambda n=s.name(h): self.open_hero.emit(n))
            t.portrait.drag_text = s.name(h)
            tiles.append(t)
        self.ban_tiles = tiles
        _fill(self.bans_lay, [_flow(tiles, 12)] if tiles else
              [label("Bans are done. What to buy and the matchups are what matter now." if bans_over else
                     "No ban data yet.", "faint", wrap=True)])
        self._fit_tiles()

        if buys:
            _fill(self.buy_lay, [_item_row(s, item, "vs " + ", ".join(who), theme.TEXT_2)
                                 for item, _why, who in buys[:7]])
        else:
            _fill(self.buy_lay, [label("Add enemy heroes to see what to plan for.", "faint", wrap=True)])

        self.summary.emit({
            "groups": [(title, [(p.hero_id, f"{p.score * 100:+.1f}", _edge_color(p.score)) for p in picks])
                       for title, picks in groups],
            "verdict": _verdict(s, allies, enemies, grid).text() if allies and enemies else ""})
        if allies and enemies:
            verdict = _verdict(s, allies, enemies, grid)
            holder = QWidget()
            g = QGridLayout(holder)
            g.setContentsMargins(0, 4, 0, 0)
            g.setSpacing(6)
            for j, e in enumerate(enemies):
                pic = HeroPortrait(76, 42)
                pic.set_hero(s, e)
                pic.set_tone("bad")
                g.addWidget(pic, 0, j + 1)
            for i, a in enumerate(allies):
                pic = HeroPortrait(76, 42)
                pic.set_hero(s, a)
                pic.set_tone("good")
                g.addWidget(pic, i + 1, 0)
                for j, e in enumerate(enemies):
                    cell = _cell(grid[i][j])
                    v = grid[i][j]
                    cell.setToolTip(f"{s.name(a)} vs {s.name(e)}: "
                                    + (tr("not enough games") if v is None else
                                       tr("{edge} (wins about {n} more of every 100 games than usual)" if v >= 0 else
                                          "{edge} (loses about {n} more of every 100 games than usual)",
                                          edge=f"{v * 100:+.1f}", n=f"{abs(v * 100):.1f}")))
                    g.addWidget(cell, i + 1, j + 1)
                row_total = sum(v for v in grid[i] if v is not None)
                total = _rich(f'<b style="color:{_edge_color(row_total)}">{row_total * 100:+.1f}</b>')
                total.setToolTip(tr("{hero} against their whole lineup", hero=s.name(a)))
                g.addWidget(total, i + 1, len(enemies) + 1, Qt.AlignCenter)
            g.addWidget(label("TOTAL", "eyebrow"), 0, len(enemies) + 1, Qt.AlignCenter)
            g.setColumnMinimumWidth(len(enemies) + 1, 60)
            g.setColumnStretch(len(enemies) + 2, 1)
            _fill(self.grid_lay, [verdict, holder])
        else:
            _fill(self.grid_lay, [label("Add heroes on both teams to compare every pairing.", "faint")])


def _verdict(s: Stats, allies: list[int], enemies: list[int], grid) -> QLabel:
    """One sentence on the whole matchup: the total, the best pairing and the one to watch."""
    pairs = [(grid[i][j], a, e) for i, a in enumerate(allies) for j, e in enumerate(enemies)
             if grid[i][j] is not None]
    if not pairs:
        return label("Not enough games between these heroes yet.", "faint")
    total = sum(round(sum(v for v in row if v is not None) * 100, 1) for row in grid) / 100  # = the TOTAL column
    best, worst = max(pairs), min(pairs)
    parts = [f'Your lineup: <b style="color:{_edge_color(total)}">{total * 100:+.1f}</b> against theirs.']
    if best[0] > 0.003:
        parts.append(f"Best: {s.name(best[1])} vs {s.name(best[2])} ({best[0] * 100:+.1f}).")
    if worst[0] < -0.003:
        parts.append(f'Watch out: <span style="color:{theme.RED}">{s.name(worst[1])} vs {s.name(worst[2])} '
                     f"({worst[0] * 100:+.1f})</span>.")
    return _rich(" ".join(parts))


def _cell(v: float | None) -> QLabel:
    lab = QLabel("·" if v is not None and abs(v) < 0.003 else _pct(v))
    lab.setAlignment(Qt.AlignCenter)
    lab.setFixedSize(76, 42)
    c = QColor(_edge_color(v))
    alpha = 0.10 + min(0.40, abs(v or 0) * 12)
    lab.setStyleSheet(f"color: {c.name()}; font-weight: 700; font-size: 10.5pt;"
                      f" background: rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f}); border-radius: 4px;")
    return lab


def _table(head: list[str], rows: list[list[QWidget]], stretch: int = 1, align_center: bool = False) -> QWidget:
    w = QWidget()
    g = QGridLayout(w)
    g.setContentsMargins(0, 2, 0, 0)
    g.setHorizontalSpacing(14)
    g.setVerticalSpacing(7)
    for c, h in enumerate(head):
        lab = label(h, "eyebrow")
        if align_center and c:
            lab.setAlignment(Qt.AlignCenter)
        g.addWidget(lab, 0, c)
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row):
            g.addWidget(cell, r, c)
    if stretch:
        g.setColumnStretch(stretch, 1)
    w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return w


# --------------------------------------------------------------------------- Heroes


# Dota's attribute colors, a little softened so they sit with the app's own green and red (which mean good and bad).
ATTR_COLORS = {"str": "#e8603f", "agi": "#7ccb4f", "int": "#4ea3f5", "all": "#c795f2"}
SORTS = (("attr", "Attribute", "Grouped by primary attribute"), ("wr", "Win rate", "Best at your rank first"),
         ("pick", "Pick rate", "Most played at your rank first"), ("name", "A–Z", "Alphabetical"))
HERO_ROLES = ("Carry", "Support", "Nuker", "Disabler", "Initiator", "Durable", "Escape", "Pusher")
MATCHUP_MIN_GAMES = 40  # fewer games than this against a hero: too noisy to call it a counter
ITEM_TILE_W = 86


class _G:
    """Segoe MDL2 glyphs this page uses (theme.Glyph holds the app-wide ones)."""

    SEARCH = "\ue721"  # magnifier
    COOLDOWN = "\ue916"  # stopwatch
    MANA = "\ue945"  # lightning
    MOVE = "\ue805"  # walking figure
    RANGE = "\ue1d2"  # target


def paint_attr(p: QPainter, r: QRectF, attr: str | None) -> None:
    """A hero attribute as a small disc in its color (Universal: thirds of the other three)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    if attr == "all":
        for i, a in enumerate(("str", "agi", "int")):
            p.setBrush(QColor(ATTR_COLORS[a]))
            p.drawPie(r, (90 - i * 120) * 16, -120 * 16)
    else:
        p.setBrush(QColor(ATTR_COLORS.get(attr or "", theme.GREY)))
        p.drawEllipse(r)
    shine = QRadialGradient(r.center() - QPointF(r.width() * 0.2, r.height() * 0.25), r.width() * 0.75)
    shine.setColorAt(0.0, QColor(255, 255, 255, 95))
    shine.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(shine)
    p.drawEllipse(r)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(0, 0, 0, 170), 1.0))
    p.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
    p.restore()


def _attr_icon(attr: str, px: int = 14) -> QIcon:
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    paint_attr(p, QRectF(0.5, 0.5, px - 1, px - 1), attr)
    p.end()
    return QIcon(pm)


def _glyph_icon(glyph: str, color: str = theme.MUTED, px: int = 16) -> QIcon:
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    f = QFont(theme.ICON_FONT)
    f.setPixelSize(int(px * 0.8))
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, px, px), Qt.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


def _name_span(name: str, query: str) -> tuple[int, int] | None:
    """Where `query` is in `name`, ignoring case, spaces and punctuation ("anti mage" finds Anti-Mage), as
    (start, length) in `name`; None if it isn't."""
    keep = [i for i, ch in enumerate(name) if ch.isalnum()]
    q = "".join(ch for ch in query.lower() if ch.isalnum())
    if not q:
        return (0, 0)
    at = "".join(name[i] for i in keep).lower().find(q)
    if at < 0:
        return None
    start, end = keep[at], keep[at + len(q) - 1] + 1
    return start, end - start


def _talent_text(text: str) -> str:
    """A talent's name without dotaconstants' unfilled placeholders ("+{s:bonus_x}% Heal" -> "+ Heal")."""
    import re

    text = re.sub(r"\{s:[^}]*\}(%|s\b)?", "", text)
    return re.sub(r"^([+-])\s*", lambda m: ("− " if m.group(1) == "-" else "+ "), text).strip()


def _grow_anim(widget: QWidget, ms: int = 520) -> QVariantAnimation:
    """A 0..1 value that eases up (bars that grow into place); the widget reads `widget._grow` when painting."""
    widget._grow = 0.0
    a = QVariantAnimation(widget)
    a.setDuration(ms)
    a.setStartValue(0.0)
    a.setEndValue(1.0)
    a.setEasingCurve(QEasingCurve.OutCubic)

    def step(v) -> None:
        widget._grow = float(v)
        widget.update()

    a.valueChanged.connect(step)
    return a


def _enter(w: QWidget, rise: int = 8) -> None:
    """Fade a view in as it's shown, rising a few pixels into place (views sit at the top of their stack)."""
    fade_in(w, NORMAL, 0)
    old = getattr(w, "_rise", None)
    if old is not None:
        old.stop()
    if rise and w.isVisible():
        a = QPropertyAnimation(w, b"pos", w)
        a.setDuration(NORMAL)
        a.setStartValue(QPoint(0, rise))
        a.setEndValue(QPoint(0, 0))
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.start()
        w._rise = a


class AttrIcon(QWidget):
    def __init__(self, attr: str = "", size: int = 14) -> None:
        super().__init__()
        self.attr = attr
        self.setFixedSize(size, size)

    def set_attr(self, attr: str) -> None:
        self.attr = attr
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        paint_attr(p, QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), self.attr)
        p.end()


class HeroCard(HeroPortrait):
    """One hero in the All heroes grid: the portrait with its attribute and name (what your search matched
    lit up in cyan), and a strip under it for its win rate at your rank: green bars grow right of the middle
    (above 50%), red ones left. Hover shows the number."""

    STRIP = 9  # px under the picture for the win-rate strip

    def __init__(self, w: int = 168) -> None:
        super().__init__(w, self.height_for(w), hover=True)
        self.attr = ""
        self.wr = 0.5
        self.match = (0, 0)
        self.top_hit = False  # Enter in the search box opens this one
        self._grow = 1.0

    @classmethod
    def height_for(cls, w: int) -> int:
        return round(w * 9 / 16) + cls.STRIP

    def set_width(self, w: int) -> None:
        if w != self.width():
            self.setFixedSize(w, self.height_for(w))

    def set_stat(self, wr: float) -> None:
        if abs(wr - self.wr) > 1e-9:
            self.wr = wr
            self.update()

    def set_match(self, start: int, length: int) -> None:
        if (start, length) != self.match:
            self.match = (start, length)
            self.update()

    def set_top_hit(self, on: bool) -> None:
        if on != self.top_hit:
            self.top_hit = on
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        img_r = QRectF(1, 1, self.width() - 2, self.height() - self.STRIP - 2)
        h = self._hover.value
        clip = QPainterPath()
        clip.addRoundedRect(img_r, 2, 2)
        p.save()
        p.setClipPath(clip)
        img = self._img
        if img is not None:
            scale = max(img_r.width() / img.width(), img_r.height() / img.height()) * (1.0 + 0.07 * h)
            tw, th = img.width() * scale, img.height() * scale
            p.drawImage(QRectF(img_r.center().x() - tw / 2, img_r.center().y() - th / 2, tw, th), img)
        else:
            g = QLinearGradient(img_r.topLeft(), img_r.bottomRight())
            g.setColorAt(0, QColor("#262b33"))
            g.setColorAt(1, QColor("#14171c"))
            p.fillRect(img_r, g)
        shade = QLinearGradient(0, img_r.bottom() - img_r.height() * 0.62, 0, img_r.bottom())
        shade.setColorAt(0, QColor(0, 0, 0, 0))
        shade.setColorAt(1, QColor(0, 0, 0, 225))
        p.fillRect(img_r, shade)
        p.restore()

        # the attribute disc and the name, bottom left
        px = max(11, min(16, int(img_r.height() * 0.15)))
        f = QFont(theme.FONT)
        f.setPixelSize(px)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        fm = p.fontMetrics()
        base = img_r.bottom() - max(5, px * 0.45)
        d = round(px * 0.78)
        x = img_r.left() + max(6, px * 0.5)
        paint_attr(p, QRectF(x, base - fm.capHeight() / 2 - d / 2, d, d), self.attr)
        x += d + 5
        name = self._name
        room = int(img_r.right() - 6 - x)
        shown = fm.elidedText(name, Qt.ElideRight, room)
        start, n = self.match
        parts = ([(name[:start], False), (name[start:start + n], True), (name[start + n:], False)]
                 if n and shown == name else [(shown, False)])
        for text, hit in parts:
            if not text:
                continue
            tw = fm.horizontalAdvance(text)
            if hit:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(63, 212, 255, 70))
                p.drawRoundedRect(QRectF(x - 1, base - fm.ascent() + 1, tw + 2, fm.ascent() + 3), 2, 2)
            p.setPen(QColor(theme.BRAND_BRIGHT if hit else theme.TEXT))
            p.drawText(QPointF(x, base), text)
            x += tw

        # a number in the top corner: the sort's (always), or the win rate (under the mouse)
        badge, color, alpha = self._badge, self._badge_color, 1.0
        if not badge and h > 0.01:
            badge, color, alpha = f"{self.wr * 100:.1f}%", _wr_color(self.wr), h
        if badge:
            bf = QFont(theme.DISPLAY)
            bf.setPixelSize(max(11, int(img_r.height() * 0.15)))
            bf.setWeight(QFont.Bold)
            p.setFont(bf)
            bw = p.fontMetrics().horizontalAdvance(badge) + 12
            bh = p.fontMetrics().height() + 2
            pill = QRectF(img_r.right() - bw - 4, img_r.top() + 4, bw, bh)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, int(185 * alpha)))
            p.drawRoundedRect(pill, 2, 2)
            c = QColor(color)
            c.setAlphaF(alpha)
            p.setPen(c)
            p.drawText(pill, Qt.AlignCenter, badge)

        # outline: quiet; cyan for the search's top hit; bright under the mouse
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(theme.BRAND) if self.top_hit else QColor(0, 0, 0, 160), 1.6 if self.top_hit else 1.0))
        p.drawRoundedRect(img_r, 2, 2)
        if h > 0.01:
            glow = QColor(theme.BRAND_BRIGHT)
            glow.setAlphaF(0.85 * h)
            p.setPen(QPen(glow, 1.8))
            p.drawRoundedRect(img_r, 2, 2)

        # the win-rate strip: a track, the 50% mark, and a bar out from it
        y = img_r.bottom() + 4
        track = QRectF(img_r.left(), y, img_r.width(), 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 16))
        p.drawRect(track)
        mid = track.center().x()
        dev = self.wr - 0.5
        span = track.width() / 2 * max(-1.0, min(1.0, dev / 0.05)) * self._grow
        c = QColor(theme.GREEN if dev >= 0 else theme.RED)
        c.setAlphaF(0.45 + 0.55 * min(1.0, abs(dev) / 0.035))
        p.setBrush(c)
        p.drawRect(QRectF(min(mid, mid + span), y, abs(span), 4))
        p.setBrush(QColor(255, 255, 255, 110))
        p.drawRect(QRectF(mid - 0.5, y - 1.5, 1, 7))
        p.end()


class _GroupHeader(QWidget):
    """A group's title over the grid: its attribute disc, name and how many heroes show, then a hairline."""

    def __init__(self, title: str, attr: str = "") -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 2)
        lay.setSpacing(8)
        if attr:
            lay.addWidget(AttrIcon(attr, 15), 0, Qt.AlignVCenter)
        self.title = label(title.upper(), "section")
        if attr:
            self.title.setStyleSheet(f"color: {ATTR_COLORS.get(attr, theme.TEXT_2)};")
        lay.addWidget(self.title, 0, Qt.AlignVCenter)
        self.count = label("", "faint")
        self.count.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 10.5pt;')
        lay.addWidget(self.count, 0, Qt.AlignVCenter)
        lay.addStretch(1)

    def set_count(self, n: int) -> None:
        self.count.setText(str(n))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        x = self.count.geometry().right() + 14
        y = self.count.geometry().center().y() + 1
        g = QLinearGradient(x, 0, self.width(), 0)
        g.setColorAt(0, QColor(theme.BORDER_STRONG))
        g.setColorAt(1, QColor(theme.BORDER_STRONG).darker(300))
        p.fillRect(QRectF(x, y, max(0, self.width() - x), 1), g)
        p.end()


class _Segmented(QWidget):
    """A row of buttons where one is on (a sort order), with a combo box's API: findData, currentData,
    setCurrentIndex, currentIndexChanged."""

    currentIndexChanged = Signal(int)

    def __init__(self, options) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self._data: list[str] = []
        for i, (data, text, tip) in enumerate(options):
            b = QPushButton(text)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            self._group.addButton(b, i)
            self._data.append(data)
            lay.addWidget(b)
        self._group.button(0).setChecked(True)
        self._index = 0
        self._group.idClicked.connect(self.setCurrentIndex)

    def count(self) -> int:
        return len(self._data)

    def findData(self, data) -> int:  # noqa: N802
        return self._data.index(data) if data in self._data else -1

    def itemData(self, i: int):  # noqa: N802
        return self._data[i]

    def currentIndex(self) -> int:  # noqa: N802
        return self._index

    def currentData(self):  # noqa: N802
        return self._data[self._index]

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        if not 0 <= i < len(self._data):
            return
        self._group.button(i).setChecked(True)
        if i != self._index:
            self._index = i
            self.currentIndexChanged.emit(i)


class _Columns(QWidget):
    """Cards in two columns on a wide window, one on a narrow one (the right column's cards then follow the
    left column's)."""

    WIDE = 1080

    def __init__(self) -> None:
        super().__init__()
        self._h = QHBoxLayout(self)
        self._h.setContentsMargins(0, 0, 0, 0)
        self._h.setSpacing(14)
        self._left, self._right = QVBoxLayout(), QVBoxLayout()
        for col in (self._left, self._right):
            col.setSpacing(14)
            self._h.addLayout(col)
        self._cards: tuple[list[QWidget], list[QWidget]] = ([], [])
        self.wide: bool | None = None

    def set_cards(self, left: list[QWidget], right: list[QWidget]) -> None:
        self._cards = (left, right)
        self._place(self.width() >= self.WIDE)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wide = event.size().width() >= self.WIDE
        if wide != self.wide:
            self._place(wide)

    def _place(self, wide: bool) -> None:
        self.wide = wide
        for col in (self._left, self._right):
            for i in reversed(range(col.count())):
                w = col.itemAt(i).widget()
                if w is not None:
                    # (removeWidget deletes the old layout item; one only taken out would stay the card's
                    # size cache, and the card would keep the height it had while its content loaded)
                    col.removeWidget(w)
                else:
                    col.takeAt(i)
        left, right = self._cards
        for c in left:
            self._left.addWidget(c)
        for c in right:
            (self._right if wide else self._left).addWidget(c)
        self._left.addStretch(1)
        self._right.addStretch(1)
        self._h.setStretch(0, 3 if wide else 1)
        self._h.setStretch(1, 2 if wide else 0)


class _Grid(QWidget):
    """Widgets in as many equal columns as fit (each at least `min_w` wide), re-flowed as the width changes."""

    def __init__(self, widgets: list[QWidget], min_w: int, max_cols: int = 3, hgap: int = 16, vgap: int = 12,
                 choices: tuple[int, ...] = ()) -> None:
        super().__init__()
        self._g = QGridLayout(self)
        self._g.setContentsMargins(0, 0, 0, 0)
        self._g.setHorizontalSpacing(hgap)
        self._g.setVerticalSpacing(vgap)
        self._ws, self._min, self._max, self._gap = widgets, min_w, max_cols, hgap
        self._choices = choices  # the column counts allowed (e.g. 1 or 4: never 3 and 1 left over)
        self._n = 0
        self._place(1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        fit = max(1, min(self._max, len(self._ws), (event.size().width() + self._gap) // (self._min + self._gap)))
        n = max((c for c in self._choices if c <= fit), default=1) if self._choices else fit
        if n != self._n:
            self._place(n)

    def _place(self, n: int) -> None:
        self._n = n
        for w in self._ws:
            self._g.removeWidget(w)
        for i, w in enumerate(self._ws):
            self._g.addWidget(w, i // n, i % n, Qt.AlignTop)
            if hasattr(w, "placed"):
                w.placed(n, i, len(self._ws))
        for c in range(self._max):
            self._g.setColumnStretch(c, 1 if c < n else 0)


class _WrBar(QWidget):
    """A win rate as a bar out from the 50% mark (green right, red left), that grows in."""

    def __init__(self, w: int = 128, h: int = 6) -> None:
        super().__init__()
        self.setFixedSize(w, h)
        self.wr = 0.5
        self._grow = 1.0
        self._anim = _grow_anim(self)

    def set_wr(self, wr: float) -> None:
        self.wr = wr
        self._anim.stop()
        self._anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        r = QRectF(self.rect())
        p.fillRect(QRectF(0, 1, r.width(), r.height() - 2), QColor(255, 255, 255, 18))
        mid, dev = r.width() / 2, self.wr - 0.5
        span = mid * max(-1.0, min(1.0, dev / 0.06)) * self._grow
        p.fillRect(QRectF(min(mid, mid + span), 1, abs(span), r.height() - 2),
                   QColor(theme.GREEN if dev >= 0 else theme.RED))
        p.fillRect(QRectF(mid - 0.5, 0, 1, r.height()), QColor(255, 255, 255, 120))
        p.end()


class _RankChart(QWidget):
    """Win rate at each rank, Herald to Immortal: bars up from 50% (green) or down (red); your rank lit."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.rows: list[tuple[str, float | None, int]] = []
        self.current = 0
        self._grow = 1.0
        self._anim = _grow_anim(self, 640)

    def set_data(self, rows: list[tuple[str, float | None, int]], current: int) -> None:
        self.rows, self.current = rows, current
        tips = [f"{n}: " + (f"{wr * 100:.1f}% in {g:,} games" if wr is not None else "not enough games")
                for n, wr, g in rows]
        self.setToolTip("\n".join(tips))
        self._anim.stop()
        self._anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.rows)
        top, bottom = 20.0, self.height() - 26.0
        devs = [wr - 0.5 for _n, wr, _g in self.rows if wr is not None]
        up = max(0.25, min(1.0, max([d for d in devs if d > 0], default=0) / 0.05))  # how far bars reach, 0..1
        down = max(0.25, min(1.0, max([-d for d in devs if d < 0], default=0) / 0.05))
        mid = top + (bottom - top) * up / (up + down)
        reach_up, reach_down = (mid - top - 2) / up, (bottom - mid - 18) / down  # px for a full 5%
        col = self.width() / n
        num = QFont(theme.DISPLAY)
        num.setPixelSize(12)
        num.setWeight(QFont.Bold)
        cap = caps_font(7.5, 700, 0.8)
        for i, (name, wr, _games) in enumerate(self.rows):
            x0 = i * col
            here = i + 1 == self.current
            if here:
                p.fillRect(QRectF(x0 + 2, 0, col - 4, self.height()), QColor(63, 212, 255, 22))
            bw = min(26.0, col * 0.46)
            bx = x0 + (col - bw) / 2
            if wr is not None:
                dev = wr - 0.5
                hgt = (reach_up if dev >= 0 else reach_down) * max(-1.0, min(1.0, dev / 0.05)) * self._grow
                c = QColor(theme.GREEN if dev >= 0 else theme.RED)
                c.setAlphaF(0.5 + 0.5 * min(1.0, abs(dev) / 0.03))
                p.setPen(Qt.NoPen)
                p.setBrush(c)
                p.drawRect(QRectF(bx, min(mid, mid - hgt), bw, max(1.0, abs(hgt))))
                p.setFont(num)
                p.setPen(QColor(theme.TEXT if here else theme.TEXT_2))
                ty = mid - hgt - 16 if dev >= 0 else mid - hgt + 2
                p.drawText(QRectF(x0, ty, col, 14), Qt.AlignCenter, f"{wr * 100:.1f}")
            else:
                p.setFont(num)
                p.setPen(QColor(theme.MUTED))
                p.drawText(QRectF(x0, mid - 16, col, 14), Qt.AlignCenter, "–")
            p.setFont(cap)
            p.setPen(QColor(theme.BRAND_BRIGHT if here else theme.MUTED))
            fits = p.fontMetrics().horizontalAdvance(name) <= col - 4
            p.drawText(QRectF(x0, bottom + 6, col, 16), Qt.AlignCenter, name if fits else name[:3])
        p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.DashLine))
        p.drawLine(QPointF(0, mid), QPointF(self.width(), mid))
        p.end()


class _Phase(QWidget):
    """One phase of the build, as a stop on a timeline: a dot and the phase's name, its items under it, and a
    line on to the next phase (down, when the phases are stacked; across, when they sit side by side)."""

    def __init__(self, title: str, body: QWidget) -> None:
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 0, 0, 14)
        v.setSpacing(8)
        self.head = label(title.upper(), "eyebrow")
        v.addWidget(self.head, 0, Qt.AlignLeft)
        v.addWidget(body)
        self.across, self.last = False, False

    def placed(self, cols: int, index: int, count: int) -> None:
        self.across = cols > 1
        self.last = index == count - 1 or (self.across and (index + 1) % cols == 0)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        x, y = 7.0, self.head.geometry().center().y() + 1.0
        p.setPen(QPen(QColor(theme.BORDER_STRONG), 2))
        if not self.last:
            if self.across:
                p.drawLine(QPointF(self.head.geometry().right() + 12, y), QPointF(self.width() + 16, y))
            else:
                p.drawLine(QPointF(x, y + 6), QPointF(x, self.height() + 2))
        p.setPen(QPen(QColor(theme.BRAND), 2))
        p.setBrush(QColor(theme.SURFACE))
        p.drawEllipse(QPointF(x, y), 5, 5)
        p.end()


class _MatchRow(QWidget):
    """One matchup: the other hero's portrait and name, how often this hero beats it, and the edge."""

    clicked = Signal()

    def __init__(self, stats: Stats, other: int, edge: float, won: float, games: int, hero_name: str) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 8, 4)
        lay.setSpacing(10)
        pic = HeroPortrait(68, 38)
        pic.set_hero(stats, other)
        pic.set_tone("good" if edge > 0 else "bad")
        lay.addWidget(pic)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        name = label(stats.name(other))
        name.setStyleSheet("font-weight: 600; font-size: 10pt;")
        texts.addWidget(name)
        sub = label(tr("wins {pct}% of {games}", pct=f"{won * 100:.0f}", games=f"{games:,}"), "faint")
        sub.setStyleSheet("font-size: 8.5pt;")
        texts.addWidget(sub)
        lay.addLayout(texts, 1)
        val = label(f"{edge * 100:+.1f}")
        val.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 15pt;'
                          f" color: {_edge_color(edge)};")
        lay.addWidget(val, 0, Qt.AlignVCenter)
        self.other = stats.name(other)
        self.setToolTip(tr("{hero} vs {other}: wins {pct}% of {games} games, {edge} against what their overall win "
                           "rates predict.\nClick for {other}.", hero=hero_name, other=self.other,
                           pct=f"{won * 100:.1f}", games=f"{games:,}", edge=f"{edge * 100:+.1f}"))
        self._hover = False

    def event(self, e) -> bool:
        if e.type() in (QEvent.HoverEnter, QEvent.HoverLeave):
            self._hover = e.type() == QEvent.HoverEnter
            self.update()
        return super().event(e)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._hover:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(63, 212, 255, 18))
            p.fillRect(QRectF(0, 0, 2, self.height()), QColor(theme.BRAND))
            p.end()


def _stat_block(caption: str, value: str, color: str = theme.TEXT, sub: str = "") -> tuple[QWidget, QLabel]:
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(1)
    v.addWidget(label(caption.upper(), "eyebrow"))
    val = label(value)
    val.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 21pt; color: {color};')
    v.addWidget(val)
    if sub:
        s = label(sub, "faint")
        s.setStyleSheet("font-size: 8.5pt;")
        v.addWidget(s)
    return w, val


def _base_stat(icon: QWidget, value: str, gain: str = "", tip: str = "", strong: str = "") -> QWidget:
    """A base number with its icon: STR 21 +1.6, move speed 310."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(5)
    h.addWidget(icon, 0, Qt.AlignVCenter)
    v = label(value)
    v.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 12.5pt;'
                    f" color: {strong or theme.TEXT};")
    h.addWidget(v, 0, Qt.AlignVCenter)
    if gain:
        g = label(gain, "faint")
        g.setStyleSheet("font-size: 9pt;")
        h.addWidget(g, 0, Qt.AlignVCenter)
    w.setToolTip(tip)
    return w


def _glyph(glyph: str, color: str = theme.TEXT_2, size: int = 14) -> QLabel:
    lab = QLabel(glyph)
    lab.setStyleSheet(f'font-family: "{theme.ICON_FONT}"; font-size: {size}px; color: {color}; background: transparent;')
    lab.setFixedWidth(size + 2)
    return lab


def _item_tile(stats: Stats, key: str) -> QWidget:
    """An item for the build: its icon, its name under it, and its cost in gold."""
    w = QWidget()
    w.setFixedWidth(ITEM_TILE_W)
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(3)
    icon = ItemIcon(60, 44, hover=True)
    icon._clickable = False
    icon.set_item(stats, key)
    v.addWidget(icon, 0, Qt.AlignHCenter)
    cost = stats.item_cost(key)
    c = label(f"{cost:,}" if cost else "")
    c.setAlignment(Qt.AlignHCenter)
    c.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 9.5pt; color: {theme.GOLD};')
    v.addWidget(c)
    name = label(stats.item_name(key), wrap=True)
    name.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
    name.setStyleSheet("font-weight: 600; font-size: 9pt;")
    f = QFont(theme.FONT)
    f.setPointSizeF(9)
    f.setWeight(QFont.DemiBold)
    name.setFixedHeight(QFontMetrics(f).lineSpacing() * 2 + 1)  # room for two lines: every tile the same height
    v.addWidget(name)
    v.addStretch(1)
    w.setToolTip(icon.toolTip())
    return w


def _ability_card(stats: Stats, key: str) -> QWidget | None:
    """One spell: its icon, name, cooldown and mana, whether BKB or a dispel stops it, and what it does."""
    a = stats.abilities.get(key, {})
    if not a.get("dname") or key == "generic_hidden":
        return None
    card = QFrame()
    card.setObjectName("abil")
    card.setStyleSheet(f"QFrame#abil {{ background: {theme.rgba('#ffffff', 0.025)}; border: 1px solid {theme.BORDER};"
                       " border-radius: 2px; }")
    h = QHBoxLayout(card)
    h.setContentsMargins(12, 12, 14, 12)
    h.setSpacing(12)
    icon = AbilityIcon(54)
    icon.set_ability(stats, key, a["dname"])
    icon.setToolTip(a["dname"])
    h.addWidget(icon, 0, Qt.AlignTop)
    texts = QVBoxLayout()
    texts.setSpacing(4)
    top = QHBoxLayout()
    top.setSpacing(8)
    nm = label(a["dname"])
    nm.setStyleSheet("font-weight: 700; font-size: 11.5pt;")
    top.addWidget(nm)
    behavior = a.get("behavior")
    behavior = behavior if isinstance(behavior, list) else [behavior or ""]
    cd = a.get("cd")
    if a.get("is_innate"):
        top.addWidget(Tag("Innate", theme.BRAND, pt=8))
    elif not cd and "Passive" in behavior:
        top.addWidget(Tag("Passive", theme.TEXT_2, pt=8))
    top.addStretch(1)
    texts.addLayout(top)
    meta = []
    icon_span = f'<span style="font-family:\'{theme.ICON_FONT}\'; font-size:11px;">{{}}</span>'
    def joined(v) -> str:  # "30 / 26 / 22", kept on one line
        return "&nbsp;/&nbsp;".join(v) if isinstance(v, list) else str(v)

    if cd:
        meta.append(f'<span style="color:{theme.TEXT_2}">{icon_span.format(_G.COOLDOWN)}&nbsp;{joined(cd)}&nbsp;s</span>')
    mc = a.get("mc")
    if mc and any(str(x).strip("0. ") for x in (mc if isinstance(mc, list) else [mc])):
        meta.append(f'<span style="color:#7fb0ff">{icon_span.format(_G.MANA)}&nbsp;{joined(mc)}&nbsp;mana</span>')
    if a.get("dmg_type"):
        meta.append(f'<span style="color:{theme.TEXT_2}">{a["dmg_type"]} damage</span>')
    if meta:
        m = _rich("&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta))
        m.setStyleSheet("font-size: 9pt;")
        m.setWordWrap(True)
        texts.addWidget(m)
    chips = QHBoxLayout()
    chips.setSpacing(6)
    hits_enemies = "Enemy" in str(a.get("target_team", ""))
    bkb = a.get("bkbpierce") if hits_enemies else None  # BKB only matters for spells cast on you
    if bkb == "Yes":
        chips.addWidget(_pill("Goes through BKB", theme.RED))
    elif bkb == "No":
        chips.addWidget(_pill("BKB stops it", theme.NEUTRAL))
    disp = a.get("dispellable")
    if disp in ("Yes", "Strong Dispels Only", "No"):
        chips.addWidget(_pill(_dispel(disp), theme.RED if disp == "No" else
                              theme.AMBER if disp == "Strong Dispels Only" else theme.NEUTRAL))
    if chips.count():
        chips.addStretch(1)
        texts.addLayout(chips)
    desc = (a.get("desc") or "").strip()
    if desc:
        d = label(desc, "muted", wrap=True)
        d.setStyleSheet("font-size: 9.5pt;")
        texts.addWidget(d)
    texts.addStretch(1)
    h.addLayout(texts, 1)
    return card


class HeroBanner(QWidget):
    """The hero's header: its portrait over a blurred, darkened copy of itself with its attribute's color along
    the top, then its name, roles and the numbers that matter at your rank."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(170)
        self._key = ""
        self._attr = ""
        self._img: QImage | None = None
        self._blur: QImage | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(22, 22, 26, 22)
        lay.setSpacing(24)
        self.portrait = HeroPortrait(320, 180)
        lay.addWidget(self.portrait, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        eyebrow = QHBoxLayout()
        eyebrow.setSpacing(8)
        self.attr_icon = AttrIcon("", 16)
        eyebrow.addWidget(self.attr_icon, 0, Qt.AlignVCenter)
        self.facts = _rich("", "muted")
        self.facts.setWordWrap(True)
        eyebrow.addWidget(self.facts, 1)
        texts.addLayout(eyebrow)
        self.title = label("", "heroTitle")
        texts.addWidget(self.title)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chips.setContentsMargins(0, 2, 0, 6)
        texts.addLayout(self.chips)
        self.numbers_w = QWidget()
        self.numbers = FlowLayout(self.numbers_w, 30)
        texts.addWidget(self.numbers_w)
        self.base_w = QWidget()
        self.base = FlowLayout(self.base_w, 18)
        texts.addWidget(self.base_w)
        texts.addStretch(1)
        lay.addLayout(texts, 3)
        # On a wide window the win rate at every rank sits here, on the right (else it's a card further down).
        self.side = QWidget()
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(0, 2, 0, 0)
        sv.setSpacing(4)
        sv.addWidget(label("WIN RATE BY RANK", "eyebrow"))
        self.ranks = _RankChart()
        self.ranks.setMinimumHeight(140)
        sv.addWidget(self.ranks, 1)
        self.side.setMinimumWidth(300)
        self.side.setMaximumWidth(470)
        self.side.hide()
        lay.addWidget(self.side, 2)
        self.side_on = False
        self.on_side = None  # called with True / False as the chart moves into the banner or out of it
        self._has_ranks = False
        self.wr_bar: _WrBar | None = None
        art().arrived.connect(self._arrived)

    SIDE_MIN = 1180  # the banner's width from which the rank chart fits beside the numbers

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        w = max(190, min(352, int(event.size().width() * 0.27)))
        if w != self.portrait.width():
            self.portrait.setFixedSize(w, w * 9 // 16)
        self._place_side()

    def _place_side(self) -> None:
        on = self._has_ranks and self.width() >= self.SIDE_MIN
        if on != self.side_on:
            self.side_on = on
            self.side.setVisible(on)
            if self.on_side is not None:
                self.on_side(on)

    def set_ranks(self, rows: list, current: int) -> None:
        self._has_ranks = bool(rows)
        if rows:
            self.ranks.set_data(rows, current)
        self._place_side()

    def set_hero(self, stats: Stats, hid: int) -> None:
        self.portrait.set_hero(stats, hid)
        self._key, path = Art.hero_key(stats, hid)
        self._attr = stats.heroes.get(hid, {}).get("primary_attr") or ""
        self.attr_icon.set_attr(self._attr)
        self.attr_icon.show()
        self._img = art().image(self._key, path)
        self._blur = None
        self.update()

    def show_numbers(self, stats: Stats, hid: int, bracket: int) -> None:
        """Name, attribute, attack, roles, and win rate, pick rate and games at this rank; base attributes."""
        h = stats.heroes.get(hid, {})
        attr = h.get("primary_attr") or ""
        color = ATTR_COLORS.get(attr, theme.TEXT_2)
        bits = [f'<b style="color:{color}">{ATTR_NAMES.get(attr, "Hero")}</b>']
        if h.get("attack_type"):
            bits.append(h["attack_type"])
        if h.get("attack_range"):
            bits.append(f'{h["attack_range"]} range')
        self.facts.setText(" &nbsp;·&nbsp; ".join(bits))
        self.title.setText(stats.name(hid))
        self.set_chips([Tag(r, theme.TEXT_2, pt=8.5) for r in h.get("roles", [])])
        _clear(self.numbers)
        wr, games = stats.win_rate(hid, bracket)
        where = tr(BRACKETS.get(bracket, "All ranks"))
        block, _v = _stat_block("Win rate", f"{wr * 100:.1f}%", _wr_color(wr))
        self.wr_bar = _WrBar(118, 6)
        block.layout().addWidget(self.wr_bar)
        block.setToolTip(tr("Wins {pct}% of {games} games at {where}", pct=f"{wr * 100:.1f}", games=f"{games:,}",
                              where=where))
        self.wr_bar.set_wr(wr)
        self.numbers.addWidget(block)
        pr = stats.pick_rate(hid, bracket)
        block, _v = _stat_block("Pick rate", f"{pr * 100:.0f}%", theme.NEUTRAL, "of games")
        block.setToolTip(tr("Picked in {pct}% of games at {where}", pct=f"{pr * 100:.1f}", where=where))
        self.numbers.addWidget(block)
        block, _v = _stat_block("Games", _short(games), theme.NEUTRAL, where)
        block.setToolTip(tr("{games} public games at {where}, from OpenDota", games=f"{games:,}", where=where))
        self.numbers.addWidget(block)
        _clear(self.base)
        for a, key, name in (("str", "str", "Strength"), ("agi", "agi", "Agility"), ("int", "int", "Intelligence")):
            base, gain = h.get(f"base_{key}"), h.get(f"{key}_gain")
            if base is None:
                continue
            strong = ATTR_COLORS[a] if a == attr else ""
            self.base.addWidget(_base_stat(AttrIcon(a, 13), f"{base:g}", f"+{gain:g}" if gain is not None else "",
                                           f"{name}: {base:g} at level 1, +{gain or 0:g} per level", strong))
        if h.get("move_speed"):
            self.base.addWidget(_base_stat(_glyph(_G.MOVE), f"{h['move_speed']}", "", "Base move speed"))
        if h.get("attack_range"):
            self.base.addWidget(_base_stat(_glyph(_G.RANGE), f"{h['attack_range']}", "",
                                           f"Attack range ({h.get('attack_type', '').lower() or 'attack'})"))
        self.base_w.setVisible(bool(self.base.count()))

    def clear(self) -> None:
        self.portrait.hero_id = None
        self.portrait.set_empty("?")
        self._key, self._img, self._blur, self._attr = "", None, None, ""
        self.attr_icon.hide()
        self.set_ranks([], 0)
        self.set_chips([])
        _clear(self.numbers)
        _clear(self.base)
        self.base_w.hide()
        self.update()

    def set_chips(self, chips: list[QWidget]) -> None:
        _clear(self.chips)
        for c in chips:
            self.chips.addWidget(c)
        self.chips.addStretch(1)

    def _arrived(self, key: str) -> None:
        if key == self._key and self._img is None:
            self._img = art().image(key, "")
            self._blur = None
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import cut_path, paint_backdrop

        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        shape = cut_path(r, theme.CUT)
        if self._img is not None:
            p.save()
            p.setClipPath(shape)
            if self._blur is None:  # shrink then enlarge: a cheap, soft blur
                self._blur = self._img.scaled(28, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            iw, ih = self._blur.width(), self._blur.height()
            scale = max(r.width() / iw, r.height() / ih)
            p.drawImage(QRectF(0, (r.height() - ih * scale) / 2, iw * scale, ih * scale), self._blur)
            shade = QLinearGradient(r.topLeft(), r.topRight())
            shade.setColorAt(0, QColor(11, 13, 16, 232))
            shade.setColorAt(0.55, QColor(11, 13, 16, 205))
            shade.setColorAt(1, QColor(11, 13, 16, 150))
            p.fillRect(r, shade)
            p.restore()
        else:
            paint_backdrop(p, r, theme.RADIUS)
        if self._attr:  # the attribute's color along the top edge, fading out
            p.save()
            p.setClipPath(shape)
            edge = QLinearGradient(r.topLeft(), QPointF(r.left() + r.width() * 0.8, r.top()))
            c = QColor(ATTR_COLORS.get(self._attr, theme.BRAND))
            edge.setColorAt(0, c)
            c2 = QColor(c)
            c2.setAlpha(0)
            edge.setColorAt(1, c2)
            p.fillRect(QRectF(r.left(), r.top(), r.width(), 3), edge)
            p.restore()
        p.setPen(QPen(QColor(63, 212, 255, 60), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(cut_path(r.adjusted(0.5, 0.5, -0.5, -0.5), theme.CUT))
        p.end()


def _short(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n:,}"


def _hero_matchups(s: Stats, hid: int) -> list[tuple[float, int, float, int]]:
    """[(edge, other hero, this hero's win rate against it, games)], best first. The edge is how much better
    this hero does against the other than their overall win rates predict, shrunk when there are few games
    (as in the Draft page). Runs on a worker thread: matchups may come from the network."""
    wr = s.win_rate(hid)[0]
    out = []
    for m in s.matchups(hid):
        other, games = m.get("hero_id"), m.get("games_played") or 0
        if other not in s.heroes or other == hid or games < MATCHUP_MIN_GAMES:
            continue
        won = (m.get("wins") or 0) / games
        expected = 0.5 + (wr - 0.5) - (s.win_rate(other)[0] - 0.5)
        out.append(((won - expected) * games / (games + draft.PRIOR_GAMES), other, won, games))
    out.sort(key=lambda row: -row[0])
    return out


class HeroesPage(QWidget):
    """Every hero in a grid you can search and filter; pick one for its build, spells, talents, matchups,
    win rate at each rank and what to buy against it."""

    def __init__(self, ctl, service: StatsService) -> None:
        super().__init__()
        self.ctl, self.svc = ctl, service
        self.setObjectName("page")
        self.setFocusPolicy(Qt.ClickFocus)  # a click on the page takes the keys: type to search, Esc, arrows
        page, outer, body, controls = _page_shell(
            "Heroes", "Builds, spells, matchups and what beats every hero, from public games at your rank.")
        page.max_width = 2240  # this page's grid and two columns use a wide window
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page)
        self.mode = ModeSwitch(False)
        outer.insertWidget(0, self.mode, 0, Qt.AlignLeft)
        self.rank = _bracket_combo(ctl)
        self.rank.currentIndexChanged.connect(lambda _i: self._rank_moved())
        controls.addWidget(_labelled("Rank", self.rank))
        self._scroll = page.findChild(QScrollArea)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # the bar over the content: filters for the grid, back / previous / next for a hero
        self.bars = PageStack()
        outer.insertWidget(2, self.bars)
        self.bars.addWidget(self._filter_bar())
        self.bars.addWidget(self._hero_bar())

        self.stack = PageStack()
        body.addWidget(self.stack)
        body.addStretch(1)

        # all heroes
        self.grid_page = QWidget()
        page_lay = QVBoxLayout(self.grid_page)
        page_lay.setContentsMargins(0, 0, 0, 0)
        page_lay.setSpacing(8)
        self.grid_holder = QWidget()
        self.grid_lay = QVBoxLayout(self.grid_holder)
        self.grid_lay.setContentsMargins(0, 0, 0, 0)
        self.grid_lay.setSpacing(8)
        self.grid_lay.addWidget(_flow([Shimmer(block=(168, 102)) for _ in range(14)], 10))
        page_lay.addWidget(self.grid_holder)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 30, 0, 30)
        el.setSpacing(10)
        self.empty_text = label("", "muted")
        self.empty_text.setAlignment(Qt.AlignCenter)
        el.addWidget(self.empty_text)
        again = button("Clear filters", "ghost")
        again.clicked.connect(self.clear_filters)
        el.addWidget(again, 0, Qt.AlignHCenter)
        self.empty.hide()
        page_lay.addWidget(self.empty)
        page_lay.addStretch(1)
        self._tiles: list[tuple[str, HeroCard]] = []
        self._groups: list[tuple[_GroupHeader, QWidget, list[int]]] = []
        self._pics: dict[int, HeroCard] = {}
        self._order: list[int] = []
        self._top: int | None = None
        self._tile_w = 0
        self.stack.addWidget(self.grid_page)

        # one hero
        self.detail = QWidget()
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(14)
        self.banner = HeroBanner()
        self.title, self.facts = self.banner.title, self.banner.facts
        self.banner.on_side = lambda on: self._cards[4].setVisible(self._has_ranks and not on)
        self._has_ranks = False
        dl.addWidget(self.banner)
        self.cols = _Columns()
        build, self.build_lay = _section("Build by game phase", "most bought by players of this hero")
        abil, self.abil_lay = _section("Abilities", "how its spells work against you")
        match, self.match_lay = _section("Matchups", "its edge against each hero · all ranks")
        vs, self.vs_lay = _section("Against this hero", "what to buy")
        ranks, self.rank_lay = _section("Win rate by rank", "your rank is lit")
        talents, self.talent_lay = _section("Talents", "levels 10 to 25: pick one of each pair")
        self.cols.set_cards([build, abil], [match, vs, ranks, talents])
        self._cards = (build, abil, match, vs, ranks, talents)
        dl.addWidget(self.cols)
        dl.addStretch(1)
        self.stack.addWidget(self.detail)

        self._current: str | None = None
        self._hid: int | None = None
        self._followed = ""
        self._built = False
        self._grown = False
        self._token = 0
        self._grid_scroll = 0
        service.ready.connect(self._on_ready)
        service.result.connect(self._on_result)
        self.title.setText("Pick a hero")
        find = QShortcut(QKeySequence(QKeySequence.Find), self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self._focus_search)
        self._scroll.viewport().installEventFilter(self)  # refit the grid as the width changes
        self._scroll.installEventFilter(self)  # (it takes the arrow keys for scrolling; ← → step through heroes)

    # the bars --------------------------------------------------------------------------
    def _filter_bar(self) -> QWidget:
        card = Card(margins=(14, 12, 14, 12), spacing=10)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search heroes…  (or just start typing)")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(_glyph_icon(_G.SEARCH, theme.TEXT_2), QLineEdit.LeadingPosition)
        self.search.setMinimumWidth(220)
        self.search.setStyleSheet("font-size: 11.5pt; padding: 8px 10px;")
        self.search.textEdited.connect(self._typed)
        self.search.returnPressed.connect(self._search_enter)
        self.search.installEventFilter(self)
        top.addWidget(self.search, 1)
        self.count = label("", "muted")
        self.count.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 11pt;')
        self.count.setMinimumWidth(116)
        top.addWidget(self.count)
        self.clear_btn = button("Clear filters", "link")
        self.clear_btn.clicked.connect(self.clear_filters)
        self.clear_btn.hide()
        top.addWidget(self.clear_btn)
        top.addSpacing(6)
        top.addWidget(label("SORT", "eyebrow"))
        self.sort = _Segmented(SORTS)
        self.sort.currentIndexChanged.connect(lambda _i: self._layout_grid())
        top.addWidget(self.sort)
        card.body.addLayout(top)

        chips_w = QWidget()  # wraps to a second line on a narrow window
        chips = FlowLayout(chips_w, 6)
        self._attrs = QButtonGroup(self)
        self._attrs.setExclusive(False)
        for i, a in enumerate(ATTR_ORDER):
            b = self._chip(ATTR_NAMES[a], tr("{name} heroes (pick more than one to see either)",
                                             name=tr(ATTR_NAMES[a])))
            b.setIcon(_attr_icon(a))
            b.setIconSize(QSize(14, 14))
            b.setProperty("attr", a)
            self._attrs.addButton(b, i)
            chips.addWidget(b)
        chips.addWidget(self._divider())
        self._attacks = QButtonGroup(self)
        self._attacks.setExclusive(False)
        for i, a in enumerate(("Melee", "Ranged")):
            b = self._chip(a, tr("{name} heroes only", name=tr(a)))
            b.setProperty("attack", a)
            self._attacks.addButton(b, i)
            chips.addWidget(b)
        chips.addWidget(self._divider())
        self._roles = QButtonGroup(self)
        self._roles.setExclusive(False)
        for i, r in enumerate(HERO_ROLES, start=1):
            b = self._chip(r, tr("{name} heroes (pick more than one to need all of them)", name=tr(r)))
            b.setProperty("role", r)
            self._roles.addButton(b, i)
            chips.addWidget(b)
        for group in (self._attrs, self._attacks, self._roles):
            group.idClicked.connect(lambda _i: self._filter())
        card.body.addWidget(chips_w)
        return card

    @staticmethod
    def _chip(text: str, tip: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("tab")
        b.setCheckable(True)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        return b

    @staticmethod
    def _divider() -> QWidget:
        d = QFrame()
        d.setFixedSize(9, 30)
        d.setStyleSheet(f"border: none; border-left: 1px solid {theme.BORDER_STRONG}; margin: 5px 0 5px 4px;")
        return d

    def _hero_bar(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        back = button("All heroes", "ghost", theme.Glyph.BACK, "Back to every hero (Esc)")
        back.clicked.connect(self.show_grid)
        h.addWidget(back)
        h.addStretch(1)
        self.prev_btn = button("‹", "ghost")
        self.next_btn = button("›", "ghost")
        for b, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            b.clicked.connect(lambda _c=False, d=step: self._step(d))
            h.addWidget(b)
        return w

    # loading -------------------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.svc.ensure_loaded()
        self._grow_in()  # (the children are visible by the time the page's show event arrives)
        from PySide6.QtWidgets import QApplication

        focus = QApplication.focusWidget()
        if focus is None or not self.isAncestorOf(focus):
            QTimer.singleShot(0, self.setFocus)  # ("just start typing" works right after clicking the tab)

    def _on_ready(self) -> None:
        s = self.svc.stats
        if not s.loaded and not self._built:  # say so, instead of a shimmer that never ends
            _clear(self.grid_lay)
            msg = label(tr("Couldn't load the hero list ({error}). Check your connection.",
                           error=s.error or tr("offline?")), "faint",
                        wrap=True)
            again = button("Try again", "ghost")
            def retry() -> None:
                self.svc._tried = -1e9  # (you asked: no waiting out the automatic retry's pause)
                again.setEnabled(False)
                self.svc.ensure_loaded()

            again.clicked.connect(retry)
            self.grid_lay.addWidget(msg)
            self.grid_lay.addWidget(again, 0, Qt.AlignLeft)
        if s.loaded:
            self._build_grid()
        if self._current:
            self.show_hero(self._current)

    # the grid -----------------------------------------------------------------------
    def _build_grid(self) -> None:
        if self._built:
            return
        self._built = True
        s = self.svc.stats
        for hid in s.heroes:
            pic = HeroCard()
            pic.set_hero(s, hid)
            pic.attr = s.heroes[hid].get("primary_attr") or ""
            pic.clicked.connect(lambda n=s.name(hid): self.show_hero(n))
            self._pics[hid] = pic
            self._tiles.append((s.name(hid).lower(), pic))
        self._layout_grid()
        self._grow_in()

    def _grow_in(self) -> None:
        """The win-rate strips grow in, the first time you see the grid."""
        if self._grown or not self._built or not self.grid_page.isVisible():
            return
        self._grown = True
        for pic in self._pics.values():
            anim = _grow_anim(pic, 700)
            anim.finished.connect(anim.deleteLater)
            anim.start()

    def _rank_moved(self) -> None:
        """A new rank: the grid's order and numbers change too, not only the hero that's open."""
        current = self._current
        self._layout_grid()
        if current:
            self.show_hero(current, animate=False)

    def _layout_grid(self) -> None:
        """Group the heroes by attribute, or one list by win rate, pick rate or name."""
        s = self.svc.stats
        if not self._pics:
            return
        for pic in self._pics.values():
            pic.setParent(None)
        _clear(self.grid_lay)
        self._groups = []
        mode = self.sort.currentData()
        bracket = int(self.rank.currentData())
        where = tr(BRACKETS.get(bracket, "All ranks")).lower()
        if mode == "attr":
            by_attr: dict[str, list[int]] = {}
            for hid, h in s.heroes.items():
                by_attr.setdefault(h.get("primary_attr") or "all", []).append(hid)  # (missing in the data)
            groups = [(ATTR_NAMES.get(a, a), a if a in ATTR_COLORS else "", sorted(by_attr[a], key=s.name))
                      for a in ATTR_ORDER + [x for x in by_attr if x not in ATTR_ORDER] if by_attr.get(a)]
        elif mode == "wr":
            groups = [(f"Win rate at {where}", "", sorted(s.heroes, key=lambda h: -s.win_rate(h, bracket)[0]))]
        elif mode == "pick":
            groups = [(f"Most picked at {where}", "", sorted(s.heroes, key=lambda h: -s.pick_rate(h, bracket)))]
        else:
            groups = [("A to Z", "", sorted(s.heroes, key=s.name))]
        self._order = []
        for title, attr, ids in groups:
            head = _GroupHeader(title, attr)
            self.grid_lay.addWidget(head)
            ids = [h for h in ids if h in self._pics]
            for h in ids:
                pic = self._pics[h]
                wr, games = s.win_rate(h, bracket)
                pic.set_stat(wr)
                if mode == "pick":
                    pic.set_badge(f"{s.pick_rate(h, bracket) * 100:.0f}%", theme.NEUTRAL)
                else:
                    pic.set_badge(f"{wr * 100:.1f}%" if mode == "wr" else "", _wr_color(wr))
                hero = s.heroes.get(h, {})
                pic.setToolTip("\n".join([
                    s.name(h), " · ".join(x for x in (ATTR_NAMES.get(hero.get("primary_attr") or "", ""),
                                                      hero.get("attack_type", ""), ", ".join(hero.get("roles", [])))
                                          if x),
                    f"Win rate at {where}: {wr * 100:.1f}% ({games:,} games)",
                    f"Picked in {s.pick_rate(h, bracket) * 100:.0f}% of games",
                    "Click for its build, spells and matchups"]))
            holder = _flow([self._pics[h] for h in ids], 10)
            self.grid_lay.addWidget(holder)
            self._groups.append((head, holder, ids))
            self._order += ids
        self._fit_grid()
        self._filter(scroll=False)

    def _fit_grid(self) -> None:
        """As many columns as fit, each tile filling its share of the width: bigger tiles on a wider window."""
        if not self._tiles:
            return
        avail = self._scroll.viewport().width() - 12  # (the body's right margin)
        if avail < 200:
            return
        gap = 10
        target = max(150, min(200, int(avail / 9.5)))
        n = max(3, (avail + gap) // (target + gap))
        w = (avail - (n - 1) * gap) // n
        if w != self._tile_w:
            self._tile_w = w
            for _name, pic in self._tiles:
                pic.set_width(w)
            for _head, holder, _ids in self._groups:
                holder.layout().invalidate()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_grid()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Resize and obj is self._scroll.viewport():
            self._fit_grid()
        elif (obj is self._scroll and event.type() == QEvent.KeyPress and self.stack.currentIndex() == 1
              and event.key() in (Qt.Key_Left, Qt.Key_Right) and not event.modifiers()):
            self._step(-1 if event.key() == Qt.Key_Left else 1)
            return True
        elif obj is self.search and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            if self.search.text():
                self.search.clear()
                self._filter()
            else:
                self.setFocus()
            return True
        return super().eventFilter(obj, event)

    def _checked(self, group: QButtonGroup, prop: str) -> set[str]:
        return {b.property(prop) for b in group.buttons() if b.isChecked()}

    def _filter(self, _text: str = "", scroll: bool = True) -> None:
        """Show the heroes that match the search and the chips (attributes: any of those picked; melee or
        ranged; roles: all of those picked), light up what the search matched, and count them."""
        q = self.search.text().strip()
        s = self.svc.stats
        attrs, attacks = self._checked(self._attrs, "attr"), self._checked(self._attacks, "attack")
        roles = self._checked(self._roles, "role")
        alias = s.find(q) if q else None
        self._top = None
        shown: set[int] = set()
        for hid in self._order:
            pic = self._pics[hid]
            h = s.heroes.get(hid, {})
            span = _name_span(s.name(hid), q) if q else (0, 0)
            ok = ((span is not None or alias == hid)
                  and (not attrs or (h.get("primary_attr") or "all") in attrs)
                  and (not attacks or h.get("attack_type") in attacks)
                  and roles <= set(h.get("roles", [])))
            pic.setVisible(ok)
            pic.set_match(*(span or (0, 0)))
            if ok:
                shown.add(hid)
                if self._top is None:
                    self._top = hid
        for hid, pic in self._pics.items():
            pic.set_top_hit(bool(q) and hid == self._top)
        for head, holder, ids in self._groups:
            n = sum(1 for h in ids if h in shown)
            head.set_count(n)
            head.setVisible(n > 0)
            holder.setVisible(n > 0)
            holder.layout().invalidate()
        total = len(self._order)
        self.count.setText(tr("All {n} heroes", n=total) if len(shown) == total else
                           tr("{shown} of {n} heroes", shown=len(shown), n=total))
        filtered = bool(q or attrs or attacks or roles)
        self.clear_btn.setVisible(filtered)
        self.empty.setVisible(self._built and not shown)
        if not shown:
            chips = bool(attrs or attacks or roles)
            what = f"“{q}”" + (tr(" with those filters") if chips else "") if q else tr("those filters")
            self.empty_text.setText(tr("No hero matches {what}.", what=what) + (" " + tr("Try fewer filters.")
                                                                                if chips else ""))
        if scroll and self.stack.currentIndex() == 0:
            self._scroll.verticalScrollBar().setValue(0)

    def _typed(self, _text: str = "") -> None:
        if self.stack.currentIndex() != 0 and self.search.text().strip():
            self.show_grid()  # (the search box only shows over the grid, but a completer or paste can still type)
        self._filter()

    def clear_filters(self) -> None:
        self.search.clear()
        for group in (self._attrs, self._attacks, self._roles):
            for b in group.buttons():
                b.setChecked(False)
        self._filter()

    def _focus_search(self) -> None:
        if self.stack.currentIndex() != 0:
            self.show_grid(restore=False)  # (a new search: its top hit at the top, not your old scroll spot)
        self.search.setFocus()
        self.search.selectAll()

    def _search_enter(self) -> None:
        text = self.search.text().strip()
        if self.svc.stats.find(text):
            self.show_hero(text)
        elif text and self._top is not None:  # Enter opens the top hit (outlined in cyan)
            self.show_hero(self.svc.stats.name(self._top))

    def show_grid(self, restore: bool = True) -> None:
        self._current = None
        if self.stack.currentIndex() == 0:
            return
        self.stack.setCurrentIndex(0)
        self.bars.setCurrentIndex(0)
        self._fit_grid()
        _enter(self.grid_page)
        self._grow_in()
        bar, at = self._scroll.verticalScrollBar(), self._grid_scroll

        def restore_scroll() -> None:  # back where you were in the grid, once it's laid out again
            try:
                bar.setValue(at)
            except RuntimeError:  # (the window closed meanwhile)
                pass

        if restore:
            QTimer.singleShot(0, restore_scroll)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key, text = event.key(), event.text()
        detail = self.stack.currentIndex() == 1
        if key == Qt.Key_Escape and detail:
            self.show_grid()  # Esc: back to all heroes
            return
        if detail and key in (Qt.Key_Left, Qt.Key_Right):
            self._step(-1 if key == Qt.Key_Left else 1)
            return
        if not detail and key in (Qt.Key_Return, Qt.Key_Enter):
            self._search_enter()
            return
        if (text and text.isprintable() and not text.isspace()
                and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
            self._focus_search()  # type anywhere on the page to search
            self.search.setText(text)
            self._filter()
            return
        super().keyPressEvent(event)

    # one hero -------------------------------------------------------------------------
    def refresh(self, st: dict | None) -> None:
        """Follow your hero once a game starts (until you look up another one)."""
        follow_rank(self.rank, self.ctl)
        hero = (st or {}).get("hero") or ""
        if hero and hero != self._followed and (self._current in (None, self._followed)):
            self._followed = hero
            self.show_hero(hero)

    def _neighbors(self, hid: int) -> tuple[int | None, int | None]:
        """The heroes before and after this one in the grid as it's sorted and filtered."""
        order = [h for h in self._order if not self._pics[h].isHidden()]
        if hid not in order:
            order = self._order
        if hid not in order or len(order) < 2:
            return None, None
        i = order.index(hid)
        return order[i - 1], order[(i + 1) % len(order)]

    def _step(self, d: int) -> None:
        if self._hid is None:
            return
        prev, nxt = self._neighbors(self._hid)
        target = prev if d < 0 else nxt
        if target is not None:
            self.show_hero(self.svc.stats.name(target))

    def show_hero(self, name: str, animate: bool = True) -> None:
        self._current = name
        stats = self.svc.stats
        if not stats.loaded:
            return
        if self.stack.currentIndex() == 0:
            self._grid_scroll = self._scroll.verticalScrollBar().value()
            for pic in self._pics.values():  # the tile you clicked never saw the mouse leave
                if pic._hover.value:
                    pic._hover._a.stop()
                    pic._hover.value = 0.0
        self.stack.setCurrentIndex(1)
        self.bars.setCurrentIndex(1)
        self._scroll.verticalScrollBar().setValue(0)  # a hero starts at the top
        hid = stats.find(name)
        self._hid = hid
        self._token += 1
        if hid is None:
            self.banner.clear()  # not the last hero's picture and numbers under a new name
            self.title.setText(name or "Pick a hero")
            self.facts.setText("No hero by that name. Try the full name, like Anti-Mage or Nature's Prophet.")
            self.cols.hide()
            for b in (self.prev_btn, self.next_btn):
                b.hide()
            return
        self.cols.show()
        bracket = int(self.rank.currentData())
        self.banner.set_hero(stats, hid)
        self.banner.show_numbers(stats, hid, bracket)
        prev, nxt = self._neighbors(hid)
        for b, other, text in ((self.prev_btn, prev, "‹   {}"), (self.next_btn, nxt, "{}   ›")):
            b.setVisible(other is not None)
            if other is not None:
                b.setText(text.format(stats.name(other)))
                b.setToolTip(f"{stats.name(other)} ({'←' if b is self.prev_btn else '→'} key)")
        self._fill_abilities(stats, hid)
        self._fill_talents(stats, hid)
        self._fill_ranks(stats, hid, bracket)
        self._fill_counters(stats, hid)
        for lay, rows in ((self.build_lay, 4), (self.match_lay, 4)):
            _fill(lay, _loading(rows))
        if animate:
            _enter(self.detail)
        token = self._token

        def work(s: Stats):
            try:
                pop = s.item_popularity(hid)
            except Exception:
                pop = {}
            try:
                vs = _hero_matchups(s, hid)
            except Exception:
                vs = []
            return token, hid, pop, vs

        self.svc.run("hero", work)

    def _on_result(self, name: str, value) -> None:
        if name != "hero" or isinstance(value, Exception):
            return
        token, hid, pop, vs = value
        if token != self._token:
            return  # you've moved on to another hero
        s = self.svc.stats
        self._fill_build(s, pop)
        self._fill_matchups(s, hid, vs)

    def _fill_build(self, s: Stats, pop: dict) -> None:
        rows: list[QWidget] = []
        for key, title in PHASES:
            items = sorted(((k, v) for k, v in (pop.get(key) or {}).items()), key=lambda kv: -kv[1])
            names = []
            for item_id, _n in items:
                key_name = _item_key(s, int(item_id))
                if key_name and (key == "start_game_items" or key_name not in SKIP_ITEMS):
                    names.append(key_name)
                if len(names) >= 6:
                    break
            rows.append(_Phase(title, _flow([_item_tile(s, k) for k in names], 6) if names else
                               label("no data yet", "faint")))
        if any(pop.get(k) for k, _t in PHASES):
            # four phases side by side on a wide card (each two or three items across), else one under another
            content = _Grid(rows, 4 * ITEM_TILE_W // 2 + 40, 4, 16, 0, choices=(1, 4))
        else:
            content = label("No build data for this hero yet (OpenDota may be busy). Try again in a minute.",
                            "faint", wrap=True)
        _fill(self.build_lay, [content])
        fade_in(content, NORMAL, 0)

    def _fill_matchups(self, s: Stats, hid: int, vs: list) -> None:
        worst = [r for r in reversed(vs) if r[0] < -0.001][:6]
        best = [r for r in vs if r[0] > 0.001][:6]
        if not worst and not best:
            _fill(self.match_lay, [label("Not enough games against other heroes yet.", "faint", wrap=True)])
            return
        cols = []
        for title, color, rows in (("Countered by", theme.RED, worst), ("Strong against", theme.GREEN, best)):
            col = QWidget()
            v = QVBoxLayout(col)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)
            head = label(title.upper(), "eyebrow")
            head.setStyleSheet(f"color: {color};")
            v.addWidget(head)
            for edge, other, won, games in rows:
                r = _MatchRow(s, other, edge, won, games, s.name(hid))
                r.clicked.connect(lambda n=s.name(other): self.show_hero(n))
                v.addWidget(r)
            if not rows:
                v.addWidget(label("none stand out", "faint"))
            v.addStretch(1)
            cols.append(col)
        grid = _Grid(cols, 250, 2, 18)
        _fill(self.match_lay, [grid])
        fade_in(grid, NORMAL, 0)

    def _fill_abilities(self, s: Stats, hid: int) -> None:
        npc = s.heroes.get(hid, {}).get("name", "")
        cards = [c for c in (_ability_card(s, k) for k in s.hero_abilities.get(npc, {}).get("abilities", [])) if c]
        _fill(self.abil_lay, [_Grid(cards, 330, 2, 12, 12)] if cards else [label("No ability data.", "faint")])

    def _fill_talents(self, s: Stats, hid: int) -> None:
        npc = s.heroes.get(hid, {}).get("name", "")
        by_level: dict[int, list[str]] = {}
        for t in s.hero_abilities.get(npc, {}).get("talents", []):
            text = _talent_text(s.abilities.get(t.get("name", ""), {}).get("dname", ""))
            if text:
                by_level.setdefault(int(t.get("level") or 0), []).append(text)
        talents_card = self._cards[5]
        talents_card.setVisible(bool(by_level))
        if not by_level:
            return
        holder = QWidget()
        g = QGridLayout(holder)
        g.setContentsMargins(0, 2, 0, 0)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(8)
        for r, level in enumerate(sorted(by_level, reverse=True)):
            pair = by_level[level] + [""] * 2
            left = label(pair[0], wrap=True)
            left.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            right = label(pair[1], wrap=True)
            for lab in (left, right):
                lab.setStyleSheet("font-size: 9.5pt;")
            badge = label(str(5 + 5 * level))
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(34, 26)
            badge.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 11.5pt;'
                                f" color: {theme.TEXT}; background: {theme.SURFACE_2};"
                                f" border: 1px solid {theme.BORDER_STRONG}; border-radius: 2px;")
            badge.setToolTip(tr("Level {n}", n=5 + 5 * level))
            g.addWidget(left, r, 0)
            g.addWidget(badge, r, 1, Qt.AlignCenter)
            g.addWidget(right, r, 2)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(2, 1)
        _fill(self.talent_lay, [holder])

    def _fill_ranks(self, s: Stats, hid: int, bracket: int) -> None:
        h = s.heroes.get(hid, {})
        rows = []
        for b in range(1, 9):
            games = h.get(f"{b}_pick") or 0
            rows.append((BRACKETS[b], (h.get(f"{b}_win") or 0) / games if games >= 100 else None, games))
        self._has_ranks = any(wr is not None for _n, wr, _g in rows)
        self.banner.set_ranks(rows if self._has_ranks else [], bracket)
        self._cards[4].setVisible(self._has_ranks and not self.banner.side_on)
        if self._has_ranks:
            chart = _RankChart()
            chart.set_data(rows, bracket)
            _fill(self.rank_lay, [chart])

    def _fill_counters(self, s: Stats, hid: int) -> None:
        traits = counters.traits_of(s.name(hid))
        if traits:
            ws = []
            for t in traits:
                head = label(t.title, "h2")
                head.setStyleSheet("font-size: 10.5pt; font-weight: 700;")
                ws.append(head)
                ws.append(_flow([_item_row(s, i, f"{s.item_cost(i):,} gold" if s.item_cost(i) else "", theme.GOLD)
                                 for i in t.items], 16))
            _fill(self.vs_lay, ws)
        else:
            _fill(self.vs_lay, [label("Nothing special: itemize for your own hero.", "faint", wrap=True)])


def _item_key(s: Stats, item_id: int) -> str:
    """dotaconstants key for an OpenDota item id."""
    if not hasattr(s, "_item_ids"):
        s._item_ids = {v.get("id"): k for k, v in s.items.items() if isinstance(v, dict)}
    return s._item_ids.get(item_id, "")


def _dispel(value) -> str:
    return {"Yes": "Any dispel removes it", "Strong Dispels Only": "Only strong dispels remove it",
            "No": "Can't be dispelled"}.get(value, "–")
