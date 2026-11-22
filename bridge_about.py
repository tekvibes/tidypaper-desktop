"""Settings › About: the copyright, the licence (AGPL-3.0), other people's work in the app, and where the source is.

The texts are legal/*.txt, bundled with the app (build/public_source/make_public_source.py --legal writes them).
"""

from __future__ import annotations

import sys
from pathlib import Path

import brand
from bridge_util import UserError, safe

SOURCE_URL = "https://github.com/tekvibes/tidypaper-desktop"
TEXTS = {"licence": ("LICENSE.txt", "The licence (GNU AGPL, version 3)"),
         "copyright": ("COPYRIGHT.txt", "Copyright and names"),
         "others": ("THIRD-PARTY-LICENSES.txt", "Other people's work in the app"),
         "sources": ("THIRD-PARTY-SOURCES.txt", "Where their source is")}


def legal_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent) / "legal"


class AboutMixin:
    """Mixed into app.Bridge."""

    @safe
    def about(self):
        return {"name": brand.NAME, "version": brand.VERSION, "copyright": "Copyright © 2026 Tekvibes Limited",
                "source": f"{SOURCE_URL}/tree/v{brand.VERSION}",
                "texts": [{"id": k, "title": t} for k, (_, t) in TEXTS.items()]}

    @safe
    def about_text(self, which: str):
        if which not in TEXTS:
            raise UserError("There's no text of that name.")
        name, title = TEXTS[which]
        try:
            return {"title": title, "text": (legal_dir() / name).read_text(encoding="utf-8")}
        except OSError:
            raise UserError(f"That text isn't in this copy. It is at {SOURCE_URL}.")
