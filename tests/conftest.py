import copy
import os

import pytest

from doty.advice import AdviceBus
from doty.config import Config
from doty.rules import RuleEngine
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

TOKEN = "test-token"


@pytest.fixture(autouse=True, scope="session")
def _no_real_kokoro(tmp_path_factory):
    """Tests never load the real 330 MB Kokoro model by accident: its folder is empty here."""
    os.environ["DOTY_KOKORO_DIR"] = str(tmp_path_factory.mktemp("kokoro-none"))
    os.environ["DOTY_ART"] = "0"  # no hero portraits or item icons from the internet either
    os.environ["DOTY_STUDY"] = "0"  # nor the app's background study of public match data
    # Match records (on by default) go to a throwaway folder, never the real one in %LOCALAPPDATA%.
    from doty import config, match_record

    records = tmp_path_factory.mktemp("records")
    config.RECORD_DIR = match_record.RECORD_DIR = records


@pytest.fixture(autouse=True)
def _no_real_hero_cards(monkeypatch):
    """Hero cards the player's own Doty has studied (cache/cards) never leak into a test: tests read and write
    cards in a folder of their own name, which only a test's own temporary cache holds."""
    from doty.knowledge import cards

    monkeypatch.setattr(cards, "CARDS_DIR", "cards-test")


@pytest.fixture(scope="session")
def real_wincred():
    """The real Windows Credential Manager, for the one test that round-trips a throwaway entry."""
    from doty import keystore

    cred = keystore._wincred()
    if cred is None:
        pytest.skip("no Windows Credential Manager here")
    return cred


@pytest.fixture(autouse=True)
def _no_real_credential_manager(monkeypatch, request):
    """Tests never read or write the player's saved API keys: the key store finds no Credential Manager
    unless a test hands it one (a fake, or `real_wincred` for a throwaway entry)."""
    from doty import keystore

    if "real_wincred" in request.fixturenames:
        request.getfixturevalue("real_wincred")  # resolve it before the lookup is switched off
    monkeypatch.setattr(keystore, "_wincred", lambda: None)


class Recorder:
    def __init__(self) -> None:
        self.items = []

    def __call__(self, adv) -> None:
        # Streaming coach cards are one object updated in place; keep each version.
        self.items.append(copy.copy(adv))

    @property
    def keys(self) -> list[str]:
        return [a.key for a in self.items]


@pytest.fixture
def harness():
    cfg = Config()
    store = GameStateStore()
    bus = AdviceBus()
    rec = Recorder()
    bus.subscribe(rec)
    engine = RuleEngine(store, bus, cfg.rules, kb=None)
    return cfg, store, engine, rec


def play_scripted(store, engine, end_clock=26 * 60 + 30, dt=1.0):
    """Drive the scripted match straight into the engine; fake wall time = game clock."""
    match = ScriptedMatch(end_clock=end_clock)
    while match.s.clock < end_clock:
        match.step(dt)
        snap = store.ingest(match.payload(TOKEN), received_t=match.s.clock + 1000.0)
        engine.on_snapshot(snap)
    return match
