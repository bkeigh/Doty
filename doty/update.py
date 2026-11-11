"""Updates: is there a newer Doty on the project's GitHub Releases page, and installing it in one click.

- **Check:** asks GitHub's public API for the latest release of `app.update_repo` ("owner/name"). Nothing
  is sent but the request itself. Off when `update_repo` is "".
- **Download:** the release's `DotySetup-<version>.exe`, checked against the `.sha256` file published next
  to it; a file that doesn't match is deleted, never run.
- **Install:** runs the installer, which closes Doty (it waits on Doty's AppMutex), updates it in place and
  can start it again. Only the installed Doty updates itself; a source checkout updates with git.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

API = "https://api.github.com"
TIMEOUT_S = 15


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    notes: str
    page_url: str
    installer_url: str
    installer_name: str
    sha256_url: str


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.2.10' -> (0, 2, 10). Anything that isn't a number counts as 0."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def version_key(text: str) -> tuple[int, ...]:
    """For comparing: '1.2.3' > '1.2.3rc2' == 'v1.2.3-rc.2' > '1.2.2'. A pre-release (any letters after
    the numbers: rc, beta, dev) comes before its final release."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)(.*)", text or "")
    if not m:
        return (0, 0, 0, 0, 0)
    nums = [int(n) for n in m.group(1).split(".")[:3]]
    nums += [0] * (3 - len(nums))
    rest = m.group(2)
    if not re.search(r"[A-Za-z]", rest):
        return (*nums, 1, 0)  # a final release
    pre = re.findall(r"\d+", rest)
    return (*nums, 0, int(pre[0]) if pre else 0)


def newer(candidate: str, current: str = __version__) -> bool:
    return version_key(candidate) > version_key(current)


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Doty-updater"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 (https URLs we build)
        return resp.read()


def check(repo: str, current: str = __version__, api: str = API) -> Release | None:
    """The latest release if it's newer than this Doty, else None. Raises UpdateError if GitHub can't be
    asked (offline, no such repository)."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise UpdateError("no update source set")
    try:
        data = json.loads(_get(f"{api}/repos/{repo}/releases/latest"))
    except Exception as exc:
        raise UpdateError(f"couldn't reach GitHub ({exc.__class__.__name__})") from exc
    tag = str(data.get("tag_name") or "")
    if not tag or not newer(tag, current):
        return None
    assets = {a.get("name", ""): a.get("browser_download_url", "") for a in data.get("assets") or []}
    wanted = f"DotySetup-{tag.lstrip('v')}.exe"  # the installer for this tag, if it's there by that name
    exe = wanted if wanted in assets else next(
        (n for n in assets if re.fullmatch(r"DotySetup-[\w.\-]+\.exe", n)), "")
    sha = assets.get(f"{exe}.sha256", "")
    if not exe or not sha:
        raise UpdateError(f"release {tag} has no installer with a checksum")
    return Release(tag.lstrip("v"), str(data.get("body") or "")[:2000], str(data.get("html_url") or ""),
                   assets[exe], exe, sha)


def download(release: Release, folder: Path, progress: Callable[[int, int], None] | None = None) -> Path:
    """Download the installer and check its SHA-256. Returns its path; deletes it and raises if it
    doesn't match the published checksum."""
    folder.mkdir(parents=True, exist_ok=True)
    expected = _get(release.sha256_url, "text/plain").decode("utf-8", "replace").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("the release's checksum file is unreadable")
    path = folder / release.installer_name
    digest = hashlib.sha256()
    req = urllib.request.Request(release.installer_url, headers={"User-Agent": "Doty-updater"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp, path.open("wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 16):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise UpdateError("the download didn't match its checksum, so it was deleted")
    return path


def install(installer: Path) -> None:
    """Start the installer (it asks Doty to close, updates it, and offers to start it again)."""
    subprocess.Popen([str(installer), "/SILENT", "/CLOSEAPPLICATIONS"], close_fds=True)  # noqa: S603
