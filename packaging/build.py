"""Build Doty for other people: dist\\Doty\\Doty.exe, then dist\\DotySetup-<version>.exe.

    .venv\\Scripts\\pip install -e .[build]       (once: PyInstaller)
    .venv\\Scripts\\python packaging\\build.py      (everything)
    .venv\\Scripts\\python packaging\\build.py --no-installer      (just the Doty.exe folder)

The installer step needs Inno Setup 6 (https://jrsoftware.org/isinfo.php). Every build runs
Doty.exe --selftest (no window) and stops if the frozen app is missing anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))

from doty import __version__  # noqa: E402


def version_file(version: str) -> Path:
    nums = (version.split(".") + ["0", "0", "0"])[:4]
    t = ", ".join(str(int("".join(c for c in n if c.isdigit()) or 0)) for n in nums)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({t}), prodvers=({t}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Doty'),
      StringStruct('FileDescription', 'Doty: live Dota 2 coach'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Doty'),
      StringStruct('OriginalFilename', 'Doty.exe'),
      StringStruct('ProductName', 'Doty'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def wizard_art() -> None:
    """The installer's sidebar and corner images, drawn from Doty's backdrop and emblem."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from doty.ui import theme
    from doty.ui.brand import display_font, hairline, paint_backdrop, paint_emblem

    app = QApplication.instance() or QApplication([])  # noqa: F841 (fonts and SVG need it)
    for name, (w, h) in {"wizard.bmp": (164, 314), "wizard@2x.bmp": (328, 628)}.items():
        img = QImage(w, h, QImage.Format_RGB32)
        img.fill(QColor(theme.BG))  # the backdrop leaves its cut corner unpainted
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        paint_backdrop(p, QRectF(0, 0, w, h))
        s = w / 164
        paint_emblem(p, QRectF(w / 2 - 46 * s, 70 * s, 92 * s, 92 * s))
        p.setPen(QColor(theme.TEXT))
        p.setFont(display_font(round(26 * s), QFont.Bold, 4 * s))
        p.drawText(QRectF(0, 176 * s, w, 34 * s), Qt.AlignHCenter | Qt.AlignVCenter, "DOTY")
        p.setPen(QColor(theme.TEXT_2))
        p.setFont(display_font(round(9 * s), QFont.DemiBold, 2 * s))
        p.drawText(QRectF(0, 208 * s, w, 20 * s), Qt.AlignHCenter | Qt.AlignVCenter, "A COACH FOR DOTA 2")
        hairline(p, QRectF(0, 0, w, h), alpha=90)
        p.end()
        img.save(str(BUILD / name))
    for name, side in {"wizard-small.bmp": 55, "wizard-small@2x.bmp": 110}.items():
        img = QImage(side, side, QImage.Format_RGB32)
        img.fill(QColor("#ffffff"))  # Inno draws this on its white header
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        paint_emblem(p, QRectF(side * 0.06, side * 0.06, side * 0.88, side * 0.88))
        p.end()
        img.save(str(BUILD / name))


def pyinstaller() -> Path:
    shutil.rmtree(DIST / "Doty", ignore_errors=True)
    cmd = [sys.executable, "-m", "PyInstaller", str(HERE / "doty.spec"), "--noconfirm", "--clean",
           "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller")]
    print("[build] PyInstaller...", flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = DIST / "Doty" / "Doty.exe"
    assert exe.is_file(), exe
    return exe


def selftest(exe: Path) -> dict:
    report = BUILD / "selftest.json"
    report.unlink(missing_ok=True)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    print("[build] self-test (no window)...", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run([str(exe), "--selftest", str(report)], env=env, timeout=300)
    result = json.loads(report.read_text(encoding="utf-8"))
    for c in result["checks"]:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}: {c['detail']}")
        if not c["ok"] and c.get("trace"):
            print("        " + c["trace"].strip().replace("\n", "\n        "))
    print(f"[build] self-test {'passed' if result['ok'] else 'FAILED'} in {time.perf_counter() - t0:.1f} s")
    if proc.returncode != 0 or not result["ok"]:
        sys.exit("[build] stopping: the frozen app failed its self-test")
    return result


def find_iscc() -> Path | None:
    candidates = [os.environ.get("ISCC", "")] + [
        str(Path(os.environ.get(v, "")) / "Inno Setup 6" / "ISCC.exe")
        for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
    ] + [str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    found = shutil.which("ISCC")
    return Path(found) if found else None


def installer() -> Path:
    iscc = find_iscc()
    if iscc is None:
        sys.exit("[build] Inno Setup 6 isn't installed (https://jrsoftware.org/isinfo.php); "
                 "run with --no-installer to build just dist\\Doty")
    icon = sorted((ROOT / "doty" / "ui" / "assets").glob("doty-*.ico"))[-1]
    print("[build] Inno Setup...", flush=True)
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={__version__}", f"/DIconFile={icon}",
                    str(HERE / "doty.iss")], check=True, cwd=HERE)
    setup = DIST / f"DotySetup-{__version__}.exe"
    assert setup.is_file(), setup
    return setup


def size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-installer", action="store_true", help="stop after dist\\Doty")
    ap.add_argument("--reuse", action="store_true", help="skip PyInstaller; reuse dist\\Doty")
    args = ap.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    print(f"[build] Doty {__version__}")
    version_file(__version__)
    wizard_art()
    exe = DIST / "Doty" / "Doty.exe" if args.reuse else pyinstaller()
    selftest(exe)
    print(f"[build] {exe.parent}  ({size_mb(exe.parent):.0f} MB)")
    if args.no_installer:
        return 0
    setup = installer()
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    (DIST / f"{setup.name}.sha256").write_text(f"{digest}  {setup.name}\n", encoding="utf-8")
    print(f"[build] {setup}  ({size_mb(setup):.0f} MB)\n[build] sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
