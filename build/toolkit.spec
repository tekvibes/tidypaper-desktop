# PyInstaller spec (one-dir, windowed).  The app's name and version come from brand.json, plus any one-off
# BRAND_NAME / BRAND_VERSION ... set by  build_installer.bat name="Something".
#
#   py -m PyInstaller build/toolkit.spec --noconfirm --workpath build/work --distpath build/dist
#
# (build_installer.bat does this for you.)  Output: build/dist/<Name>/<Name>.exe, e.g. build/dist/TidyPaper/TidyPaper.exe

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

ROOT = Path(SPECPATH).resolve().parent          # repo root (this file lives in build/)

import json, sys
from PyInstaller.config import CONF
sys.path.insert(0, str(ROOT))
import brand
BRAND = brand.for_build()
STEM = brand.file_stem(BRAND["name"])
VERSION = tuple((list(map(int, BRAND["version"].split("."))) + [0, 0, 0, 0])[:4])
# the app reads its name from a brand.json bundled next to ui/ - write the merged one for this build
BRAND_FILE = Path(CONF["workpath"]) / "brand.json"
BRAND_FILE.parent.mkdir(parents=True, exist_ok=True)
BRAND_FILE.write_text(json.dumps(BRAND, indent=2), encoding="utf-8")
print(f"Building {BRAND['name']} {BRAND['version']} -> {STEM}.exe")

datas, binaries, hiddenimports = [], [], []

# --- the web UI (index.html, css, js, fonts) lands in <bundle>/_internal/ui
datas.append((str(ROOT / "ui"), "ui"))
datas.append((str(BRAND_FILE), "."))
datas.append((str(ROOT / "prompts.json"), "."))     # what the AI is told (shared with the iPad app)

# --- engine modules live at the repo root and are partly imported lazily
hiddenimports += ["tools", "pdf_editor", "signing", "taskrunner", "darkdetect", "brand", "convert", "msoffice", "updater", "instance"]

# pywebview: the contrib hook ships the WebView2 DLLs; backends are imported dynamically by name
hiddenimports += collect_submodules("webview")

# pyHanko + its certificate validator: namespace packages with data files and lazily-loaded submodules
for pkg in ("pyhanko", "pyhanko_certvalidator"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
for dist in ("pyHanko", "pyhanko-certvalidator"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

# HEIC support for Pillow, reportlab fonts.  PyMuPDF needs no collect_all: its fonts are compiled into the
# DLLs that import analysis already finds, and collect_all would drag in pymupdf.layout -> torch/onnxruntime.
for pkg in ("pillow_heif", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

hiddenimports += ["openpyxl", "pypdf", "cryptography", "PIL.ImageOps", "PIL.ImageDraw", "PIL.ImageFont"]

# Changing a word in a scan (bridge_scanedit -> scanedit, scanpatch: imported only when it is used) needs numpy.
# Windows' own reader of the words in a picture (scanwords._windows), for PCs without the Tesseract add-on.
hiddenimports += ["scanedit", "scanpatch", "numpy"]
try:
    hiddenimports += collect_submodules("winrt")
except Exception:
    pass

# AI assistant: the Claude SDK is imported only when someone asks a question, so collect it explicitly.
# Its Bedrock / Vertex / Foundry clients need extra cloud packages the app doesn't use - leave them out.
hiddenimports += collect_submodules(
    "anthropic", filter=lambda n: not n.startswith(("anthropic.lib.bedrock", "anthropic.lib.vertex", "anthropic.lib.foundry")))
for dist in ("anthropic", "httpx2"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

# tkinter stays in: pdf_editor.py imports it at module level (the Editor class).  Drop it once the
# tkinter Editor is split out of pdf_editor.py.
excludes = [
    "fitz", "torch", "torchvision", "onnxruntime", "onnx", "pymupdf.layout", "pymupdf4llm", "sympy", "transformers",
    "matplotlib", "scipy", "pandas", "IPython", "jupyter", "notebook", "pytest", "unittest.mock",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "qtpy", "cefpython3",
    "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cocoa", "webview.platforms.android",
    "webview.platforms.cef",
]
hiddenimports = [h for h in hiddenimports if not any(h == e or h.startswith(e + ".") for e in excludes)]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=VERSION, prodvers=VERSION, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                      subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", BRAND["publisher"]),
            StringStruct("FileDescription", f"{BRAND['name']} - {BRAND['tagline']}"),
            StringStruct("FileVersion", ".".join(map(str, VERSION))),
            StringStruct("InternalName", STEM),
            StringStruct("OriginalFilename", f"{STEM}.exe"),
            StringStruct("ProductName", BRAND["name"]),
            StringStruct("ProductVersion", ".".join(map(str, VERSION[:3]))),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=STEM,
    icon=str(ROOT / "build" / "icon.ico"),
    version=version_info,
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name=STEM,
    upx=False,
)
