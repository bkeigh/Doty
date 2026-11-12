"""Capture mode: frames and game data of one match, saved locally, only when it's on."""

import json
import time

from PIL import Image

from doty.config import CaptureConfig
from doty.match_capture import MatchCapture, scrub
from doty.state import GameStateStore

# who anyone is, at any depth: the scrub's rule is structural --------------------------------------------------------
NAME, STEAM, ACCOUNT = "Secret Player", "76561190000000001", "123456789"


def test_a_name_in_previously_player_is_removed():
    """The reproduction: Dota's "previously" holds a changed field's old value, a player's name included."""
    raw = {"player": {"name": NAME, "activity": "playing", "kills": 1},
           "previously": {"player": {"name": "Old " + NAME, "steamid": STEAM, "kills": 0}},
           "hero": {"name": "npc_dota_hero_axe"}}
    for keep_match in (True, False):
        text = json.dumps(scrub(raw, keep_match=keep_match))
        assert NAME not in text and STEAM not in text
    capture = scrub(raw)  # (capture mode keeps "previously", scrubbed; the record leaves it out)
    assert capture["previously"]["player"] == {"kills": 0} and capture["hero"]["name"] == "npc_dota_hero_axe"
    assert "previously" not in scrub(raw, keep_match=False)


def test_a_spectators_ten_players_under_previously_and_added_are_scrubbed():
    block = {"team2": {f"player{i}": {"name": f"{NAME} {i}", "accountid": ACCOUNT, "kills": i,
                                      "team_name": "radiant"} for i in range(5)},
             "team3": {f"player{i}": {"name": f"{NAME} {i}", "steamid": STEAM, "kills": i} for i in range(5, 10)}}
    raw = {"player": block, "previously": {"player": block, "map": {"matchid": "999"}},
           "added": {"player": {"team2": {"player0": {"name": NAME}}}},
           "hero": {"team2": {"player0": {"name": "npc_dota_hero_juggernaut"}}}}
    clean = scrub(raw)
    text = json.dumps(clean)
    assert NAME not in text and ACCOUNT not in text and STEAM not in text
    assert clean["previously"]["player"]["team2"]["player1"] == {"kills": 1, "team_name": "radiant"}
    assert clean["hero"]["team2"]["player0"]["name"] == "npc_dota_hero_juggernaut"  # (a hero's name stays)
    record = scrub(raw, keep_match=False)
    assert "999" not in json.dumps(record) and "added" not in record and "previously" not in record


def test_a_name_inside_added_is_removed():
    raw = {"added": {"player": {"name": NAME, "personaname": NAME, "kills": True}}}
    assert NAME not in json.dumps(scrub(raw))
    assert scrub(raw)["added"]["player"] == {"kills": True}


def test_no_planted_identity_survives_at_any_depth():
    """Any nested payload: identity planted under an identity key anywhere, or as text in any player section at
    any depth (inside "previously", "added", team blocks or none), never comes out; nor the match id, for a
    record."""
    import random

    rng = random.Random(1234)
    secret_keys = ["steamid", "accountid", "account_id", "steam_id", "personaname", "player_name", "avatar",
                   "auth", "party_id", "lobby_id", "SteamID64", "AccountId"]
    plain_keys = ["map", "hero", "items", "abilities", "events", "previously", "added", "team2", "team3",
                  "player0", "player7", "slot0", "draft", "provider", "minimap", "league"]
    for trial in range(300):
        planted: list[str] = []

        def build(depth: int, in_player: bool) -> dict:
            node: dict = {}
            for _ in range(rng.randint(1, 4)):
                roll = rng.random()
                if roll < 0.2:
                    value = f"SECRET-{trial}-{len(planted)}"
                    planted.append(value)
                    node[rng.choice(secret_keys)] = value if rng.random() < 0.5 else {"x": value}
                elif in_player and roll < 0.4:
                    value = f"SECRET-{trial}-{len(planted)}"
                    planted.append(value)
                    node[rng.choice(["name", "nickname", "a_new_field", "clan"])] = value
                elif depth < 6 and roll < 0.75:
                    key = "player" if rng.random() < 0.3 else rng.choice(plain_keys)
                    child = build(depth + 1, in_player or key == "player")
                    node[key] = [child] if rng.random() < 0.15 else child
                else:
                    node[rng.choice(["kills", "gold", "clock_time", "level"])] = rng.randint(0, 99)
            return node

        payload = build(0, False)
        if not isinstance(payload.get("map"), dict):
            payload["map"] = {}
        payload["map"]["matchid"] = f"MATCH-{trial}"
        for keep_match in (True, False):
            text = json.dumps(scrub(payload, keep_match=keep_match))
            for value in planted:
                assert value not in text, (trial, value)
        assert f"MATCH-{trial}" not in json.dumps(scrub(payload, keep_match=False))


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
