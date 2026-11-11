"""Settings > AI coach: which AI runs the coach, connecting it, and the model.

The rows follow the provider you pick:
- **Key providers** (Claude, OpenAI, Gemini, OpenRouter): an API key field.
- **ChatGPT:** a "Continue with ChatGPT" button.
- **Ollama:** how to install a free local model.

Keys and sign-ins take effect at once; switching providers takes effect when you save.
"""

from __future__ import annotations

import asyncio
import os
import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QWidget

from ..i18n import tr
from ..coach.backends import PROVIDERS, UNAVAILABLE, OpenAIChatBackend
from . import theme
from .controller import save_api_key
from .widgets import SettingRow, button, label

CLAUDE_MODELS = [
    ("claude-sonnet-5-5", "Sonnet 5.5: smart and affordable"),
    ("claude-opus-5-5", "Opus 5.5: deepest advice, 2x the cost"),
    ("claude-haiku-5-5", "Haiku 5.5: fastest, a few cents a game"),
]
KEY_PREFIX = {"claude": "sk-ant-"}


def _combo() -> QComboBox:
    """A picker lined up with the others on the Settings page."""
    from .pages import FitCombo

    return FitCombo()


class AIRows(QObject):
    signed_in = Signal(str)  # "" = ok, else the problem
    models = Signal(str, object)  # (provider, [model ids] or an error message)

    def __init__(self, page) -> None:
        super().__init__()
        self.page = page  # the SettingsPage: dirty flag, toast, controller
        self.signed_in.connect(self._on_signed_in)
        self.models.connect(self._on_models)
        self._models: dict[str, list[str]] = {}
        self._connect_box = self._model_box = None
        self.d = None

    def _row(self, glyph: str, title: str, description: str, control: QWidget, keys=()) -> QWidget:
        """A row in the page's style (icon, words, control), or a plain SettingRow outside the Settings page."""
        make = getattr(self.page, "setting_row", None)
        return make(glyph, title, description, control, keys) if make else SettingRow(title, description, control)

    def rows(self, d) -> list[SettingRow]:
        self.d = d
        combo = _combo()
        for key, p in PROVIDERS.items():
            combo.addItem(p.label, key)
            if key == "claude":  # right under the Claude key: the plan option people look for, and why it's off
                for text, why in UNAVAILABLE:
                    combo.addItem(text, None)
                    item = combo.model().item(combo.count() - 1)
                    item.setEnabled(False)
                    item.setToolTip(why)
        combo.setCurrentIndex(max(0, combo.findData(d.coach.provider)))
        combo.currentIndexChanged.connect(lambda _i: self._provider_changed(combo.currentData()))
        self._connect_box, self._model_box = QWidget(), QWidget()
        for box in (self._connect_box, self._model_box):
            lay = QHBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
        combo.fit_list()
        provider = self._row("", "AI provider", "Claude with an API key, your ChatGPT plan, OpenAI, "
                             "Gemini or OpenRouter (free tiers), or a free model on this PC. Claude Pro and Max plans "
                             "can't be used by other apps (Anthropic's rule), so Claude needs an API key.", combo,
                             [("coach", "provider")])
        self._connect_row = self._row("", "Connect", "", self._connect_box)  # (a key)
        self._model_row = self._row("", "Model", "", self._model_box,  # (a light bulb)
                                    [("coach", "model"), ("coach", "provider_model")])
        self._fill()
        return [provider, self._connect_row, self._model_row]

    # building -------------------------------------------------------------------------
    def _provider_changed(self, key: str) -> None:
        self.d.coach.provider = key
        self.d.coach.provider_model = ""
        self.page._dirty()
        self._fill()

    @staticmethod
    def _reset(box: QWidget) -> QHBoxLayout:
        lay = box.layout()
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        return lay

    def _set_hint(self, row: SettingRow, text: str) -> None:
        hint = getattr(row, "_hint", None)
        if hint is None:
            hint = label("", "muted", wrap=True)
            hint.setStyleSheet("font-size: 9.5pt;")
            (getattr(row, "texts", None) or row.layout().itemAt(0).layout()).addWidget(hint)
            row._hint = hint
        hint.setText(text)
        hint.setVisible(bool(text))

    def _fill(self) -> None:
        key = self.d.coach.provider
        p = PROVIDERS.get(key, PROVIDERS["claude"])
        lay = self._reset(self._connect_box)
        if key == "chatgpt":
            from ..coach import chatgpt_auth

            email = chatgpt_auth.signed_in()
            if email:
                lay.addWidget(label(tr("Signed in as {email}", email=email), "muted"))
                out = button("Sign out", "ghost")
                out.clicked.connect(self._sign_out)
                lay.addWidget(out)
            else:
                b = button("Continue with ChatGPT", "primary")
                b.clicked.connect(lambda _c=False, b=b: self._sign_in(b))
                lay.addWidget(b)
            self._set_hint(self._connect_row, "Uses your ChatGPT Plus or Pro plan through OpenAI's official sign-in; "
                                              "your browser opens OpenAI's page. Usage counts against your plan's "
                                              "limits, and you can cap it in ChatGPT's settings.")
        elif key == "ollama":
            get = button("Get Ollama", "link")
            get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(p.keys_url)))
            check = button("Check", "ghost")
            check.clicked.connect(lambda: self._load_models(force=True))
            lay.addWidget(get)
            lay.addWidget(check)
            self._set_hint(self._connect_row, "Free and private: the model runs on this PC. Install Ollama, then pull "
                                              "a model that can see images, e.g. run: ollama pull gemma3. A strong "
                                              "GPU helps a lot.")
        else:
            saved = bool(os.environ.get(p.key_env, "").strip())
            field = QLineEdit()
            field.setEchoMode(QLineEdit.Password)
            field.setPlaceholderText("Key saved; paste a new one to replace it" if saved else "Paste your API key")
            field.setMinimumWidth(260)
            save = button("Save key", "ghost", theme.Glyph.CHECK)
            get = button("Get a key", "link")
            get.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(p.keys_url)))
            save.clicked.connect(lambda: self._save_key(field))
            field.returnPressed.connect(lambda: self._save_key(field))
            for w in (field, save, get):
                lay.addWidget(w)
            notes = {"gemini": "Gemini's free tier needs no card; Google may use free-tier data to improve its "
                               "products.", "openrouter": "Models ending in :free cost nothing (rate-limited).",
                     "claude": "Claude Max and Ultra plans include API credits you can use here."}
            free = notes.get(key, "")
            self._set_hint(self._connect_row, tr("Saved to your Windows user account on this PC.")
                           + (" " + tr(free) if free else ""))
        self._fill_models()

    def _fill_models(self) -> None:
        key = self.d.coach.provider
        lay = self._reset(self._model_box)
        box = _combo()
        if key == "claude":
            for value, text in CLAUDE_MODELS:
                box.addItem(text, value)
            box.setCurrentIndex(max(0, box.findData(self.d.coach.model)))
            box.currentIndexChanged.connect(lambda _i: (setattr(self.d.coach, "model", box.currentData()),
                                                        self.page._dirty()))
            self._set_hint(self._model_row, "Sonnet is the default. Opus thinks deeper for twice the price; Haiku is "
                                            "the quickest and cheapest.")
        else:
            box.setEditable(True)
            box.addItem("Automatic (a fast model that can see images)", "")
            for m in self._models.get(key, []):
                box.addItem(m, m)
            current = self.d.coach.provider_model
            if current and box.findData(current) < 0:
                box.addItem(current, current)
            box.setCurrentIndex(max(0, box.findData(current)))
            box.currentIndexChanged.connect(lambda _i: (setattr(self.d.coach, "provider_model", box.currentData() or ""),
                                                        self.page._dirty()))
            def typed() -> None:
                text = box.currentText().strip()
                self.d.coach.provider_model = "" if text == box.itemText(0) else text
                self.page._dirty()

            box.lineEdit().editingFinished.connect(typed)
            self._set_hint(self._model_row, "Pick one that understands images: the coach looks at your screen.")
            if key not in self._models:
                self._load_models()
        box.fit_list()
        lay.addWidget(box)

    # actions ----------------------------------------------------------------------------
    def _load_models(self, force: bool = False) -> None:
        key = self.d.coach.provider
        if key == "claude" or (key in self._models and not force):
            return
        p = PROVIDERS[key]
        token = None
        if key == "chatgpt":
            from ..coach import chatgpt_auth

            if not chatgpt_auth.signed_in():
                return
            token = chatgpt_auth.access_token
        api_key = os.environ.get(p.key_env, "").strip() if p.key_env else ""
        if p.key_env and not api_key:
            return
        base = self.d.coach.ollama_url if key == "ollama" else ""

        def work() -> None:
            async def go():
                if token is not None:
                    from ..coach.backends import ChatGPTBackend

                    b = ChatGPTBackend(p, "", token_source=token)
                else:
                    b = OpenAIChatBackend(p, "", api_key, base)
                try:
                    return sorted(m for m in await b.list_models() if m)
                finally:
                    await b.aclose()

            try:
                self.models.emit(key, asyncio.run(go()))
            except Exception as exc:
                self.models.emit(key, str(exc))

        threading.Thread(target=work, name="doty-models", daemon=True).start()

    def _on_models(self, key: str, found) -> None:
        if isinstance(found, str):
            if key == self.d.coach.provider:
                self.page.toast.show_message(found[0].upper() + found[1:])
            return
        self._models[key] = found
        if key == self.d.coach.provider:
            try:
                self._fill_models()
                if key == "ollama":
                    self.page.toast.show_message(tr("Ollama is running with {n} model(s).", n=len(found)) if found else
                                                 "Ollama is running but has no models: run ollama pull gemma3")
            except RuntimeError:
                pass  # the page was rebuilt

    def _save_key(self, field: QLineEdit) -> None:
        key = self.d.coach.provider
        p = PROVIDERS[key]
        value = field.text().strip()
        if len(value) < 12 or " " in value or not value.startswith(KEY_PREFIX.get(key, "")):
            self.page.toast.show_message(tr("That doesn't look like a {name} API key.", name=p.name))
            return
        save_api_key(value, p.key_env)
        field.clear()
        self._models.pop(key, None)
        ctl = self.page.ctl
        self.page.toast.show_message("Key saved." + (" Restarting the coach…" if ctl.running and
                                                     ctl.cfg.coach.provider == key else ""))
        if ctl.running and ctl.cfg.coach.provider == key:
            ctl.restart()
        self._fill()

    def _sign_in(self, b) -> None:
        from ..coach import chatgpt_auth

        b.setEnabled(False)

        def work() -> None:
            try:
                chatgpt_auth.sign_in()
                self.signed_in.emit("")
            except chatgpt_auth.SignInError as exc:
                self.signed_in.emit(str(exc))
            except Exception as exc:
                self.signed_in.emit(f"sign-in failed ({exc.__class__.__name__})")

        threading.Thread(target=work, name="doty-chatgpt-sign-in", daemon=True).start()
        self.page.toast.show_message("Finish signing in in your browser…", 6000)

    def _on_signed_in(self, problem: str) -> None:
        ctl = self.page.ctl
        if problem:
            self.page.toast.show_message(problem[0].upper() + problem[1:])
        else:
            self.page.toast.show_message("Signed in to ChatGPT." + (" Restarting the coach…" if ctl.running and
                                                                   ctl.cfg.coach.provider == "chatgpt" else
                                                                   " Save to switch the coach to it."))
            if ctl.running and ctl.cfg.coach.provider == "chatgpt":
                ctl.restart()
        try:
            self._fill()
        except RuntimeError:
            pass

    def _sign_out(self) -> None:
        from ..coach import chatgpt_auth

        chatgpt_auth.sign_out()
        self.page.toast.show_message("Signed out of ChatGPT.")
        self._fill()
