"""Build Doty for other people: dist\\Doty\\Doty.exe, then dist\\DotySetup-<version>.exe.

    .venv\\Scripts\\pip install -e .[build]       (once: PyInstaller)
    .venv\\Scripts\\python packaging\\build.py      (everything)
    .venv\\Scripts\\python packaging\\build.py --no-installer      (just the Doty.exe folder)

The installer step needs Inno Setup 6 (https://jrsoftware.org/isinfo.php). Every build runs
Doty.exe --selftest (no window) and stops if the frozen app is missing anything.

Code signing (docs/RELEASING.md, "Code signing") is switched on by one environment variable:

    DOTY_SIGN_ARGS   what goes between `signtool sign` and the file, for example
                     /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256
                     /dlib "<folder>/Azure.CodeSigning.Dlib.dll" /dmdf "<folder>/metadata.json"
    DOTY_SIGNTOOL    (optional) the full path of signtool.exe, if it isn't on PATH or in the Windows SDK

With it set, the build signs Doty.exe, has Inno Setup sign the uninstaller and the installer, and stops
unless Windows then reports a valid signature on each. Without it the build is unsigned and says so.
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


SIGN_ARGS = os.environ.get("DOTY_SIGN_ARGS", "").strip()


def find_signtool() -> Path | None:
    explicit = os.environ.get("DOTY_SIGNTOOL", "").strip().strip('"')
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    found = shutil.which("signtool")
    if found:
        return Path(found)
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    hits = sorted(kits.glob("10.*/x64/signtool.exe"))
    return hits[-1] if hits else None


def sign_prefix() -> str:
    """`"<signtool>" sign <your arguments>` (the file goes after it), or "" when signing isn't set up.
    Kept as one string so the quoting in DOTY_SIGN_ARGS reaches signtool exactly as written."""
    if not SIGN_ARGS:
        return ""
    tool = find_signtool()
    if tool is None:
        sys.exit("[build] DOTY_SIGN_ARGS is set but signtool.exe wasn't found: install the Windows SDK's "
                 "signing tools, or set DOTY_SIGNTOOL to its full path")
    return f'"{tool}" sign {SIGN_ARGS}'


def signed_by(path: Path) -> str:
    """Who Windows says signed this file ("" = no valid signature)."""
    from doty import authenticode

    return authenticode.publisher(path)


def sign(path: Path, prefix: str) -> str:
    print(f"[build] signing {path.name}...", flush=True)
    subprocess.run(f'{prefix} "{path}"', check=True)
    return must_be_signed(path)


def must_be_signed(path: Path) -> str:
    who = signed_by(path)
    if not who:
        sys.exit(f"[build] stopping: {path.name} was sent for signing, but Windows sees no valid signature on it")
    print(f"[build] {path.name} is signed by {who}")
    return who


def find_iscc() -> Path | None:
    candidates = [os.environ.get("ISCC", "")] + [
        str(Path(os.environ.get(v, "")) / "Inno Setup 6" / "ISCC.exe")
        for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")
    ] + [str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    found = shutil.which("ISCC")
    if found:
        return Path(found)
    for v in ("ProgramFiles(x86)", "ProgramFiles"):  # a later major version ("Inno Setup 7")
        hits = sorted(Path(os.environ.get(v) or "C:/none").glob("Inno Setup */ISCC.exe"))
        if hits:
            return hits[-1]
    return None


def inno_sign_args(prefix: str) -> list[str]:
    """Inno Setup's side of signing: it signs the uninstaller it generates and the finished installer
    itself, with the command given here ($q is a quote and $f the quoted file, in Inno's notation)."""
    if not prefix:
        return []
    return ["/DSign=1", "/Sdotysign=" + prefix.replace('"', "$q") + " $f"]


def installer(prefix: str = "") -> Path:
    iscc = find_iscc()
    if iscc is None:
        sys.exit("[build] Inno Setup 6 isn't installed (https://jrsoftware.org/isinfo.php); "
                 "run with --no-installer to build just dist\\Doty")
    icon = sorted((ROOT / "doty" / "ui" / "assets").glob("doty-*.ico"))[-1]
    print("[build] Inno Setup...", flush=True)
    subprocess.run([str(iscc), "/Q", f"/DAppVersion={__version__}", f"/DIconFile={icon}",
                    *inno_sign_args(prefix), str(HERE / "doty.iss")], check=True, cwd=HERE)
    setup = DIST / f"DotySetup-{__version__}.exe"
    assert setup.is_file(), setup
    return setup


def write_checksum(setup: Path) -> str:
    """The .sha256 published next to the installer. Always of the finished file: signing changes it."""
    digest = hashlib.sha256(setup.read_bytes()).hexdigest()
    (DIST / f"{setup.name}.sha256").write_text(f"{digest}  {setup.name}\n", encoding="utf-8")
    return digest


def finish(require_signed: bool) -> int:
    """After a signing service has put signed files back into dist (the GitHub release workflow): say who
    signed what, stop if that isn't one publisher on both, and write the checksum of the finished installer."""
    exe, setup = DIST / "Doty" / "Doty.exe", DIST / f"DotySetup-{__version__}.exe"
    for path in (exe, setup):
        if not path.is_file():
            sys.exit(f"[build] {path} isn't there: build it first")
    app, inst = signed_by(exe), signed_by(setup)
    print(f"[build] Doty.exe: {'signed by ' + app if app else 'NOT signed'}")
    print(f"[build] {setup.name}: {'signed by ' + inst if inst else 'NOT signed'}")
    if require_signed and (not app or app != inst):
        sys.exit("[build] stopping: both files must carry a valid signature from the same publisher")
    print(f"[build] sha256 {write_checksum(setup)}")
    return 0


def size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-installer", action="store_true", help="stop after dist\\Doty")
    ap.add_argument("--reuse", action="store_true", help="skip PyInstaller; reuse dist\\Doty")
    ap.add_argument("--finish", action="store_true",
                    help="build nothing: report who signed dist's files and write the installer's .sha256")
    ap.add_argument("--require-signed", action="store_true", help="with --finish: stop unless both are signed")
    args = ap.parse_args()
    if args.finish:
        return finish(args.require_signed)
    BUILD.mkdir(parents=True, exist_ok=True)
    print(f"[build] Doty {__version__}")
    version_file(__version__)
    wizard_art()
    prefix = sign_prefix()
    exe = DIST / "Doty" / "Doty.exe" if args.reuse else pyinstaller()
    publisher = sign(exe, prefix) if prefix else ""  # before the self-test: what's tested is what ships
    selftest(exe)
    print(f"[build] {exe.parent}  ({size_mb(exe.parent):.0f} MB)")
    if not prefix:
        already = signed_by(exe)  # a signing service may have signed Doty.exe between two runs of this script
        print(f"[build] Doty.exe is signed by {already}" if already else
              "[build] NOT code-signed (DOTY_SIGN_ARGS isn't set): Windows will warn people who download this")
    if args.no_installer:
        return 0
    setup = installer(prefix)
    if prefix and must_be_signed(setup) != publisher:
        sys.exit("[build] stopping: the installer and Doty.exe are signed by different publishers")
    digest = write_checksum(setup)
    print(f"[build] {setup}  ({size_mb(setup):.0f} MB)\n[build] sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
