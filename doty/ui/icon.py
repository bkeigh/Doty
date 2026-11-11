"""Doty's app icon: the mark (assets/doty.svg: a cut-corner tile with a leaning cyan-and-white D), rendered
for every Windows icon slot."""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QPainter, QPixmap

from ..config import CACHE_DIR
from .brand import ASSETS, paint_emblem

# The .ico ships with the app, beside the emblem. Not in %LOCALAPPDATA%: when Doty is run from
# a packaged (MSIX) app such as a terminal or the Claude app, Windows silently redirects AppData
# writes into that app's private folder, Explorer can't see the file, and shortcuts show a blank
# page. Versioned file name: Explorer caches icons by path, so new artwork gets a new path.
ICO_VERSION = 9  # 9: the D's hairline seam fixed (2026-11-09)
ICO_PATH = ASSETS / f"doty-{ICO_VERSION}.ico"
ICO_FALLBACK = CACHE_DIR.parent / ICO_PATH.name  # if the install folder is read-only
PNG_PATH = CACHE_DIR.parent / f"doty-{ICO_VERSION}.png"  # Linux: menus and launchers read PNGs


def write_png(path: Path | None = None) -> Path:
    """The mark as a 256 px PNG, for Linux's .desktop entry (needs a Q(Gui)Application to build)."""
    path = path or PNG_PATH
    if path.exists():
        return path
    QGuiApplication.instance() or QGuiApplication([])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    render(256).save(str(tmp), "PNG")
    tmp.replace(path)
    for old in path.parent.glob("doty-*.png"):  # older artwork versions
        if old != path:
            old.unlink(missing_ok=True)
    return path


def render(size: int) -> QImage:
    """The mark at one size, drawn 4x and scaled down so small sizes stay crisp."""
    big = QImage(size * 4, size * 4, QImage.Format_ARGB32_Premultiplied)
    big.fill(Qt.transparent)
    p = QPainter(big)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    # At 16-24 px every pixel counts: let the tile fill the slot.
    pad = 0.0 if size <= 24 else size * 4 * 0.02
    paint_emblem(p, QRectF(pad, pad, size * 4 - 2 * pad, size * 4 - 2 * pad))
    p.end()
    return big.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(render(size)))
    return icon


ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _png(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _dib(img: QImage) -> bytes:
    """One icon frame as Windows writes it: a BITMAPINFOHEADER with double height, 32-bit BGRA
    rows bottom-up, then the 1-bit AND mask (set where the pixel is fully transparent).
    Pillow leaves the mask out while still doubling the height, so Windows reads past the frame,
    and parts of Explorer then fall back to a blank page."""
    n = img.width()
    a = img.convertToFormat(QImage.Format_ARGB32)  # straight alpha, BGRA in memory
    bits, stride = bytes(a.constBits()), a.bytesPerLine()
    rows = [bits[y * stride:y * stride + n * 4] for y in range(n)]
    mask_stride = (n + 31) // 32 * 4
    mask = bytearray()
    for row in rows:
        line = bytearray(mask_stride)
        for x in range(n):
            if row[x * 4 + 3] == 0:
                line[x // 8] |= 0x80 >> (x % 8)
        mask += line
    rows.reverse()
    mask_rows = [mask[y * mask_stride:(y + 1) * mask_stride] for y in range(n)][::-1]
    pixels, and_mask = b"".join(rows), b"".join(mask_rows)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(pixels) + len(and_mask), 0, 0, 0, 0)
    return header + pixels + and_mask


def ico_bytes(sizes=ICO_SIZES) -> bytes:
    """A standard multi-size .ico: BMP frames below 256 px and a PNG frame at 256 px (the
    layout Windows' own tools produce, which every part of Windows reads)."""
    frames = [(s, _png(render(s)) if s >= 256 else _dib(render(s))) for s in sizes]
    out = bytearray(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    for s, blob in frames:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for _, blob in frames:
        out += blob
    return bytes(out)


def write_ico(path: Path | None = None) -> Path:
    """The multi-size .ico for shortcuts, built from the mark if it isn't there yet
    (needs a Q(Gui)Application to build)."""
    if path is None:
        if ICO_PATH.exists():
            return ICO_PATH
        try:
            return write_ico(ICO_PATH)
        except OSError:
            return ICO_FALLBACK if ICO_FALLBACK.exists() else write_ico(ICO_FALLBACK)
    QGuiApplication.instance() or QGuiApplication([])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(ico_bytes())
    tmp.replace(path)  # never leave a half-written icon where Explorer can read it
    if path.parent == ASSETS:  # older artwork versions
        for old in ASSETS.glob("doty-*.ico"):
            if old != path:
                old.unlink(missing_ok=True)
    return path
