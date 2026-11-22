"""Changing words in a scan: Edit text on a page that is a picture of the paper.  The desktop side of the iPad's
ScanEdit.swift (scanpatch.py does the picture work; bridge_scanedit.py is what the window calls).

There are no letters in such a page to retype - only a picture, with (once "Make its words selectable" has run, or
the scanner did it) the words written invisibly over it.  So the word is read where it is in the picture, painted
over in the picture itself and drawn again in the nearest of the usual fonts, and the invisible words are put right to
match - so Find and Copy give the new words, and the old ones are gone from the file, not covered.

    picture_under(page, pt)             is there a picture there (words there may be the picture's)?  (scanwords')
    find(page, pt, lines) -> Found      the word of a scan under a point: a Spot, or why not
    spot.show(kind, text, face, size)   how the change would look: (picture, problem, squeezed) - nothing is changed
    spot.apply(page, kind, text, ...)   make the change in the page (ScanEditError: undo it)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

import scanpatch as sp
import scanwords

JPEG_QUALITY = 92

TOO_LONG_WORD = "Too long to fit before the next word. Shorten it, choose a smaller size, or change the whole line."
TOO_LONG_LINE = "Too long to fit on the line. Shorten it or choose a smaller size."
LETTERS = "Some of these letters can't be drawn here."
FAILED = "The picture of this page couldn't be changed, so nothing was changed."
UNSAFE = "This page couldn't be changed without disturbing other words on it, so nothing was changed."
LAYERS = "This scan is stored in layers, so its words can't be changed here."
NOT_THESE = "These words couldn't be picked out of the picture, so they can't be changed here."
OWN_WORDS = "These words can't be changed here - they're turned sideways or stored as part of a drawing."

PAINTABLE = ("DeviceGray", "DeviceRGB", "CalGray", "CalRGB", "ICCBased")


class ScanEditError(Exception):
    """Something to tell the person; nothing should be left changed."""


@dataclass
class Found:
    spot: "Spot | None" = None
    cant: str | None = None         # words, but not ones that can be changed (and why)


# ---------------------------------------------------------------- the page's pictures and words

_pictures = scanwords.pictures
picture_under = scanwords.picture_under


def _unit(pt, m) -> tuple:
    q = fitz.Point(pt) * ~m
    return q.x, q.y


def _letters(page) -> list:
    """Every letter on the page that isn't a space: (letter, middle, box, written invisibly?, start, end) - in the
    space of the page as shown (PyMuPDF gives them in the page's own, which for a page stored on its side is turned).
    `start` and `end`: where the letter stands on its line - its own place, and where the next letter would go."""
    out = []
    shown = page.rotation_matrix
    for span in page.get_texttrace():
        hidden = span.get("type") == 3 or span.get("opacity", 1) == 0
        d = span.get("dir") or (1, 0)
        size = span.get("size") or 0
        for ch in span.get("chars", []):
            c = chr(ch[0])
            if c.isspace():
                continue
            box = fitz.Rect(ch[3])
            # how far the letter goes along its line: its box is that wide (and, on a line that leans, wider by the
            # lean of its height - which PyMuPDF takes as the size the letters are written at)
            if abs(d[0]) >= abs(d[1]):
                step = (box.width - size * abs(d[1])) / abs(d[0])
            else:
                step = (box.height - size * abs(d[0])) / abs(d[1])
            start = fitz.Point(ch[2])
            end = start + fitz.Point(d) * max(0.0, step)
            start, end = start * shown, end * shown
            b = box * shown
            out.append((c, ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2), b, hidden, (start.x, start.y), (end.x, end.y)))
    return out


def read_lines(page, engine: str | None = None) -> list:
    """The page's lines of words as they lie in its picture, each word with its own box (page space, as shown).
    Notes, highlights and signatures on the page aren't read: only the picture."""
    png, z, size = scanwords.picture(page, annots=False)
    return scanwords.to_page(page, scanwords.read_picture(png, size, engine, reach=False), size)


def _pick(pt, lines):
    """The word under (or nearest) a point: its line and its place in the line."""
    best = None
    for li, line in enumerate(lines):
        for wi, w in enumerate(line):
            ax, ay = w.br[0] - w.bl[0], w.br[1] - w.bl[1]
            ux, uy = w.tl[0] - w.bl[0], w.tl[1] - w.bl[1]
            ln, h = math.hypot(ax, ay), math.hypot(ux, uy)
            if ln <= 0.1 or h <= 0.1:
                continue
            dx, dy = pt[0] - w.bl[0], pt[1] - w.bl[1]
            along, up = (dx * ax + dy * ay) / ln, (dx * ux + dy * uy) / h
            out = math.hypot(max(0, -along, along - ln), max(0, -up, up - h))      # how far outside the word's box
            if out <= max(4, h * 0.6) and (best is None or out < best[0]):
                best = (out, li, wi)
    return (best[1], best[2]) if best else None


def _pixels(doc, xref: int):
    """A picture's pixels, uint8 (rows, columns, 3), and whether it is grey - or None when it isn't a kind that can
    be painted."""
    try:
        pix = fitz.Pixmap(doc, xref)
    except Exception:
        return None
    if pix.alpha or pix.n not in (1, 3) or pix.width < 1 or pix.height < 1:
        return None
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * pix.n]
    if pix.n == 1:
        return np.repeat(a.reshape(pix.height, pix.width, 1), 3, axis=2), True
    return a.reshape(pix.height, pix.width, 3).copy(), False


def _paintable(doc, xref: int, info: dict) -> bool:
    """One picture, in a kind of colour that can be painted, with nothing cut out of it."""
    if not xref or info.get("has-mask") or info.get("bpc") != 8 or info.get("colorspace") not in (1, 3):
        return False
    if not str(info.get("cs-name", "")).startswith(PAINTABLE):
        return False
    for key in ("SMask", "Mask", "ImageMask", "Decode"):
        if doc.xref_get_key(xref, key)[0] != "null":
            return False
    return True


# ---------------------------------------------------------------- a word, found

class Spot:
    """A word of a scan (and its line), found in the page's picture and ready to be changed."""

    def __init__(self, xref, placed, place, targets, per_point, has_words, pixel_size, grey):
        self.xref = xref
        self.placed = placed                # the picture's unit square -> the page as shown
        self.place = place
        self.targets = targets              # {"word": Target, "line": Target}
        self.per_point = per_point          # the picture's pixels to a point of the page
        self.has_words = has_words          # the page has words written invisibly over the picture
        self.pixel_size = pixel_size        # (columns, rows)
        self.grey = grey
        self.word = targets["word"].text
        self.line = targets["line"].text if "line" in targets else None
        self.face = place.fit.face
        self.size = round(place.fit.size / per_point * 2) / 2          # points
        self.word_bounds = self._bounds(targets["word"].ink)
        self.line_bounds = self._bounds(targets["line"].ink) if "line" in targets else None

    def on_page(self, u: float, v: float) -> tuple:
        """A point of the strip -> the page as shown."""
        q = self.place.geometry.picture(u, v)
        p = fitz.Point(q[0] / self.pixel_size[0], q[1] / self.pixel_size[1]) * self.placed
        return p.x, p.y

    def in_strip(self, pt) -> tuple:
        """A point of the page as shown -> the strip (on_page, the other way)."""
        q = fitz.Point(pt) * ~self.placed
        return self.place.geometry.strip((q.x * self.pixel_size[0], q.y * self.pixel_size[1]))

    def _bounds(self, r: sp.IntRect):
        pts = [self.on_page(x, y) for x, y in ((r.x, r.y), (r.max_x, r.y), (r.x, r.max_y), (r.max_x, r.max_y))]
        return fitz.Rect(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    # ---- showing and making the change

    def _compose(self, kind: str, text: str, face, size):
        t = self.targets.get(kind)
        if t is None:
            raise ScanEditError(FAILED)
        try:
            return t, sp.compose(self.place, t, text, face, size * self.per_point if size else None)
        except sp.TooLong:
            raise ScanEditError(TOO_LONG_WORD if kind == "word" else TOO_LONG_LINE)
        except sp.Letters:
            raise ScanEditError(LETTERS)

    def show(self, kind: str, text: str, face=None, size=None):
        """How the change would look: the words round it, straightened, with the new ones in place.  Nothing is
        changed.  -> (picture uint8 (rows, columns, 3) or None, problem or None, squeezed)."""
        t = self.targets.get(kind)
        if t is None:
            return None, NOT_THESE, False
        try:
            _, c = self._compose(kind, text, face, size)
            return sp.picture(self.place, c, sp.view(self.place, t, c)), None, c.squeezed
        except ScanEditError as e:
            return sp.picture(self.place, None, sp.view(self.place, t, None)), str(e), False

    def apply(self, page, kind: str, text: str, face=None, size=None) -> sp.Composed:
        """Make the change in the page.  ScanEditError: it couldn't be made - the caller puts the page back."""
        t, c = self._compose(kind, text, face, size)
        doc = page.parent

        # 1. the picture
        got = _pixels(doc, self.xref)
        if got is None or (got[0].shape[1], got[0].shape[0]) != self.pixel_size:
            raise ScanEditError(FAILED)
        px = got[0]
        changed = sp.write(c, self.place.geometry, px)
        if changed.empty:
            raise ScanEditError(FAILED)
        wrote = _grey(px, changed)
        try:
            _store(doc, self.xref, px, self.grey)
        except Exception:
            raise ScanEditError(FAILED)

        # 2. the words written invisibly over the picture: the old ones out, the new ones in their place
        if self.has_words:
            self._put_words_right(page, t, c, kind)

        # 3. read it back: the picture is as it was painted
        back = _pixels(doc, self.xref)
        if back is None or back[0].shape != px.shape:
            raise ScanEditError(FAILED)
        now = _grey(back[0], changed)
        if not now.size or np.abs(now - wrote).mean() >= 12:
            raise ScanEditError(FAILED)
        return c

    def _new_words(self, t, c, kind: str) -> list:
        """The new words as they lie on the page, to be written invisibly: a word followed by another keeps a space
        after it (so copied words come out with spaces) and reaches to it."""
        p = self.place
        follows = kind == "word" and p.pick + 1 < len(p.words)
        nxt = p.words[p.pick + 1].ink.x if follows and p.words[p.pick + 1] is not None else None
        # as tall as the line's other words are written (their boxes as read), so they read as one line with them
        own = p.words[p.pick].box if p.words[p.pick] is not None else None
        out = []
        for k, (text, box) in enumerate(c.pieces):
            last = k == len(c.pieces) - 1
            right = max(box.max_x, nxt or 0) if last else c.pieces[k + 1][1].x
            top, bottom = (own.y, own.max_y) if own is not None else (box.y, box.max_y)
            bl, br, tl = self.on_page(box.x, bottom), self.on_page(right, bottom), self.on_page(box.x, top)
            # square to its line, as the words a reader gives are (where the scan's letters lean the strip leans with
            # them, and a word written leaning couldn't be measured again when the page's words are next put right)
            ax, ay = br[0] - bl[0], br[1] - bl[1]
            ln = math.hypot(ax, ay)
            if ln > 0:
                ux, uy = ay / ln, -ax / ln
                h = (tl[0] - bl[0]) * ux + (tl[1] - bl[1]) * uy
                tl = (bl[0] + ux * h, bl[1] + uy * h)
            out.append(scanwords.Word(text + ("" if last and not follows else " "), bl, br, tl))
        return out

    def _old_letters(self, t):
        """Which of the letters written invisibly are the old word's (or line's)?  -> a test for one of _letters().

        The invisible words only roughly follow the picture's.  Each was stretched over the box a reader drew round
        its word - snug to the ink (Tesseract) or a line tall (Vision), often a point wide of it - and in Helvetica's
        widths, not the scan's own font's.  So a letter is the old word's when it stands on this line (its foot
        within the line's own height of where the picture's letters stand) and its middle is along the old ink, or
        in the nearer half of the clear paper on either side of it.

        (Windows 0.2.15 asked for the middle of the letter's box to be inside the old ink's box.  A word of short
        letters - "is", "invoice", "total" - has ink too low for that, and Vision's boxes begin early, so a first "l"
        or "i" fell outside: the change was refused as disturbing other words, or left a letter behind.)"""
        fit, ink = self.place.fit, t.ink
        em = max(1.0, fit.size)
        left = ink.x - min(t.clear[0] / 2, 0.6 * em)
        right = ink.max_x + min(t.clear[1] / 2, 0.6 * em)

        def old(letter) -> bool:
            (u0, v0), (u1, v1) = self.in_strip(letter[4]), self.in_strip(letter[5])
            u, v = (u0 + u1) / 2, (v0 + v1) / 2
            return left < u < right and -0.75 * em <= v - fit.baseline(u) <= 0.35 * em
        return old

    def _put_words_right(self, page, t, c, kind: str):
        new = self._new_words(t, c, kind)
        before = _letters(page)
        if _own_letters_in(before, self._bounds(t.ink)):
            raise ScanEditError(UNSAFE)
        old = self._old_letters(t)
        gone = [l for l in before if l[3] and old(l)]
        kept = _count(l[0] for l in before if not (l[3] and old(l)))
        try:
            rebuilt = _rebuild(page, gone, new) if all(l[3] for l in before) else False
            if not rebuilt:
                # the old word's letters out (a dot on each, just over the line it stands on, takes that letter and
                # no other - on a line that leans too), the new word after everything else on the page
                if gone:
                    stored = page.derotation_matrix
                    for l in gone:
                        (x0, y0), (x1, y1) = l[4], l[5]
                        ln = math.hypot(x1 - x0, y1 - y0) or 1.0
                        x, y = (x0 + x1) / 2 + (y1 - y0) / ln * 0.3, (y0 + y1) / 2 - (x1 - x0) / ln * 0.3
                        page.add_redact_annot((fitz.Rect(x - 0.3, y - 0.3, x + 0.3, y + 0.3) * stored).normalize(), fill=False)
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
                if new:
                    scanwords.write(page, [new])
        except scanwords.ScanError:
            raise ScanEditError(UNSAFE)
        # every other word still there, and the new ones with them
        after = _count(l[0] for l in _letters(page))
        for ch, n in kept.items():
            if after.get(ch, 0) < n:
                raise ScanEditError(UNSAFE)
        if sum(after.values()) > sum(kept.values()) + sum(len(w.text.replace(" ", "")) for w in new):
            raise ScanEditError(UNSAFE)


def _own_letters_in(letters: list, box) -> bool:
    return any(not l[3] and box.contains(fitz.Point(l[1])) for l in letters)


def _count(chars) -> dict:
    out: dict = {}
    for c in chars:
        out[c] = out.get(c, 0) + 1
    return out


def _here(points):
    """Is one of these points here (to a twentieth of a point)?  -> a test for a point."""
    cells: dict = {}
    for p in points:
        cells.setdefault((round(p[0]), round(p[1])), []).append(p)

    def here(p) -> bool:
        i, j = round(p[0]), round(p[1])
        return any(abs(q[0] - p[0]) < 0.05 and abs(q[1] - p[1]) < 0.05
                   for di in (-1, 0, 1) for dj in (-1, 0, 1) for q in cells.get((i + di, j + dj), ()))
    return here


def _corners(run: list, d: tuple):
    """A written word's corners - bottom-left, bottom-right, top-left - from its letters' boxes, on a line that runs
    along `d` (the page as shown, y down).  PyMuPDF gives each letter's box upright; on a line that leans that box is
    the leaning one's outline, so the word's length and height are worked back out of it.  None: they can't be."""
    x0, x1 = min(c["bbox"][0] for c in run), max(c["bbox"][2] for c in run)
    y0, y1 = min(c["bbox"][1] for c in run), max(c["bbox"][3] for c in run)
    c, s = d
    if abs(s) < 1e-6:
        return (x0, y1), (x1, y1), (x0, y0)
    a = abs(s)
    ln = ((x1 - x0) * c - (y1 - y0) * a) / (c * c - s * s)
    h = ((y1 - y0) * c - (x1 - x0) * a) / (c * c - s * s)
    if ln <= 0 or h <= 0:
        return None
    bl = (x0 - min(0.0, h * s), y1 - max(0.0, ln * s))
    return bl, (bl[0] + ln * c, bl[1] + ln * s), (bl[0] + h * s, bl[1] - h * c)


def _rebuild(page, gone: list, new: list) -> bool:
    """A page whose only words are the ones written invisibly over its picture: they are all written again, in
    their order, with the new words where the old ones (`gone`, of _letters) were - so they read (and are found) in
    their place.  Each word goes back where it was, leaning as it leant: a photographed page's lines lean, and
    Vision gives even a straight scan's short line a few degrees.  False: not this page (its lines run up, down or
    backwards, or have letters the invisible writing can't write) - nothing was changed."""
    old = _here(l[4] for l in gone)
    lines = []
    put = False
    shown = page.rotation_matrix                     # PyMuPDF's words are in the page's own space
    for b in page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            d = ln.get("dir") or (1, 0)
            d = (d[0] * shown.a + d[1] * shown.c, d[0] * shown.b + d[1] * shown.d)
            if d[0] <= 0 or abs(d[1]) > 0.5:
                return False
            line, run, tail = [], [], []

            def word():
                # the letters so far are a word.  The space written after it is part of it, so it reaches as far as
                # it did (to where the next word began when it was written - not to wherever the new word begins)
                if run:
                    at = _corners(run, d)
                    spaced = _corners(run + tail, d) if tail else None
                    if at is None or (tail and spaced is None):
                        return False
                    text = "".join(c["c"] for c in run)
                    if spaced and math.dist(spaced[0], spaced[1]) > math.dist(at[0], at[1]) + 0.05:
                        text, at = text + " ", spaced
                    line.append(scanwords.Word(text, *at))
                run.clear()
                tail.clear()
                return True
            for s in ln.get("spans", []):
                for ch in s.get("chars", []):
                    start = fitz.Point(ch["origin"]) * shown
                    box = {"c": ch["c"], "bbox": tuple(fitz.Rect(ch["bbox"]) * shown)}
                    if ch["c"].isspace():
                        if run:
                            tail.append(box)
                    elif old((start.x, start.y)):
                        if not word():
                            return False
                        if not put:
                            line.extend(new)
                            put = True
                    else:
                        if tail and not word():
                            return False
                        run.append(box)
            if not word():
                return False
            if line:
                lines.append(line)
    if not put:
        return False
    # a word with no space written after it reaches to the next and is given one, as words are first written
    spaced = []
    for line in lines:
        out = []
        for k, w in enumerate(line):
            if k + 1 < len(line) and not w.text.endswith(" "):
                nxt = line[k + 1].bl
                ax, ay = w.br[0] - w.bl[0], w.br[1] - w.bl[1]
                ln = math.hypot(ax, ay)
                reach = ((nxt[0] - w.bl[0]) * ax + (nxt[1] - w.bl[1]) * ay) / ln if ln > 0 else 0
                br = (w.bl[0] + ax / ln * reach, w.bl[1] + ay / ln * reach) if reach > ln else w.br
                w = scanwords.Word(w.text + " ", w.bl, br, w.tl)
            out.append(w)
        spaced.append(out)
    every = "".join(w.text for line in spaced for w in line)
    try:
        every.encode("cp1252")
    except UnicodeEncodeError:
        path = next((p for p in scanwords.ARIAL if scanwords._exists(p)), None)
        if not path:
            return False
        f = fitz.Font(fontfile=path)
        if not all(ch.isspace() or f.has_glyph(ord(ch)) for ch in every):
            return False
    big = fitz.Rect(-1e5, -1e5, 1e5, 1e5)
    page.add_redact_annot(big, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    scanwords.write(page, spaced)
    return True


def _grey(px: np.ndarray, r: sp.IntRect) -> np.ndarray:
    """A part of a picture in grey (to compare what was painted with what the saved page holds)."""
    c = r.clipped(px.shape[1], px.shape[0])
    return px[c.y:c.max_y, c.x:c.max_x].astype(np.float32) @ sp.LUM


def _store(doc, xref: int, px: np.ndarray, grey: bool):
    """Put a picture's changed pixels back into the file: a photo or scan stays a JPEG (kept as plain pixels it could
    be ten times the size)."""
    h, w = px.shape[:2]
    if grey:
        data = np.ascontiguousarray(px[:, :, 0]).tobytes()
        pix = fitz.Pixmap(fitz.csGRAY, w, h, data, False)
    else:
        data = np.ascontiguousarray(px).tobytes()
        pix = fitz.Pixmap(fitz.csRGB, w, h, data, False)
    kind, value = doc.xref_get_key(xref, "Filter")
    if value.replace("]", " ").split()[-1:] == ["/DCTDecode"]:
        doc.update_stream(xref, pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY), new=True, compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        doc.update_stream(xref, data, new=True, compress=True)
    doc.xref_set_key(xref, "DecodeParms", "null")
    for key, want in (("Width", w), ("Height", h), ("BitsPerComponent", 8)):
        doc.xref_set_key(xref, key, str(want))


# ---------------------------------------------------------------- finding the word

def find(page, pt, lines: list) -> Found | None:
    """The word of a scan under a point of the page (as shown).  `lines`: the page's words as read (read_lines).
    None: no words under the point."""
    if not lines:
        return None
    hit = _pick(pt, lines)
    if hit is None:
        return None
    li, wi = hit
    tapped = lines[li][wi]
    middle = ((tapped.br[0] + tapped.tl[0]) / 2, (tapped.br[1] + tapped.tl[1]) / 2)

    # words of the page's own that lie over a picture aren't the picture's
    tr = (tapped.tl[0] + tapped.br[0] - tapped.bl[0], tapped.tl[1] + tapped.br[1] - tapped.bl[1])
    xs, ys = [p[0] for p in (tapped.bl, tapped.br, tapped.tl, tr)], [p[1] for p in (tapped.bl, tapped.br, tapped.tl, tr)]
    letters = _letters(page)
    if _own_letters_in(letters, fitz.Rect(min(xs), min(ys), max(xs), max(ys))):
        return Found(cant=OWN_WORDS)

    # the picture the word is in: one picture, in a kind of colour that can be painted
    over = []
    for xref, m, info in _pictures(page):
        u, v = _unit(middle, m)
        if 0 <= u <= 1 and 0 <= v <= 1:
            over.append((xref, m, info))
    if not over:
        return Found(cant=OWN_WORDS)
    if len(over) != 1:
        return Found(cant=LAYERS)
    xref, m, info = over[0]
    doc = page.parent
    if not _paintable(doc, xref, info):
        return Found(cant=NOT_THESE)
    got = _pixels(doc, xref)
    if got is None:
        return Found(cant=NOT_THESE)
    px, grey = got
    H, W = px.shape[:2]
    # its pixels are square on the page (not a fax's, twice as tall as wide)
    across, down = W / max(0.001, math.hypot(m.a, m.b)), H / max(0.001, math.hypot(m.c, m.d))
    if abs(across / down - 1) >= 0.06:
        return Found(cant=NOT_THESE)

    # the words as they lie in the picture's own pixels
    inv = ~m

    def pixel(q):
        u = fitz.Point(q) * inv
        return (u.x * W, u.y * H)

    def reading(word):
        return sp.Reading(word.text, [pixel(word.bl), pixel(word.br), pixel(word.tl)])
    place = sp.look(px, [reading(w) for w in lines[li]], wi, [[reading(w) for w in l] for k, l in enumerate(lines) if k != li])
    word = sp.target("word", place) if place is not None else None
    if word is None:
        return Found(cant=NOT_THESE)
    targets = {"word": word}
    if len(lines[li]) > 1:
        line = sp.target("line", place)
        if line is not None:
            targets["line"] = line
    return Found(spot=Spot(xref, m, place, targets, (across + down) / 2, any(l[3] for l in letters), (W, H), grey))
