"""Where Doty keeps your API keys between runs on Linux.

Windows keeps them in your user environment (what `setx` does: ui/controller.save_api_key), so every app
started afterwards has them. Linux has no such place, so there:
1. **The desktop's keyring** (GNOME Keyring, KWallet: the Secret Service), through the `keyring` package, when
   it's installed and a keyring is running. Encrypted, unlocked when you log in.
2. **Otherwise a file only you can read:** ~/.config/doty/keys.env (permissions 600), one NAME=key a line.
   Not encrypted: anyone with your user account can read it, the same as the Windows user environment.

At start-up `load_into_environ()` puts the saved keys into the process environment, where the rest of Doty
looks for them. A key already set in the environment (exported in your shell) wins over a saved one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE = "doty"
# Every key Doty asks for (coach/backends.PROVIDERS' key_env, and ElevenLabs'): tests check this list.
KNOWN = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY")


def keys_file() -> Path:
    from .config import APPDATA_DIR

    return APPDATA_DIR / "keys.env"


def _keyring():
    """The keyring module when it has a working backend, else None (then the file is used)."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:
        return None
    return keyring


def _read_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            out[name.strip()] = value.strip()
    return out


def _write_file(path: Path, keys: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = "# Doty's API keys (Settings saves them here when there's no desktop keyring). Keep this private.\n"
    body += "".join(f"{name}={value}\n" for name, value in sorted(keys.items()) if value)
    # Created private (600) before the keys go in, then swapped into place.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(path)


def save(name: str, key: str, ring=None, path: Path | None = None) -> str:
    """Keep a key (or forget it, if `key` is empty). Returns where it went: "keyring" or "file"."""
    key = key.strip()
    ring = _keyring() if ring is None else ring
    path = path or keys_file()
    if ring:
        try:
            if key:
                ring.set_password(SERVICE, name, key)
            else:
                try:
                    ring.delete_password(SERVICE, name)
                except Exception:
                    pass  # wasn't there
            stored = _read_file(path)  # an older copy in the file must not come back
            if name in stored:
                stored.pop(name)
                _write_file(path, stored)
            return "keyring"
        except Exception as exc:  # a locked or broken keyring: the file still works
            print(f"[doty] keyring unavailable ({exc.__class__.__name__}); saving the key to {path}")
    stored = _read_file(path)
    if key:
        stored[name] = key
    else:
        stored.pop(name, None)
    _write_file(path, stored)
    return "file"


def load_into_environ(env=None, ring=None, path: Path | None = None) -> list[str]:
    """Put saved keys into the environment (Linux; Windows already has them there). Returns the names loaded."""
    env = os.environ if env is None else env
    ring = _keyring() if ring is None else ring
    path = path or keys_file()
    loaded = []
    stored = _read_file(path)
    for name in dict.fromkeys([*KNOWN, *stored]):
        if env.get(name, "").strip():
            continue
        value = ""
        if ring:
            try:
                value = ring.get_password(SERVICE, name) or ""
            except Exception:
                value = ""
        value = value or stored.get(name, "")
        if value:
            env[name] = value
            loaded.append(name)
    return loaded
