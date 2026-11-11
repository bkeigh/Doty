"""Doty.exe, the installed app (the PyInstaller entry point).

    Doty.exe                         open Doty
    Doty.exe --selftest report.json  check this build without opening a window
    Doty.exe --uninstall-cleanup     remove Doty's file from Dota's folder (run by the uninstaller)
"""

import multiprocessing
import sys


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--selftest"]:
        from doty.selftest import run

        return run(args[1] if len(args) > 1 else None)
    if args[:1] == ["--uninstall-cleanup"]:
        try:
            from doty.gsi import install

            install.uninstall()
        except Exception:
            pass  # never block an uninstall
        return 0
    from doty.ui.main import main as app_main

    return app_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
