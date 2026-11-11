"""Hero portraits and item and ability icons.

They come from Valve's public image CDN, the same pictures OpenDota and Dotabuff show. Each one is
downloaded the first time it's needed and cached on disk, so the pages work offline afterwards.

Nothing is bundled: the installer ships no Valve art, and Doty's own brand never uses it.
"""

from __future__ import annotations

import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..config import CACHE_DIR

CDN = "https://cdn.cloudflare.steamstatic.com"
ART_DIR = CACHE_DIR / "art"


def enabled() -> bool:
    """DOTY_ART=0 turns downloads off (the tests set it)."""
    return os.environ.get("DOTY_ART", "1") != "0"


class Art(QObject):
    arrived = Signal(str)  # an image's key, once it's downloaded (or failed for good)

    def __init__(self, folder: Path = ART_DIR, fetch=None) -> None:
        super().__init__()
        self.folder = folder
        self._fetch = fetch or _http_bytes
        self._images: dict[str, QImage] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="doty-art")

    def image(self, key: str, path: str) -> QImage | None:
        """The picture for `key` (like "hero/antimage") at CDN `path`, or None while it downloads."""
        img = self._images.get(key)
        if img is not None or not key:
            return img
        file = self.folder / f"{key}.png"
        if file.is_file():
            img = QImage(str(file))
            if not img.isNull():
                self._images[key] = img
                return img
        if not path or key in self._failed or not enabled():
            return None
        with self._lock:
            if key in self._pending:
                return None
            self._pending.add(key)
        self._pool.submit(self._download, key, path, file)
        return None

    def _download(self, key: str, path: str, file: Path) -> None:
        try:
            data = self._fetch(CDN + path.split("?")[0])
            img = QImage.fromData(data)  # QImage (unlike QPixmap) is safe off the GUI thread
            if img.isNull():
                raise ValueError("not an image")
            file.parent.mkdir(parents=True, exist_ok=True)
            part = file.with_suffix(".part")
            part.write_bytes(data)
            part.replace(file)
            self._images[key] = img
        except Exception:
            self._failed.add(key)  # offline or missing: the placeholder stays until the next start
        finally:
            with self._lock:
                self._pending.discard(key)
        try:
            self.arrived.emit(key)
        except RuntimeError:  # the app is closing
            pass

    # what to ask for -------------------------------------------------------------------
    @staticmethod
    def hero_key(stats, hero_id: int | None) -> tuple[str, str]:
        h = (stats.heroes.get(hero_id) or {}) if stats is not None and hero_id else {}
        npc = h.get("name", "").removeprefix("npc_dota_hero_")
        return (f"hero/{npc}", h.get("img", "")) if npc else ("", "")

    @staticmethod
    def item_key(stats, key: str) -> tuple[str, str]:
        key = (key or "").removeprefix("item_")
        it = (stats.items.get(key) or {}) if stats is not None and key else {}
        return (f"item/{key}", it.get("img", "")) if key else ("", "")

    @staticmethod
    def ability_key(stats, key: str) -> tuple[str, str]:
        a = (stats.abilities.get(key) or {}) if stats is not None and key else {}
        return (f"ability/{key}", a.get("img", "")) if key else ("", "")


_art: Art | None = None


def art() -> Art:
    """The app's one image cache (create it on the GUI thread)."""
    global _art
    if _art is None:
        _art = Art()
    return _art


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Doty (open-source Dota 2 coach)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()
