"""Capture mode: keep a record of one match on this PC, to make Doty better at reading your screen.

When it's on (Settings > Advanced > Capture matches), during a match Doty saves into
Documents\\Doty\\captures\\<match>\\:
- **frames:** the Dota window every 30 s, at the horn, when you die, when a fight starts and at level 6
  (JPEG), plus full-quality crops of the top bar and minimap (PNG). These calibrate the screen readers
  (vision.py) to where your HUD really draws portraits and icons at your resolution.
- **game data:** the raw game-state updates once a second, plus every update whose events changed
  (gsi.jsonl), and the first sample of every data section Dota sends (sections.json). That shows what a
  player's feed really contains.

Doty doesn't send any of it anywhere. Your Steam ID, account ID and name are left out of the game data,
and frames are only taken while Dota is the window in front (never another app over it). The folder is
in Documents, so if OneDrive (or another backup) syncs Documents, it uploads these too; delete the
folder whenever you like. A match keeps at most MAX_FRAMES frames. It's off by default.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import hud
from .config import CaptureConfig, documents_dir
from .state import GameStateStore, Snapshot

GSI_EVERY_S = 1.0
MAX_FRAMES = 300  # per match: ~150 MB at 1440p, about 2 hours at one every 30 s
PRIVATE_KEYS = {"steamid", "accountid", "name"}  # who you are: not needed to calibrate anything
# Anywhere in a payload, a key that names a person or an account (Dota's own names, and the spellings other
# GSI versions and tools use). A spectator's or a coach's feed lists all ten players.
IDENTITY_KEYS = {"steamid", "steam_id", "steamid64", "accountid", "account_id", "personaname", "persona",
                 "player_name", "playername", "nickname", "nick", "real_name", "realname", "profile", "avatar",
                 "avatarurl", "avatar_url", "friend_id", "party_id", "lobby_id", "server_steam_id"}
# In the player section only these texts stay: everything else Dota puts there is a number (kills, gold...), and
# a text it adds in a later version is left out rather than risk it being a name.
PLAYER_TEXT_KEPT = {"activity", "team_name"}


def capture_folder(cfg: CaptureConfig) -> Path:
    return Path(cfg.folder) if cfg.folder else documents_dir() / "Doty" / "captures"


# Dota's copy of what changed since the last update ("previously": the old values; "added": which fields are new).
# They mirror the payload's own shape, so a player's name can sit at previously.player.name. Nothing in Doty reads
# them (the store, the rules and the bench use the current values), so a match record leaves them out altogether.
CHANGE_KEYS = {"previously", "added"}


def _drop_identity(value: Any, in_player: bool, keep_match: bool) -> Any:
    """The rule is structural, so it holds at any depth and in any mirror of the payload: an identity key goes
    wherever it is, and everything under a key named "player" (the top-level section, its copies inside
    "previously" and "added", and the ten per-player blocks a spectator's feed nests under it as
    team2/team3 > player0..9) is a player section, where "name" and any other text but the team and activity go."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if key in IDENTITY_KEYS or key == "auth" or "steam" in key or "account" in key:
                continue
            if in_player and (key == "name" or (isinstance(v, str) and key not in PLAYER_TEXT_KEPT)):
                continue  # (in "player", "name" is the player's name; elsewhere it's a hero, item or ability)
            if key == "matchid" and not keep_match:
                out[k] = "recorded"
                continue
            out[k] = _drop_identity(v, in_player or key == "player", keep_match)
        return out
    if isinstance(value, list):
        return [_drop_identity(v, in_player, keep_match) for v in value]
    return value


def scrub(payload: dict[str, Any], keep_match: bool = True) -> dict[str, Any]:
    """The payload without the auth token or who anyone is: Steam and account IDs and names wherever they appear,
    and in every player section (yours, every player's when Dota sends all ten, and their copies in "previously"
    and "added") any text but the team and activity.

    `keep_match=False` is the match record's (match_record.py, on by default): it also hides the match id wherever
    it appears, since anyone can look one up to see who played, and leaves out "previously" and "added", which
    nothing in Doty reads. Capture mode (off unless turned on) keeps them, scrubbed the same way."""
    if not keep_match:
        payload = {k: v for k, v in payload.items() if str(k).lower() not in CHANGE_KEYS}
    return _drop_identity(payload, False, keep_match)


class MatchCapture:
    def __init__(self, cfg: CaptureConfig, grab: Callable[[], Any] | None = None,
                 foreground: Callable[[], bool] | None = None) -> None:
        self.cfg = cfg
        self.grab = grab  # () -> PIL image of the Dota window, or None
        self.foreground = foreground  # () -> True while Dota is the window in front
        self.folder: Path | None = None
        self.match = ""
        self.frames = 0
        self._gsi_fh = None
        self._last_gsi = 0.0
        self._last_frame = -1e9
        self._sections: dict[str, Any] = {}
        self._was_fighting = False
        self._was_alive = True
        self._level = 0
        self._events = ""  # the last events list written (Dota resends a rolling list every update)
        self._broken = ""  # the match whose folder couldn't be written (skipped until the next match)
        self._busy = threading.Lock()

    @property
    def on(self) -> bool:
        return bool(self.cfg.enabled)

    # game data ------------------------------------------------------------------
    def on_payload(self, payload: dict[str, Any], snap: Snapshot, store: GameStateStore) -> None:
        """Called for every update from Dota (the engine's thread). Cheap: writes are small and frames are
        saved on a worker thread."""
        if not self.on or not snap.match_id or snap.match_id == self._broken:
            return
        try:
            self._record(payload, snap)
        except OSError as exc:  # a full disk or an unplugged drive must never break the game feed
            print(f"[doty] capture: couldn't write to {self.folder} ({exc!r}); off for this match")
            self._broken = snap.match_id
            self.close()
            self.folder = None
            return
        reason = self._frame_reason(snap, store)
        if reason:
            self.take_frame(reason, snap.clock)

    def _record(self, payload: dict[str, Any], snap: Snapshot) -> None:
        if snap.match_id != self.match:
            self._start(snap.match_id)
        now = time.time()
        clean = scrub(payload)
        new = [k for k in clean if k not in self._sections]
        for key in new:
            self._sections[key] = clean[key]
        if new:
            self._write_json("sections.json", self._sections)
        events = json.dumps(clean.get("events") or [])
        if (events != self._events and events != "[]") or now - self._last_gsi >= GSI_EVERY_S:
            self._events = events
            self._last_gsi = now
            if self._gsi_fh is not None:
                self._gsi_fh.write(json.dumps({"t": now, "payload": clean}) + "\n")
                self._gsi_fh.flush()

    def _frame_reason(self, snap: Snapshot, store: GameStateStore) -> str:
        if not snap.has_hero:
            return ""
        fighting = store.in_fight()
        reason = ""
        if self._was_alive and not snap.alive:
            reason = "death"
        elif fighting and not self._was_fighting:
            reason = "fight"
        elif self._level < 6 <= snap.level:
            reason = "level6"
        elif -1 <= snap.clock <= 0 and time.monotonic() - self._last_frame > 5:
            reason = "horn"
        elif snap.in_game and time.monotonic() - self._last_frame >= self.cfg.every_s:
            reason = "every"
        self._was_fighting, self._was_alive, self._level = fighting, snap.alive, snap.level
        return reason

    # frames ---------------------------------------------------------------------
    def take_frame(self, reason: str, clock: int) -> None:
        """Save a frame on a worker thread (skipped if the last one is still being written, if Dota isn't
        the window in front, or once the match has MAX_FRAMES)."""
        if self.grab is None or self.folder is None or self.frames >= MAX_FRAMES:
            return
        if self.foreground is not None and not self.foreground():
            return  # Dota isn't in front: the grab would save whatever window covers it
        if not self._busy.acquire(blocking=False):
            return
        self._last_frame = time.monotonic()
        folder = self.folder

        def work() -> None:
            try:
                img = self.grab()
                if img is None:
                    return
                stamp = f"{'-' if clock < 0 else ''}{abs(clock) // 60:02d}m{abs(clock) % 60:02d}s-{reason}"
                img.save(folder / f"frame-{stamp}.jpg", quality=92)
                w, h = img.size
                tx, ty, tw, th = hud.topbar_rect(w, h)
                img.crop((tx, ty, tx + tw, ty + th)).save(folder / f"topbar-{stamp}.png")
                for side in ("left", "right"):  # the minimap can be on either side (a Dota option)
                    mx, my, ms, _ = hud.minimap_rect(w, h, side)
                    img.crop((mx, my, mx + ms, my + ms)).save(folder / f"minimap-{side}-{stamp}.png")
                self.frames += 1
            except Exception as exc:  # a full disk or a closing game must never break coaching
                print(f"[doty] capture: couldn't save a frame ({exc!r})")
            finally:
                self._busy.release()

        threading.Thread(target=work, name="doty-capture", daemon=True).start()

    # files ----------------------------------------------------------------------
    def _start(self, match_id: str) -> None:
        self.close()
        self.match = match_id
        self.folder = capture_folder(self.cfg) / f"{time.strftime('%Y%m%d-%H%M')}-{match_id}"
        self._sections = {}
        self._events = ""
        self.frames = 0
        self.folder.mkdir(parents=True, exist_ok=True)
        self._gsi_fh = (self.folder / "gsi.jsonl").open("a", encoding="utf-8")
        print(f"[doty] capture mode: saving this match to {self.folder}")

    def _write_json(self, name: str, data: Any) -> None:
        if self.folder is not None:
            (self.folder / name).write_text(json.dumps(data, indent=1), encoding="utf-8")

    def close(self) -> None:
        if self._gsi_fh is not None:
            try:
                self._gsi_fh.close()
            except OSError:
                pass
            self._gsi_fh = None
