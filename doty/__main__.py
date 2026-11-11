"""Command line: `python -m doty <command>` (or `doty <command>` once installed)."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

from .config import load_config


def _url(cfg) -> str:
    return f"http://{cfg.gsi.host}:{cfg.gsi.port}/"


def cmd_install(args, cfg) -> int:
    from .gsi import install

    try:
        path = install.install(cfg, Path(args.dota_dir) if args.dota_dir else None)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(f"Wrote {path}")
    state = install.launch_option_set()
    if state is True:
        print(f"Launch option {install.LAUNCH_OPTION} is already set. Restart Dota if it's running.")
    else:
        print(f"\nOne manual step: in Steam, right-click Dota 2 > Properties > General > Launch Options,\n"
              f"and add:  {install.LAUNCH_OPTION}\nThen (re)start Dota. Use Borderless Window display mode "
              f"so the overlay can draw over the game.")
    return 0


def cmd_uninstall(args, cfg) -> int:
    from .gsi import install

    removed = install.uninstall(Path(args.dota_dir) if args.dota_dir else None)
    print(f"Removed {removed}" if removed else "Nothing to remove.")
    return 0


def cmd_run(args, cfg) -> int:
    from .app import Doty

    if args.record:
        cfg.gsi.record_dir = args.record
    Doty(cfg, overlay=not args.no_overlay, voice=not args.no_voice, coach=not args.no_coach,
         listen=not args.no_listen, quiet=args.quiet).run()
    return 0


def cmd_demo(args, cfg) -> int:
    """Doty plus the scripted match in one process: see everything without Dota."""
    from .app import Doty
    from .sim import run_sim

    def start_sim(doty: Doty) -> None:
        def go() -> None:
            asyncio.run(run_sim(_url(cfg), cfg.gsi.auth_token, speed=args.speed, end_clock=args.minutes * 60))
            if args.exit_after:
                doty.request_stop()
        threading.Thread(target=go, name="doty-sim", daemon=True).start()

    Doty(cfg, overlay=not args.no_overlay, voice=not args.no_voice, coach=not args.no_coach,
         listen=not args.no_listen).run(after_start=start_sim)
    return 0


def cmd_mic(args, cfg) -> int:
    """Hold the push-to-talk key and speak; prints what Whisper heard and how fast."""
    import time

    from .listen import Recorder, VoiceInput, WhisperTranscriber

    class PrintCoach:
        def ask_question(self, question, _shot):
            pass  # VoiceInput already printed what it heard

    if args.list:
        import sounddevice as sd

        from .listen import HOSTAPI

        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and sd.query_hostapis(d["hostapi"])["name"] == HOSTAPI:
                print(f"{i:3d}  {d['name']}")
        return 0
    from .advice import AdviceBus

    bus = AdviceBus()
    bus.subscribe(lambda a: a.final and a.source == "player" and print(f"  -> {a.text}"))
    vi = VoiceInput(cfg.listen, bus, PrintCoach(), recorder=Recorder(cfg.listen.mic),
                    transcriber=WhisperTranscriber(cfg.listen.whisper_model, cfg.listen.device))
    print(f"Loading the speech model (first run downloads it)… mic: {vi.recorder.device_name}")
    vi.start()
    while not vi.ready.is_set() and vi.status.startswith("loading"):
        time.sleep(0.1)
    if not vi.ready.is_set():
        return 1
    print(f"Hold {cfg.listen.key} and talk; release to transcribe. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    vi.stop()
    return 0


def cmd_sim(args, cfg) -> int:
    from .sim import run_sim

    asyncio.run(run_sim(_url(cfg), cfg.gsi.auth_token, speed=args.speed, end_clock=args.minutes * 60))
    return 0


def cmd_replay(args, cfg) -> int:
    from .sim import replay_file

    asyncio.run(replay_file(Path(args.file), _url(cfg), speed=args.speed))
    return 0


def cmd_voices(args, cfg) -> int:
    from .voice import list_voices

    for v in list_voices():
        print(v)
    return 0


def cmd_gpu_voice(args, cfg) -> int:
    """Set up Kokoro on an NVIDIA GPU (source checkouts), then prove it works."""
    import time

    from . import kokoro, kokoro_gpu
    from .config import FROZEN

    if FROZEN:
        print("The installed Doty uses the CPU for Kokoro. GPU voice is for running Doty from source.")
        return 1
    if not args.check:
        if kokoro_gpu.install() != 0:
            print("pip failed; see the messages above.")
            return 1
    if not kokoro_gpu.available():
        print("The GPU voice runtime isn't usable: it needs an NVIDIA GPU and driver, plus cuBLAS and cuDNN "
              "(pip install -e .[gpu]).")
        return 1
    if not kokoro.installed():
        print("GPU runtime ready. Download the Kokoro voice in the app (Setup page), and Doty uses the GPU.")
        return 0
    t = time.perf_counter()
    k = kokoro.Kokoro(device="gpu")
    try:
        k.load()
        loaded = time.perf_counter() - t
        t = time.perf_counter()
        pcm = b"".join(k.synth("Their carry is missing from the map, so play safe.", "af_heart"))
        took = time.perf_counter() - t
    except kokoro.KokoroError as exc:
        print(f"Kokoro on the GPU failed: {exc}. Doty keeps using the CPU.")
        return 1
    finally:
        k.close()
    print(f"Kokoro runs on the GPU: started in {loaded:.1f} s, made {len(pcm) / 2 / kokoro.RATE:.1f} s of speech "
          f"in {took * 1000:.0f} ms. Restart Doty to use it.")
    return 0


def cmd_doctor(args, cfg) -> int:
    from .checks import run_checks

    print(f"Config: {cfg.source_path or 'defaults (no doty.toml)'}")
    checks = run_checks(cfg)
    for c in checks:
        mark = {True: "OK  ", False: "FAIL", None: "?   "}[c.ok]
        print(f"[{mark}] {c.label}{' — ' + c.detail if c.detail else ''}")
    return 0 if all(c.ok is not False for c in checks) else 1


def cmd_app(args, cfg) -> int:
    from .ui.main import main as app_main

    return app_main(cfg)


def cmd_shortcut(args, cfg) -> int:
    from . import shortcuts

    if args.remove:
        for path in shortcuts.remove_shortcuts():
            print(f"Removed {path}")
        return 0
    for path in shortcuts.install_shortcuts(desktop=not args.no_desktop):
        print(f"Created {path}")
    print("Open Doty from your applications menu." if sys.platform.startswith("linux") else
          "Open Doty from the Start menu, then right-click its taskbar icon > Pin to taskbar.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="doty", description="Doty — real-time Dota 2 coach")
    p.add_argument("--config", help="path to doty.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("install", help="write the GSI config into your Dota 2 folder")
    sp.add_argument("--dota-dir")
    sp.set_defaults(fn=cmd_install)

    sp = sub.add_parser("uninstall", help="remove Doty's GSI config")
    sp.add_argument("--dota-dir")
    sp.set_defaults(fn=cmd_uninstall)

    def add_run_flags(sp):
        sp.add_argument("--no-overlay", action="store_true")
        sp.add_argument("--no-voice", action="store_true")
        sp.add_argument("--no-coach", action="store_true", help="rules only, no Claude calls")
        sp.add_argument("--no-listen", action="store_true", help="disable push-to-talk questions")

    sp = sub.add_parser("run", help="start Doty and wait for Dota")
    add_run_flags(sp)
    sp.add_argument("--record", metavar="DIR", help="save raw GSI payloads for replay")
    sp.add_argument("--quiet", action="store_true", help="don't print advice to the console")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("demo", help="run Doty against a scripted fake match")
    add_run_flags(sp)
    sp.add_argument("--speed", type=float, default=10.0, help="game seconds per real second")
    sp.add_argument("--minutes", type=int, default=22, help="game length to simulate")
    sp.add_argument("--exit-after", action="store_true", help="quit when the match ends")
    sp.set_defaults(fn=cmd_demo)

    sp = sub.add_parser("sim", help="send the scripted match to an already-running Doty")
    sp.add_argument("--speed", type=float, default=10.0)
    sp.add_argument("--minutes", type=int, default=22)
    sp.set_defaults(fn=cmd_sim)

    sp = sub.add_parser("replay", help="replay a recorded .jsonl session into a running Doty")
    sp.add_argument("file")
    sp.add_argument("--speed", type=float, default=1.0)
    sp.set_defaults(fn=cmd_replay)

    sp = sub.add_parser("mic", help="test push-to-talk: hold the key, speak, see the transcript")
    sp.add_argument("--list", action="store_true", help="list microphones")
    sp.set_defaults(fn=cmd_mic)

    sub.add_parser("app", help="open the Doty window").set_defaults(fn=cmd_app)

    sp = sub.add_parser("shortcut", help="add Doty to the Start menu and desktop")
    sp.add_argument("--no-desktop", action="store_true", help="Start menu only")
    sp.add_argument("--remove", action="store_true", help="remove the shortcuts")
    sp.set_defaults(fn=cmd_shortcut)

    sub.add_parser("doctor", help="check your setup").set_defaults(fn=cmd_doctor)
    sub.add_parser("voices", help="list Windows speech voices (none on Linux)").set_defaults(fn=cmd_voices)
    sp = sub.add_parser("gpu-voice", help="run the Kokoro voice on an NVIDIA GPU (~0.15 s a line instead of ~0.8 s)")
    sp.add_argument("--check", action="store_true", help="only test it; don't install anything")
    sp.set_defaults(fn=cmd_gpu_voice)

    args = p.parse_args(argv)
    # Windows consoles default to a legacy code page; advice text uses em dashes.
    # (Under pythonw there is no console and these are None; the app logs to a file.)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform.startswith("linux"):
        from .keystore import load_into_environ

        load_into_environ()  # the API keys saved in Settings (Windows keeps them in the environment)
    cfg = load_config(Path(args.config) if args.config else None)
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
