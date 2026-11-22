"""Opening things (Batch 4 of the Folio features) for the Bridge: a PDF from a web link, a webpage as a PDF, and the
sample tour.  The rules for links are in weblink.py; the webpage printing in webpage_pdf.py."""

from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path

import brand
import weblink
import webpage_pdf
from bridge_util import UserError, log, safe, unique

SAMPLE_NAME = f"Welcome to {brand.NAME}.pdf"


def sample_source() -> Path:
    """The tour as shipped (ui/sample/, made by build/make_sample.py; bundled with ui/)."""
    import app
    return app.UI_DIR / "sample" / "Welcome to TidyPaper.pdf"


class OpenMixin:
    """Mixed into app.Bridge; relies on self.open_file() and app.CONFIG_DIR."""

    @safe
    def fetch_link(self, text: str, allow_private: bool = False):
        """Look at a web link.  A PDF is downloaded to Downloads -> {"path", "status"} (the page then opens it);
        a webpage -> {"webpage": True, "url", "title"}; this computer's own network -> {"ask_private": True, "host"}."""
        try:
            r = weblink.fetch(text, allow_private=bool(allow_private))
        except weblink.PrivateAddress as p:
            return {"ask_private": True, "host": p.host}
        if r["kind"] == "webpage":
            return {"webpage": True, "url": r["url"], "title": r["title"]}
        path = Path(r["path"])
        log.info("downloaded %s (%d bytes) from %s", path, r["bytes"], r["url"])
        return {"path": str(path), "status": f"Downloaded to {path.parent.name} as {path.name}."}

    @safe
    def webpage_to_pdf(self, url: str, title: str = ""):
        """Make a PDF of a webpage (A4, words you can select) and save it in Downloads -> {"path", "status"}."""
        if threading.current_thread() is threading.main_thread():
            raise UserError("Making a PDF of a webpage needs the app window.")
        url = weblink.normalise(url)
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title or "").strip(" .")[:100] or "Webpage"
        out = unique(weblink.downloads_dir() / f"{name}.pdf")
        try:
            webpage_pdf.make_pdf(url, out)
        except webpage_pdf.WebpageError as e:
            out.unlink(missing_ok=True)
            raise UserError(str(e)) from None
        self._tag_webpage(out, url, name)
        log.info("made %s from %s", out, url)
        return {"path": str(out), "status": f"Made a PDF of the webpage and saved it in {out.parent.name} as {out.name}."}

    @staticmethod
    def _tag_webpage(path: Path, url: str, title: str):
        """Remember where it came from in the PDF itself (Document details shows it)."""
        try:
            import pymupdf as fitz
            d = fitz.open(path)
            meta = d.metadata or {}
            meta.update({"title": meta.get("title") or title, "subject": f"Made from the webpage {url}",
                         "creator": f"{brand.NAME} (from a webpage)"})
            d.set_metadata(meta)
            d.saveIncr()
            d.close()
        except Exception:
            log.exception("couldn't tag %s", path)

    @safe
    def sample_path(self):
        """A fresh copy of the tour to open (the shipped one is never changed; Save puts copies in Documents)."""
        import app
        src = sample_source()
        if not src.exists():
            raise UserError("The sample document is missing from this copy of the app.")
        folder = app.CONFIG_DIR / "sample"
        folder.mkdir(parents=True, exist_ok=True)
        copy = folder / SAMPLE_NAME
        shutil.copyfile(src, copy)
        return {"path": str(copy)}
