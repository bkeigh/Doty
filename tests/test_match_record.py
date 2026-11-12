"""The match record (match_record.py): on by default, game data and every line said, never who anyone is; the
session log and review carrying what the bench needs; the bench scoring a record; and the Settings controls for
the record, the drawings and hero study."""

import gzip
import json
import time

from doty.advice import Advice, AdviceBus, Priority
from doty.bench import records as bench_records
from doty.bench.__main__ import main as bench_main
from doty.config import Config, RecordConfig, dump_config, load_config
from doty.match_capture import scrub
from doty.match_record import (ADVICE_FILE, GAME_FILE, INFO_FILE, MatchRecord, list_records, pack, rule_of,
                               tag_rules)
from doty.review import MatchRecorder, Review
from doty.rules import RuleEngine
from doty.sim import ScriptedMatch
from doty.state import GameStateStore

from .conftest import TOKEN
from .test_app_ui import make_window, pump, qapp  # noqa: F401  (the fixture)
from .test_bench import KB

# Who someone is, put everywhere a real payload (or a spectator's, which lists all ten players) could carry it.
SECRETS = ("76561190000000001", "123456789", "Secret Player", "SecretPersona", "https://avatar.example/x.jpg",
           "88877766655")


def _identified(payload):
    """A scripted payload with identity in every place Dota (or a later Dota) might put it."""
    p = json.loads(json.dumps(payload))
    p["player"].update(steamid=SECRETS[0], accountid=SECRETS[1], name=SECRETS[2], personaname=SECRETS[3],
                       avatar=SECRETS[4], a_new_text_field=SECRETS[2])
    p["player"]["team2"] = {"player0": {"steamid": SECRETS[0], "accountid": int(SECRETS[1]), "name": SECRETS[2],
                                        "kills": 3, "team_name": "radiant"}}
    p["hero"]["team2"] = {"player0": {"name": "npc_dota_hero_juggernaut", "steam_id": SECRETS[0]}}
    p["events"] = [{"event_type": "bounty_rune_pickup", "player_id": 0, "game_time": 100,
                    "account_id": SECRETS[1]}]
    p["map"]["matchid"] = SECRETS[5]
    p["auth"] = {"token": "secret-token"}
    return p


# who anyone is never gets in -------------------------------------------------------------------------------------------
def test_scrub_removes_every_identity_field_and_any_new_text_in_the_player_section():
    m = ScriptedMatch()
    m.step(300)
    raw = _identified(m.payload(TOKEN))
    kept = json.dumps(scrub(raw, keep_match=False))
    for secret in SECRETS + ("secret-token",):
        assert secret not in kept, secret
    clean = scrub(raw, keep_match=False)
    # What Doty reads is still there: the hero's name, items, gold, the team.
    assert clean["hero"]["name"] == "npc_dota_hero_juggernaut" and clean["player"]["gold"] == raw["player"]["gold"]
    assert clean["player"]["team_name"] == "radiant" and clean["items"]["slot0"]["name"].startswith("item_")
    assert clean["map"]["matchid"] == "recorded" and SECRETS[5] in json.dumps(scrub(raw))  # (capture keeps it)
    # The only texts left in the player section are the reviewed ones: a text Dota adds later is dropped.
    def texts(d, path=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from texts(v, f"{path}{k}.")
            elif isinstance(v, str):
                yield f"{path}{k}"
    assert set(texts(clean["player"])) <= {"activity", "team_name", "team2.player0.team_name"}


# the record ---------------------------------------------------------------------------------------------------------
def _play(tmp_path, end_clock=700, identified=False, cfg=None, lineup=None):
    """The scripted match through a store, the rules and a record, the way app.Doty wires them."""
    store, bus = GameStateStore(), AdviceBus()
    m = ScriptedMatch(end_clock=end_clock)
    record = MatchRecord(cfg or RecordConfig(), store, root=tmp_path / "records", demos=True,
                         now=lambda: 2_000_000_000.0 + m.s.clock)  # (wall time keeps pace with the game)
    engine = RuleEngine(store, bus, Config().rules, KB)
    tag_rules(engine)
    bus.subscribe(record.on_advice)
    while m.s.clock < end_clock:
        m.step(1.0)
        payload = m.payload(TOKEN)
        snap = store.ingest(_identified(payload) if identified else payload)
        if lineup and m.s.clock == 30:
            store.memory["lineup_votes"] = lineup
        record.on_payload(payload if not identified else _identified(payload), snap)
        engine.on_snapshot(snap)
    bus.publish(Advice("What you asked over push-to-talk", Priority.INFO, "player", source="player"))
    record.close()
    for job in record.packing:
        job.join(10)
    return record


class _Lineup:
    complete = True

    def confirmed(self, side):
        return ["npc_dota_hero_juggernaut", "npc_dota_hero_luna"] if side == "radiant" else ["npc_dota_hero_lion"]


def test_a_match_is_recorded_packed_and_never_holds_who_played(tmp_path):
    record = _play(tmp_path, identified=True, lineup=_Lineup())
    (folder,) = list_records(tmp_path / "records")
    assert sorted(p.name for p in folder.iterdir()) == [ADVICE_FILE, GAME_FILE + ".gz", INFO_FILE]
    with gzip.open(folder / (GAME_FILE + ".gz"), "rt", encoding="utf-8") as fh:
        game = fh.read()
    everything = game + (folder / ADVICE_FILE).read_text(encoding="utf-8") + (folder / INFO_FILE).read_text("utf-8")
    for secret in SECRETS + ("secret-token", "push-to-talk"):
        assert secret not in everything, secret
    updates = game.splitlines()
    assert 700 <= len(updates) <= 900  # about one a second (the scripted match sends one a second)
    lines = [json.loads(x) for x in (folder / ADVICE_FILE).read_text(encoding="utf-8").splitlines()]
    assert lines and set(lines[0]) == {"t", "clock", "source", "rule", "priority", "category", "speak", "spoken",
                                       "text"}
    low = next(x for x in lines if x["rule"] == "low_hp")
    assert low["spoken"] and low["priority"] == 1 and low["source"] == "rules"
    info = json.loads((folder / INFO_FILE).read_text(encoding="utf-8"))
    assert info["hero"] == "npc_dota_hero_juggernaut" and info["team"] == "radiant" and info["ended"]
    assert info["lineup"] == {"radiant": ["npc_dota_hero_juggernaut", "npc_dota_hero_luna"],
                              "dire": ["npc_dota_hero_lion"], "complete": True}
    assert record.folder is None


def test_the_record_is_off_with_its_switch_and_skips_menus(tmp_path):
    cfg = RecordConfig(enabled=False)
    _play(tmp_path, end_clock=60, cfg=cfg)
    assert list_records(tmp_path / "records") == []
    store = GameStateStore()
    record = MatchRecord(RecordConfig(), store, root=tmp_path / "menus", demos=True)
    record.on_payload({"map": {"game_state": "DOTA_GAMERULES_STATE_INIT"}}, store.ingest({"map": {}}))
    assert record.folder is None and not (tmp_path / "menus").exists()
    demo = MatchRecord(RecordConfig(), GameStateStore(), root=tmp_path / "demo")  # (the demo isn't a real game)
    m = ScriptedMatch()
    m.step(10)
    demo.on_payload(m.payload(TOKEN), demo.store.ingest(m.payload(TOKEN)))
    assert demo.folder is None


def test_switched_off_mid_match_not_one_more_line_is_written(tmp_path):
    cfg = RecordConfig()
    store = GameStateStore()
    record = MatchRecord(cfg, store, root=tmp_path / "records", demos=True)
    m = ScriptedMatch()
    for _ in range(30):
        m.step(1.0)
        payload = m.payload(TOKEN)
        record.on_payload(payload, store.ingest(payload))
    record.on_advice(Advice("Said while on", Priority.NORMAL, "general"))
    folder = record.folder
    assert folder is not None
    cfg.enabled = False  # (the switch, on the config the running record holds)
    record.on_advice(Advice("Said after the switch", Priority.NORMAL, "general"))
    assert record.folder is None  # (the match's files are finished at once)
    for job in record.packing:
        job.join(10)
    text = (folder / ADVICE_FILE).read_text(encoding="utf-8")
    assert "Said while on" in text and "Said after the switch" not in text
    m.step(1.0)
    payload = m.payload(TOKEN)
    record.on_payload(payload, store.ingest(payload))
    assert record.folder is None and len(list_records(tmp_path / "records")) == 1


def test_saving_the_switch_off_reaches_the_running_record_at_once(qapp, tmp_path):  # noqa: F811
    """Settings' Save goes through Controller.apply_config, which restarts the running engine with the new
    settings: the old record is closed and the new one is off, mid-match or not."""
    import copy

    from doty.selftest import quiet_config
    from doty.ui.controller import Controller

    cfg = quiet_config()
    cfg.record.enabled, cfg.record.folder = True, str(tmp_path / "records")
    cfg.source_path = tmp_path / "doty.toml"
    ctl = Controller(cfg)
    try:
        assert ctl.start(), ctl.error
        old = ctl.doty.record
        assert old.on
        changed = copy.deepcopy(cfg)
        changed.record.enabled = False
        ctl.apply_config(changed)
        assert ctl.doty is not None and not ctl.doty.record.on and old.folder is None
    finally:
        ctl.stop()


def test_old_records_are_pruned_by_count_and_size_and_delete_all_removes_them(tmp_path):
    root = tmp_path / "records"
    for n in range(5):
        old = root / f"2026010{n}-120000"
        old.mkdir(parents=True)
        (old / INFO_FILE).write_text("{}", encoding="utf-8")
        (old / GAME_FILE).write_bytes(b'{"t": 1, "payload": {}}\n' * 2000)  # (left unpacked by a crash)
    record = MatchRecord(RecordConfig(keep_matches=3, keep_mb=1), GameStateStore(), root=root)
    gone = record.prune()
    left = list_records(root)
    assert [p.name for p in gone] == ["20260100-120000", "20260101-120000"]
    assert [p.name for p in left] == ["20260102-120000", "20260103-120000", "20260104-120000"]
    assert all((p / (GAME_FILE + ".gz")).exists() and not (p / GAME_FILE).exists() for p in left)  # packed
    big = root / "20260105-120000"
    big.mkdir()
    (big / INFO_FILE).write_text("{}", encoding="utf-8")
    (big / "gsi.jsonl.gz").write_bytes(b"x" * (1024 * 1024 + 10))  # over keep_mb on its own with the rest
    record.prune()
    assert list_records(root)[-1].name == "20260105-120000" and len(list_records(root)) == 1
    assert record.delete_all() == 1 and list_records(root) == []


def test_a_packed_record_cut_off_mid_write_still_reads(tmp_path):
    from doty.bench.replay import read_recording

    folder = tmp_path / "r"
    folder.mkdir()
    (folder / GAME_FILE).write_text("".join(json.dumps({"t": i, "payload": {"map": {"clock_time": i}}}) + "\n"
                                            for i in range(50)), encoding="utf-8")
    pack(folder)
    data = (folder / (GAME_FILE + ".gz")).read_bytes()
    (folder / (GAME_FILE + ".gz")).write_bytes(data[: len(data) // 2])
    assert 0 < sum(1 for _ in read_recording(folder)) < 50


# the session log and the review -------------------------------------------------------------------------------------
def test_rule_ids_reach_the_lines():
    store, bus, seen = GameStateStore(), AdviceBus(), []
    bus.subscribe(seen.append)
    engine = RuleEngine(store, bus, Config().rules, None)
    tag_rules(engine)
    tag_rules(engine)  # (twice is the same as once)
    m = ScriptedMatch()
    for _ in range(300):
        m.step(1.0)
        engine.on_snapshot(store.ingest(m.payload(TOKEN)))
    assert seen and all(rule_of(a) for a in seen)
    assert rule_of(Advice("x", source="coach", category="watching")) == "coach:watching"


def test_the_review_keeps_every_purchase_including_parts(tmp_path):
    rec, store = MatchRecorder(None, out_dir=tmp_path), GameStateStore()
    m = ScriptedMatch(end_clock=700)
    while m.s.clock < 700:
        m.step(1.0)
        rec.on_snapshot(store.ingest(m.payload(TOKEN)), store)
    review = rec.last_review  # (written at the end screen)
    items = [i for i, _c in review.purchases]
    assert items.count("item_branches") == 2 and "item_magic_wand" in items and "item_tpscroll" in items
    assert ("item_phase_boots", 400) in review.purchases and len(review.bought) < len(review.purchases)
    again = Review.from_dict(json.loads(json.dumps(review.to_dict())))
    assert again.purchases == review.purchases
    assert Review.from_dict({"match_id": "1"}).purchases == []  # (older reviews load)


def test_the_session_log_says_rule_speak_and_match(tmp_path, monkeypatch):
    from doty import app as appmod

    from .test_app_ui import quiet_cfg

    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path)
    doty = appmod.Doty(quiet_cfg(tmp_path), overlay=False, voice=False, coach=False, listen=False, quiet=True)
    doty._log_path = tmp_path / "session-test.jsonl"
    adv = Advice("Buy a TP scroll", Priority.NORMAL, "economy", speak=False)
    adv.rule_id = "missing_tp"
    doty.bus.publish(adv)
    doty._log.close()
    row = json.loads((tmp_path / "session-test.jsonl").read_text(encoding="utf-8"))
    assert row["rule"] == "missing_tp" and row["speak"] is False and row["match"] == doty.store.generation
    assert doty.record.on and doty.cfg.record.enabled  # (on by default)


# the bench on records -------------------------------------------------------------------------------------------------
def test_the_bench_judges_a_record_with_the_recorded_state(tmp_path, capsys):
    _play(tmp_path, end_clock=900, lineup=_Lineup())
    (folder,) = list_records(tmp_path / "records")
    # A line the coach "said" at 13:00 to a player who holds a TP scroll (the scripted player has one again from
    # 11:40), naming a hero who isn't in the match.
    with (folder / ADVICE_FILE).open("a", encoding="utf-8") as fh:
        t = json.loads((folder / ADVICE_FILE).read_text(encoding="utf-8").splitlines()[-1])["t"] + 0.5
        fh.write(json.dumps({"t": t, "clock": 899, "source": "coach", "rule": "coach:coach", "priority": 2,
                             "category": "coach", "speak": False, "spoken": True,
                             "text": "Buy a TP scroll now, Tidehunter is coming."}) + "\n")
    match = bench_records.record_match(folder, KB)
    assert match.kind == "record" and all(x.ctx is not None and x.ctx.snap is not None for x in match.lines)
    assert match.lines[-1].ctx.heroes == {"npc_dota_hero_juggernaut", "npc_dota_hero_luna", "npc_dota_hero_lion"}
    from doty.bench import score

    card = score.score_match(match)
    fails = {(f["checker"], f["at"]) for f in card["fails"]}
    assert ("tp_nag", "14:59") in fails
    assert card["checks"]["timing"]["checked"] > 0 and card["checks"]["passed_on"]["checked"] >= 0
    summary = bench_records.last_game_summary(tmp_path / "records", KB)
    assert summary.startswith("Last game Doty said") and "TP scroll you held (1 time)" in summary
    out = tmp_path / "card.json"
    assert bench_main(["--latest", "--records", str(tmp_path / "records"), "--json", str(out), "--quiet",
                       "--no-kb"]) == 0
    assert [m["kind"] for m in json.loads(out.read_text(encoding="utf-8"))["matches"]] == ["record"]
    assert bench_main([str(folder), "--quiet", "--no-kb"]) == 0


def test_an_older_session_log_still_reads_and_a_newer_one_uses_its_fields(tmp_path):
    from doty.bench import logs

    log = tmp_path / "logs" / "session-x.jsonl"
    log.parent.mkdir()
    rows = [{"t": 1000 + c, "clock": c, "source": "rules", "category": "timer", "priority": 3, "text": f"Line {c}",
             "rule": "spawn_timer", "speak": c != 120, "match": 1 if c < 400 else 2} for c in (0, 60, 120, 180)]
    rows += [{"t": 2000 + c, "clock": c, "source": "rules", "category": "timer", "priority": 3, "text": f"L{c}",
              "match": 2} for c in (400, 500, 600)]  # (no rule or speak: an older Doty)
    log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    a, b = logs.log_matches(log, None, min_minutes=1)
    assert [x.rule for x in a.lines] == ["spawn_timer"] * 4 and [x.spoken for x in a.lines] == [True, True, False,
                                                                                                    True]
    assert [x.spoken for x in b.lines] == [None, None, None] and b.lines[0].said  # (estimated from priority)


# settings -------------------------------------------------------------------------------------------------------------
def test_the_new_settings_round_trip(tmp_path):
    cfg = Config()
    assert cfg.record.enabled and cfg.record.keep_matches == 30 and cfg.record.keep_mb == 200 and cfg.app.study
    cfg.record.enabled, cfg.app.study, cfg.overlay.mark_width, cfg.overlay.mark_opacity = False, False, 3.5, 0.8
    path = tmp_path / "doty.toml"
    path.write_text(dump_config(cfg), encoding="utf-8")
    back = load_config(path)
    assert (back.record.enabled, back.app.study, back.overlay.mark_width, back.overlay.mark_opacity) == (
        False, False, 3.5, 0.8)


def _row(page, key):
    return next(row for row, keys, _tab in page._tracked if key in keys)


def test_settings_drawing_sliders_follow_the_switch_and_apply_live(qapp, tmp_path):
    from PySide6.QtWidgets import QSlider

    from doty.ui.widgets import Switch

    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    seen = []
    ctl.preview_drawing = lambda w, o: seen.append((w, o))
    width, opacity = _row(page, ("overlay", "mark_width")), _row(page, ("overlay", "mark_opacity"))
    width.findChild(QSlider).setValue(7)
    opacity.findChild(QSlider).setValue(40)
    assert page.draft.overlay.mark_width == 3.5 and page.draft.overlay.mark_opacity == 0.4
    assert seen[-1] == (3.5, 0.4) and ctl.cfg.overlay.mark_width == 2.0  # (shown now, saved on Save)
    assert width.findChild(QSlider).minimum() == 2 and width.findChild(QSlider).maximum() == 12  # 1 to 6 px
    assert opacity.findChild(QSlider).minimum() == 30 and opacity.findChild(QSlider).maximum() == 100
    draw = _row(page, ("overlay", "draw")).findChild(Switch)
    draw.click()
    assert not width.isEnabled() and not opacity.isEnabled()
    draw.click()
    assert width.isEnabled()
    page._save()
    assert load_config(tmp_path / "doty.toml").overlay.mark_width == 3.5
    win.close()


def test_settings_record_switch_delete_button_and_study_status(qapp, tmp_path):
    from doty import match_record
    from doty.ui.pages import study_progress
    from doty.ui.widgets import Switch

    ctl, win = make_window(qapp, tmp_path)
    page = win.settings_page
    ctl.cfg.record.folder = str(tmp_path / "records")
    page._build()
    record = _row(page, ("record", "enabled"))
    hint = page._record_hint(page.draft)
    assert str(tmp_path / "records") in hint and "Nothing is sent anywhere" in hint and "30 matches" in hint
    record.findChild(Switch).click()
    assert page.draft.record.enabled is False
    study = _row(page, ("app", "study"))
    study.findChild(Switch).click()
    page._save()
    saved = load_config(tmp_path / "doty.toml")
    assert saved.record.enabled is False and saved.app.study is False
    # Delete records: the first click asks, the second deletes.
    folder = tmp_path / "records" / "20260101-120000"
    folder.mkdir(parents=True)
    (folder / match_record.INFO_FILE).write_text("{}", encoding="utf-8")
    btn = page._delete_records_btn
    btn.click()
    assert folder.exists()
    btn.click()
    assert not folder.exists()
    win.close()
    # The study status, from card files alone.
    cache = tmp_path / "cache"
    from doty.knowledge import cards as hero_cards

    card_dir = cache / hero_cards.CARDS_DIR  # (tests keep cards in a folder of their own: conftest)
    card_dir.mkdir(parents=True)
    (cache / "heroes.json").write_text(json.dumps({str(i): {"id": i} for i in range(1, 6)}), encoding="utf-8")
    (cache / "patch.json").write_text(json.dumps([{"name": "7.41"}]), encoding="utf-8")
    for i in (1, 2):
        (card_dir / f"card_{i}.json").write_text(json.dumps(
            {"version": 1, "hero_id": i, "patch": "7.41", "built_at": time.time()}), encoding="utf-8")
    (card_dir / "card_3.json").write_text(json.dumps(
        {"version": 1, "hero_id": 3, "patch": "7.40", "built_at": time.time()}), encoding="utf-8")
    assert study_progress(cache) == "Studied 2 of 5 heroes for 7.41."
    assert study_progress(tmp_path / "empty") == ""
