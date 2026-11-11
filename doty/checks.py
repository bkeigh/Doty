"""Setup checks, shared by `doty doctor` and the app's Setup page."""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass

from .config import Config
from .i18n import tr


@dataclass
class Check:
    key: str
    label: str
    ok: bool | None  # True = good, False = broken, None = unknown / optional
    detail: str = ""


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ai_check(cfg: Config) -> Check:
    """Is the AI coach connected? (No network here: Doty checks the key itself when it starts.)"""
    from .coach.backends import PROVIDERS

    p = PROVIDERS.get(cfg.coach.provider, PROVIDERS["claude"])
    name = p.name or p.label
    if p.key == "claude":
        ok = has_api_key()
        return Check("key", "Claude API key", ok or None,
                     "found" if ok else "needed for the AI coach; alerts, timers and draft stats work without it")
    if p.key == "chatgpt":
        from .coach import chatgpt_auth

        email = chatgpt_auth.signed_in()
        return Check("key", "AI coach: ChatGPT", bool(email) or None,
                     f"signed in as {email}" if email else "not signed in yet: Continue with ChatGPT in Settings")
    if p.key == "ollama":
        return Check("key", "AI coach: local model (Ollama)", None,
                     "runs on this PC; needs Ollama running with a vision model (Settings > AI coach > Check)")
    ok = bool(os.environ.get(p.key_env, "").strip())
    return Check("key", f"AI coach: {name}", ok or None, "key found" if ok else f"add your {name} key in Settings")


def linux_checks(env=None, x_ok=None, which=None) -> list[Check]:
    """What works on this Linux desktop: the screen, overlay and hotkeys need X11 (or XWayland); the backup
    voice needs eSpeak NG or speech-dispatcher."""
    import shutil

    from . import x11

    env = os.environ if env is None else env
    kind = x11.session_kind(env)
    x_ok = x11.available() if x_ok is None else x_ok
    which = which or shutil.which
    if kind == "x11" and x_ok:
        screen = Check("desktop", "Screen capture, overlay and hotkeys", True, "X11")
    elif kind == "xwayland" and x_ok:
        screen = Check("desktop", "Screen capture, overlay and hotkeys", None,
                       "Wayland: through XWayland; if the coach's screenshots come back black, sign in to an "
                       "X11 session")
    else:
        screen = Check("desktop", "Screen capture, overlay and hotkeys", False,
                       "not available without X11 or XWayland yet: Doty coaches from the game data only")
    speaker = next((name for name in ("espeak-ng", "espeak", "spd-say") if which(name)), "")
    voice = Check("voices", "System speech voice", True if speaker else None,
                  speaker if speaker else "optional: install espeak-ng for a backup voice")
    return [screen, voice]


def run_checks(cfg: Config, *, doty_running: bool = False, gsi_seen: bool = False) -> list[Check]:
    from .gsi import install

    out: list[Check] = []
    dota = install.find_dota()
    out.append(Check("dota", "Dota 2 is installed", dota is not None,
                     str(dota) if dota else "Steam or Dota 2 not found"))
    if dota:
        cfg_path = dota / install.GSI_SUBDIR / install.CFG_NAME
        if not cfg_path.is_file():
            out.append(Check("gsi", "Dota connection file", False, "not installed yet"))
        else:
            matches = cfg_path.read_text(encoding="utf-8") == install.render_cfg(cfg)
            out.append(Check("gsi", "Dota connection file", True if matches else None,
                             "installed" if matches else "out of date with your settings; reinstall it"))
        video = dota / "game" / "dota" / "cfg" / "video.txt"
        if video.is_file():
            text = video.read_text(encoding="utf-8", errors="ignore")
            fullscreen = '"setting.fullscreen"\t\t"1"' in text
            out.append(Check("display", "Display mode works with the overlay", not fullscreen,
                             "switch Dota to Borderless Window" if fullscreen else "borderless or windowed"))
    lo = True if gsi_seen else install.launch_option_set()  # data arriving proves it's set
    out.append(Check("launch", "Steam launch option (lets Dota send live data)", lo,
                     "working: Doty is receiving Dota's data" if gsi_seen else
                     {True: "set", False: "not set in Steam yet", None: "couldn't read Steam's settings"}[lo]))
    with socket.socket() as s:
        free = s.connect_ex((cfg.gsi.host, cfg.gsi.port)) != 0
    if doty_running:
        out.append(Check("port", "Ready to receive Dota's data", True,
                         tr("Doty is running (port {port})", port=cfg.gsi.port)))
    else:
        out.append(Check("port", "Ready to receive Dota's data", free or None,
                         tr("port {port} is free", port=cfg.gsi.port) if free else
                         tr("port {port} is in use: is another Doty already running?", port=cfg.gsi.port)))
    out.append(ai_check(cfg))
    from .eleven import api_key as eleven_key

    has_eleven = bool(eleven_key())
    engine = cfg.voice.engine
    out.append(Check("eleven", "ElevenLabs voice (optional)", True if has_eleven else None,
                     ("key saved; Doty speaks with ElevenLabs" if engine in ("auto", "elevenlabs") else
                      "key saved, but Settings > Voice is set to another voice") if has_eleven else
                     "a human-sounding voice; add a key to turn it on"))
    from . import kokoro

    try:
        import espeakng_loader  # noqa: F401

        has_espeak = True
    except ImportError:
        has_espeak = False
    from . import languages

    lang = languages.known(getattr(cfg.app, "language", "en"))
    lang_name = languages.english_name(lang)
    has_kokoro = kokoro.installed()
    if not has_espeak:
        out.append(Check("kokoro", "Free natural voice: Kokoro (optional)", False,
                         "eSpeak NG is missing: pip install -e ."))
    elif not kokoro.speaks(lang):
        out.append(Check("kokoro", "Free natural voice: Kokoro (optional)", None,
                         f"it doesn't speak {lang_name}: ElevenLabs or your PC's own voice says it"))
    elif has_kokoro and not kokoro.installed(language=lang):
        out.append(Check("kokoro", "Free natural voice: Kokoro (optional)", None,
                         f"downloaded, but its {lang_name} voices aren't yet: one "
                         f"{kokoro.missing_mb(language=lang)} MB download"))
    elif has_kokoro:
        from . import kokoro_gpu

        speaking = engine == "kokoro" or (engine == "auto" and not has_eleven)
        on_gpu = kokoro_gpu.available() and cfg.voice.kokoro_device != "cpu"
        where = "on the GPU" if on_gpu else "on the CPU"
        out.append(Check("kokoro", "Free natural voice: Kokoro (optional)", True,
                         f"downloaded; Doty speaks with it, {where}" if speaking else
                         "downloaded; it takes over if ElevenLabs is out" if has_eleven and engine != "windows" else
                         "downloaded, but Settings > Voice is set to another voice"))
    else:
        out.append(Check("kokoro", "Free natural voice: Kokoro (optional)", None,
                         f"a human-sounding voice that runs on this PC, free; one {kokoro.DOWNLOAD_MB} MB download"))
    if sys.platform == "win32":
        from .voice import list_voices

        voices = list_voices()
        out.append(Check("voices", "Windows speech voices", bool(voices),
                         tr("{n} installed", n=len(voices))
                         + (tr("; the backup voice") if has_eleven or has_kokoro else "")))
    elif sys.platform.startswith("linux"):
        out.extend(linux_checks())
    if cfg.listen.enabled:
        try:
            import faster_whisper  # noqa: F401

            from .listen import Recorder, cuda_libs_available

            out.append(Check("mic", "Push-to-talk mic", True, Recorder(cfg.listen.mic).device_name))
            gpu = cuda_libs_available()
            out.append(Check("gpu", "Speech recognition on GPU", gpu or None,
                             "CUDA libraries found" if gpu else "CPU fallback (~0.8 s per question); see README"))
        except ImportError as exc:
            out.append(Check("mic", "Push-to-talk", False, f"{exc.name} not installed: pip install -e ."))
        except Exception as exc:
            out.append(Check("mic", "Push-to-talk mic", False, str(exc)))
    return out
