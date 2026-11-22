"""The app's name, the AI service's name and the version - read from brand.json.

Change brand.json (or build with  build\build_installer.bat name="Something")  to rename the app; nothing else in the
code needs editing.  Internal names that existing installs depend on stay fixed whatever the brand:
the ~/.toolkit data folder, the "Toolkit/ai/" saved-key prefix, the installer's AppId and the relay's address.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULTS = {"name": "TidyPaper", "ai_name": "Shared AI", "version": "0.1.0",
            "tagline": "PDF and document helper", "publisher": "TidyPaper",
            "update_url": "",     # where the app checks for new versions (the relay's /version); empty = never
            "store": ""}          # "microsoft" in the Microsoft Store's package (build/make_msix.py): the Store updates it


def _candidates():
    base = getattr(sys, "_MEIPASS", None)            # the built app: brand.json is bundled next to ui/
    if base:
        yield Path(base) / "brand.json"
    yield Path(__file__).resolve().parent / "brand.json"


def load(path: Path | None = None) -> dict:
    out = dict(DEFAULTS)
    for p in ([path] if path else _candidates()):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.update({k: str(v).strip() for k, v in data.items() if k in DEFAULTS and str(v).strip()})
        break
    return out


def for_build() -> dict:
    """brand.json plus one-off overrides from the build command (BRAND_NAME, BRAND_AI_NAME, BRAND_VERSION ...).
    The build bundles the result as the app's own brand.json; the running app never reads these variables."""
    import os
    out = load(Path(__file__).resolve().parent / "brand.json")
    for k in DEFAULTS:
        v = os.environ.get("BRAND_" + k.upper(), "").strip()
        if v:
            out[k] = v
    if not re.fullmatch(r"\d+(\.\d+){0,3}", out["version"]):
        raise SystemExit(f"brand version must look like 1.2.3, not {out['version']!r}")
    return out


def file_stem(name: str) -> str:
    """The name as it can appear in file names: TidyPaper.exe, TidyPaper-portable.zip."""
    return re.sub(r'[<>:"/\|?*\x00-\x1f]', "", name).strip(" .") or "App"


BRAND = load()
NAME = BRAND["name"]
AI_NAME = BRAND["ai_name"]
VERSION = BRAND["version"]
TAGLINE = BRAND["tagline"]
PUBLISHER = BRAND["publisher"]
UPDATE_URL = BRAND["update_url"] if BRAND["update_url"].startswith("https://") else ""
EXE_STEM = file_stem(NAME)
STORE = BRAND["store"].lower()      # "" = our own installer;  "microsoft" = installed from the Microsoft Store


if __name__ == "__main__":                            # the build asks: py brand.py name|stem|version
    b = for_build()
    key = sys.argv[1] if len(sys.argv) > 1 else "name"
    print(file_stem(b["name"]) if key == "stem" else b.get(key, ""))
