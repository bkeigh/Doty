"""Locate the Dota 2 install and write Doty's Game State Integration config."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ..config import Config

CFG_NAME = "gamestate_integration_doty.cfg"
DOTA_SUBDIR = Path("steamapps") / "common" / "dota 2 beta"
GSI_SUBDIR = Path("game") / "dota" / "cfg" / "gamestate_integration"
LAUNCH_OPTION = "-gamestateintegration"
# The game executable under game/bin: Windows, then the native Linux build.
GAME_EXES = (Path("win64") / "dota2.exe", Path("linuxsteamrt64") / "dota2")

# Sections Dota sends while you're *playing*. Spectator-only sections
# (draft, minimap, roshan, couriers, neutralitems, all-player data) are
# simply absent in your own games; requesting them is harmless. What a player
# actually receives isn't documented: capture mode (capture.py) records any
# section that shows up, so it can be checked. Doty only ever uses what your own
# screen shows (fair play), whatever a section contains.
DATA_SECTIONS = [
    "provider", "map", "player", "hero", "abilities", "items", "events",
    "buildings", "league", "draft", "wearables", "couriers", "neutralitems", "roshan", "minimap",
]
DISABLED_SECTIONS = {"wearables", "league"}


def steam_root() -> Path | None:
    if sys.platform == "win32":
        import winreg

        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    path = Path(winreg.QueryValueEx(k, value)[0])
                    if path.is_dir():
                        return path
            except OSError:
                continue
    for guess in steam_guesses():
        if guess.is_dir():
            # Linux: follow ~/.steam/steam to the real folder, the path libraryfolders.vdf lists it by.
            return guess.resolve() if sys.platform.startswith("linux") else guess
    return None


def steam_guesses(platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    """Where Steam usually lives when the registry doesn't say. Linux: the native install (~/.steam/steam
    is a link to it, usually ~/.local/share/Steam), then the Flatpak and Snap packages of Steam."""
    home = home if home is not None else Path.home()
    if platform.startswith("linux"):
        return [home / ".steam" / "steam", home / ".local" / "share" / "Steam", home / ".steam" / "root",
                home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
                home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"]
    return [Path(r"C:\Program Files (x86)\Steam"), home / ".steam" / "steam"]


def library_folders(root: Path) -> list[Path]:
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            p = Path(raw.replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    return libs


def dota_installs(root: Path | None = None) -> list[Path]:
    """Every 'dota 2 beta' folder across Steam libraries, the real install first.
    Old libraries can keep a leftover folder (cfg files, no game); the real one has
    Steam's install record (appmanifest_570.acf) and the game executable."""
    root = root or steam_root()
    if not root:
        return []
    found = []
    for lib in library_folders(root):
        candidate = lib / DOTA_SUBDIR
        if (candidate / "game" / "dota").is_dir():
            score = ((lib / "steamapps" / "appmanifest_570.acf").is_file()
                     + any((candidate / "game" / "bin" / exe).is_file() for exe in GAME_EXES))
            found.append((-score, len(found), candidate))
    return [c for _, _, c in sorted(found)]


def find_dota() -> Path | None:
    installs = dota_installs()
    return installs[0] if installs else None


def render_cfg(cfg: Config) -> str:
    g = cfg.gsi
    data = "\n".join(
        f'        "{name}"{" " * max(1, 16 - len(name))}"{0 if name in DISABLED_SECTIONS else 1}"'
        for name in DATA_SECTIONS
    )
    return f'''"Doty"
{{
    "uri"           "http://{g.host}:{g.port}/"
    "timeout"       "5.0"
    "buffer"        "{g.buffer}"
    "throttle"      "{g.throttle}"
    "heartbeat"     "{g.heartbeat}"
    "data"
    {{
{data}
    }}
    "auth"
    {{
        "token"     "{g.auth_token}"
    }}
}}
'''


def install(cfg: Config, dota_dir: Path | None = None) -> Path:
    dota_dir = dota_dir or find_dota()
    if dota_dir is None:
        example = ("~/.local/share/Steam/steamapps/common/dota 2 beta" if sys.platform.startswith("linux")
                   else "...\\steamapps\\common\\dota 2 beta")
        raise FileNotFoundError(f"Couldn't find Dota 2. Pass --dota-dir \"{example}\".")
    target_dir = dota_dir / GSI_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CFG_NAME
    target.write_text(render_cfg(cfg), encoding="utf-8")
    return target


def uninstall(dota_dir: Path | None = None) -> Path | None:
    dota_dir = dota_dir or find_dota()
    if dota_dir is None:
        return None
    target = dota_dir / GSI_SUBDIR / CFG_NAME
    if target.exists():
        target.unlink()
        return target
    return None


def launch_option_set(steam: Path | None = None) -> bool | None:
    """True/False if we can tell whether -gamestateintegration is in Dota's
    launch options for any local Steam user; None if we can't tell."""
    steam = steam or steam_root()
    if not steam:
        return None
    found_any = False
    for vdf in (steam / "userdata").glob("*/config/localconfig.vdf"):
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        found_any = True
        # The app block for Dota is keyed "570"; launch options live inside it.
        for m in re.finditer(r'"570"\s*\{', text):
            block = text[m.end(): m.end() + 4000]
            lo = re.search(r'"LaunchOptions"\s+"([^"]*)"', block)
            if lo and LAUNCH_OPTION in lo.group(1):
                return True
    return False if found_any else None
