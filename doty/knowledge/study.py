"""Study the game's data: fetch public statistics for heroes and build their cards (`cards.py`).

    python -m doty.knowledge.study "Spirit Breaker" 1 --print --role 4 --bracket 4
    python -m doty.knowledge.study --all

Per hero, four requests to OpenDota's free API, each cached on disk (so a run that stops resumes where it left
off, and a request is never made twice while its answer is fresh):

- `/benchmarks?hero_id=` (shared with Knowledge as `bench_<id>.json`): whole-game per-minute percentiles.
- `/scenarios/itemTimings?hero_id=`: every item's finish time in buckets, with wins, in one answer (Knowledge
  asks item by item for its own use; this is the same data, whole).
- the match explorer over pro matches of this patch (`PRO_SQL`): per position, the items finished and when,
  the skill order, and last hits, gold and experience at 10:00 and 20:00.
- the match explorer over the last few days of public ranked and Turbo matches (`PUB_SQL`): by rank and
  mode, results against every enemy hero and by game length.

Shared once per run: dotaconstants (heroes, items, abilities), Valve's patch list and notes (the files
Knowledge keeps), and how many public matches each rank plays (`RANK_SQL`). About 4 x 127 + 15 requests for
every hero: under OpenDota's 2,000 a day, at no more than REQUESTS_PER_MIN a minute.

Polite by design: a minute and a day budget (`RateLimiter`), one request at a time, a growing wait after a
failure and a pause of PAUSE_S after MAX_FAILURES in a row (never hammering a busy server), and every failure
falls back quietly to what's cached. `study_in_background` does a few requests at a time for the app: the
heroes of the current match first, then the player's most-played heroes (hero ids only: nothing about the
player is sent), then the rest.

Fair play and privacy: hero-level public statistics only. Nothing here looks up a player.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from ..config import CACHE_DIR
from . import cards
from .constants import CONSTANTS_BASE, OPENDOTA, PATCH_FEED

STUDY_DIR = "study"
REQUESTS_PER_MIN = 50  # OpenDota's free limit is 60 a minute without a key
MIN_GAP_S = 1.0  # and never two requests within a second
DAY_BUDGET = 1200  # of OpenDota's 2,000 a day: the rest is left for the app itself
BACKOFF_S = 15.0  # wait after a failure, doubling with each one in a row
MAX_BACKOFF_S = 300.0
MAX_FAILURES = 3  # in a row: then the study pauses
PAUSE_S = 30 * 60
RAW_TTL_S = 7 * 24 * 3600
THIN_TTL_S = 24 * 3600  # a blended (thin) sample is fetched again the next day
DAILY_TTL_S = 24 * 3600
PUBLIC_DAYS = 4  # the public window: the last few days of the current letter patch
FRESH_PATCH_S = 3 * 24 * 3600  # a patch younger than this has too few games of its own: blend the one before
PRO_MIN_GAMES = 150  # pro games of the hero on this patch before the previous patch is left out
PATCH_NOTES_KEPT = 4
TIMEOUT_S = 60  # (an explorer query can take several seconds)

# Positions from lane and farm order (see cards.position_of): the three best-farmed players of a team (gold a
# minute) are its cores, by lane; of the other two, the one in the safe lane is the hard support.
PRO_SQL = (
    "WITH ms AS (SELECT DISTINCT pm.match_id FROM player_matches pm JOIN match_patch mp USING (match_id) "
    "WHERE pm.hero_id={hero_id} AND mp.patch IN ({patches})), "
    "t AS (SELECT pm.match_id, pm.player_slot, pm.hero_id, pm.lane_role, pm.purchase_log, pm.ability_upgrades_arr, "
    "pm.lh_t, pm.gold_t, pm.xp_t, pm.last_hits, pm.gold_per_min, pm.xp_per_min, m.duration, "
    "((pm.player_slot < 128) = m.radiant_win) win, rank() OVER (PARTITION BY pm.match_id, pm.player_slot < 128 "
    "ORDER BY pm.gold_per_min DESC, pm.player_slot) farm FROM player_matches pm JOIN matches m USING (match_id) "
    "WHERE pm.match_id IN (SELECT match_id FROM ms)), "
    "h AS (SELECT *, CASE WHEN farm <= 3 THEN CASE lane_role WHEN 1 THEN 1 WHEN 2 THEN 2 ELSE 3 END "
    "ELSE CASE WHEN lane_role = 1 THEN 5 ELSE 4 END END pos FROM t WHERE hero_id={hero_id}), "
    "p AS (SELECT h.pos, h.win, h.match_id, h.player_slot, e->>'key' item, min((e->>'time')::int) tm "
    "FROM h, unnest(h.purchase_log) e GROUP BY 1,2,3,4,5) "
    "SELECT 'g' k, pos, '' x, count(*) n, sum(win::int) w, avg(lh_t[11]) v1, avg(gold_t[11]) v2, avg(xp_t[11]) v3, "
    "count(lh_t[21]) v4, avg(lh_t[21]) v5, avg(gold_t[21]) v6, avg(xp_t[21]) v7, "
    "avg(last_hits * 60.0 / nullif(duration, 0)) v8, avg(gold_per_min) v9, avg(xp_per_min) v10 FROM h GROUP BY pos "
    "UNION ALL SELECT 'i', pos, item, count(*), sum(win::int), percentile_disc(0.5) WITHIN GROUP (ORDER BY tm), "
    "null, null, null, null, null, null, null, null, null FROM p GROUP BY pos, item HAVING count(*) >= 3 "
    "UNION ALL SELECT 's', pos, u.lvl || ':' || u.ab, count(*), sum(win::int), null, null, null, null, null, null, "
    "null, null, null, null FROM h CROSS JOIN LATERAL unnest(h.ability_upgrades_arr) WITH ORDINALITY u(ab, lvl) "
    "WHERE u.lvl <= 25 GROUP BY pos, u.lvl, u.ab HAVING count(*) >= 2")
# Rank brackets from the match's average rank tier (10 x medal + stars). OpenDota's average tops out at 75, where
# Immortal games land, so 75 and up is counted as Immortal (an approximation).
_BRACKET = "CASE WHEN avg_rank_tier >= 75 THEN 8 ELSE coalesce(floor(avg_rank_tier / 10), 0) END"
PUB_SQL = (
    "WITH m AS (SELECT " + _BRACKET + " b, game_mode g, duration / 300 d, "
    "(({hero_id} = ANY(radiant_team)) = radiant_win)::int w, "
    "CASE WHEN {hero_id} = ANY(radiant_team) THEN dire_team ELSE radiant_team END foes FROM public_matches "
    "WHERE start_time >= {start} AND game_mode IN (22, 23) "
    "AND ({hero_id} = ANY(radiant_team) OR {hero_id} = ANY(dire_team))) "
    "SELECT 'v' k, b, g, e x, count(*) n, sum(w) w FROM m, unnest(foes) e GROUP BY 2, 3, 4 HAVING count(*) >= 5 "
    "UNION ALL SELECT 'd', b, g, d, count(*), sum(w) FROM m GROUP BY 2, 3, 4")
RANK_SQL = ("SELECT " + _BRACKET + " b, count(*) n FROM public_matches "
            "WHERE start_time >= {start} AND game_mode = 22 GROUP BY 1")


class Paused(Exception):
    """The study stops for now: the day's budget is spent, this run's budget is spent, or OpenDota keeps
    failing. (Everything fetched so far stays cached; the next run carries on.)"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RateLimiter:
    """At most `per_minute` requests in any 60 seconds, `min_gap_s` apart, and `per_day` a calendar day (the
    day count lives in `state`, which the study saves, so it holds across runs)."""

    def __init__(self, per_minute: int = REQUESTS_PER_MIN, per_day: int = DAY_BUDGET, min_gap_s: float = MIN_GAP_S,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = time.sleep,
                 today: Callable[[], str] | None = None, state: dict | None = None) -> None:
        self.per_minute, self.per_day, self.min_gap_s = per_minute, per_day, min_gap_s
        self.clock, self.sleep = clock, sleep
        self.today = today or (lambda: time.strftime("%Y-%m-%d"))
        self.state = state if state is not None else {}
        self.sent: list[float] = []

    def left_today(self) -> int:
        if self.state.get("day") != self.today():
            self.state.update(day=self.today(), calls=0)
        return max(0, self.per_day - int(self.state.get("calls") or 0))

    def acquire(self) -> bool:
        """Wait until a request may go out and count it. False (at once) when the day's budget is spent."""
        if self.left_today() <= 0:
            return False
        now = self.clock()
        self.sent = [t for t in self.sent if now - t < 60.0]
        wait = 0.0
        if len(self.sent) >= self.per_minute:
            wait = 60.0 - (now - self.sent[0])
        if self.sent:
            wait = max(wait, self.min_gap_s - (now - self.sent[-1]))
        if wait > 0:
            self.sleep(wait)
        self.sent.append(self.clock())
        self.state["calls"] = int(self.state.get("calls") or 0) + 1
        return True


def http_json(url: str) -> Any:
    """One GET, no retries (the study decides when to ask again)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Doty (open-source Dota 2 coach)"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def explorer_rows(data: Any) -> list | None:
    """The rows of an explorer answer, or None for an error (which is never cached)."""
    if isinstance(data, dict) and not data.get("err") and isinstance(data.get("rows"), list):
        return data["rows"]
    return None


def _sql_url(sql: str) -> str:
    return f"{OPENDOTA}/explorer?sql={urllib.parse.quote(sql)}"


class Study:
    """Fetches what the cards need, politely, and builds them. One per thread."""

    def __init__(self, cache_dir: Path = CACHE_DIR, fetch: Callable[[str], Any] | None = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], Any] = time.sleep,
                 limiter: RateLimiter | None = None, log: Callable[[str], Any] = print) -> None:
        self.cache_dir = Path(cache_dir)
        self.fetch = fetch or http_json
        self.now, self.sleep, self.log = now, sleep, log
        self.state_path = self.cache_dir / STUDY_DIR / "state.json"
        self.state = self._read_state()
        self.limiter = limiter or RateLimiter(sleep=sleep)
        self.limiter.state = self.state
        self.failures = 0  # in a row
        self.calls = 0  # requests made by this Study
        self.max_calls: int | None = None
        self._ctx: dict | None = None
        self._ctx_at = 0.0

    # state ----------------------------------------------------------------------------------------------
    def _read_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            part = self.state_path.with_name("state.json.part")
            part.write_text(json.dumps(self.state), encoding="utf-8")
            part.replace(self.state_path)
        except OSError:
            pass

    # fetching -------------------------------------------------------------------------------------------
    def _saved(self, path: Path, ttl: float, newer_than: float = 0) -> Any:
        """A cached answer still fresh enough (None if not)."""
        try:
            mtime = path.stat().st_mtime
            if self.now() - mtime >= ttl or mtime < newer_than:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def get(self, name: str, url: str, ttl: float = RAW_TTL_S, newer_than: float = 0,
            valid: Callable[[Any], bool] = lambda d: d not in (None, [], {})) -> Any:
        """The answer for `url`, from the cache file `name` (under the cache folder) while fresh, else fetched
        through the rate limiter and saved. None when it can't be had now (a failure: waited on, then given up
        on for this hero). Raises Paused when the study should stop."""
        path = self.cache_dir / name
        data = self._saved(path, ttl, newer_than)
        if data is not None:
            return data
        if float(self.state.get("pause_until") or 0) > self.now():
            raise Paused("paused_after_errors")
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise Paused("step_budget")
        if not self.limiter.acquire():
            self._save_state()
            raise Paused("day_budget")
        self.calls += 1
        try:
            data = self.fetch(url)
            ok = valid(data)
            why = "" if ok else "empty or error answer"
        except urllib.error.HTTPError as exc:
            data, ok, why = None, False, f"HTTP {exc.code}"
        except Exception as exc:  # offline, a timeout, bad JSON
            data, ok, why = None, False, exc.__class__.__name__
        if not ok:
            self._failed(url, why)
            return None
        self.failures = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".study-part")  # never a half-written file (Knowledge reads these too)
            part.write_text(json.dumps(data), encoding="utf-8")
            part.replace(path)
        except OSError as exc:
            self.log(f"[doty] couldn't save {name} ({exc.__class__.__name__})")
        self._save_state()
        return data

    def _failed(self, url: str, why: str) -> None:
        self.failures += 1
        self.log(f"[doty] study: couldn't fetch {url.split('?')[0]} ({why})")
        if self.failures >= MAX_FAILURES:
            self.state["pause_until"] = self.now() + PAUSE_S
            self._save_state()
            raise Paused("paused_after_errors")
        self.sleep(min(MAX_BACKOFF_S, BACKOFF_S * 2 ** (self.failures - 1)))

    # what every hero shares --------------------------------------------------------------------------------
    def context(self) -> dict:
        """dotaconstants, the patch and its notes, and the rank ladder: loaded once an hour."""
        if self._ctx is not None and self.now() - self._ctx_at < 3600:
            return self._ctx
        const = {}
        for name in ("heroes", "items", "abilities", "hero_abilities", "ability_ids", "patch"):
            const[name] = self.get(f"{name}.json", f"{CONSTANTS_BASE}/{name}.json", ttl=DAILY_TTL_S) or {}
        # Every hero's win rate by rank (shared with stats.Stats): a matchup is judged against both heroes' own.
        hero_stats = self.get("heroStats.json", f"{OPENDOTA}/heroStats", ttl=DAILY_TTL_S,
                              valid=lambda d: isinstance(d, list) and bool(d))
        listing = self.get("patchnoteslist.json", f"{PATCH_FEED}/patchnoteslist?language=english", ttl=DAILY_TTL_S,
                           valid=lambda d: isinstance(d, dict) and isinstance(d.get("patches"), list))
        patch, patch_ts = cards.current_patch(self.cache_dir)
        majors = [str(p.get("name")) for p in const["patch"] if isinstance(p, dict)] if isinstance(
            const["patch"], list) else []
        major = cards.major(patch) or (majors[-1] if majors else "")
        previous = majors[-2] if len(majors) >= 2 and majors[-1] == major else ""
        names = [str(p.get("patch_number") or "") for p in (listing or {}).get("patches") or []
                 if isinstance(p, dict)]
        versions = [n for n in names if cards.major(n) == major][-PATCH_NOTES_KEPT:]
        notes = {}
        for version in versions:
            data = self.get(f"patchnotes_{version}.json",
                            f"{PATCH_FEED}/patchnotes?version={version}&language=english", ttl=RAW_TTL_S)
            if isinstance(data, dict):
                notes[version] = data
        now = self.now()
        day = 24 * 3600
        start = int((now - PUBLIC_DAYS * day) // day * day)
        blended = bool(patch_ts) and now - patch_ts < FRESH_PATCH_S
        if patch_ts and not blended:
            start = max(start, int(patch_ts))
        days = max(1, round((now - start) / day))
        if blended:
            older = versions[-2] if len(versions) >= 2 else "the patch before"
            age = max(0, int((now - patch_ts) // day))
            public = f"last {days} days: {older} and {patch} ({patch} is {age} days old)"
        else:
            public = f"{patch or major}, last {days} days"
        rank_start = int((now - day) // day * day)  # (one day of matches is plenty: ~800,000)
        rank = self.get(f"{STUDY_DIR}/rankdist.json", _sql_url(RANK_SQL.format(start=rank_start)),
                        valid=lambda d: bool(explorer_rows(d)))
        self._ctx = {
            "patch": patch or major, "patch_ts": patch_ts, "major": major, "previous_major": previous,
            "heroes": const["heroes"], "items": const["items"], "abilities": const["abilities"],
            "hero_abilities": const["hero_abilities"], "ability_ids": const["ability_ids"], "patch_notes": notes,
            "hero_stats": hero_stats or [],
            "rank_pct": cards.rank_percentiles(explorer_rows(rank) or []), "public_start": start,
            "basis": {"public": public, "blended": blended, "public_from": start},
        }
        self._ctx_at = now
        return self._ctx

    def hero_ids(self) -> list[int]:
        heroes = self.context()["heroes"]
        return sorted(int(k) for k in heroes if str(k).isdigit()) if isinstance(heroes, dict) else []

    # one hero ---------------------------------------------------------------------------------------------
    def hero(self, hero_id: int) -> dict | None:
        """Fetch what's missing for one hero and build its card (saved). None if something couldn't be had
        now: what did come back stays cached for next time."""
        ctx = self.context()
        hid = int(hero_id)
        patch, major, ts = ctx["patch"], ctx["major"], float(ctx.get("patch_ts") or 0)
        blended = ctx["basis"].get("blended")
        if not major or not re.fullmatch(r"\d+\.\d+[a-z]?", patch or ""):
            self.log("[doty] study: the current patch isn't known yet (offline?)")
            return None
        bench = self.get(f"bench_{hid}.json", f"{OPENDOTA}/benchmarks?hero_id={hid}", newer_than=ts,
                         valid=lambda d: isinstance(d, dict) and bool(d.get("result")))
        timings = self.get(f"{STUDY_DIR}/itemtimes_{hid}.json", f"{OPENDOTA}/scenarios/itemTimings?hero_id={hid}",
                           newer_than=ts, valid=lambda d: isinstance(d, list) and bool(d))
        patches = [major]
        pro = self.get(f"{STUDY_DIR}/pro_{hid}_{major}.json",
                       _sql_url(PRO_SQL.format(hero_id=hid, patches=f"'{major}'")),
                       valid=lambda d: explorer_rows(d) is not None)
        pro_rows = explorer_rows(pro)
        pro_games = sum(int(cards._num(r.get("n"))) for r in pro_rows or [] if r.get("k") == "g")
        if pro_rows is not None and pro_games < PRO_MIN_GAMES and ctx.get("previous_major"):
            prev = ctx["previous_major"]
            both = self.get(f"{STUDY_DIR}/pro_{hid}_{prev}+{major}.json",
                            _sql_url(PRO_SQL.format(hero_id=hid, patches=f"'{prev}','{major}'")),
                            valid=lambda d: explorer_rows(d) is not None)
            if explorer_rows(both) is not None:
                pro_rows, patches = explorer_rows(both), [prev, major]
                pro_games = sum(int(cards._num(r.get("n"))) for r in pro_rows if r.get("k") == "g")
        pub = self.get(f"{STUDY_DIR}/pub_{hid}_{patch}_{ctx['public_start']}.json",
                       _sql_url(PUB_SQL.format(hero_id=hid, start=ctx["public_start"])),
                       ttl=THIN_TTL_S if blended else RAW_TTL_S, valid=lambda d: explorer_rows(d) is not None)
        if bench is None or timings is None or pro_rows is None or explorer_rows(pub) is None:
            return None
        ctx_hero = dict(ctx)
        ctx_hero["basis"] = {**ctx["basis"], "pro": f"{'+'.join(patches)}, {pro_games} games",
                             "pro_patches": patches, "pro_games": pro_games,
                             "pro_blended": len(patches) > 1}
        card = cards.build_card(hid, {"pro": pro_rows, "pub": explorer_rows(pub), "timings": timings,
                                      "bench": bench}, ctx_hero, now=self.now())
        ctx["_traits"] = ctx_hero.get("_traits", {})  # (kept for the next hero)
        cards.save(card, self.cache_dir)
        done = self.state.setdefault("done", {})
        done[str(hid)] = patch
        self._save_state()
        return card

    # many heroes ----------------------------------------------------------------------------------------
    def run(self, hero_ids: Iterable[int], max_calls: int | None = None,
            stop: threading.Event | None = None, force: bool = False) -> dict:
        """Study these heroes in order, skipping those whose card is current (unless `force`: built again,
        from the cached answers where they're fresh). Returns {"built": [...],
        "current": n, "failed": [...], "calls": n, "stopped": reason or ""}. Never raises for the network."""
        self.max_calls = None if max_calls is None else self.calls + max_calls
        out: dict[str, Any] = {"built": [], "current": 0, "failed": [], "calls": 0, "stopped": ""}
        start_calls = self.calls
        try:
            patch = self.context()["patch"]
            for hid in hero_ids:
                if stop is not None and stop.is_set():
                    out["stopped"] = "stopped"
                    break
                if not force and not cards.is_stale(cards.load(int(hid), self.cache_dir), patch, self.now()):
                    out["current"] += 1
                    continue
                card = self.hero(int(hid))
                (out["built"] if card else out["failed"]).append(int(hid))
        except Paused as exc:
            out["stopped"] = exc.reason
        finally:
            self.max_calls = None
            out["calls"] = self.calls - start_calls
            self._save_state()
        return out


def priority(current: Iterable[int] = (), favourites: Iterable[int] = (), everyone: Iterable[int] = ()) -> list[int]:
    """The heroes of the current match first, then the player's most-played heroes, then the rest; each once."""
    return list(dict.fromkeys(int(h) for group in (current, favourites, everyone) for h in group if int(h or 0) > 0))


def study_in_background(current: Iterable[int] = (), favourites: Iterable[int] = (),
                        stop: threading.Event | None = None, cache_dir: Path = CACHE_DIR, calls_per_step: int = 3,
                        gap_s: float = 20.0, study: Study | None = None) -> dict:
    """For the app, on a worker thread: a few requests (`calls_per_step`), a pause (`gap_s`), again, until every
    hero's card is current, the day's budget is spent, OpenDota keeps failing, or `stop` is set. Returns the
    last step's summary with every hero built along the way."""
    stop = stop or threading.Event()
    study = study or Study(cache_dir, sleep=lambda s: stop.wait(s))
    built: list[int] = []
    order: list[int] | None = None
    while not stop.is_set():
        if order is None:
            try:
                order = priority(current, favourites, study.hero_ids())
            except Paused as exc:
                return {"built": built, "stopped": exc.reason}
        result = study.run(order, max_calls=calls_per_step, stop=stop)
        built += result["built"]
        if result["stopped"] not in ("step_budget",):
            return {**result, "built": built}
        stop.wait(gap_s)
    return {"built": built, "stopped": "stopped"}


# --- command line ---------------------------------------------------------------------------------------------
def _resolve(names: list[str], cache_dir: Path) -> list[int]:
    out = []
    for name in names:
        hid = cards.hero_id_of(name, cache_dir)
        if hid:
            out.append(hid)
        else:
            print(f"unknown hero: {name}", file=sys.stderr)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m doty.knowledge.study", description=__doc__.split("\n")[0])
    ap.add_argument("heroes", nargs="*", help="hero ids or names (default: --all)")
    ap.add_argument("--all", action="store_true", help="every hero")
    ap.add_argument("--cache", type=Path, default=CACHE_DIR, help="cache folder (default: the app's)")
    ap.add_argument("--max-calls", type=int, default=None, help="stop after this many requests")
    ap.add_argument("--force", action="store_true", help="build cards again even when current")
    ap.add_argument("--print", action="store_true", help="print each card's prompt block")
    ap.add_argument("--json", action="store_true", help="print each card's structured view")
    ap.add_argument("--role", default=None, help="position 1-5, core or support (for --print)")
    ap.add_argument("--bracket", type=int, default=0, help="0 all ranks, 1 Herald ... 8 Immortal (for --print)")
    ap.add_argument("--mode", default="normal", choices=("normal", "turbo"))
    ap.add_argument("--enemies", default="", help="comma-separated enemy heroes (for --print)")
    args = ap.parse_args(argv)
    study = Study(args.cache)
    ids = study.hero_ids() if args.all or not args.heroes else _resolve(args.heroes, args.cache)
    if not ids:
        print("nothing to study (offline, or no heroes known yet)")
        return 1
    result = study.run(ids, max_calls=args.max_calls, force=args.force)
    print(f"built {len(result['built'])}, current {result['current']}, failed {len(result['failed'])}, "
          f"{result['calls']} requests" + (f", stopped: {result['stopped']}" if result["stopped"] else ""))
    role: Any = int(args.role) if args.role and args.role.isdigit() else args.role
    enemies = [e.strip() for e in args.enemies.split(",") if e.strip()]
    for hid in ids:
        if args.print:
            print(cards.prompt_block(hid, role, args.bracket, args.mode, enemies, args.cache) or f"(no card for {hid})")
            print()
        if args.json:
            print(json.dumps(cards.card(hid, role, args.bracket, args.mode, enemies, args.cache), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
