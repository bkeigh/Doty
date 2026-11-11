"""Doty's languages (AppConfig.language): their names, the order they're offered in, and which voice
services speak each one.

Light on purpose (no imports from the rest of Doty): the first-run language choice shows before anything
heavy is loaded.

- **The six offered first** (`FEATURED`): English, then the languages most Dota is played in. There is no
  official count by language; this goes by where the players are (Russia and its neighbours the largest
  single group, then China, Spanish-speaking South America, Brazil, and Indonesia in Southeast Asia;
  the Philippines mostly plays in English). The other ten sit under "More languages".
- **Voices** (checked 2026-11-10):
  - Kokoro v1.0 has voices for English, Spanish, Brazilian Portuguese and French that need only eSpeak NG
    (kokoro.py). Its Chinese and Japanese voices need a separate text front end Doty doesn't ship, so they
    aren't offered.
  - Kitten speaks English only.
  - ElevenLabs: its fast models (Flash v2.5, Turbo v2.5; 32 languages) speak every Doty language but Thai;
    Multilingual v2 also lacks Vietnamese; v3 has all of them
    (elevenlabs.io/docs/help-center/other/what-languages-do-you-support).
  - The Windows voice speaks the languages whose speech is installed in Windows (voice.windows_languages).
"""

from __future__ import annotations

# code -> (name in English, own name).
LANGUAGES = {
    "en": ("English", "English"), "es": ("Spanish", "Español"), "pt": ("Brazilian Portuguese", "Português (Brasil)"),
    "ru": ("Russian", "Русский"), "zh": ("Simplified Chinese", "简体中文"), "fr": ("French", "Français"),
    "de": ("German", "Deutsch"), "pl": ("Polish", "Polski"), "uk": ("Ukrainian", "Українська"),
    "tr": ("Turkish", "Türkçe"), "ko": ("Korean", "한국어"), "ja": ("Japanese", "日本語"),
    "th": ("Thai", "ไทย"), "vi": ("Vietnamese", "Tiếng Việt"), "id": ("Indonesian", "Bahasa Indonesia"),
    "fil": ("Filipino", "Filipino"),
}
FEATURED = ("en", "ru", "zh", "es", "pt", "id")

KOKORO = ("en", "es", "pt", "fr")
KITTEN = ("en",)
# ElevenLabs models -> the Doty languages they don't speak. A model that isn't listed is taken to speak all.
ELEVEN_MISSING = {
    "eleven_flash_v2_5": ("th",), "eleven_turbo_v2_5": ("th",), "eleven_multilingual_v2": ("th", "vi"),
    "eleven_v3": (),
}
ELEVEN_ENGLISH_ONLY = ("eleven_flash_v2", "eleven_turbo_v2", "eleven_monolingual_v1")
ELEVEN_DEFAULT = "eleven_flash_v2_5"


def ordered() -> list[str]:
    """Every language: the featured six, then the rest."""
    return [*FEATURED, *(c for c in LANGUAGES if c not in FEATURED)]


def known(code: str) -> str:
    """`code` as one of Doty's languages ("EN" and "pt-BR", typed by hand, are English and Portuguese);
    English if it's none of them."""
    code = (code or "en").strip().lower()
    return code if code in LANGUAGES else from_locale(code)


def own_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES["en"])[1]


def english_name(code: str) -> str:
    return LANGUAGES.get(code, LANGUAGES["en"])[0]


def from_locale(name: str) -> str:
    """A system locale ("pt_BR", "zh-CN", "tl_PH", "ru") as one of Doty's languages; English if it's none."""
    primary = (name or "").replace("-", "_").split("_")[0].strip().lower()
    if primary == "tl":  # Tagalog: Windows and Linux both name Filipino this way too
        primary = "fil"
    return primary if primary in LANGUAGES else "en"


def kokoro_speaks(code: str) -> bool:
    return (code or "en") in KOKORO


def kitten_speaks(code: str) -> bool:
    return (code or "en") in KITTEN


def eleven_speaks(code: str, model: str = "") -> bool:
    code, model = code or "en", model or ELEVEN_DEFAULT
    if model in ELEVEN_ENGLISH_ONLY:
        return code == "en"
    return code not in ELEVEN_MISSING.get(model, ())


def eleven_languages(model: str = "") -> list[str]:
    return [c for c in ordered() if eleven_speaks(c, model)]
