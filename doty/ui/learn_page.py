"""The Learn tab: getting better at Dota, at your level. How strong players see the game, last hits and a drill to
practice them, when to leave lane, items against heroes, drafting, fights, dying less and climbing ranked.

Laid out to be looked at more than read: a topic menu with each topic's color and item picture, search across
every tip, tips as cards with the items and heroes they talk about, and "Read to me" in Doty's own voice.

Your level (picked here, in Get started, in Settings, or from the rank on the Profile page) also sets how Doty
coaches you in a match: a player new to Dota hears the basics, a top-ranked one hears fewer reminders.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..i18n import tr
from ..knowledge import lessons
from ..knowledge.profile import Profile, rank_name
from . import theme
from .motion import fade_in
from .stats_pages import StatsService
from .visuals import HeroPortrait, ItemIcon
from .widgets import Card, FlowLayout, Switch, button, caps_font, label

INTRO = ("Tips from how strong players think and play, picked for your level. Your level also sets how Doty "
         "coaches you in a match.")
PURPLE = "#b48cff"  # drafting and the ranked mindset: thinking about the game, not playing it
# Each topic's color and picture (an item that says what it's about).
TOPIC_STYLE: dict[str, tuple[str, str]] = {
    "basics": (theme.BLUE, "tpscroll"),
    "vision": (theme.BRAND, "ward_observer"),
    "laning": (theme.GREEN, "quelling_blade"),
    "leave_lane": (theme.GREEN, "boots"),
    "farming": (theme.GOLD, "hand_of_midas"),
    "items": (theme.GOLD, "black_king_bar"),
    "draft": (PURPLE, "ultimate_scepter"),
    "fights": (theme.AMBER, "smoke_of_deceit"),
    "die_less": (theme.RED, "glimmer_cape"),
    "ranked": (PURPLE, "aegis"),
    "quick": (theme.BLUE, "blink"),
}
# Short names for the menu (the full title heads the topic itself).
SHORT = {"basics": "The basics", "vision": "Seeing the game", "laning": "Laning & last hits",
         "leave_lane": "Leaving lane", "farming": "Farming", "items": "Items vs heroes", "draft": "Drafting",
         "fights": "Fights & objectives", "die_less": "Dying less", "ranked": "Climbing ranked",
         "quick": "Quick wins"}
WIDE = 1150  # from this window width the topic menu sits on the left; narrower, it's a row of chips on top
ALL_TOPICS = "all"


def _style(key: str) -> tuple[str, str]:
    return TOPIC_STYLE.get(key, (theme.BRAND, ""))


def _rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def _matches(tip: lessons.Tip, query: str) -> bool:
    from ..i18n import tr

    q = query.strip().lower()
    return not q or q in (f"{tip.title} {tip.text} {tr(tip.title)} {tr(tip.text)} {' '.join(tip.items)} "
                          f"{' '.join(tip.heroes)}").lower()


def _big_number(text: str, color: str, pt: int) -> QLabel:
    lab = label(text)
    lab.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: {pt}pt; color: {color};'
                      " background: transparent;")
    return lab


def _clear(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


class LevelSwitch(QWidget):
    """New · Learning · Climbing · Experienced · Top."""

    def __init__(self, on_pick) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, name, who in lessons.LEVELS:
            b = QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(who)
            b.clicked.connect(lambda _c=False, k=key: on_pick(k))
            self._group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)

    def set_level(self, level: str) -> None:
        b = self.buttons.get(level)
        if b is not None and not b.isChecked():
            b.setChecked(True)


class TopicButton(QPushButton):
    """One entry in the topic menu: the topic's picture, its name, how many tips; its color when selected."""

    def __init__(self, key: str, title: str, svc: StatsService, chip: bool = False) -> None:
        super().__init__()
        self.key, self.title = key, title
        color, item = _style(key) if key != ALL_TOPICS else (theme.BRAND, "")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(title)
        self.setObjectName("topicChip" if chip else "topicBtn")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8 if chip else 12, 3, 10 if chip else 12, 3)
        lay.setSpacing(8)
        self.icon = ItemIcon(28 if chip else 30, 20 if chip else 22) if item else None
        if self.icon is not None:
            self.icon.set_item(svc.stats, item, title)
            self.icon.setAttribute(Qt.WA_TransparentForMouseEvents)
            lay.addWidget(self.icon)
        else:
            dot = QLabel("◆")
            dot.setFixedSize(28 if chip else 30, 20 if chip else 22)
            dot.setAlignment(Qt.AlignCenter)
            dot.setAttribute(Qt.WA_TransparentForMouseEvents)
            dot.setStyleSheet(f"color: {color}; font-size: 12pt; background: transparent;")
            lay.addWidget(dot)
        self.name = label(title)
        self.name.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.name.setStyleSheet("font-weight: 600; font-size: 10pt; background: transparent;")
        lay.addWidget(self.name, 0 if chip else 1)
        self.count = label("")
        self.count.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.count.setStyleSheet(f"color: {theme.MUTED}; font-size: 9pt; background: transparent;")
        lay.addWidget(self.count)
        self.setMinimumHeight(32 if chip else 36)
        bar = "border-bottom: 2px solid" if chip else "border-left: 3px solid"
        name = self.objectName()
        self.setStyleSheet(
            f"QPushButton#{name} {{ background: {theme.SURFACE if chip else 'transparent'}; border: none;"
            f" {bar} transparent; border-radius: 2px; }}"
            f"QPushButton#{name}:hover {{ background: {theme.SURFACE_HOVER}; }}"
            f"QPushButton#{name}:checked {{ background: {_rgba(color, 0.13)}; {bar} {color}; }}"
            f"QPushButton#{name}:disabled {{ background: transparent; }}")

    def set_count(self, n: int) -> None:
        self.count.setText(str(n) if n else "")
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802 (a QPushButton sizes itself from its own text, which is empty here)
        return self.layout().sizeHint()

    def minimumSizeHint(self):  # noqa: N802
        hint = self.layout().sizeHint()
        return hint if self.objectName() == "topicChip" else hint.boundedTo(hint.__class__(140, hint.height()))

    def refresh_art(self, svc: StatsService) -> None:
        if self.icon is not None:
            self.icon.set_item(svc.stats, _style(self.key)[1], self.title)


class TipCard(QFrame):
    """A tip: the items and heroes it's about, its title in the topic's color, then the advice."""

    def __init__(self, tip: lessons.Tip, color: str, svc: StatsService) -> None:
        super().__init__()
        self.tip = tip
        self.color = QColor(color)
        self.setObjectName("tipCard")
        self.setStyleSheet(f"QFrame#tipCard {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};"
                           " border-radius: 3px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 14, 13)
        lay.setSpacing(7)
        pics = [(k, "item") for k in tip.items] + [(h, "hero") for h in tip.heroes]
        if pics:
            row = QHBoxLayout()
            row.setSpacing(6)
            stats = svc.stats
            for key, kind in pics:
                if kind == "item":
                    w = ItemIcon(44, 32)
                    w.set_item(stats, key)
                else:
                    w = HeroPortrait(57, 32, radius=3)
                    hid = stats.find(key) if stats.loaded else None
                    w.set_hero(stats, hid, key)
                row.addWidget(w)
            row.addStretch(1)
            lay.addLayout(row)
        head = label((tip.title or "Tip").upper())
        head.setFont(caps_font(10.5, 800, 1.2))
        head.setStyleSheet(f"color: {color}; background: transparent;")
        lay.addWidget(head)
        body = label(tip.text, wrap=True)
        body.setStyleSheet(f"color: {theme.TEXT}; font-size: 10pt; background: transparent;")
        lay.addWidget(body)
        lay.addStretch(1)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.fillRect(QRectF(0, 0, 3, self.height()), self.color)  # the topic's color down the left edge
        p.end()


class TopicSection(QWidget):
    """A topic: its picture, title and blurb, "Read to me", then its tips as cards in a grid."""

    def __init__(self, page: "LearnPage", topic: lessons.Topic, tips: list[lessons.Tip]) -> None:
        super().__init__()
        self.page, self.topic, self.tips = page, topic, tips
        color, item = _style(topic.key)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        if item:
            pic = ItemIcon(48, 35)
            pic.set_item(page.svc.stats, item, topic.title)
            head.addWidget(pic, 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        title = label(topic.title.upper())
        title.setFont(caps_font(15, 800, 1.4))
        title.setStyleSheet(f"color: {color};")
        texts.addWidget(title)
        texts.addWidget(label(topic.blurb, "muted", wrap=True))
        head.addLayout(texts, 1)
        read = button("Read to me", "ghost", theme.Glyph.VOLUME, "Doty reads these tips out loud")
        read.clicked.connect(lambda: page.read([f"{topic.title}."] + [f"{t.title}. {t.text}" if t.title else t.text
                                                                       for t in tips]))
        head.addWidget(read, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(10)
        lay.addLayout(self.grid)
        self.cards = [TipCard(t, color, page.svc) for t in tips]
        for c in self.cards:
            c.setParent(self)  # (a card shown before it has a parent pops up as its own little window)
        self.columns = 0
        self.query = ""

    def set_columns(self, n: int, force: bool = False) -> None:
        if n == self.columns and not force:
            return
        self.columns = n
        for c in self.cards:
            self.grid.removeWidget(c)
        shown = [c for c in self.cards if _matches(c.tip, self.query)]
        for i, c in enumerate(shown):
            self.grid.addWidget(c, i // n, i % n)
        for c in self.cards:
            c.setVisible(c in shown)
        for col in range(3):
            self.grid.setColumnStretch(col, 1 if col < n else 0)

    def apply_filter(self, query: str) -> int:
        """Show only the tips matching the search, laid out again without gaps. Returns how many are shown."""
        self.query = query
        self.set_columns(self.columns or 1, force=True)
        n = sum(1 for c in self.cards if _matches(c.tip, query))
        self.setVisible(n > 0)
        return n


class LearnPage(QWidget):
    def __init__(self, ctl, service: StatsService, toast=None) -> None:
        super().__init__()
        self.ctl, self.svc, self.toast = ctl, service, toast
        self.setObjectName("page")
        self.topic = ALL_TOPICS
        self.query = ""
        self.rank_tier: int | None = None
        self._asked_rank = 0
        self._shown_level = ""
        self._shown_key = None
        self._columns = 2
        self._wide: bool | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(34, 26, 34, 16)
        outer.setSpacing(12)
        top = QHBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label("Learn", "h1"))
        head.addWidget(label(INTRO, "muted", wrap=True))
        top.addLayout(head, 1)
        self.switch = LevelSwitch(self.pick)
        top.addWidget(self.switch, 0, Qt.AlignTop)
        outer.addLayout(top)

        tools = QHBoxLayout()
        tools.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search tips: last hits, Roshan, Black King Bar, Phantom Assassin…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        tools.addWidget(self.search, 1)
        self.every_level = Switch()
        self.every_level.setToolTip("Show the tips for every level, not just yours")
        self.every_level.toggled.connect(lambda _on: self.render(force=True))
        tools.addWidget(self.every_level, 0, Qt.AlignVCenter)
        every = label("Every level", "muted")
        every.setToolTip(self.every_level.toolTip())
        tools.addWidget(every, 0, Qt.AlignVCenter)
        tools.addSpacing(6)
        self.stop_btn = button("Stop reading", "ghost", theme.Glyph.STOP)
        self.stop_btn.clicked.connect(self._stop_reading)
        self.stop_btn.hide()
        tools.addWidget(self.stop_btn)
        outer.addLayout(tools)

        # narrow windows: the topics as a row of chips above the tips
        self.chips_holder = QWidget()
        self.chips = FlowLayout(self.chips_holder, 6)
        outer.addWidget(self.chips_holder)

        split = QHBoxLayout()
        split.setSpacing(20)
        self.rail = QWidget()
        self.rail.setFixedWidth(236)
        rail_lay = QVBoxLayout(self.rail)
        rail_lay.setContentsMargins(0, 2, 0, 0)
        rail_lay.setSpacing(2)
        rail_lay.addWidget(label("TOPICS", "eyebrow"))
        rail_lay.addSpacing(4)
        self.rail_list = QVBoxLayout()
        self.rail_list.setSpacing(2)
        rail_lay.addLayout(self.rail_list)
        rail_lay.addStretch(1)
        split.addWidget(self.rail, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 12, 12)
        self.body.setSpacing(18)
        self.scroll.setWidget(inner)
        split.addWidget(self.scroll, 1)
        outer.addLayout(split, 1)

        self.plan_card = Card(margins=(20, 16, 20, 16), spacing=10)
        self.lh_card = Card(margins=(20, 16, 20, 16), spacing=10)
        self.body.addWidget(self.plan_card)
        self.body.addWidget(self.lh_card)
        self.topics = QVBoxLayout()
        self.topics.setSpacing(28)
        self.body.addLayout(self.topics)
        self.empty = label("", "muted", wrap=True)
        self.empty.hide()
        self.body.addWidget(self.empty)
        self.body.addStretch(1)

        self._menu_group = QButtonGroup(self)
        self._menu_group.setExclusive(True)
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)
        self.menu: dict[str, TopicButton] = {}
        self.chip_buttons: dict[str, TopicButton] = {}
        for key, title in [(ALL_TOPICS, "All topics")] + [(t.key, SHORT.get(t.key, t.title)) for t in lessons.TOPICS]:
            for group, store, holder, chip in ((self._menu_group, self.menu, self.rail_list, False),
                                               (self._chip_group, self.chip_buttons, self.chips, True)):
                b = TopicButton(key, title, service, chip=chip)
                b.clicked.connect(lambda _c=False, k=key: self.pick_topic(k))
                group.addButton(b)
                store[key] = b
                holder.addWidget(b)
        self.menu[ALL_TOPICS].setChecked(True)
        self.chip_buttons[ALL_TOPICS].setChecked(True)

        self._read_timer = QTimer(self)
        self._read_timer.setInterval(500)
        self._read_timer.timeout.connect(self._poll_reading)
        service.result.connect(self._on_result)
        service.ready.connect(self._on_stats)
        ctl.changed.connect(self._sync)
        self._fit()
        self.render()

    # level -------------------------------------------------------------------------------------------------
    @property
    def level(self) -> str:
        lv = getattr(self.ctl.cfg.coach, "level", "some")
        return lv if lv in lessons.LEVEL_KEYS else "some"

    def pick(self, level: str) -> None:
        """You picked a level: Doty coaches you at it from now on (and stops following your rank)."""
        self.ctl.set_level(level, from_rank=False)
        name = next((f"{n} ({w})" for k, n, w in lessons.LEVELS if k == level), level)
        if self.toast is not None:
            self.toast.show_message(tr("Doty now coaches you as {name}.", name=tr(name)))
        self.render()

    def _sync(self) -> None:
        if self.level != self._shown_level:
            self.render()

    # topics and search --------------------------------------------------------------------------------------
    def pick_topic(self, key: str) -> None:
        self.topic = key
        for group in (self.menu, self.chip_buttons):
            if key in group:
                group[key].setChecked(True)
        self.render(force=True)
        self.scroll.verticalScrollBar().setValue(0)

    def _apply_search(self) -> None:
        """Filter the cards already there (no rebuild: typing doesn't make the page flash)."""
        self.query = self.search.text().strip()
        if self.topic != ALL_TOPICS or not self._sections():
            self.render(force=True)
            return
        searching = bool(self.query)
        self.plan_card.setVisible(not searching)
        self.lh_card.setVisible(not searching)
        counts = {s.topic.key: s.apply_filter(self.query) for s in self._sections()}
        self._set_counts(counts)
        total = sum(counts.values())
        self.empty.setVisible(total == 0)
        if total == 0:
            self.empty.setText(f'No tips match "{self.query}". Try another word, or turn on Every level.')
        self._shown_key = self._key()

    # reading aloud ------------------------------------------------------------------------------------------
    def read(self, lines: list[str]) -> None:
        why = self.ctl.read_aloud(lines)
        if why:
            if self.toast is not None:
                self.toast.show_message(why)
            return
        self.stop_btn.show()
        self._read_timer.start()

    def _stop_reading(self) -> None:
        self.ctl.stop_reading()
        self.stop_btn.hide()
        self._read_timer.stop()

    def _poll_reading(self) -> None:
        if not self.ctl.reading():
            self.stop_btn.hide()
            self._read_timer.stop()

    # your rank (from the Profile page's account) ---------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.svc.ensure_loaded()  # (item and hero pictures)
        acc = int(getattr(self.ctl.cfg.app, "account_id", 0) or 0)
        if acc and self.rank_tier is None and self._asked_rank != acc:
            self._asked_rank = acc
            self.svc.run("learn_rank", lambda s, a=acc: Profile(s).player(a))
        self.render()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if not self.ctl.reading():
            self._read_timer.stop()

    def _on_stats(self) -> None:
        for b in list(self.menu.values()) + list(self.chip_buttons.values()):
            b.refresh_art(self.svc)
        self.render(force=True)  # (hero portraits need the hero list)

    def _on_result(self, name: str, value) -> None:
        player = None
        if name == "learn_rank" and isinstance(value, dict):
            player = value
        elif (name == "profile" and isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], dict)
              and value[0] == getattr(self.ctl.cfg.app, "account_id", 0)):
            player = value[1]  # (your own account only: looking at another doesn't change your level)
        if player is None:
            return
        tier = player.get("rank_tier")
        if not tier:
            return
        self.rank_tier = int(tier)
        coach = self.ctl.cfg.coach
        if getattr(coach, "level_from_rank", True):
            level = lessons.level_for_rank(self.rank_tier)
            bracket = min(8, self.rank_tier // 10) if not coach.bracket else None
            if level != coach.level or bracket is not None:
                self.ctl.set_level(level, bracket=bracket)
        self.render()

    # layout -------------------------------------------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        wide = self.width() >= WIDE
        if wide != self._wide:
            self._wide = wide
            self.rail.setVisible(wide)
            self.chips_holder.setVisible(not wide)
        width = self.width() - (236 + 20 if wide else 0) - 80
        cols = 3 if width >= 1400 else 2 if width >= 700 else 1
        if cols != self._columns or any(isinstance(w, TopicSection) and w.columns != cols for w in self._sections()):
            stacked = (self._columns == 1) != (cols == 1)
            self._columns = cols
            for w in self._sections():
                w.set_columns(cols)
            if stacked and self.plan_card.isVisibleTo(self):
                self._fill_plan(self.level, self._last_game())  # (the plan's tiles: side by side or stacked)

    def _sections(self) -> list[TopicSection]:
        return [w for i in range(self.topics.count()) if isinstance(w := self.topics.itemAt(i).widget(), TopicSection)]

    # content ------------------------------------------------------------------------------------------------
    def _topics(self) -> list[tuple[lessons.Topic, list[lessons.Tip]]]:
        """The topics and tips to show: your level's (or every level's), matching the search."""
        if self.every_level.isChecked():
            rows = [(t, list(t.tips)) for t in lessons.TOPICS]
        else:
            rows = lessons.tips_for(self.level)
        return [(t, tips) for t, tips in ((t, [x for x in tips if _matches(x, self.query)]) for t, tips in rows)
                if tips]

    def _key(self) -> tuple:
        return (self.level, self.rank_tier, tuple(self._last_game()),
                getattr(self.ctl.cfg.coach, "level_from_rank", True), self.topic, self.query,
                self.every_level.isChecked())

    def _set_counts(self, counts: dict[str, int]) -> None:
        """Each topic's tip count; a topic with none to show is hidden (not a button that does nothing)."""
        for group in (self.menu, self.chip_buttons):
            for k, b in group.items():
                b.set_count(sum(counts.values()) if k == ALL_TOPICS else counts.get(k, 0))
                b.setVisible(k == ALL_TOPICS or counts.get(k, 0) > 0 or k == self.topic)

    def render(self, force: bool = False) -> None:
        level = self.level
        last = self._last_game()
        key = self._key()
        if key == self._shown_key and not force:
            return  # (nothing changed: no rebuild, no flicker)
        self._shown_key = key
        self._shown_level = level
        self.switch.set_level(level)
        shown = self._topics()
        self._set_counts({t.key: len(tips) for t, tips in shown})

        overview = self.topic == ALL_TOPICS and not self.query
        self.plan_card.setVisible(overview)
        self.lh_card.setVisible(overview)
        if overview:
            self._fill_plan(level, last)
            self._fill_last_hits(level)

        _clear(self.topics)
        picked = [(t, tips) for t, tips in shown if self.topic in (ALL_TOPICS, t.key)]
        for t, tips in picked:
            section = TopicSection(self, t, tips)
            section.set_columns(self._columns)
            self.topics.addWidget(section)
            fade_in(section)
        self.empty.setVisible(not picked)
        if not picked:
            self.empty.setText(f'No tips match "{self.query}". Try another word, or turn on Every level.'
                               if self.query else "No tips for this topic at your level. Turn on Every level.")
        self._fit()

    def _card_head(self, card: Card, title: str, hint: str, read_lines: list[str] | None = None) -> None:
        row = QHBoxLayout()
        row.addWidget(label(title.upper(), "section"))
        row.addStretch(1)
        if hint:
            h = label(hint, "faint")
            h.setStyleSheet("font-size: 8.5pt;")
            row.addWidget(h)
        if read_lines:
            read = button("Read to me", "ghost", theme.Glyph.VOLUME, "Doty reads this out loud")
            read.clicked.connect(lambda: self.read(read_lines))
            row.addWidget(read)
        card.body.addLayout(row)

    def _fill_plan(self, level: str, last: list[str]) -> None:
        card = self.plan_card
        _clear(card.body)
        name, who = next(((n, w) for k, n, w in lessons.LEVELS if k == level), ("Climbing", ""))
        focus = list(lessons.FOCUS[level])
        self._card_head(card, "Your plan", "what wins the most games at your level",
                        [f"Your plan, as {name}."] + focus + (["From your last game."] + last[:2] if last else []))
        from ..i18n import tr

        where = (tr("Your rank: <b>{rank}</b>. Doty coaches you as <b>{name}</b> ({who}).",
                    rank=rank_name(self.rank_tier), name=tr(name), who=tr(who))
                 if self.rank_tier and getattr(self.ctl.cfg.coach, "level_from_rank", True)
                 else tr("Doty coaches you as <b>{name}</b> ({who}). Pick another level at the top.",
                         name=tr(name), who=tr(who)))
        intro = label(where, "muted", wrap=True)
        intro.setTextFormat(Qt.RichText)
        card.body.addWidget(intro)
        tiles = QGridLayout()
        tiles.setHorizontalSpacing(10)
        tiles.setVerticalSpacing(10)
        colors = (theme.BRAND, theme.GREEN, theme.AMBER)
        stacked = self._columns == 1
        for i, text in enumerate(focus):
            tile = QFrame()
            tile.setObjectName("focusTile")
            tile.setStyleSheet(f"QFrame#focusTile {{ background: {_rgba(colors[i], 0.08)}; border-top: 2px solid "
                               f"{colors[i]}; border-radius: 2px; }}")
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(14, 8, 14, 12)
            tl.setSpacing(2)
            tl.addWidget(_big_number(str(i + 1), colors[i], 20))
            body = label(text, wrap=True)
            body.setStyleSheet("font-size: 10pt; background: transparent;")
            tl.addWidget(body)
            tl.addStretch(1)
            if stacked:
                tiles.addWidget(tile, i, 0)
            else:
                tiles.addWidget(tile, 0, i)
                tiles.setColumnStretch(i, 1)
        card.body.addLayout(tiles)
        if last:
            card.body.addWidget(label("FROM YOUR LAST GAME", "eyebrow"))
            for text in last[:2]:
                row = label(f'<span style="color:{theme.AMBER}">›</span>&nbsp; {text}', wrap=True)
                row.setTextFormat(Qt.RichText)
                row.setStyleSheet("font-size: 10pt;")
                card.body.addWidget(row)

    def _fill_last_hits(self, level: str) -> None:
        card = self.lh_card
        _clear(card.body)
        start, goal = lessons.LAST_HIT_TARGETS[level]
        drill = list(lessons.DRILL) if level in lessons.DRILL_LEVELS else []
        self._card_head(card, "Last hits", "where your gold comes from",
                        [tr("Last hits by ten minutes, as a core: start at {start}, aim for {goal}.", start=start, goal=goal)] + drill)
        row = QHBoxLayout()
        row.setSpacing(30)
        for value, caption, color in ((start, "START AT", theme.TEXT), (goal, "AIM FOR", theme.GREEN)):
            v = QVBoxLayout()
            v.setSpacing(0)
            v.addWidget(_big_number(str(value), color, 30))
            v.addWidget(label(tr(caption) + " · " + tr("BY 10:00"), "eyebrow"))
            row.addLayout(v)
        tip = label("As a core. Supports earn from pulls, stacks and assists instead; Doty's laning gauge tracks "
                    "your pace in the first ten minutes of a real game.", "muted", wrap=True)
        row.addWidget(tip, 1)
        pics = QHBoxLayout()
        pics.setSpacing(6)
        for item in ("quelling_blade", "tango", "magic_wand"):
            ic = ItemIcon(44, 32)
            ic.set_item(self.svc.stats, item)
            pics.addWidget(ic)
        row.addLayout(pics)
        card.body.addLayout(row)
        if drill:
            card.body.addWidget(label("PRACTICE DRILL · DEMO HERO", "eyebrow"))
            steps = QGridLayout()
            steps.setHorizontalSpacing(12)
            steps.setVerticalSpacing(6)
            for i, text in enumerate(drill):
                steps.addWidget(_big_number(str(i + 1), theme.BRAND, 14), i, 0, Qt.AlignTop)
                t = label(text, wrap=True)
                t.setStyleSheet("font-size: 10pt;")
                steps.addWidget(t, i, 1)
            steps.setColumnMinimumWidth(0, 20)
            steps.setColumnStretch(1, 1)
            card.body.addLayout(steps)

    @staticmethod
    def _last_game() -> list[str]:
        """The post-game review's points to work on, from your latest game (none if there's no review)."""
        try:
            from ..review import latest_review

            review = latest_review()
        except Exception:
            return []
        return list(review.focus) if review is not None else []
