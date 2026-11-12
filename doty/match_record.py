"""A light record of every match, on this PC only, so Doty's coaching can be checked against what really happened.

On by default (Settings > Advanced > "Keep a record of each match"; RecordConfig). Each match gets a folder in
%LOCALAPPDATA%\\Doty\\records (RECORD_DIR, not Documents, so OneDrive doesn't sync it), named by when it started:

- gsi.jsonl.gz: the game data, as capture mode keeps it (match_capture.scrub: no Steam ID, account ID, player
  names or auth token) and with the match id hidden too. Dota's change copies ("previously", "added") are left
  out: nothing in Doty reads them. One update a second, plus every update where your items,
  life, level or the game's events changed. Written plain while the match runs, packed when it ends.
- advice.jsonl: every line Doty showed, with the game clock, who said it (rules or coach), the rule's id, its
  priority and category, and whether it was said out loud. What you said over push-to-talk isn't kept.
- match.json: your hero, team, the game mode and the lineup once Doty knows it, and when the match began and
  ended.

No screenshots. Nothing is sent anywhere. The oldest records go once there are more than `keep_matches` of them
or they add up to more than `keep_mb`; "Delete records" in Settings removes them all. `python -m doty.bench
--latest` scores the coach on them. (Capture mode, with screenshots, is separate and stays off unless you turn it
on: match_capture.py.)
"""

from __future__ import annotations

import gzip
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import RECORD_DIR, RecordConfig
from .match_capture import scrub
from .state import GameStateStore, Snapshot

FORMAT = 1
EVERY_S = 1.0  # at least one game-data update a second is kept
GAME_FILE = "gsi.jsonl"
ADVICE_FILE = "advice.jsonl"
INFO_FILE = "match.json"


def record_folder(cfg: RecordConfig | None = None) -> Path:
    return Path(cfg.folder) if cfg is not None and cfg.folder else RECORD_DIR


def list_records(root: Path) -> list[Path]:
    """The record folders, oldest first (their names are their start times)."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / INFO_FILE).exists())


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def rule_of(adv) -> str:
    """Which part of Doty said a line: the rule's id (set by `tag_rules`), "coach:<category>" for the AI coach."""
    rule = getattr(adv, "rule_id", "")
    if rule:
        return rule
    return f"coach:{adv.category}" if adv.source == "coach" else ""


def tag_rules(engine) -> None:
    """Have every rule mark the lines it makes with its id (Advice.rule_id), for the session log, the record and
    the bench. Wraps each rule's evaluate once; the rules themselves don't change."""
    for rule in engine.rules:
        if getattr(rule.evaluate, "_tags_rule", False):
            continue
        rule.evaluate = _tagged(rule.evaluate, rule.id)


def _tagged(evaluate, rule_id: str):
    def run(ctx):
        for adv in evaluate(ctx):
            adv.rule_id = rule_id
            yield adv
    run._tags_rule = True
    return run


def spoken(adv, voiced_max: int = 3) -> bool:
    """Said out loud (as far as Doty decides it: the voice may still drop a line that comes too late). The coach's
    answers are spoken a sentence at a time, apart from their card, so the card itself says speak=False."""
    if adv.source == "coach" and adv.category == "coach" and int(adv.priority) <= 2:
        # (the speech gate may have kept an answer's sentences quiet: the card knows, speech.py)
        heard = getattr(adv, "heard", None)
        return bool(adv.speak) if heard is None else bool(heard)
    return bool(adv.speak) and int(adv.priority) <= voiced_max


class MatchRecord:
    """Writes the record of the match being played. Called on the game-data thread (`on_payload`) and from
    whichever thread publishes advice (`on_advice`); both are cheap. `demos`: record Doty's scripted demo too
    (tests); real use leaves it out."""

    def __init__(self, cfg: RecordConfig, store: GameStateStore, voiced_max: int = 3, root: Path | None = None,
                 demos: bool = False, now=time.time) -> None:
        self.cfg = cfg
        self.now = now  # wall time (tests pass their own)
        self.store = store
        self.voiced_max = voiced_max
        self.root = Path(root) if root is not None else record_folder(cfg)
        self.demos = demos
        self.folder: Path | None = None
        self._key: tuple | None = None
        self._game = None
        self._advice = None
        self._info: dict[str, Any] = {}
        self._last_t = 0.0
        self._last_sig: tuple | None = None
        self._pending: tuple | None = None  # (payload, snapshot) of the latest update not written
        self._said: set[int] = set()
        self._lineup: tuple | None = None
        self._lock = threading.RLock()
        self._broken = False
        self.packing: list[threading.Thread] = []  # matches being packed (tests wait on these)

    @property
    def on(self) -> bool:
        return bool(self.cfg.enabled)

    # the game data --------------------------------------------------------------------------------------------
    def on_payload(self, payload: dict[str, Any], snap: Snapshot) -> None:
        if not self.on or self._broken:
            return
        try:
            with self._lock:
                self._on_payload(payload, snap)
        except OSError as exc:  # a full disk must never break coaching
            print(f"[doty] match record: couldn't write ({exc.__class__.__name__}); off until Doty restarts")
            self._broken = True
            self.close()

    def _on_payload(self, payload: dict[str, Any], snap: Snapshot) -> None:
        key = (self.store.generation, self.store.match_id)
        playing = snap.has_hero or snap.in_draft
        if self.folder is not None and key != self._key and playing:
            self.close()  # a new match: the last one is done
        if self.folder is None:
            if not snap.match_id or not playing or snap.custom_game or (snap.is_demo and not self.demos):
                return
            self._start()
            self._key = key
        if not playing:
            return  # (the menus between matches aren't part of either)
        now = self.now()
        sig = (json.dumps(payload.get("events") or []), tuple(snap.item_list()), snap.alive, snap.level,
               snap.game_state)
        if sig == self._last_sig and now - self._last_t < EVERY_S:
            self._pending = (payload, snap)  # (written after all if a line comes from it: see on_advice)
            return
        self._last_sig, self._last_t = sig, now
        self._write(payload, now)
        self._note(snap)

    def _write(self, payload: dict[str, Any], now: float) -> None:
        self._pending = None
        self._game.write(json.dumps({"t": now, "payload": scrub(payload, keep_match=False)}) + "\n")
        self._game.flush()

    def _start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S")
        folder, n = self.root / name, 1
        while folder.exists():
            n += 1
            folder = self.root / f"{name}-{n}"
        folder.mkdir()
        self.folder = folder
        self._game = (folder / GAME_FILE).open("a", encoding="utf-8")
        self._advice = (folder / ADVICE_FILE).open("a", encoding="utf-8")
        self._info = {"format": FORMAT, "started": self.now(), "ended": None, "hero": "", "hero_id": 0,
                      "team": "", "mode": "", "duration": None, "lineup": None}
        self._said.clear()
        self._lineup = None
        self._last_sig = self._pending = None
        self._write_info()
        self.prune()

    def _note(self, snap: Snapshot) -> None:
        """Keep match.json up to date with what's known about the match (rewritten only when it changes)."""
        info, changed = self._info, False
        if snap.has_hero and (info["hero"] != snap.hero_name or info["team"] != snap.team):
            info.update(hero=snap.hero_name, hero_id=snap.hero_id, team=snap.team)
            changed = True
        mode = getattr(self.store.memory.get("mode"), "key", "")
        if mode and mode != info["mode"]:
            info["mode"], changed = mode, True
        tracker = self.store.memory.get("lineup_votes")
        if tracker is not None:
            lineup = (tuple(tracker.confirmed("radiant")), tuple(tracker.confirmed("dire")),
                      bool(getattr(tracker, "complete", False)))
            if lineup != self._lineup and (lineup[0] or lineup[1]):
                self._lineup = lineup
                info["lineup"] = {"radiant": list(lineup[0]), "dire": list(lineup[1]), "complete": lineup[2]}
                changed = True
        if snap.in_game or snap.has_hero:
            info["duration"] = snap.clock
        if changed:
            self._write_info()

    def _write_info(self) -> None:
        if self.folder is not None:
            part = self.folder / (INFO_FILE + ".part")
            part.write_text(json.dumps(self._info, indent=1), encoding="utf-8")
            part.replace(self.folder / INFO_FILE)

    # what Doty said ---------------------------------------------------------------------------------------------
    def on_advice(self, adv) -> None:
        if not self.on:  # switched off mid-match: not one more line, and the match's files are finished now
            if self.folder is not None:
                self.close()
            return
        if not adv.final or not adv.display or adv.source == "player" or not adv.text.strip():
            return  # (what you said over push-to-talk stays out of the record)
        if getattr(adv, "held", False):
            return  # (the speech gate holds it for a slot: written when it's said or dropped, sent again then)
        with self._lock:
            if self._advice is None or adv.id in self._said:
                return
            self._said.add(adv.id)
            snap = self.store.current
            try:
                # The update this line came from goes in first, so the line is judged against exactly what Doty
                # saw (an update that changed only the gold isn't otherwise kept).
                if self._pending is not None and self._pending[1] is snap and self._game is not None:
                    self._write(self._pending[0], self.now())
                self._advice.write(json.dumps({
                    "t": self.now(), "clock": snap.clock if snap is not None else None, "source": adv.source,
                    "rule": rule_of(adv), "priority": int(adv.priority), "category": adv.category,
                    "speak": bool(adv.speak), "spoken": spoken(adv, self.voiced_max), "text": adv.text,
                }, ensure_ascii=False) + "\n")
                self._advice.flush()
            except OSError:
                pass

    # the end of a match, and keeping the folder small -----------------------------------------------------------
    def close(self) -> None:
        """The match is over (or Doty is closing): finish its files and pack the game data, off this thread."""
        with self._lock:
            folder, game, advice = self.folder, self._game, self._advice
            self.folder = self._game = self._advice = None
            self._key = None
            if folder is None:
                return
            self._info["ended"] = self.now()
            try:
                for fh in (game, advice):
                    if fh is not None:
                        fh.close()
                part = folder / (INFO_FILE + ".part")
                part.write_text(json.dumps(self._info, indent=1), encoding="utf-8")
                part.replace(folder / INFO_FILE)
            except OSError:
                pass
        job = threading.Thread(target=pack, args=(folder,), name="doty-record-pack", daemon=True)
        self.packing.append(job)
        job.start()

    def prune(self) -> list[Path]:
        """Delete the oldest records beyond the limits (never the one being written). Records a crash left
        unpacked are packed first. Returns the folders removed."""
        records = [p for p in list_records(self.root) if p != self.folder]
        for p in records:
            if (p / GAME_FILE).exists() and not (p / (GAME_FILE + ".gz")).exists():
                pack(p)
        gone = []
        keep = max(1, int(self.cfg.keep_matches))
        limit = max(1, int(self.cfg.keep_mb)) * 1024 * 1024
        sizes = {p: folder_size(p) for p in records}
        current = folder_size(self.folder) if self.folder is not None else 0
        # (by size, the newest finished record stays even when it alone is over: something to look at)
        while records and (len(records) + (self.folder is not None) > keep
                           or (sum(sizes[p] for p in records) + current > limit and len(records) > 1)):
            old = records.pop(0)
            shutil.rmtree(old, ignore_errors=True)
            gone.append(old)
        return gone

    def delete_all(self) -> int:
        """Remove every record, the one being written included (Settings > Delete records). Returns how many."""
        with self._lock:
            open_one = self.folder
            for fh in (self._game, self._advice):
                if fh is not None:
                    fh.close()
            self.folder = self._game = self._advice = None
            self._key = None
            found = list_records(self.root)
            if open_one is not None and open_one not in found:
                found.append(open_one)
            for p in found:
                shutil.rmtree(p, ignore_errors=True)
            return len(found)


def pack(folder: Path) -> None:
    """gsi.jsonl -> gsi.jsonl.gz (about a twentieth of the size)."""
    plain = Path(folder) / GAME_FILE
    packed = plain.with_name(GAME_FILE + ".gz")
    try:
        part = packed.with_name(packed.name + ".part")
        with plain.open("rb") as src, gzip.open(part, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        part.replace(packed)
        plain.unlink()
    except OSError as exc:
        print(f"[doty] match record: couldn't pack {plain.parent.name} ({exc.__class__.__name__})")


def delete_records(root: Path) -> int:
    """Remove every record under `root` (when Doty isn't running). Returns how many."""
    found = list_records(root)
    for p in found:
        shutil.rmtree(p, ignore_errors=True)
    return len(found)


def summary(root: Path) -> tuple[int, int]:
    """(how many records, their total size in bytes)."""
    found = list_records(root)
    return len(found), sum(folder_size(p) for p in found)
