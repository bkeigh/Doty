"""The app in the player's language (AppConfig.language).

The catalogs are doty/locales/<code>.json: {English text: translation}, from the English list that
tools/extract_strings.py collects (en.json). `tr()` looks a text up, and `install_qt()` makes Qt's text setters
(labels, buttons, tooltips, placeholders, menu and list items, painted text) translate on their own, so the
screens are translated without every line of UI code calling tr(). Text built with numbers in it (f-strings)
stays English unless its call site passes a template: tr("{name} in {secs}", name=..., secs=...).

What isn't in a catalog is shown as it is (English), never blank.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "locales"
_lang = "en"
_cat: dict[str, str] = {}
_folded: dict[str, str] = {}  # lowercase English -> translation (titles shown in capitals)
_installed = False
_as_is = threading.local()  # .n > 0 inside `as_is()` on this thread: text is shown as given


class as_is:  # noqa: N801
    """`with as_is():` text set on a widget (or painted) is shown as it is, not looked up: a language's own
    name, or words already in a language other than the app's (the language choice). Per thread: the rules
    and the voice translate their lines on their own threads meanwhile."""

    def __enter__(self) -> None:
        _as_is.n = getattr(_as_is, "n", 0) + 1

    def __exit__(self, *exc) -> None:
        _as_is.n = getattr(_as_is, "n", 1) - 1


def language() -> str:
    return _lang


def set_language(code: str) -> None:
    """Load a language's catalog ("en" or an unknown code: English)."""
    global _lang, _cat, _folded
    code = (code or "en").strip().lower()
    cat: dict[str, str] = {}
    if code != "en":
        try:
            raw = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
            cat = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
        except (OSError, ValueError):
            cat = {}
    _lang = code if cat or code == "en" else "en"
    _cat = cat
    _folded = {k.lower(): v for k, v in cat.items()}


_others: dict[str, dict[str, str]] = {}


def text_in(code: str, text: str, /, **fields) -> str:
    """`text` in another language than the one the app is in (the first-run language choice shows each
    language's own words; a voice sample is said in the language being tried). English if it has no catalog
    or no such text."""
    code = (code or "en").strip().lower()
    out = text
    if code != "en":
        if code not in _others:
            try:
                raw = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
                _others[code] = {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}
            except (OSError, ValueError):
                _others[code] = {}
        out = _others[code].get(text, text)
    if fields:
        try:
            return out.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text.format(**fields)
    return out


def available() -> list[str]:
    """Languages with a catalog (and English)."""
    return ["en"] + sorted(p.stem for p in LOCALES.glob("*.json") if p.stem != "en")


_SPACE = re.compile(r"^(\s*)(.*?)(\s*)$", re.DOTALL)


def tr(text, /, **fields):  # (positional-only: a template may have a {text} field)
    """`text` in the player's language. With fields, `text` is a template: tr("{name} in {secs}", name=..., secs=5)."""
    if not isinstance(text, str) or not text or _lang == "en" or not _cat or getattr(_as_is, "n", 0):
        return text.format(**fields) if fields and isinstance(text, str) else text
    out = _cat.get(text)
    if out is None:
        lead, core, trail = _SPACE.match(text).groups()
        out = _cat.get(core)
        if out is None and core.isupper():  # a title shown in capitals ("YOUR PLAN" from "Your plan")
            found = _folded.get(core.lower())
            out = found.upper() if found is not None else None
        if out is not None:
            out = lead + out + trail
    if out is None:
        out = text
    if fields:
        try:
            return out.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text.format(**fields)
    return out


def install_qt() -> None:
    """Translate text as Qt receives it: labels, buttons, tooltips, placeholders, list and menu items, window
    titles and text painted by Doty's own widgets. Once, before the window is built."""
    global _installed
    if _installed or _lang == "en":
        return
    _installed = True
    from PySide6.QtGui import QAction, QPainter
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QLineEdit, QMenu, QPushButton, QWidget

    def args(a: tuple) -> tuple:
        """The call's text (its first string) translated; any later string is data (a combo item's value)."""
        out, done = [], False
        for x in a:
            if not done and isinstance(x, str):
                x, done = tr(x), True
            out.append(x)
        return tuple(out)

    def wrap_init(cls) -> None:
        orig = cls.__init__

        def init(self, *a, **k):
            orig(self, *args(a), **k)
        cls.__init__ = init

    def wrap_setter(cls, name: str) -> None:
        orig = getattr(cls, name)

        def setter(self, *a, **k):
            return orig(self, *args(a), **k)
        setattr(cls, name, setter)

    for cls in (QLabel, QPushButton, QAction):
        wrap_init(cls)
    wrap_setter(QLabel, "setText")
    wrap_setter(QAbstractButton, "setText")
    wrap_setter(QAction, "setText")
    wrap_setter(QWidget, "setToolTip")
    wrap_setter(QWidget, "setWindowTitle")
    wrap_setter(QLineEdit, "setPlaceholderText")
    wrap_setter(QComboBox, "addItem")
    wrap_setter(QComboBox, "setItemText")
    wrap_setter(QMenu, "addAction")
    wrap_setter(QMenu, "setTitle")
    wrap_setter(QPainter, "drawText")
    orig_items = QComboBox.addItems
    QComboBox.addItems = lambda self, items: orig_items(self, [tr(x) for x in items])
