"""The Setup and Settings pages, plus small helpers the Coach page shares."""

from __future__ import annotations

import copy
import re
import sys
import threading

from PySide6.QtCore import (QCoreApplication, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRectF, QSize,
                            Qt, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence,
                           QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractButton, QBoxLayout, QButtonGroup, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                               QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from .. import i18n
from ..i18n import tr
from ..checks import Check, run_checks
from ..config import is_user_config
from ..knowledge.lessons import LEVELS
from ..knowledge.stats import BRACKETS
from . import theme
from .controller import Controller, save_api_key
from .live import Bar
from .motion import Collapsible, fade_in
from .theme import AMBER, BLUE, BRAND, GOLD, GREEN, GREY, MUTED, RED, TEXT, Glyph, rgba
from .widgets import (Card, Switch, Tag, Toast, button, caps_font, fit_width, icon_label, label, set_button,
                      set_icon_color)

PTT_KEYS = [
    ("mouse5", "Mouse 5 (forward side button)"),
    ("mouse4", "Mouse 4 (back side button)"),
    ("mouse3", "Middle mouse button"),
    ("f8", "F8"),
    ("f9", "F9"),
    ("ctrl+alt+t", "Ctrl+Alt+T"),
]
LOOK_EVERY = [
    (10, "Every 10 s"),
    (15, "Every 15 s"),
    (20, "Every 20 s"),
    (30, "Every 30 s"),
    (45, "Every 45 s"),
    (60, "Every minute"),
    (120, "Every 2 minutes"),
    (0, "Only when I ask"),
]
SPEAK_UP = [
    ("important", "When it matters"),
    ("every", "Every look"),
    ("never", "Never (feed only)"),
]
# "When I go my own way" (CoachConfig.own_way; rules/own_way.py).
OWN_WAY = [
    ("once", "Say it once, with a reason"),
    ("follow", "Follow me silently"),
    ("remind", "Keep reminding me"),
]
# The subjects of "What Doty suggests", by their rows' names (for what Doty took from your instructions).
TOPIC_TITLES = {
    "items": "Items to buy", "skills": "Skill points", "farm": "Farm and lane", "timers": "Runes and Roshan",
    "enemy": "Enemy timings", "gold": "Gold and TP scroll", "deaths": "Death reviews",
}
# One choice for how chatty the coach is: (key, name, looks every N s, talks, what it means).
COACH_PRESETS = [
    ("quiet", "Quiet", 60, "important", "Looks every minute. Speaks when it matters."),
    ("balanced", "Balanced", 30, "important", "Looks every 30 s. Speaks when it matters."),
    ("chatty", "Chatty", 15, "every", "Looks every 15 s and says what it sees."),
    ("ask", "Only when I ask", 0, "important", "Never looks on its own; answers when you ask."),
]


def coach_preset(look_every_s: int, speak_up: str) -> tuple | None:
    """The preset matching these settings, or None (custom)."""
    for p in COACH_PRESETS:
        if p[2] == look_every_s and (p[3] == speak_up or look_every_s == 0):
            return p
    return None


# A typical look, in tokens: new input (screenshot, state), input read from the prompt cache (instructions, match
# brief), and the answer. Measured on real matches' usage lines (2026-11).
TYPICAL_LOOK = {"in": 1500, "cached": 5500, "out": 220}
GAME_S = 40 * 60
CALM_EXTRA = 1.3  # calm-moment looks (respawn, level 6, after a fight) on top of the rhythm
OTHER_LOOKS = 8  # the opening plan, death looks, a few asks: on the main model


def preset_cost(look_every_s: int, model: str, provider: str = "claude", watch_model: str = "claude-haiku-5-5") -> str:
    """Roughly what a 40-minute game costs at this rhythm, from Anthropic's prices: live looks on the fast model
    (`watch_model`), the rest on the main one ("" when we can't say)."""
    from ..coach.backends import cost_usd

    if provider != "claude":
        return "" if look_every_s > 0 else tr("only what you ask")
    main = cost_usd(model or "", TYPICAL_LOOK)
    live = cost_usd(watch_model or model or "", TYPICAL_LOOK)
    if main is None or live is None:
        return ""
    looks = GAME_S / look_every_s * CALM_EXTRA if look_every_s > 0 else 0
    return tr("{cost} a game", cost=_money(looks * live + OTHER_LOOKS * main))


def _money(usd: float) -> str:
    """About how much, in words: "about 8¢", "about $1.20"."""
    if usd < 0.01:
        return tr("under 1¢")
    if usd < 1:
        return tr("about {n}¢", n=round(usd * 100))
    return tr("about ${n}", n=f"{usd:.2f}".replace(".00", ""))


def save_claude_key(ctl, toast, key: str) -> bool:
    """Save an Anthropic key for this Windows user and restart the coach with it."""
    key = key.strip()
    if not key.startswith("sk-ant-"):
        toast.show_message("That doesn't look like an Anthropic API key (they start with sk-ant-).")
        return False
    save_api_key(key)
    toast.show_message("Key saved. Restarting the coach…")
    ctl.restart()
    return True


STEAM_DOTA = "steam://nav/games/details/570"
# The line Play sample says (eleven.SAMPLE_LINE; written out here so it's in every language's catalog).
VOICE_SAMPLE = "Bounty runes in fifteen seconds. Their carry is missing from the map, so play safe."
KEYS_URL = "https://console.anthropic.com/settings/keys"
VOICE_ENGINES = [
    ("auto", "Best available"),
    ("kokoro", "Kokoro (free, natural, runs on this PC)"),
    ("kitten", "Kitten (free, softer, runs on this PC)"),
    ("elevenlabs", "ElevenLabs (natural, paid, needs a key)"),
    # (Linux: eSpeak NG or speech-dispatcher, voice.SystemSpeaker)
    ("windows", "Windows voice (free, robotic)" if sys.platform == "win32" else "System voice (free, robotic)"),
]


def windows_voice_name(desc: str) -> str:
    return desc.split(" - ")[0].replace("Microsoft ", "")


def pretty_key(spec: str) -> str:
    named = {"mouse5": "Mouse 5", "mouse4": "Mouse 4", "mouse3": "middle mouse", "middle": "middle mouse"}
    return named.get(spec.lower(), "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in spec.split("+")))


def pretty_model(model: str) -> str:
    m = re.match(r"claude-(\w+)-(\d+)-(\d+)", model)
    return f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)}" if m else model


def short_mic(name: str) -> str:
    m = re.match(r"^[\w ]+\((.+?)\)?$", name)
    return m.group(1) if m else name


def header(title: str, subtitle: str) -> QVBoxLayout:
    box = QVBoxLayout()
    box.setSpacing(2)
    box.addWidget(label(title, "h1"))
    box.addWidget(label(subtitle, "muted", wrap=True))
    return box


# --------------------------------------------------------------------------- Setup


STATE = {True: ("ok", GREEN, Glyph.CHECK), False: ("bad", RED, Glyph.WARN), None: ("warn", AMBER, "")}


class CheckRow(QWidget):
    """One setup step. Done steps shrink to a single line so what's left stands out."""

    def __init__(self, check: Check, detail: str | None = None, compact: bool | None = None) -> None:
        super().__init__()
        self.setObjectName("checkrow")
        self.setAttribute(Qt.WA_StyledBackground)
        compact = check.ok is True if compact is None else compact
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*((16, 8, 16, 8) if compact else (18, 14, 18, 14)))
        lay.setSpacing(12 if compact else 14)
        _, color, glyph = STATE[check.ok]
        size = 22 if compact else 30
        badge = icon_label(glyph, 10 if compact else 13, color)
        badge.setFixedSize(size, size)
        ring = f" border: 2px solid {color};" if check.ok is None else ""
        set_icon_color(badge, color, f"background: rgba(255,255,255,0.05); border-radius: {size // 2}px;{ring}")
        lay.addWidget(badge, 0, Qt.AlignVCenter if compact else Qt.AlignTop)
        texts = QHBoxLayout() if compact else QVBoxLayout()
        texts.setSpacing(10 if compact else 2)
        title = label(check.label)
        title.setStyleSheet(f"font-size: {'10pt' if compact else '10.5pt'}; font-weight: 600;")
        self.title_row = QHBoxLayout()
        self.title_row.setSpacing(8)
        self.title_row.addWidget(title)
        if not compact:
            self.title_row.addStretch(1)
        texts.addLayout(self.title_row)
        self.detail = label(detail or check.detail, "muted", wrap=not compact)
        self.detail.setStyleSheet("font-size: 9pt;")
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        texts.addWidget(self.detail, 1)
        outer = QVBoxLayout()
        outer.setSpacing(0)
        outer.addLayout(texts)
        self.extra = QVBoxLayout()
        self.extra.setContentsMargins(0, 6, 0, 0)
        outer.addLayout(self.extra)
        lay.addLayout(outer, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        lay.addLayout(self.actions)

    def set_tag(self, text: str) -> None:
        if not text:
            return
        color = AMBER if text == "Required" else MUTED
        c = QColor(color)
        pill = QLabel(text.upper())
        pill.setStyleSheet(f"color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},0.13);"
                           " border-radius: 4px; padding: 1px 6px; font-size: 7.5pt; font-weight: 800;")
        self.title_row.insertWidget(1, pill, 0, Qt.AlignVCenter)

    def add_action(self, text: str, fn, glyph: str | None = None, kind: str = "ghost"):
        b = button(text, kind, glyph)
        b.clicked.connect(lambda _checked=False: fn())
        self.actions.addWidget(b, 0, Qt.AlignVCenter)
        return b


class SetupPage(QWidget):
    checked = Signal(object, object)  # (checks, shortcut paths), from the worker thread
    summary = Signal(int)  # number of things that need attention
    progress = Signal(int, int)  # (required steps done, required steps)
    next_step = Signal(str, str)  # (check key, its name) of the first required step left, or ("", "")
    checks_ready = Signal(object)  # {check key: Check}, for the Coach page's Get started steps
    eleven_checked = Signal(str, bool)  # (message, key usable), from the key-check thread
    open_settings = Signal()

    def __init__(self, ctl: Controller, toast: Toast) -> None:
        super().__init__()
        self.ctl, self.toast = ctl, toast
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(34, 28, 34, 24)
        outer.setSpacing(16)
        top = QHBoxLayout()
        top.addLayout(header("Setup", "One-time steps so Dota 2 talks to Doty. Doty fixes what it can by itself."), 1)
        self.recheck_btn = button("Check again", "ghost", Glyph.REFRESH)
        self.recheck_btn.clicked.connect(lambda: self.recheck())
        top.addWidget(self.recheck_btn, 0, Qt.AlignTop)
        outer.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.rows = QVBoxLayout(inner)
        self.rows.setContentsMargins(0, 0, 10, 10)
        self.rows.setSpacing(10)
        self.rows.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        self.checked.connect(self._show)
        self.eleven_checked.connect(self._eleven_saved)
        ctl.kokoro_progress.connect(self._kokoro_progress)
        ctl.kokoro_done.connect(self._kokoro_done)
        self._kokoro_btn = None
        self._busy = False
        self._auto_fixed = False
        self._optional_open = False
        self._passed_open = False
        self._outer = outer

    def recheck(self) -> None:
        if self._busy:
            return
        self._busy = True
        set_button(self.recheck_btn, "Checking…", Glyph.REFRESH)
        self.recheck_btn.setEnabled(False)
        cfg, running = copy.deepcopy(self.ctl.cfg), self.ctl.running
        seen = running and self.ctl.doty.server.received > 0 and not self.ctl.demo_running

        def work() -> None:
            try:
                checks = run_checks(cfg, doty_running=running, gsi_seen=seen)
            except Exception as exc:
                checks = [Check("error", "Couldn't run the checks", False, str(exc))]
            try:
                from ..shortcuts import installed_shortcuts

                links = [str(p) for p in installed_shortcuts()]
            except Exception:
                links = []
            self.checked.emit(checks, links)

        threading.Thread(target=work, name="doty-checks", daemon=True).start()

    def _clear(self) -> None:
        while self.rows.count() > 1:
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _section(self, text: str) -> None:
        lab = label(text.upper(), "section")
        lab.setContentsMargins(4, 10, 0, 2)
        self.rows.insertWidget(self.rows.count() - 1, lab)

    def _add(self, row: QWidget) -> None:
        self.rows.insertWidget(self.rows.count() - 1, row)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        fit_width(self._outer, self.width(), 1100)

    def _show(self, checks: list[Check], links: list[str]) -> None:
        self._busy = False
        set_button(self.recheck_btn, "Check again", Glyph.REFRESH)
        self.recheck_btn.setEnabled(True)
        by_key = {c.key: c for c in checks}
        gsi = by_key.get("gsi")
        if gsi is not None and gsi.ok is None and not self._auto_fixed and is_user_config(self.ctl.cfg):
            self._auto_fixed = True  # out of date with your settings: just rewrite it
            if self._install_gsi(quiet=True):
                return
        self._clear()
        attention = sum(1 for c in checks if c.ok is not True and c.key in ("dota", "gsi", "launch"))  # required

        required = [by_key[k] for k in ("dota", "gsi", "launch") if k in by_key]
        done = sum(c.ok is True for c in required)
        self.checks_ready.emit(by_key)
        self.progress.emit(done, len(required))
        ready = done == len(required)
        nxt = next((c for c in required if c.ok is not True), None)
        self.next_step.emit(nxt.key if nxt else "", nxt.label if nxt else "")
        left = len(required) - done
        head = Card(margins=(20, 14, 20, 16), spacing=6)
        title = label("You're ready to play." if ready else
                      tr("1 required step left: {step}", step=tr(nxt.label)) if left == 1 else
                      tr("{n} required steps left: {step}", n=left, step=tr(nxt.label)), "h2")
        title.setStyleSheet(f"color: {GREEN if ready else AMBER};")
        head.body.addWidget(title)
        head.body.addWidget(label("Launch Dota 2 and Doty connects by itself." if ready else
                                  "Do it below, then restart Dota.", "muted", wrap=True))
        bar = Bar(GREEN if ready else AMBER, 6)
        bar.set(done / max(1, len(required)), "")
        head.body.addWidget(bar)
        self._add(head)

        first_open = next((c.key for c in required if c.ok is not True), None)
        todo, passed = [], []
        # ("desktop" is Linux only: whether X11 is there for the screen, the overlay and the hotkeys)
        for key in ("dota", "gsi", "launch", "key", "display", "desktop", "port", "mic", "voices", "gpu", "error"):
            c = by_key.get(key)
            if c is None:
                continue
            if key == "key":
                row = (self._provider_row(c) if self.ctl.cfg.coach.provider != "claude" else self._key_row(c))
            else:
                row = self._row(c, primary=key == first_open)
            tag = "Required" if key in ("dota", "gsi", "launch") else \
                "Recommended" if key in ("key", "display", "desktop") else "Optional"
            (passed if c.ok is True else todo).append((row, tag))
        if todo:
            self._section("To do")
            for row, tag in todo:
                row.set_tag(tag)
                self._add(row)
        if passed:
            fold = Collapsible(tr("Done ({n})", n=len(passed)), "", expanded=self._passed_open or not todo)
            fold.toggled.connect(lambda on: setattr(self, "_passed_open", on))
            fold.body_lay.setSpacing(8)
            for row, _tag in passed:
                fold.body_lay.addWidget(row)
            fold.setContentsMargins(4, 8, 0, 0)
            self._add(fold)
        kokoro, eleven = by_key.get("kokoro"), by_key.get("eleven")
        if kokoro is not None or eleven is not None:
            from .. import kokoro as kokoro_voices
            from .. import languages

            lang = languages.known(self.ctl.cfg.app.language)
            eleven_ok = (eleven is not None and eleven.ok is True
                         and languages.eleven_speaks(lang, self.ctl.cfg.voice.eleven_model))
            if kokoro is not None and kokoro.ok is True and kokoro_voices.speaks(lang):
                summary = "Kokoro is on this PC: Doty speaks with a natural voice."
            elif eleven_ok:
                summary = "ElevenLabs is set up: Doty speaks with a natural voice."
            elif not kokoro_voices.speaks(lang):
                summary = tr("Doty's free voices don't speak {language} yet: your PC's own voice or ElevenLabs "
                             "says it. Voice settings shows which voices speak which language.",
                             language=languages.own_name(lang))
            else:
                summary = "Doty uses the Windows voice. Kokoro is free and sounds like a person."
            fold = Collapsible("Optional: a more natural voice", summary,
                               expanded=self._optional_open or self.ctl.kokoro_pct is not None)
            fold.toggled.connect(lambda on: setattr(self, "_optional_open", on))
            if kokoro is not None:
                fold.body_lay.addWidget(self._kokoro_row(kokoro))
            if eleven is not None:
                fold.body_lay.addWidget(self._eleven_row(eleven))
            fold.setContentsMargins(4, 8, 0, 0)
            self._add(fold)
        self.summary.emit(attention)

    def _row(self, c: Check, primary: bool = False) -> CheckRow:
        kind = "accent" if primary else "ghost"
        if c.key == "gsi":
            row = CheckRow(c, "Tells Dota 2 to send live game data to Doty on this PC." if not c.ok else
                           "Installed. Dota sends its game data to Doty.")
            if not c.ok:
                row.add_action("Install" if "not installed" in c.detail else "Update", self._install_gsi,
                               Glyph.REPAIR, kind)
            return row
        if c.key == "launch":
            row = CheckRow(c, "Set. Dota sends Doty its game data whenever it runs." if c.ok else
                           "1) Click Copy option. 2) In Steam: right-click Dota 2 > Properties > General > Launch "
                           "Options, and paste. 3) Restart Dota. (While Steam is open this can stay amber until it "
                           "saves its settings; the Coach page turns green once data arrives.)")
            if not c.ok:
                row.add_action("Copy option", self._copy_launch_option, Glyph.COPY, kind)
                row.add_action("Open in Steam", lambda: QDesktopServices.openUrl(QUrl(STEAM_DOTA)), Glyph.OPEN)
            return row
        if c.key == "display" and not c.ok:
            return CheckRow(c, "Exclusive fullscreen hides overlays. In Dota: Settings > Video > Display Mode > "
                               "Borderless Window.")
        return CheckRow(c)

    def _secret_row(self, c: Check, ok_text: str, missing_text: str, placeholder: str, url: str,
                    on_save) -> CheckRow:
        """A check row with a password field for an API key (shown when missing, or on Change)."""
        row = CheckRow(c, ok_text if c.ok else missing_text)
        field = QLineEdit()
        field.setEchoMode(QLineEdit.Password)
        field.setPlaceholderText(placeholder)
        field.setMinimumWidth(320)
        save = button("Save key", "ghost", Glyph.CHECK)
        get = button("Get a key", "link")
        get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        line = QHBoxLayout()
        line.setSpacing(8)
        line.addWidget(field, 1)
        line.addWidget(save)
        line.addWidget(get)
        holder = QWidget()
        holder.setLayout(line)
        line.setContentsMargins(0, 0, 0, 0)
        row.extra.addWidget(holder)
        if c.ok:
            holder.hide()
            change = row.add_action("Change key", lambda: (holder.show(), change.hide(), field.setFocus()))

        def do_save() -> None:
            if on_save(field.text().strip()):
                field.clear()

        save.clicked.connect(do_save)
        field.returnPressed.connect(do_save)
        return row

    def _provider_row(self, c: Check) -> CheckRow:
        row = CheckRow(c)
        row.add_action("Open AI settings", lambda: self.open_settings.emit(), Glyph.SETTINGS)
        return row

    def _key_row(self, c: Check) -> CheckRow:
        def save(key: str) -> bool:
            if not save_claude_key(self.ctl, self.toast, key):
                return False
            self.recheck()
            return True

        return self._secret_row(
            c, "Saved on this PC. The AI coach is working.",
            "Lets the coach answer you and watch your screen: about 8¢ a game. No Claude key? Settings > "
            "AI coach takes your ChatGPT plan, Gemini or OpenRouter (Gemini and OpenRouter have free tiers), or a "
            "model on this PC.",
            "sk-ant-…", KEYS_URL, save)

    def _eleven_row(self, c: Check) -> CheckRow:
        from ..eleven import KEY_ENV, KEYS_URL as ELEVEN_KEYS_URL, ElevenError, ElevenLabs

        def save(key: str) -> bool:
            if len(key) < 20 or " " in key:
                self.toast.show_message("That doesn't look like an ElevenLabs API key. Copy it from ElevenLabs > "
                                        "Settings > API keys.")
                return False
            save_api_key(key, KEY_ENV)
            self.toast.show_message("Key saved. Checking it with ElevenLabs…")

            def check() -> None:
                try:
                    ElevenLabs(key).voices()
                    msg, ok = "ElevenLabs key works. Doty's voice is switching over.", True
                except ElevenError as exc:
                    if exc.code == "missing_permissions":
                        msg, ok = "Key saved. It can't list voices, so Doty uses a default voice.", True
                    elif exc.fatal:
                        msg, ok = f"Saved, but {exc}. Check the key on elevenlabs.io.", False
                    else:
                        msg, ok = f"Key saved; couldn't check it right now ({exc}).", True
                self.eleven_checked.emit(msg, ok)

            threading.Thread(target=check, name="doty-eleven-check", daemon=True).start()
            return True

        return self._secret_row(
            c, "Saved. Doty speaks with ElevenLabs; Kokoro (once downloaded) or the Windows voice fills in if it "
               "can't reach them. Pick the voice in Settings > Voice.",
            "Optional, paid: another human-sounding voice, from the cloud. Paste an ElevenLabs API key; it's saved "
            "to your Windows user account on this PC. ElevenLabs bills per character spoken (a game is a few "
            "thousand characters). Without it Doty uses Kokoro if it's downloaded, else the Windows voice.",
            "sk_…", ELEVEN_KEYS_URL, save)

    def _kokoro_row(self, c: Check) -> CheckRow:
        from .. import kokoro

        self._kokoro_btn = None
        lang = self.ctl.cfg.app.language
        if not kokoro.speaks(lang):  # nothing to download for your language: the check says who speaks it
            return CheckRow(c)
        if c.ok is True:
            detail = tr(c.detail)
            return CheckRow(c, detail[:1].upper() + detail[1:] + ". " + tr("Pick the voice and hear it in Settings > Voice."))
        if c.ok is False:
            return CheckRow(c)
        DOWNLOAD_MB = kokoro.missing_mb(language=lang) or kokoro.DOWNLOAD_MB  # noqa: N806 (what's still to fetch)
        row = CheckRow(c, tr("Optional: a human-sounding voice that runs on this PC. Free, no account, no limits, "
                             "works offline. One {mb} MB download; then Doty speaks with it whenever there's no "
                             "ElevenLabs key.", mb=DOWNLOAD_MB))
        self._kokoro_btn = row.add_action(tr("Download ({mb} MB)", mb=DOWNLOAD_MB), self.ctl.download_kokoro,
                                          Glyph.DOWNLOAD)
        if self.ctl.kokoro_pct is not None:
            self._kokoro_progress(self.ctl.kokoro_pct)
        return row

    def _kokoro_progress(self, pct: int) -> None:
        if self._kokoro_btn is not None:
            try:
                set_button(self._kokoro_btn, f"Downloading {pct}%", Glyph.DOWNLOAD)
                self._kokoro_btn.setEnabled(False)
            except RuntimeError:  # the row was rebuilt
                self._kokoro_btn = None

    def _kokoro_done(self, msg: str) -> None:
        if msg:
            self.toast.show_message(msg[0].upper() + msg[1:])
        elif self.ctl.kokoro_would_speak():
            self.toast.show_message("Kokoro is downloaded. Doty speaks with it now." if self.ctl.running else
                                    "Kokoro is downloaded. Doty speaks with it when coaching starts.")
        else:
            self.toast.show_message("Kokoro is downloaded. ElevenLabs stays your voice while it has a key; "
                                    "Kokoro takes over if it can't speak.")
        self.recheck()

    def _eleven_saved(self, msg: str, ok: bool) -> None:
        self.toast.show_message(msg)
        if ok and self.ctl.running:
            self.ctl.restart()
        self.recheck()

    def _install_gsi(self, quiet: bool = False) -> bool:
        from ..gsi import install

        try:
            install.install(self.ctl.cfg)
        except Exception as exc:
            if not quiet:
                self.toast.show_message(tr("Couldn't install: {error}", error=exc))
            return False
        self.toast.show_message("Updated Dota's connection file. Restart Dota 2 if it's open." if quiet else
                                "Installed. Restart Dota 2 if it's open.")
        self._busy = False
        self.recheck()
        return True

    def _copy_launch_option(self) -> None:
        from ..gsi import install

        QGuiApplication.clipboard().setText(install.LAUNCH_OPTION)
        self.toast.show_message(tr("Copied {option}. Paste it into Dota 2's Launch Options in Steam.",
                                    option=install.LAUNCH_OPTION))


# --------------------------------------------------------------------------- Settings


class SG:
    """More Segoe MDL2 Assets code points (theme.ICON_FONT), for the Settings page's icons."""

    POWER = ""
    CLOUD = ""
    KEY = ""
    BULB = ""
    CLOCK = ""
    EYE = ""
    CAP = ""
    STAR = ""
    SPEAKER = ""
    PERSON = ""
    SLIDERS = ""
    GAUGE = ""
    FILTER = ""
    HEADPHONES = ""
    HEADSET = ""
    KEYBOARD = ""
    NOTE = ""
    CARDS = ""
    POINTER = ""
    RESIZE = ""
    DOCK = ""
    MONITOR = ""
    BELL = ""
    RUNE = ""
    STACK = ""
    MOON = ""
    HEART = ""
    GAMEPAD = ""
    TRAY = ""
    SYNC = ""
    WINDOW = ""
    TOOLS = ""
    CAMERA = ""
    MAP = ""
    ANTENNA = ""
    CHART = ""
    VIDEO = ""
    EDIT = ""
    SEARCH = ""
    CHECKLIST = ""


# Each section's icon and color, on its tab and its header. Cyan is Doty's own (the AI coach) and amber the alerts;
# the others are identity colors only, kept away from green and red (good, bad) and gold (money).
VIOLET, PINK, MINT, STEEL = "#b18cff", "#ff7eb6", "#4fd8b4", "#9aa8b6"
SECTIONS = [
    ("AI coach", Glyph.ROBOT, BRAND),
    ("Voice", Glyph.VOLUME, VIOLET),
    ("Push-to-talk", Glyph.MIC, PINK),
    ("Overlay", SG.MONITOR, BLUE),
    ("Alerts", SG.BELL, AMBER),
    ("App", SG.WINDOW, MINT),
    ("Advanced", SG.TOOLS, STEEL),
]
SETTINGS_TABS = [name for name, _glyph, _color in SECTIONS]
CONTROL_W = 300  # pickers are at least this wide, so the controls line up down the page
RAIL_AT = 1180  # from this page width the sections are a rail on the left; below it, a strip of tabs on top
UNSAVED = AMBER  # changed on this page but not saved yet: the row's edge, its tab's dot, the save bar
GROUP_FILL = "#121821"
QWIDGETSIZE_MAX = 16777215


def _icon_font(px: int) -> QFont:
    f = QFont(theme.ICON_FONT)
    f.setPixelSize(px)
    return f


def glyph_icon(glyph: str, color: str, px: int = 16) -> QIcon:
    """An icon-font glyph as a QIcon (for a line edit's action)."""
    pm = QPixmap(px * 2, px * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setFont(_icon_font(px - 3))
    p.setPen(QColor(color))
    p.drawText(QRectF(0, 0, px, px), Qt.AlignCenter, glyph)
    p.end()
    return QIcon(pm)


class FitCombo(QComboBox):
    """A picker CONTROL_W wide, or wider if an option needs it (no clipped words), so the controls line up down
    the page. (Its width is its size hint: the stylesheet's min-width would undo a fixed width when it polishes.)"""

    def __init__(self, width: int = CONTROL_W) -> None:
        super().__init__()
        self._width = width
        self.setCursor(Qt.PointingHandCursor)
        self.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802
        s = super().sizeHint()
        return QSize(min(460, max(self._width, s.width())), s.height())

    def fit_list(self) -> None:
        """The open list as wide as its longest option."""
        self.view().setMinimumWidth(self.view().sizeHintForColumn(0) + 40)
        self.updateGeometry()


def _fit_combo(box: FitCombo) -> FitCombo:
    box.fit_list()
    return box


class FlagStrip(QWidget):
    """The languages a voice speaks, as a strip of their flags (two lines when there are many). The app's own
    language is ringed when it's among them. Hover for the names."""

    PER_LINE, FLAG_H, GAP = 8, 16, 6

    def __init__(self, codes: list[str], current: str = "en", empty: str = "") -> None:
        super().__init__()
        from .. import languages

        self.codes, self.current, self.empty = list(codes), current, empty
        lines = max(1, -(-len(self.codes) // self.PER_LINE))
        self._fw = round(self.FLAG_H * 1.5)
        self.setFixedSize(CONTROL_W, lines * (self.FLAG_H + self.GAP) + 4)
        with i18n.as_is():
            self.setToolTip(", ".join(languages.own_name(c) for c in self.codes))

    def paintEvent(self, event) -> None:  # noqa: N802
        from .flags import flag_pixmap

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.codes:
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(self.rect()), Qt.AlignRight | Qt.AlignVCenter, self.empty)
            p.end()
            return
        for i, code in enumerate(self.codes):
            line, col = divmod(i, self.PER_LINE)
            n = min(self.PER_LINE, len(self.codes) - line * self.PER_LINE)
            x = self.width() - n * (self._fw + self.GAP) + self.GAP + col * (self._fw + self.GAP) - 4
            y = 4 + line * (self.FLAG_H + self.GAP)
            p.drawPixmap(x, y, flag_pixmap(code, self.FLAG_H))
            if code == self.current:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(BRAND), 2))
                p.drawRoundedRect(QRectF(x - 2, y - 2, self._fw + 4, self.FLAG_H + 4), 3, 3)
        p.end()


class IconBadge(QWidget):
    """A glyph on a soft disc in a section's color (each setting), or on a cut-corner tile (a section's header)."""

    def __init__(self, glyph: str, color: str, size: int = 30, glyph_px: int = 14, tile: bool = False) -> None:
        super().__init__()
        self.glyph, self.color, self.tile, self.glyph_px = glyph, QColor(color), tile, glyph_px
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._glow = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(900)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_glow)

    def _set_glow(self, v) -> None:
        self._glow = float(v)
        self.update()

    def flash(self) -> None:
        """Light up for a moment (the section you just jumped to)."""
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from .brand import cut_path

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = QColor(self.color)
        fill.setAlphaF(min(1.0, (0.16 if self.tile else 0.13) + 0.3 * self._glow))
        if self.tile:
            path = cut_path(r, round(self.width() * 0.26))
            p.fillPath(path, fill)
            edge = QColor(self.color)
            edge.setAlphaF(0.5 + 0.5 * self._glow)
            p.setPen(QPen(edge, 1.0))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawEllipse(r)
        p.setFont(_icon_font(self.glyph_px))
        p.setPen(self.color.lighter(112))
        p.drawText(QRectF(self.rect()), Qt.AlignCenter, self.glyph)
        p.end()


class SettingsRow(QWidget):
    """One setting: its icon on a soft disc, its name and what it does, and the control on the right (under the
    words when the row is too narrow for both). A row you changed but haven't saved gets an amber edge."""

    ROOM = 250  # the words keep at least this much width, else the control moves under them

    def __init__(self, glyph: str, color: str, title: str, description: str, control: QWidget) -> None:
        super().__init__()
        self._stacked = False
        self.control, self.title_text, self.modified = control, title, False
        self.engine_hidden = self.filtered = False  # (the voice rows for other services; a search)
        self.search_text = f"{title} {description}".lower()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(14)
        self.badge = IconBadge(glyph, color)
        lay.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.body = QBoxLayout(QBoxLayout.LeftToRight)
        self.body.setSpacing(20)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addStretch(1)
        self.texts = QVBoxLayout()  # (the AI rows add a hint line here)
        self.texts.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = label(title)
        self.title.setStyleSheet("font-size: 10.5pt; font-weight: 600;")
        top.addWidget(self.title)
        self.mark = QLabel("Unsaved")
        self.mark.setFont(caps_font(8, 800, 1.2))
        self.mark.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-size: 8pt; font-weight: 800; color: {UNSAVED};')
        self.mark.hide()
        top.addWidget(self.mark, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.texts.addLayout(top)
        if description:
            desc = label(description, "muted", wrap=True)
            desc.setStyleSheet("font-size: 9.5pt;")
            self.texts.addWidget(desc)
        words.addLayout(self.texts)
        words.addStretch(1)
        self.body.addLayout(words, 1)
        self.body.addWidget(control, 0, Qt.AlignVCenter | Qt.AlignRight)
        lay.addLayout(self.body, 1)
        if not control.accessibleName():
            control.setAccessibleName(title)  # (screen readers say which setting a switch or picker is)

    def set_modified(self, on: bool) -> None:
        if on != self.modified:
            self.modified = on
            self.mark.setVisible(on)
            self.update()

    def refresh_visibility(self) -> None:
        self.setVisible(not (self.engine_hidden or self.filtered))

    def event(self, event) -> bool:
        done = super().event(event)
        if event.type() in (QEvent.Resize, QEvent.LayoutRequest) and hasattr(self, "body"):
            self._fit()  # (the AI rows swap their controls when you pick another provider)
        return done

    def _fit(self) -> None:
        lay = self.layout()
        m = lay.contentsMargins()
        need = max(self.control.sizeHint().width(), self.control.minimumWidth())
        room = self.width() - m.left() - m.right() - self.badge.width() - lay.spacing() - 20 - need
        stack = room < self.ROOM + (24 if self._stacked else 0)
        if stack != self._stacked:
            self._stacked = stack
            self.body.setDirection(QBoxLayout.TopToBottom if stack else QBoxLayout.LeftToRight)
            self.body.setSpacing(8 if stack else 20)
            self.body.setAlignment(self.control, Qt.AlignVCenter | (Qt.AlignLeft if stack else Qt.AlignRight))

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self.modified:
            p = QPainter(self)
            tint = QColor(UNSAVED)
            tint.setAlphaF(0.06)
            p.fillRect(self.rect(), tint)
            p.fillRect(QRectF(0, 0, 3, self.height()), QColor(UNSAVED))
            p.end()


class _Veil(QWidget):
    """Dims what's under it while clicks go through: settings that wait on their section's main switch."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._alpha = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set)
        self.hide()

    def set_dimmed(self, on: bool, animate: bool = True) -> None:
        target = 0.5 if on else 0.0
        self._anim.stop()
        if not animate or not self.parentWidget().isVisible():
            self._set(target)
            return
        self._anim.setStartValue(self._alpha)
        self._anim.setEndValue(target)
        self._anim.start()

    @property
    def dimmed(self) -> bool:
        return self._alpha > 0.01

    def _set(self, v) -> None:
        self._alpha = float(v)
        self.setVisible(self._alpha > 0.01)
        self.raise_()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        c = QColor(theme.SURFACE)
        c.setAlphaF(self._alpha)
        p.fillRect(self.rect(), c)
        p.end()


class _GroupBox(QWidget):
    """The panel behind a group: a shade lighter than its card (tinted in the section's color for its main switch),
    with hairlines between the rows you can see."""

    def __init__(self, color: str | None) -> None:
        super().__init__()
        self.color = QColor(color) if color else None
        self.veil = _Veil(self)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.veil.setGeometry(self.rect())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(GROUP_FILL))
        if self.color is not None:
            tint = QColor(self.color)
            tint.setAlphaF(0.06)
            p.fillRect(self.rect(), tint)
            edge = QColor(self.color)
            edge.setAlphaF(0.3)
        else:
            edge = QColor("#1e2631")
        p.setPen(QPen(edge, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setPen(QPen(QColor("#1d242e"), 1))
        lay, first = self.layout(), True
        for i in range(lay.count() if lay else 0):
            w = lay.itemAt(i).widget()
            if w is None or w.isHidden() or w is self.veil:
                continue
            if not first:  # in the 1 px gap above the row, from where the words start
                p.drawLine(58, w.y() - 1, self.width() - 2, w.y() - 1)
            first = False
        p.end()


class SettingsGroup(QWidget):
    """Related settings on one panel, with a small caption above it."""

    def __init__(self, caption: str, rows: list, color: str | None = None) -> None:
        super().__init__()
        self.rows = rows
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        if caption:
            cap = label(caption.upper(), "eyebrow")
            cap.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
            cap.setContentsMargins(2, 0, 0, 0)
            lay.addWidget(cap)
        self.box = _GroupBox(color)
        box_lay = QVBoxLayout(self.box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(1)  # the box draws a hairline in each gap
        for row in rows:
            box_lay.addWidget(row)
        lay.addWidget(self.box)
        self.box.veil.raise_()


class _Clickable(QWidget):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class SettingsSection(Card):
    """A section: a cut-corner card with its color along the top edge, its icon tile and title, then its groups.
    `fold`: the groups open on a click (Advanced)."""

    def __init__(self, name: str, glyph: str, color: str, subtitle: str, fold: bool = False) -> None:
        super().__init__(margins=(24, 20, 24, 22), spacing=16, accent=color)
        self.name, self.color = name, color
        self.groups: list[SettingsGroup] = []
        self.followers: list[SettingsGroup] = []  # dimmed while the section's main switch is off
        self.head = _Clickable() if fold else QWidget()
        head = QHBoxLayout(self.head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(14)
        self.badge = IconBadge(glyph, color, 42, 20, tile=True)
        head.addWidget(self.badge, 0, Qt.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(1)
        title = label(name.upper(), "section")
        title.setStyleSheet(f"color: {TEXT}; font-size: 14pt; font-weight: 800;")
        words.addWidget(title)
        if subtitle:
            sub = label(subtitle, "muted", wrap=True)
            sub.setStyleSheet("font-size: 9.5pt;")
            words.addWidget(sub)
        head.addLayout(words, 1)
        self.state = Tag("", GREY, pt=9)
        self.state.hide()
        head.addWidget(self.state, 0, Qt.AlignVCenter)
        self.body.addWidget(self.head)
        self.fold = None
        if fold:
            self.head.setCursor(Qt.PointingHandCursor)
            self.count = label("", "faint")
            self.toggle = button("Show", "link")
            self.chevron = icon_label(Glyph.CHEVRON_RIGHT, 12, color)
            for w in (self.count, self.toggle, self.chevron):
                head.addWidget(w, 0, Qt.AlignVCenter)
            self.fold = Collapsible("", "", expanded=False)
            self.fold.header.hide()
            self.fold.body_lay.setContentsMargins(0, 2, 0, 0)
            self.fold.body_lay.setSpacing(16)
            self.body.addWidget(self.fold)
            self.head.clicked.connect(self.flip)
            self.toggle.clicked.connect(lambda _c=False: self.flip())
            self.fold.toggled.connect(self._folded)
            self._folded(False)

    def flip(self) -> None:
        self.fold.set_open(not self.fold.is_open)

    def _folded(self, on: bool) -> None:
        set_button(self.toggle, "Hide" if on else "Show")
        self.chevron.setText(Glyph.CHEVRON_DOWN if on else Glyph.CHEVRON_RIGHT)
        self.count.setVisible(not on)

    def add(self, group: SettingsGroup, follows: bool = False) -> None:
        (self.fold.body_lay if self.fold is not None else self.body).addWidget(group)
        self.groups.append(group)
        if follows:
            self.followers.append(group)
        if self.fold is not None:
            self.count.setText(tr("{n} settings", n=sum(len(g.rows) for g in self.groups)))

    def set_on(self, on: bool, animate: bool = True) -> None:
        """The section's main switch: ON or OFF by the title, and the settings that wait on it dim while off."""
        self.state.set("On" if on else "Off", GREEN if on else GREY)
        for g in self.followers:
            g.box.veil.set_dimmed(not on, animate)


class _NavTab(QAbstractButton):
    """A section's tab: its icon in the section's color, then its name. On the rail (wide windows) the tabs stack
    down the left; in a strip (narrower) they run along the top, icons only if even that's too tight. An amber
    dot: the section has changes you haven't saved."""

    def __init__(self, name: str, glyph: str, color: str) -> None:
        super().__init__()
        self.name, self.glyph, self.color = name, glyph, QColor(color)
        self.setText(name)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover)
        self.setAccessibleName(f"{name} settings")
        self.mode, self.dot, self.faded = "rail", False, False
        self._font = caps_font(11, 700, 1.3)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.setToolTip(self.name if mode == "icons" else "")
        self.setSizePolicy(QSizePolicy.Expanding if mode == "rail" else QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.updateGeometry()
        self.update()

    def set_dot(self, on: bool) -> None:
        if on != self.dot:
            self.dot = on
            self.update()

    def set_faded(self, on: bool) -> None:
        if on != self.faded:
            self.faded = on
            self.update()

    def strip_width(self) -> int:
        return 13 + 20 + 9 + QFontMetrics(self._font).horizontalAdvance(self.name) + 18

    def sizeHint(self) -> QSize:  # noqa: N802
        if self.mode == "rail":
            return QSize(190, 42)
        return QSize(46 if self.mode == "icons" else self.strip_width(), 40)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, 42) if self.mode == "rail" else self.sizeHint()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, hover = self.isChecked(), self.underMouse()
        if hover and not on:
            p.fillRect(self.rect(), QColor(255, 255, 255, 9))
        icon, text = QColor(self.color), QColor(theme.TEXT if on or hover else theme.TEXT_2)
        if not (on or hover):
            icon.setAlphaF(0.85)
        if self.faded:  # a search found nothing here
            icon.setAlphaF(0.3)
            text.setAlphaF(0.35)
        ix = (self.width() - 20) / 2 if self.mode == "icons" else 16 if self.mode == "rail" else 13
        p.setFont(_icon_font(16))
        p.setPen(icon)
        p.drawText(QRectF(ix, 0, 20, self.height()), Qt.AlignCenter, self.glyph)
        tx = ix + 29
        if self.mode != "icons":
            p.setFont(self._font)
            p.setPen(text)
            p.drawText(QRectF(tx, 0, self.width() - tx, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.name)
        if self.dot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(UNSAVED))
            if self.mode == "rail":
                c = QPointF(self.width() - 16, self.height() / 2)
            elif self.mode == "icons":
                c = QPointF(ix + 21, 10)
            else:
                c = QPointF(tx + QFontMetrics(self._font).horizontalAdvance(self.name) + 7, self.height() / 2 - 5)
            p.drawEllipse(c, 3.2, 3.2)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.BRAND), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        p.end()


class _NavHighlight(QWidget):
    """The selected tab's backing: a wash of the section's color with a bar (left on the rail, under a tab in the
    strip). It slides to the tab you pick and takes on its color."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.color, self.vertical = QColor(BRAND), True
        self._geo = QPropertyAnimation(self, b"geometry", self)
        self._geo.setDuration(240)
        self._geo.setEasingCurve(QEasingCurve.OutCubic)
        self._tint = QVariantAnimation(self)
        self._tint.setDuration(240)
        self._tint.valueChanged.connect(self._set_color)
        self.hide()

    def _set_color(self, c) -> None:
        self.color = QColor(c)
        self.update()

    def move_to(self, tab: QWidget, animate: bool = True) -> None:
        r, color = tab.geometry(), QColor(tab.color)
        self.lower()
        if not animate or self.isHidden() or self.geometry().isEmpty():
            self._geo.stop()
            self._tint.stop()
            self.setGeometry(r)
            self._set_color(color)
            self.show()
            return
        for anim, start, end in ((self._geo, self.geometry(), r), (self._tint, QColor(self.color), color)):
            anim.stop()
            anim.setStartValue(start)
            anim.setEndValue(end)
            anim.start()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        wash = QColor(self.color)
        wash.setAlphaF(0.13)
        p.fillRect(self.rect(), wash)
        if self.vertical:
            p.fillRect(QRectF(0, 0, 3, self.height()), self.color)
        else:
            p.fillRect(QRectF(0, self.height() - 2, self.width(), 2), self.color)
        p.end()


class _SettingsNav(QWidget):
    """The section tabs, as a rail or a strip."""

    def __init__(self) -> None:
        super().__init__()
        self.lay = QBoxLayout(QBoxLayout.TopToBottom, self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(2)
        self.tabs: list[_NavTab] = []
        self.highlight = _NavHighlight(self)
        self.mode = ""
        self.keys = label("Ctrl+F  find a setting\nCtrl+S  save", "faint")  # (on the rail, under the tabs)
        self.keys.setStyleSheet("font-size: 9pt;")
        self.keys.setContentsMargins(16, 0, 0, 4)

    def add(self, tab: _NavTab) -> None:
        self.lay.addWidget(tab)
        self.tabs.append(tab)

    def finish(self) -> None:
        self.lay.addStretch(1)
        self.lay.addWidget(self.keys)

    def strip_width(self) -> int:
        return sum(t.strip_width() for t in self.tabs) + 4 * (len(self.tabs) - 1)

    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        rail = mode == "rail"
        self.lay.setDirection(QBoxLayout.TopToBottom if rail else QBoxLayout.LeftToRight)
        self.lay.setSpacing(2 if rail else 4)
        for t in self.tabs:
            t.set_mode(mode)
        self.keys.setVisible(rail)
        self.highlight.vertical = rail
        if rail:
            self.setFixedWidth(200)
            self.setMinimumHeight(0)
            self.setMaximumHeight(QWIDGETSIZE_MAX)
        else:
            self.setMinimumWidth(0)
            self.setMaximumWidth(QWIDGETSIZE_MAX)
            self.setFixedHeight(40)
        QTimer.singleShot(0, lambda: self.follow(animate=False))

    def follow(self, tab: _NavTab | None = None, animate: bool = True) -> None:
        tab = tab or next((t for t in self.tabs if t.isChecked()), None)
        if tab is not None:
            self.highlight.move_to(tab, animate)

    def event(self, event) -> bool:
        done = super().event(event)
        if event.type() == QEvent.LayoutRequest:
            self.follow(animate=False)
        return done

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.follow(animate=False)


class _ChangeList(QLabel):
    """One line (ending in "…" if it doesn't fit) listing what you changed; click it to go to the first one."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet("font-size: 9.5pt;")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Go to the first change")

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(QColor(theme.TEXT_2))
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft,
                   self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width()))
        p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _SearchField(QLineEdit):
    """Find a setting by name; Esc clears it."""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self.text():
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)


class SettingsPage(QWidget):
    eleven_loaded = Signal(object)  # list of voices, or an error message
    preview_done = Signal(str)  # "" or a problem to show
    open_setup = Signal()  # the setup checklist (it has no tab of its own)
    kitten_progress = Signal(int)  # percent, while the Kitten voice downloads
    kitten_done = Signal(str)  # "" = downloaded, else the problem
    instructions_read = Signal(object)  # (coach/instructions.Reading, what the settings held before it)

    def __init__(self, ctl: Controller, toast: Toast) -> None:
        super().__init__()
        self.ctl, self.toast = ctl, toast
        self.instructions_read.connect(self._on_instructions_read)
        ctl.preferences_changed.connect(self._on_preferences_changed)
        self._read_note = ""  # how the instructions were last read (shown under them)
        self._read_extra: dict = {}  # subjects and talk that reading changed: shown with the items, to untick
        self._read_in_background = True  # (tests read on the spot)
        self._took_lay: QVBoxLayout | None = None
        self._eleven_voices: list[dict] | str | None = None
        self._eleven_loading = False
        self._filling = False
        self.eleven_loaded.connect(self._on_eleven_loaded)
        self.preview_done.connect(self._on_preview_done)
        self.kitten_progress.connect(self._on_kitten_progress)
        self.kitten_done.connect(self._on_kitten_done)
        self._kitten_btn = None
        self._kitten_downloading = False
        ctl.kokoro_progress.connect(self._on_kokoro_progress)
        ctl.kokoro_done.connect(self._on_kokoro_done)
        self._kokoro_btn = None
        self._play_after_download = False
        from .ai_settings import AIRows

        self.ai = AIRows(self)
        self.setObjectName("page")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(34, 26, 34, 18)
        self.outer.setSpacing(16)
        top = QHBoxLayout()  # the title with search and the checklist beside it, so narrow windows lose no line
        top.setSpacing(10)
        top.addWidget(label("Settings", "h1"), 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.search = _SearchField()
        self.search.setPlaceholderText("Find a setting")
        self.search.setToolTip("Find a setting (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(glyph_icon(SG.SEARCH, theme.TEXT_2), QLineEdit.LeadingPosition)
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 0, Qt.AlignVCenter)
        self.setup_btn = button("Setup checklist", "ghost", SG.CHECKLIST,
                                "Connect Dota, the AI coach, the voice and the mic, one step at a time")
        self.setup_btn.clicked.connect(self.open_setup)
        top.addWidget(self.setup_btn, 0, Qt.AlignVCenter)
        head = QVBoxLayout()
        head.setSpacing(0)
        head.addLayout(top)
        head.addWidget(label("Changes apply when you save. Doty restarts itself if it's running.", "muted", wrap=True))
        self.outer.addLayout(head)

        self.body = QBoxLayout(QBoxLayout.LeftToRight)
        self.body.setSpacing(22)
        self.nav = _SettingsNav()
        self.tabs = QButtonGroup(self)
        self.tabs.setExclusive(True)
        for i, (name, glyph, color) in enumerate(SECTIONS):
            tab = _NavTab(name, glyph, color)
            self.tabs.addButton(tab, i)
            self.nav.add(tab)
        self.nav.finish()
        self.tabs.button(0).setChecked(True)
        self.tabs.idClicked.connect(self._jump)
        self.tabs.idToggled.connect(lambda i, on: self.nav.follow(self.tabs.button(i)) if on else None)
        self.body.addWidget(self.nav)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().valueChanged.connect(self._follow_scroll)
        self.body.addWidget(self.scroll, 1)
        self.outer.addLayout(self.body, 1)
        self._cards: dict[str, SettingsSection] = {}
        self._scroll_anim: QPropertyAnimation | None = None
        self._jumping = False

        self.bar = Card("savebar", margins=(14, 10, 12, 10))
        self.bar.setStyleSheet(f"#savebar {{ background: {theme.SURFACE_2}; border: 1px solid {rgba(UNSAVED, 0.35)};"
                               f" border-left: 3px solid {UNSAVED}; border-radius: 2px; }}")
        bar = QHBoxLayout()
        bar.setSpacing(12)
        bar.addWidget(IconBadge(SG.EDIT, UNSAVED, 34, 15), 0, Qt.AlignVCenter)
        words = QVBoxLayout()
        words.setSpacing(0)
        self.bar_title = label("Unsaved changes")
        self.bar_title.setStyleSheet("font-size: 10.5pt; font-weight: 700;")
        words.addWidget(self.bar_title)
        self.bar_list = _ChangeList()
        self.bar_list.clicked.connect(self._show_first_change)
        words.addWidget(self.bar_list)
        bar.addLayout(words, 1)
        discard = button("Discard", "ghost", tooltip="Put everything back the way it's saved")
        save = button("Save changes", "primary", Glyph.CHECK, "Save changes (Ctrl+S)")
        discard.clicked.connect(lambda: self._build())
        save.clicked.connect(lambda: self._save())
        bar.addWidget(discard)
        bar.addWidget(save)
        self.bar.body.addLayout(bar)
        self.outer.addWidget(self.bar)
        for keys, fn in ((QKeySequence.Save, self._save_shortcut), (QKeySequence.Find, self._find_shortcut)):
            QShortcut(QKeySequence(keys), self).activated.connect(fn)
        self._voices: list[str] | None = None
        self._system_langs: set[str] | None = None
        self._mics: list[str] | None = None
        self._tracked: list[tuple[SettingsRow, set, int]] = []
        self._build()
        self._fit_layout()

    # helpers ---------------------------------------------------------------------
    def _save_shortcut(self) -> None:
        if self.isVisible() and not self.bar.isHidden():
            self._save()

    def _find_shortcut(self) -> None:
        if self.isVisible():
            self.search.setFocus(Qt.ShortcutFocusReason)
            self.search.selectAll()

    def _key(self, obj, attr: str) -> tuple[str, str]:
        """(section, field) of a setting, e.g. ("coach", "model"): what the save bar compares."""
        from dataclasses import fields

        for f in fields(self.draft):
            if getattr(self.draft, f.name) is obj:
                return f.name, attr
        return "", attr

    def _pending(self) -> list[tuple[str, str]]:
        """The settings that differ from what the page started from (what saving would change)."""
        from dataclasses import fields, is_dataclass

        out = []
        for section in fields(self.draft):
            mine, base = getattr(self.draft, section.name), getattr(self._base, section.name)
            if not is_dataclass(mine):
                continue
            for f in fields(mine):
                key, value = (section.name, f.name), getattr(mine, f.name)
                if value != getattr(base, f.name) and not (key in self._auto and value == self._auto[key]):
                    out.append(key)
        return out

    def _dirty(self, *_args) -> None:
        """Something changed: mark each setting that now differs from what's saved (its row and its section's tab),
        list them on the save bar and show it (or hide it, if you changed everything back)."""
        pending = set(self._pending())
        tabs, titles = set(), []
        for row, keys, tab in self._tracked:
            on = bool(keys & pending)
            try:
                row.set_modified(on)
            except RuntimeError:  # rebuilt meanwhile
                continue
            if on:
                tabs.add(tab)
                if row.title_text not in titles:
                    titles.append(row.title_text)
        for i, tab in enumerate(self.nav.tabs):
            tab.set_dot(i in tabs)
        n = len(titles) or len(pending)
        self.bar_title.setText(tr("1 unsaved change") if n == 1 else tr("{n} unsaved changes", n=n))
        self.bar_list.setText(" · ".join(titles) if titles else "Save to apply them.")
        self.bar_list.update()
        self._update_cost()
        was_hidden = self.bar.isHidden()
        self.bar.setVisible(bool(pending))
        if pending and was_hidden and self.isVisible():
            fade_in(self.bar, rise=0)

    def _show_first_change(self) -> None:
        row = next((r for r, _keys, _tab in self._tracked if r.modified), None)
        if row is None:
            return
        card = next((c for c in self._cards.values() if c.isAncestorOf(row)), None)
        if card is not None and card.fold is not None and not card.fold.is_open:
            card.fold.set_open(True, animate=False)
            QTimer.singleShot(0, self._show_first_change)
            return
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self._scroll_to(row.mapTo(self.scroll.widget(), QPoint(0, 0)).y() - 90, after=row.badge.flash)

    def setting_row(self, glyph: str, title: str, description: str, control: QWidget, keys=()) -> SettingsRow:
        """A row in the section being built. `keys`: the (section, field) settings it edits (else the control's
        own, which _switch and _combo note)."""
        row = SettingsRow(glyph, self._color, title, description, control)
        self._tracked.append((row, set(keys) or set(getattr(control, "_keys", ())), self._tab))
        return row

    def _switch(self, obj, attr: str, invert: bool = False) -> Switch:
        sw = Switch(bool(getattr(obj, attr)) != invert)
        sw.toggled.connect(lambda on: (setattr(obj, attr, on != invert), self._dirty()))
        sw._keys = {self._key(obj, attr)}
        return sw

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_layout()
        self._fit_tail()

    def _fit_layout(self) -> None:
        """A rail of sections on the left when there's room, else a strip of tabs on top (icons only if tight)."""
        w = self.width()
        rail = w >= RAIL_AT
        fit_width(self.outer, w, 1290 if rail else 1100)
        m = self.outer.contentsMargins()
        mode = "rail" if rail else "strip" if self.nav.strip_width() <= w - m.left() - m.right() else "icons"
        if mode != self.nav.mode:
            self.nav.set_mode(mode)
            self.body.setDirection(QBoxLayout.LeftToRight if rail else QBoxLayout.TopToBottom)
            self.body.setSpacing(22 if rail else 12)

    def _combo(self, obj, attr: str, options: list[tuple[object, str]]) -> QComboBox:
        box = FitCombo()
        current = getattr(obj, attr)
        values = [v for v, _ in options]
        if current not in values:
            options = options + [(current, str(current))]
        for value, text in options:
            box.addItem(text, value)
        box.setCurrentIndex([v for v, _ in options].index(current))
        box.currentIndexChanged.connect(lambda _i: (setattr(obj, attr, box.currentData()), self._dirty()))
        box._keys = {self._key(obj, attr)}
        return _fit_combo(box)

    def _level_combo(self, d) -> QComboBox:
        """Your level; picking one yourself stops it following the rank on the Profile page."""
        box = self._combo(d.coach, "level", [(k, f"{tr(name)} · {tr(who)}") for k, name, who in LEVELS])
        def picked(_i) -> None:
            base = getattr(self, "_base", None)
            same = base is not None and d.coach.level == base.coach.level
            d.coach.level_from_rank = base.coach.level_from_rank if same else False  # (back to where it was: as saved)
            self._dirty()

        box.currentIndexChanged.connect(picked)
        box._keys.add(("coach", "level_from_rank"))
        return box

    # your instructions to the coach -------------------------------------------------------------------------------
    def _instructions_row(self, d) -> SettingsRow:
        """The player's instructions in their own words, and under them what Doty took from them as settings (to
        untick) and what it kept as style. Read on saving (coach/instructions.py)."""
        from PySide6.QtWidgets import QPlainTextEdit

        from ..coach.prompts import INSTRUCTIONS_MAX

        count = QLabel(f"{len(d.coach.instructions)}/{INSTRUCTIONS_MAX}")
        count.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        row = self.setting_row(
            SG.EDIT, "Your instructions to the coach", "How you play and what you want to hear, like custom "
            "instructions in a chat app. On saving, Doty turns what it can into settings; the AI reads the rest as "
            "your style.", count, keys=[("coach", "instructions"), ("coach", "never_suggest"),
                                        ("coach", "hero_builds")])
        edit = QPlainTextEdit()
        edit.setPlainText(d.coach.instructions)
        edit.setPlaceholderText(tr("For example: I play position 4. On Spirit Breaker I always build Boots of "
                                   "Bearing. Never suggest Radiance."))
        edit.setTabChangesFocus(True)
        edit.setFixedHeight(88)
        edit.setStyleSheet(f"QPlainTextEdit {{ background: {theme.SURFACE_2}; border: 1px solid {theme.BORDER_STRONG};"
                           f" border-radius: 2px; padding: 4px 6px; }} QPlainTextEdit:focus {{ border-color: "
                           f"{theme.BRAND}; }}")

        def changed() -> None:
            text = edit.toPlainText()
            if len(text) > INSTRUCTIONS_MAX:  # (kept short: it goes with every look)
                edit.blockSignals(True)
                edit.setPlainText(text[:INSTRUCTIONS_MAX])
                edit.moveCursor(edit.textCursor().MoveOperation.End)
                edit.blockSignals(False)
                text = text[:INSTRUCTIONS_MAX]
            d.coach.instructions = text
            count.setText(f"{len(text)}/{INSTRUCTIONS_MAX}")
            self._dirty()

        edit.textChanged.connect(changed)
        self._instructions_edit = edit
        row.texts.addWidget(edit)
        took = QWidget()
        self._took_lay = QVBoxLayout(took)
        self._took_lay.setContentsMargins(0, 4, 0, 0)
        self._took_lay.setSpacing(3)
        row.texts.addWidget(took)
        self._fill_took()
        return row

    def _names(self):
        """Something that names items and heroes: the running engine's knowledge, else names made from the keys."""
        kb = self.ctl.doty.kb if self.ctl.doty is not None else None
        if kb is not None and kb.items:
            return kb
        if getattr(self, "_names_kb", None) is not None:  # (the one the last reading loaded)
            return self._names_kb
        from ..knowledge.constants import Knowledge

        return Knowledge()

    def _fill_took(self) -> None:
        """'What Doty took from this': each setting it made, ticked (untick to drop it), then what it kept as style."""
        from PySide6.QtWidgets import QCheckBox

        lay, d = self._took_lay, self.draft
        if lay is None:
            return
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        kb = self._names()
        entries: list[tuple[str, bool, object]] = []  # (text, ticked, what unticking undoes)
        base = self._base.coach
        for item in dict.fromkeys(list(base.never_suggest) + list(d.coach.never_suggest)):
            entries.append((tr("Never suggest {item}", item=kb.item_name(item)), item in d.coach.never_suggest,
                            ("never", item)))
        for hero, items in {**base.hero_builds, **d.coach.hero_builds}.items():
            entries.append((tr("On {hero}: {items}", hero=kb.hero_name(hero),
                               items=", ".join(kb.item_name(i) for i in items)),
                            hero in d.coach.hero_builds, ("build", hero, list(items))))
        for topic in self._read_extra.get("topics", []):
            entries.append((tr("Don't bring up: {topic}", topic=tr(TOPIC_TITLES.get(topic, topic))),
                            not getattr(d.suggest, topic, True), ("topic", topic)))
        speak = self._read_extra.get("speak")
        if speak:
            entries.append((tr("When it talks: {choice}", choice=tr(dict(SPEAK_UP).get(speak[0], speak[0]))),
                            d.coach.speak_up == speak[0], ("speak",) + tuple(speak)))
        style = list(d.coach.instructions_style)
        if not entries and not style:
            if not self._read_note:
                return
        head = label("What Doty took from this", "eyebrow")
        head.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        lay.addWidget(head)
        for text, ticked, what in entries:
            box = QCheckBox(text)
            box.setChecked(ticked)
            box.setCursor(Qt.PointingHandCursor)
            box.toggled.connect(lambda on, what=what: self._untick(what, on))
            lay.addWidget(box)
        for line in style:
            kept = label(tr("Kept as style: {text}", text=line), "muted", wrap=True)
            kept.setStyleSheet("font-size: 9pt;")
            lay.addWidget(kept)
        if not entries and not style:
            lay.addWidget(label("Nothing yet. Write your instructions and save.", "muted", wrap=True))
        if self._read_note:
            note = label(self._read_note, "faint", wrap=True)
            note.setStyleSheet("font-size: 8.5pt;")
            lay.addWidget(note)

    def _untick(self, what: tuple, on: bool) -> None:
        """A setting taken from the instructions, ticked or unticked (a draft change, saved with Save)."""
        coach = self.draft.coach
        kind = what[0]
        if kind == "never":
            item = what[1]
            now = [i for i in coach.never_suggest if i != item]
            coach.never_suggest = now + [item] if on else now
        elif kind == "build":
            builds = dict(coach.hero_builds)
            if on:
                builds[what[1]] = list(what[2])
            else:
                builds.pop(what[1], None)
            coach.hero_builds = builds
        elif kind in ("topic", "speak"):
            # (through the setting's own control on this page, so the switch or picker shows it too)
            key = ("suggest", what[1]) if kind == "topic" else ("coach", "speak_up")
            control = next((r.control for r, keys, _tab in self._tracked if key in keys and r.control is not None
                            and key != ("coach", "instructions")), None)
            if kind == "topic":
                if isinstance(control, Switch):
                    control.setChecked(not on)
                setattr(self.draft.suggest, what[1], not on)
            else:
                value = what[1] if on else what[2]
                if isinstance(control, QComboBox) and control.findData(value) >= 0:
                    control.setCurrentIndex(control.findData(value))
                coach.speak_up = value
        self._dirty()

    def _reading_kb(self):
        """Knowledge with the real item and hero names to check a reading against (on a worker thread)."""
        kb = self.ctl.doty.kb if self.ctl.doty is not None else None
        if kb is not None and kb.items:
            return kb
        import asyncio

        from ..knowledge.constants import Knowledge

        kb = Knowledge()
        try:
            asyncio.run(kb.load())
        except Exception as exc:  # (no names: the reading keeps everything as style)
            print(f"[doty] couldn't load item names to read your instructions: {exc!r}")
        return kb

    def _read_job(self, text: str, coach_cfg, before: tuple) -> tuple:
        """Read the instructions (the AI when one is set up, else Doty's own reader). Runs off the GUI thread."""
        import asyncio

        from ..coach import instructions
        from ..coach.backends import BackendError, make_backend

        kb = self._reading_kb()
        if kb.items:
            self._names_kb = kb  # (so the list names items as the game does)

        async def read():
            backend = None
            if coach_cfg.enabled:
                try:
                    backend = make_backend(coach_cfg)
                except (BackendError, Exception) as exc:  # (no key, no provider: read in code)
                    print(f"[doty] no AI to read your instructions: {exc}")
            try:
                return await instructions.read_with_ai(text, kb, backend)
            finally:
                if backend is not None:
                    await backend.aclose()

        try:
            reading = asyncio.run(read())
        except Exception as exc:
            print(f"[doty] reading your instructions failed: {exc!r}")
            reading = instructions.read_simply(text, kb)
        return reading, before

    def _read_instructions(self, text: str) -> None:
        """Saved new instructions: read them into settings, in the background (the AI may take a few seconds)."""
        cfg = self.ctl.cfg
        before = (list(cfg.coach.never_suggest), dict(cfg.coach.hero_builds), cfg.coach.speak_up)
        if not text.strip():
            self._read_extra, self._read_note = {}, ""
            self.ctl.set_preferences(instructions_style=[])
            return
        self._read_note = tr("Reading your instructions with the AI…") if cfg.coach.enabled else ""
        coach_cfg = copy.deepcopy(cfg.coach)
        if not self._read_in_background:
            self._on_instructions_read(self._read_job(text, coach_cfg, before))
            return
        threading.Thread(target=lambda: self.instructions_read.emit(self._read_job(text, coach_cfg, before)),
                         daemon=True, name="doty-read-instructions").start()

    def _on_instructions_read(self, result) -> None:
        """A reading is in: into the saved (and running) settings at once, with no restart, and shown to untick."""
        from ..coach import instructions

        reading, (never, builds, speak_was) = result
        cfg = copy.deepcopy(self.ctl.cfg)
        instructions.apply(cfg, reading, never, builds)
        self._read_extra = {"topics": list(reading.topics_off)}
        if reading.speak_up:
            self._read_extra["speak"] = (reading.speak_up, speak_was)
        self._read_note = tr("Read by the AI.") if reading.by == "ai" else tr("Read by Doty itself, without the AI.")
        self.ctl.set_preferences(suggest={t: False for t in reading.topics_off},
                                 never_suggest=cfg.coach.never_suggest, hero_builds=cfg.coach.hero_builds,
                                 speak_up=cfg.coach.speak_up, instructions_style=cfg.coach.instructions_style)

    def _on_preferences_changed(self) -> None:
        """The never-suggest list or a reading changed outside the page: shown at once. With nothing unsaved the page
        is rebuilt (the switches show it too); otherwise only those fields and the list change."""
        try:
            pending = bool(self._pending())
        except AttributeError:
            return
        if not pending:
            self._build()
            return
        for name in ("never_suggest", "hero_builds", "instructions_style"):
            value = copy.deepcopy(getattr(self.ctl.cfg.coach, name))
            setattr(self._base.coach, name, value)
            setattr(self.draft.coach, name, copy.deepcopy(value))
        self._fill_took()
        self._dirty()

    def _slider(self, lo: int, hi: int, value: int, fmt, on_change, step: int = 1) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setPageStep(step)
        s.setValue(value)
        s.setFixedWidth(CONTROL_W - 12 - 64)
        s.setCursor(Qt.PointingHandCursor)
        val = QLabel(fmt(value))  # the value, plainly, in the section's color
        val.setFixedSize(64, 28)
        val.setAlignment(Qt.AlignCenter)
        val.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-size: 12pt; font-weight: 800; color: {self._color};'
                          f" background: {rgba(self._color, 0.12)}; border-radius: 2px;")
        s.valueChanged.connect(lambda v: (val.setText(fmt(v)), on_change(v), self._dirty()))
        lay.addWidget(s)
        lay.addWidget(val)
        return w

    def _new_section(self, lay: QVBoxLayout, tab: int, subtitle: str, fold: bool = False) -> SettingsSection:
        """Start a section: the rows made next take its color and count as its changes."""
        name, glyph, color = SECTIONS[tab]
        self._tab, self._color = tab, color
        card = SettingsSection(name, glyph, color, subtitle, fold)
        lay.addWidget(card)
        self._cards[name] = card
        if fold:
            self._advanced = card.fold
        return card

    def _main_switch(self, card: SettingsSection, sw: Switch) -> None:
        """`sw` is the section's main switch: the header says ON or OFF, and what waits on it dims while off."""
        card.set_on(sw.isChecked(), animate=False)
        sw.toggled.connect(card.set_on)

    def _jump(self, index: int) -> None:
        """Scroll smoothly to a section (opening Advanced if that's where you're going)."""
        name = SETTINGS_TABS[index]
        self.tabs.button(index).setChecked(True)
        card = self._cards.get(name)
        if card is not None and card.isHidden() and self.search.text():  # a search hid it: show everything
            self.search.clear()
            QTimer.singleShot(0, lambda: self._jump(index))
            return
        if name == "Advanced" and getattr(self, "_advanced", None) is not None and not self._advanced.is_open:
            self._advanced.set_open(True, animate=False)
            QTimer.singleShot(0, lambda: self._jump(index))  # once the opened section is laid out
            return
        if card is None:
            return
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)  # (sections just shown again: place them)
        self._scroll_to(card.y() - 4, after=card.badge.flash)

    def _scroll_to(self, y: int, after=None) -> None:
        bar = self.scroll.verticalScrollBar()
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
        self._jumping = True
        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(320)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(bar.value())
        anim.setEndValue(min(bar.maximum(), max(0, y)))

        def done() -> None:
            self._jumping = False
            if after is not None:
                try:
                    after()
                except RuntimeError:  # rebuilt meanwhile
                    pass

        anim.finished.connect(done)
        self._scroll_anim = anim
        anim.start()

    def _settle(self, at: int) -> None:
        """After a rebuild is laid out: back to where you were, with that section's tab lit."""
        if self._scroll_anim is not None and self._scroll_anim.state() == QPropertyAnimation.Running:
            return  # a jump is under way (Settings opened at a section)
        self._fit_tail()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(min(at, bar.maximum()))
        self._follow_scroll(bar.value())

    def _fit_tail(self) -> None:
        tail, last = getattr(self, "_tail", None), self._cards.get("Advanced")
        if tail is None or last is None:
            return
        try:
            tail.setFixedHeight(max(0, self.scroll.viewport().height() - last.height() - 24))
        except RuntimeError:  # rebuilt meanwhile
            pass

    def _follow_scroll(self, value: int) -> None:
        """Light up the tab for the section you've scrolled to."""
        if self._jumping or not self._cards:
            return
        current = None
        line = value + self.scroll.viewport().height() // 3  # the section a third of the way down
        for i, name in enumerate(SETTINGS_TABS):
            card = self._cards.get(name)
            if card is not None and not card.isHidden() and (current is None or card.y() <= line):
                current = i
        b = self.tabs.button(current or 0)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def _filter(self, text: str) -> None:
        """Show only the settings whose name or description has what you typed (a section's name shows it all)."""
        q = " ".join(text.lower().split())
        found = False
        for i, (name, _glyph, _color) in enumerate(SECTIONS):
            card = self._cards.get(name)
            if card is None:
                continue
            whole = bool(q) and q in name.lower()
            shown = False
            for g in card.groups:
                g_shown = False
                for r in g.rows:
                    r.filtered = bool(q) and not whole and q not in r.search_text
                    r.refresh_visibility()
                    g_shown |= not r.isHidden()
                g.setVisible(g_shown or not q)
                g.box.update()
                shown |= g_shown
            card.setVisible(shown or not q)
            self.tabs.button(i).set_faded(bool(q) and not shown)
            if q and shown and card.fold is not None and not card.fold.is_open:
                card.fold.set_open(True, animate=False)
            found |= shown
        self._empty.setText(tr("No setting matches “{text}”.", text=text.strip()))
        self._empty.setVisible(bool(q) and not found)
        self._tail.setVisible(not q)
        self.scroll.verticalScrollBar().setValue(0)
        self._follow_scroll(0)
        if q and not found:
            self.nav.highlight.hide()  # (no section to point at)
        else:
            self.nav.follow(animate=False)

    def _update_cost(self) -> None:
        """What a game costs at the rhythm and models picked, under How often it looks (gold: money)."""
        lab = getattr(self, "_cost", None)
        if lab is None:
            return
        c = self.draft.coach
        try:
            cost = preset_cost(c.speculative_interval_s, c.model, c.provider, c.watch_model)
        except Exception:
            cost = ""
        try:
            lab.setText(tr("Costs {cost}", cost=cost) if cost else "")
            lab.setVisible(bool(cost))
        except RuntimeError:  # rebuilt meanwhile
            pass

    def _voice_list(self) -> list[str]:
        if self._voices is None:
            try:
                from ..voice import list_voices

                self._voices = list_voices()
            except Exception:
                self._voices = []
        return self._voices

    def _system_languages(self) -> set[str]:
        """The languages the system's own voice can speak (voice.system_languages), looked up once."""
        if self._system_langs is None:
            try:
                from ..voice import system_languages

                self._system_langs = system_languages()
            except Exception:
                self._system_langs = set()
        return self._system_langs

    def _language_combo(self, d) -> QComboBox:
        """Doty's languages by flag and own name: the six most played first."""
        from .. import languages
        from .flags import flag_icon

        with i18n.as_is():  # (a language's own name is never translated)
            box = self._combo(d.app, "language", [(c, languages.own_name(c)) for c in languages.ordered()])
        for i in range(box.count()):
            box.setItemIcon(i, flag_icon(str(box.itemData(i)), 14))
        box.setIconSize(QSize(21, 14))
        box.blockSignals(True)  # (the line under the six most played isn't a choice, or a change)
        box.insertSeparator(len(languages.FEATURED))
        box.blockSignals(False)
        return box

    def _eleven_combo(self, d) -> QComboBox:
        """ElevenLabs voices, loaded from your account in the background the first time."""
        from ..eleven import api_key as eleven_key

        box = FitCombo()
        self._eleven_box = box
        self._fill_eleven(box, d)
        box._keys = {("voice", "eleven_voice")}
        box.currentIndexChanged.connect(
            lambda _i: None if self._filling else (setattr(d.voice, "eleven_voice", box.currentData() or ""),
                                                   self._dirty()))
        if not eleven_key():
            box.setEnabled(False)
        elif self._eleven_voices is None and not self._eleven_loading:
            self._eleven_loading = True

            def load() -> None:
                from ..eleven import ElevenError, ElevenLabs

                try:
                    found = ElevenLabs(eleven_key()).voices()
                except ElevenError as exc:
                    found = str(exc)
                self.eleven_loaded.emit(found)

            threading.Thread(target=load, name="doty-eleven-voices", daemon=True).start()
        return box

    def _fill_eleven(self, box: QComboBox, d) -> None:
        self._filling = True
        box.clear()
        current = d.voice.eleven_voice
        voices = self._eleven_voices if isinstance(self._eleven_voices, list) else []
        box.addItem("Automatic (a good default)", "")
        for v in sorted(voices, key=lambda v: v["name"].lower()):
            accent = v["labels"].get("accent") or v["labels"].get("gender") or ""
            box.addItem(v["name"] + (f"  ({accent})" if accent else ""), v["id"])
        if current and box.findData(current) < 0:
            box.addItem("Loading voices…" if self._eleven_loading else current, current)
        if isinstance(self._eleven_voices, str):
            box.setToolTip(tr("Couldn't load your voices: {error}", error=self._eleven_voices))
        box.setCurrentIndex(max(0, box.findData(current)))
        _fit_combo(box)  # (your voices arrive later, maybe with longer names)
        self._filling = False

    def _on_eleven_loaded(self, found) -> None:
        self._eleven_loading = False
        self._eleven_voices = found
        box = getattr(self, "_eleven_box", None)
        if box is not None:
            try:
                self._fill_eleven(box, self.draft)
            except RuntimeError:  # the page was rebuilt; the old combo is gone
                pass

    def _picker(self, d, attr: str, voices: dict, default: str, installed: bool, mb: int, download) -> tuple:
        """A free voice's picker (Kokoro, Kitten), plus a Download button until its model is on this PC."""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        box = FitCombo()
        for vid, (text, _extra) in voices.items():
            box.addItem(text, vid)
        current = getattr(d.voice, attr) if getattr(d.voice, attr) in voices else default
        box.setCurrentIndex(box.findData(current))
        box.currentIndexChanged.connect(lambda _i: (setattr(d.voice, attr, box.currentData()), self._dirty()))
        lay.addWidget(_fit_combo(box))
        w._keys = {("voice", attr)}
        b = None
        if not installed:
            b = button(tr("Download ({mb} MB)", mb=mb), "ghost", Glyph.DOWNLOAD)
            b.clicked.connect(lambda _checked=False: download())
            lay.addWidget(b)
        return w, b

    def _kitten_picker(self, d) -> QWidget:
        """Kitten's voices, plus a Download button until its model is on this PC."""
        from .. import kitten

        w, self._kitten_btn = self._picker(d, "kitten_voice", kitten.VOICES, kitten.DEFAULT_VOICE,
                                           kitten.installed(), kitten.DOWNLOAD_MB, self._download_kitten)
        return w

    def _download_kitten(self) -> None:
        if self._kitten_downloading:
            return
        self._kitten_downloading = True
        from .. import kitten

        def work() -> None:
            try:
                kitten.download(progress=lambda done, total: self.kitten_progress.emit(int(100 * done / max(1, total))))
                msg = ""
            except Exception as exc:
                msg = f"couldn't download the Kitten voice ({exc})"
            self.kitten_done.emit(msg)

        threading.Thread(target=work, name="doty-kitten-download", daemon=True).start()
        self._on_kitten_progress(0)

    def _on_kitten_progress(self, pct: int) -> None:
        if self._kitten_btn is not None:
            try:
                set_button(self._kitten_btn, f"Downloading… {pct}%", Glyph.DOWNLOAD)
                self._kitten_btn.setEnabled(False)
            except RuntimeError:  # the page was rebuilt
                pass

    def _on_kitten_done(self, msg: str) -> None:
        self._kitten_downloading = False
        self.toast.show_message(msg or "The Kitten voice is ready. Save to start using it.")
        if not msg:
            self._build(keep_edits=True)  # your unsaved choices (Kitten itself, maybe) stay

    def _kokoro_picker(self, d) -> QWidget:
        """Kokoro's voices in the app's language (English's when Kokoro doesn't speak it), plus a Download
        button until the model, and that language's voices, are on this PC."""
        from .. import kokoro

        lang = self.ctl.cfg.app.language if kokoro.speaks(self.ctl.cfg.app.language) else "en"
        attr = "kokoro_lang_voice" if lang in kokoro.LANG_VOICES else "kokoro_voice"
        w, self._kokoro_btn = self._picker(d, attr, kokoro.voices_for(lang), kokoro.default_voice(lang),
                                           kokoro.installed(language=lang), kokoro.missing_mb(language=lang),
                                           self.ctl.download_kokoro)
        if self._kokoro_btn is not None and self.ctl.kokoro_pct is not None:
            self._on_kokoro_progress(self.ctl.kokoro_pct)
        return w

    def _on_kokoro_progress(self, pct: int) -> None:
        for b in (self._kokoro_btn, getattr(self, "_preview_btn", None) if self._play_after_download else None):
            if b is not None:
                try:
                    set_button(b, f"Downloading {pct}%", Glyph.DOWNLOAD)
                    b.setEnabled(False)
                except RuntimeError:  # the page was rebuilt
                    pass

    def _on_kokoro_done(self, msg: str) -> None:
        play, self._play_after_download = self._play_after_download, False
        try:
            if self._kokoro_btn is not None:
                if msg:
                    set_button(self._kokoro_btn, "Try the download again", Glyph.DOWNLOAD)
                    self._kokoro_btn.setEnabled(True)
                else:
                    self._kokoro_btn.hide()
            if play and msg:
                self._on_preview_done("")
        except RuntimeError:
            return
        if play and not msg:
            self._play()

    def _play(self) -> None:
        from .. import kokoro
        from ..voice import preview

        b = self._preview_btn
        vcfg = copy.deepcopy(self.draft.voice)
        lang = self.ctl.cfg.app.language  # (the sample is said in the app's language, by a voice that speaks it)
        if vcfg.engine == "kokoro" and kokoro.speaks(lang) and not kokoro.installed(language=lang):
            self._play_after_download = True  # get the voice first, then play it
            self.ctl.download_kokoro()
            self._on_kokoro_progress(self.ctl.kokoro_pct or 0)
            return
        b.setEnabled(False)
        set_button(b, "Playing…", Glyph.VOLUME)

        def work() -> None:
            try:
                v = preview(vcfg, i18n.text_in(lang, VOICE_SAMPLE), language=lang)
                msg = v.problem or ""
            except Exception as exc:
                msg = f"Couldn't play the sample: {exc}"
            self.preview_done.emit(msg)

        threading.Thread(target=work, name="doty-voice-preview", daemon=True).start()

    def _preview_button(self, d):
        b = button("Play sample", "ghost", Glyph.PLAY)
        self._preview_btn = b
        b.clicked.connect(lambda _checked=False: self._play())
        return b

    def _on_preview_done(self, msg: str) -> None:
        b = getattr(self, "_preview_btn", None)
        if b is not None:
            try:
                set_button(b, "Play sample", Glyph.PLAY)
                b.setEnabled(True)
            except RuntimeError:
                pass
        if msg:
            self.toast.show_message(msg[0].upper() + msg[1:])

    def _mic_list(self) -> list[str]:
        if self._mics is None:
            try:
                from ..listen import list_mics

                self._mics = list_mics()
            except Exception:
                self._mics = []
        return self._mics

    def _shortcuts_button(self) -> QWidget:
        b = button("Add again", "ghost", Glyph.PIN)

        def add() -> None:
            from ..shortcuts import install_shortcuts

            try:
                install_shortcuts()
            except Exception as exc:
                self.toast.show_message(tr("Couldn't add shortcuts: {error}", error=exc))
                return
            self.toast.show_message("Added to Start and your desktop. Right-click Doty's taskbar icon to pin it.")

        b.clicked.connect(lambda _checked=False: add())
        return b

    # build -------------------------------------------------------------------------
    def _build(self, keep_edits: bool = False) -> None:
        """(Re)build the page from the settings. `keep_edits`: keep what you changed but haven't saved."""
        if keep_edits and getattr(self, "draft", None) is not None:
            d = self.draft
        else:
            self._base = copy.deepcopy(self.ctl.cfg)  # what the page started from, to save only your changes
            self.draft = d = copy.deepcopy(self.ctl.cfg)
        # A rebuild (save, discard, coming back to the page) keeps your place and Advanced open if it was.
        was_at = self.scroll.verticalScrollBar().value() if self.scroll.widget() is not None else 0
        was_open = getattr(self, "_advanced", None) is not None and self._advanced.is_open
        self._cards = {}
        self._advanced = None
        self._tracked = []
        self._auto: dict[tuple[str, str], object] = {}  # filled in by the page, not by you: not a change
        self._cost = None
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 10, 10)
        lay.setSpacing(16)
        self._empty = label("", "muted", wrap=True)
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setMinimumHeight(120)
        self._empty.hide()
        lay.addWidget(self._empty)
        row = self.setting_row

        # AI coach
        card = self._new_section(lay, 0, "Judgment calls: what to do now, why you died, what to buy, and what to "
                                         "point at on screen.")
        on = self._switch(d.coach, "enabled")
        card.add(SettingsGroup("", [row(SG.POWER, "Use the AI coach", "Off: timers, alerts and draft stats only.",
                                        on)], self._color))
        card.add(SettingsGroup("Provider", self.ai.rows(d)), follows=True)
        looks = row(SG.CLOCK, "How often it looks", "More often feels more live. It also looks at calm moments: after "
                    "you respawn, after a fight, at level 6.",
                    self._combo(d.coach, "speculative_interval_s", LOOK_EVERY))
        self._cost = label("")
        self._cost.setStyleSheet(f'font-family: "{theme.DISPLAY}"; font-size: 10pt; font-weight: 700; color: {GOLD};')
        looks.texts.addWidget(self._cost)
        card.add(SettingsGroup("How it coaches", [
            looks,
            row(Glyph.BOLT, "Live looks (Claude)", "The looks it takes on its own can use a faster model: tips land "
                "sooner and cost less. Your questions and plans always use the main model.",
                self._combo(d.coach, "watch_model", [("claude-haiku-5-5", "Fast: Haiku 5.5 (recommended)"),
                                                     ("", "Same as the main model")])),
            row(Glyph.CHAT, "When it talks", "Everything also shows in the feed.",
                self._combo(d.coach, "speak_up", SPEAK_UP)),
            row(SG.EYE, "Let the coach see your screen", "Sends the Dota window with each look. Off: game data only.",
                self._switch(d.coach, "use_screenshot")),
        ]), follows=True)
        card.add(SettingsGroup("About you", [
            row(SG.CAP, "Your experience", "New: the basics out loud, plain words. Top: short calls, fewer "
                "reminders. Also on the Learn tab.", self._level_combo(d)),
            row(SG.STAR, "Your rank", "Draft help and builds use games at this rank.",
                self._combo(d.coach, "bracket", [(k, v) for k, v in BRACKETS.items()])),
        ]))
        # (not behind the main switch: what Doty takes from them is enforced by the rules too, with the AI off)
        card.add(SettingsGroup("Your instructions", [self._instructions_row(d)]))
        self._main_switch(card, on)

        # Voice
        from .. import kitten, kokoro
        from ..eleven import api_key as eleven_key

        from .. import languages

        lang = languages.known(self.ctl.cfg.app.language)
        own = languages.own_name(lang)
        voices = [("", "Windows default")] + [(v, windows_voice_name(v)) for v in self._voice_list()]
        eleven_ok = languages.eleven_speaks(lang, d.voice.eleven_model)
        kokoro_ok = kokoro.speaks(lang)
        has_key = bool(eleven_key()) and eleven_ok
        has_kokoro = kokoro_ok and kokoro.installed(language=lang)
        best = "ElevenLabs" if has_key else "Kokoro" if has_kokoro else "the Windows voice"
        card = self._new_section(lay, 1, "Kokoro and Kitten are free, sound like a real person and run on this PC. "
                                         "ElevenLabs is paid and needs a key (Setup page). The Windows voice fills "
                                         "in when none of them can speak.")
        on = self._switch(d.voice, "enabled")
        card.add(SettingsGroup("", [row(Glyph.VOLUME, "Speak advice", "Cards still show when this is off.", on)],
                               self._color))
        engine_box = self._combo(d.voice, "engine", VOICE_ENGINES)
        # Only the rows for the voice service you chose (Best available shows the one it would use).
        rows = {
            "kokoro": row(SG.PERSON, "Kokoro voice",
                          tr("Kokoro doesn't speak {language}: this voice is used when Doty's language is English.",
                             language=own) if not kokoro_ok else
                          "Free, on this PC, no account." if has_kokoro and lang != "en" else
                          "Free, on this PC, no account. Heart sounds the most natural." if has_kokoro else
                          tr("Free, on this PC, no account. One {mb} MB download.",
                             mb=kokoro.missing_mb(language=lang)),
                          self._kokoro_picker(d)),
            "kitten": row(SG.PERSON, "Kitten voice", tr("Free, on this PC, no account. Softer and darker than Kokoro, "
                                                        "a little slower.")
                          + ("" if kitten.installed() else " " + tr("One {mb} MB download.", mb=kitten.DOWNLOAD_MB)),
                          self._kitten_picker(d)),
            "tone": row(SG.SLIDERS, "Tone", "Warm removes the ringing whistle and sharp \"s\" sounds of the free voices "
                        "and plays a little quieter. Clear keeps more sparkle. Bright is the raw voice.",
                        self._combo(d.voice, "kokoro_tone", [("warm", "Warm (softest)"), ("clear", "Clear (brighter)"),
                                                             ("bright", "Bright (original)")])),
            "elevenlabs": row(SG.CLOUD, "ElevenLabs voice", "Voices on your ElevenLabs account, including ones you "
                              "add there." if has_key else "Add an ElevenLabs key on the Setup page to choose one.",
                              self._eleven_combo(d)),
            "windows": row(SG.PERSON, "Windows voice" if sys.platform == "win32" else "System voice", "",
                           self._combo(d.voice, "voice", voices)),
        }
        service = row(SG.SPEAKER, "Voice service",
                      tr("Best available tries ElevenLabs, then Kokoro, then Windows: {best} right now.", best=tr(best))
                      + ("" if has_key or has_kokoro or not kokoro_ok else
                         " " + tr("Download Kokoro below for a free natural voice.")),
                      engine_box)
        voice_group = SettingsGroup("Voice", [service, *rows.values()])
        # Which service speaks which language, by flag; yours is ringed where it's spoken.
        system = self._system_languages()

        def speaks(yes: bool) -> str:
            return " " + (tr("Speaks {language}.", language=own) if yes else
                          tr("Doesn't speak {language}.", language=own))

        order = languages.ordered()
        backup = "Windows voice" if sys.platform == "win32" else "System voice"
        spoken_group = SettingsGroup("Languages each voice speaks", [
            row(SG.PERSON, "Kokoro", tr("Free, on this PC.") + speaks(kokoro_ok),
                FlagStrip([c for c in order if kokoro.speaks(c)], lang)),
            row(SG.PERSON, "Kitten", tr("Free, on this PC.") + speaks(languages.kitten_speaks(lang)),
                FlagStrip([c for c in order if languages.kitten_speaks(c)], lang)),
            row(SG.CLOUD, "ElevenLabs", tr("Paid, from the cloud, needs a key.") + speaks(eleven_ok),
                FlagStrip(languages.eleven_languages(d.voice.eleven_model), lang)),
            row(SG.MONITOR, backup, tr("Built in and robotic. It speaks the languages whose speech is installed on "
                                       "this PC; add more in your system's speech settings.") + speaks(lang in system),
                FlagStrip([c for c in order if c in system], lang, tr("None found"))),
        ])

        def show_rows(_i: int = 0) -> None:
            engine = engine_box.currentData()
            if engine == "auto":
                engine = "elevenlabs" if has_key else "kokoro"
            wanted = {engine, "tone"} if engine in ("kokoro", "kitten") else {engine}
            for key, r in rows.items():
                r.engine_hidden = key not in wanted
                r.refresh_visibility()
            voice_group.box.update()

        engine_box.currentIndexChanged.connect(show_rows)
        show_rows()
        card.add(voice_group, follows=True)
        card.add(spoken_group, follows=True)
        card.add(SettingsGroup("Delivery", [
            row(SG.GAUGE, "Speaking speed", "", self._slider(-4, 8, d.voice.rate,
                                                             lambda v: f"{v:+d}" if v else "Normal",
                                                             lambda v: setattr(d.voice, "rate", v)),
                [("voice", "rate")]),
            row(SG.FILTER, "What to say out loud", "Everything still appears on screen.",
                self._combo(d.voice, "min_priority", [(1, "Only urgent warnings"), (2, "Urgent and important"),
                                                      (3, "Everything useful")])),
            row(SG.HEADPHONES, "Hear it", "Plays a sample line with the settings above, before you save.",
                self._preview_button(d)),
        ]), follows=True)
        self._main_switch(card, on)

        # Push-to-talk
        mics = [("", "Automatic")] + [(m, m) for m in self._mic_list()]
        match = next((m for m in self._mic_list() if d.listen.mic and d.listen.mic.lower() in m.lower()), None)
        if match:
            if d.listen.mic != match:
                self._auto[("listen", "mic")] = match
            d.listen.mic = match  # doty.toml may hold part of the name; select the full device
        card = self._new_section(lay, 2, "Hold the key, ask out loud, let go. Speech is turned into text on this PC; "
                                         "only the text goes to the coach.")
        on = self._switch(d.listen, "enabled")
        card.add(SettingsGroup("", [row(Glyph.MIC, "Push-to-talk", "Ask the coach anything while you play.", on)],
                               self._color))
        card.add(SettingsGroup("", [
            row(SG.KEYBOARD, "Key", "Dota still receives this key, so pick one it doesn't use.",
                self._combo(d.listen, "key", PTT_KEYS)),
            row(SG.HEADSET, "Microphone", "Automatic skips capture cards and stereo mix.",
                self._combo(d.listen, "mic", mics)),
            row(SG.NOTE, "Beeps", "A short tone when Doty starts and stops listening.",
                self._switch(d.listen, "earcons")),
        ]), follows=True)
        self._main_switch(card, on)

        # Overlay
        card = self._new_section(lay, 3, "Cards and the coach's drawings over Dota. They size themselves to your game "
                                         "window, from 1080p to ultrawide and 4K.")
        on = self._switch(d.overlay, "enabled")
        card.add(SettingsGroup("", [row(SG.CARDS, "Show cards over Dota", "Needs Borderless Window mode in Dota's "
                                        "video settings.", on)], self._color))
        card.add(SettingsGroup("", [
            row(SG.POINTER, "Let the coach point things out", "It can circle a hero, draw an arrow to safety or box "
                "an item right on your screen, with a pointer that shows where to look.",
                self._switch(d.overlay, "draw")),
            row(SG.GAUGE, "Live strip above the cards", "Countdowns to the next runes and objectives, and the item "
                "you're building with the gold still to pay.", self._switch(d.overlay, "strip")),
            row(SG.RESIZE, "Size", "On top of the automatic sizing.",
                self._combo(d.overlay, "size", [(0.85, "Small"), (1.0, "Normal"), (1.2, "Large"),
                                                (1.4, "Extra large")])),
            row(SG.DOCK, "Position", "Where the cards stack on your screen.",
                self._combo(d.overlay, "anchor", [("right", "Right side"), ("left", "Left side"),
                                                  ("top", "Top center")])),
        ]), follows=True)
        self._main_switch(card, on)

        # Alerts
        card = self._new_section(lay, 4, "The instant, rule-based calls. These never wait on the AI.")
        card.add(SettingsGroup("Game", [
            row(SG.GAUGE, "Game mode", "Turbo runs about twice as fast as a normal game, so item timings, buyback "
                "and Roshan's timer all move. Automatic goes by what the match shows, then by your recent matches.",
                self._combo(d.rules, "game_mode", [("auto", "Automatic"), ("normal", "Normal"), ("turbo", "Turbo")])),
        ]))
        card.add(SettingsGroup("What Doty suggests", [
            row(SG.STAR, "Items to buy", "Buy calls, item timings and the coach's item advice. Off: you pick your "
                "own items, and Doty only talks about them when you ask.", self._switch(d.suggest, "items")),
            row(SG.PERSON, "When I go my own way", "When you build something other than what Doty suggests. Once: it "
                "may say so one time per item, with a reason from this game, then follows your build.",
                self._combo(d.coach, "own_way", OWN_WAY)),
            row(SG.CAP, "Skill points", "Reminders to spend a skill point, and the level 6 call.",
                self._switch(d.suggest, "skills")),
            row(SG.CHART, "Farm and lane", "Last-hit, deny and farm pace checks.",
                self._switch(d.suggest, "farm")),
            row(SG.CLOCK, "Runes and Roshan", "Countdowns to runes, shrines, lotus pools, neutral items, Roshan "
                "and the Aegis.", self._switch(d.suggest, "timers")),
            row(SG.EYE, "Enemy timings", "The enemy's big items before they land, who the long game favours, and "
                "pick-off windows.", self._switch(d.suggest, "enemy")),
            row(SG.KEY, "Gold and TP scroll", "Unspent gold, items in the stash, buyback and a missing TP scroll.",
                self._switch(d.suggest, "gold")),
            row(SG.BULB, "Death reviews", "After each death: why it happened and what to change.",
                self._switch(d.suggest, "deaths")),
        ]))
        card.add(SettingsGroup("Timings", [
            row(SG.RUNE, "Bounty rune reminders", "A heads-up before the bounty runes spawn.",
                self._switch(d.rules, "announce_bounty_runes")),
            row(SG.STACK, "Stack timings", "For supports: a heads-up before each minute's stack.",
                self._switch(d.rules, "announce_stacks")),
            row(SG.MOON, "Day and night changes", "When the map turns to night or back to day.",
                self._switch(d.rules, "announce_day_night")),
        ]))
        card.add(SettingsGroup("Warnings", [
            row(SG.HEART, "Low HP warning at", "Doty warns you when your health drops below this.",
                self._slider(15, 50, int(round(d.rules.low_hp_pct * 100)), lambda v: f"{v}%",
                             lambda v: setattr(d.rules, "low_hp_pct", v / 100), step=5), [("rules", "low_hp_pct")]),
        ]))

        # App
        card = self._new_section(lay, 5, "How Doty opens, closes and keeps itself up to date.")
        card.add(SettingsGroup("Language", [
            row(Glyph.CHAT, "Language", "Doty's screens, alerts and AI coach, and what push-to-talk listens for. "
                "The free Kokoro voice speaks English, Spanish, Portuguese and French; the others are said by "
                "ElevenLabs or your PC's own voice (Settings > Voice shows which). Doty restarts to switch. Also "
                "the flag in the top bar.",
                self._language_combo(d), [("app", "language")]),
        ]))
        card.add(SettingsGroup("Opening and closing", [
            row(SG.GAMEPAD, "Follow the game", "Draft opens for hero selection and Coach when the match starts. Esc "
                "takes you back.", self._switch(d.app, "follow_game")),
            row(Glyph.PLAY, "Start coaching when Doty opens", "Off: Doty opens paused.",
                self._switch(d.app, "start_coaching_on_open")),
            row(SG.TRAY, "Keep running in the tray when closed", "Closing the window keeps Doty coaching. Quit from "
                "the tray icon.", self._switch(d.app, "close_to_tray")),
        ]))
        card.add(SettingsGroup("Install", [
            row(Glyph.PIN, "Start menu and desktop shortcuts", "Doty adds them the first time it opens. To pin it, "
                "right-click its taskbar icon and choose Pin to taskbar.", self._shortcuts_button()),
            row(SG.SYNC, "Updates", self._update_hint(d), self._update_button(d)),
        ]))

        # Advanced
        card = self._new_section(lay, 6, "Rarely needed. Open it if something's off.", fold=True)
        card.add(SettingsGroup("Coaching", [
            row(SG.CAMERA, "Coach without game data", "If Dota's launch option isn't set, keep coaching from "
                "screenshots while Dota is the active window.", self._switch(d.coach, "screen_only")),
            row(SG.MAP, "Minimap side", "Match Dota's HUD setting so the coach reads the right corner.",
                self._combo(d.coach, "minimap_side", [("left", "Left (default)"), ("right", "Right")])),
        ]))
        card.add(SettingsGroup("Display and data", [
            row(SG.ANTENNA, "Hide from streams and recordings", "OBS, Discord and other screen capture won't see the "
                "cards.", self._switch(d.overlay, "exclude_from_capture")),
            row(SG.CHART, "Show technical details", "Timings on the Coach page, in the feed and on each alert card: "
                "updates a second, milliseconds to each card, the speech model.",
                self._switch(d.overlay, "show_latency")),
            row(SG.VIDEO, "Capture matches", self._capture_hint(), self._capture_row(d), [("capture", "enabled")]),
        ]))

        self._tail = QWidget()  # room below the last card, so each tab's section can reach the top
        lay.addWidget(self._tail)
        lay.addStretch(1)
        if self._advanced is not None:
            if was_open:
                self._advanced.set_open(True, animate=False)
            self._advanced.toggled.connect(lambda _on: QTimer.singleShot(260, self._fit_tail))
        QTimer.singleShot(0, self._fit_tail)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._jumping = True  # (the new page starts at the top: no tab change until it's laid out)
        self.scroll.setWidget(inner)
        self._jumping = False
        if self.search.text():
            self._filter(self.search.text())
        QTimer.singleShot(0, lambda: self._settle(was_at))
        self._dirty()  # (nothing to save after a fresh build; your edits, after keep_edits)
        if not keep_edits:
            self.bar.setVisible(False)

    # updates -------------------------------------------------------------------------
    update_state = Signal(str, object)  # ("found" | "none" | "error" | "progress" | "ready", detail)

    @staticmethod
    def _update_hint(d) -> str:
        from ..config import FROZEN

        if not FROZEN:
            return "Running from the source folder: update with git."
        if not d.app.update_repo:
            return "Checks Doty's GitHub Releases for a new version. Not set up yet: Doty isn't published."
        return f"Checks {d.app.update_repo} on GitHub for a new version, and installs it when you say so."

    def _update_button(self, d) -> QWidget:
        from ..config import FROZEN

        self._update_btn = button("Check for updates", "ghost", Glyph.DOWNLOAD)
        self._update_btn.setEnabled(FROZEN and bool(d.app.update_repo))
        self._update_btn.clicked.connect(lambda _c=False: self._update_clicked(d.app.update_repo))
        self._release = None
        if not getattr(self, "_update_wired", False):
            self.update_state.connect(self._on_update_state)
            self._update_wired = True
        return self._update_btn

    def _update_clicked(self, repo: str) -> None:
        from .. import update

        release = self._release
        set_button(self._update_btn, "Downloading…" if release else "Checking…", Glyph.DOWNLOAD)
        self._update_btn.setEnabled(False)

        def work() -> None:
            try:
                if release is None:
                    found = update.check(repo)
                    self.update_state.emit("found" if found else "none", found)
                else:
                    from ..config import CACHE_DIR

                    path = update.download(release, CACHE_DIR / "updates",
                                           lambda done, total: self.update_state.emit(
                                               "progress", int(100 * done / max(1, total))))
                    self.update_state.emit("ready", path)
            except Exception as exc:
                self.update_state.emit("error", str(exc))

        threading.Thread(target=work, name="doty-update", daemon=True).start()

    def _on_update_state(self, state: str, detail) -> None:
        btn = self._update_btn
        try:
            if state == "progress":
                set_button(btn, f"Downloading… {detail}%", Glyph.DOWNLOAD)
                return
            btn.setEnabled(True)
            if state == "found":
                self._release = detail
                set_button(btn, f"Install Doty {detail.version}", Glyph.DOWNLOAD)
                self.toast.show_message(tr("Doty {version} is out. Click Install to update.", version=detail.version))
            elif state == "none":
                set_button(btn, "Check for updates", Glyph.DOWNLOAD)
                self.toast.show_message("You have the latest Doty.")
            elif state == "error":
                set_button(btn, "Check for updates", Glyph.DOWNLOAD)
                self._release = None
                self.toast.show_message(tr("Update: {detail}", detail=detail))
            elif state == "ready":
                from .. import update
                from .window import quit_doty

                self.toast.show_message("Installing the update. Doty will close and come back.")
                update.install(detail)
                QTimer.singleShot(1500, quit_doty)
        except RuntimeError:  # the page was rebuilt meanwhile
            pass

    def set_setup_attention(self, attention: int) -> None:
        """The checklist button says when something needs setting up (the Settings tab shows a dot too)."""
        set_button(self.setup_btn, tr("Setup checklist · {n} to do", n=attention) if attention else "Setup checklist",
                   SG.CHECKLIST)
        if attention:
            set_icon_color(self.setup_btn._icon, AMBER)

    @staticmethod
    def _capture_hint() -> str:
        from ..config import documents_dir

        synced = "onedrive" in str(documents_dir()).lower()
        return (tr("Saves frames of the game (only while Dota is in front) and its data, without your Steam ID or "
                   "name, to Documents\\Doty\\captures, so Doty can learn where your HUD draws things. Doty sends "
                   "none of it anywhere") + (tr(", but OneDrive backs up your Documents folder, so it syncs them too")
                                             if synced else "") +
                tr(". About 150 MB a match: turn it on for a match or two, then off. Restart Dota after turning it on."))

    def _capture_row(self, d) -> QWidget:
        from ..match_capture import capture_folder

        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        open_btn = button("Open folder", "link")

        def open_folder() -> None:
            folder = capture_folder(d.capture)
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

        open_btn.clicked.connect(lambda _c=False: open_folder())
        lay.addWidget(open_btn)
        lay.addWidget(self._switch(d.capture, "enabled"))
        return box

    def _changes(self):
        """The settings as they are now (the overlay you dragged, the Coach menu's preset...) with only the
        fields you changed on this page on top, so saving here never undoes a change made elsewhere."""
        from dataclasses import fields, is_dataclass

        merged = copy.deepcopy(self.ctl.cfg)
        for section in fields(merged):
            now, base, mine = (getattr(c, section.name) for c in (merged, self._base, self.draft))
            if not is_dataclass(now):
                continue
            for f in fields(now):
                if getattr(mine, f.name) != getattr(base, f.name):
                    setattr(now, f.name, copy.deepcopy(getattr(mine, f.name)))
        return merged

    def _save(self) -> None:
        before = getattr(self.ctl.cfg.app, "language", "en")
        try:
            changes = self._changes()
            if getattr(changes.app, "language", "en") != before:
                changes.app.language_picked = True  # (chosen here: the opening choice needn't ask)
            reread = changes.coach.instructions != self._base.coach.instructions
            self.ctl.apply_config(changes)
        except Exception as exc:
            self.toast.show_message(tr("Couldn't save: {error}", error=exc))
            return
        self.bar.setVisible(False)
        self.draft = copy.deepcopy(self.ctl.cfg)
        if reread:  # (new instructions: what can be a setting is taken out of them, then shown to untick)
            self._read_instructions(self.ctl.cfg.coach.instructions)
        self._build()
        if getattr(self.ctl.cfg.app, "language", "en") != before:  # (the screens are built in one language)
            from PySide6.QtCore import QTimer

            from .window import relaunch_doty

            self.toast.show_message("Saved. Doty is restarting in your language…")
            QTimer.singleShot(900, relaunch_doty)
            return
        self.toast.show_message("Saved." + (" Doty restarted with your new settings." if self.ctl.running else ""))
