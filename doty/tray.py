"""System-tray icon: the only clickable part of Doty (the overlay is click-through)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

if TYPE_CHECKING:
    from .app import Doty


def _icon() -> QIcon:
    from .ui.icon import app_icon

    return app_icon()


def make_tray(app: QApplication, doty: "Doty") -> QSystemTrayIcon:
    tray = QSystemTrayIcon(_icon(), app)
    tray.setToolTip("Doty — Dota 2 coach")
    menu = QMenu()

    ask = QAction("Ask Doty now", menu)
    ask.triggered.connect(lambda: doty.coach.ask("now"))
    plan = QAction("5-minute plan", menu)
    plan.triggered.connect(lambda: doty.coach.ask("plan"))
    mute = QAction("Mute voice", menu, checkable=True)
    mute.triggered.connect(lambda _checked: doty._toggle_mute())
    hide = QAction("Hide overlay", menu, checkable=True)
    hide.triggered.connect(lambda _checked: doty.overlay.toggle_visible())
    quit_ = QAction("Quit Doty", menu)
    quit_.triggered.connect(app.quit)

    for action in (ask, plan, None, mute, hide, None, quit_):
        if action is None:
            menu.addSeparator()
        else:
            menu.addAction(action)
    tray.setContextMenu(menu)
    tray.show()
    tray._menu = menu  # keep a reference; Qt doesn't own it
    return tray
