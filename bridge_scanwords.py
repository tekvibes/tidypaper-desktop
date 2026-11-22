"""Words in a scan (Batch 5) for the Bridge: which pages are pictures of text, and reading their words in the
background so they can be selected, copied, highlighted and found.  The reading and writing are in scanwords.py
(the same rules as the iPad's ScanWords.swift)."""

from __future__ import annotations

import threading

import scanwords
from bridge_util import UserError, log, safe


def message(written: int, read: int, total: int, stopped: bool, failed: int) -> str:
    """What to tell the person afterwards (the iPad's ScanWordsJob.message, with "Save" for the desktop's Save)."""
    if not written:
        if stopped:
            return "Stopped - nothing was changed."
        return ("No words were found in the scan, so nothing was changed." if not read and not failed
                else "The words couldn't be added, so nothing was changed.")
    pages = "1 page" if written == 1 else f"{written} pages"
    m = f"The words on {pages} can now be selected, copied, highlighted and found. Save to keep them."
    if stopped:
        m = "Stopped. " + m
    elif written < total:
        m += f" ({total - written} had no words that could be read.)"
    return m


class ScanWordsMixin:
    """Mixed into app.Bridge; relies on self._lock, self._need_doc(), self._info()."""

    def _scan_job(self) -> dict:
        """This tab's job (app.Tab.scanjob)."""
        return self._scanjob

    @staticmethod
    def _edit_mark(m):
        """Changes since: the number of undo steps and the last one (the list stops growing at 40)."""
        return len(m.undo_stack), id(m.undo_stack[-1]) if m.undo_stack else None

    @safe
    def scan_pages(self):
        """The pages that are pictures of text -> {"pages": [0-based], "engine": "vision"/"tesseract"/None}."""
        with self._lock:
            m = self._need_doc()
            pages = [i for i, pg in enumerate(m.doc) if scanwords.is_scan(pg)]
        return {"pages": pages, "engine": scanwords.default_engine() if pages else None}

    @safe
    def scan_words_start(self, pages=None):
        """Start reading the scanned pages (all of them, or `pages`) in the background.  Poll scan_words_status()."""
        job = self._scan_job()
        if job.get("running"):
            return {"running": True}
        engine = scanwords.default_engine()
        if not engine:
            raise UserError("Reading the words in a scan needs the free Tesseract add-on "
                            "(Tools: Pull text from a scan → Install).")
        with self._lock:
            m = self._need_doc()
            if pages is None:
                pages = [i for i, pg in enumerate(m.doc) if scanwords.is_scan(pg)]
            pages = [int(i) for i in pages if 0 <= int(i) < len(m)]
            if not pages:
                raise UserError("There are no scanned pages here.")
            doc, edits = m.doc, self._edit_mark(m)
        job.clear()
        job.update(running=True, done=0, total=len(pages), stop=False, result=None)
        self._thread(self._scan_words_run, job, pages, engine, doc, edits).start()   # stays with this tab
        return {"running": True, "total": len(pages)}

    def _scan_words_run(self, job, pages, engine, doc, edits):
        read, failed = [], 0
        try:
            for i in pages:
                if job["stop"]:
                    break
                with self._lock:                       # the picture needs the document; the reading doesn't
                    m = self._model
                    if not m or m.doc is not doc:
                        break
                    pg = m.page(i)
                    png, z, size = scanwords.picture(pg)
                    rect = pg.rect
                try:
                    lines = scanwords.read_picture(png, size, engine)
                    if lines:
                        read.append((i, lines, size))
                except Exception:
                    log.exception("reading the words on page %d", i + 1)
                    failed += 1
                job["done"] += 1
            stopped = job["stop"]
            written = []
            with self._lock:
                m = self._model
                if not m or m.doc is not doc or self._edit_mark(m) != edits:
                    job["result"] = {"error": "The document changed while its words were being read, so nothing "
                                              "was changed. Try again."}
                    return
                if read:
                    was_dirty = m.dirty
                    m.snapshot()
                    for i, lines, size in read:
                        pg = m.page(i)
                        try:
                            if scanwords.write(pg, scanwords.to_page(pg, lines, size)):
                                written.append(i)
                        except Exception:
                            log.exception("writing the words on page %d", i + 1)
                    if written:
                        m.dirty = True
                    else:
                        m.undo_stack.pop()             # nothing changed after all
                        m.dirty = was_dirty
                job["result"] = {**self._info(), "changed": written, "stopped": bool(stopped),
                                 "status": message(len(written), len(read), len(pages), stopped, failed)}
                log.info("scan words: %d of %d pages (%s)", len(written), len(pages), engine)
        except Exception as e:
            log.exception("scan words")
            job["result"] = {"error": str(e) if isinstance(e, (UserError, scanwords.ScanError)) else
                             "The words couldn't be read, so nothing was changed."}
        finally:
            job["running"] = False

    @safe
    def scan_words_status(self):
        """{"running", "done", "total", "result"} - result is the finished edit (or {"error"})."""
        job = self._scan_job()
        out = {k: job.get(k) for k in ("running", "done", "total")}
        if not job.get("running") and job.get("result") is not None:
            out["result"] = job["result"]
            job["result"] = None
        return out

    @safe
    def scan_words_stop(self):
        self._scan_job()["stop"] = True
        return {"ok": True}
