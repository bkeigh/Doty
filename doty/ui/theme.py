"""Colors, fonts and the app-wide stylesheet."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from ..config import CACHE_DIR

# "Broadcast": an esports HUD. Near-black panels with a cut corner, condensed capitals for labels and
# numbers, one cyan for Doty itself, team colors for good and bad. No Valve art.
BG = "#06080b"
HEADER = "#0b0e13"
SURFACE = "#0e1217"  # panels
SURFACE_2 = "#121820"  # fields and quiet buttons
SURFACE_HOVER = "#1a222c"
BORDER = "#1a2028"
BORDER_STRONG = "#283240"
TEXT = "#f2f4f7"
TEXT_2 = "#a3aeba"
MUTED = "#8793a0"  # ~5.5:1 on the panels
BRAND = "#3fd4ff"  # Doty: the coach's lines, the main button, focus, links, the selected tab
BRAND_BRIGHT = "#8fe6ff"
BRAND_DIM = "#3aa7c8"  # the coach just watching
INK = "#04131a"  # text on cyan
GOLD = "#ffc94d"  # money: your gold, what an item still costs
GREEN = "#4fd16a"  # Radiant, good
AMBER = "#ffaa2b"  # coming up soon, needs a look
RED = "#ff5a4a"  # Dire, danger, act now
BLUE = "#5b9cf0"  # information
GREY = "#6b7682"
HP = "#3fbf5a"
MANA = "#3b7fe0"
RADIUS = 2
CUT = 12  # the cut top-right corner of a panel

NEUTRAL = "#e9edf1"  # you, and plain facts: no judgment attached

# What each color means, everywhere: cyan = Doty, green = good, red = danger and the enemy,
# amber = needs a look or coming up soon, blue = information, gold = gold.
STATE_COLORS = {"ok": GREEN, "warn": AMBER, "bad": RED, "info": BLUE, "off": GREY}

FONT = "Barlow"  # body text (SIL OFL, bundled)
DISPLAY = "Barlow Condensed"  # labels, numbers, titles, buttons (SIL OFL, bundled)
MONO = "Cascadia Mono, Consolas"
ICON_FONT = "Segoe MDL2 Assets"  # ships with Windows 10 and 11


LIVE = "#d42c1e"  # the LIVE tag: a deeper red, so white letters read on it (5:1)


def rgba(color: str, alpha: float) -> str:
    """A theme color at some opacity, for stylesheets."""
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _luminance(c: QColor) -> float:
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(c.redF()) + 0.7152 * lin(c.greenF()) + 0.0722 * lin(c.blueF())


def ink_on(fill: str | QColor) -> str:
    """Dark ink or white, whichever reads better on `fill` (WCAG contrast)."""
    lum = _luminance(QColor(fill))
    dark = (lum + 0.05) / (_luminance(QColor(INK)) + 0.05)
    light = 1.05 / (lum + 0.05)
    return INK if dark >= light else "#ffffff"


class Glyph:
    """Segoe MDL2 Assets code points."""

    REPAIR = ""
    SETTINGS = ""
    CONNECT = ""
    ROBOT = ""
    VOLUME = ""
    MUTE = ""
    MIC = ""
    PLAY = ""
    STOP = ""
    CHAT = ""
    LIST = ""
    VIEW = ""
    HIDE = ""
    BOLT = ""
    REFRESH = ""
    COPY = ""
    OPEN = ""
    CHECK = ""
    WARN = ""
    INFO = ""
    PIN = ""
    DOWNLOAD = ""
    PEOPLE = ""
    PROFILE = ""
    CHEVRON_DOWN = ""
    CHEVRON_RIGHT = ""
    PAUSE = "\ue769"
    BACK = ""


def _chevron_png() -> str:
    """QSS can't draw a combo-box arrow on its own; render one to the cache once."""
    path = CACHE_DIR / "ui" / "chevron-b.png"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pm = QPixmap(20, 20)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(TEXT_2), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        path_ = QPainterPath(QPointF(6, 8))
        path_.lineTo(10, 12)
        path_.lineTo(14, 8)
        p.drawPath(path_)
        p.end()
        pm.save(str(path))
    return path.as_posix()


def stylesheet() -> str:
    from .brand import register_fonts

    chevron = _chevron_png()
    register_fonts()
    d, r = DISPLAY, RADIUS
    brand = lambda a: rgba(BRAND, a)  # noqa: E731
    return f"""
* {{ font-family: "{FONT}"; color: {TEXT}; font-size: 10.5pt; }}
QMainWindow, #root {{ background: {BG}; }}
QWidget#page {{ background: {BG}; }}
#header {{ background: {HEADER}; border-bottom: 1px solid {BORDER}; }}

QPushButton#navtab {{
    background: transparent; border: none; padding: 0 16px; font-family: "{d}"; font-size: 13.5pt;
    font-weight: 700; color: {MUTED};
}}
QPushButton#navtab:hover {{ color: {TEXT}; }}
QPushButton#navtab:checked {{ color: {TEXT}; }}
#navIndicator {{ background: {BRAND}; border: none; }}
QPushButton#tab {{
    background: transparent; border: 1px solid {BORDER_STRONG}; border-radius: {r}px; padding: 4px 14px;
    color: {TEXT_2}; font-family: "{d}"; font-weight: 700; font-size: 11pt;
}}
QPushButton#tab:hover {{ border-color: {brand(0.55)}; color: {TEXT}; }}
QPushButton#tab:checked {{ background: {brand(0.14)}; border-color: {BRAND}; color: {BRAND_BRIGHT}; }}

#card, #heroArt {{ background: transparent; border: none; }}
#checkrow {{ border: 1px solid {BORDER}; border-radius: {r}px; background: {SURFACE}; }}
#feeditem {{ background: transparent; border: none; border-bottom: 1px solid {BORDER}; }}
#savebar {{ background: {SURFACE_2}; border: 1px solid {brand(0.45)}; border-radius: {r}px; }}
#toast {{
    background: {HEADER}; border: 1px solid {brand(0.4)}; border-left: 3px solid {BRAND}; border-radius: 0;
    padding: 10px 18px;
}}
#pill {{ border-radius: {r}px; padding: 2px 8px; font-size: 8pt; font-weight: 700; }}

QLabel#h1 {{ font-family: "{d}"; font-size: 24pt; font-weight: 800; color: {TEXT}; }}
QLabel#h2 {{ font-size: 12pt; font-weight: 700; }}
QLabel#heroTitle {{ font-family: "{d}"; font-size: 27pt; font-weight: 800; }}
QLabel#display {{ font-family: "{d}"; font-weight: 800; }}
QLabel#muted {{ color: {TEXT_2}; }}
QLabel#faint {{ color: {MUTED}; }}
QLabel#eyebrow, QLabel#gold {{ font-family: "{d}"; color: {TEXT_2}; font-size: 10pt; font-weight: 700; }}
QLabel#section {{ font-family: "{d}"; color: {TEXT_2}; font-size: 11pt; font-weight: 700; }}
QLabel#tileValue {{ font-family: "{d}"; font-size: 17pt; font-weight: 800; }}
QLabel#num {{ font-family: "{d}"; font-weight: 800; }}
QLabel#mono {{ font-family: {MONO}; color: {TEXT_2}; font-size: 9pt; }}

QPushButton {{ font-size: 10pt; }}
QPushButton#primary, QPushButton#accent, QPushButton#start {{ background: transparent; border: none; }}
QPushButton#stop {{
    border: 1px solid {RED}; border-radius: {r}px; background: {rgba(RED, 0.08)};
}}
QPushButton#stop:hover {{ background: {rgba(RED, 0.18)}; }}
QPushButton#danger {{ border: 1px solid {brand(0.45)}; border-radius: {r}px; background: {brand(0.07)}; }}
QPushButton#danger:hover {{ background: {brand(0.15)}; }}
QPushButton#ghost, QPushButton#icon {{
    background: #10151c; border: 1px solid {BORDER_STRONG}; border-radius: {r}px;
    font-family: "{d}"; font-weight: 700; font-size: 11pt; padding: 8px 14px;
}}
QPushButton#icon {{ padding: 0; }}
QPushButton#ghost:hover, QPushButton#icon:hover {{ background: {SURFACE_HOVER}; border-color: {brand(0.6)}; }}
QPushButton#ghost:pressed, QPushButton#icon:pressed {{ background: {SURFACE}; }}
QPushButton#ghost:disabled, QPushButton#icon:disabled {{ background: {SURFACE}; }}
QPushButton#link {{ background: transparent; border: none; color: {BRAND}; font-weight: 600; padding: 4px 2px; }}
QPushButton#link:hover {{ color: {BRAND_BRIGHT}; }}
QPushButton#ghost:focus, QPushButton#icon:focus, QPushButton#tab:focus, QPushButton#danger:focus,
QPushButton#stop:focus {{ border: 2px solid {BRAND}; }}
QPushButton#link:focus {{ text-decoration: underline; color: {BRAND_BRIGHT}; }}
QPushButton#navtab:focus {{ color: {BRAND_BRIGHT}; text-decoration: underline; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #243040; border-radius: 2px; min-height: 36px; }}
QScrollBar::handle:vertical:hover {{ background: #33445a; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #243040; border-radius: 2px; min-width: 36px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QComboBox, QLineEdit {{
    background: {SURFACE_2}; border: 1px solid {BORDER_STRONG}; border-radius: {r}px; padding: 7px 12px;
    selection-background-color: {brand(0.35)};
}}
QComboBox {{ min-width: 160px; }}
QComboBox:hover, QLineEdit:hover {{ border-color: {brand(0.5)}; }}
QComboBox:focus, QLineEdit:focus {{ border-color: {BRAND}; }}
QComboBox:disabled {{ background: transparent; color: #5d6773; border-color: {BORDER}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 20px; height: 20px; }}
QComboBox QAbstractItemView {{
    background: {HEADER}; border: 1px solid {brand(0.45)}; outline: 0; padding: 4px;
    selection-background-color: {brand(0.2)};
}}
QSlider::groove:horizontal {{ height: 4px; background: #243040; border-radius: 0; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1d7f9e, stop:1 {BRAND}); border-radius: 0;
}}
QSlider::handle:horizontal {{
    background: {BRAND_BRIGHT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; border: 1px solid #1a6a82;
}}
QToolTip {{ background: {HEADER}; color: {TEXT}; border: 1px solid {brand(0.45)}; padding: 6px 8px; }}
QListView#completer, QCompleter QAbstractItemView {{
    background: {HEADER}; color: {TEXT}; border: 1px solid {brand(0.45)};
    selection-background-color: {brand(0.2)}; selection-color: {TEXT}; outline: 0; padding: 4px;
}}
QListView#completer::item {{ padding: 6px 10px; border-radius: {r}px; }}
QMenu {{ background: {HEADER}; border: 1px solid {brand(0.4)}; padding: 6px; }}
QMenu::item {{ padding: 7px 30px 7px 18px; border-radius: {r}px; }}
QMenu::item:selected {{ background: {brand(0.18)}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QTableWidget, QTableView {{
    background: {SURFACE}; border: 1px solid {BORDER}; gridline-color: {BORDER};
    selection-background-color: {brand(0.16)};
}}
QHeaderView::section {{
    background: {SURFACE_2}; color: {TEXT_2}; border: none; border-bottom: 1px solid {BORDER_STRONG};
    padding: 6px 8px; font-family: "{d}"; font-size: 9.5pt; font-weight: 700;
}}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")  # predictable base for the stylesheet on every Windows version
    app.setFont(QFont(FONT, 10.5))
    app.setStyleSheet(stylesheet())


def dark_title_bar(widget) -> None:
    """Ask Windows 10 (20H1+) / 11 to draw a dark title bar for this window."""
    if sys.platform != "win32":
        return
    import ctypes

    hwnd = int(widget.winId())
    on = ctypes.c_int(1)
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on pre-20H1 builds)
        if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
            break
