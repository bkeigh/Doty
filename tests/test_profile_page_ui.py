"""The Profile page: its states (link your account, loading, error, no games, your games), its layout at
the window sizes Doty supports, and the pieces that count up and grow."""

import re
import time

from tests.test_app_ui import _fake_stats_service, pump, qapp, quiet_cfg  # noqa: F401 (qapp is a fixture)


def _games(n=6):
    from doty.knowledge.profile import Game

    now = int(time.time())
    heroes = [1, 1, 2, 1, 5, 2, 1, 5][:n]
    won = [True, True, False, True, False, True, False, True][:n]
    return [Game(100 + i, h, w, 5 + i, 2 + i % 3, 7, 450 + 10 * i, 500, 120, 2000 + 60 * i, now - 3600 * (i + 1),
                 None if i == 2 else 40.0 + 6 * i) for i, (h, w) in enumerate(zip(heroes, won))]


def _page(tmp_path, size=None):
    from doty.ui.controller import Controller
    from doty.ui.profile_page import ProfilePage

    page = ProfilePage(Controller(quiet_cfg(tmp_path)), _fake_stats_service())
    if size:
        page.resize(*size)
        page.show()  # (no account typed, so showing it loads nothing)
    return page


def _texts(page):
    from PySide6.QtWidgets import QLabel

    return " ".join(re.sub(r"<[^>]+>", "", lab.text()) for lab in page.findChildren(QLabel) if lab.isVisibleTo(page))


PLAYER = {"profile": {"personaname": "Tester", "avatarfull": "https://avatars.steamstatic.com/abc_full.jpg"},
          "rank_tier": 54}


def test_without_an_account_it_shows_how_to_link_one(qapp, tmp_path):
    page = _page(tmp_path)
    assert page._view.kind == "link"
    assert "Link your Dota account" in _texts(page) and "Expose Public Match Data" in _texts(page)
    page.account.setText("hello")
    page.load()
    assert page._view.kind == "link" and "not an account ID" in _texts(page)


def test_your_games_fill_the_header_form_heroes_and_list(qapp, tmp_path):
    from PySide6.QtGui import QColor

    from doty.ui.profile_page import _FormChart, _GameRow, _HeroCard, _PlayerHeader, rank_color

    page = _page(tmp_path)
    page._shown = 42
    games = _games()
    page._on_result("profile", (42, PLAYER, games))
    assert page._view.kind == "data"
    text = _texts(page)
    assert "Tester" in text and "Legend 4" in text and "4 of 5 stars" in text
    assert "4–2" in text and "67%" in text  # the record and win rate tiles, at their final values
    assert page._view.parts[1].a.streak.text() == "2 win streak"
    header = page.findChildren(_PlayerHeader)[0]
    assert header.bar == QColor(rank_color(54))  # the medal's color down the left
    chart = page.findChildren(_FormChart)[0]
    assert [g.match_id for g in chart._games] == [g.match_id for g in reversed(games)]  # oldest first
    heroes = page.findChildren(_HeroCard)
    assert heroes[0].name.full_text() == "Alpha" and "3 games" in _texts(heroes[0])
    rows = page.findChildren(_GameRow)
    assert len(rows) == len(games) and "WON" in text and "LOST" in text
    assert "5 / 2 / 7" in text  # K/D/A, deaths colored but the same words


def test_the_layout_fits_every_supported_window(qapp, tmp_path):
    from doty.ui.profile_page import MAX_W, _GamesCard, _MostPlayedCard, _PlayerHeader, _Split

    page = _page(tmp_path, (1340, 920))
    page._shown = 42
    page._on_result("profile", (42, PLAYER, _games(8)))
    for (w, h), side, tiles in (((1340, 920), True, 6), ((900, 700), False, 6), ((960, 1700), False, 6),
                                ((700, 900), False, 3), ((1920, 1080), True, 6)):
        page.resize(w, h)
        pump(qapp, 0.15)
        col = page.column
        left = col.mapTo(page, col.rect().topLeft()).x()
        assert left >= 30 and left + col.width() <= w - 10, (w, left, col.width())  # nothing clipped
        assert col.width() <= MAX_W
        if w >= MAX_W + 68:
            assert abs(left - (w - col.width()) / 2) <= 1  # centered on wide windows
        assert page.findChildren(_Split)[0].side_by_side() is side
        assert page.findChildren(_PlayerHeader)[0].tiles.columns() == tiles
        games = page.findChildren(_GamesCard)[0]
        assert games.compact is (col.width() < 790)
        for row in games.rows:
            assert row.width() <= col.width()
        most = page.findChildren(_MostPlayedCard)[0]
        assert most.count in (3, 4) and all(t.width() <= most.width() for t in most.tiles)
    page.close()


def test_numbers_count_up_and_bars_grow_when_shown(qapp, tmp_path):
    from doty.ui.profile_page import _FormChart, _StatTile

    page = _page(tmp_path, (1340, 920))
    page._shown = 42
    page._on_result("profile", (42, PLAYER, _games()))
    chart = page.findChildren(_FormChart)[0]
    assert chart.ease.t < 1.0  # it starts from nothing
    pump(qapp, 3.0, until=lambda: chart.ease.t >= 1.0)
    assert chart.ease.t == 1.0
    record = page.findChildren(_StatTile)[0].value
    pump(qapp, 1.0)
    assert re.sub(r"<[^>]+>", "", record.text()) == "4–2"
    page.close()


def test_errors_keep_the_games_you_had_and_late_answers_are_ignored(qapp, tmp_path):
    page = _page(tmp_path)
    page._on_result("profile", RuntimeError("HTTP 503"))  # nothing shown yet: a designed error with a retry
    assert page._view.kind == "error" and "Couldn't reach OpenDota" in _texts(page)
    page._shown = 42
    page._on_result("profile", (42, PLAYER, _games()))
    page._on_result("profile", RuntimeError("HTTP 503"))
    assert page._view.kind == "data" and "Couldn't refresh" in page.status.text()
    page._on_result("profile", (7, {"profile": {"personaname": "Someone"}}, _games()))  # an older request
    assert "Someone" not in _texts(page)
    page._on_result("profile", (42, {"profile": {"personaname": "Tester"}}, []))
    assert page._view.kind == "empty" and "No public games yet" in _texts(page) and "Unranked" in _texts(page)


def test_your_avatar_replaces_your_initials(qapp, tmp_path):
    from PySide6.QtGui import QColor, QImage

    from doty.ui.profile_page import _avatar_url_ok, _PlayerHeader

    assert _avatar_url_ok("https://avatars.steamstatic.com/abc_full.jpg")
    assert not _avatar_url_ok("http://avatars.steamstatic.com/abc_full.jpg")
    assert not _avatar_url_ok("https://example.com/abc_full.jpg")
    page = _page(tmp_path)
    page._shown = 42
    page._on_result("profile", (42, PLAYER, _games()))
    header = page.findChildren(_PlayerHeader)[0]
    assert not header.avatar.has_image()
    img = QImage(32, 32, QImage.Format_RGB32)
    img.fill(QColor("#336699"))
    page._on_result("profile_avatar", (42, img))
    assert header.avatar.has_image()
    page._on_result("profile", (42, PLAYER, _games(3)))  # drawn again: the avatar is remembered
    assert page._view.parts[0].avatar.has_image()
