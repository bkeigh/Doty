"""The Coach page: Doty's home. It follows what you're doing.

- **Not set up yet:** Get started, one step at a time: connect Dota, add an AI key, try the demo.
- **Before a match:** your last game's recap, a box to plan your next hero, the keys to remember.
- **Hero selection:** the top picks from the Draft page (the window usually opens Draft by itself).
- **In a match:** your game is the header (status, clock, all-set line), then what to buy next, what Doty
  sees, and the feed.
- **Paused:** one Resume button.

Problems show as chips that fix themselves in place (paste a key right there, unmute, jump to the setting).
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (QBoxLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout,
                               QWidget)

from ..i18n import tr
from ..rules.base import mmss
from . import theme
from .controller import Controller
from .live import DEMO_TEXT, BuildPanel, GamePanel, ScreenView, TimersPanel
from .motion import fade_in
from .pages import (COACH_PRESETS, KEYS_URL, coach_preset, pretty_key, pretty_model, preset_cost, save_claude_key,
                    short_mic, windows_voice_name)
from .stats_pages import PageStack
from .theme import AMBER, BLUE, BRAND, GREEN, GREY, RED, Glyph
from .visuals import HeroPortrait
from .widgets import ArtBanner, Card, Dot, Feed, button, caps_font, fit_width, icon_label, label, set_button, set_icon_color

# Pages the Coach page can send you to (the window's page order), and Settings tabs.
DRAFT, PROFILE, LEARN = 1, 3, 6  # (the window's page numbers)
TAB_AI, TAB_VOICE, TAB_TALK = 0, 1, 2
# What the left column shows.
SETUP_VIEW, HUB_VIEW, DRAFT_VIEW, LIVE_VIEW = range(4)


def clipboard_key() -> str:
    """A Claude API key you just copied, if that's what's on the clipboard (it never leaves this PC)."""
    try:
        text = QGuiApplication.clipboard().text().strip()
    except Exception:
        return ""
    return text if text.startswith("sk-ant-") and len(text) > 30 and " " not in text else ""


# --------------------------------------------------------------------------- pieces


class StatusChip(QWidget):
    """One part of the status strip: an icon colored by state, a short value, a detail line and, when
    there's something to do, the fix ("Paste key ›")."""

    clicked = Signal()

    def __init__(self, glyph: str, title: str) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(10)
        self.icon = icon_label(glyph, 16, theme.TEXT_2)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.caption = label(title, "faint")
        self.caption.setStyleSheet("font-size: 8.5pt;")
        texts.addWidget(self.caption)
        self.value = label("—")
        self.value.setStyleSheet("font-size: 10.5pt; font-weight: 700;")
        texts.addWidget(self.value)
        self.detail = label("", "muted")
        self.detail.setStyleSheet("font-size: 8.5pt;")
        texts.addWidget(self.detail)
        lay.addLayout(texts, 1)
        self.fix = label("")
        self.fix.setStyleSheet(f"color: {theme.BRAND_BRIGHT}; font-size: 9pt; font-weight: 700;")
        lay.addWidget(self.fix, 0, Qt.AlignVCenter)
        self.state = "off"
        self._hovered = False

    def set(self, value: str, detail: str, state: str, fix: str = "") -> None:
        self.state = state
        self.value.setText(value)
        self.detail.setText(detail)
        self.detail.setToolTip(detail)
        self.fix.setText(f"{fix} ›" if fix else "")
        color = theme.STATE_COLORS.get(state, theme.GREY)
        set_icon_color(self.icon, color if state in ("ok", "warn", "bad", "info") else theme.TEXT_2)
        tone = {"warn": theme.AMBER, "bad": theme.RED}.get(state)
        self.value.setStyleSheet(f"font-size: 10.5pt; font-weight: 700; color: {tone or theme.TEXT};")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SURFACE_HOVER))
            p.drawRect(QRectF(self.rect()))
            p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StepNumber(QWidget):
    """A numbered circle for the Get started steps; a green check once that step is done."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.setFixedSize(30, 30)
        self.n, self.done = n, False

    def set_done(self, done: bool) -> None:
        self.done = done
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(1, 1, 28, 28)
        color = QColor(theme.GREEN if self.done else theme.BRAND)
        fill = QColor(color)
        fill.setAlphaF(0.14)
        p.setBrush(fill)
        p.setPen(QPen(color, 1.3))
        p.drawEllipse(r)
        f = QFont(theme.ICON_FONT if self.done else theme.FONT)
        f.setPixelSize(12 if self.done else 13)
        if not self.done:
            f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(color if self.done else QColor(theme.BRAND_BRIGHT))
        p.drawText(r, Qt.AlignCenter, Glyph.CHECK if self.done else str(self.n))
        p.end()


class Step(QWidget):
    """One Get started step: number, title, a line of detail, room under it, and its button."""

    def __init__(self, n: int, title: str, detail: str, action: QWidget | None = None) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(16)
        self.number = StepNumber(n)
        lay.addWidget(self.number, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        self.title = label(title)
        self.title.setStyleSheet("font-size: 11pt; font-weight: 700;")
        texts.addWidget(self.title)
        self.detail = label(detail, "muted", wrap=True)
        texts.addWidget(self.detail)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(6)
        texts.addLayout(self.extra)
        lay.addLayout(texts, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        lay.addLayout(self.actions)
        if action is not None:
            self.actions.addWidget(action, 0, Qt.AlignVCenter)
        self.action = action


class KeyEntry(QWidget):
    """Paste a Claude API key and save it, with a shortcut for a key you just copied."""

    saved = Signal()

    def __init__(self, ctl: Controller, toast, kind: str = "accent") -> None:
        super().__init__()
        self.ctl, self.toast = ctl, toast
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.Password)
        self.field.setPlaceholderText("Paste your key: sk-ant-…")
        self.field.setMinimumWidth(240)
        self.field.returnPressed.connect(self._save)
        row.addWidget(self.field, 1)
        self.save = button("Save key", kind, Glyph.CHECK)
        self.save.clicked.connect(self._save)
        row.addWidget(self.save)
        v.addLayout(row)
        links = QHBoxLayout()
        links.setSpacing(14)
        self.use_copied = button("Use the key you copied", "link")
        self.use_copied.clicked.connect(lambda: (self.field.setText(clipboard_key()), self._save()))
        links.addWidget(self.use_copied)
        get = button("Get a key", "link")
        get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(KEYS_URL)))
        links.addWidget(get)
        links.addStretch(1)
        v.addLayout(links)
        self.refresh_clipboard()

    def refresh_clipboard(self) -> None:
        self.use_copied.setVisible(bool(clipboard_key()))

    def _save(self) -> None:
        if self.toast is not None and save_claude_key(self.ctl, self.toast, self.field.text()):
            self.field.clear()
            self.saved.emit()


class Popover(QFrame):
    """A small floating box under the thing you clicked; closes when you click elsewhere."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("popover")
        self.setStyleSheet(f"#popover {{ background: {theme.SURFACE_2}; border: 1px solid rgba(63, 212, 255, 0.45);"
                           " border-radius: 2px; }")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 12)
        self.lay.setSpacing(8)

    def open_under(self, anchor: QWidget) -> None:
        """Under the thing you clicked, or above it when there's no room below (the controls sit at the
        bottom of the window)."""
        self.adjustSize()
        at = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        window = anchor.window()
        bottom = window.mapToGlobal(QPoint(0, window.height())).y() if window is not None else None
        screen = anchor.screen().availableGeometry() if anchor.screen() is not None else None
        if screen is not None:
            bottom = min(bottom, screen.bottom()) if bottom is not None else screen.bottom()
        if bottom is not None and at.y() + self.height() > bottom:
            at.setY(anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6)
        if screen is not None:  # and always on the screen
            at.setX(max(screen.left() + 8, min(at.x(), screen.right() - self.width() - 8)))
            at.setY(max(screen.top() + 8, min(at.y(), screen.bottom() - self.height() - 8)))
        self.move(at)
        self.show()


class _PresetRow(QWidget):
    """One choice in the coaching popover: a check, a name, what it means and what it costs."""

    clicked = Signal()

    def __init__(self, name: str, detail: str, cost: str, current: bool, enabled: bool = True) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        self._enabled, self._hovered = enabled, False
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 7, 10, 7)
        h.setSpacing(10)
        check = icon_label(Glyph.CHECK if current else "", 12, theme.BRAND_BRIGHT)
        h.addWidget(check, 0, Qt.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        top = QHBoxLayout()
        title = label(name)
        title.setStyleSheet(f"font-weight: 700; font-size: 10pt; color: {theme.BRAND_BRIGHT if current else theme.TEXT};")
        top.addWidget(title)
        top.addStretch(1)
        if cost:
            c = label(cost, "faint")
            c.setStyleSheet("font-size: 8.5pt;")
            top.addWidget(c)
        texts.addLayout(top)
        d = label(detail, "muted")
        d.setStyleSheet("font-size: 9pt;")
        texts.addWidget(d)
        h.addLayout(texts, 1)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = self._enabled
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(63, 212, 255, 46))
            p.drawRect(QRectF(self.rect()))
            p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._enabled and event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()


class KeysCard(Card):
    """The keys to remember, kept in step with your settings."""

    def __init__(self, ctl: Controller) -> None:
        super().__init__(margins=(20, 12, 20, 12), spacing=6)
        self.ctl = ctl
        from .motion import Collapsible

        self.fold = Collapsible("Keys to remember", "", expanded=False)
        self.body.addWidget(self.fold)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        self.fold.body_lay.addLayout(self.grid)
        self._shown = None
        self.refill()

    def refill(self) -> None:
        cfg = self.ctl.cfg
        hk = cfg.hotkeys
        keys = ([(f"Hold {pretty_key(cfg.listen.key)}", "Ask anything out loud, then let go")]
                if cfg.listen.enabled else []) + [
            (hk.ask.title(), "What should I do right now?"),
            (hk.plan.title(), "A 5-minute plan and item build"),
            (hk.mute.title(), "Mute or unmute Doty's voice"),
            (hk.toggle_overlay.title(), "Hide or show the cards over Dota")]
        if keys == self._shown:
            return
        self._shown = keys
        from ..i18n import tr

        self.fold.subtitle.setText(" · ".join(f"{combo}: {tr(what).split(',')[0].rstrip('?？')}"
                                              for combo, what in keys[:2]))
        self.fold.subtitle.setVisible(True)
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for i, (combo, what) in enumerate(keys):
            cap = QLabel(combo)
            cap.setStyleSheet(f"color: {theme.BRAND_BRIGHT}; background: {theme.SURFACE_2}; border: 1px solid "
                              f"{theme.BORDER_STRONG}; border-bottom: 2px solid {theme.BORDER_STRONG};"
                              " border-radius: 2px; padding: 2px 8px; font-weight: 700; font-size: 9pt;")
            self.grid.addWidget(cap, i, 0, Qt.AlignLeft)  # (one per line: it reads in a narrow column too)
            self.grid.addWidget(label(what, "muted"), i, 1)


# --------------------------------------------------------------------------- the page


class CoachPage(QWidget):
    go_setup = Signal()
    navigate = Signal(int)  # a page index
    install_gsi = Signal()  # fix Dota's connection file right from here
    copy_launch = Signal()  # copy the Steam launch option
    recheck_setup = Signal()
    open_settings_tab = Signal(int)
    open_hero = Signal(str)

    def __init__(self, ctl: Controller, stats_service=None, toast=None) -> None:
        super().__init__()
        self.ctl, self.svc, self.toast = ctl, stats_service, toast
        self.setObjectName("page")
        self._setup_done, self._setup_total = 0, 3
        self._next_key, self._next_label = "", ""
        self._checks: dict = {}
        self._keys_cards: list[KeysCard] = []
        self._phase = ""
        self._coach_ok = False
        self._needs_key = False
        self._lines: dict[int, dict] = {}  # what Doty said during the current match
        self._last_game: dict | None = None
        self._last_clock = 0
        self.recap: dict | None = None
        self._draft: dict | None = None
        outer = self._outer = QBoxLayout(QBoxLayout.LeftToRight, self)  # the left column beside the rail
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(16)
        column = self._column = QVBoxLayout()
        column.setSpacing(14)

        # banner (everything but a live match, where your game is up top) --------------------------
        self.banner = ArtBanner()
        row = QHBoxLayout()
        row.setSpacing(18)
        texts = QVBoxLayout()
        texts.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = Dot(6)
        top.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.eyebrow = label("STATUS", "gold")
        top.addWidget(self.eyebrow, 0, Qt.AlignVCenter)
        top.addStretch(1)
        texts.addLayout(top)
        self.title = label("Doty is paused", "heroTitle")
        self.subtitle = label("", "muted", wrap=True)
        self.subtitle.setStyleSheet("font-size: 10.5pt;")
        texts.addWidget(self.title)
        texts.addWidget(self.subtitle)
        self.ready_line = label("", "muted")
        self.ready_line.setTextFormat(Qt.RichText)
        self.ready_line.setStyleSheet("font-size: 9.5pt;")
        self.ready_line.hide()
        texts.addWidget(self.ready_line)
        row.addLayout(texts, 1)
        self.power = button("Resume", "start", Glyph.PLAY)
        self.power.clicked.connect(self._resume)
        row.addWidget(self.power, 0, Qt.AlignVCenter)
        self.banner.body.addLayout(row)
        column.addWidget(self.banner)

        # status strip: only when something needs a look -------------------------------------------
        self.strip = Card(margins=(10, 8, 10, 8), spacing=0)
        strip = QHBoxLayout()
        strip.setSpacing(6)
        self.t_game = StatusChip(Glyph.CONNECT, "Game data")
        self.t_game.setToolTip("Exact numbers straight from Dota (HP, gold, items, clock) about 10 times a second. "
                               "This is what makes alerts instant.")
        self.t_coach = StatusChip(Glyph.ROBOT, "AI coach")
        self.t_voice = StatusChip(Glyph.VOLUME, "Voice")
        self.t_talk = StatusChip(Glyph.MIC, "Push-to-talk")
        self.t_game.clicked.connect(lambda: self.install_gsi.emit() if self._next_key == "gsi" else self.go_setup.emit())
        self.t_coach.clicked.connect(lambda: self._fix_coach(self.t_coach))
        from ..i18n import tr

        self.t_voice.clicked.connect(lambda: self.ctl.toggle_mute() if self.t_voice.value.text() == tr("Muted")
                                     else self.open_settings_tab.emit(TAB_VOICE))
        self.t_talk.clicked.connect(lambda: self.open_settings_tab.emit(TAB_TALK))
        for i, t in enumerate((self.t_game, self.t_coach, self.t_voice, self.t_talk)):
            if i:
                line = QFrame()
                line.setFixedWidth(1)
                line.setStyleSheet(f"background: {theme.BORDER};")
                strip.addWidget(line)
            strip.addWidget(t, 1)
        self.strip.body.addLayout(strip)
        self.strip.hide()
        column.addWidget(self.strip)

        # what fits the moment: Get started, the hub, top picks, or (in a match) your hero ------------
        self.left = PageStack()  # as tall as the view showing, so what Doty sees sits right by it
        self.welcome = self._build_setup_view()
        self.hub = self._build_hub_view()
        self.draft_view = self._build_draft_view()
        self.game = GamePanel(stats_service)
        self.game.open_hero.connect(self.open_hero.emit)
        self.live_view = self.game
        for w in (self.welcome, self.hub, self.draft_view, self.game):
            self.left.addWidget(w)
        # What Doty sees: above your hero in a match, under the view otherwise, whenever Dota is open
        self.screen = ScreenView()
        self.screen.hide()
        column.addWidget(self.recap_card)
        column.addWidget(self.left, 0)
        column.addWidget(self.screen, 1)
        column.addStretch(1)  # takes the room the picture doesn't
        outer.addLayout(column, 1)

        # the rail: next up and what to buy (in a match), the feed, and the controls -------------------
        self.rail = QWidget()
        rail = QVBoxLayout(self.rail)
        rail.setContentsMargins(0, 0, 0, 0)
        rail.setSpacing(12)
        self.timers = TimersPanel()
        self.build = BuildPanel(stats_service)
        self.build.on_skip = ctl.skip_item  # "not this game" on an item: it leaves this match's build
        self._pair = QBoxLayout(QBoxLayout.TopToBottom)  # side by side on a tall window
        self._pair.setSpacing(12)
        self._pair.addWidget(self.timers, 1)
        self._pair.addWidget(self.build, 1)
        rail.addLayout(self._pair)

        feed_card = self.feed_card = Card(margins=(16, 12, 8, 4), spacing=2, accent=theme.BRAND)
        head = QHBoxLayout()
        head.setSpacing(10)
        self.feed_title = label("LIVE FEED", "section")
        head.addWidget(self.feed_title)
        head.addStretch(1)
        clear = button("Clear", "link")
        head.addWidget(clear)
        feed_card.body.addLayout(head)
        self.feed = Feed("Nothing yet. Everything Doty says shows up here, newest first.")
        clear.clicked.connect(lambda: self.feed.clear())
        self.feed.report.connect(self._report_tip)
        feed_card.body.addWidget(self.feed, 1)
        rail.addWidget(feed_card, 1)

        self.actions = QWidget()
        acts = self._acts = QBoxLayout(QBoxLayout.TopToBottom, self.actions)  # one row on a tall window
        acts.setContentsMargins(0, 0, 0, 0)
        acts.setSpacing(6)
        hk = ctl.cfg.hotkeys
        self._ask_tip = tr("What should I do right now? ({ask}, or hold {key} and ask out loud)",
                           ask=hk.ask.title(), key=pretty_key(ctl.cfg.listen.key))
        self.b_ask = button("Ask now", "accent", Glyph.CHAT, self._ask_tip)
        self.b_plan = button("5-min plan", "ghost", Glyph.LIST, tr("A 5-minute plan plus item build ({key})", key=hk.plan.title()))
        self.b_coaching = button("Coach: Balanced", "ghost", Glyph.CHEVRON_DOWN,
                                 "How often the coach looks at your game, and when it talks")
        self.b_coaching.setLayoutDirection(Qt.RightToLeft)  # the chevron goes after the words
        self.b_coaching.clicked.connect(self._coach_menu)
        self.b_mute = button("Mute", "icon", Glyph.MUTE, tr("Mute voice ({key})", key=hk.mute.title()))
        self.b_overlay = button("Hide for now", "icon", Glyph.HIDE,
                                tr("Hide the cards over Dota until you show them again ({key})",
                                   key=hk.toggle_overlay.title()))
        self.b_demo = button("Try a demo", "ghost", Glyph.BOLT,
                             "Plays a scripted 26-minute match at 6x speed so you can see and hear Doty "
                             "without Dota.")
        self.b_pause = button("Pause", "icon", Glyph.PAUSE, "Pause Doty: nothing watches or listens until you "
                                                           "resume")
        self.b_ask.clicked.connect(self._ask_clicked)
        self.b_plan.clicked.connect(lambda: ctl.ask("plan"))
        self.b_mute.clicked.connect(lambda: ctl.toggle_mute())
        self.b_overlay.clicked.connect(lambda: ctl.toggle_overlay())
        self.b_demo.clicked.connect(lambda: ctl.stop_demo() if ctl.demo_running else ctl.start_demo())
        self.b_pause.clicked.connect(lambda: ctl.stop())
        row1, row2, row3 = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        for r in (row1, row2, row3):
            r.setSpacing(6)
        row1.addWidget(self.b_ask, 1)
        row1.addWidget(self.b_plan)
        row2.addWidget(self.b_coaching, 1)
        row2.addWidget(self.b_demo, 1)  # (side by side with the Coach menu: short labels, so both fit the rail)
        # One control for the rest: Mute, Hide and Pause live in the Coach menu ("Right now"). The buttons keep
        # their state here, out of sight, and the menu mirrors them.
        self._quick = QWidget(self.actions)
        self._quick.hide()
        quick = QHBoxLayout(self._quick)
        for b in (self.b_mute, self.b_overlay, self.b_pause):
            quick.addWidget(b)
        acts.addLayout(row1)
        acts.addLayout(row2)
        acts.addLayout(row3)
        rail.addWidget(self.actions)
        outer.addWidget(self.rail, 0)
        self._portrait: bool | None = None
        self._set_portrait(False)
        self.rail.hide()

        ctl.advice.connect(self.feed.upsert)
        ctl.advice.connect(self._note_line)
        ctl.advice.connect(self.screen.show_line)
        self.game.ready.linkActivated.connect(self._fix_link)
        if stats_service is not None:
            stats_service.ready.connect(self._attach_completer)
        self._update_steps()

    RAIL_WIDTH = 356

    def _set_portrait(self, portrait: bool) -> None:
        """A tall window (a portrait monitor) stacks the page: the picture and your hero, then next up beside
        what to buy, the feed, and the controls in one row. A wide one puts the rail on the right."""
        self._portrait = portrait
        self._outer.setDirection(QBoxLayout.TopToBottom if portrait else QBoxLayout.LeftToRight)
        self._outer.setStretch(0, 0 if portrait else 1)
        self._outer.setStretch(1, 1 if portrait else 0)
        self._pair.setDirection(QBoxLayout.LeftToRight if portrait else QBoxLayout.TopToBottom)
        self._acts.setDirection(QBoxLayout.LeftToRight if portrait else QBoxLayout.TopToBottom)
        if portrait:
            self.rail.setMinimumWidth(0)
            self.rail.setMaximumWidth(16777215)
            self.feed_card.setMinimumHeight(240)
        else:
            self.rail.setFixedWidth(self.RAIL_WIDTH if self.width() >= 1200 else 320)
            self.feed_card.setMinimumHeight(0)
        self._fit_column()

    def _fit_column(self) -> None:
        """Beside the rail in a match, your hero's panel takes the room under the picture (which stays
        16:9), up to a point; otherwise the column keeps to its content."""
        col = self._column
        live = self.left.currentIndex() == LIVE_VIEW and not self._portrait
        col.setStretch(col.indexOf(self.left), 1 if live else 0)
        col.setStretch(col.count() - 1, 0 if live else 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        portrait = self.height() > self.width() * 1.05
        if portrait != self._portrait:
            self._set_portrait(portrait)
        if portrait:
            m = self._outer.contentsMargins()
            self._outer.setContentsMargins(16, m.top(), 16, m.bottom())
        else:
            self.rail.setFixedWidth(self.RAIL_WIDTH if self.width() >= 1200 else 320)
            fit_width(self._outer, self.width(), 1800, side=20)

    # Get started --------------------------------------------------------------------------------
    def _build_setup_view(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        card = Card(margins=(24, 18, 24, 14), spacing=2)
        card.body.addWidget(label("GET STARTED", "section"))
        self.go_setup_btn = button("Install it", "accent", Glyph.REPAIR)
        self.go_setup_btn.clicked.connect(self._setup_action)
        self.recheck_link = button("Check again", "link")
        self.recheck_link.clicked.connect(lambda: self.recheck_setup.emit())
        self.step_setup = Step(1, "Connect Dota 2", "")
        self.step_setup.actions.addWidget(self.recheck_link, 0, Qt.AlignVCenter)
        self.step_setup.actions.addWidget(self.go_setup_btn, 0, Qt.AlignVCenter)
        self.key_entry = KeyEntry(self.ctl, self.toast, "ghost")
        self.key_entry.saved.connect(lambda: self.recheck_setup.emit())
        self.step_key = Step(2, "Add your AI key", "")
        self.step_key.extra.addWidget(self.key_entry)
        self.ai_btn = button("AI settings", "ghost", Glyph.SETTINGS)
        self.ai_btn.clicked.connect(lambda: self.open_settings_tab.emit(TAB_AI))
        self.skip_key = button("Not now", "link")
        self.skip_key.clicked.connect(self._skip_key)
        self.step_key.actions.addWidget(self.ai_btn, 0, Qt.AlignTop)
        self.step_key.actions.addWidget(self.skip_key, 0, Qt.AlignTop)
        self.voice_btn = button("Voice settings", "ghost", Glyph.SETTINGS)
        self.voice_btn.clicked.connect(lambda: self.open_settings_tab.emit(TAB_VOICE))
        self.step_voice = Step(3, "Pick a voice", "", self.voice_btn)
        from .learn_page import LevelSwitch

        self.level_switch = LevelSwitch(self._pick_level)
        self.step_level = Step(4, "How well do you know Dota?", "New: Doty teaches the basics out loud and uses plain "
                                                                "words. Top: short calls only. Change it any time on "
                                                                "the Learn tab.")
        self.step_level.extra.addWidget(self.level_switch, 0, Qt.AlignLeft)
        demo = button("Play demo", "ghost", Glyph.BOLT)
        demo.clicked.connect(lambda: self.ctl.start_demo())
        self.step_demo = Step(5, "See Doty in action", "A scripted 26-minute game at 6x speed: watch the alerts and "
                                                       "hear the coach without opening Dota.", demo)
        for i, s in enumerate((self.step_setup, self.step_key, self.step_voice, self.step_level, self.step_demo)):
            if i:
                line = QFrame()
                line.setFixedHeight(1)
                line.setStyleSheet(f"background: {theme.BORDER};")
                card.body.addWidget(line)
            card.body.addWidget(s)
        lay.addWidget(card)
        self._keys_cards.append(KeysCard(self.ctl))
        lay.addWidget(self._keys_cards[-1])
        lay.addStretch(1)
        return w

    def _pick_level(self, level: str) -> None:
        from ..knowledge.lessons import LEVELS

        self.ctl.set_level(level, from_rank=False)
        name = next((f"{n} ({w})" for k, n, w in LEVELS if k == level), level)
        self.toast.show_message(tr("Doty will coach you as {name}.", name=tr(name)))
        self._update_steps()

    def set_setup_progress(self, done: int, total: int) -> None:
        self._setup_done, self._setup_total = done, total
        self._update_steps()

    def set_next_step(self, key: str, name: str) -> None:
        self._next_key, self._next_label = key, name
        self._update_steps()

    def set_checks(self, by_key: dict) -> None:
        """The Setup page's latest checks, so Get started can show the exact next step."""
        self._checks = dict(by_key)
        self._update_steps()

    def _setup_action(self) -> None:
        if self._next_key == "gsi":
            self.install_gsi.emit()
        elif self._next_key == "launch":
            self.copy_launch.emit()
        else:
            self.go_setup.emit()

    def _setup_ready(self) -> bool:
        return bool(self._setup_total) and self._setup_done >= self._setup_total

    def _key_missing(self) -> bool:
        """Only a missing Claude key holds Get started open; other AI providers (and a local model) show any
        problem in the status instead."""
        c = self.ctl.cfg.coach
        key = self._checks.get("key")
        return c.enabled and c.provider == "claude" and key is not None and key.ok is not True

    def _skip_key(self) -> None:
        from ..config import save_config

        self.ctl.cfg.app.ai_step_skipped = True
        try:
            save_config(self.ctl.cfg)
        except OSError:
            pass
        self._update_steps()
        self.refresh(self.ctl.status())

    def _voice_step(self) -> tuple[bool, str]:
        """Get started's voice step: done once a natural voice is ready (or you chose Windows')."""
        from .. import kitten, kokoro, languages
        from ..eleven import api_key as eleven_key
        from ..i18n import tr

        engine = self.ctl.cfg.voice.engine
        lang = languages.known(self.ctl.cfg.app.language)
        if engine == "windows":
            return True, ("Doty talks with the Windows voice, as you chose." if sys.platform == "win32" else
                          "Doty talks with the system voice, as you chose.")
        if engine == "kitten" and not languages.kitten_speaks(lang) and kokoro.speaks(lang):
            return False, tr("Kitten speaks English only: choose Kokoro in Voice settings to hear {language}.",
                             language=languages.own_name(lang))
        if engine == "kitten" and kitten.installed() and languages.kitten_speaks(lang):
            return True, "Doty talks with Kitten, the free voice on this PC."
        if (engine in ("auto", "elevenlabs") and eleven_key()
                and languages.eleven_speaks(lang, self.ctl.cfg.voice.eleven_model)):
            return True, "Doty talks with your ElevenLabs voice."
        # (ElevenLabs chosen but no key yet: Kokoro fills in when it's here.)
        if engine != "kitten" and kokoro.speaks(lang) and kokoro.installed(language=lang):
            return True, "Doty talks with Kokoro, the free voice on this PC."
        if not kokoro.speaks(lang):  # nothing to download: the step is to know who speaks your language
            return True, tr("Doty's free voices don't speak {language} yet: your PC's own voice or ElevenLabs "
                            "says it. Voice settings shows which voices speak which language.",
                            language=languages.own_name(lang))
        mb = kitten.DOWNLOAD_MB if engine == "kitten" else kokoro.missing_mb(language=lang)
        return False, tr("Doty talks with the robotic Windows voice for now. A free, natural voice that runs on this PC "
                         "is one {mb} MB download.", mb=mb)

    def _wants_setup_view(self) -> bool:
        return not self._setup_ready() or (self._key_missing() and not self.ctl.cfg.app.ai_step_skipped)

    def _update_steps(self) -> None:
        """Get started shows one thing to do at a time, with its button."""
        ready = self._setup_ready()
        self.step_setup.number.set_done(ready)
        self.recheck_link.setVisible(not ready and self._next_key == "launch")
        if ready:
            self.step_setup.detail.setText("Done: Dota sends Doty its game data.")
            self.go_setup_btn.hide()
        else:
            self.go_setup_btn.show()
            if self._next_key == "gsi":
                text, btn, glyph = ("First, Dota's connection file: it tells the game to send Doty live data. "
                                    "One click, then restart Dota if it's open.", "Install it", Glyph.REPAIR)
            elif self._next_key == "launch":
                text, btn, glyph = ("Last, a Steam launch option. Copy it, then in Steam right-click Dota 2 > "
                                    "Properties > Launch Options, paste it there, and restart Dota.",
                                    "Copy option", Glyph.COPY)
            elif self._next_key == "dota":
                text, btn, glyph = ("Doty can't find Dota 2 on this PC. Setup shows where it looked.", "Open Setup",
                                    Glyph.REPAIR)
            else:
                from ..i18n import tr

                nxt = " " + tr("Next: {step}.", step=tr(self._next_label)) if self._next_label else ""
                text, btn, glyph = (tr("{done} of {total} one-time steps done.", done=self._setup_done,
                                       total=self._setup_total) + nxt, "Open Setup", Glyph.REPAIR)
            self.step_setup.detail.setText(text)
            set_button(self.go_setup_btn, btn, glyph, "accent")

        provider = self.ctl.cfg.coach.provider
        key = self._checks.get("key")
        key_ok = key is not None and key.ok is True
        self.step_key.number.set_done(key_ok)
        self.step_key.title.setText("Add your Claude API key" if provider == "claude" else "Connect your AI coach")
        if key_ok:
            self.step_key.detail.setText("The AI coach is ready." if provider == "claude" else
                                         "Connected. The AI coach is ready.")
        elif provider == "claude":
            self.step_key.detail.setText("Lets the coach answer you and watch your screen: about 8¢ a game. Or "
                                         "use ChatGPT, Gemini, OpenRouter or a model on this PC (AI settings).")
        else:
            self.step_key.detail.setText("Finish connecting it in AI settings.")
        self.key_entry.setVisible(provider == "claude" and not key_ok)
        if self.isVisible() and self.left.currentIndex() == SETUP_VIEW and not self._clip_hooked:
            from PySide6.QtGui import QGuiApplication

            QGuiApplication.clipboard().dataChanged.connect(self.key_entry.refresh_clipboard)  # (on a change only)
            self._clip_hooked = True
            self.key_entry.refresh_clipboard()
        set_button(self.key_entry.save, "Save key", Glyph.CHECK, "accent" if ready else "ghost")
        self.ai_btn.setVisible(not key_ok)
        self.skip_key.setVisible(not key_ok and ready)
        coach = self.ctl.cfg.coach
        self.step_level.number.set_done(not getattr(coach, "level_from_rank", True))
        self.level_switch.set_level(coach.level)
        voice_ok, voice_text = self._voice_step()
        self.step_voice.number.set_done(voice_ok)
        self.step_voice.detail.setText(voice_text)
        set_button(self.voice_btn, "Voice settings" if voice_ok else "Get a free voice", Glyph.SETTINGS,
                   "ghost")
        if not self.ctl.running:
            # the cyan button goes to whatever is blocking: setup first, then Resume
            set_button(self.power, "Try again" if self.ctl.error else "Resume", Glyph.PLAY,
                       "start" if ready or self.ctl.error else "ghost")

    # before a match: the hub ----------------------------------------------------------------------
    def _build_hub_view(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.recap_card = Card(margins=(24, 18, 24, 18), spacing=10)  # placed above the views in __init__
        head = QHBoxLayout()
        head.addWidget(label("LAST GAME", "section"))
        head.addStretch(1)
        learn = button("Learn", "link", tooltip="Tips for what this review found, at your level")
        learn.clicked.connect(lambda: self.navigate.emit(LEARN))
        head.addWidget(learn)
        games = button("Profile", "link")
        games.clicked.connect(lambda: self.navigate.emit(PROFILE))
        head.addWidget(games)
        close = button("Dismiss", "link")
        close.clicked.connect(self._dismiss_recap)
        head.addWidget(close)
        self.recap_card.body.addLayout(head)
        self.recap_title = label("", "h2")
        self.recap_title.setFont(caps_font(20, 800, 1.0))
        self.recap_card.body.addWidget(self.recap_title)
        self.recap_stats = label("", wrap=True)
        self.recap_stats.setTextFormat(Qt.RichText)
        self.recap_card.body.addWidget(self.recap_stats)
        # From the post-game review (review.py), most useful first: what to work on, each death with its likely
        # cause, your core items; then the moments Doty noticed.
        self.recap_focus_head = label("WHAT TO WORK ON", "eyebrow")
        self.recap_focus = label("", wrap=True)
        self.recap_focus.setTextFormat(Qt.RichText)
        self.recap_items_head = label("YOUR CORE ITEMS", "eyebrow")
        self.recap_items = label("", wrap=True)
        self.recap_items.setTextFormat(Qt.RichText)
        self.recap_noticed_head = label("WHAT DOTY NOTICED", "eyebrow")
        self.recap_lines = label("", wrap=True)
        self.recap_lines.setTextFormat(Qt.RichText)
        self.recap_deaths_head = label("YOUR DEATHS", "eyebrow")
        for part in (self.recap_focus, self.recap_items, self.recap_lines):
            part.setStyleSheet("font-size: 10.5pt;")
        self.recap_card.body.addWidget(self.recap_focus_head)
        self.recap_card.body.addWidget(self.recap_focus)
        self.recap_card.body.addWidget(self.recap_deaths_head)
        self._clip_hooked = False  # (Get started's paste button follows the clipboard)
        self.recap_deaths = label("", wrap=True)
        self.recap_deaths.setTextFormat(Qt.RichText)
        self.recap_deaths.linkActivated.connect(self._ask_about_death)  # each death: "why?" asks the coach
        self.recap_deaths.setStyleSheet("font-size: 10.5pt;")
        self.recap_card.body.addWidget(self.recap_deaths)
        for part in (self.recap_items_head, self.recap_items, self.recap_noticed_head, self.recap_lines):
            self.recap_card.body.addWidget(part)
        self.recap_ask = button("Ask Doty about this game", "ghost", Glyph.CHAT,
                                "The coach reads your review and answers out loud: what went wrong and what to try")
        self.recap_ask.clicked.connect(self._ask_about_game)
        self.recap_card.body.addWidget(self.recap_ask, 0, Qt.AlignLeft)
        self.review = None
        for part in (self.recap_focus_head, self.recap_focus, self.recap_items_head, self.recap_items,
                     self.recap_deaths_head, self.recap_deaths, self.recap_ask):
            part.hide()
        self.recap_card.hide()

        plan = Card(margins=(22, 16, 22, 16), spacing=8)
        plan.body.addWidget(label("PLAN YOUR NEXT HERO", "section"))
        plan.body.addWidget(label("Type a hero for its build, its spells and what beats it.", "muted"))
        self.hero_search = QLineEdit()
        self.hero_search.setPlaceholderText("Find a hero…")
        self.hero_search.setClearButtonEnabled(True)
        self.hero_search.returnPressed.connect(self._search_hero)
        plan.body.addWidget(self.hero_search)
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, glyph, page in (("Draft help", Glyph.PEOPLE, DRAFT), ("Learn", Glyph.INFO, LEARN),
                                  ("Profile", Glyph.PROFILE, PROFILE)):
            b = button(text, "ghost", glyph)
            b.clicked.connect(lambda _c=False, p=page: self.navigate.emit(p))
            row.addWidget(b)
        row.addStretch(1)
        plan.body.addLayout(row)
        pair = QHBoxLayout()
        pair.setSpacing(14)
        pair.addWidget(plan, 3)
        self._keys_cards.append(KeysCard(self.ctl))
        pair.addWidget(self._keys_cards[-1], 2, Qt.AlignTop)
        lay.addLayout(pair)
        lay.addStretch(1)
        return w

    def _attach_completer(self) -> None:
        from .stats_pages import hero_completer

        if self.svc is not None and self.svc.stats.loaded:
            c = hero_completer(self.svc.stats.hero_names(), self.hero_search)
            c.activated.connect(lambda _t: self._search_hero())
            self.hero_search.setCompleter(c)

    def _search_hero(self) -> None:
        text = self.hero_search.text().strip()
        if not text:
            return
        c = self.hero_search.completer()
        if c is not None and self.svc is not None and not self.svc.stats.find(text):
            c.setCompletionPrefix(text)
            if c.completionCount():
                text = c.currentCompletion()
        self.hero_search.clear()
        self.open_hero.emit(text)

    # hero selection -------------------------------------------------------------------------------
    def _build_draft_view(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        card = Card(margins=(22, 16, 22, 16), spacing=10)
        head = QHBoxLayout()
        head.addWidget(label("TOP PICKS RIGHT NOW", "section"))
        head.addStretch(1)
        open_draft = button("Open Draft", "accent", Glyph.PEOPLE)
        open_draft.clicked.connect(lambda: self.navigate.emit(DRAFT))
        head.addWidget(open_draft)
        card.body.addLayout(head)
        self.draft_body = QVBoxLayout()
        self.draft_body.setSpacing(8)
        card.body.addLayout(self.draft_body)
        self.draft_verdict = label("", wrap=True)
        self.draft_verdict.setTextFormat(Qt.RichText)
        card.body.addWidget(self.draft_verdict)
        lay.addWidget(card)
        lay.addStretch(1)
        self._draft_shown = None
        self._show_draft()
        return w

    def set_draft_summary(self, summary: dict) -> None:
        """The Draft page's latest suggestions: {"groups": [(title, [(hero id, "+4.2", color)])], "verdict": html}."""
        self._draft = summary
        self._show_draft()

    def _show_draft(self) -> None:
        s = getattr(self.svc, "stats", None)
        groups = (self._draft or {}).get("groups") or []
        key = tuple((t, tuple(h for h, _e, _c in heroes)) for t, heroes in groups)
        if key == self._draft_shown:
            return
        self._draft_shown = key
        while self.draft_body.count():
            item = self.draft_body.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                inner = item.layout()
                while inner.count():
                    sub = inner.takeAt(0)
                    if sub.widget():
                        sub.widget().hide()
                        sub.widget().deleteLater()
        if not groups or s is None:
            self.draft_body.addWidget(label("Reading the picks from your screen…", "faint"))
        for title, heroes in groups:
            if title:
                self.draft_body.addWidget(label(title.upper(), "eyebrow"))
            row = QHBoxLayout()
            row.setSpacing(8)
            for hid, edge, color in heroes[:5]:
                pic = HeroPortrait(104, 58, hover=True)
                pic.caption = True
                pic.set_hero(s, hid)
                pic.set_badge(edge, color)
                pic.setToolTip(tr("{hero}: click for its build", hero=s.name(hid)))
                pic.clicked.connect(lambda n=s.name(hid): self.open_hero.emit(n))
                row.addWidget(pic)
            row.addStretch(1)
            self.draft_body.addLayout(row)
        self.draft_verdict.setText((self._draft or {}).get("verdict") or "")

    # the match ----------------------------------------------------------------------------------
    def _note_line(self, e: dict) -> None:
        """Remember what Doty says during a match, for the recap."""
        if self._phase == "live" and e.get("final", True):
            self._lines[e["id"]] = e

    def _track_game(self, st: dict) -> None:
        if st.get("game"):
            self._last_game = dict(st["game"])
            self._last_clock = st.get("clock") or 0

    def _end_game(self, st: dict | None) -> None:
        """A match just ended (or Dota stopped): keep a recap for the hub."""
        g, clock, lines = self._last_game, self._last_clock, list(self._lines.values())
        self._last_game, self._last_clock, self._lines = None, 0, {}
        if g is None or clock < 60:
            return
        win = ((st or {}).get("win_team") or "").lower()
        team = (g.get("team") or "").lower()
        result = ""
        if win in ("radiant", "dire") and team in ("radiant", "dire"):
            result = "won" if win == team else "lost"
        coach = [e for e in lines if e["source"] == "coach" and e["category"] != "watching"]
        picked = coach or [e for e in lines if int(e["priority"]) <= 2 and e["source"] == "rules"]
        picked = sorted(picked, key=lambda e: (int(e["priority"]), -e["id"]))[:3]
        self.recap = {"result": result, "game": g, "clock": clock,
                      "lines": [(e["clock"], e["text"]) for e in sorted(picked, key=lambda e: e["id"])]}
        self.review = None
        self._show_recap()
        QTimer.singleShot(2500, self._attach_review)  # the review is written as the match ends

    def _attach_review(self) -> None:
        """Add the post-game review to the recap: it's the one for this game if it was saved just now."""
        import time as _time

        from ..review import latest_review

        try:
            review = latest_review(demos=True)  # (the demo's own recap shows its review)
        except Exception as exc:
            print(f"[doty] couldn't read the review: {exc!r}")
            return
        if review is None or self.recap is None or _time.time() - review.saved_at > 600:
            return
        self.review = review
        self._show_recap()

    def _ask_about_death(self, link: str) -> None:
        from ..review import death_prompt

        if self.review is None or not link.startswith("death:"):
            return
        i = int(link.split(":", 1)[1])
        if 0 <= i < len(self.review.deaths):
            self.ctl.ask_question(death_prompt(self.review, i))

    def _ask_about_game(self) -> None:
        from ..review import review_prompt

        if self.review is not None:
            self.ctl.ask_question(review_prompt(self.review))

    def _show_recap(self) -> None:
        r = self.recap
        self.recap_card.setVisible(r is not None and self.left.currentIndex() in (SETUP_VIEW, HUB_VIEW))
        if r is None:
            return
        g = r["game"]
        title = {"won": "Victory", "lost": "Defeat"}.get(r["result"], "Game over")
        color = {"won": theme.GREEN, "lost": theme.RED}.get(r["result"], theme.TEXT)
        self.recap_title.setText(f"{tr(title)} · {g.get('hero', '')}")
        self.recap_title.setStyleSheet(f"color: {color};")
        k, d, a = g.get("kda") or (0, 0, 0)

        def stat(value, caption, c=theme.TEXT) -> str:
            return (f'<span style="font-size:17pt; font-weight:800; color:{c}">{value}</span>'
                    f'&nbsp;<span style="color:{theme.MUTED}">{caption}</span>')

        self.recap_stats.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join((
            stat(f"{k} / {d} / {a}", "K / D / A"), stat(g.get("last_hits", 0), "last hits"),
            stat(g.get("gpm", 0), "gold a minute", theme.GOLD), stat(mmss(r["clock"]), "long"))))
        lines = r["lines"][-5:]  # (the latest few: the feed has them all)
        self.recap_noticed_head.setVisible(bool(lines))
        self.recap_lines.setVisible(bool(lines))
        self.recap_lines.setText("".join(
            f'<div style="margin-bottom:5px"><span style="color:{theme.MUTED}">{clock}</span>&nbsp;&nbsp;'
            f'{_html(text)}</div>' for clock, text in lines))
        rv = self.review
        focus = rv.focus if rv is not None else []
        deaths = rv.deaths if rv is not None else []
        self.recap_focus_head.setVisible(bool(focus))
        self.recap_focus.setVisible(bool(focus))
        self.recap_focus.setText("".join(f'<div style="margin-bottom:7px"><span style="color:{theme.AMBER};'
                                         f' font-weight:800">{i + 1}</span>&nbsp;&nbsp;{_html(p)}</div>'
                                         for i, p in enumerate(focus)))
        pace = rv.item_pace[:4] if rv is not None else []
        self.recap_items_head.setVisible(bool(pace))
        self.recap_items.setVisible(bool(pace))
        self.recap_items.setText("".join(f'<div style="margin-bottom:5px">{self._pace_row(name, clock, usual)}</div>'
                                         for name, clock, usual in pace))
        self.recap_deaths_head.setVisible(bool(deaths))
        self.recap_deaths.setVisible(bool(deaths))
        from ..review import CAUSE_TEXT

        rows = []
        ask = self._coach_ok and rv is not None
        for i, d in enumerate(deaths[:6]):
            why = "; ".join(CAUSE_TEXT.get(c, c) for c in d.causes if c != "after_warning") or "no clear cause"
            warned = next((s for s in reversed(d.said) if s.get("text")), None)
            extra = f' · Doty had said "{_html(warned["text"])}"' if warned else ""
            link = (f'&nbsp;&nbsp;<a href="death:{i}" style="color:{theme.BRAND}; text-decoration:none">Why?</a>'
                    if ask else "")
            rows.append(f'<div style="margin-bottom:7px"><span style="color:{theme.RED}; font-weight:700">'
                        f'{mmss(d.clock)}</span>&nbsp;&nbsp;{_html(why)}{extra}{link}</div>')
        if len(deaths) > 6:
            rows.append(f'<div style="color:{theme.MUTED}">and {len(deaths) - 6} more</div>')
        self.recap_deaths.setText("".join(rows))
        self.recap_ask.setVisible(rv is not None and self._coach_ok)

    @staticmethod
    def _pace_row(name: str, clock: int, usual: int) -> str:
        """"Shadow Blade  31:20 · usually by 25:00 (6 min late)", amber when 3+ minutes late."""
        from ..review import LATE_ITEM_S

        behind = clock - usual
        if behind >= LATE_ITEM_S:
            note, color = f"{round(behind / 60)} min late", theme.AMBER
        elif behind <= -60:
            note, color = f"{round(-behind / 60)} min early", theme.GREEN
        else:
            note, color = "on time", theme.GREEN
        return (f'{_html(name)}&nbsp;&nbsp;<span style="color:{theme.MUTED}">{mmss(clock)} · usually by '
                f'{mmss(usual)}</span>&nbsp;&nbsp;<span style="color:{color}">{note}</span>')

    def _dismiss_recap(self) -> None:
        self.recap = None
        self._show_recap()

    # helpers ------------------------------------------------------------------------------------
    def _ask_clicked(self) -> None:
        if self._coach_ok:
            self.ctl.ask("now")
        elif self._needs_key:
            self._key_popover(self.b_ask)
        else:
            self.open_settings_tab.emit(TAB_AI)

    def _fix_link(self, name: str) -> None:
        """A problem's fix, clicked in the match header."""
        if name == "coach":
            self._fix_coach(self.game.ready)
        elif name == "voice":
            self.open_settings_tab.emit(TAB_VOICE)
        elif name == "talk":
            self.open_settings_tab.emit(TAB_TALK)
        else:
            self.go_setup.emit()

    def _fix_coach(self, anchor: QWidget) -> None:
        if self._needs_key and self.ctl.cfg.coach.provider == "claude":
            self._key_popover(anchor)
        else:
            self.open_settings_tab.emit(TAB_AI)

    def _new_popover(self) -> Popover:
        old = getattr(self, "_popover", None)
        if old is not None:
            old.deleteLater()  # one at a time; the last one is done with
        self._popover = Popover(self)
        return self._popover

    def _key_popover(self, anchor: QWidget) -> None:
        pop = self._new_popover()
        t = label("Paste your Claude API key")
        t.setStyleSheet("font-weight: 700; font-size: 10.5pt;")
        pop.lay.addWidget(t)
        pop.lay.addWidget(label("Saved for your Windows user on this PC. The coach restarts with it.", "faint"))
        entry = KeyEntry(self.ctl, self.toast)
        entry.field.setMinimumWidth(320)
        entry.saved.connect(pop.close)
        entry.saved.connect(lambda: self.recheck_setup.emit())
        pop.lay.addWidget(entry)
        self.key_popover = pop  # (kept for tests)
        pop.open_under(anchor)
        entry.field.setFocus()

    def _coach_menu(self) -> None:
        """How chatty the coach is: four presets, one click."""
        c = self.ctl.cfg.coach
        current = coach_preset(c.speculative_interval_s, c.speak_up)
        model = c.model if c.provider == "claude" else ""
        pop = self._new_popover()
        pop.lay.setSpacing(2)
        if getattr(self, "_coach_off", False):  # no AI coach: just the controls (Pause, Mute, Hide)
            self._right_now(pop)
            pop.setMinimumWidth(300)
            self.coach_menu = pop
            pop.open_under(self.b_coaching)
            return
        head = label("HOW CHATTY IS THE COACH?", "eyebrow")
        head.setAccessibleName("How chatty is the coach?")
        head.setContentsMargins(8, 0, 0, 6)
        pop.lay.addWidget(head)
        if current is None:
            from .pages import LOOK_EVERY, SPEAK_UP

            looks = dict(LOOK_EVERY).get(c.speculative_interval_s, f"every {c.speculative_interval_s} s").lower()
            talks = dict(SPEAK_UP).get(c.speak_up, c.speak_up).lower()
            pop.lay.addWidget(_PresetRow("Custom", f"Looks {looks}; talks {talks}.", "", True, enabled=False))
        for key, name, look, speak, detail in COACH_PRESETS:
            row = _PresetRow(name, detail, preset_cost(look, model, c.provider, c.watch_model), current is not None and
                             current[0] == key)
            row.clicked.connect(lambda lk=look, sp=speak: (self.ctl.set_coaching(look_every_s=lk, speak_up=sp),
                                                           pop.close()))
            pop.lay.addWidget(row)
        self._right_now(pop)
        more = button("Exact settings…", "link")
        more.clicked.connect(lambda: (pop.close(), self.open_settings_tab.emit(TAB_AI)))
        pop.lay.addSpacing(4)
        pop.lay.addWidget(more, 0, Qt.AlignLeft)
        pop.setMinimumWidth(380)
        self.coach_menu = pop  # (kept for tests)
        pop.open_under(self.b_coaching)

    def _right_now(self, pop) -> None:
        """The menu's "Right now" row: Mute, Hide the cards, Pause (copies of the real buttons, in their state)."""
        quick = [b for b in (self.b_mute, self.b_overlay, self.b_pause) if not b.isHidden()]
        if not quick:
            return
        now = label("RIGHT NOW", "eyebrow")
        now.setContentsMargins(8, 10, 0, 4)
        pop.lay.addWidget(now)
        row = QHBoxLayout()
        row.setSpacing(6)
        for b in quick:
            twin = button(b.accessibleName() or "…", "ghost", None, b.toolTip())
            twin.setEnabled(b.isEnabled())  # (a muted-voice "Mute" that can't work isn't clickable here either)
            twin.clicked.connect(lambda _c=False, src=b: (pop.close(), src.click()))
            row.addWidget(twin)
        row.addStretch(1)
        pop.lay.addLayout(row)

    def _resume(self) -> None:
        if not self.ctl.running:
            self.ctl.start()

    # state ----------------------------------------------------------------------------------------
    def _view(self, index: int) -> None:
        if self.left.currentIndex() != index:
            self.left.setCurrentIndex(index)
            fade_in(self.left.currentWidget(), rise=0)
        self.recap_card.setVisible(self.recap is not None and index in (SETUP_VIEW, HUB_VIEW))
        # in a match the picture leads and your hero sits under it; otherwise the picture follows the view
        col = self._column
        screen_first = col.indexOf(self.screen) < col.indexOf(self.left)
        if screen_first != (index == LIVE_VIEW):
            col.removeWidget(self.screen)
            col.insertWidget(col.indexOf(self.left) + (0 if index == LIVE_VIEW else 1), self.screen, 1)

    def _set_state(self, color: str, pulse: bool) -> None:
        """The banner's state color: its dot, its eyebrow and the bar down its left."""
        self.dot.set_state(color, pulse)
        self.banner.set_bar(color)
        self.eyebrow.setStyleSheet(f"color: {theme.MUTED if color == GREY else color};")  # grey text: too dim

    def _report_tip(self, advice_id: int, text: str) -> None:
        """'Wrong tip?' on a coach line: save what the coach saw, and open the folder."""
        try:
            folder = self.ctl.report_tip(advice_id, text)
        except OSError as exc:
            folder = None
            print(f"[doty] couldn't save the report: {exc!r}")
        if self.toast is None:
            return
        if folder is None:
            self.toast.show_message("Doty no longer has that line's details: it keeps the last 12 coach lines.")
            return
        self.toast.show_message("Saved. Attach that folder to a bug report: it has the screenshot, the game "
                                "state and the coach's full answer.", 5000)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def refresh(self, st: dict | None) -> None:
        ctl = self.ctl
        running = st is not None
        self.feed.details = ctl.cfg.overlay.show_latency  # Settings > Advanced > Show technical details
        phase = st["phase"] if running else ""
        live = phase == "live"
        drafting = running and phase == "menu" and bool(st.get("draft"))
        if running and self._phase == "live" and phase in ("menu", "stale"):
            self._end_game(st)  # the match ended (Pause, or a restart that has no game data yet, keeps it going)
        if live:
            self._track_game(st)
        entering_live = live and self._phase != "live"
        if running and phase != "waiting":
            self._phase = phase
        for card in self._keys_cards:
            card.refill()
        self.feed_title.setText("LIVE FEED" if live else "FEED")

        if live:
            self._view(LIVE_VIEW)
        elif drafting:
            self._view(DRAFT_VIEW)
        elif self._wants_setup_view():
            self._view(SETUP_VIEW)
        else:
            self._view(HUB_VIEW)
        if entering_live:
            fade_in(self.game)
        # what Doty sees: always in a match (the demo says why there's no picture); otherwise once there's
        # a picture of Dota, under whatever view fits the moment
        self.screen.demo = running and ctl.demo_running
        if self.screen.demo and self.screen._pixmap is None:
            self.screen._placeholder(DEMO_TEXT)
        show_screen = running and (live or (self.screen._pixmap is not None and
                                            self.left.currentIndex() != SETUP_VIEW))
        if show_screen and self.screen.isHidden():
            fade_in(self.screen)
        self.screen.setVisible(show_screen)
        self._fit_column()
        self.banner.setVisible(not live)
        if running and self.rail.isHidden():
            fade_in(self.rail)
        self.rail.setVisible(running)
        self.actions.setVisible(running)
        self.timers.setVisible(live)
        self.build.setVisible(live)
        self.game.set(st.get("game") if live else None)
        if live:
            g = st.get("game") or {}
            self.build.set(g)
            self.timers.set(g.get("timers") or [])
        self.power.setVisible(not running)

        if not running:
            self.strip.hide()
            self.ready_line.hide()
            self.b_ask.setEnabled(False)
            if ctl.error:
                self._set_state(RED, False)
                self.eyebrow.setText("PROBLEM")
                self.title.setText("Doty couldn't start")
                self.subtitle.setText(ctl.error[:1].upper() + ctl.error[1:])
            else:
                self._set_state(GREY, False)
                self.eyebrow.setText("PAUSED")
                self.title.setText("Doty is paused")
                self.subtitle.setText("Nothing is watching or listening. Resume when you're ready to play.")
            self._update_steps()
            return

        details = ctl.cfg.overlay.show_latency
        demo = " · DEMO" if ctl.demo_running else ""
        key = pretty_key(st["listen_key"])

        # the chips ---------------------------------------------------------------------------------
        if phase in ("waiting", "screen"):
            self.t_game.set("Not connected", "Launch Dota 2" if self._setup_ready() else "Finish Get started",
                            "off")  # normal before Dota opens: not a problem
        elif phase == "stale":
            self.t_game.set("No data", "Dota is closed or in its main menu", "off")  # normal between games
        else:
            self.t_game.set("Connected", f"{st['rate']:.0f} updates/s · rules in {st['rules_ms']:.1f} ms"
                            if details and st.get("rate") else "Reading your game live", "ok")

        coach = st["coach"]
        model = pretty_model(st["coach_model"])
        if st.get("coach_provider") not in ("", None, "Claude"):
            model = f"{st['coach_provider']} · {st['coach_model']}"
        ttft = st.get("coach_ttft_ms")
        self._needs_key = "no API key" in coach or "bad API key" in coach
        if coach == "off":
            self.t_coach.set("Off", "Turned off in Settings", "off")
        elif coach.startswith("ready"):
            detail = f"{model} · first words in {ttft / 1000:.1f} s" if details and ttft else model
            spend = st.get("coach_spend") or {}
            if spend.get("looks"):  # what this match has cost so far
                from .pages import _money

                cost = f", {_money(spend['usd'])}" if spend.get("usd") else ""
                detail = tr("{model} · this game: {n} looks", model=model, n=int(spend["looks"])) + cost
            self.t_coach.set("Ready", detail, "ok")
        elif "no API key" in coach:
            self.t_coach.set("Needs a key", "The AI coach needs your Claude API key", "warn", "Paste key")
        elif "bad API key" in coach:
            self.t_coach.set("Key rejected", "Claude didn't accept the saved key", "bad", "Paste key")
        else:
            self.t_coach.set("Unavailable", coach.removeprefix("off (").rstrip(")"), "bad", "Settings")
        self._coach_ok = coach.startswith("ready")

        voice = st["voice"]
        problem = st.get("voice_problem", "")
        if st.get("voice_engine") in ("elevenlabs", "kokoro"):
            voice_name = " · ".join(x for x in ({"elevenlabs": "ElevenLabs", "kokoro": "Kokoro"}[st["voice_engine"]],
                                                 st.get("voice_name"), st.get("voice_device")) if x)
        else:
            voice_name = windows_voice_name(ctl.cfg.voice.voice) or ("Windows voice" if sys.platform == "win32"
                                                                     else "System voice")
        if problem:
            voice_name = problem[0].upper() + problem[1:]
        if voice == "muted":
            self.t_voice.set("Muted", "Doty is quiet; cards still show", "off", "Unmute")  # your choice
        elif voice == "off":
            self.t_voice.set("Off", "Turned off in Settings", "off")
        else:
            self.t_voice.set("On", voice_name, "warn" if problem else "ok", "Settings" if problem else "")

        talk = st["listen"]
        if talk == "off":
            self.t_talk.set("Off", "Turned off in Settings", "off")
        elif talk == "ready":
            mic = tr("{mic} mic", mic=short_mic(st["mic"]))
            self.t_talk.set(tr("Hold {key}", key=key), f"{mic} · {st['stt_model']}" if details and st.get("stt_model") else mic,
                            "ok")
        elif talk.startswith("loading"):
            self.t_talk.set("Loading…", "Preparing speech recognition", "info")
        else:
            self.t_talk.set("Unavailable", talk, "bad", "Settings")

        chips = (self.t_game, self.t_coach, self.t_voice, self.t_talk)
        trouble = any(t.state in ("warn", "bad") for t in chips)
        self.strip.setVisible(trouble and not live)
        parts = []
        if self.t_coach.state == "ok":
            parts.append(f"Coach: {model}")
        if voice == "muted":
            parts.append("Voice: muted")
        elif self.t_voice.state == "ok":
            parts.append("Voice: " + {"kokoro": "natural (Kokoro)", "elevenlabs": "natural (ElevenLabs)"}.get(
                st.get("voice_engine"), "Windows"))
        if self.t_talk.state == "ok":
            parts.append(f"Mic: {short_mic(st['mic'])}")
        elif talk == "off":
            parts.append("Push-to-talk off")
        ready_html = (f'<span style="font-family:\'{theme.ICON_FONT}\'; color:{theme.GREEN}">{Glyph.CHECK}</span>'
                      f'&nbsp; All set &nbsp;·&nbsp; {" &nbsp;·&nbsp; ".join(parts)}') if not trouble else ""
        ready_tip = "\n".join(f"{t.caption.text()}: {t.value.text()} ({t.detail.text()})" for t in chips)
        self.ready_line.setText(ready_html)
        self.ready_line.setToolTip(ready_tip)
        # "All set" only once setup is done (Get started still has steps otherwise)
        self.ready_line.setVisible(bool(ready_html) and not live and not self._wants_setup_view())

        # the header ------------------------------------------------------------------------------
        if live:
            check = f'<span style="font-family:\'{theme.ICON_FONT}\'; color:{theme.GREEN}">{Glyph.CHECK}</span>'
            if trouble:
                bits = []
                for name, t in (("game", self.t_game), ("coach", self.t_coach), ("voice", self.t_voice),
                                ("talk", self.t_talk)):
                    if t.state in ("warn", "bad"):
                        fix = t.fix.text()
                        bits.append(f'<span style="color:{AMBER if t.state == "warn" else RED}">'
                                    f'{t.caption.text()}: {t.value.text()}</span>' +
                                    (f' &nbsp;<a href="{name}" style="color:{theme.BRAND_BRIGHT}; '
                                     f'text-decoration:none"><b>{fix}</b></a>' if fix else ""))
                header = " &nbsp;&nbsp;·&nbsp;&nbsp; ".join(bits)
            else:
                header = f"{check}&nbsp; All set"  # the details are on hover
            self.game.set_ready(header, ready_tip)  # (the top bar carries LIVE and the clock)
        elif drafting:
            self._set_state(BRAND, True)
            self.eyebrow.setText("HERO SELECTION" + demo)
            self.title.setText("Drafting")
            self.subtitle.setText("Doty reads the picks from your screen. The best picks against their lineup are "
                                  f"below; hold {key} to ask about any of them.")
        elif st.get("post_game") or (phase in ("menu", "stale") and self.recap is not None):
            r = self.recap or {}
            res = r.get("result", "")
            self._set_state({"won": GREEN, "lost": RED}.get(res, BLUE), False)
            self.eyebrow.setText("GAME OVER" + demo)
            self.title.setText({"won": "Victory", "lost": "Defeat"}.get(res, "Game over"))
            self.subtitle.setText("Your recap is below. Queue up when you're ready; Doty picks the next game up "
                                  "on its own.")
        elif phase == "menu":
            self._set_state(BLUE, False)
            self.eyebrow.setText("CONNECTED" + demo)
            self.title.setText("In Dota's menus")
            self.subtitle.setText(tr("Alerts start with the match. Hold {key} to ask Doty anything meanwhile.", key=key))
        elif phase == "screen":
            self._set_state(BRAND, True)
            self.eyebrow.setText("WATCHING YOUR SCREEN")
            self.title.setText("Watching your screen")
            self.subtitle.setText("No game data yet, so the coach works from screenshots while Dota is in front. "
                                  "For instant alerts, timers and exact stats, finish Get started.")
        elif phase == "stale":
            self._set_state(AMBER, False)
            self.eyebrow.setText("NO DATA" + demo)
            self.title.setText("Dota stopped sending")
            self.subtitle.setText("The game was closed, or it's sitting in the main menu. Doty picks up again on "
                                  "its own.")
        else:
            self._set_state(AMBER, True)
            self.eyebrow.setText("WAITING" + demo)
            self.title.setText("Waiting for Dota 2")
            self.subtitle.setText("Launch Dota 2 and Doty connects by itself." if self._setup_ready() else
                                  "Do the steps below once, then launch Dota 2.")

        # the actions -------------------------------------------------------------------------------
        if self._coach_ok:
            set_button(self.b_ask, "Ask now", Glyph.CHAT, "accent")
            self._ask_tip = tr("What should I do right now? ({ask}, or hold {key} and ask out loud)",
                               ask=ctl.cfg.hotkeys.ask.title(), key=pretty_key(ctl.cfg.listen.key))
            self.b_ask.setToolTip(self._ask_tip)
        elif self._needs_key:
            set_button(self.b_ask, "Ask now", Glyph.CHAT, "ghost")
            self.b_ask.setToolTip("The AI coach needs your Claude API key. Click to add it.")
        else:
            set_button(self.b_ask, "AI coach is off" if coach == "off" else "Coach unavailable", Glyph.CHAT, "ghost")
            self.b_ask.setToolTip("Turn the AI coach on in Settings > AI coach.")
        self.b_ask.setEnabled(True)
        self.b_plan.setVisible(live)
        self.b_plan.setEnabled(self._coach_ok)
        preset = coach_preset(ctl.cfg.coach.speculative_interval_s, ctl.cfg.coach.speak_up)
        self._coach_off = coach == "off"
        if self._coach_off:  # no AI coach: the same button holds Pause, Mute and Hide
            set_button(self.b_coaching, "Controls", Glyph.CHEVRON_DOWN)
            self.b_coaching.setToolTip("Pause Doty, mute the voice or hide the cards")
        else:
            set_button(self.b_coaching, f"Coach: {preset[1] if preset else 'custom'}", Glyph.CHEVRON_DOWN)
            self.b_coaching.setToolTip((f"{preset[4]} " if preset else "") + "Click to change how chatty the coach "
                                                                             "is.")
        set_button(self.b_mute, "Unmute" if voice == "muted" else "Mute",
                   Glyph.VOLUME if voice == "muted" else Glyph.MUTE)
        self.b_mute.setToolTip(("Unmute voice" if voice == "muted" else "Mute voice") +
                               f" ({ctl.cfg.hotkeys.mute.title()})")
        self.b_mute.setVisible(live)
        self.b_mute.setEnabled(voice != "off")
        overlay = st["overlay"]
        set_button(self.b_overlay, "Show cards" if overlay == "hidden" else "Hide for now",
                   Glyph.VIEW if overlay == "hidden" else Glyph.HIDE)
        self.b_overlay.setToolTip(("Show the cards over Dota" if overlay == "hidden" else
                                   "Hide the cards over Dota until you show them again (Settings keeps them on)")
                                  + f" ({ctl.cfg.hotkeys.toggle_overlay.title()})")
        self.b_overlay.setVisible(live and overlay != "off")
        # The demo makes sense only before Dota is connected.
        self.b_demo.setVisible(ctl.demo_running or (phase in ("waiting", "screen")
                                                    and self.left.currentIndex() != SETUP_VIEW))
        set_button(self.b_demo, "Stop demo" if ctl.demo_running else "Demo match",
                   Glyph.STOP if ctl.demo_running else Glyph.BOLT)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
