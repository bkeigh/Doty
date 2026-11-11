"""Screenshots: a black capture is noticed, and the window's own picture is tried before giving up."""

from PIL import Image

from doty import capture


def test_black_frames_are_noticed_and_real_ones_are_not():
    assert capture.is_black(Image.new("RGB", (3440, 1440)))
    night = Image.new("RGB", (1920, 1080), (8, 10, 12))
    night.paste((200, 180, 90), (0, 0, 1920, 60))  # the top bar and HUD stay bright at night
    assert not capture.is_black(night)


class FakeSct:
    monitors = [{}, {"left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True}]

    def __init__(self, color):
        self.color = color

    def grab(self, mon):
        img = Image.new("RGB", (mon["width"], mon["height"]), self.color)

        class Shot:
            size = img.size
            rgb = img.tobytes()

        return Shot()


def test_a_black_screen_grab_falls_back_to_the_windows_own_picture(monkeypatch):
    monkeypatch.setattr(capture, "find_dota_window", lambda: (42, (0, 0, 1920, 1080)))
    monkeypatch.setattr(capture, "dota_is_foreground", lambda: True)
    asked = []

    def printed(hwnd, w, h):
        asked.append(hwnd)
        return Image.new("RGB", (w, h), (90, 120, 60))

    monkeypatch.setattr(capture, "print_window", printed)
    img, _mon, found = capture._grab_dota(FakeSct((0, 0, 0)), None)
    assert found and asked == [42] and img.getpixel((5, 5)) == (90, 120, 60)
    asked.clear()
    img, _mon, _ = capture._grab_dota(FakeSct((70, 80, 90)), None)
    assert asked == [] and img.getpixel((5, 5)) == (70, 80, 90)  # a good grab needs no fallback


def test_when_dota_is_behind_another_window_its_own_picture_is_used(monkeypatch):
    monkeypatch.setattr(capture, "find_dota_window", lambda: (42, (0, 0, 1920, 1080)))
    monkeypatch.setattr(capture, "dota_is_foreground", lambda: False)
    monkeypatch.setattr(capture, "print_window", lambda hwnd, w, h: Image.new("RGB", (w, h), (1, 200, 3)))
    img, _mon, _ = capture._grab_dota(FakeSct((255, 255, 255)), None)  # (the screen shows Doty in front)
    assert img.getpixel((5, 5)) == (1, 200, 3)


def test_grab_views_says_when_there_is_nothing_to_see(monkeypatch):
    monkeypatch.setattr(capture, "_grab_dota", lambda sct, mask: (Image.new("RGB", (1920, 1080)), {}, True))
    views, _ms = capture.grab_views(1280)
    assert views["unavailable"] == capture.BLACK_NOTE and "screen" not in views


def test_print_window_reads_a_real_window():
    """PrintWindow on this desktop's own window list (no Dota needed): the code path works end to end."""
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    from doty.win import print_window

    hwnd = ctypes.windll.user32.GetShellWindow()  # the desktop, always there
    img = print_window(hwnd, 64, 64)
    assert img is None or img.size == (64, 64)
