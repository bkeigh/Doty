"""Settings, loaded from doty.toml (project root or %APPDATA%\\Doty; ~/.config/doty on Linux) with defaults."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

# Running as the installed Doty.exe (a PyInstaller build) rather than from the source folder.
FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")


def user_dirs(platform: str = sys.platform, env=os.environ, home: Path | None = None) -> tuple[Path, Path]:
    """(the settings folder, the folder for everything else Doty keeps on this PC: cache, logs, models,
    sign-ins, reviews). Windows: %APPDATA%\\Doty and %LOCALAPPDATA%\\Doty. Linux: the XDG folders,
    ~/.config/doty and ~/.local/share/doty (one folder for the rest, since models and reviews aren't cache)."""
    home = home if home is not None else Path.home()
    if platform.startswith("linux"):
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
        return config / "doty", data / "doty"
    return Path(env.get("APPDATA", home)) / "Doty", Path(env.get("LOCALAPPDATA", home)) / "Doty"


APPDATA_DIR, _LOCAL_DIR = user_dirs()
CACHE_DIR = _LOCAL_DIR / "cache"
LOG_DIR = _LOCAL_DIR / "logs"


@dataclass
class GsiConfig:
    host: str = "127.0.0.1"
    port: int = 53000
    # Dota echoes this token back in every payload; anything else is rejected.
    # It only guards a 127.0.0.1 listener, so a fixed default is fine.
    auth_token: str = "doty-local"
    # Seconds. Dota batches changes for `buffer`, then waits at least `throttle`
    # between POSTs. 0.05/0.1 gives ~10 updates/s, the practical ceiling.
    buffer: float = 0.05
    throttle: float = 0.1
    heartbeat: float = 10.0
    record_dir: str = ""  # if set, raw payloads are appended to a .jsonl here


@dataclass
class RulesConfig:
    enabled: bool = True
    # Seconds of warning before a timed objective.
    rune_lead_s: int = 15
    stack_lead_s: int = 7
    announce_bounty_runes: bool = True
    announce_stacks: bool = False  # turn on if you play support/jungle
    announce_day_night: bool = False
    low_hp_pct: float = 0.30
    unspent_gold_threshold: int = 2500
    # The kind of match you play: "auto" (Doty works it out: what this match shows, else what your recent
    # matches were), "normal" or "turbo". Turbo runs about twice as fast (knowledge/mode.py), so item
    # timings, the enemy's power spikes, buyback and Roshan's timer all move.
    game_mode: str = "auto"
    muted_rules: list[str] = field(default_factory=list)


@dataclass
class SuggestConfig:
    """What Doty gives advice about. With a subject off, neither the instant calls nor the AI coach bring it
    up by themselves; asking the coach about it still gets an answer. (rules/engine.TOPIC_RULES lists which
    calls belong to which subject; staying-alive calls are under Alerts, not here.)"""

    items: bool = True  # what to buy: "Buy Ogre Axe", "You can afford ...", item pace, the coach's item advice
    skills: bool = True  # skill points to spend, level 6
    farm: bool = True  # last hits, the lane and farm pace
    timers: bool = True  # runes, shrines, lotus pools, neutral tiers, Roshan and Aegis
    enemy: bool = True  # the enemy's item timings, who the long game favours, pick-off windows
    gold: bool = True  # unspent gold, the stash, buyback, a missing TP scroll
    deaths: bool = True  # after each death: why, and what to change


@dataclass
class CoachConfig:
    enabled: bool = True
    # Sonnet 5.5: half Opus 5.5's price ($2/$10 vs $4/$20 per million tokens) and plenty for
    # short coaching calls. Low effort keeps time-to-first-token down.
    model: str = "claude-sonnet-5-5"
    # Live looks (the coach watching on its own) use a fast model, so tips land sooner; asks, plans and
    # questions use `model`. "" = use `model` for everything. Claude only.
    watch_model: str = "claude-haiku-5-5"
    # Doty's own top-bar reader (vision.py), about once a second in a match: "on" counts its steady reads
    # towards who's in the game (seconds after loading, instead of after several AI looks) and tells the
    # coach who's dead; "shadow" only logs what it reads next to the AI's reads; "off" doesn't run it.
    vision: str = "on"
    effort: str = "low"
    # Game-seconds between background looks at your screen + game state. Each
    # look keeps an instant answer ready for the ask hotkey, and with `live` on
    # the coach also speaks up when it sees something worth interrupting for.
    # 0 = only call Claude when you ask.
    speculative_interval_s: int = 30
    # What a look may say out loud: "important" (only when it's worth interrupting you),
    # "every" (a suggestion on every look), or "never" (the app's feed only).
    speak_up: str = "important"
    max_words: int = 28
    use_screenshot: bool = True
    screenshot_width: int = 1280
    minimap_side: str = "left"  # "right" if you use Dota's minimap-on-the-right HUD option
    # Your rank, for hero statistics: 0 = all ranks, 1 Herald ... 8 Immortal (set in Settings).
    bracket: int = 0
    # How much Dota the player knows (knowledge/lessons.LEVELS): "new" (never played: plain words, the basics said
    # out loud), "learning" (Herald-Guardian), "some" (Crusader-Legend), "experienced" (Ancient-Divine), "pro"
    # (Immortal: terse, fewer reminders). Sets what the coach says, what the rules say out loud, and the Learn tab.
    level: str = "some"
    # Follow the rank on the Profile page (level and rank for statistics) until you pick a level yourself.
    level_from_rank: bool = True
    # The AI behind the coach (coach/backends.py): claude | chatgpt | openai | gemini | openrouter | ollama.
    # "model" is the Claude model; the others use provider_model ("" = a sensible default).
    provider: str = "claude"
    provider_model: str = ""
    ollama_url: str = "http://127.0.0.1:11434/v1"
    # With no game data (launch option missing), still coach from screenshots while Dota is
    # the active window. Timers, alerts and exact stats need the game data.
    screen_only: bool = True
    # "When I go my own way" (rules/own_way.py): what Doty does when the player builds differently from its
    # suggestion. "once": it may disagree once per item per match, at a buying moment or when asked, and only
    # with a reason from this match (no reason: it says nothing), then follows the player. "follow": it never
    # argues, it follows. "remind": the old way (the coach up to three times, "you can afford it" twice).
    own_way: str = "once"
    # The player's own instructions to the coach, in their words (like custom instructions in a chat app).
    # Given to the AI on every look as preferences about style and priorities, fenced so they can't change the
    # fair-play rule, the reply format or Doty's checks. What can be a setting is taken out of it on saving
    # (coach/instructions.py) into the fields below, which the code enforces whether or not the AI is on.
    instructions: str = ""
    # Items Doty never suggests, in any match ("item_black_king_bar"): left out of the build like an item passed
    # on, by the rules and the coach alike. From the instructions, or "never suggest ... again" said out loud.
    never_suggest: list[str] = field(default_factory=list)
    # The player's own build per hero ("npc_dota_hero_spirit_breaker" -> ["item_boots_of_bearing"]): it leads
    # the build Doty points at for that hero, ahead of the coach's own plan.
    hero_builds: dict[str, list[str]] = field(default_factory=dict)
    # What the instructions said that isn't a setting (shown in Settings as "kept as style"; the AI reads the
    # whole text anyway).
    instructions_style: list[str] = field(default_factory=list)


@dataclass
class VoiceConfig:
    enabled: bool = True
    # "auto" = ElevenLabs when ELEVENLABS_API_KEY is set (human-sounding, paid per character),
    # else Kokoro once it's downloaded (human-sounding, free, runs on this PC), else the Windows
    # voice. "elevenlabs" / "kokoro" / "windows" force one. If ElevenLabs is out, Kokoro takes
    # over when downloaded; the Windows voice is always the last fallback.
    engine: str = "auto"
    eleven_voice: str = ""  # ElevenLabs voice ID; "" = pick a good one from your account
    eleven_model: str = "eleven_flash_v2_5"  # ~100 ms, the cheaper low-latency model
    kokoro_voice: str = "af_heart"  # see doty/kokoro.py VOICES
    # Kokoro's voice in the app's language when that isn't English (kokoro.py LANG_VOICES: Spanish, Brazilian
    # Portuguese, French). "" or a voice of another language = that language's first voice.
    kokoro_lang_voice: str = ""
    # "warm" removes the whistle Kokoro's vocoder leaves (4.8 and 9.6 kHz), tilts its fizzy top end down toward
    # natural speech and tames harsh "s" sounds; "clear" does the same with a brighter top; "bright" is its raw sound.
    # (Kitten uses the same setting.)
    kokoro_tone: str = "warm"
    # Kitten: another free voice on this PC (KittenML, Apache 2.0), softer and darker than Kokoro.
    # Engine "kitten" picks it. See doty/kitten.py VOICES.
    kitten_voice: str = "jasper"
    # "auto" = the NVIDIA GPU when `doty gpu-voice` set it up (~0.15 s a line), else the CPU
    # (~0.6-1.1 s a line). "cpu" forces the CPU.
    kokoro_device: str = "auto"
    rate: int = 2  # speaking speed, -10..10 (Windows voice rate; ElevenLabs maps it to 0.7-1.2x)
    volume: int = 90  # 0..100
    voice: str = ""  # Windows voice: substring of an installed voice name; "" = system default
    min_priority: int = 3  # speak advice with priority <= this (1 = critical)


@dataclass
class OverlayConfig:
    enabled: bool = True
    anchor: str = "right"  # right | left | top. Cards sit below the hero bar, clear of the HUD (hud.py)
    # Where you dragged the cards to (by their status badge), as [x, y] fractions of the game window;
    # empty = the anchor's usual spot. Double-click the badge to reset.
    position: list[float] = field(default_factory=list)
    size: float = 1.0  # card and text size on top of the automatic scaling with the game window
    draw: bool = True  # let the coach draw circles and arrows over the game to point things out
    mark_width: float = 2.0  # the drawings' line, in px at 1080p (it scales with the window)
    mark_opacity: float = 0.65  # how solid the drawings' lines are, 0..1 (their labels stay readable)
    # The live strip above the cards: countdowns to the next runes and objectives, and the item you're
    # building with the gold still to pay.
    strip: bool = True
    max_cards: int = 3
    ttl_s: float = 7.0
    opacity: float = 1.0  # (unused: the card plates carry the transparency)
    # Hide the overlay from all screen capture (OBS display capture, Discord
    # share) via SetWindowDisplayAffinity. Off by default: Doty is a coach, not
    # something to hide. Doty's own screenshots mask the overlay out either way.
    exclude_from_capture: bool = False
    show_latency: bool = False


@dataclass
class ListenConfig:
    """Push-to-talk questions: hold `key`, speak, release; Whisper transcribes locally."""

    enabled: bool = True
    # Held, not tapped. Mouse side buttons are unbound in Dota by default.
    # Also accepts keys/chords like "ctrl+alt+t" (the game still sees the key).
    key: str = "mouse5"
    engine: str = "whisper"  # whisper | off
    # auto = small.en on an NVIDIA GPU, base.en on CPU. Downloaded once on first use.
    whisper_model: str = "auto"
    device: str = "auto"  # auto | cuda | cpu
    mic: str = ""  # substring of an input device name; "" = Windows default mic
    min_s: float = 0.35  # shorter presses are treated as accidental
    max_s: float = 15.0
    earcons: bool = True  # short beeps on press/release


@dataclass
class HotkeyConfig:
    enabled: bool = True
    ask: str = "ctrl+alt+a"
    plan: str = "ctrl+alt+p"
    mute: str = "ctrl+alt+m"
    toggle_overlay: str = "ctrl+alt+h"
    # "Not that one": leaves the item Doty is pointing at out of this match's build and moves to the next.
    skip_item: str = "ctrl+alt+x"


@dataclass
class AppConfig:
    """The Doty window (`doty app`)."""

    start_coaching_on_open: bool = True
    close_to_tray: bool = True  # closing the window keeps Doty coaching from the tray
    # The window follows the game: Draft opens for hero selection, Coach when the match starts.
    follow_game: bool = True
    ai_step_skipped: bool = False  # "Not now" on Get started's AI key step
    # Your Dota account (for the Profile page): learned from the game data, or typed in. 0 = not known yet.
    account_id: int = 0
    # Updates from GitHub Releases ("owner/name"); "" = off. (update.py)
    update_repo: str = "bkeigh/Doty"
    # Doty's language (languages.LANGUAGES): the screens, the alerts, what the AI coach writes and says, and
    # what push-to-talk listens for. Which voices speak it: languages.py.
    language: str = "en"
    # You've chosen a language (the first-run choice, the flag in the top bar, or Settings); until then the
    # choice is offered once when Doty opens.
    language_picked: bool = False


@dataclass
class CaptureConfig:
    """Capture mode: save frames and the game data of a match on this PC only, to calibrate Doty's screen
    readers on your real HUD and to look into odd coach moments. Off unless you turn it on."""

    enabled: bool = False
    every_s: float = 30.0  # a frame this often in a match, plus at deaths, fights and the horn
    folder: str = ""  # "" = Documents\Doty\captures


def documents_dir() -> Path:
    """The user's Documents folder (wherever OneDrive or a policy moved it)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # CSIDL_PERSONAL
            return Path(buf.value)
    if LINUX:
        found = xdg_user_dir("DOCUMENTS")
        if found is not None:
            return found
    return Path.home() / "Documents"


def xdg_user_dir(name: str, env=os.environ, home: Path | None = None) -> Path | None:
    """A Linux desktop's own folder for DOCUMENTS, DESKTOP and so on (named in the user's language), from
    ~/.config/user-dirs.dirs, which xdg-user-dirs writes; None if it isn't set."""
    home = home if home is not None else Path.home()
    path = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "user-dirs.dirs"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == f"XDG_{name}_DIR" and value:
            value = value.strip().strip('"').replace("$HOME", str(home))
            if value.rstrip("/") != str(home).rstrip("/"):  # "$HOME" itself means the folder is turned off
                return Path(value)
    return None


@dataclass
class Config:
    gsi: GsiConfig = field(default_factory=GsiConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    coach: CoachConfig = field(default_factory=CoachConfig)
    suggest: SuggestConfig = field(default_factory=SuggestConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    app: AppConfig = field(default_factory=AppConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    source_path: Path | None = None
    load_problem: str = ""  # why the settings file couldn't be read as written (shown in the app)


def _same_kind(default: Any, value: Any) -> bool:
    """A value from the file fits a setting: a number for a number, text for text, a list for a list."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, (list, tuple)):
        return isinstance(value, (list, tuple))
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _apply(obj: Any, data: dict[str, Any], where: str = "", skipped: list[str] | None = None) -> None:
    """Copy settings from the file onto `obj`, skipping any whose value doesn't fit (`skipped` lists them)."""
    for f in fields(obj):
        if f.name not in data:
            continue
        current, value = getattr(obj, f.name), data[f.name]
        name = f"{where}{f.name}"
        if is_dataclass(current):
            if isinstance(value, dict):
                _apply(current, value, name + ".", skipped)
            elif skipped is not None:
                skipped.append(name)
        elif _same_kind(current, value):
            setattr(obj, f.name, value)
        elif skipped is not None:
            skipped.append(name)


def config_candidates() -> list[Path]:
    if FROZEN:  # the install folder belongs to the installer; settings live with the user
        return [APPDATA_DIR / "doty.toml"]
    return [PROJECT_ROOT / "doty.toml", APPDATA_DIR / "doty.toml"]


def is_user_config(cfg: Config) -> bool:
    """True for your real settings, False for a throwaway config (tests, previews). Things that change
    your PC on their own, like adding shortcuts or rewriting Dota's connection file, check this first."""
    return cfg.source_path is None or cfg.source_path in config_candidates()


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    for candidate in [path] if path else config_candidates():
        if candidate and candidate.is_file():
            # utf-8-sig: Notepad and PowerShell like to add a BOM, which tomllib rejects.
            text = candidate.read_text(encoding="utf-8-sig")
            skipped: list[str] = []
            try:
                _apply(cfg, tomllib.loads(text), skipped=skipped)
            except tomllib.TOMLDecodeError as exc:
                # Keep the file as you wrote it: the next save would otherwise replace it with defaults.
                backup = candidate.with_name(candidate.name + ".broken")
                try:
                    backup.write_text(text, encoding="utf-8")
                except OSError:
                    backup = None
                cfg.load_problem = (f"Doty couldn't read {candidate.name} ({exc}), so it's using default settings"
                                    + (f". Your file is kept as {backup.name}." if backup else "."))
                print(f"[doty] {cfg.load_problem}")
            if skipped:
                cfg.load_problem = cfg.load_problem or (
                    f"Some settings in {candidate.name} had the wrong kind of value and were left at their "
                    f"defaults: {', '.join(skipped[:6])}.")
                print(f"[doty] {cfg.load_problem}")
            cfg.source_path = candidate
            break
    from .languages import known

    cfg.app.language = known(cfg.app.language)  # ("EN", "pt-BR": the voices and the screens must agree on it)
    return cfg


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):  # an inline table, keys quoted ("npc_dota_hero_x" = [...])
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)} = {_toml_value(v)}"
                               for k, v in value.items()) + "}"
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(str(value), ensure_ascii=False)


def dump_config(cfg: Config) -> str:
    """TOML holding only the settings that differ from the defaults."""
    defaults = Config()
    out = ["# Doty settings. Anything not listed uses the default (see doty.example.toml).", ""]
    for section in fields(cfg):
        current, default = getattr(cfg, section.name), getattr(defaults, section.name)
        if not is_dataclass(current):
            continue
        changed = [(f.name, getattr(current, f.name)) for f in fields(current)
                   if getattr(current, f.name) != getattr(default, f.name)]
        if changed:
            out.append(f"[{section.name}]")
            out += [f"{name} = {_toml_value(value)}" for name, value in changed]
            out.append("")
    return "\n".join(out)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or cfg.source_path or config_candidates()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config(cfg), encoding="utf-8")
    cfg.source_path = path
    return path
