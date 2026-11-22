# PyInstaller spec for the Mac app (one-dir .app bundle, Apple Silicon).  Name and version come from brand.json
# (plus BRAND_NAME / BRAND_VERSION ... set by build_mac.sh name=... version=...), as on Windows.
#
#   build/build_mac.sh          (does this, then signs, makes the .dmg and notarises)
#
# Output: build/dist-mac/<Name>.app.  Tesseract and FFmpeg from Homebrew are bundled in <bundle>/bin with the
# libraries they need; the English and script-detection languages in <bundle>/tessdata (tools.py looks there first).

import json
import os
import shutil
import sys
from pathlib import Path

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent          # repo root (this file lives in build/)
sys.path.insert(0, str(ROOT))
import brand
BRAND = brand.for_build()
STEM = brand.file_stem(BRAND["name"])
VERSION = BRAND["version"]
# Fixed whatever the app is called (like ~/.toolkit and the "Toolkit/..." Keychain items): macOS ties the Keychain's
# "Always Allow", privacy answers and Open With to it, so a rename must not change it.
BUNDLE_ID = "com.tekvibes.toolkit"

BRAND_FILE = Path(CONF["workpath"]) / "brand.json"
BRAND_FILE.parent.mkdir(parents=True, exist_ok=True)
BRAND_FILE.write_text(json.dumps(BRAND, indent=2), encoding="utf-8")
print(f"Building {BRAND['name']} {VERSION} -> {STEM}.app ({BUNDLE_ID})")

datas, binaries, hiddenimports = [], [], []
datas.append((str(ROOT / "ui"), "ui"))
datas.append((str(BRAND_FILE), "."))
datas.append((str(ROOT / "legal"), "legal"))        # Settings › About: the licence and the notices
datas.append((str(ROOT / "prompts.json"), "."))     # what the AI is told (shared with the iPad app)

hiddenimports += ["tools", "pdf_editor", "signing", "taskrunner", "darkdetect", "brand", "convert", "msoffice", "updater", "instance",
                  "mac_keychain", "Security", "AppKit", "Foundation", "WebKit", "Vision", "Quartz"]
hiddenimports += [m for m in collect_submodules("webview")
                  if not m.startswith(("webview.platforms.winforms", "webview.platforms.edgechromium",
                                       "webview.platforms.mshtml", "webview.platforms.qt", "webview.platforms.gtk",
                                       "webview.platforms.cef", "webview.platforms.android"))]

for pkg in ("pyhanko", "pyhanko_certvalidator"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
for dist in ("pyHanko", "pyhanko-certvalidator", "anthropic"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass
for pkg in ("pillow_heif", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
hiddenimports += ["openpyxl", "pypdf", "cryptography", "PIL.ImageOps", "PIL.ImageDraw", "PIL.ImageFont"]
# changing a word in a scan (bridge_scanedit -> scanedit, scanpatch: imported only when it is used) needs numpy
hiddenimports += ["scanedit", "scanpatch", "numpy"]
hiddenimports += collect_submodules(
    "anthropic", filter=lambda n: not n.startswith(("anthropic.lib.bedrock", "anthropic.lib.vertex", "anthropic.lib.foundry")))

# --- Tesseract and FFmpeg (Homebrew's builds; PyInstaller collects and relinks the libraries they load)
for prog in ("tesseract", "ffmpeg"):
    exe = shutil.which(prog) or f"/opt/homebrew/bin/{prog}"
    if not Path(exe).exists():
        raise SystemExit(f"{prog} isn't installed - run:  brew install {prog}")
    binaries.append((os.path.realpath(exe), "bin"))
tessdata = Path(os.environ.get("TESSDATA_DIR", "/opt/homebrew/share/tessdata"))
for lang in ("eng", "osd"):
    datas.append((os.path.realpath(tessdata / f"{lang}.traineddata"), "tessdata"))

# the old Tk window isn't shipped on a Mac (pdf_editor.py works without Tk)
excludes = [
    "tkinter", "_tkinter", "toolkit",
    "fitz", "torch", "torchvision", "onnxruntime", "onnx", "pymupdf.layout", "pymupdf4llm", "sympy", "transformers",
    "matplotlib", "scipy", "pandas", "IPython", "jupyter", "notebook", "pytest", "unittest.mock",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "qtpy", "cefpython3", "clr", "pythonnet",
    "webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.mshtml",
    "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef", "webview.platforms.android",
]
hiddenimports = [h for h in hiddenimports if not any(h == e or h.startswith(e + ".") for e in excludes)]

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
    console=False,
    argv_emulation=True,           # a file double-clicked in Finder (or Open With) arrives in sys.argv at launch
    target_arch="arm64",
    codesign_identity=None,        # build_mac.sh signs the finished bundle (with the hardened runtime)
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name=STEM, upx=False)

OFFICE = ["org.openxmlformats.wordprocessingml.document", "com.microsoft.word.doc",
          "org.openxmlformats.spreadsheetml.sheet", "com.microsoft.excel.xls",
          "org.openxmlformats.presentationml.presentation", "com.microsoft.powerpoint.ppt",
          "org.oasis-open.opendocument.text", "org.oasis-open.opendocument.spreadsheet",
          "org.oasis-open.opendocument.presentation", "public.rtf"]
OTHER = ["public.plain-text", "public.comma-separated-values-text", "org.idpf.epub-container"]

app = BUNDLE(
    coll,
    name=f"{STEM}.app",
    icon=str(ROOT / "build" / "icon.icns"),
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": BRAND["name"],
        "CFBundleDisplayName": BRAND["name"],
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHumanReadableCopyright": BRAND.get("publisher", ""),
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,          # follow light / dark mode
        "NSLocalNetworkUsageDescription": "Your phone sends photos of paper pages to this Mac over your Wi-Fi.",
        # "Open With" in Finder.  Alternate = offered, never taking over from Preview, Word...
        "CFBundleDocumentTypes": [
            {"CFBundleTypeName": "PDF document", "CFBundleTypeRole": "Editor", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": ["com.adobe.pdf"]},
            {"CFBundleTypeName": "Office document", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": OFFICE},
            {"CFBundleTypeName": "Picture", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": ["public.image"]},
            {"CFBundleTypeName": "Text or e-book", "CFBundleTypeRole": "Viewer", "LSHandlerRank": "Alternate",
             "LSItemContentTypes": OTHER},
        ],
    },
)
