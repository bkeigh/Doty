"""A smoke test of a Doty build, with no window on screen: `Doty.exe --selftest report.json`.

It exercises what a frozen (PyInstaller) build tends to lose: Qt plugins (the JPEG splash
art), bundled assets, native libraries (PortAudio, CTranslate2, ONNX Runtime), Windows COM
(SAPI, shortcuts), TLS certificates for Claude and ElevenLabs, and the engine starting and
stopping its Game State Integration server. It writes a JSON report; exit code 0 = all passed.

On a build machine (GitHub Actions) there are no speakers, maybe no voices and no desktop to capture. With
DOTY_SELFTEST_NO_HARDWARE=1 the three checks that need those report "not checked" instead of failing.
Everything a build can get wrong (a missing library, plugin, asset or certificate) is still checked.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


def run(report: str | None = None) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"  # nothing appears on screen
    results: list[dict] = []
    no_hardware = bool(os.environ.get("DOTY_SELFTEST_NO_HARDWARE"))

    def check(name: str, hardware: bool = False):
        """`hardware`: this check needs a device a build machine may not have (see the module docstring)."""
        def wrap(fn):
            t0 = time.perf_counter()
            try:
                detail = fn()
                results.append({"check": name, "ok": True, "detail": detail or "",
                                "ms": round((time.perf_counter() - t0) * 1000)})
            except Exception as exc:
                if hardware and no_hardware:
                    results.append({"check": name, "ok": True, "ms": round((time.perf_counter() - t0) * 1000),
                                    "detail": f"not checked: no such device on this build machine ({exc!r})"})
                    return fn
                results.append({"check": name, "ok": False, "detail": f"{exc!r}",
                                "trace": traceback.format_exc()[-1500:],
                                "ms": round((time.perf_counter() - t0) * 1000)})
            return fn
        return wrap

    from . import __version__
    from .config import FROZEN, config_candidates

    @check("settings location")
    def _():
        path = config_candidates()[0]
        if FROZEN:
            assert "AppData" in str(path) and "Roaming" in str(path), path
        return str(path)

    @check("engine imports")
    def _():
        from . import app  # noqa: F401  (anthropic, aiohttp, rules, coach, overlay)
        import anthropic

        return f"anthropic {anthropic.__version__}"

    @check("key store and signature check")
    def _():
        from . import authenticode, keystore

        signed_by = authenticode.publisher(sys.executable)
        if sys.platform == "win32":
            assert keystore._wincred() is not None, "win32cred (Windows Credential Manager) is missing"
        return f"this build is signed by {signed_by}" if signed_by else "this build is not code-signed"

    @check("assets")
    def _():
        from .ui.brand import EMBLEM, FONT_FILES
        from .ui.icon import ICO_PATH

        files = [EMBLEM, ICO_PATH, *FONT_FILES]
        missing = [p.name for p in files if not p.is_file()]
        assert not missing, f"missing {missing}"
        return ", ".join(p.name for p in files)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    @check("brand (emblem, display font, splash)")
    def _():
        from PySide6.QtGui import QImage

        from .ui.brand import register_fonts
        from .ui.icon import render
        from .ui.splash import render_splash

        family = register_fonts()
        assert family == "Barlow Condensed", f"display font fell back to {family}"
        img = render(64)
        assert img.pixelColor(32, 40).alpha() > 200, "the emblem rendered empty (Qt SVG plugin missing?)"
        assert not render_splash().isNull()
        return f"{family}; emblem and splash render"

    @check("window builds")
    def _():
        from .ui import theme
        from .ui.controller import Controller
        from .ui.window import MainWindow

        theme.apply(app)
        cfg = quiet_config()
        ctl = Controller(cfg)
        win = MainWindow(ctl)
        win.resize(1340, 920)
        img = win.grab()
        assert img.width() > 1000
        assert ctl.start(), ctl.error  # GSI server up on a spare port, then down
        ok = ctl.status()["phase"] == "waiting"
        ctl.stop()
        win.coach_page.screen.stop()
        win.deleteLater()
        assert ok
        return "Coach/Setup/Settings pages render; engine starts and stops"

    @check("tls certificates")
    def _():
        import ssl

        ctx = ssl.create_default_context()
        n = ctx.cert_store_stats()["x509_ca"]
        assert n > 0, "no CA certificates: HTTPS to Claude/ElevenLabs would fail"
        try:  # a real handshake (no request sent); certificate errors fail, being offline doesn't
            with socket.create_connection(("api.anthropic.com", 443), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="api.anthropic.com") as tls:
                    version = tls.version()
        except ssl.SSLCertVerificationError:
            raise
        except OSError as exc:
            return f"{n} CA certs; handshake skipped (offline: {exc.__class__.__name__})"
        return f"{n} CA certs; {version} handshake with api.anthropic.com verified"

    @check("audio library (PortAudio)")
    def _():
        import sounddevice as sd  # loads the bundled PortAudio DLL: must work everywhere

        return sd.get_portaudio_version()[1]

    @check("audio output", hardware=True)
    def _():
        import sounddevice as sd

        out = sd.query_devices(kind="output")
        return out["name"]

    @check("windows voice (SAPI)", hardware=True)
    def _():
        from .voice import list_voices

        voices = list_voices()
        assert voices, "no SAPI voices"
        return f"{len(voices)} voices"

    @check("kokoro voice (eSpeak NG + ONNX Runtime)")
    def _():
        from . import kokoro

        import numpy as np

        ps = kokoro.phonemize("Bounty runes in fifteen seconds.")
        assert ps == "bˈWnti ɹˈunz ɪn fˈɪftin sˈɛkəndz.", ps
        t = np.arange(kokoro.RATE) / kokoro.RATE  # the Warm filter: a whistle at 4.8 kHz goes, 300 Hz stays
        sound = (0.3 * np.sin(2 * np.pi * 300 * t) + 0.05 * np.sin(2 * np.pi * 4800 * t)).astype(np.float32)
        spec = np.abs(np.fft.rfft(kokoro.clean(sound)))
        assert spec[4800] < 0.05 * np.abs(np.fft.rfft(sound))[4800] and spec[300] > 0.9 * np.abs(np.fft.rfft(sound))[300]
        if not kokoro.installed():
            return "phonemes and filter OK; the model isn't downloaded on this PC, so no speech test"
        k = kokoro.Kokoro()
        k.load()
        try:
            pcm = b"".join(k.synth("Roshan is up.", "af_heart"))
        finally:
            k.close()
        seconds = len(pcm) / 2 / kokoro.RATE
        assert 0.4 < seconds < 3, f"{seconds:.2f} s of speech"
        return f"phonemes and filter OK; spoke {seconds:.1f} s with the downloaded model"

    @check("shortcuts (COM)")
    def _():
        from .shortcuts import AUMID, create_shortcut, launch_command, read_shortcut, shortcut_icon

        target, args = launch_command()
        with tempfile.TemporaryDirectory() as tmp:
            link = create_shortcut(Path(tmp) / "Doty.lnk", target, args, Path(shortcut_icon()), Path(tmp))
            info = read_shortcut(link)
        assert info["app_id"] == AUMID and info["target"].lower() == target.lower()
        return f"{target} {args}".strip()

    @check("speech recognition (faster-whisper)")
    def _():
        import ctranslate2
        import faster_whisper
        import onnxruntime  # noqa: F401  (voice-activity filter)

        assets = Path(faster_whisper.__file__).parent / "assets"
        assert any(assets.glob("silero_vad*.onnx")), "VAD model missing"
        from .listen import cuda_libs_available

        return (f"faster-whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}, "
                f"{'GPU' if cuda_libs_available() else 'CPU'}")

    @check("screen capture", hardware=True)
    def _():
        import mss

        with mss.mss() as m:
            shot = m.grab({"left": 0, "top": 0, "width": 64, "height": 64})
        import ctypes

        from .win import print_window

        # The window's own picture (used when the screen grab of Dota comes back black): the desktop's.
        own = print_window(ctypes.windll.user32.GetShellWindow(), 32, 32)
        return f"{shot.width}x{shot.height}; window capture {'works' if own is not None else 'unavailable'}"

    passed = all(r["ok"] for r in results)
    out = {"ok": passed, "version": __version__, "frozen": FROZEN, "python": sys.version.split()[0],
           "executable": sys.executable, "checks": results}
    text = json.dumps(out, indent=2)
    if report:
        Path(report).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    return 0 if passed else 1


def quiet_config() -> Config:
    """Everything that would touch the user (voice, overlay, hotkeys, mic, Claude) off."""
    from .config import Config

    cfg = Config()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        cfg.gsi.port = s.getsockname()[1]
    cfg.voice.enabled = cfg.coach.enabled = cfg.listen.enabled = False
    cfg.overlay.enabled = cfg.hotkeys.enabled = False
    cfg.app.start_coaching_on_open = False
    cfg.source_path = Path(tempfile.gettempdir()) / "doty-selftest.toml"  # never the user's settings
    return cfg
