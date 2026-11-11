"""Shadow mode: Doty's own top-bar reader logs what it reads next to the AI's lineup, and changes nothing."""

import json
import time

from PIL import Image

from doty import app as appmod
from doty import vision
from doty.config import Config
from doty.lineup import Lineup


class FakeReader:
    last_ms, last_fit = 5.0, None

    def read(self, frame, candidates=None):
        sides = [("radiant", i) for i in range(5)] + [("dire", i) for i in range(5)]
        heroes = ["npc_dota_hero_axe", "npc_dota_hero_lion"] + [""] * 8
        return [vision.Slot(side, i, h, 0.9 if h else 0.0, 0.3, False) for (side, i), h in zip(sides, heroes)]


def test_shadow_reads_are_logged_beside_the_ai_lineup(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(appmod, "dota_is_foreground", lambda: True)
    monkeypatch.setattr(appmod, "grab_window", lambda: Image.new("RGB", (1920, 1080)))
    monkeypatch.setattr(vision.TopBarReader, "from_cache", classmethod(lambda cls, **k: FakeReader()))
    cfg = Config()
    cfg.coach.vision = "shadow"
    doty = appmod.Doty(cfg, overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb.heroes = {"npc_dota_hero_axe": {}, "npc_dota_hero_lion": {}}
    doty.store.ingest({"map": {"matchid": "1", "clock_time": 300, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                       "player": {"team_name": "radiant"},
                       "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "level": 5}})
    votes = doty.store.memory["lineup_votes"] = Lineup()
    for _ in range(2):  # two agreeing AI reads: Axe and Lion on Radiant, Pudge on Dire
        votes.add(["npc_dota_hero_axe", "npc_dota_hero_lion"], ["npc_dota_hero_pudge"], "npc_dota_hero_axe", "radiant")
    doty.running = True
    monkeypatch.setattr(appmod.Doty, "VISION_EVERY_S", 0.01)
    import threading

    t = threading.Thread(target=doty._vision_loop, daemon=True)
    t.start()
    end = time.time() + 3
    while time.time() < end and not list(tmp_path.glob("vision-*.jsonl")):
        time.sleep(0.02)
    time.sleep(0.1)
    doty.running = False
    t.join(2)
    line = json.loads(next(tmp_path.glob("vision-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
    assert line["slots"][0][:3] == ["radiant", 0, "axe"] and line["agree"] == 2 and line["disagree"] == 0
    assert votes.reads == 2 and votes.confirmed("dire") == ["npc_dota_hero_pudge"]  # shadow mode changed nothing
    doty.shutdown()


def test_one_bad_read_doesnt_stop_the_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(appmod, "dota_is_foreground", lambda: True)
    grabs = []

    def grab():
        grabs.append(1)
        if len(grabs) == 1:
            raise OSError("the window closed mid-grab")
        return Image.new("RGB", (1920, 1080))

    monkeypatch.setattr(appmod, "grab_window", grab)
    monkeypatch.setattr(vision.TopBarReader, "from_cache", classmethod(lambda cls, **k: FakeReader()))
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb.heroes = {"npc_dota_hero_axe": {}}
    doty.store.ingest({"map": {"matchid": "1", "clock_time": 300, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                       "player": {"team_name": "radiant"},
                       "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "level": 5}})
    doty.running = True
    monkeypatch.setattr(appmod.Doty, "VISION_EVERY_S", 0.01)
    import threading

    t = threading.Thread(target=doty._vision_loop, daemon=True)
    t.start()
    end = time.time() + 3
    while time.time() < end and not list(tmp_path.glob("vision-*.jsonl")):
        time.sleep(0.02)
    doty.running = False
    t.join(2)
    assert len(grabs) >= 2 and list(tmp_path.glob("vision-*.jsonl"))  # it read again after the error
    doty.shutdown()


TEN = (["npc_dota_hero_axe", "npc_dota_hero_lion", "npc_dota_hero_zuus", "npc_dota_hero_dazzle", "npc_dota_hero_jakiro"],
       ["npc_dota_hero_pudge", "npc_dota_hero_bane", "npc_dota_hero_warlock", "npc_dota_hero_nevermore",
        "npc_dota_hero_juggernaut"])


class SureReader:
    """All ten read surely; Bane and Warlock greyed out when reading only the ten known."""

    last_ms = 5.0
    last_fit = vision.SlotFit([], True, (9.0, 9.0, 9.0), (0.0, 0.0), (1.0, 1.0), 0.0, found=(1.0, 1.0))

    def __init__(self):
        self.restricted = 0

    def read(self, frame, candidates=None):
        if candidates:
            self.restricted += 1
        slots = []
        for side, names in zip(("radiant", "dire"), TEN):
            for i, h in enumerate(names):
                dead = bool(candidates) and h in ("npc_dota_hero_bane", "npc_dota_hero_warlock")
                slots.append(vision.Slot(side, i, h, 0.5 if dead else 0.9, 0.3, dead))
        return slots


def test_vision_on_feeds_the_lineup_and_tells_who_is_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(appmod, "dota_is_foreground", lambda: True)
    monkeypatch.setattr(appmod, "grab_window", lambda: Image.new("RGB", (1920, 1080)))
    reader = SureReader()
    monkeypatch.setattr(vision.TopBarReader, "from_cache", classmethod(lambda cls, **k: reader))
    monkeypatch.setattr(appmod.Doty, "VISION_VOTE_EVERY_S", 0.0)  # (a vote every few seconds in real life)
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    assert doty.cfg.coach.vision == "on"  # the default now
    doty.kb.heroes = {h: {} for side in TEN for h in side}
    doty.store.ingest({"map": {"matchid": "1", "clock_time": 600, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                       "player": {"team_name": "radiant"},
                       "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": True, "level": 8}})
    reads = []
    doty.coach.screen_read = lambda r, d, match_id="": (reads.append((r, d)), doty.coach._screen_read(r, d, match_id))
    state = {"reader": None, "built": 0.0, "log": None}
    for _ in range(3):
        doty._vision_step(vision, state)
    assert reads and reads[0] == TEN  # steady reads count towards the lineup
    assert doty.store.memory["lineup_complete"]
    for _ in range(2):  # now that all ten are known, the second read tells who's dead
        doty._vision_step(vision, state)
    assert reader.restricted >= 2
    screen = doty.store.memory["screen"]
    assert screen["dead"] == {"radiant": [], "dire": ["npc_dota_hero_bane", "npc_dota_hero_warlock"]}
    assert screen["since"] == {"npc_dota_hero_bane": 600, "npc_dota_hero_warlock": 600}
    assert doty._screen_signals() == {"enemies_dead": 2, "allies_dead": 0}
    raw = doty.store.current.raw
    doty.store.ingest({**raw, "map": {**raw["map"], "clock_time": 625}})
    doty._vision_step(vision, state)
    doty._vision_step(vision, state)
    assert doty.store.memory["screen"]["since"]["npc_dota_hero_bane"] == 600  # still the first time seen
    from doty.coach import prompts

    assert prompts.dead_now(doty.store, "radiant", None)["enemies"] == ["bane (dead 25 s)", "warlock (dead 25 s)"]
    line = json.loads((tmp_path / next(p.name for p in tmp_path.glob("vision-*.jsonl"))).read_text(
        encoding="utf-8").splitlines()[-1])
    assert line["dead"]["dire"] == ["npc_dota_hero_bane", "npc_dota_hero_warlock"]
    state["log"].close()
    doty.shutdown()


def test_real_match_reads_confirm_the_whole_lineup_within_seconds(tmp_path, monkeypatch):
    """Top-bar reads logged in a real match (2026-11-07, Huskar on Radiant): the AI never read this lineup,
    the screen reader has all ten right about 25 s after the game loaded, despite flickering reads."""
    from pathlib import Path

    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    doty = appmod.Doty(Config(), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty.kb.heroes = {}
    doty.store.ingest({"map": {"matchid": "7", "clock_time": -85, "game_state": "DOTA_GAMERULES_STATE_PRE_GAME"},
                       "player": {"team_name": "radiant"},
                       "hero": {"name": "npc_dota_hero_huskar", "id": 59, "alive": True, "level": 1}})
    doty.coach.screen_read = lambda r, d, match_id="": doty.coach._screen_read(r, d, match_id)
    fit = vision.SlotFit([], True, (9.0, 9.0, 9.0), (0.0, 0.0), (1.0, 1.0), 0.0, found=(1.0, 1.0))
    clock = {"now": 0.0}
    monkeypatch.setattr(appmod.time, "monotonic", lambda: clock["now"])
    state, done = {}, None
    for line in (Path(__file__).parent / "data" / "vision_real_match.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        clock["now"] = row["t"]
        slots = [vision.Slot(s[0], s[1], f"npc_dota_hero_{s[2]}" if s[2] else "", s[3], 0.0, s[4])
                 for s in row["slots"]]
        doty._use_reading(vision, slots, fit, doty.store.current, state, None)
        if doty.store.memory.get("lineup_complete") and done is None:
            done = row["clock"]
    assert done is not None and done <= -60
    got = {side: {h.removeprefix("npc_dota_hero_") for h in names}
           for side, names in doty.store.memory["lineup_votes"].as_dict().items()}
    assert got == {"radiant": {"huskar", "bloodseeker", "jakiro", "dazzle", "zuus"},
                   "dire": {"dragon_knight", "warlock", "bane", "nevermore", "juggernaut"}}
    doty.shutdown()
