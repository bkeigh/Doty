"""Draft help from public hero statistics: counter picks, a matchup grid and ban suggestions.

How a pick is scored:
- **Matchup advantage:** OpenDota's hero-vs-hero results, taken from each enemy's side. Against an
  enemy E, a hero C's advantage is how much better C does against E than the two heroes' overall
  win rates predict.
- **Shrunk by sample size:** that advantage is shrunk toward 0 when there are few games, so a 60%
  over 40 games doesn't beat a solid 53% over 40,000.
- **Plus how strong the hero is:** its win rate at your rank. The score estimates the expected
  edge over a coin flip, so a strong hero with good matchups beats a weak one with great matchups.

All of it is hero-level public data. It never looks at who the players in your lobby are.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .stats import Stats

PRIOR_GAMES = 300  # matchup shrinkage: weight = games / (games + PRIOR_GAMES)
META_WEIGHT = 1.0  # the hero's own win rate at your rank counts in full


@dataclass
class Suggestion:
    hero_id: int
    name: str
    score: float  # expected edge over 50%: (win rate - 0.5) + summed matchup advantages
    win_rate: float  # at your rank
    vs: list[tuple[str, float]] = field(default_factory=list)  # (enemy, advantage), best first


def advantage(stats: Stats, hero_id: int, enemy_id: int) -> float | None:
    """How much better `hero_id` does against `enemy_id` than their overall win rates predict."""
    row = next((m for m in stats.matchups(enemy_id) if m.get("hero_id") == hero_id), None)
    if not row or not row.get("games_played"):
        return None
    games = row["games_played"]
    vs = 1.0 - row["wins"] / games  # the enemy's wins are this hero's losses
    expected = 0.5 + (stats.win_rate(hero_id)[0] - 0.5) - (stats.win_rate(enemy_id)[0] - 0.5)
    return (vs - expected) * games / (games + PRIOR_GAMES)


def suggest(stats: Stats, allies: list[int], enemies: list[int], banned: list[int] = (), bracket: int = 0,
            role: str = "any", top: int = 10) -> list[Suggestion]:
    taken = set(allies) | set(enemies) | set(banned)
    out = []
    for hid in stats.heroes:
        if hid in taken:
            continue
        if role == "core" and not stats.is_core(hid):
            continue
        if role == "support" and not stats.is_support(hid):
            continue
        wr, _games = stats.win_rate(hid, bracket)
        vs = []
        for e in enemies:
            adv = advantage(stats, hid, e)
            if adv is not None:
                vs.append((stats.name(e), adv))
        score = sum(a for _, a in vs) + META_WEIGHT * (wr - 0.5)
        out.append(Suggestion(hid, stats.name(hid), score, wr, sorted(vs, key=lambda x: -x[1])))
    out.sort(key=lambda s: -s.score)
    return out[:top]


def matrix(stats: Stats, allies: list[int], enemies: list[int]) -> list[list[float | None]]:
    """Rows: your heroes; columns: theirs; each cell your hero's advantage (None = no data)."""
    return [[advantage(stats, a, e) for e in enemies] for a in allies]


def ban_suggestions(stats: Stats, bracket: int = 0, exclude: list[int] = (), top: int = 6) -> list[tuple[int, float, float]]:
    """Heroes doing best at your rank and picked often enough to meet: [(hero id, win rate, pick rate)]."""
    rows = []
    for hid in stats.heroes:
        if hid in exclude:
            continue
        wr, games = stats.win_rate(hid, bracket)
        pr = stats.pick_rate(hid, bracket)
        if games < 500 or pr < 0.03:
            continue
        rows.append((hid, wr, pr, (wr - 0.5) * min(1.0, pr / 0.1)))
    rows.sort(key=lambda r: -r[3])
    return [(hid, wr, pr) for hid, wr, pr, _ in rows[:top]]


def draft_summary(stats: Stats, picks: dict, bracket: int = 0) -> dict:
    """The draft as the coach should see it: both teams, plus stat-backed counter picks and threats."""
    you = picks.get("you") or ""
    if you not in ("radiant", "dire"):
        return {"draft_read_so_far": picks}
    ids = {side: [h for h in (stats.find(n) for n in picks.get(side, [])) if h] for side in ("radiant", "dire")}
    allies, enemies = ids[you], ids["dire" if you == "radiant" else "radiant"]
    banned = [h for h in (stats.find(n) for n in picks.get("bans", [])) if h]
    out: dict = {"your_team": [stats.name(h) for h in allies], "enemy_team": [stats.name(h) for h in enemies]}
    if enemies:
        out["counter_picks_from_stats"] = [
            f"{s.name} ({s.score * 100:+.1f}: " + ", ".join(f"vs {n} {a * 100:+.1f}" for n, a in s.vs[:2]) + ")"
            for s in suggest(stats, allies, enemies, banned, bracket, top=6)]
    return out
