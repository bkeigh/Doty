"""Doty's Linux code paths, run here (on any system) with sys.platform, paths and the X11 layer faked.

These prove the Linux branches pick the right folders, commands and fallbacks. They can't prove X11, eSpeak NG
or a real Steam install behave as expected: that needs a run on a Linux desktop (docs/GUIDE.md, Linux).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from doty.config import Config, VoiceConfig


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    return monkeypatch


# folders ----------------------------------------------------------------------------------------------
def test_linux_keeps_settings_and_data_in_the_xdg_folders(tmp_path):
    from doty.config import user_dirs

    home = tmp_path / "home"
    assert user_dirs("linux", {}, home) == (home / ".config" / "doty", home / ".local" / "share" / "doty")
    env = {"XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_DATA_HOME": str(tmp_path / "data")}
    assert user_dirs("linux", env, home) == (tmp_path / "cfg" / "doty", tmp_path / "data" / "doty")


def test_windows_folders_are_unchanged(tmp_path):
    from doty.config import user_dirs

    env = {"APPDATA": str(tmp_path / "Roaming"), "LOCALAPPDATA": str(tmp_path / "Local")}
    assert user_dirs("win32", env, tmp_path) == (tmp_path / "Roaming" / "Doty", tmp_path / "Local" / "Doty")
    assert user_dirs("win32", {}, tmp_path) == (tmp_path / "Doty", tmp_path / "Doty")


def test_xdg_user_dirs_are_read_from_user_dirs_file(tmp_path):
    from doty.config import xdg_user_dir

    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "user-dirs.dirs").write_text(
        '# written by xdg-user-dirs-update\nXDG_DESKTOP_DIR="$HOME/Schreibtisch"\n'
        'XDG_DOCUMENTS_DIR="$HOME/Dokumente"\nXDG_MUSIC_DIR="$HOME/"\n', encoding="utf-8")
    assert xdg_user_dir("DOCUMENTS", {}, home) == Path(f"{home}/Dokumente")
    assert xdg_user_dir("DESKTOP", {}, home) == Path(f"{home}/Schreibtisch")
    assert xdg_user_dir("MUSIC", {}, home) is None  # set to $HOME itself: turned off
    assert xdg_user_dir("VIDEOS", {}, home) is None
    assert xdg_user_dir("DOCUMENTS", {}, tmp_path / "nobody") is None


# Steam and Dota -----------------------------------------------------------------------------------------
def test_linux_looks_for_steam_in_its_usual_places(tmp_path):
    from doty.gsi.install import steam_guesses

    guesses = steam_guesses("linux", tmp_path)
    assert guesses[0] == tmp_path / ".steam" / "steam"
    assert tmp_path / ".local" / "share" / "Steam" in guesses
    assert tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" in guesses  # Flatpak
    assert not any("Program Files" in str(g) for g in guesses)
    assert steam_guesses("win32", tmp_path)[0] == Path(r"C:\Program Files (x86)\Steam")


def test_linux_dota_install_is_found_across_libraries(tmp_path):
    from doty.gsi import install

    steam, games = tmp_path / "Steam", tmp_path / "games"
    (steam / "steamapps").mkdir(parents=True)
    # A leftover folder in the main library (cfg only), the real game in a second library.
    (steam / install.DOTA_SUBDIR / "game" / "dota").mkdir(parents=True)
    real = games / install.DOTA_SUBDIR
    (real / "game" / "dota").mkdir(parents=True)
    (real / "game" / "bin" / "linuxsteamrt64").mkdir(parents=True)
    (real / "game" / "bin" / "linuxsteamrt64" / "dota2").write_bytes(b"")
    (games / "steamapps" / "appmanifest_570.acf").write_text("", encoding="utf-8")
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{steam.as_posix()}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{games.as_posix()}"\n\t}}\n}}\n', encoding="utf-8")
    assert install.dota_installs(steam)[0] == real
    written = install.install(Config(), real)
    assert written == real / "game" / "dota" / "cfg" / "gamestate_integration" / install.CFG_NAME


def test_linux_launch_option_is_read_from_steam_userdata(tmp_path):
    from doty.gsi import install

    vdf = tmp_path / "userdata" / "123" / "config" / "localconfig.vdf"
    vdf.parent.mkdir(parents=True)
    vdf.write_text('"apps"\n{\n\t"570"\n\t{\n\t\t"LaunchOptions"\t\t"-novid -gamestateintegration"\n\t}\n}\n',
                   encoding="utf-8")
    assert install.launch_option_set(tmp_path) is True


# X11 ---------------------------------------------------------------------------------------------------
def test_x11_key_and_button_bits():
    from doty import x11

    assert x11.keysyms_for_vk(ord("A")) == (ord("a"),)
    assert x11.keysyms_for_vk(ord("5")) == (ord("5"),)
    assert x11.keysyms_for_vk(0x70) == (0xFFBE,)  # F1
    assert 0xFFE3 in x11.keysyms_for_vk(0x11)  # ctrl: Control_L
    assert x11.keysyms_for_vk(0x06) == ()  # mouse buttons aren't keys
    keymap = bytearray(32)
    keymap[38 >> 3] |= 1 << (38 & 7)
    assert x11.keycode_down(bytes(keymap), 38) and not x11.keycode_down(bytes(keymap), 39)
    assert not x11.keycode_down(bytes(keymap), 0)
    mask = bytes([0, 0b10])  # button 9 (forward: "mouse5")
    assert x11.button_in_mask(mask, x11.VK_BUTTONS[0x06]) and not x11.button_in_mask(mask, 8)
    assert not x11.button_in_mask(b"", 1)


def test_x11_session_kinds_and_qt_platform_choice():
    from doty import x11

    assert x11.session_kind({"DISPLAY": ":0"}) == "x11"
    assert x11.session_kind({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}) == "xwayland"
    assert x11.session_kind({"XDG_SESSION_TYPE": "wayland"}) == "wayland"
    assert x11.session_kind({}) == "none"
    env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}
    assert x11.prefer_xcb("linux", env) is True and env["QT_QPA_PLATFORM"] == "xcb"
    env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORM": "wayland"}
    assert x11.prefer_xcb("linux", env) is False and env["QT_QPA_PLATFORM"] == "wayland"  # the player's choice
    env = {"DISPLAY": ":0"}
    assert x11.prefer_xcb("linux", env) is False and "QT_QPA_PLATFORM" not in env  # plain X11: nothing to do
    env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}
    assert x11.prefer_xcb("win32", env) is False and "QT_QPA_PLATFORM" not in env


def test_x11_needs_a_display(linux):
    from doty import x11

    linux.delenv("DISPLAY", raising=False)
    linux.setattr(x11.X11._local, "conn", False, raising=False)
    assert x11.X11.get() is None and not x11.available()
    assert x11.dota_window("Dota 2") is None and x11.active_title() == ""
    assert not x11.key_down(0x41) and x11.held({0x41, 0x06}) == set()


def test_linux_finds_the_dota_window_through_x11(linux):
    from doty import win, x11

    linux.setattr(x11, "dota_window", lambda title: (77, (100, 50, 1920, 1080)) if title == "Dota 2" else None)
    linux.setattr(x11, "active_title", lambda: "Dota 2")
    assert win.find_dota_window() == (77, (100, 50, 1920, 1080))
    assert win.find_dota_rect() == (100, 50, 1920, 1080)
    assert win.dota_is_foreground()
    assert win.print_window(77, 1920, 1080) is None  # no PrintWindow on Linux: the screen grab is used
    linux.setattr(x11, "dota_window", lambda title: (77, (0, 0, 150, 90)))  # a tiny window isn't the game
    assert win.find_dota_window() is None
    linux.setattr(x11, "active_title", lambda: "Terminal")
    assert not win.dota_is_foreground()


def test_linux_without_x_says_the_screen_cant_be_seen(linux):
    from doty import capture, x11

    linux.setattr(x11, "available", lambda: False)
    views, _ms = capture.grab_views()
    assert views["unavailable"] == capture.NO_SCREEN_NOTE and "screen" not in views


# keys ----------------------------------------------------------------------------------------------------
def test_chords_become_keys_to_hold():
    from doty.hotkeys import chord_keys

    assert chord_keys("ctrl+alt+a") == [0x11, 0x12, ord("A")]
    assert chord_keys("shift+f9") == [0x10, 0x78]
    with pytest.raises(ValueError):
        chord_keys("ctrl+nope")


def test_linux_hotkeys_fire_once_per_press():
    from doty.hotkeys import Hotkeys

    fired, frames = [], [set(), {0x11, 0x12}, {0x11, 0x12, 0x41}, {0x11, 0x12, 0x41}, {0x11}, {0x11, 0x12, 0x41}]
    done = threading.Event()

    def held(codes):
        assert codes == {0x11, 0x12, 0x41, 0x50}
        if not frames:
            done.set()
            return set()
        return frames.pop(0)

    hk = Hotkeys()
    hk.bind("ctrl+alt+a", lambda: fired.append("ask"))
    hk.bind("ctrl+alt+p", lambda: fired.append("plan"))
    hk.bind("ctrl+bogus", lambda: fired.append("never"))
    hk._start_polling(held)
    assert done.wait(5)
    hk.stop()
    assert fired == ["ask", "ask"]  # held across ticks counts once; pressed again, fires again
    assert hk.failed == ["ctrl+bogus"]


def test_linux_hotkeys_without_x_report_every_chord_failed(linux):
    from doty import x11
    from doty.hotkeys import Hotkeys

    linux.setattr(x11, "available", lambda: False)
    hk = Hotkeys()
    hk.bind("ctrl+alt+a", lambda: None)
    hk.start()
    assert hk.failed == ["ctrl+alt+a"] and hk._thread is None
    hk.stop()


def test_linux_push_to_talk_polls_x11(linux):
    from doty import holdkey, x11

    asked = []
    linux.setattr(x11, "key_down", lambda vk: asked.append(vk) or vk == 0x06)
    assert holdkey._async_key_down(0x06) and not holdkey._async_key_down(0x05)
    assert asked == [0x06, 0x05]
    linux.setattr(x11, "available", lambda: False)
    key = holdkey.HoldKey("mouse5", lambda: None, lambda: None)
    key.start()
    assert key._thread is None  # no X server: nothing to poll
    linux.setattr(x11, "available", lambda: True)
    key.start()
    assert key._thread is not None
    key.stop()


# voice and sound ----------------------------------------------------------------------------------------
class FakeProc:
    def __init__(self, cmd, **_kw):
        self.cmd, self.done, self.killed = cmd, False, False

    def poll(self):
        return 0 if self.done or self.killed else None

    def terminate(self):
        self.killed = True


def test_system_speaker_prefers_espeak_ng():
    from doty.voice import SystemSpeaker

    procs = []
    found = {"espeak-ng": "/usr/bin/espeak-ng", "spd-say": "/usr/bin/spd-say"}
    sp = SystemSpeaker(VoiceConfig(rate=2, volume=90), "de", which=found.get,
                       popen=lambda cmd, **kw: procs.append(FakeProc(cmd)) or procs[-1])
    assert sp.kind == "espeak"
    assert sp.command("Hallo") == ["/usr/bin/espeak-ng", "-s", "199", "-a", "90", "-v", "de", "--", "Hallo"]
    sp.speak("one", purge=False)
    sp.speak("two", purge=True)  # cuts off the first
    assert procs[0].killed and not procs[1].killed and procs[1].cmd[-1] == "two"
    sp.wait(lambda: True)  # an interrupt ends the wait at once
    procs[1].done = True
    sp.wait(lambda: False)
    sp.close()


def test_system_speaker_falls_back_to_speech_dispatcher_then_gives_up():
    from doty.voice import SystemSpeaker

    sp = SystemSpeaker(VoiceConfig(rate=-10, volume=0), "zh", which={"spd-say": "/usr/bin/spd-say"}.get)
    assert sp.command("hi") == ["/usr/bin/spd-say", "--wait", "-r", "-100", "-i", "-100", "-l", "zh", "--", "hi"]
    assert SystemSpeaker(VoiceConfig(), "zh", which={"espeak": "/usr/bin/espeak"}.get).command("hi")[6] == "cmn"
    with pytest.raises(RuntimeError):
        SystemSpeaker(VoiceConfig(), which=lambda name: None)


def test_linux_voice_uses_the_system_speaker(linux):
    from doty.voice import SapiSpeaker, SystemSpeaker, Voice

    v = Voice(VoiceConfig())
    assert v.available and v._sapi_factory is SystemSpeaker
    linux.setattr(sys, "platform", "win32")
    assert Voice(VoiceConfig())._sapi_factory is SapiSpeaker  # Windows: unchanged


def test_linux_earcons_play_through_the_sound_server(linux, tmp_path):
    import subprocess

    from doty import listen

    assert listen.linux_player({"paplay": "/usr/bin/paplay", "aplay": "/usr/bin/aplay"}.get) == "/usr/bin/paplay"
    assert listen.linux_player(lambda name: None) is None
    played = []
    linux.setattr(listen, "CACHE_DIR", tmp_path)
    linux.setattr(listen, "linux_player", lambda which=None: "/usr/bin/pw-play")
    linux.setattr(subprocess, "Popen", lambda cmd, **kw: played.append(cmd))
    ear = listen.Earcons(True)
    assert ear.enabled
    ear.play(ear.start_wav)
    assert played == [["/usr/bin/pw-play", str(tmp_path / "ptt_start.wav")]]
    linux.setattr(listen, "linux_player", lambda which=None: None)
    assert not listen.Earcons(True).enabled  # nothing to play beeps with


# API keys ------------------------------------------------------------------------------------------------
def test_keystore_knows_every_key_doty_asks_for():
    from doty import eleven, keystore
    from doty.coach.backends import PROVIDERS

    wanted = {p.key_env for p in PROVIDERS.values() if p.key_env} | {eleven.KEY_ENV}
    assert wanted <= set(keystore.KNOWN)


def test_keystore_file_fallback_round_trip(tmp_path):
    from doty import keystore

    path = tmp_path / "doty" / "keys.env"
    assert keystore.save("ANTHROPIC_API_KEY", " sk-ant-test ", ring=False, path=path) == "file"
    keystore.save("ELEVENLABS_API_KEY", "el-test", ring=False, path=path)
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o777) == 0o600
    env = {"ELEVENLABS_API_KEY": "from-shell"}
    loaded = keystore.load_into_environ(env, ring=False, path=path)
    assert loaded == ["ANTHROPIC_API_KEY"] and env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["ELEVENLABS_API_KEY"] == "from-shell"  # an exported key wins
    keystore.save("ANTHROPIC_API_KEY", "", ring=False, path=path)  # removing a key
    assert "ANTHROPIC_API_KEY" not in path.read_text(encoding="utf-8")
    assert keystore.load_into_environ({}, ring=False, path=path) == ["ELEVENLABS_API_KEY"]


class FakeRing:
    def __init__(self, broken: bool = False):
        self.store, self.broken = {}, broken

    def set_password(self, service, name, value):
        if self.broken:
            raise RuntimeError("locked")
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name))


def test_keystore_prefers_the_desktop_keyring(tmp_path):
    from doty import keystore

    path, ring = tmp_path / "keys.env", FakeRing()
    keystore.save("OPENAI_API_KEY", "old", ring=False, path=path)  # from before a keyring was set up
    assert keystore.save("OPENAI_API_KEY", "sk-new", ring=ring, path=path) == "keyring"
    assert ring.store[("doty", "OPENAI_API_KEY")] == "sk-new"
    assert "sk-" not in path.read_text(encoding="utf-8") and "old" not in path.read_text(encoding="utf-8")
    env = {}
    assert keystore.load_into_environ(env, ring=ring, path=path) == ["OPENAI_API_KEY"]
    assert env["OPENAI_API_KEY"] == "sk-new"
    keystore.save("OPENAI_API_KEY", "", ring=ring, path=path)
    assert ("doty", "OPENAI_API_KEY") not in ring.store
    assert keystore.save("GEMINI_API_KEY", "g-key", ring=FakeRing(broken=True), path=path) == "file"  # locked


def test_linux_settings_save_keys_to_the_keystore(linux):
    from doty import keystore
    from doty.ui import controller

    saved = []
    linux.setattr(keystore, "save", lambda name, key: saved.append((name, key)))
    linux.delenv("OPENROUTER_API_KEY", raising=False)
    controller.save_api_key(" or-key ", "OPENROUTER_API_KEY")
    assert saved == [("OPENROUTER_API_KEY", "or-key")] and os.environ["OPENROUTER_API_KEY"] == "or-key"


# shortcuts ----------------------------------------------------------------------------------------------
def test_linux_shortcut_is_a_desktop_entry(linux, tmp_path):
    from doty import shortcuts

    linux.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert shortcuts.start_menu_path() == tmp_path / "data" / "applications" / "doty.desktop"
    from doty import config

    linux.setattr(config, "xdg_user_dir", lambda name: tmp_path / "Desk" if name == "DESKTOP" else None)
    assert shortcuts.desktop_path() == tmp_path / "Desk" / "doty.desktop"
    assert shortcuts.installed_shortcuts() == []
    target = "/home/me/my games/.venv/bin/python"
    link = shortcuts.create_shortcut(shortcuts.start_menu_path(), target, "-m doty app",
                                     Path("doty-9.png"), tmp_path)
    text = link.read_text(encoding="utf-8")
    assert '\nExec="/home/me/my games/.venv/bin/python" -m doty app\n' in text
    assert "\nStartupWMClass=Doty\n" in text and text.startswith("[Desktop Entry]\n")
    info = shortcuts.read_shortcut(link)
    assert info == {"target": target, "arguments": "-m doty app", "icon": "doty-9.png", "app_id": shortcuts.AUMID}
    assert shortcuts.installed_shortcuts() == [link]
    assert shortcuts.remove_shortcuts() == [link] and not link.exists()


def test_desktop_entry_quotes_what_the_spec_reserves():
    from doty.shortcuts import _exec_quote, read_desktop_entry

    assert _exec_quote("/usr/bin/python3") == "/usr/bin/python3"
    assert _exec_quote('/a "b"/$x') == '"/a \\"b\\"/\\$x"'
    assert read_desktop_entry('Exec="/a \\"b\\"/\\$x" -m doty app\n')["target"] == '/a "b"/$x'


def test_linux_adds_the_menu_entry_once(linux, tmp_path):
    from doty import config, shortcuts

    made = []
    linux.setattr(config, "CACHE_DIR", tmp_path / "doty" / "cache")
    linux.setattr(config, "FROZEN", False)
    linux.setattr(shortcuts, "installed_shortcuts", lambda: [])
    linux.setattr(shortcuts, "install_shortcuts", lambda desktop=True: made.append(desktop) or [])
    assert shortcuts.add_shortcuts_once() is True and made == [False]  # the menu only, no desktop icon
    assert shortcuts.add_shortcuts_once() is False


# setup checks and the overlay ---------------------------------------------------------------------------
def test_linux_setup_checks():
    from doty.checks import linux_checks

    def which(found):
        return lambda name: f"/usr/bin/{name}" if name in found else None

    screen, voice = linux_checks({"DISPLAY": ":0"}, x_ok=True, which=which({"espeak-ng"}))
    assert (screen.key, screen.ok, screen.detail) == ("desktop", True, "X11")
    assert (voice.key, voice.ok, voice.detail) == ("voices", True, "espeak-ng")
    screen, voice = linux_checks({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}, x_ok=True, which=which(()))
    assert screen.ok is None and "XWayland" in screen.detail
    assert voice.ok is None and "espeak-ng" in voice.detail
    screen, _ = linux_checks({"WAYLAND_DISPLAY": "wayland-0"}, x_ok=False, which=which(()))
    assert screen.ok is False and "game data" in screen.detail


def test_overlay_bypasses_the_window_manager_on_linux_only(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from doty import overlay

    QApplication.instance() or QApplication([])
    w = QWidget()
    monkeypatch.setattr(sys, "platform", "win32")
    overlay._overlay_flags(w)
    assert not w.windowFlags() & Qt.X11BypassWindowManagerHint
    assert w.windowFlags() & Qt.WindowTransparentForInput
    monkeypatch.setattr(sys, "platform", "linux")
    overlay._overlay_flags(w)
    assert w.windowFlags() & Qt.X11BypassWindowManagerHint
    assert w.windowFlags() & Qt.WindowStaysOnTopHint and w.windowFlags() & Qt.WindowTransparentForInput
    w.deleteLater()
