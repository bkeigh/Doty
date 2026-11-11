"""The Doty main window: a top bar (Doty's name, the page tabs, and what's happening: the match's LIVE tag,
team, day or night and clock, or Doty's status) over the Coach, Draft, Heroes, Profile, Setup and Settings
pages."""

from __future__ import annotations

import sys

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

from ..i18n import tr
from .. import __version__
from ..knowledge.timings import PATCH
from ..rules.base import mmss
from ..win import find_dota_rect
from . import theme
from .coach_page import SETUP_VIEW, CoachPage
from .controller import Controller
from .icon import app_icon
from .motion import FadeStack, NavIndicator
from .pages import SettingsPage, SetupPage
from .learn_page import LearnPage
from .profile_page import ProfilePage
from .stats_pages import DraftPage, HeroesPage, StatsService
from .widgets import StatusIcon, TabButton, Tag, Toast, Wordmark, caps_font


OFF_TEXT = "Doty is paused. Resume and you'll see what it's watching here."
COACH, DRAFT, HEROES, PROFILE, SETUP, SETTINGS, LEARN = range(7)


def relaunch_doty() -> None:
    """Start Doty again (a new copy, in the new language) and quit this one."""
    import sys

    from PySide6.QtCore import QProcess, QProcessEnvironment

    from ..config import FROZEN
    from .main import TAKEOVER_ENV

    args = sys.argv[1:] if FROZEN else ["-m", "doty", *(sys.argv[1:] or ["app"])]
    exe = sys.executable
    if not FROZEN and exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"  # (no console window)
    proc = QProcess()
    proc.setProgram(exe)
    proc.setArguments(args)
    env = QProcessEnvironment.systemEnvironment()
    env.insert(TAKEOVER_ENV, "1")  # the new copy waits for this one to close, then takes over (ui/main.py)
    proc.setProcessEnvironment(env)
    proc.startDetached()
    quit_doty()


def quit_doty() -> None:
    """Quit for real. Qt 6 cancels a quit when a window refuses to close, and the window refuses (it hides
    to the tray instead) unless it knows Doty is quitting."""
    app = QApplication.instance()
    if app is not None:
        app.setProperty("doty_quitting", True)
        app.quit()


class MainWindow(QMainWindow):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.tray: AppTray | None = None
        self._told_about_tray = False
        self._tagged = False
        self._game_state = "off"  # off | idle | draft | live | post, for following the game
        self._auto_back: int | None = None  # the page to return to with Esc after an automatic switch
        self.setWindowTitle("Doty")
        self.setWindowIcon(app_icon())
        self.resize(1340, 920)
        self.setMinimumSize(900, 700)  # down to a portrait monitor's half

        root = QWidget()
        root.setObjectName("root")
        root.setFocusPolicy(Qt.ClickFocus)  # holds focus at the start, so no tab shows a keyboard outline
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._header())
        self.stack = FadeStack()
        v.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.toast = Toast(root)
        self.stats = StatsService()
        self.coach_page = CoachPage(ctl, self.stats, self.toast)
        self.draft_page = DraftPage(ctl, self.stats)
        self.heroes_page = HeroesPage(ctl, self.stats)
        self.profile_page = ProfilePage(ctl, self.stats)
        self.setup_page = SetupPage(ctl, self.toast)
        self.settings_page = SettingsPage(ctl, self.toast)
        self.learn_page = LearnPage(ctl, self.stats, self.toast)
        for p in (self.coach_page, self.draft_page, self.heroes_page, self.profile_page, self.setup_page,
                  self.settings_page, self.learn_page):
            self.stack.addWidget(p)
        self.setup_page.summary.connect(self._setup_badge)
        self.setup_page.progress.connect(self.coach_page.set_setup_progress)
        self.setup_page.next_step.connect(self.coach_page.set_next_step)
        self.setup_page.checks_ready.connect(self.coach_page.set_checks)
        self.coach_page.install_gsi.connect(lambda: self.setup_page._install_gsi())
        self.coach_page.copy_launch.connect(lambda: self.setup_page._copy_launch_option())
        self.coach_page.recheck_setup.connect(lambda: self.setup_page.recheck())
        self.coach_page.go_setup.connect(lambda: self.go(SETUP))
        self.coach_page.navigate.connect(self.go)
        self.coach_page.open_settings_tab.connect(self.open_settings)
        self.coach_page.open_hero.connect(self.open_hero)
        self.draft_page.open_hero.connect(self.open_hero)
        self.draft_page.summary.connect(self.coach_page.set_draft_summary)
        for box in (self.draft_page.rank, self.heroes_page.rank):  # one rank everywhere
            box.currentIndexChanged.connect(lambda _i, b=box: self._sync_rank(b))
        self.setup_page.open_settings.connect(lambda: self.go(SETTINGS))
        self.settings_page.open_setup.connect(lambda: self.go(SETUP))
        for page in (self.draft_page, self.heroes_page):  # the Draft / All heroes switch
            page.mode.changed.connect(lambda draft: self.go(DRAFT if draft else HEROES))
        self.nav_buttons[0].setChecked(True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        ctl.changed.connect(self.refresh)
        self.refresh()
        self.setup_page.recheck()
        QTimer.singleShot(1500, self.stats.ensure_loaded)  # portraits and item icons for the game panel
        if getattr(ctl.cfg, "load_problem", ""):  # a typo in doty.toml: say so (the console isn't visible)
            QTimer.singleShot(800, lambda: self.toast.show_message(ctl.cfg.load_problem))

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("header")
        bar.setFixedHeight(56)
        h = QHBoxLayout(bar)
        h.setContentsMargins(18, 0, 20, 0)
        h.setSpacing(0)
        mark = Wordmark()
        mark.setToolTip(tr("Doty v{version} · timings for patch {patch}", version=__version__, patch=PATCH))
        h.addWidget(mark)
        h.addSpacing(18)

        # Five tabs for seven pages: Heroes holds Draft and All heroes (a switch at the top of each), and the
        # setup checklist opens from Settings (its dot shows on the Settings tab). `nav_buttons[page]` is the
        # tab that page belongs to.
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        coach, heroes, learn, profile, settings = (TabButton(t) for t in ("Coach", "Heroes", "Learn", "Profile",
                                                                          "Settings"))
        learn.setToolTip("Get better at Dota: tips for your level, last-hit practice, items, drafting, ranked")
        self.nav_buttons = [coach, heroes, heroes, profile, settings, settings, learn]
        for b, page in ((coach, COACH), (heroes, HEROES), (learn, LEARN), (profile, PROFILE), (settings, SETTINGS)):
            self.nav_group.addButton(b, page)
            h.addWidget(b)
        self._heroes_mode = HEROES  # the Heroes tab reopens on Draft or All heroes, whichever you used last
        self.nav_group.idClicked.connect(lambda page: self._go(self._heroes_mode if page == HEROES else page))
        self.indicator = NavIndicator(bar)
        h.addStretch(1)

        # what's happening: Doty's status, or (in a match) LIVE, your team, day or night and the clock
        self.side_dot = StatusIcon(22)  # the same eye / turning arrows as the overlay's badge
        h.addWidget(self.side_dot, 0, Qt.AlignVCenter)
        h.addSpacing(6)
        self.side_status = QLabel("Paused")
        self.side_status.setFont(caps_font(11, 700, 1.4))
        self.side_status.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 11pt;'
                                       f" color: {theme.TEXT_2};")
        h.addWidget(self.side_status, 0, Qt.AlignVCenter)
        self.live_tag = Tag("LIVE", theme.LIVE, solid=True, pt=10.5)
        self.mode_tag = Tag("TURBO", theme.AMBER, pt=10.5)
        self.mode_tag.setToolTip("Doty is coaching this as a Turbo game: items and levels come about twice as fast. "
                                 "Not Turbo? Set the game mode in Settings > Alerts.")
        self.team_tag = Tag("", theme.GREEN, pt=10.5)
        self.daynight = QLabel("")
        self.daynight.setFont(caps_font(10.5, 700, 1.5))
        self.daynight.setToolTip("Day: everyone sees far. Night: vision shrinks, so ganks are easier.")
        self.clock = QLabel("")
        self.clock.setObjectName("num")
        self.clock.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 800; font-size: 21pt;')
        self.match_bits = (self.live_tag, self.mode_tag, self.team_tag, self.daynight, self.clock)
        for i, w in enumerate(self.match_bits):
            if i:
                h.addSpacing(10)
            h.addWidget(w, 0, Qt.AlignVCenter)
            w.hide()
        # your language, by its flag: one click from every page
        from .. import languages
        from .flags import flag_icon

        code = languages.known(getattr(self.ctl.cfg.app, "language", "en"))
        self.lang_btn = QPushButton()
        self.lang_btn.setObjectName("icon")
        self.lang_btn.setCursor(Qt.PointingHandCursor)
        self.lang_btn.setFocusPolicy(Qt.TabFocus)
        self.lang_btn.setFixedSize(40, 30)
        self.lang_btn.setIcon(flag_icon(code, 16))
        self.lang_btn.setIconSize(QSize(24, 16))
        self.lang_btn.setAccessibleName("Language")
        self.lang_btn.setToolTip(tr("Language: {name}", name=languages.own_name(code)))
        self.lang_btn.clicked.connect(lambda _checked=False: self.choose_language())
        h.addSpacing(14)
        h.addWidget(self.lang_btn, 0, Qt.AlignVCenter)
        return bar

    def choose_language(self) -> None:
        """The language choice (the flag in the top bar). A new language is saved and Doty restarts in it."""
        from ..config import save_config
        from .language_dialog import LanguageDialog

        before = getattr(self.ctl.cfg.app, "language", "en")
        dlg = LanguageDialog(before, self)
        dlg.exec()
        if not dlg.chosen:
            return
        a = self.ctl.cfg.app
        was_picked = getattr(a, "language_picked", False)
        a.language, a.language_picked = dlg.chosen, True
        try:
            save_config(self.ctl.cfg)
        except OSError as exc:
            a.language, a.language_picked = before, was_picked  # (nothing changed: the app stays as it is)
            self.toast.show_message(tr("Couldn't save: {error}", error=exc))
            return
        if dlg.chosen != before:  # (the screens are built in one language)
            self.toast.show_message("Saved. Doty is restarting in your language…")
            QTimer.singleShot(900, relaunch_doty)

    def go(self, index: int) -> None:
        self.nav_buttons[index].setChecked(True)
        self._go(index)

    def open_settings(self, tab: int) -> None:
        """Settings, scrolled to one section (a status chip's "Settings ›")."""
        self.go(SETTINGS)
        QTimer.singleShot(0, lambda: self.settings_page._jump(tab))

    def _sync_rank(self, source) -> None:
        from ..config import save_config

        value = source.currentData()
        for box in (self.draft_page.rank, self.heroes_page.rank):
            if box is not source and box.currentData() != value:
                box.setCurrentIndex(box.findData(value))
        if self.ctl.cfg.coach.bracket != int(value):
            self.ctl.cfg.coach.bracket = int(value)
            try:
                save_config(self.ctl.cfg)
            except OSError:
                pass

    def open_hero(self, name: str) -> None:
        if name:
            self.go(HEROES)
            self.heroes_page.show_hero(name)

    def _go(self, index: int) -> None:
        self._auto_back = None  # any move of yours cancels "Esc goes back"
        if index in (DRAFT, HEROES):
            self._heroes_mode = index
            for page in (self.draft_page, self.heroes_page):
                page.mode.set_mode(index == DRAFT)
        self.nav_buttons[index].setChecked(True)
        self.indicator.move_to(self.nav_buttons[index])
        self.stack.setCurrentIndex(index)
        if index in (SETUP, COACH):  # (a key saved in Settings shows up in Get started at once)
            self.setup_page.recheck()
        elif (index == SETTINGS and self.settings_page.bar.isHidden()
              and getattr(self.settings_page, "_base", None) != self.ctl.cfg):
            self.settings_page._build()  # pick up changes made from the Coach page (only when there are some)

    def _setup_badge(self, attention: int) -> None:
        self.nav_buttons[SETUP].set_badge(attention > 0)
        self.nav_buttons[SETUP].setToolTip(tr("{n} thing(s) to set up: Settings › Setup checklist", n=attention)
                                           if attention else "")
        self.settings_page.set_setup_attention(attention)
        QTimer.singleShot(0, lambda: self.indicator.move_to(self.nav_buttons[self.stack.currentIndex()], False))

    def refresh(self) -> None:
        st = self.ctl.status()
        kind = self._game_kind(st)
        # Paused, or just restarted (saving settings restarts the engine) and no game data yet: that says
        # nothing about the game, so the draft or match carries on rather than looking new.
        if kind in ("off", "waiting"):
            kind = self._game_state if self._game_state in ("draft", "live") else kind
        if kind != self._game_state:
            prev, self._game_state = self._game_state, kind
            self._follow(prev, kind)
        self.coach_page.refresh(st)
        self.draft_page.refresh(st)
        self.heroes_page.refresh(st)
        self.profile_page.refresh(st)
        screen = self.coach_page.screen
        shown = (st is not None and self.stack.currentIndex() == COACH
                 and self.coach_page.left.currentIndex() != SETUP_VIEW)  # (Get started has no picture to fill)
        view = self._live_view_state() if shown else "hidden"
        screen.set_active(view == "seen", covering=view == "covering")
        if st is None:
            screen._placeholder(OFF_TEXT)
        # One status language everywhere (top bar, overlay badge, tray): an eye while Doty watches, turning
        # arrows while it waits; green good, amber waiting, red stopped, grey paused.
        if st is None:
            icon, color, spin, text = (("sync", theme.RED, False, "Couldn't start") if self.ctl.error else
                                       ("eye", theme.GREY, False, "Paused"))
        else:
            icon, color, spin, text = {
                "live": ("eye", theme.GREEN, False, "Coaching live"),
                "menu": ("eye", theme.GREEN, False, "Connected to Dota"),
                "stale": ("sync", theme.RED, False, "Dota not sending"),
            }.get(st["phase"], ("sync", theme.AMBER, True, "Waiting for Dota"))
        self.side_dot.set_status(icon, color, spin)
        self.side_dot.setToolTip(text)
        self.side_status.setText(text)
        self._show_match(st)
        if self.tray is not None:
            self.tray.setToolTip(f"Doty: {tr(text)}")
            self.tray.show_status(icon, color)

    def _show_match(self, st: dict | None) -> None:
        """In a match the top bar carries it: LIVE (or PAUSED, or DEMO), your team, day or night, the clock."""
        live = st is not None and st["phase"] == "live"
        self.side_dot.setVisible(not live)
        self.side_status.setVisible(not live)
        for w in self.match_bits:
            w.setVisible(live)
        if not live:
            return
        g = st.get("game") or {}
        self.mode_tag.setVisible(g.get("mode") == "turbo")
        if self.ctl.demo_running:
            self.live_tag.set("DEMO", theme.BRAND, solid=True)
        elif st.get("paused"):
            self.live_tag.set("PAUSED", theme.AMBER, solid=True)
        else:
            self.live_tag.set("LIVE", theme.LIVE, solid=True)
        team = (g.get("team") or "").lower()
        self.team_tag.set(team.upper() if team in ("radiant", "dire") else "",
                          theme.GREEN if team == "radiant" else theme.RED)
        day = g.get("daytime", True)
        self.daynight.setText("Day" if day else "Night")
        self.daynight.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 10.5pt;'
                                    f" color: {'#ffd27a' if day else '#9fb6ff'};")
        self.clock.setText(mmss(st.get("clock") or 0))

    @staticmethod
    def _game_kind(st: dict | None) -> str:
        if st is None:
            return "off"
        if st["phase"] == "waiting":
            return "waiting"
        if st.get("draft"):
            return "draft"
        if st["phase"] == "live":
            return "live"
        if st.get("post_game"):
            return "post"
        return "idle"

    def _follow(self, prev: str, kind: str) -> None:
        """Open the page that fits the moment: Draft for hero selection, Coach once the match starts.
        Pages you're in the middle of (Setup, Settings) are left alone, and Esc goes back."""
        if kind == "draft" and prev != "draft":
            self.draft_page.new_draft()  # last game's picks are done with
        if kind == "live" and prev != "live":
            self.ctl.stop_reading()  # (a lesson read aloud stops when the match starts: the coach takes over)
        if not self.ctl.cfg.app.follow_game:
            return
        here = self.stack.currentIndex()
        if kind == "draft" and here in (COACH, HEROES, PROFILE, LEARN):
            self.go(DRAFT)
            self._auto_back = here
            self.toast.show_message("Hero selection: Draft is open. Esc goes back.")
        elif kind == "live" and here in (DRAFT, HEROES, PROFILE, LEARN):
            self.go(COACH)
            self._auto_back = here
            self.toast.show_message("The match started: Coach is open. Esc goes back.")
        elif kind == "post" and prev == "live" and here != COACH:
            self.toast.show_message("Game over. Your recap is on the Coach page.")

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            if self._auto_back is not None:
                back, self._auto_back = self._auto_back, None
                self.go(back)
                return
            if self.stack.currentIndex() == HEROES and self.heroes_page.stack.currentIndex() == 1:
                self.heroes_page.show_grid()
                return
        super().keyPressEvent(event)

    def _live_view_state(self) -> str:
        """Whether the live view can film Dota for you: "seen" when it can; "hidden" while nobody can see
        this window (minimized, or Dota is on top while you play); "covering" while this window is in front
        of Dota, where filming that part of the screen would only film Doty."""
        if not self.isVisible() or self.isMinimized():
            return "hidden"
        if sys.platform.startswith("linux"):
            dota = find_dota_rect()
            if dota is None:
                return "seen"
            from ..overlay import logical_to_native

            g, screen = self.frameGeometry(), self.screen()
            left, top = logical_to_native(screen, g.x(), g.y())
            right, bottom = logical_to_native(screen, g.x() + g.width(), g.y() + g.height())
            x, y, w, h = dota
            if not (left < x + w and x < right and top < y + h and y < bottom):
                return "seen"
            return "covering" if self.isActiveWindow() else "hidden"
        if sys.platform != "win32":
            return "seen"
        dota = find_dota_rect()
        if dota is None:
            return "seen"  # nothing to film: the view says Dota isn't open
        import ctypes
        from ctypes import wintypes

        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(int(self.winId()), ctypes.byref(r))
        x, y, w, h = dota
        overlaps = r.left < x + w and x < r.right and r.top < y + h and y < r.bottom
        if not overlaps:
            return "seen"  # e.g. Doty on your second monitor while you play
        return "covering" if self.isActiveWindow() else "hidden"

    # window behavior -------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        theme.dark_title_bar(self)
        QTimer.singleShot(0, self.centralWidget().setFocus)  # Tab still walks the tabs and buttons from here
        QTimer.singleShot(0, lambda: self.indicator.move_to(self.nav_buttons[self.stack.currentIndex()], False))
        if not self._tagged:
            self._tagged = True
            from ..config import is_user_config
            from ..shortcuts import add_shortcuts_once, tag_window, update_existing_shortcuts

            if is_user_config(self.ctl.cfg):
                try:
                    add_shortcuts_once()  # Start menu and desktop, the first time Doty opens
                except Exception as exc:
                    print(f"[doty] couldn't add the shortcuts: {exc!r}")

            try:
                tag_window(int(self.winId()))
            except Exception as exc:
                print(f"[doty] couldn't set the taskbar icon: {exc!r}")
            try:
                update_existing_shortcuts()  # repair the desktop/Start shortcuts (icon, target)
            except Exception as exc:
                print(f"[doty] couldn't update the shortcuts: {exc!r}")

    def bring_to_front(self) -> None:
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        tray_ok = self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable()
        quitting = bool(QApplication.instance().property("doty_quitting"))
        if self.ctl.cfg.app.close_to_tray and self.ctl.running and tray_ok and not quitting:
            event.ignore()
            self.hide()
            if not self._told_about_tray:
                self._told_about_tray = True
                self.tray.showMessage("Doty is still coaching",
                                      "It's in the tray. Right-click the icon to quit.", app_icon(), 4000)
            return
        event.accept()
        quit_doty()


class AppTray(QSystemTrayIcon):
    def __init__(self, window: MainWindow, ctl: Controller) -> None:
        super().__init__(app_icon(), window)
        self.window, self.ctl = window, ctl
        self.setToolTip("Doty")
        self._status: tuple[str, str] | None = None
        from .pages import COACH_PRESETS

        menu = QMenu()
        self.a_open = QAction("Open Doty", menu)
        self.a_power = QAction("Pause coaching", menu)
        self.a_ask = QAction("Ask Doty now", menu)
        self.a_plan = QAction("5-minute plan", menu)
        self.a_mute = QAction("Mute voice", menu)
        self.a_cards = QAction("Hide the cards for now", menu)
        self.a_quit = QAction("Quit Doty", menu)
        self.a_open.triggered.connect(window.bring_to_front)
        self.a_power.triggered.connect(lambda: ctl.stop() if ctl.running else ctl.start())
        self.a_ask.triggered.connect(lambda: ctl.ask("now"))
        self.a_plan.triggered.connect(lambda: ctl.ask("plan"))
        self.a_mute.triggered.connect(lambda: ctl.toggle_mute())
        self.a_cards.triggered.connect(lambda: ctl.toggle_overlay())
        self.a_quit.triggered.connect(quit_doty)
        self.coaching = QMenu("How chatty the coach is", menu)
        group = QActionGroup(self.coaching)
        self.a_presets = {}
        for key, name, look, speak, _detail in COACH_PRESETS:
            a = QAction(name, self.coaching)
            a.setCheckable(True)
            a.triggered.connect(lambda _on=False, lk=look, sp=speak: ctl.set_coaching(look_every_s=lk, speak_up=sp))
            group.addAction(a)
            self.coaching.addAction(a)
            self.a_presets[key] = a
        for a in (self.a_open, None, self.a_power, self.a_ask, self.a_plan, self.coaching, self.a_mute, self.a_cards,
                  None, self.a_quit):
            if a is None:
                menu.addSeparator()
            elif isinstance(a, QMenu):
                menu.addMenu(a)
            else:
                menu.addAction(a)
        menu.aboutToShow.connect(self._update_menu)
        self.setContextMenu(menu)
        self._menu = menu  # Qt doesn't take ownership
        self.activated.connect(self._on_activated)

    def show_status(self, icon: str, color: str) -> None:
        """Doty's mark with a small status dot in the corner, in the same colors as the top bar and the
        overlay badge (green watching, amber waiting, red stopped, grey paused)."""
        if (icon, color) == self._status:
            return
        self._status = (icon, color)
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QIcon, QPainter

        icon_out = QIcon()
        for size in (16, 20, 24, 32, 48):
            pm = app_icon().pixmap(size, size)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            d = size * 0.42
            p.setPen(QColor(theme.BG))
            p.setBrush(QColor(color))
            p.drawEllipse(QRectF(size - d - 0.5, size - d - 0.5, d, d))
            p.end()
            icon_out.addPixmap(pm)
        self.setIcon(icon_out)

    def _update_menu(self) -> None:
        from .pages import coach_preset

        running = self.ctl.running
        self.a_power.setText("Pause coaching" if running else "Resume coaching")
        c = self.ctl.cfg.coach
        preset = coach_preset(c.speculative_interval_s, c.speak_up)
        for key, a in self.a_presets.items():
            a.setChecked(preset is not None and preset[0] == key)
        self.coaching.setEnabled(c.enabled)
        overlay = self.ctl.doty.overlay if running else None
        self.a_cards.setEnabled(overlay is not None)
        self.a_cards.setText("Show the cards" if overlay is not None and not overlay.isVisible() else
                             "Hide the cards for now")
        coach_ready = running and self.ctl.doty.coach.status.startswith("ready")
        self.a_ask.setEnabled(coach_ready)
        self.a_plan.setEnabled(coach_ready)
        self.a_mute.setEnabled(running and self.ctl.cfg.voice.enabled)
        self.a_mute.setText("Unmute voice" if running and self.ctl.doty.voice.muted else "Mute voice")

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.window.bring_to_front()
