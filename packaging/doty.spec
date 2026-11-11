# PyInstaller spec for Doty.exe (a folder build, which starts faster than one-file).
# Don't run this directly; use:  .venv\Scripts\python packaging\build.py
import os
from glob import glob

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ASSETS = os.path.join(ROOT, "doty", "ui", "assets")
ICON = sorted(glob(os.path.join(ASSETS, "doty-*.ico")))[-1]
BUILD = os.path.join(SPECPATH, "build")

datas = [(ASSETS, "doty/ui/assets")]  # icon + splash art (Higgsfield) and the .ico
datas += [(os.path.join(ROOT, "doty", "locales"), "doty/locales")]  # the app's translations
datas += collect_data_files("faster_whisper")  # the Silero voice-activity model
# Kokoro's text-to-phonemes: eSpeak NG's DLL and data (GPLv3; see docs/RELEASING.md). Only the
# English dictionary is kept (the others are ~17 MB). The Kokoro model is downloaded at first use.
datas += [d for d in collect_data_files("espeakng_loader")
          if not d[0].endswith("_dict") or os.path.basename(d[0]) == "en_dict"]
binaries = (collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")
            + collect_dynamic_libs("espeakng_loader"))
hiddenimports = collect_submodules("doty") + [
    "win32com.propsys", "win32com.shell", "win32com.client", "pythoncom", "pywintypes", "win32timezone",
]
excludes = [
    # NVIDIA's CUDA libraries are 2 GB; the build uses the CPU for speech recognition (~0.8 s per
    # question). Anyone with the CUDA 12 runtime installed system-wide still gets the GPU.
    "nvidia",
    "tkinter", "pytest", "IPython", "matplotlib",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.QtPdf",
    "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtDesigner", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtPositioning",
]

a = Analysis(
    [os.path.join(SPECPATH, "doty_app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
)
# Belt and braces: no CUDA DLLs even if a hook pulled them in.
a.binaries = [b for b in a.binaries if "nvidia" not in b[1].lower().replace("\\", "/").split("/")]
# Qt pieces a widgets app never loads (~50 MB): software OpenGL, PDF, QML/Quick, translations.
# Doty.exe --selftest (run by build.py) proves the window still builds without them.
QT_UNUSED = ("opengl32sw.dll", "qt6pdf", "qpdf.dll", "qt6quick", "qt6qml", "qt6qmlmodels", "qt6virtualkeyboard")


def _keep(entry) -> bool:
    name = entry[0].lower().replace("\\", "/")
    base = name.rsplit("/", 1)[-1]
    if name.startswith("pyside6/translations/"):
        return False
    return not any(base.startswith(u) or base == u for u in QT_UNUSED)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Doty",
    icon=ICON,
    version=os.path.join(BUILD, "version_info.txt"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Doty")
