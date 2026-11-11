"""Entry point for the Doty app window (`doty app`, or the Start-menu shortcut)."""

from __future__ import annotations

import getpass
import sys
import time
from pathlib import Path

from ..config import Config, load_config

INSTANCE_NAME = f"doty-app-{getpass.getuser()}"
# Set for the copy Doty starts when it restarts itself (a new language): that copy waits for this one to
# finish closing and takes over, instead of finding it still there and handing back to it.
TAKEOVER_ENV = "DOTY_TAKEOVER"
# A named mutex while Doty is open, so the installer and uninstaller (Inno Setup's AppMutex)
# can ask you to close it first instead of failing on files in use.
APP_MUTEX = "DotyCoachRunning"
_mutex = None


def _log_to_file_if_windowless() -> None:
    """pythonw has no console (sys.stdout is None): send prints and tracebacks to a log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from ..config import LOG_DIR

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "app.log"
    if path.exists() and path.stat().st_size > 2_000_000:
        path.replace(path.with_suffix(".log.1"))
    fh = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    fh.write(f"\n===== Doty app started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    sys.stdout = sys.stderr = fh


def code_stamp() -> str:
    """Which build of Doty this is: changes whenever the code on disk changes (an update, or an edit
    to a source checkout), so a newer launch can tell that the copy already running is out of date."""
    from .. import __version__
    from ..config import FROZEN

    if FROZEN:
        return f"{__version__}:{Path(sys.executable).stat().st_mtime:.0f}"
    root = Path(__file__).resolve().parents[1]
    newest = max((f.stat().st_mtime for f in root.rglob("*") if f.suffix in (".py", ".svg", ".ttf")), default=0)
    return f"{__version__}:{newest:.0f}"


def _hand_off_to_running_instance(stamp: str) -> bool:
    """If Doty is already open, ask it to come to the front and return True.

    If that copy is an older build, it closes instead, and this launch takes over (returns False)."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_NAME)
    if not sock.waitForConnected(300):
        return False
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it come to the front
    sock.write(b"show " + stamp.encode())
    sock.flush()
    sock.waitForBytesWritten(500)
    reply = bytes(sock.readAll()) if sock.waitForReadyRead(1500) else b""
    sock.disconnectFromServer()
    if not reply.startswith(b"restart"):
        return True
    print("[doty] an older Doty was running; it's closing so this one can take over", flush=True)
    _wait_until_gone()
    return False


def _wait_until_gone() -> None:
    """Wait (up to 20 s) until the Doty that's closing has fully exited: its port and mutex are free."""
    from PySide6.QtNetwork import QLocalSocket

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:  # wait until it has fully exited (its port and mutex are free)
        if sys.platform == "win32":
            import ctypes

            h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, APP_MUTEX)  # SYNCHRONIZE
            if not h:
                break
            ctypes.windll.kernel32.CloseHandle(h)
        else:
            probe = QLocalSocket()
            probe.connectToServer(INSTANCE_NAME)
            if not probe.waitForConnected(200):
                break
            probe.disconnectFromServer()
        time.sleep(0.25)


def main(cfg: Config | None = None) -> int:
    _log_to_file_if_windowless()
    t0 = time.perf_counter()
    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from ..shortcuts import set_process_app_id

    if sys.platform.startswith("linux"):
        from .. import keystore, x11

        x11.prefer_xcb()  # (before the QApplication) on Wayland, the overlay needs X11 to sit over the game
        keystore.load_into_environ()  # the API keys saved in Settings
    elif sys.platform == "win32":
        from .. import keystore

        keystore.load_into_environ()  # the API keys saved in Settings (Windows Credential Manager)
    set_process_app_id()  # taskbar grouping and pinning use the shortcut's identity
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Doty")
    app.setApplicationDisplayName("Doty")
    app.setQuitOnLastWindowClosed(False)
    stamp = code_stamp()
    import os

    if os.environ.pop(TAKEOVER_ENV, "") == "1":
        _wait_until_gone()  # (started by a Doty that's restarting itself: never hand back to it)
    elif _hand_off_to_running_instance(stamp):
        return 0
    from .. import i18n

    cfg = cfg or load_config()
    server = QLocalServer()
    QLocalServer.removeServer(INSTANCE_NAME)  # clean up after a crash
    server.listen(INSTANCE_NAME)
    if sys.platform == "win32":
        import ctypes

        global _mutex
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)  # held until exit
    from .language_dialog import ask_at_first_run, first_run_wanted

    if first_run_wanted(cfg):  # a new install: your language first, so everything after it is in it
        from . import theme as _theme
        from .icon import app_icon as _app_icon

        _theme.apply(app)
        app.setWindowIcon(_app_icon())
        ask_at_first_run(cfg)
        t0 = time.perf_counter()  # (the splash's minimum time starts after the choice)
    i18n.set_language(getattr(cfg.app, "language", "en"))  # (before anything is drawn: the splash too)
    i18n.install_qt()

    # Show the splash art right away; the slow imports happen behind it.
    from .splash import make_splash

    splash = make_splash()
    if splash is not None:
        splash.show()
        app.processEvents()
    # Heavy (aiohttp, rules, audio): load it now, not on Start. The AI SDK isn't in it; it loads behind the window.
    from .. import app as _engine  # noqa: F401
    from . import theme
    from .controller import Controller
    from .icon import app_icon
    from .window import AppTray, MainWindow

    theme.apply(app)
    app.setWindowIcon(app_icon())
    ctl = Controller(cfg)
    win = MainWindow(ctl)
    tray = AppTray(win, ctl)
    win.tray = tray
    tray.show()

    def on_second_launch() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            win.bring_to_front()
            return
        msg = bytes(conn.readAll()) if conn.waitForReadyRead(500) else b""
        theirs = msg.removeprefix(b"show").strip().decode(errors="replace")
        if theirs and theirs != stamp:  # a newer Doty was launched: hand over to it
            conn.write(b"restart")
            conn.flush()
            conn.waitForBytesWritten(500)
            conn.disconnectFromServer()
            server.close()
            from .window import quit_doty

            QTimer.singleShot(50, quit_doty)  # (a plain quit is cancelled while the window is open)
            return
        conn.deleteLater()
        win.bring_to_front()

    server.newConnection.connect(on_second_launch)
    app.aboutToQuit.connect(win.coach_page.screen.stop)
    app.aboutToQuit.connect(ctl.stop)
    if splash is not None:
        # Long enough to see the art, never longer than it takes to load.
        while time.perf_counter() - t0 < 1.4:
            app.processEvents()
            time.sleep(0.02)
    win.show()
    if splash is not None:
        splash.finish(win)
    print(f"[doty] window up {time.perf_counter() - t0:.2f} s after launch", flush=True)
    # The AI SDK takes ~2 s to import: load it behind the open window so the coach's first use doesn't wait.
    import threading

    from ..coach.backends import preload

    threading.Thread(target=preload, name="doty-preload", daemon=True).start()
    if cfg.app.start_coaching_on_open:
        QTimer.singleShot(250, ctl.start)
    QTimer.singleShot(1500, ctl.fetch_language_voices)  # (Kokoro is here, your new language's voices aren't)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
