"""Where Doty keeps your API keys between runs.

**Windows: Credential Manager** (Control Panel > Credential Manager > Windows Credentials, entries named
"Doty/<NAME>"). Windows encrypts them for your account. Doty up to 0.1.1 put the key in your user
environment instead (what `setx` does), where every program you start gets a copy; a key still there keeps
working, but a key saved in Settings from now on goes to Credential Manager only and wins over it.

**Linux:**
1. **The desktop's keyring** (GNOME Keyring, KWallet: the Secret Service), through the `keyring` package, when
   it's installed and a keyring is running. Encrypted, unlocked when you log in.
2. **Otherwise a file only you can read:** ~/.config/doty/keys.env (permissions 600), one NAME=key a line.
   Not encrypted: anyone with your user account can read it.

At start-up `load_into_environ()` puts the saved keys into this process's environment, where the rest of Doty
looks for them. On Linux a key already exported in your shell wins over a saved one. On Windows the saved one
wins: the environment may still hold the key an older Doty wrote there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE = "doty"
# Every key Doty asks for (coach/backends.PROVIDERS' key_env, and ElevenLabs'): tests check this list.
KNOWN = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY")


CRED_PREFIX = "Doty/"  # Credential Manager target names: "Doty/ANTHROPIC_API_KEY"


def _wincred():
    """pywin32's win32cred on Windows, else None."""
    if sys.platform != "win32":
        return None
    try:
        import win32cred
    except ImportError:
        return None
    return win32cred


def _cred_write(cred, name: str, key: str) -> None:
    target = CRED_PREFIX + name
    if key:
        cred.CredWrite({"Type": cred.CRED_TYPE_GENERIC, "TargetName": target, "UserName": name,
                        "CredentialBlob": key, "Persist": cred.CRED_PERSIST_LOCAL_MACHINE,
                        "Comment": "An API key saved in Doty's Settings"}, 0)
        return
    try:
        cred.CredDelete(target, cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        pass  # wasn't there


def _cred_read(cred, name: str) -> str:
    try:
        blob = cred.CredRead(CRED_PREFIX + name, cred.CRED_TYPE_GENERIC, 0).get("CredentialBlob")
    except Exception:
        return ""  # not saved
    if isinstance(blob, bytes):
        blob = blob.decode("utf-16-le", "ignore")
    return (blob or "").strip()


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


def save(name: str, key: str, ring=None, path: Path | None = None, cred=None) -> str:
    """Keep a key (or forget it, if `key` is empty). Returns where it went: "credential manager" (Windows),
    "keyring" or "file"."""
    key = key.strip()
    cred = _wincred() if cred is None else cred
    if cred:
        try:
            _cred_write(cred, name, key)
            return "credential manager"
        except Exception as exc:  # (not seen in practice) the private file still works
            print(f"[doty] Credential Manager unavailable ({exc.__class__.__name__}); saving the key to a file")
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


def load_into_environ(env=None, ring=None, path: Path | None = None, cred=None) -> list[str]:
    """Put saved keys into this process's environment. Returns the names loaded."""
    env = os.environ if env is None else env
    cred = _wincred() if cred is None else cred
    loaded = []
    if cred:  # Windows: a key saved in Settings wins over one an older Doty left in the user environment
        for name in KNOWN:
            value = _cred_read(cred, name)
            if value:
                env[name] = value
                loaded.append(name)
    ring = _keyring() if ring is None else ring
    path = path or keys_file()
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
