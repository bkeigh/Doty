"""Capture mode: frames and game data of one match, saved locally, only when it's on."""

import json
import time

from PIL import Image

from doty.config import CaptureConfig
from doty.match_capture import MatchCapture
from doty.state import GameStateStore


def _payload(clock, alive=True, hp=1000, level=3, extra=None):
    return {"map": {"matchid": "777", "clock_time": clock, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "player": {"team_name": "radiant"},
            "hero": {"name": "npc_dota_hero_axe", "id": 2, "alive": alive, "health": hp, "max_health": 1000,
                     "level": level},
            "auth": {"token": "secret"}, **(extra or {})}


def _wait(cap, n):
    end = time.time() + 5
    while cap.frames < n and time.time() < end:
        time.sleep(0.02)


def test_off_by_default_and_saves_nothing(tmp_path):
    cap = MatchCapture(CaptureConfig(folder=str(tmp_path)), grab=lambda: Image.new("RGB", (1920, 1080)))
    store = GameStateStore()
    p = _payload(100)
    cap.on_payload(p, store.ingest(p), store)
    assert not cap.on and not any(tmp_path.iterdir())


def test_saves_frames_crops_and_game_data(tmp_path):
    cap = MatchCapture(CaptureConfig(enabled=True, folder=str(tmp_path), every_s=3600),
                       grab=lambda: Image.new("RGB", (3440, 1440), (20, 40, 60)))
    store = GameStateStore()
    t = 100.0
    for clock, alive, hp, extra in ((100, True, 1000, {"minimap": {"o0": {"xpos": 1}}}), (101, True, 1000, None),
                                    (102, False, 0, {"events": [{"event_type": "x"}]})):
        t += 1
        p = _payload(clock, alive, hp, extra=extra)
        cap.on_payload(p, store.ingest(p, received_t=t), store)
        _wait(cap, 1)
    _wait(cap, 2)
    cap.close()
    folder = next(tmp_path.iterdir())
    names = {f.name for f in folder.iterdir()}
    assert "gsi.jsonl" in names and "sections.json" in names
    sections = json.loads((folder / "sections.json").read_text(encoding="utf-8"))
    assert "minimap" in sections and "auth" not in sections  # what a player's feed really has; never the token
    lines = (folder / "gsi.jsonl").read_text(encoding="utf-8").splitlines()
    assert all("secret" not in line for line in lines)
    assert any(n.startswith("frame-") and n.endswith("-death.jpg") for n in names)  # a frame when you die
    assert any(n.startswith("topbar-") for n in names) and any(n.startswith("minimap-left-") for n in names)


def test_never_saves_other_apps_or_who_you_are_and_survives_a_bad_folder(tmp_path, monkeypatch):
    """Frames only while Dota is in front; the Steam ID, account ID and name are left out; a rolling events
    list Dota resends isn't written every update; a folder that can't be written turns capture off for the
    match instead of breaking the game feed."""
    from doty import match_capture

    front = [False]
    cap = MatchCapture(CaptureConfig(enabled=True, folder=str(tmp_path), every_s=0),
                       grab=lambda: Image.new("RGB", (1920, 1080)), foreground=lambda: front[0])
    store = GameStateStore()
    who = {"player": {"team_name": "radiant", "steamid": "7656119", "accountid": "123456789", "name": "me"}}
    for i in range(5):
        p = _payload(100 + i, extra={**who, "events": [{"event_type": "bounty_rune_pickup"}]})
        cap.on_payload(p, store.ingest(p, received_t=100.0 + i), store)
    time.sleep(0.2)
    assert cap.frames == 0  # another window was in front the whole time
    front[0] = True
    cap.take_frame("every", 105)
    _wait(cap, 1)
    assert cap.frames == 1
    cap.close()
    folder = next(tmp_path.iterdir())
    text = (folder / "gsi.jsonl").read_text(encoding="utf-8") + (folder / "sections.json").read_text(encoding="utf-8")
    assert "7656119" not in text and "123456789" not in text and '"me"' not in text
    assert len((folder / "gsi.jsonl").read_text(encoding="utf-8").splitlines()) <= 2  # not 5: same events

    def broken(*a, **k):
        raise PermissionError("drive gone")

    monkeypatch.setattr(match_capture.MatchCapture, "_start", broken)
    p = _payload(200, extra={"map": {"matchid": "888", "clock_time": 200,
                                     "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"}})
    cap.on_payload(p, store.ingest(p), store)  # no exception
    cap.on_payload(p, store.ingest(p), store)
    assert cap._broken == "888"
