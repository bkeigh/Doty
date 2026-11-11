"""Nothing in Doty's window ever pops up as a window of its own.

A widget that is shown (show() / setVisible(True)) before it has a parent becomes a top-level window for an
instant: on Windows a small white frame titled "Doty" flashes up. Opening Profile used to flash dozens of them
(the Learn tab rebuilding its tip cards when the hero list arrived). This walks every tab and the rebuilds that
happen in the background, and fails on any such window."""

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QWidget

from tests.test_app_ui import make_window, pump, qapp  # noqa: F401 (qapp is a fixture)
from tests.test_profile_page_ui import PLAYER, _games


class _Spy(QObject):
    def __init__(self, allowed) -> None:
        super().__init__()
        self.allowed, self.seen = allowed, []

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow()
                and obj not in self.allowed and obj.windowType() not in (Qt.Popup, Qt.ToolTip, Qt.SplashScreen)):
            self.seen.append(f"{type(obj).__name__}: {getattr(obj, 'text', lambda: '')() if hasattr(obj, 'text') else ''}")
        return False


def test_no_widget_flashes_up_as_its_own_window(qapp, tmp_path):
    from doty.ui.window import COACH, HEROES, LEARN, PROFILE, SETTINGS, SETUP

    _ctl, win = make_window(qapp, tmp_path)
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    spy = _Spy([win])
    qapp.installEventFilter(spy)
    try:
        win.resize(1340, 920)
        win.show()
        pump(qapp, 0.5)
        for page in (LEARN, PROFILE, HEROES, SETTINGS, SETUP, COACH):
            win.go(page)
            pump(qapp, 0.4)
        win.learn_page._on_stats()  # (the hero list arriving while you're on another tab)
        win.learn_page.render(force=True)
        win.go(LEARN)
        pump(qapp, 0.4)
        win.profile_page._shown = 42
        win.profile_page._on_result("profile", (42, PLAYER, _games(8)))
        win.go(PROFILE)
        pump(qapp, 0.4)
        win.resize(800, 900)  # (narrow: cards and columns lay out again)
        for page in (LEARN, PROFILE, SETTINGS):
            win.go(page)
            pump(qapp, 0.3)
    finally:
        qapp.removeEventFilter(spy)
        win.close()
    assert spy.seen == []
