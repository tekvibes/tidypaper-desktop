"""Changing a word in a scan (Edit text on a page that is a picture of the paper) for the Bridge: the iPad's
"Fix this word" / "Fix this line".  The finding and the page work are in scanedit.py, the picture work in
scanpatch.py; both need numpy, so they are loaded only when a scan's word is first picked.

    scan_spot(i, x, y)                      the word under the pointer: read, found in the picture, the font nearest it
    scan_show(kind, text, face, size)       how the change would look (nothing is changed)
    scan_replace(kind, text, face, size)    make it - one step to undo
"""
from __future__ import annotations

from bridge_util import UserError, data_url, fitz, log, safe

import scanwords

NO_READER = "Reading the words in a scan needs the free Tesseract add-on (Tools: Pull text from a scan → Install)."
MOVED_ON = "The page has changed since this word was picked. Pick it again."


class ScanEditMixin:
    """Mixed into app.Bridge; relies on self._lock, self._need_doc(), self._page_ok(), self._P(), self._done(),
    self._edit_mark() and this tab's self._scanspot."""

    @safe
    def scan_spot(self, i: int, x: float, y: float):
        """The word of a scan under a point -> {word, line, word_bbox, line_bbox, face, size, faces}, or {"none"}
        (no words there) or {"cant": why}."""
        import scanedit
        i = int(i)
        keep = self._scanspot
        with self._lock:
            m = self._page_ok(i)
            pg = m.page(i)
            pt = self._P(i, x, y) * pg.rotation_matrix                  # the page as shown: where its picture's words are read
            if not scanwords.picture_under(pg, pt):
                return {"none": True}
            doc, mark = m.doc, self._edit_mark(m)
            read = keep.get("read")
            lines = read[3] if read and read[0] is doc and read[1] == mark and read[2] == i else None
            if lines is None:
                engine = scanwords.default_engine()
                if not engine:
                    raise UserError(NO_READER)
                png, z, size = scanwords.picture(pg, annots=False)      # the picture needs the document; the reading doesn't
        if lines is None:
            try:
                raw = scanwords.read_picture(png, size, engine, reach=False)
            except scanwords.ScanError as e:
                raise UserError(str(e))
        with self._lock:
            m = self._need_doc()
            if m.doc is not doc or self._edit_mark(m) != mark:
                raise UserError("The document changed while its words were being read. Try again.")
            pg = m.page(i)
            if lines is None:
                lines = scanwords.to_page(pg, raw, size)
                keep["read"] = (doc, mark, i, lines)                    # read once for a page, until it changes
            found = scanedit.find(pg, pt, lines)
            if found is None:
                return {"none": True}
            if found.spot is None:
                return {"cant": found.cant}
            s = found.spot
            keep["spot"] = (doc, mark, i, s)
            fit = s.place.fit
            log.info("scan word p%d: %s %.1f pt, width x%.2f, soft %.1f, weight %+.1f, alike %.2f", i + 1, fit.face.name,
                     s.size, fit.xscale, fit.blur, fit.weight, fit.likeness)
            import scanpatch
            box = lambda r: [round(v, 2) for v in r]                    # (as shown already: the window's own space)
            return {"word": s.word, "line": s.line, "word_bbox": box(s.word_bounds),
                    "line_bbox": box(s.line_bounds) if s.line_bounds is not None else None,
                    "face": s.face.name, "size": s.size, "faces": [f.name for f in scanpatch.FACES], "page": i}

    def _scan_spot(self):
        """The word picked last, if the page is still as it was: (model, page index, Spot)."""
        m = self._need_doc()
        got = self._scanspot.get("spot")
        if not got or got[0] is not m.doc or got[1] != self._edit_mark(m):
            raise UserError(MOVED_ON)
        return m, got[2], got[3]

    @staticmethod
    def _scan_request(s, kind, text, face, size):
        import scanpatch
        if kind not in ("word", "line"):
            raise UserError("Unknown kind of change.")
        chosen = next((f for f in scanpatch.FACES if f.name == face), None)     # None: the font found nearest
        try:
            size = float(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        if size is not None and (abs(size - s.size) < 0.01 or not 4 <= size <= 200):
            size = None                                                        # None: the size found
        return kind, str(text or ""), chosen, size

    @safe
    def scan_show(self, kind: str, text: str, face: str | None = None, size: float | None = None):
        """How the change would look -> {image, w, h, problem, squeezed}.  Nothing is changed."""
        with self._lock:
            m, i, s = self._scan_spot()
            pic, problem, squeezed = s.show(*self._scan_request(s, kind, text, face, size))
            out = {"problem": problem, "squeezed": squeezed, "image": None}
            if pic is not None:
                h, w = pic.shape[:2]
                out.update(image=data_url(fitz.Pixmap(fitz.csRGB, w, h, pic.tobytes(), False).tobytes("png"), "image/png"), w=w, h=h)
            return out

    @safe
    def scan_replace(self, kind: str, text: str, face: str | None = None, size: float | None = None):
        """Paint the old word (or line) out of the page's picture and draw the new one in - one step to undo."""
        import scanedit
        with self._lock:
            m, i, s = self._scan_spot()
            kind, text, chosen, size = self._scan_request(s, kind, text, face, size)
            was_dirty = m.dirty
            m.snapshot()
            problem = None
            try:
                c = s.apply(m.page(i), kind, text, chosen, size)
            except scanedit.ScanEditError as e:
                problem = str(e)
            except Exception:
                log.exception("changing a word in a scan")
                problem = scanedit.FAILED
            self._scanspot.clear()
            if problem:
                m.undo()                                # it may be half done: back as it was,
                m.redo_stack.pop()                      # with nothing to "redo" into
                m.dirty = was_dirty
                raise UserError(problem)
            what = "Word" if kind == "word" else "Line"
            if not text.strip():
                return self._done(i, f"{what} removed from the picture.")
            closer = " The letters are set a little closer to fit." if c.squeezed else ""
            return self._done(i, f"{what} replaced - drawn in {c.face.name} to match the scan.{closer}")
