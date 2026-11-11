"""Choosing a language: the six offered first, the flags, the first-run choice, and which voices speak which
language (Kokoro's Spanish, Portuguese and French voices; ElevenLabs; the system's own voice)."""

import os
import time

import pytest

from doty import i18n, kokoro, languages
from doty.config import Config, VoiceConfig, load_config
from doty.voice import Voice, kokoro_voice, local_speaks

try:
    import espeakng_loader  # noqa: F401

    HAVE_ESPEAK = True
except ImportError:
    HAVE_ESPEAK = False


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from doty.ui import theme

    app = QApplication.instance() or QApplication([])
    theme.apply(app)
    return app


# the list --------------------------------------------------------------------------


def test_six_languages_are_offered_first_and_all_sixteen_once():
    from doty.coach import prompts

    assert languages.FEATURED == ("en", "ru", "zh", "es", "pt", "id") and languages.FEATURED[0] == "en"
    order = languages.ordered()
    assert order[:6] == list(languages.FEATURED) and sorted(order) == sorted(languages.LANGUAGES) and len(order) == 16
    assert prompts.LANGUAGES is languages.LANGUAGES  # (one list: the coach's and the app's)
    assert languages.own_name("pt") == "Português (Brasil)" and languages.english_name("zh") == "Simplified Chinese"
    assert languages.known("RU ") == "ru" and languages.known("xx") == "en" and languages.known("") == "en"
    assert languages.known("pt-BR") == "pt" and languages.known("EN") == "en"  # (typed by hand in doty.toml)


def test_a_hand_typed_language_is_the_same_one_everywhere(tmp_path):
    path = tmp_path / "doty.toml"
    path.write_text('[app]\nlanguage = "PT-br"\n', encoding="utf-8")
    assert load_config(path).app.language == "pt"  # (the screens, the coach and the voices all get "pt")
    path.write_text('[app]\nlanguage = "klingon"\n', encoding="utf-8")
    assert load_config(path).app.language == "en"


def test_the_pcs_own_language_is_the_one_preselected():
    for name, code in (("pt_BR", "pt"), ("zh-CN", "zh"), ("ru", "ru"), ("es-419", "es"), ("tl_PH", "fil"),
                       ("fil-PH", "fil"), ("id_ID", "id"), ("en-US", "en"), ("sv_SE", "en"), ("", "en")):
        assert languages.from_locale(name) == code, name


def test_which_voice_services_speak_which_language():
    assert [c for c in languages.ordered() if languages.kokoro_speaks(c)] == ["en", "es", "pt", "fr"]
    assert languages.kitten_speaks("en") and not languages.kitten_speaks("es")
    # ElevenLabs' fast model (Doty's default) speaks all of them but Thai; v3 has Thai; Multilingual v2 lacks
    # Vietnamese too; the old English-only models speak English.
    assert [c for c in languages.LANGUAGES if not languages.eleven_speaks(c)] == ["th"]
    assert languages.eleven_speaks("th", "eleven_v3") and not languages.eleven_speaks("vi", "eleven_multilingual_v2")
    assert languages.eleven_speaks("en", "eleven_turbo_v2") and not languages.eleven_speaks("es", "eleven_turbo_v2")
    assert languages.eleven_speaks("ru", "a-model-from-next-year")  # unknown: not held back
    assert len(languages.eleven_languages()) == 15
    assert set(languages.KOKORO) == {"en", *kokoro.LANG_VOICES}  # the list the labels use is what Kokoro has


# Kokoro in Spanish, Portuguese and French ----------------------------------------------


def test_kokoro_has_voices_for_three_more_languages():
    assert set(kokoro.LANG_VOICES) == {"es", "pt", "fr"}
    assert kokoro.speaks("en") and kokoro.speaks("pt") and not kokoro.speaks("ru") and not kokoro.speaks("zh")
    assert kokoro.voices_for("es") == kokoro.LANG_VOICES["es"] and kokoro.voices_for("ru") is kokoro.VOICES
    assert kokoro.default_voice("es") == "ef_dora" and kokoro.default_voice("fr") == "ff_siwis"
    assert kokoro.default_voice("ru") == kokoro.DEFAULT_VOICE == kokoro.default_voice()
    assert kokoro.voice_language("pm_alex") == "pt" and kokoro.voice_language("af_heart") == "en"
    assert kokoro.voice_language("nobody") == "en"
    assert kokoro.voice_label("ef_dora") == "Dora" and kokoro.voice_label("af_heart") == "Heart"
    for voices in kokoro.LANG_VOICES.values():
        for vid, (label, sha) in voices.items():
            assert ":" in label and len(sha) == 64 and vid not in kokoro.VOICES


def test_an_english_install_needs_nothing_new_and_a_language_adds_only_its_voices(tmp_path):
    for _src, name, size, _sha in kokoro.FILES:  # an install from before the languages
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)
    assert kokoro.installed(tmp_path) and kokoro.missing_mb(tmp_path) == 0
    assert not kokoro.installed(tmp_path, "es") and kokoro.installed(tmp_path, "ru")  # (ru: nothing to add)
    missing = [f[1] for f in kokoro.missing_files(tmp_path, "es")]
    assert missing == ["voices/ef_dora.bin", "voices/em_alex.bin", "voices/em_santa.bin"]
    assert kokoro.missing_mb(tmp_path, "es") == 2 and kokoro.missing_mb(tmp_path, "fr") == 1
    k = kokoro.Kokoro(folder=tmp_path)
    assert k.installed() and not k.has_language("es") and not k.has_language("ru")
    for name in missing:
        with open(tmp_path / name, "wb") as fh:
            fh.truncate(kokoro.VOICE_BYTES)
    assert k.has_language("es") and not k.has_language("pt")
    assert kokoro.missing_mb(tmp_path / "empty", "es") > kokoro.DOWNLOAD_MB  # a new install: all of it


def test_another_languages_text_isnt_rewritten_the_english_way():
    assert kokoro.normalize("at 4:00 with 1.5k") == "at 4 minutes with 1 point 5 thousand"
    # Thousands as Doty writes them, game time as two numbers, and a decimal the way these languages say it.
    assert kokoro.normalize_plain("a las 4:30 con 4,250 de oro, 2.5 y 50%") == "a las 4 30 con 4250 de oro, 2,5 y 50%"
    # eSpeak's language-switch flags (around an English name in a French line) and its ties are dropped.
    assert kokoro._to_kokoro_plain("lœʁ (e^n)kˈaɹi(f^r) t^ʃa-o") == "lœʁ kˈaɹi ʧao"


@pytest.mark.skipif(not HAVE_ESPEAK, reason="espeakng-loader not installed")
def test_each_language_is_read_by_its_own_espeak_voice():
    spanish = kokoro.phonemize("¡Retrocede, poca vida!", language="es")
    assert "θ" in spanish and "ð" in spanish and spanish.endswith("!")  # Spanish sounds, punctuation kept
    french = kokoro.phonemize("Leur carry a disparu de la carte.", language="fr")
    assert "ʁ" in french and "(" not in french
    portuguese = kokoro.phonemize("Não lute sozinho.", language="pt")
    assert "ɐ̃" in portuguese
    assert kokoro.phonemize("Back off, low HP.") == kokoro.phonemize("Back off, low HP.", language="en")  # as before
    assert kokoro.phonemize("Back off") != kokoro.phonemize("Back off", language="es")


# the voice ---------------------------------------------------------------------------


class FakeKokoro:
    model_id, device = "fake", "CPU"

    def __init__(self, languages_here=("en",)) -> None:
        self.here, self.loaded, self.calls = languages_here, False, []

    def installed(self) -> bool:
        return True

    def has_language(self, language: str) -> bool:
        return language in self.here

    def load(self) -> None:
        self.loaded = True

    def close(self) -> None:
        pass

    def synth(self, text, voice="af_heart", speed=1.0, stop=None, tone="warm"):
        self.calls.append((text, voice))
        yield b"\x01\x00" * 100


class FakePlayer:
    pending_s = 0.0

    def __init__(self) -> None:
        self.audio = bytearray()

    def write(self, pcm, volume=1.0) -> None:
        self.audio += pcm

    def clear(self) -> None:
        pass

    def wait(self, interrupted) -> None:
        pass

    def close(self) -> None:
        pass


class FakeSapi:
    def __init__(self, speaks_language=True) -> None:
        self.speaks_language, self.said = speaks_language, []

    def speak(self, text, purge) -> None:
        self.said.append(text)

    def purge(self) -> None:
        pass

    def wait(self, interrupted) -> None:
        pass


def _voice(tmp_path, language, fake=None, **cfg):
    from doty.eleven import PhraseCache

    made = []
    fake = fake or FakeKokoro()
    v = Voice(VoiceConfig(**cfg), player_factory=FakePlayer, cache=PhraseCache(tmp_path / "voice"),
              kokoro_factory=lambda: made.append(1) or fake)
    v.language = language
    return v, fake, made


def test_your_kokoro_voice_follows_the_apps_language():
    cfg = VoiceConfig(kokoro_voice="am_michael")
    assert kokoro_voice(cfg) == kokoro_voice(cfg, "en") == "am_michael"
    assert kokoro_voice(cfg, "es") == "ef_dora" and kokoro_voice(cfg, "fr") == "ff_siwis"
    cfg.kokoro_lang_voice = "em_alex"
    assert kokoro_voice(cfg, "es") == "em_alex"
    assert kokoro_voice(cfg, "pt") == "pf_dora"  # (a Spanish voice isn't a Portuguese one: that language's first)
    assert kokoro_voice(cfg, "ru") == "am_michael"  # (not spoken by Kokoro: nothing changes)
    assert local_speaks(cfg, "es") and not local_speaks(cfg, "ru")
    assert local_speaks(VoiceConfig(engine="kitten"), "en") and not local_speaks(VoiceConfig(engine="kitten"), "es")


def test_kokoro_speaks_spanish_with_a_spanish_voice(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    v, fake, _made = _voice(tmp_path, "es", FakeKokoro(("en", "es")), engine="kokoro", kokoro_lang_voice="em_santa")
    assert v._setup_kokoro() == "" and v.engine == "kokoro" and v.voice_name == "Santa" and fake.loaded
    v._speak("Retrocede, poca vida", False)
    assert fake.calls == [("Retrocede, poca vida", "em_santa")]
    v2, _fake, _ = _voice(tmp_path, "es", FakeKokoro(("en", "es")), engine="kokoro")
    v2._setup_kokoro()
    assert v._kokoro_key("hola")[0] != v2._kokoro_key("hola")[0]  # (a line saved in one voice isn't another's)


def test_kokoro_isnt_loaded_for_a_language_it_cant_speak(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    v, fake, made = _voice(tmp_path, "ru", engine="kokoro")
    v._setup()
    assert not made and not fake.loaded and v.engine == "windows"
    assert "Kokoro doesn't speak Russian" in v.problem
    # Best available says nothing about it (there was never a choice to explain).
    v, _fake, made = _voice(tmp_path, "ru", engine="auto")
    v._setup()
    assert not made and v.problem == ""
    # Its voices for your language not downloaded yet: said plainly.
    v, fake, _made = _voice(tmp_path, "es", FakeKokoro(("en",)), engine="kokoro")
    v._setup()
    assert not fake.loaded and "Spanish voices aren't downloaded yet" in v.problem
    # Kitten is English only.
    v, _fake, made = _voice(tmp_path, "es", engine="kitten")
    v._setup()
    assert not made and "Kitten doesn't speak Spanish" in v.problem


def test_elevenlabs_isnt_asked_to_speak_thai(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    v, _fake, _made = _voice(tmp_path, "th", engine="auto")
    monkeypatch.setattr(v, "_setup_eleven", lambda: pytest.fail("ElevenLabs was set up for Thai"))
    v._setup()
    assert v.engine == "windows" and "doesn't speak Thai" in v.problem and v._client is None


def test_the_windows_voice_stays_quiet_without_a_voice_for_your_language(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    v, _fake, _made = _voice(tmp_path, "ru", engine="windows")
    v._sapi = FakeSapi(speaks_language=False)
    v._speak("Назад, мало здоровья", False)
    assert v._sapi.said == [] and "no voice on this PC speaks Russian" in v.problem
    v._sapi = FakeSapi(speaks_language=True)
    v._speak("Назад, мало здоровья", False)
    assert v._sapi.said == ["Назад, мало здоровья"]


def test_with_no_voice_for_your_language_the_status_says_so_from_the_start(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # Russian, Best available, no Russian Windows voice: known at start-up, not at the first alert of a match.
    v, _fake, _made = _voice(tmp_path, "ru", engine="auto")
    v._sapi = FakeSapi(speaks_language=False)
    v._setup()
    v._note_if_nothing_speaks()
    assert v.problem.startswith("Kokoro doesn't speak Russian; no voice on this PC speaks Russian")
    assert "or an ElevenLabs key" in v.problem
    # Spanish with Kokoro here but its Spanish voices not yet: the download is what's named, not lost.
    v, _fake, _made = _voice(tmp_path, "es", FakeKokoro(("en",)), engine="auto")
    v._sapi = FakeSapi(speaks_language=False)
    v._setup()
    v._note_if_nothing_speaks()
    assert "Spanish voices aren't downloaded yet" in v.problem and "no voice on this PC speaks Spanish" in v.problem
    once = v.problem
    v._speak("Retrocede", False)  # (a line that can't be said doesn't rewrite the reason)
    assert v.problem == once and v._sapi.said == []
    # A voice that does speak it: nothing to say.
    v, _fake, _made = _voice(tmp_path, "ru", engine="auto")
    v._sapi = FakeSapi(speaks_language=True)
    v._setup()
    v._note_if_nothing_speaks()
    assert v.problem == ""
    # Thai with a key: ElevenLabs' fast model can't, so a key isn't what's suggested.
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    v, _fake, _made = _voice(tmp_path, "th", engine="auto")
    v._sapi = FakeSapi(speaks_language=False)
    v._setup()
    v._note_if_nothing_speaks()
    assert "doesn't speak Thai; no voice on this PC speaks Thai" in v.problem and "ElevenLabs key" not in v.problem


def test_a_free_voice_without_your_language_gives_way_to_elevenlabs_when_theres_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test")
    v, fake, made = _voice(tmp_path, "ru", engine="kokoro")

    def eleven() -> str:
        v.engine = "elevenlabs"
        return ""

    monkeypatch.setattr(v, "_setup_eleven", eleven)
    v._setup()
    assert v.engine == "elevenlabs" and not made and v.problem == "Kokoro doesn't speak Russian; using ElevenLabs"
    # In a language Kokoro has, your choice of Kokoro stands.
    v, fake, _made = _voice(tmp_path, "es", FakeKokoro(("en", "es")), engine="kokoro")
    monkeypatch.setattr(v, "_setup_eleven", lambda: pytest.fail("ElevenLabs took over a voice that can speak"))
    v._setup()
    assert v.engine == "kokoro" and v.problem == ""
    # Kitten in Spanish: the reason says who does speak it.
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    v, _fake, _made = _voice(tmp_path, "es", engine="kitten")
    v._setup()
    assert "Kitten doesn't speak Spanish (Kokoro does: Settings > Voice)" in v.problem


@pytest.mark.skipif(os.name != "nt", reason="the Windows voice")
def test_the_windows_voice_finds_a_speech_packs_voice_for_your_language(monkeypatch):
    import win32com.client

    from doty import voice

    class Token:
        def __init__(self, name, lcid) -> None:
            self.name, self.lcid = name, lcid

        def GetDescription(self):  # noqa: N802
            return self.name

        def GetAttribute(self, _name):  # noqa: N802
            return self.lcid

    class Tokens(list):
        @property
        def Count(self):  # noqa: N802
            return len(self)

        def Item(self, i):  # noqa: N802
            return self[i]

    desktop = Tokens([Token("Microsoft David Desktop - English (United States)", "409"),
                      Token("Microsoft Zira Desktop - English (United States)", "409")])
    onecore = Tokens([Token("Microsoft David - English (United States)", "409"),
                      Token("Microsoft Andika - Indonesian (Indonesia)", "421")])

    class Sp:
        Rate = Volume = 0
        Voice = None

        def GetVoices(self):  # noqa: N802
            return desktop

        def Speak(self, text, flags):  # noqa: N802
            pass

    class Category:
        def SetId(self, key, create):  # noqa: N802
            assert "Speech_OneCore" in key

        def EnumerateTokens(self):  # noqa: N802
            return onecore

    monkeypatch.setattr(win32com.client, "Dispatch", lambda name: Sp() if name == "SAPI.SpVoice" else Category())
    # Indonesian has no desktop voice: the one its speech pack installs is used.
    s = voice.SapiSpeaker(VoiceConfig(), "id")
    assert s.speaks_language and s.sp.Voice is onecore[1]
    s = voice.SapiSpeaker(VoiceConfig(), "ru")
    assert not s.speaks_language and s.sp.Voice is None  # no Russian voice: Doty won't have an English one read it
    s = voice.SapiSpeaker(VoiceConfig(voice="zira"), "fil")
    assert s.speaks_language and s.sp.Voice is desktop[1]  # Filipino: your English voice reads it
    s = voice.SapiSpeaker(VoiceConfig(voice="zira"), "en")
    assert s.speaks_language and s.sp.Voice is desktop[1]
    assert voice.system_languages() == {"en", "id", "fil"}


def test_the_sample_line_is_said_in_the_language_being_tried():
    from doty.eleven import SAMPLE_LINE
    from doty.ui.pages import VOICE_SAMPLE

    assert VOICE_SAMPLE == SAMPLE_LINE
    spanish = i18n.text_in("es", VOICE_SAMPLE)
    assert spanish != VOICE_SAMPLE and "quince" in spanish  # (numbers in words: a voice reads it)
    assert i18n.text_in("en", VOICE_SAMPLE) == VOICE_SAMPLE == i18n.text_in("xx", VOICE_SAMPLE)
    assert i18n.text_in("es", "Language: {name}", name="Español") == "Idioma: Español"


# settings ----------------------------------------------------------------------------


def test_the_choice_is_saved_and_only_offered_once(tmp_path, monkeypatch):
    from doty.ui import language_dialog as ld

    monkeypatch.delenv(ld.PROMPT_ENV, raising=False)
    cfg = Config()
    assert ld.first_run_wanted(cfg)  # your real settings, no language chosen yet
    monkeypatch.setenv(ld.PROMPT_ENV, "0")
    assert not ld.first_run_wanted(cfg)
    monkeypatch.delenv(ld.PROMPT_ENV)
    cfg.app.language = "ru"
    assert not ld.first_run_wanted(cfg)  # (you chose one in Settings before this existed)
    cfg.app.language, cfg.app.language_picked = "en", True
    assert not ld.first_run_wanted(cfg)
    throwaway = Config()
    throwaway.source_path = tmp_path / "doty.toml"
    assert not ld.first_run_wanted(throwaway)  # a test's or a preview's settings: never
    unread = Config()
    unread.load_problem = "Doty couldn't read doty.toml"
    assert not ld.first_run_wanted(unread)  # (its defaults aren't a new install; and nothing saves over the file)

    class Picked:
        chosen = "pt"

        def __init__(self, current) -> None:
            Picked.offered = current

        def exec(self) -> None:
            pass

    monkeypatch.setattr(ld, "LanguageDialog", Picked)
    monkeypatch.setattr(ld, "system_guess", lambda: "es")
    ld.ask_at_first_run(throwaway)
    assert Picked.offered == "es" and throwaway.app.language == "pt" and throwaway.app.language_picked
    saved = load_config(throwaway.source_path)
    assert saved.app.language == "pt" and saved.app.language_picked
    Picked.chosen = None  # closed without choosing: English stays, and it doesn't ask again
    closed = Config()
    closed.source_path = tmp_path / "closed.toml"
    ld.ask_at_first_run(closed)
    assert closed.app.language == "en" and closed.app.language_picked


def test_the_voice_line_says_who_speaks_a_language():
    from doty.ui import language_dialog as ld

    assert ld.voice_line("es", set()) == ld.VOICE_FREE == ld.voice_line("en", {"en"})
    assert ld.voice_line("ru", {"en", "ru"}) == ld.VOICE_SYSTEM
    assert ld.voice_line("ru", {"en"}) == ld.VOICE_NEEDS
    assert ld.voice_line("th", {"en", "th"}) == ld.VOICE_SYSTEM_ONLY  # (ElevenLabs' fast model has no Thai)
    assert ld.voice_line("th", {"en"}) == ld.VOICE_NEEDS_SYSTEM


# screens -----------------------------------------------------------------------------


def test_every_language_has_a_flag(qapp):
    from doty.ui.flags import flag_icon, flag_pixmap, has_flag

    seen = set()
    for code in languages.LANGUAGES:
        assert has_flag(code), code
        pm = flag_pixmap(code, 20, 1.0)
        assert (pm.width(), pm.height()) == (30, 20)
        image = pm.toImage()
        seen.add(bytes(image.constBits()))
        assert not flag_icon(code).isNull()
    assert len(seen) == 16  # sixteen different pictures
    assert flag_pixmap("ru", 20, 1.0) is flag_pixmap("ru", 20, 1.0)  # (drawn once)
    assert not has_flag("xx") and flag_pixmap("xx", 20, 1.0).width() == 30


def test_the_language_choice_shows_six_then_the_rest_in_their_own_words(qapp):
    from doty.ui import language_dialog as ld

    dlg = ld.LanguageDialog("en", system={"en"})
    assert list(dlg.tiles) == languages.ordered()
    assert [t.big for t in dlg.tiles.values()] == [True] * 6 + [False] * 10
    assert dlg.selected() == "en" and dlg.rest.isHidden() and dlg.title.text() == ld.TITLE
    assert dlg.voice.text() == ld.VOICE_FREE
    narrow = dlg.ok.minimumWidth()
    dlg.tiles["ru"].setChecked(True)  # the words follow the language you point at
    assert dlg.title.text() == "Выбери язык" and dlg.ok._text.text() == "Продолжить"
    assert dlg.ok.minimumWidth() > narrow  # (the button grows with its word)
    assert dlg.voice.text() == i18n.text_in("ru", ld.VOICE_NEEDS) != ld.VOICE_NEEDS
    dlg.tiles["es"].setChecked(True)
    assert dlg.title.text() == "Elige tu idioma" and dlg.voice.text() == i18n.text_in("es", ld.VOICE_FREE)
    dlg._show_more(True)
    assert not dlg.rest.isHidden()
    assert dlg.chosen is None
    dlg._accept()
    assert dlg.chosen == "es"
    # A language outside the six opens with the rest showing; a double-click chooses and goes on.
    other = ld.LanguageDialog("th", system={"en"})
    assert other.selected() == "th" and not other.rest.isHidden()
    other.tiles["ja"].mouseDoubleClickEvent(None)
    assert other.chosen == "ja"
    assert ld.LanguageDialog("xx", system=set()).selected() == "en"


def test_the_choice_reads_right_inside_an_app_in_another_language(qapp):
    """In a Spanish app, pointing at English shows English (the app's own translation doesn't put it back)."""
    from doty.ui import language_dialog as ld

    i18n.set_language("es")
    i18n.install_qt()  # (Qt's text setters translate from here on, as in a Spanish app)
    try:
        assert i18n.tr("Continue") == "Continuar"
        with i18n.as_is():
            assert i18n.tr("Continue") == "Continue"
        assert i18n.tr("Continue") == "Continuar"
        dlg = ld.LanguageDialog("es", system={"en"})
        assert dlg.title.text() == "Elige tu idioma"
        dlg.tiles["en"].setChecked(True)
        assert dlg.title.text() == ld.TITLE and dlg.voice.text() == ld.VOICE_FREE
        assert dlg.ok._text.text() == ld.CONTINUE and dlg.more_btn.text() == ld.MORE
        # Another thread (the rules, the voice) still gets its lines in the app's language meanwhile.
        import threading

        got = []
        with i18n.as_is():
            t = threading.Thread(target=lambda: got.append(i18n.tr("Continue")))
            t.start()
            t.join()
        assert got == ["Continuar"]
    finally:
        i18n.set_language("en")


def test_the_flag_strip_fits_many_languages_on_two_lines(qapp):
    from doty.ui.pages import FlagStrip

    one = FlagStrip(["en"], "en")
    many = FlagStrip(languages.eleven_languages(), "es")
    none = FlagStrip([], "ru", "None found")
    assert many.height() > one.height() == none.height() and one.width() == many.width()
    assert "Español" in many.toolTip() and "ไทย" not in many.toolTip()
    for strip in (one, many, none):
        assert not strip.grab().isNull()


def test_settings_show_the_language_by_flag_and_which_voice_speaks_it(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel

    from doty.ui.controller import Controller
    from doty.ui.pages import FlagStrip
    from doty.ui.window import MainWindow

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = Config()
    cfg.gsi.port = 53317
    cfg.voice.enabled = cfg.coach.enabled = cfg.listen.enabled = cfg.overlay.enabled = cfg.hotkeys.enabled = False
    cfg.source_path = tmp_path / "doty.toml"
    cfg.app.language = "es"
    ctl = Controller(cfg)
    win = MainWindow(ctl)
    end = time.time() + 20
    while win.setup_page._busy and time.time() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert not win.lang_btn.icon().isNull() and "Español" in win.lang_btn.toolTip()
    page = win.settings_page
    page._system_langs = {"en"}  # (this PC's own voices aren't the test's business)
    page._build()
    assert page._pending() == []  # building the page isn't a change (the line in the language list included)
    box = next(c for c in page.findChildren(type(page._language_combo(page.draft))) if c.findData("fil") >= 0
               and c.findData("ru") >= 0 and c.parent() is not None)
    codes = [box.itemData(i) for i in range(box.count()) if box.itemData(i)]
    assert codes == languages.ordered() and box.currentData() == "es"
    assert box.itemText(box.findData("zh")) == "简体中文" and not box.itemIcon(box.findData("zh")).isNull()
    strips = page.findChildren(FlagStrip)
    assert [s.codes for s in strips] == [["en", "es", "pt", "fr"], ["en"], languages.eleven_languages(), ["en"]]
    assert all(s.current == "es" for s in strips)
    words = " ".join(lab.text() for lab in page.findChildren(QLabel))
    assert "Languages each voice speaks".upper() in words.upper()
    assert words.count("Speaks Español.") == 2 and words.count("Doesn't speak Español.") == 2
    # Kokoro's picker offers the Spanish voices, and saves the pick apart from the English one.
    from PySide6.QtWidgets import QComboBox

    voice_box = next(c for c in page.findChildren(QComboBox) if c.findData("em_alex") >= 0)
    assert voice_box.findData("af_heart") < 0 and voice_box.currentData() == "ef_dora"
    voice_box.setCurrentIndex(voice_box.findData("em_alex"))
    assert page.draft.voice.kokoro_lang_voice == "em_alex" and page.draft.voice.kokoro_voice == "af_heart"
    # Get started's voice step: Kokoro isn't here yet, and the download is named for what's missing.
    done, text = win.coach_page._voice_step()
    assert not done and "MB" in text
    ctl.cfg.app.language = "ru"
    done, text = win.coach_page._voice_step()
    assert done and "Русский" in text  # nothing to download: it says who speaks Russian
    # Kitten is English only: in Spanish the step points at Kokoro; in English it names Kitten's own download.
    from doty import kitten, kokoro as kokoro_mod

    ctl.cfg.app.language, ctl.cfg.voice.engine = "es", "kitten"
    done, text = win.coach_page._voice_step()
    assert not done and text.startswith("Kitten speaks English only") and "Español" in text
    ctl.cfg.app.language = "en"
    done, text = win.coach_page._voice_step()
    assert not done and f"{kitten.DOWNLOAD_MB} MB" in text
    # ElevenLabs chosen with no key, Kokoro on this PC: Kokoro is what speaks (never "one 0 MB download").
    monkeypatch.setattr(kokoro_mod, "installed", lambda folder=None, language="en": True)
    ctl.cfg.voice.engine = "elevenlabs"
    assert win.coach_page._voice_step() == (True, "Doty talks with Kokoro, the free voice on this PC.")
    ctl.stop()
    win.close()


def test_your_new_languages_voices_are_fetched_when_kokoro_is_already_here(qapp, tmp_path, monkeypatch):
    from doty import config, kokoro as kokoro_mod
    from doty.ui.controller import Controller

    cfg = Config()
    cfg.source_path = tmp_path / "doty.toml"
    cfg.app.language, cfg.voice.engine = "es", "auto"
    ctl = Controller(cfg)
    fetched = []
    monkeypatch.setattr(ctl, "download_kokoro", lambda: fetched.append(ctl.cfg.app.language))
    here = {"en"}
    monkeypatch.setattr(kokoro_mod, "installed", lambda folder=None, language="en": language in here)
    ctl.fetch_language_voices()
    assert fetched == []  # a throwaway config never downloads
    monkeypatch.setattr(config, "is_user_config", lambda cfg: True)
    ctl.fetch_language_voices()
    assert fetched == ["es"]  # Kokoro is here, its Spanish voices aren't: 2 MB, fetched without being asked
    here.add("es")
    ctl.fetch_language_voices()
    assert fetched == ["es"]  # already here
    here.discard("es")
    for language, engine in (("ru", "auto"), ("en", "auto"), ("es", "kitten"), ("es", "windows")):
        ctl.cfg.app.language, ctl.cfg.voice.engine = language, engine
        ctl.fetch_language_voices()
    assert fetched == ["es"]  # nothing to fetch for a language Kokoro lacks, or a voice you didn't choose
    here.clear()
    ctl.cfg.app.language, ctl.cfg.voice.engine = "es", "auto"
    ctl.fetch_language_voices()
    assert fetched == ["es"]  # no Kokoro at all: the 330 MB is yours to start, not Doty's


def test_restarting_in_a_new_language_starts_a_copy_that_waits_for_this_one(qapp, monkeypatch):
    from PySide6 import QtCore

    from doty.ui import main as ui_main
    from doty.ui import window

    started = []

    class Proc:
        def setProgram(self, exe):  # noqa: N802
            self.exe = exe

        def setArguments(self, args):  # noqa: N802
            self.args = args

        def setProcessEnvironment(self, env):  # noqa: N802
            self.env = env

        def startDetached(self):  # noqa: N802
            started.append(self)

    monkeypatch.setattr(QtCore, "QProcess", Proc)
    monkeypatch.setattr(window, "quit_doty", lambda: started.append("quit"))
    window.relaunch_doty()
    proc, quit_ = started
    assert quit_ == "quit" and proc.env.value(ui_main.TAKEOVER_ENV) == "1"  # (it won't hand back to this copy)
    assert "doty" in " ".join(proc.args) or proc.exe
