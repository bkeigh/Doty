"""Start-menu and desktop shortcuts, so Doty opens like any other app and can be
pinned to the taskbar.

The shortcut and the running window share an AppUserModelID. That is what lets
Windows pin the *shortcut* (which knows to run `-m doty app`) rather than the bare
pythonw.exe when you right-click the taskbar icon and choose "Pin to taskbar".

Linux: a .desktop entry instead (the applications menu: ~/.local/share/applications/doty.desktop, and
the desktop folder), tied to the window by its desktop file name and window class, as docks expect.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

AUMID = "Doty.DotaCoach"
NAME = "Doty"
DESCRIPTION = "Doty: live Dota 2 coach"
DESKTOP_ID = "doty"  # Linux: the entry is doty.desktop, and the window says it belongs to it


def _linux() -> bool:
    return sys.platform.startswith("linux")


def set_process_app_id() -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    elif _linux():
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.setDesktopFileName(DESKTOP_ID)  # docks match the window to doty.desktop by this


def tag_window(hwnd: int) -> None:
    """Tell the taskbar this window is Doty: its app ID, icon, name and how to relaunch it
    when pinned. Without this the taskbar can fall back to a generic icon for pythonw."""
    if sys.platform != "win32":
        return
    import pythoncom
    from win32com.propsys import propsys, pscon

    target, args = launch_command()

    def key(name: str):
        return getattr(pscon, name, None) or propsys.PSGetPropertyKeyFromName(
            "System.AppUserModel." + name.removeprefix("PKEY_AppUserModel_"))

    store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
    for name, value in (("PKEY_AppUserModel_ID", AUMID),
                        ("PKEY_AppUserModel_RelaunchCommand", f'"{target}" {args}'.strip()),
                        ("PKEY_AppUserModel_RelaunchDisplayNameResource", NAME),
                        ("PKEY_AppUserModel_RelaunchIconResource", f"{shortcut_icon()},0")):
        store.SetValue(key(name), propsys.PROPVARIANTType(value, pythoncom.VT_LPWSTR))
    store.Commit()


def refresh_shell_icons(paths=()) -> None:
    """Make Explorer re-read these shortcuts and redraw icons (after replacing the icon file)."""
    if sys.platform == "win32":
        import ctypes

        notify = ctypes.windll.shell32.SHChangeNotify
        notify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_void_p]
        for p in paths:
            notify(0x00002000, 0x0005, str(p), None)  # SHCNE_UPDATEITEM, SHCNF_PATHW
        notify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED


def launch_command() -> tuple[str, str]:
    """(program, arguments) that open the Doty window without a console."""
    from .config import FROZEN

    if FROZEN:  # the installed Doty.exe opens the window by itself
        return sys.executable, ""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe), "-m doty app"


def shortcut_icon() -> str:
    """The icon for shortcuts and the taskbar: Doty.exe carries it when installed."""
    from .config import FROZEN

    if FROZEN:
        return sys.executable
    if _linux():  # menus and launchers want a PNG
        from .ui.icon import write_png

        return str(write_png())
    from .ui.icon import write_ico

    return str(write_ico())


def _folder(csidl: int) -> Path:
    from win32com.shell import shell

    return Path(shell.SHGetFolderPath(0, csidl, None, 0))


def start_menu_path() -> Path:
    if _linux():
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return data / "applications" / f"{DESKTOP_ID}.desktop"
    from win32com.shell import shellcon

    return _folder(shellcon.CSIDL_PROGRAMS) / f"{NAME}.lnk"


def desktop_path() -> Path:
    if _linux():
        from .config import xdg_user_dir

        return (xdg_user_dir("DESKTOP") or Path.home() / "Desktop") / f"{DESKTOP_ID}.desktop"
    from win32com.shell import shellcon

    return _folder(shellcon.CSIDL_DESKTOPDIRECTORY) / f"{NAME}.lnk"


def _exec_quote(arg: str) -> str:
    """One argument of a .desktop Exec line: quoted, with the characters the spec reserves escaped."""
    if arg and not any(c in arg for c in " \t\n\"'\\><~|&;$*?#()`="):
        return arg
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return f'"{escaped}"'


def desktop_entry(target: str, arguments: str, icon: Path, workdir: Path, app_id: str = AUMID,
                  description: str = DESCRIPTION) -> str:
    """A .desktop entry (freedesktop.org's shortcut) that opens Doty."""
    exec_line = " ".join([_exec_quote(target), *(_exec_quote(a) for a in arguments.split())])
    return "".join(f"{line}\n" for line in (
        "[Desktop Entry]", "Type=Application", f"Name={NAME}", f"Comment={description}", f"Exec={exec_line}",
        f"Path={workdir}", f"Icon={icon}", "Terminal=false", "Categories=Game;", "StartupNotify=true",
        f"StartupWMClass={NAME}", f"X-Doty-AppId={app_id}"))


def _exec_split(line: str) -> list[str]:
    """An Exec line back into its arguments (the spec's quoting: inside quotes, \\ escapes " ` $ and \\)."""
    parts, cur, quoted, i, started = [], [], False, 0, False
    while i < len(line):
        c = line[i]
        if quoted and c == "\\" and i + 1 < len(line) and line[i + 1] in '"`$\\':
            cur.append(line[i + 1])
            i += 1
        elif c == '"':
            quoted, started = not quoted, True
        elif c == " " and not quoted:
            if cur or started:
                parts.append("".join(cur))
            cur, started = [], False
        else:
            cur.append(c)
        i += 1
    if cur or started:
        parts.append("".join(cur))
    return parts


def read_desktop_entry(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() not in fields:
            fields[name.strip()] = value.strip()
    parts = _exec_split(fields.get("Exec", ""))
    return {"target": parts[0] if parts else "", "arguments": " ".join(parts[1:]),
            "icon": fields.get("Icon", ""), "app_id": fields.get("X-Doty-AppId", "")}


def create_shortcut(path: Path, target: str, arguments: str, icon: Path, workdir: Path,
                    app_id: str = AUMID, description: str = DESCRIPTION) -> Path:
    if _linux():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry(target, arguments, icon, workdir, app_id, description), encoding="utf-8")
        path.chmod(0o755)  # launchers on the desktop must be executable to open
        return path
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.SetPath(target)
        link.SetArguments(arguments)
        link.SetWorkingDirectory(str(workdir))
        link.SetIconLocation(str(icon), 0)
        link.SetDescription(description)
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id, pythoncom.VT_LPWSTR))
        store.Commit()
        path.parent.mkdir(parents=True, exist_ok=True)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
        del store, link
    finally:
        pythoncom.CoUninitialize()
    return path


def read_shortcut(path: Path) -> dict:
    if _linux():
        return read_desktop_entry(path.read_text(encoding="utf-8", errors="replace"))
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        info = {
            "target": link.GetPath(shell.SLGP_RAWPATH)[0],
            "arguments": link.GetArguments(),
            "icon": link.GetIconLocation()[0],
            "app_id": store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue(),
        }
        del store, link
        return info
    finally:
        pythoncom.CoUninitialize()


def install_shortcuts(desktop: bool = True) -> list[Path]:
    from .config import FROZEN, PROJECT_ROOT

    icon = shortcut_icon()
    target, args = launch_command()
    workdir = Path(sys.executable).parent if FROZEN else PROJECT_ROOT
    paths = [start_menu_path()] + ([desktop_path()] if desktop else [])
    made = [create_shortcut(p, target, args, Path(icon), workdir) for p in paths]
    refresh_shell_icons(made)
    return made


def update_existing_shortcuts() -> None:
    """Repair Doty's shortcuts each time the app opens: point them at the current icon and
    launch command, and add the Start-menu one if only the desktop one exists.

    This runs inside the app on purpose. `doty shortcut` run from a packaged (MSIX) app, such as
    a Store terminal or the Claude app, has its AppData writes (the Start menu) silently
    redirected into that app's private folder, so that shortcut never really appears. The app
    itself is launched from the desktop and writes to the real Start menu."""
    from .config import FROZEN

    if (sys.platform != "win32" and not _linux()) or FROZEN:  # an installed Doty's belong to its installer
        return
    links = installed_shortcuts()
    if not links:  # never added, or removed on purpose
        return
    infos = [read_shortcut(p) for p in links]
    if any(i["target"].lower().endswith("doty.exe") for i in infos):
        return  # the installed Doty owns these; a source checkout must not repoint them
    icon = shortcut_icon().lower()
    target, args = launch_command()

    def current(info: dict) -> bool:
        return (info["icon"].lower() == icon and info["target"].lower() == target.lower()
                and info["arguments"] == args and info["app_id"] == AUMID)

    if start_menu_path() not in links or not all(current(i) for i in infos):
        install_shortcuts(desktop=desktop_path() in links)


def add_shortcuts_once() -> bool:
    """Add the Start-menu and desktop shortcuts the first time Doty opens from a source checkout (the
    installer adds its own). Never again after that, so deleting one on purpose sticks."""
    from .config import CACHE_DIR, FROZEN

    marker = CACHE_DIR.parent / "state" / "shortcuts-added"
    if (sys.platform != "win32" and not _linux()) or FROZEN or marker.exists():
        return False
    if not installed_shortcuts():
        install_shortcuts(desktop=not _linux())  # Linux: the applications menu (many desktops hide desktop icons)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("Doty added its shortcuts once; it won't add them again.\n", encoding="utf-8")
    return True


def installed_shortcuts() -> list[Path]:
    if sys.platform != "win32" and not _linux():
        return []
    return [p for p in (start_menu_path(), desktop_path()) if p.exists()]


def remove_shortcuts() -> list[Path]:
    removed = []
    for p in installed_shortcuts():
        p.unlink()
        removed.append(p)
    return removed
