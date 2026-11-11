"""Choosing Doty's language: what a new install shows first, and what the flag in the top bar opens.

Six languages up front (languages.FEATURED), each by its flag and its own name, with the other ten under
"More languages". The words on the dialog follow the language you point at, so it can be read by someone
who doesn't read English, and a line under the choices says which of Doty's voices speak it.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QDialog, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from .. import i18n, languages
from ..config import Config, is_user_config, save_config
from . import theme
from .flags import flag_pixmap
from .widgets import button, label, set_button

PROMPT_ENV = "DOTY_LANGUAGE_PROMPT"  # "0": never offer the choice on opening (tests, scripted runs)
TITLE = "Choose your language"
HINT = "You can change it any time with the flag in the top bar."
MORE = "More languages"
CONTINUE = "Continue"
VOICE_FREE = "Doty speaks it with Kokoro, the free voice on this PC."
VOICE_SYSTEM = "Doty's free voice doesn't speak it yet. Your PC's own voice does; ElevenLabs (paid) sounds more natural."
VOICE_SYSTEM_ONLY = "Doty's free voice doesn't speak it yet. Your PC's own voice does."
VOICE_NEEDS = ("Doty's free voice doesn't speak it yet. To hear it, add this language's speech in your system's "
               "settings, or an ElevenLabs key (paid). Until then the advice is on screen only.")
VOICE_NEEDS_SYSTEM = ("Doty's free voice doesn't speak it yet. To hear it, add this language's speech in your "
                      "system's settings. Until then the advice is on screen only.")


def voice_line(code: str, system: set[str]) -> str:
    """Which voices speak a language, as one of the lines above (in English: the caller translates it)."""
    if languages.kokoro_speaks(code):
        return VOICE_FREE
    eleven = languages.eleven_speaks(code)
    if code in system:
        return VOICE_SYSTEM if eleven else VOICE_SYSTEM_ONLY
    return VOICE_NEEDS if eleven else VOICE_NEEDS_SYSTEM


class LanguageTile(QAbstractButton):
    """One language: its flag and its own name. The six featured ones are tall, the rest one line."""

    picked = Signal()  # double-clicked: choose it and go on

    def __init__(self, code: str, big: bool) -> None:
        super().__init__()
        self.code, self.big = code, big
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(languages.own_name(code))
        self.setFixedSize(QSize(190, 98) if big else QSize(190, 42))

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.setChecked(True)
        self.picked.emit()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on, hover = self.isChecked(), self.underMouse()
        fill = QColor(theme.BRAND)
        fill.setAlphaF(0.14 if on else 0.0)
        p.setBrush(fill if on else QColor(theme.SURFACE_HOVER if hover else theme.SURFACE))
        edge = theme.BRAND if on else theme.BRAND_DIM if hover or self.hasFocus() else theme.BORDER_STRONG
        p.setPen(QPen(QColor(edge), 2 if on else 1))
        p.drawRoundedRect(r, 3, 3)
        f = QFont(theme.FONT, 12 if self.big else 10.5)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT if on or hover else theme.NEUTRAL))
        name = languages.own_name(self.code)
        with i18n.as_is():  # (a language's own name is never translated)
            if self.big:
                flag = flag_pixmap(self.code, 38)
                p.drawPixmap(round((self.width() - 57) / 2), 15, flag)
                p.drawText(QRectF(4, 60, self.width() - 8, 28), Qt.AlignHCenter | Qt.AlignVCenter, name)
            else:
                p.drawPixmap(12, round((self.height() - 18) / 2), flag_pixmap(self.code, 18))
                p.drawText(QRectF(50, 0, self.width() - 56, self.height()), Qt.AlignLeft | Qt.AlignVCenter, name)
        p.end()


class LanguageDialog(QDialog):
    """`chosen` is the language picked with Continue, or None if the dialog was closed."""

    def __init__(self, current: str = "en", parent: QWidget | None = None, system: set[str] | None = None) -> None:
        super().__init__(parent)
        self.chosen: str | None = None
        self.system = system if system is not None else _system_languages()
        self.setWindowTitle("Doty")
        self.setModal(True)
        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(0)
        self.title = label("", "title")
        self.title.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-weight: 700; font-size: 19pt;'
                                 f" color: {theme.TEXT};")
        lay.addWidget(self.title)
        lay.addSpacing(14)

        current = languages.known(current)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tiles: dict[str, LanguageTile] = {}
        top = QGridLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        for i, code in enumerate(languages.FEATURED):
            top.addWidget(self._tile(code, True), i // 3, i % 3)
        lay.addLayout(top)
        lay.addSpacing(10)

        self.more_btn = button(MORE, "link")
        self.more_btn.clicked.connect(lambda _checked=False: self._show_more(not self.rest.isVisible()))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.more_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.rest = QWidget()
        grid = QGridLayout(self.rest)
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setSpacing(10)
        others = [c for c in languages.ordered() if c not in languages.FEATURED]
        for i, code in enumerate(others):
            grid.addWidget(self._tile(code, False), i // 3, i % 3)
        lay.addWidget(self.rest)
        self.rest.setVisible(current not in languages.FEATURED)
        lay.addSpacing(14)

        self.voice = label("", "muted", wrap=True)
        self.voice.setMinimumHeight(40)
        self.voice.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lay.addWidget(self.voice)
        lay.addSpacing(12)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        self.hint = label("", "muted", wrap=True)
        foot.addWidget(self.hint, 1)
        foot.addSpacing(16)
        # (With a glyph the words sit in a label of their own: sized to each language, and shown as set.)
        self.ok = button(CONTINUE, "accent", theme.Glyph.CHECK)
        self.ok.setDefault(True)
        self.ok.clicked.connect(lambda _checked=False: self._accept())
        foot.addWidget(self.ok, 0, Qt.AlignBottom)
        lay.addLayout(foot)

        self.group.idToggled.connect(lambda _id, on: on and self._retext())
        self.tiles[current].setChecked(True)
        self._retext()
        self.setFixedWidth(self.sizeHint().width())
        theme.dark_title_bar(self)

    def _tile(self, code: str, big: bool) -> LanguageTile:
        tile = LanguageTile(code, big)
        self.tiles[code] = tile
        self.group.addButton(tile)
        tile.picked.connect(self._accept)
        return tile

    def selected(self) -> str:
        b = self.group.checkedButton()
        return b.code if isinstance(b, LanguageTile) else "en"

    def _show_more(self, on: bool) -> None:
        self.rest.setVisible(on)
        self.adjustSize()

    def _retext(self) -> None:
        """The dialog's own words, in the language that's selected."""
        code = self.selected()
        with i18n.as_is():
            self.title.setText(i18n.text_in(code, TITLE))
            self.hint.setText(i18n.text_in(code, HINT))
            self.voice.setText(i18n.text_in(code, voice_line(code, self.system)))
            self.more_btn.setText(i18n.text_in(code, MORE))
            set_button(self.ok, i18n.text_in(code, CONTINUE))

    def _accept(self) -> None:
        self.chosen = self.selected()
        self.accept()


def _system_languages() -> set[str]:
    try:
        from ..voice import system_languages

        return system_languages()
    except Exception:
        return set()


def system_guess() -> str:
    """The language this PC is set to, if it's one of Doty's (else English): the one pre-selected."""
    from PySide6.QtCore import QLocale

    loc = QLocale.system()
    for name in [*loc.uiLanguages(), loc.name()]:
        code = languages.from_locale(name)
        if code != "en" or name.lower().startswith("en"):
            return code
    return "en"


def first_run_wanted(cfg: Config) -> bool:
    """Offer the choice when Doty opens: once, on your real settings, while no language has been chosen."""
    a = cfg.app
    if getattr(cfg, "load_problem", ""):
        return False  # (the settings file couldn't be read: don't take its defaults for a new install, or save over it)
    return (is_user_config(cfg) and not getattr(a, "language_picked", False) and getattr(a, "language", "en") == "en"
            and os.environ.get(PROMPT_ENV, "1") != "0")


def ask_at_first_run(cfg: Config) -> None:
    """Show the choice and keep it (closing the dialog keeps English, and doesn't ask again)."""
    dlg = LanguageDialog(system_guess())
    dlg.exec()
    if dlg.chosen:
        cfg.app.language = dlg.chosen
    cfg.app.language_picked = True
    try:
        save_config(cfg)
    except OSError:
        pass
