"""The app in the player's language: lookups, templates, capitals, and Qt's text setters."""

import json

from .test_app_ui import qapp  # noqa: F401  (the fixture)

import pytest

from doty import i18n


@pytest.fixture
def spanish(tmp_path, monkeypatch):
    (tmp_path / "es.json").write_text(json.dumps({
        "Your plan": "Tu plan", "Back off, low HP": "Retrocede, poca vida", "{name} in {secs}": "{name} en {secs}",
        "Bounty runes": "Runas de recompensa", "Read to me": "Léemelo", "Muted": "Silenciado",
    }), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES", tmp_path)
    i18n.set_language("es")
    yield
    i18n.set_language("en")


def test_lookups_templates_and_capitals(spanish):
    assert i18n.tr("Your plan") == "Tu plan"
    assert i18n.tr("YOUR PLAN") == "TU PLAN"  # (titles shown in capitals)
    assert i18n.tr("  Your plan ") == "  Tu plan "
    assert i18n.tr("Not in the catalog") == "Not in the catalog"  # English, never blank
    assert i18n.tr("{name} in {secs}", name=i18n.tr("Bounty runes"), secs=15) == "Runas de recompensa en 15"
    assert i18n.tr("No match for {text}", text="axe") == "No match for axe"  # (a field may be called "text")


def test_english_and_an_unknown_language_are_english(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "LOCALES", tmp_path)
    i18n.set_language("xx")
    assert i18n.language() == "en" and i18n.tr("Your plan") == "Your plan"
    assert i18n.tr("{name} in {secs}", name="Runes", secs=5) == "Runes in 5"


def test_rule_alerts_are_said_in_the_language(spanish):
    from doty.advice import AdviceBus
    from doty.config import RulesConfig
    from doty.rules import RuleEngine
    from doty.state import GameStateStore

    from .conftest import Recorder

    store, bus, rec = GameStateStore(), AdviceBus(), Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, RulesConfig())
    engine.on_snapshot(store.ingest({"map": {"matchid": "1", "clock_time": 225,
                                             "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                                     "player": {"team_name": "radiant"}}, received_t=1.0))
    timer = [a for a in rec.items if a.category == "timer"]
    assert timer and timer[0].speech == "Runas de recompensa, Water runes en 15"


def test_the_catalogs_cover_every_text():
    """en.json is current with the code (run tools/extract_strings.py after changing UI text), and every language
    has every text with the same placeholders (tools/check_locales.py)."""
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    try:
        import check_locales
        import extract_strings
    finally:
        sys.path.remove(str(tools))
    english = json.loads((i18n.LOCALES / "en.json").read_text(encoding="utf-8"))
    assert extract_strings.extract() == english, "en.json is out of date: run tools/extract_strings.py"
    codes = [p.stem for p in i18n.LOCALES.glob("*.json") if p.stem != "en"]
    assert len(codes) >= 15
    assert [p for code in codes for p in check_locales.check(code, english)] == []


def test_qt_text_is_translated(spanish, qapp):
    from PySide6.QtWidgets import QComboBox, QLabel

    i18n._installed = False
    i18n.install_qt()
    assert QLabel("Read to me").text() == "Léemelo"
    box = QComboBox()
    box.addItem("Read to me", "Read to me")
    assert box.itemText(0) == "Léemelo" and box.itemData(0) == "Read to me"  # (the value stays as it is)
