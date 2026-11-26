#!/usr/bin/env python3
"""
PDF Editor - annotate, fill, redact, fix text, rearrange pages.  Part of Toolkit.

    python pdf_editor.py [file.pdf]

Needs PyMuPDF:  py -m pip install pymupdf

Layout
    pdf_editor.py
      PdfModel   - every document operation, no GUI (so it can be tested or scripted)
      Editor     - the tkinter window

Coordinates: the canvas shows the page as displayed (rotation applied) at `zoom` pixels per
point.  PyMuPDF annotations live in UNROTATED page space, so every canvas point is converted
with  canvas -> /zoom -> * page.derotation_matrix.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk
    HAS_TK = True
except ImportError:      # no Tk (e.g. Homebrew Python, the Mac app): PdfModel works, only the old window needs it
    class _NoTk:
        Tk = Toplevel = object
    tk = _NoTk
    colorchooser = filedialog = messagebox = simpledialog = ttk = None
    HAS_TK = False

try:
    import pymupdf as fitz  # PyMuPDF (new import name)
except ImportError:
    try:
        import fitz  # older PyMuPDF
    except ImportError:  # pragma: no cover
        fitz = None

IS_WIN = platform.system() == "Windows"
CONFIG_DIR = Path.home() / ".toolkit"
SIGNATURE_FILE = CONFIG_DIR / "signature.json"

SERIF_NAMES = ("times", "serif", "georgia", "garamond", "cambria", "bookman", "bookantiqua", "minion", "palatino",
               "caslon", "baskerville", "didot", "bodoni", "schoolbook", "goudy")   # see serif_name


def serif_name(name: str) -> bool:
    """A font's name says it's a serif face (Gotham-Book isn't one; Book Antiqua is)."""
    n = name.lower().replace(" ", "").replace("-", "")
    return "sans" not in n and any(k in n for k in SERIF_NAMES)


FONTS = {"Helvetica": "helv", "Helvetica Bold": "hebo", "Times": "tiro", "Times Bold": "tibo",
         "Courier": "cour", "Courier Bold": "cobo"}


def hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def rgb_to_hex(rgb):
    if not rgb:
        return "#000000"
    return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb[:3])


# ------------------------------------------------------------- text helpers

_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
              "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "}


def _normalise(text: str) -> str:
    """Expand ligature glyphs that built-in fonts can't draw (ﬁ -> fi) and tidy whitespace."""
    for k, v in _LIGATURES.items():
        if k in ("\u2019", "\u2018", "\u201c", "\u201d"):
            continue  # keep typographic quotes - most fonts have them
        text = text.replace(k, v)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _turn_of(direction) -> int:
    """A line's writing direction (PyMuPDF's line "dir": cos, -sin) -> 0 / 90 / 180 / 270, the `rotate` that
    insert_text / insert_textbox need to write the same way (right, up, left, down on the unturned page)."""
    import math
    if not direction:
        return 0
    dx, dy = float(direction[0]), float(direction[1])
    return int(round(math.degrees(math.atan2(-dy, dx)) / 90.0)) % 4 * 90


def paragraph_box(base, extra: float, turn: int = 0):
    """The box a paragraph is retyped into: its own box, a hair longer along the lines and with room for a little more
    text where the lines go next (down the page for ordinary words; the other ways for words written turned)."""
    b = fitz.Rect(base)
    if turn == 90:          # lines run up the page; the next line is to the right
        return fitz.Rect(b.x0, b.y0 - 0.75, b.x1 + extra, b.y1)
    if turn == 180:         # run leftwards; the next line is above
        return fitz.Rect(b.x0 - 0.75, b.y0 - extra, b.x1, b.y1)
    if turn == 270:         # run down the page; the next line is to the left
        return fitz.Rect(b.x0 - extra, b.y0, b.x1, b.y1 + 0.75)
    return fitz.Rect(b.x0, b.y0, b.x1 + 0.75, b.y1 + extra)


def _text_space(r, turn: int):
    """A rectangle on the page -> measured along words written `turn` (x along a line, y from one line to the next)."""
    r = fitz.Rect(r)
    if turn == 90:          # words run up the page, lines follow to the right
        return fitz.Rect(-r.y1, r.x0, -r.y0, r.x1)
    if turn == 180:
        return fitz.Rect(-r.x1, -r.y1, -r.x0, -r.y0)
    if turn == 270:         # words run down the page, lines follow to the left
        return fitz.Rect(r.y0, -r.x1, r.y1, -r.x0)
    return r


def _paragraph_lines(blocks, hit, turn: int) -> list:
    """The lines of the paragraph `hit` is part of, for words written turned: neighbouring blocks with lines going
    the same way, of about the same size, that carry on line after line with no bigger gap than a line's own."""
    def info(b):
        ls = [l for l in b["lines"] if l["spans"]]
        if not ls or _turn_of(ls[0].get("dir")) != turn:
            return None
        sz = max(s["size"] for l in ls for s in l["spans"])
        return ls, _text_space(fitz.Rect(b["bbox"]), turn), sz
    first = info(hit)
    if first is None:
        return [l for l in hit["lines"] if l["spans"]]
    chosen, pool = [hit], [b for b in blocks if b is not hit]
    grew = True
    while grew:
        grew = False
        cur = fitz.Rect()
        for b in chosen:
            cur |= _text_space(fitz.Rect(b["bbox"]), turn)
        for b in list(pool):
            got = info(b)
            if got is None:
                continue
            ls, tr, sz = got
            if abs(sz - first[2]) > 0.6:
                continue
            gap = max(tr.y0 - cur.y1, cur.y0 - tr.y1)                  # between the paragraph and this block
            overlap = min(tr.x1, cur.x1) - max(tr.x0, cur.x0)          # along the lines
            if gap < sz * 0.8 and overlap > 0:
                chosen.append(b); pool.remove(b); grew = True
    return [l for b in chosen for l in b["lines"] if l["spans"]]


def _tracking(spans) -> float:
    """The letter spacing a paragraph was set with (a design program's "tracking"), in em: how much closer (or further
    apart) neighbouring letters are than their own widths - the median, so a kerned pair here and there doesn't count.
    0 for ordinary text. (A heading set 0.06 em tight would not fit its own box at its own size without it.)"""
    ds = []
    for sp in spans:
        ch, size = sp.get("chars") or [], sp.get("size") or 0
        for a, c in zip(ch, ch[1:]):
            if size and not a["c"].isspace() and not c["c"].isspace():
                ds.append((c["origin"][0] - a["origin"][0] - (a["bbox"][2] - a["bbox"][0])) / size)
    if len(ds) < 4:
        return 0.0
    t = sorted(ds)[len(ds) // 2]
    return round(max(-0.2, min(t, 0.5)), 3) if abs(t) >= 0.01 else 0.0


def _unseen(span) -> bool:
    """Written invisibly (the words a scanner, or "Make its words selectable", lays over the picture of a page)."""
    return span.get("alpha", 255) == 0


def font_object(fontname: str, fontbuffer=None):
    """A PyMuPDF Font for measuring and writing: the document's / this PC's font, else a built-in (helv, tiro, ...)."""
    return fitz.Font(fontbuffer=fontbuffer) if fontbuffer is not None else fitz.Font(fontname)


def layout_paragraph(text: str, font, size: float, box, lineheight: float, align: int, tracking: float = 0.0,
                     first_baseline: float | None = None):
    """Lay words out in a box (unturned text) as the page had them: the paragraph's letter spacing (`tracking`, em)
    kept, justified lines filled to the right edge, a word too long for a line split.  -> (lines, spare_lines) with
    lines = [(x, baseline, text, extra_per_space)], or None when it doesn't fit at this size.  `first_baseline`: where
    the original's first line sat (else the font's ascender below the top)."""
    box = fitz.Rect(box)
    track = tracking * size
    def width(t):
        return font.text_length(t, fontsize=size) + track * max(len(t) - 1, 0) if t else 0.0
    space = width(" ") + track
    maxw = box.width
    rows = []                                    # (text, may justify)
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}" if line else word
            if not line or width(cand) <= maxw:
                if width(cand) <= maxw:
                    line = cand
                    continue
            if line:
                rows.append((line, True))
                line = ""
            while width(word) > maxw:            # a word longer than a line: split it, as PyMuPDF did
                k = max(1, max(n for n in range(1, len(word) + 1) if n == 1 or width(word[:n]) <= maxw))
                rows.append((word[:k], False))
                word = word[k:]
            line = word
        rows.append((line, False))               # a paragraph's last line is never stretched
    asc, desc = font.ascender, font.descender
    lh = size * lineheight
    top = first_baseline if first_baseline is not None else box.y0 + asc * size
    last = top + lh * (len(rows) - 1)
    room = box.y1 - (last - desc * size)
    if room < -0.01:
        return None
    out = []
    for k, (t, stretch) in enumerate(rows):
        w = width(t)
        x, extra = box.x0, 0.0
        if align == 1:
            x += (maxw - w) / 2
        elif align == 2:
            x += maxw - w
        elif align == 3 and stretch and t.count(" "):
            extra = (maxw - w) / t.count(" ")
        out.append((x, top + lh * k, t, extra))
    return out, int(room // lh) if lh else 0


def write_lines(pg, lines, font, size: float, color, tracking: float = 0.0):
    """Write laid-out lines (layout_paragraph) on the page, letter by letter where the letters are spaced, so the
    letter spacing and justified word gaps are exactly as laid out."""
    tw = fitz.TextWriter(pg.rect)
    track = tracking * size
    for x, y, t, extra in lines:
        if not track and not extra:
            tw.append((x, y), t, font=font, fontsize=size)
            continue
        for c in t:
            tw.append((x, y), c, font=font, fontsize=size)
            x += font.text_length(c, fontsize=size) + track + (extra if c == " " else 0)
    tw.write_text(pg, color=color)


def _letters(pg, skip=()) -> dict:
    """Every letter on a page (with where it is), less the spans in `skip` - to check an edit left the rest alone."""
    from collections import Counter
    out = Counter()
    keep = {(round(o[0], 1), round(o[1], 1), round(z, 1)) for _, o, z in skip}
    for b in pg.get_text("rawdict")["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                if (round(sp["origin"][0], 1), round(sp["origin"][1], 1), round(sp["size"], 1)) in keep:
                    continue
                for c in sp["chars"]:
                    if not c["c"].isspace():
                        out[(c["c"], round(c["origin"][0]), round(c["origin"][1]))] += 1
    return out


def _join_lines(lines: list[str]) -> str:
    """Turn the wrapped lines of one text block back into flowing text.
    A line break is kept only where it looks deliberate (blank line, bullet/number start, or a very short
    previous line); hyphenated breaks are mended."""
    out = ""
    for idx, ln in enumerate(lines):
        ln = ln.strip()
        if idx == 0:
            out = ln; continue
        prev = lines[idx - 1].strip()
        if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and ln[:1].islower():
            out = out[:-1] + ln            # mend "specifi-" + "cally"
            continue
        deliberate = (not prev or not ln or ln[:1] in "•-–—*·" or (ln[:2].rstrip(".)").isdigit() and ln[1:3].strip(" .)") != "")
                      or len(prev) < 0.45 * max(len(x) for x in lines))
        out += ("\n" if deliberate else " ") + ln
    return out


def _font_family(name: str) -> str:
    """'ABCDEF+NotoSerif-Bold' -> 'NotoSerif-Bold'; strips subset prefix only (style kept for matching)."""
    if "+" in name and len(name.split("+")[0]) == 6:
        name = name.split("+", 1)[1]
    return name.strip()


def _font_key(name: str) -> str:
    return "".join(c for c in _font_family(name).lower() if c.isalnum())


def _same_font(a: str, b: str) -> bool:
    """'Arial Unicode MS Regular' (a PDF's font name) is the same as 'ArialUnicodeMS' (the name its text reports)."""
    ka, kb = _font_key(a), _font_key(b)
    return ka == kb or ka.removesuffix("regular") == kb.removesuffix("regular")


_SYSTEM_FONTS: dict[str, str] | None = None
_FONT_LOCK = threading.Lock()


def _system_font_index() -> dict[str, str]:
    """Map normalised font name -> file, for fonts installed on this PC (cached on disk).
    Built once (in the background at startup); later calls return instantly."""
    with _FONT_LOCK:
        return _build_font_index()


def _build_font_index() -> dict[str, str]:
    global _SYSTEM_FONTS
    if _SYSTEM_FONTS is not None:
        return _SYSTEM_FONTS
    cache = CONFIG_DIR / "fontcache.json"
    dirs = []
    if IS_WIN:
        dirs += [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"]
    elif platform.system() == "Darwin":
        dirs += [Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path.home() / ".fonts", Path.home() / ".local/share/fonts"]
    files = [p for d in dirs if d.exists() for p in d.rglob("*") if p.suffix.lower() in (".ttf", ".otf")]
    try:
        cached = json.loads(cache.read_text())
        if cached.get("n") == len(files):
            _SYSTEM_FONTS = cached["map"]; return _SYSTEM_FONTS
    except Exception:
        pass
    idx = {}
    for p in files:
        try:
            f = fitz.Font(fontfile=str(p))
            idx[_font_key(f.name)] = str(p)
            idx.setdefault(_font_key(p.stem), str(p))
        except Exception:
            continue
    _SYSTEM_FONTS = idx
    try:
        CONFIG_DIR.mkdir(exist_ok=True); cache.write_text(json.dumps({"n": len(files), "map": idx}))
    except Exception:
        pass
    return idx


def _system_font_path(span_font: str, flags: int) -> str | None:
    idx = _system_font_index()
    key = _font_key(span_font)
    if key in idx:
        return idx[key]
    # try family + style words, e.g. 'Calibri' + 'Bold'
    fam = key
    for suffix in ("mt", "psmt", "regular", "roman"):
        if fam.endswith(suffix):
            fam = fam[: -len(suffix)]
    bold, italic = bool(flags & 16) or "bold" in key, bool(flags & 2) or "italic" in key
    for cand in ([fam + "bolditalic", fam + "boldoblique", fam + "bi", fam + "z"] if bold and italic else
                 [fam + "bold", fam + "bd", fam + "b"] if bold else
                 [fam + "italic", fam + "oblique", fam + "i"] if italic else
                 [fam, fam + "regular", fam + "mt"]):
        if cand in idx:
            return idx[cand]
    for k, v in idx.items():               # last resort: family name is a prefix
        if k.startswith(fam) and bold == ("bold" in k) and italic == ("italic" in k or "oblique" in k):
            return v
    return None


def _clear_field(page, widget):
    """Empty a text/choice form box.  Widget.update() only writes non-empty values, so set /V ourselves."""
    try:
        mupdf = fitz.mupdf
        pa = widget._annot.this
        mupdf.pdf_set_field_value(mupdf.pdf_annot_page(pa).doc(), mupdf.pdf_annot_obj(pa), "", 1)
        mupdf.pdf_update_annot(pa)
    except Exception:                      # other PyMuPDF builds: write /V on the field that holds it
        doc, xref = page.parent, widget.xref
        if doc.xref_get_key(xref, "V")[0] == "null" and doc.xref_get_key(xref, "Parent")[0] == "xref":
            xref = int(doc.xref_get_key(xref, "Parent")[1].split()[0])
        doc.xref_set_key(xref, "V", "()")
        widget.update()


# ====================================================================== model

class PdfModel:
    """All document edits.  Keeps an undo/redo stack of serialized documents."""

    def __init__(self, path: str | None = None):
        if fitz is None:
            raise RuntimeError("PyMuPDF is not installed.  Run:  py -m pip install pymupdf")
        self.path: Path | None = Path(path) if path else None
        self.doc = fitz.open(str(path)) if path else fitz.open()
        if not path:
            self.doc.new_page()
        self.undo_stack: list[bytes] = []
        self.redo_stack: list[bytes] = []
        self.dirty = False
        self._held_page = None  # widgets/annots only weakly reference their page; we keep it alive here

    # ---- housekeeping
    def __len__(self):
        return len(self.doc)

    def page(self, i: int):
        return self.doc[i]

    def snapshot(self):
        """Call BEFORE a change."""
        self.undo_stack.append(self.doc.tobytes(garbage=0, deflate=False))
        if len(self.undo_stack) > 40:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.dirty = True

    def _restore(self, data: bytes):
        self.doc.close()
        self.doc = fitz.open("pdf", data)
        self.dirty = True

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.doc.tobytes(garbage=0, deflate=False))
        self._restore(self.undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.doc.tobytes(garbage=0, deflate=False))
        self._restore(self.redo_stack.pop())
        return True

    def save(self, path: str | Path, user_pw: str = "", owner_pw: str = ""):
        path = Path(path)
        kw = dict(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)   # no password given: none in the file
        if user_pw or owner_pw:
            kw.update(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=user_pw, owner_pw=owner_pw or user_pw,
                      permissions=fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
        # write to a temp file first so a failure never damages the target
        tmp = path.with_suffix(path.suffix + ".tmp")
        self.doc.save(str(tmp), **kw)
        shutil.move(str(tmp), str(path))
        self.path = path
        self.dirty = False

    # ---- rendering
    def render(self, i: int, zoom: float):
        pix = self.doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix  # caller turns it into a PhotoImage via pix.tobytes("ppm")

    # ---- coordinate helpers
    def to_page(self, i: int, x: float, y: float, zoom: float):
        """canvas pixels -> unrotated page point"""
        pg = self.doc[i]
        return fitz.Point(x / zoom, y / zoom) * pg.derotation_matrix

    def rect_to_page(self, i: int, x0, y0, x1, y1, zoom):
        p0, p1 = self.to_page(i, x0, y0, zoom), self.to_page(i, x1, y1, zoom)
        return fitz.Rect(min(p0.x, p1.x), min(p0.y, p1.y), max(p0.x, p1.x), max(p0.y, p1.y))

    def rect_to_canvas(self, i: int, r, zoom):
        """unrotated page rect -> canvas pixel bbox"""
        pg = self.doc[i]
        q = fitz.Rect(r) * pg.rotation_matrix
        return (q.x0 * zoom, q.y0 * zoom, q.x1 * zoom, q.y1 * zoom)

    # ---- pages
    def rotate(self, i: int, deg: int):
        self.snapshot(); pg = self.doc[i]; pg.set_rotation((pg.rotation + deg) % 360)

    def delete_page(self, i: int):
        if len(self.doc) == 1:
            raise ValueError("A PDF needs at least one page.")
        self.snapshot(); self.doc.delete_page(i)

    def move_page(self, i: int, to: int):
        if i == to or not 0 <= to < len(self.doc):
            return
        # PyMuPDF puts the page before the target; "after the last page" must be -1 (2.0 refuses len(doc))
        self.snapshot(); self.doc.move_page(i, to if to < i else (to + 1 if to + 1 < len(self.doc) else -1))

    def duplicate_page(self, i: int):
        self.snapshot(); self.doc.fullcopy_page(i, i + 1)

    def insert_pdf(self, i: int, other_path: str):
        self.snapshot()
        with fitz.open(other_path) as other:
            self.doc.insert_pdf(other, start_at=i + 1)

    def insert_blank(self, i: int):
        self.snapshot(); r = self.doc[i].rect; self.doc.new_page(pno=i + 1, width=r.width, height=r.height)

    def extract_pages(self, pages: list[int], out_path: str):
        new = fitz.open()
        for p in pages:
            new.insert_pdf(self.doc, from_page=p, to_page=p)
        new.save(out_path, garbage=3, deflate=True); new.close()

    def export_png(self, i: int, out_path: str, dpi: int = 150):
        self.doc[i].get_pixmap(dpi=dpi).save(out_path)

    def crop(self, i: int, rect):
        self.snapshot(); pg = self.doc[i]
        self.doc[i].set_cropbox(fitz.Rect(rect) & pg.mediabox)

    # ---- text helpers
    def words_in(self, i: int, rect):
        """rects of words that intersect rect (for highlight tools)"""
        return [fitz.Rect(w[:4]) for w in self.doc[i].get_text("words") if fitz.Rect(w[:4]).intersects(rect)]

    def span_at(self, i: int, pt):
        """the text span under a point: dict(text,size,color,font,bbox,origin,turn) or None (turn: its line's direction,
        0/90/180/270 - a page stored sideways has its words turned against it)"""
        for b in self.doc[i].get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for line in b["lines"]:
                for s in line["spans"]:
                    if _unseen(s):
                        continue
                    if fitz.Rect(s["bbox"]).contains(pt):
                        turn = _turn_of(line.get("dir"))
                        return {**s, "turn": turn, "fontxref": self.font_xref(i, s, turn)}
        return None

    def font_xref(self, i: int, span, turn: int = 0):
        """Which of the page's fonts a span is written in, when several have the same name - else None.

        A webpage made into a PDF (Chrome, Edge, WebView2) names every web font the same few garbled letters, so the
        name alone picked the first of them: a bold serif headline was retyped in the page's plain sans, or in
        Helvetica when that one lacked a letter (16 Nov, BBC News).  MuPDF doesn't say which font object a span uses,
        so the words are measured in each candidate and the one that comes out the span's own width is taken."""
        fam = _font_family(span.get("font", ""))
        text = span.get("text", "")
        xrefs = []
        for entry in self.doc[i].get_fonts(full=True):
            if _same_font(entry[3], fam) and entry[0] not in xrefs:
                xrefs.append(entry[0])
        if len(xrefs) < 2 or not text.strip():
            return None
        box = _text_space(fitz.Rect(span["bbox"]), turn)
        best = None
        for xref in xrefs:
            try:
                buf = self.doc.extract_font(xref)[3]
                f = fitz.Font(fontbuffer=buf)
                if not all(f.has_glyph(ord(c)) for c in text if not c.isspace()):
                    continue                   # the span's own letters aren't all in it: not its font
                off = abs(f.text_length(text, span["size"]) - box.width)
            except Exception:
                continue
            if best is None or off < best[0]:
                best = (off, xref)
        return best[1] if best else None

    def block_at(self, i: int, pt):
        """The paragraph under a point, analysed: text (reflowed), font, size, colour, alignment, line height, turn.
        Words written turned (a page stored sideways) are measured along their own direction; MuPDF may give each such
        line a block of its own, so neighbouring lines of the same paragraph are joined back up."""
        blocks = [b for b in self.doc[i].get_text("rawdict")["blocks"] if b.get("type") == 0]
        for b in blocks:                       # the words written invisibly over a scan aren't words to retype
            for l in b["lines"]:
                l["spans"] = [s for s in l["spans"] if not _unseen(s)]
        blocks = [b for b in blocks if any(l["spans"] for l in b["lines"])]
        for b in blocks:
            for l in b["lines"]:
                for s in l["spans"]:
                    s["text"] = "".join(c["c"] for c in s["chars"])
        hit = next((b for b in blocks if fitz.Rect(b["bbox"]).contains(pt)), None)
        if hit is None:
            return None
        lines = [l for l in hit["lines"] if l["spans"]]
        turn = _turn_of(lines[0].get("dir"))
        if turn:
            lines = _paragraph_lines(blocks, hit, turn)
        # positions measured along the words (x: along a line, y: line after line), whatever the turn
        T = lambda r: _text_space(fitz.Rect(r), turn)
        lines.sort(key=lambda l: T(l["bbox"]).y0)
        # dominant span = the one with the most characters (sets font/size/colour)
        spans = [s for l in lines for s in l["spans"]]
        main = max(spans, key=lambda s: len(s["text"]))
        size = main["size"]
        # join wrapped lines into flowing text; keep a break only where a line is clearly a new paragraph
        raw = []
        for l in lines:
            raw.append("".join(s["text"] for s in l["spans"]).rstrip())
        text = _join_lines(raw)
        bbox = fitz.Rect()
        for l in lines:
            bbox |= fitz.Rect(l["bbox"])
        tb = _text_space(bbox, turn)
        # alignment from geometry
        tol = max(2.0, size * 0.35)
        body = lines[:-1] if len(lines) > 1 else lines   # last line of a justified paragraph is ragged
        left_ok = all(abs(T(l["bbox"]).x0 - tb.x0) < tol for l in body)
        right_ok = all(abs(T(l["bbox"]).x1 - tb.x1) < tol for l in body)
        if len(lines) > 1 and left_ok and right_ok:
            align = fitz.TEXT_ALIGN_JUSTIFY
        elif left_ok:
            align = fitz.TEXT_ALIGN_LEFT
        elif right_ok:
            align = fitz.TEXT_ALIGN_RIGHT
        else:
            centres = [(T(l["bbox"]).x0 + T(l["bbox"]).x1) / 2 for l in lines]
            align = fitz.TEXT_ALIGN_CENTER if all(abs(c - (tb.x0 + tb.x1) / 2) < tol for c in centres) else fitz.TEXT_ALIGN_LEFT
        # leading from baselines (measured line after line)
        origins = [_text_space(fitz.Rect(fitz.Point(l["spans"][0]["origin"]), fitz.Point(l["spans"][0]["origin"])), turn).y0
                   for l in lines]
        gaps = [b2 - b1 for b1, b2 in zip(origins, origins[1:]) if b2 > b1]
        lineheight = (sorted(gaps)[len(gaps) // 2] / size) if gaps and size else 1.2
        return dict(bbox=bbox, text=text, size=size, color=main.get("color", 0), font=main.get("font", ""),
                    fontxref=self.font_xref(i, main, turn),
                    flags=main.get("flags", 0), align=align, lineheight=max(0.9, min(lineheight, 2.5)),
                    first_origin=fitz.Point(lines[0]["spans"][0]["origin"]), turn=turn,
                    tracking=0.0 if turn else _tracking(spans),
                    spans=[(tuple(sp["bbox"]), tuple(sp["origin"]), sp["size"]) for sp in spans if sp["text"].strip()])

    # ---- fonts
    def resolve_font(self, i: int, span_font: str, flags: int, text: str, xref: int | None = None):
        """Pick the best font for re-inserting text that was set in `span_font`.
        `xref`: which of the page's fonts that is, when several share the name (font_xref).

        Returns (fontname_for_insert, fontbuffer_or_None, human_description).
        Order: the document's embedded font (if it has every glyph) -> same font installed on this PC
               -> closest built-in (serif/sans/mono x bold/italic)."""
        needed = {c for c in text if not c.isspace()}
        pg = self.doc[i]
        fam = _font_family(span_font)
        flags = self.true_flags(i, span_font, flags, xref)
        want = xref
        # 1. embedded font in this PDF
        for entry in pg.get_fonts(full=True):
            xref, ext, ftype, basefont = entry[:4]
            if not _same_font(basefont, fam) or (want is not None and xref != want):
                continue
            try:
                base, ext2, ftype2, buf = self.doc.extract_font(xref)
            except Exception:
                continue
            if not buf or ext2 in ("n/a", ""):
                break
            try:
                f = fitz.Font(fontbuffer=buf)
                if all(f.has_glyph(ord(c)) for c in needed):
                    return f"TK{xref}", buf, f"original font ({_font_family(basefont)}, embedded)"
            except Exception:
                pass
            break
        # 2. installed on this computer
        path = _system_font_path(span_font, flags)
        if path:
            try:
                f = fitz.Font(fontfile=path)
                if all(f.has_glyph(ord(c)) for c in needed):
                    return "TKSYS" + str(abs(hash(path)) % 10**8), Path(path).read_bytes(), f"{Path(path).stem} (installed on this PC)"
            except Exception:
                pass
        # 3. closest built-in
        serif, mono, bold, italic = bool(flags & 4), bool(flags & 8), bool(flags & 16), bool(flags & 2)
        if not (serif or mono) and serif_name(fam):
            serif = True
        if any(k in fam.lower() for k in ("courier", "mono", "consol")):
            mono = True
        if "bold" in span_font.lower() or "black" in span_font.lower():
            bold = True
        if "italic" in span_font.lower() or "oblique" in span_font.lower():
            italic = True
        table = {("mono", False, False): "cour", ("mono", True, False): "cobo", ("mono", False, True): "coit", ("mono", True, True): "cobi",
                 ("serif", False, False): "tiro", ("serif", True, False): "tibo", ("serif", False, True): "tiit", ("serif", True, True): "tibi",
                 ("sans", False, False): "helv", ("sans", True, False): "hebo", ("sans", False, True): "heit", ("sans", True, True): "hebi"}
        kind = "mono" if mono else ("serif" if serif else "sans")
        name = table[(kind, bold, italic)]
        return name, None, f"built-in {name} (original '{fam}' not available)"

    def true_flags(self, i: int, span_font: str, flags: int, want: int | None = None) -> int:
        """MuPDF's flags for a span, with "serif" (4) as the PDF itself says in the font's descriptor: MuPDF reports
        serif for fonts it can't tell about (Gotham, embedded by InDesign, came out serif - so a stand-in was Times)."""
        fam = _font_family(span_font)
        try:
            for entry in self.doc[i].get_fonts(full=True):
                xref, basefont = entry[0], entry[3]
                if not _same_font(basefont, fam) or (want is not None and xref != want):
                    continue
                kind, fd = self.doc.xref_get_key(xref, "FontDescriptor")
                if kind != "xref":                         # a Type0 font: the descriptor is its descendant's
                    kind, df = self.doc.xref_get_key(xref, "DescendantFonts")
                    ref = df.strip("[] ").split(" ")[0] if kind in ("array", "xref") else ""
                    if not ref.isdigit():
                        return flags
                    if kind == "xref":
                        arr = self.doc.xref_object(int(ref)).strip("[] ").split(" ")
                        ref = arr[0] if arr and arr[0].isdigit() else ""
                        if not ref:
                            return flags
                    kind, fd = self.doc.xref_get_key(int(ref), "FontDescriptor")
                    if kind != "xref":
                        return flags
                kind, v = self.doc.xref_get_key(int(fd.split(" ")[0]), "Flags")
                if kind == "int":
                    return (flags & ~4) | (4 if int(v) & 2 else 0)
                return flags
        except Exception:
            pass
        return flags

    def _use_font(self, pg, fontname, buf):
        if buf is not None:
            pg.insert_font(fontname=fontname, fontbuffer=buf)
        return fontname

    def search(self, text: str):
        """[(page, rect), ...]"""
        hits = []
        for i, pg in enumerate(self.doc):
            hits += [(i, r) for r in pg.search_for(text)]
        return hits

    def page_text(self, i: int) -> str:
        return self.doc[i].get_text()

    # ---- annotations
    def _finish(self, annot, color=None, width=None, fill=None, opacity=None):
        if color is not None or fill is not None:
            annot.set_colors(stroke=color, fill=fill)
        if width is not None:
            annot.set_border(width=width)
        if opacity is not None:
            annot.set_opacity(opacity)
        annot.update()
        return annot

    def add_markup(self, i: int, kind: str, rect, color):
        rects = self.words_in(i, rect) or [fitz.Rect(rect)]
        self.snapshot(); pg = self.doc[i]
        fn = {"highlight": pg.add_highlight_annot, "underline": pg.add_underline_annot,
              "strikeout": pg.add_strikeout_annot, "squiggly": pg.add_squiggly_annot}[kind]
        return self._finish(fn(rects), color=color)

    def add_markup_rects(self, i: int, kind: str, rects, color):
        """Highlight / underline / strike the given rectangles (one per text line) as a single annotation."""
        rects = [fitz.Rect(r) for r in rects if fitz.Rect(r).width > 0.5 and fitz.Rect(r).height > 0.5]
        if not rects:
            raise ValueError("Drag across some text first.")
        self.snapshot(); pg = self.doc[i]
        fn = {"highlight": pg.add_highlight_annot, "underline": pg.add_underline_annot,
              "strikeout": pg.add_strikeout_annot, "squiggly": pg.add_squiggly_annot}[kind]
        return self._finish(fn(rects), color=color)

    def add_ink(self, i: int, strokes, color, width):
        self.snapshot()
        # PyMuPDF insists on plain (x, y) float pairs, not Point objects
        plain = [[(float(p[0]), float(p[1])) for p in stroke] for stroke in strokes]
        return self._finish(self.doc[i].add_ink_annot(plain), color=color, width=width)

    def add_rect(self, i: int, rect, color, width, fill=None, ellipse=False):
        self.snapshot(); pg = self.doc[i]
        a = (pg.add_circle_annot if ellipse else pg.add_rect_annot)(rect)
        return self._finish(a, color=color, width=width, fill=fill)

    def add_line(self, i: int, p1, p2, color, width, arrow=False):
        self.snapshot(); a = self.doc[i].add_line_annot(p1, p2)
        if arrow:
            a.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_OPEN_ARROW)
        return self._finish(a, color=color, width=width)

    def add_textbox(self, i: int, pt, text, size, color, fontname="helv", border=False):
        """A text box with its top-left corner at pt (a point on the page) - laid out as the page is shown, so on a
        page stored sideways (/Rotate) it reads the right way up."""
        self.snapshot()
        pg = self.doc[i]
        lines = text.split("\n")
        w = max(fitz.get_text_length(l, fontname=fontname, fontsize=size) for l in lines) + 6
        h = size * 1.3 * len(lines) + 6
        sp = fitz.Point(pt) * pg.rotation_matrix                   # page -> as shown
        rect = fitz.Rect(sp.x, sp.y, sp.x + w, sp.y + h) * pg.derotation_matrix
        a = pg.add_freetext_annot(rect, text, fontsize=size, fontname=fontname, text_color=color,
                                  fill_color=None, border_color=color if border else None, rotate=pg.rotation)
        a.update(); return a

    def add_note(self, i: int, pt, text, color=(1, 0.8, 0)):
        self.snapshot(); a = self.doc[i].add_text_annot(pt, text, icon="Note")
        return self._finish(a, color=color)

    def add_image(self, i: int, rect, filename):
        self.snapshot(); self.doc[i].insert_image(rect, filename=filename, keep_proportion=True)

    def add_typed_signature(self, i: int, rect, name: str, fontfile: str | None, color):
        """Write a name in a handwriting font, scaled to fit rect (vector text, not an image)."""
        self.snapshot(); pg = self.doc[i]
        rect = fitz.Rect(rect)
        if fontfile:
            fname = "TKSIG" + str(abs(hash(fontfile)) % 10**8)
            pg.insert_font(fontname=fname, fontfile=fontfile)
            f = fitz.Font(fontfile=fontfile)
        else:
            fname = "tiit"; f = fitz.Font("tiit")
        w1 = f.text_length(name, fontsize=1) or 1
        size = min((rect.width - 4) / w1, rect.height / (f.ascender - f.descender or 1.2) * 0.9)
        size = max(4, size)
        text_w = w1 * size
        x = rect.x0 + (rect.width - text_w) / 2
        baseline = rect.y0 + (rect.height + size * (f.ascender + f.descender)) / 2  # vertical centre
        pg.insert_text(fitz.Point(x, baseline), name, fontsize=size, fontname=fname, color=color)

    def add_stamp_text(self, i: int, pt, text, size, color, fontname="helv"):
        """Text burned into the page content (not an annotation)."""
        self.snapshot(); self.doc[i].insert_text(pt, text, fontsize=size, fontname=fontname, color=color)

    def redact(self, i: int, rect, fill=(0, 0, 0)):
        self.snapshot(); pg = self.doc[i]
        pg.add_redact_annot(rect, fill=fill)
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    def redact_rects(self, i: int, rects, fill=(0, 0, 0)):
        """Permanently remove everything inside several areas (one undo step)."""
        self.snapshot(); pg = self.doc[i]
        for r in rects:
            pg.add_redact_annot(fitz.Rect(r), fill=fill)
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

    def redact_words(self, i: int, bands, boxes, fill=(0, 0, 0)):
        """Selected words (one undo step): take out the letters under the thin bands (see bridge_edit.text_redaction),
        then black out the boxes (or `fill` them) - pictures and drawings under them too, but no more letters."""
        self.snapshot(); pg = self.doc[i]
        for r in bands:
            pg.add_redact_annot(fitz.Rect(r), fill=False)
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                            text=fitz.PDF_REDACT_TEXT_REMOVE)
        for r in boxes:
            pg.add_redact_annot(fitz.Rect(r), fill=fill)
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, text=fitz.PDF_REDACT_TEXT_NONE)

    def _erase(self, pg, rect):
        """Remove text (only) inside rect, leaving a white fill. Shrunk slightly so neighbouring lines survive."""
        r = fitz.Rect(rect); r.y0 += 0.5; r.y1 -= 0.5
        pg.add_redact_annot(r, fill=(1, 1, 1))
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)

    def _erase_spans(self, pg, spans, turn: int = 0, fallback=None):
        """Take exactly these lines' letters out of the page.  MuPDF removes every letter whose font box touches a
        redaction box, and a font's box reaches well above and below its letters - so a box over the whole paragraph
        also took the line above it when lines are set close (a heading right under a title).  Instead: a thin strip
        through the middle of each line's letters, with no white fill (a coloured background stays as it was).
        Turned words: the paragraph's box, as before.  Then a check that every other letter on the page is still
        there (else ValueError - the caller undoes)."""
        if turn or not spans:
            return self._erase(pg, fallback)
        before = _letters(pg, spans)
        for bbox, origin, size in spans:
            y = origin[1] - 0.35 * size
            pg.add_redact_annot(fitz.Rect(bbox[0] + 0.25, y - 0.05 * size, max(bbox[0] + 0.5, bbox[2] - 0.25), y + 0.05 * size),
                                fill=False)
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        after = _letters(pg)
        if any(after[k] < n for k, n in before.items()):
            raise ValueError("Changing this text would have removed words next to it too - edit undone.")

    def whiteout_retype(self, i: int, span, new_text, size, color, fontname=None, fontbuffer=None):
        """Remove the text of one span and write replacement text at the same baseline, same font if possible."""
        new_text = _normalise(new_text)
        if fontname is None:
            fontname, fontbuffer, _ = self.resolve_font(i, span.get("font", ""), span.get("flags", 0), new_text,
                                                        span.get("fontxref"))
        self.snapshot(); pg = self.doc[i]
        try:                                   # NB: redaction drops unused page fonts, so register ours after it
            self._erase_spans(pg, [(tuple(span["bbox"]), tuple(span["origin"]), span["size"])], span.get("turn", 0), span["bbox"])
        except ValueError:
            self.undo()
            raise
        if new_text.strip():
            try:
                self._use_font(pg, fontname, fontbuffer)
                pg.insert_text(fitz.Point(span["origin"]), new_text, fontsize=size, fontname=fontname, color=color,
                               rotate=span.get("turn", 0))
            except Exception:
                pg.insert_text(fitz.Point(span["origin"]), new_text, fontsize=size, fontname="helv", color=color,
                               rotate=span.get("turn", 0))

    def replace_block(self, i: int, block, new_text, size, color, fontname=None, fontbuffer=None,
                      align=None, lineheight=None):
        """Remove a paragraph and re-lay new text inside the same box: same font, size, alignment and
        line spacing as the original where possible; shrinks the size only if the text no longer fits.
        Returns the size actually used."""
        new_text = _normalise(new_text)
        if fontname is None:
            fontname, fontbuffer, _ = self.resolve_font(i, block.get("font", ""), block.get("flags", 0), new_text,
                                                        block.get("fontxref"))
        align = block.get("align", 0) if align is None else align
        lineheight = block.get("lineheight") if lineheight is None else lineheight
        self.snapshot(); pg = self.doc[i]
        base = fitz.Rect(block["bbox"])
        turn = block.get("turn", 0)
        # erase exactly the old paragraph; write into a box with the same left/right edges (so wrapping
        # matches) that may extend a little into the blank gap below when the new text is longer
        try:
            self._erase_spans(pg, block.get("spans"), turn, base)
        except ValueError:
            self.undo()
            raise
        box = paragraph_box(base, size * lineheight * 0.6, turn)

        def attempt(font, buf):
            if not turn:
                try:
                    f = font_object(font, buf)
                except Exception:
                    return None
                s = size
                while s >= 4:
                    laid = self.lay_out(block, new_text, s, align, f, size, lineheight)
                    if laid:
                        try:
                            write_lines(pg, laid[0], f, s, color, block.get("tracking", 0.0))
                        except Exception:
                            return None
                        return s
                    s -= 0.5
                return None
            s = size
            while s >= 4:
                try:
                    self._use_font(pg, font, buf)   # after the erase: redaction drops unused page fonts
                    kw = dict(fontsize=s, fontname=font, color=color, align=align, rotate=turn)
                    try:
                        rc = pg.insert_textbox(box, new_text, lineheight=lineheight, **kw)
                    except TypeError:            # older PyMuPDF without lineheight
                        rc = pg.insert_textbox(box, new_text, **kw)
                except Exception:
                    return None                  # this font can't be used at all
                if rc >= 0:                      # insert_textbox writes nothing when it doesn't fit
                    return s
                s -= 0.5
            return None

        used = attempt(fontname, fontbuffer)
        if used is None and fontbuffer is not None:
            used = attempt("helv" if not (block.get("flags", 0) & 4) else "tiro", None)
        if used is None:
            self.undo()
            raise ValueError("The new text could not be laid out in the paragraph's box - edit undone.")
        return used

    def lay_out(self, block, text, s, align, font, size=None, lineheight=None):
        """A paragraph's new words laid out at size `s` in its box (layout_paragraph): its letter spacing, line height,
        and first line where the original's was (moved up in proportion when the size is smaller)."""
        size = size or block["size"]
        lineheight = lineheight or block.get("lineheight") or 1.2
        base = fitz.Rect(block["bbox"])
        box = paragraph_box(base, size * lineheight * 0.6, 0)
        box.x1 = base.x1                       # justified lines end where the original's did ...
        fo = block.get("first_origin")
        first = base.y0 + (fo.y - base.y0) * s / size if fo is not None else None
        laid = layout_paragraph(_normalise(text), font, s, fitz.Rect(box.x0, box.y0, box.x1 + 0.75, box.y1), lineheight,
                                align, block.get("tracking", 0.0), first)   # ... and a line may run 0.75 pt past it
        if laid and align == 3:
            laid = ([(x, y, t, e - 0.75 / t.count(" ") if e and t.count(" ") else e) for x, y, t, e in laid[0]], laid[1])
        return laid

    def annot_at(self, i: int, pt):
        self._held_page = self.doc[i]
        for a in self._held_page.annots():
            if a.rect.contains(pt):
                return a
        return None

    def _refetch_annot(self, i: int, annot):
        pg = self.doc[i]; self._held_page = pg
        return pg, next((a for a in pg.annots() if a.xref == annot.xref), None)

    def delete_annot(self, i: int, annot):
        self.snapshot(); pg, a = self._refetch_annot(i, annot)
        if a is not None:
            pg.delete_annot(a)

    def set_annot_text(self, i: int, annot, text):
        self.snapshot(); pg, a = self._refetch_annot(i, annot)
        if a is not None:
            a.set_info(content=text); a.update()

    # ---- forms
    def widget_at(self, i: int, pt):
        # keep the Page object alive: widgets hold only a weak reference to their page
        self._held_page = self.doc[i]
        for w in self._held_page.widgets():
            if w.rect.contains(pt):
                return w
        return None

    def set_widget(self, i: int, widget, value):
        xref = widget.xref
        self.snapshot()
        pg = self.doc[i]                      # fresh page from the (possibly re-opened) document
        w = next((x for x in pg.widgets() if x.xref == xref), None)
        if w is None:
            raise ValueError("Form field not found.")
        if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
            w.field_value = w.on_state() if value else False
        else:
            w.field_value = value
        w.update()
        if w.field_type not in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON) and value in ("", None):
            _clear_field(pg, w)               # PyMuPDF skips empty values, so the old text would stay
        self._held_page = pg

    # ---- document
    def metadata(self):
        return dict(self.doc.metadata or {})

    def set_metadata(self, md: dict):
        self.snapshot(); self.doc.set_metadata(md)

    def add_link(self, i: int, rect, uri):
        self.snapshot(); self.doc[i].insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(rect), "uri": uri})


# ====================================================================== GUI

TOOLS = [
    ("select", "Select / Fill"), ("pan", "Pan"), None,
    ("highlight", "Highlight"), ("underline", "Underline"), ("strikeout", "Strike"), None,
    ("pen", "Pen"), ("rect", "Box"), ("ellipse", "Ellipse"), ("line", "Line"), ("arrow", "Arrow"), None,
    ("text", "Text box"), ("note", "Note"), ("image", "Image"), ("signature", "Sign"), None,
    ("whiteout", "Fix word"), ("block", "Edit paragraph"), ("redact", "Redact"), ("link", "Link"), ("crop", "Crop"), None,
    ("dsign", "Digital sign"),
]

TIPS = {
    "select": "Click a form field to fill it. Right-click an annotation to edit or delete it.",
    "pan": "Drag to scroll.",
    "highlight": "Drag over text to highlight it.", "underline": "Drag over text to underline it.",
    "strikeout": "Drag over text to strike it through.",
    "pen": "Draw freehand.", "rect": "Drag a box.", "ellipse": "Drag an ellipse.",
    "line": "Drag a line.", "arrow": "Drag an arrow (points to where you release).",
    "text": "Click where the text should start.", "note": "Click to place a sticky note.",
    "image": "Drag the area for the picture, then choose the file.",
    "signature": "Drag the area for your signature.",
    "whiteout": "Click a word or line to replace it (removes the original text, keeps the baseline).",
    "block": "Click a paragraph to rewrite it inside the same box.",
    "redact": "Drag to permanently remove content (black box, text actually deleted).",
    "link": "Drag the clickable area, then enter a URL.",
    "crop": "Drag the area to keep.",
    "dsign": "Drag where the signature block should appear, then choose your certificate. Do this LAST - later edits invalidate the signature.",
}


class Editor(tk.Tk):
    def __init__(self, path: str | None):
        super().__init__()
        self.title("PDF Editor")
        self.geometry("1200x800")
        self.minsize(900, 600)
        self.zoom = 1.25
        self.page_no = 0
        self.tool = tk.StringVar(value="select")
        self.color = "#ff0000"
        self.width = tk.IntVar(value=2)
        self.font_size = tk.IntVar(value=11)
        self.font_name = tk.StringVar(value="Helvetica")
        self.drag = None
        self.ink_points = []
        self.photo = None
        self.thumbs = []
        self.search_hits = []
        self.search_idx = -1
        self.model: PdfModel | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._bind_keys()
        # index the PC's fonts once, in the background, so text edits can match the original font
        threading.Thread(target=_system_font_index, daemon=True).start()
        if path:
            self.open_file(path)
        else:
            self.after(100, self.open_dialog)

    # ---- layout
    def _build(self):
        if IS_WIN:
            try:
                ttk.Style(self).theme_use("vista")
            except tk.TclError:
                pass
        self._menu()

        tb = ttk.Frame(self); tb.pack(fill="x", padx=4, pady=2)
        for item in TOOLS:
            if item is None:
                ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=4, pady=2)
                continue
            key, label = item
            ttk.Radiobutton(tb, text=label, value=key, variable=self.tool, style="Toolbutton",
                            command=self._tool_changed).pack(side="left")
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=4)
        self.color_btn = tk.Button(tb, text="Colour", bg=self.color, fg="white", width=7, command=self.pick_color)
        self.color_btn.pack(side="left", padx=2)
        ttk.Label(tb, text="Width").pack(side="left", padx=(6, 0))
        ttk.Spinbox(tb, from_=1, to=20, textvariable=self.width, width=3).pack(side="left")
        ttk.Label(tb, text="Font").pack(side="left", padx=(6, 0))
        ttk.Combobox(tb, textvariable=self.font_name, values=list(FONTS), width=14, state="readonly").pack(side="left")
        ttk.Spinbox(tb, from_=4, to=96, textvariable=self.font_size, width=3).pack(side="left")

        body = ttk.Panedwindow(self, orient="horizontal"); body.pack(fill="both", expand=True)

        # thumbnails
        left = ttk.Frame(body, width=170)
        self.thumb_canvas = tk.Canvas(left, width=160, bg="#e8e8e8", highlightthickness=0)
        tsb = ttk.Scrollbar(left, command=self.thumb_canvas.yview)
        self.thumb_canvas.configure(yscrollcommand=tsb.set)
        self.thumb_canvas.pack(side="left", fill="both", expand=True); tsb.pack(side="right", fill="y")
        self.thumb_canvas.bind("<Button-1>", self.thumb_click)
        self.thumb_canvas.bind("<Button-3>", self.thumb_menu)
        self.thumb_canvas.bind("<MouseWheel>", lambda e: self.thumb_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        body.add(left, weight=0)

        # page canvas
        mid = ttk.Frame(body)
        self.canvas = tk.Canvas(mid, bg="#707070", highlightthickness=0)
        vsb = ttk.Scrollbar(mid, command=self.canvas.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew"); vsb.grid(row=0, column=1, sticky="ns"); hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1); mid.columnconfigure(0, weight=1)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Control-MouseWheel>", lambda e: self.set_zoom(self.zoom * (1.1 if e.delta > 0 else 0.9)))
        body.add(mid, weight=1)

        # bottom bar
        bb = ttk.Frame(self); bb.pack(fill="x", padx=4, pady=2)
        ttk.Button(bb, text="◄", width=3, command=lambda: self.goto(self.page_no - 1)).pack(side="left")
        self.page_entry = ttk.Entry(bb, width=4); self.page_entry.pack(side="left", padx=2)
        self.page_entry.bind("<Return>", lambda e: self.goto(int(self.page_entry.get() or 1) - 1))
        self.page_total = ttk.Label(bb, text="/ 0"); self.page_total.pack(side="left")
        ttk.Button(bb, text="►", width=3, command=lambda: self.goto(self.page_no + 1)).pack(side="left", padx=(2, 10))
        ttk.Button(bb, text="−", width=3, command=lambda: self.set_zoom(self.zoom * 0.8)).pack(side="left")
        ttk.Button(bb, text="+", width=3, command=lambda: self.set_zoom(self.zoom * 1.25)).pack(side="left")
        ttk.Button(bb, text="Fit width", command=self.fit_width).pack(side="left", padx=2)
        ttk.Label(bb, text="Find:").pack(side="left", padx=(12, 2))
        self.search_var = tk.StringVar()
        se = ttk.Entry(bb, textvariable=self.search_var, width=22); se.pack(side="left")
        se.bind("<Return>", lambda e: self.find_next())
        self.search_entry = se
        ttk.Button(bb, text="Next", command=self.find_next).pack(side="left", padx=2)
        self.status = ttk.Label(bb, text="", anchor="e"); self.status.pack(side="right", fill="x", expand=True)

    def _menu(self):
        m = tk.Menu(self); self.config(menu=m)
        f = tk.Menu(m, tearoff=0); m.add_cascade(label="File", menu=f)
        f.add_command(label="Open…", accelerator="Ctrl+O", command=self.open_dialog)
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        f.add_command(label="Save As…", accelerator="Ctrl+Shift+S", command=self.save_as)
        f.add_command(label="Save with password…", command=self.save_password)
        f.add_separator()
        f.add_command(label="Export current page as PNG…", command=self.export_png)
        f.add_command(label="Extract all text to .txt…", command=self.extract_text)
        f.add_command(label="Edit text in Word (round-trip via LibreOffice)…", command=self.edit_in_word)
        f.add_separator()
        f.add_command(label="Document properties…", command=self.edit_metadata)
        f.add_separator()
        f.add_command(label="Exit", command=self.on_close)
        e = tk.Menu(m, tearoff=0); m.add_cascade(label="Edit", menu=e)
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        p = tk.Menu(m, tearoff=0); m.add_cascade(label="Page", menu=p)
        p.add_command(label="Rotate left", command=lambda: self.page_op("rotate", -90))
        p.add_command(label="Rotate right", command=lambda: self.page_op("rotate", 90))
        p.add_command(label="Move up", command=lambda: self.page_op("move", -1))
        p.add_command(label="Move down", command=lambda: self.page_op("move", 1))
        p.add_command(label="Duplicate", command=lambda: self.page_op("dup"))
        p.add_command(label="Delete", command=lambda: self.page_op("delete"))
        p.add_separator()
        p.add_command(label="Insert blank page after", command=lambda: self.page_op("blank"))
        p.add_command(label="Insert PDF after…", command=lambda: self.page_op("insert"))
        p.add_command(label="Extract this page to a new PDF…", command=lambda: self.page_op("extract"))
        p.add_command(label="Clear signature stored for 'Sign' tool", command=self.clear_signature)
        s = tk.Menu(m, tearoff=0); m.add_cascade(label="Sign", menu=s)
        s.add_command(label="Sign digitally (visible block)…", command=lambda: (self.tool.set("dsign"), self._tool_changed()))
        s.add_command(label="Sign digitally (invisible)…", command=lambda: self.digital_sign(None))
        s.add_command(label="Verify signatures…", command=self.verify_signatures)
        s.add_separator()
        s.add_command(label="Create my certificate…", command=self.create_certificate)
        s.add_command(label="Export my certificate (.cer) to share…", command=self.export_certificate)
        s.add_command(label="Open trusted-certificates folder", command=self.open_trusted_folder)
        s.add_separator()
        s.add_command(label="About digital signatures", command=self.help_sign)
        h = tk.Menu(m, tearoff=0); m.add_cascade(label="Help", menu=h)
        h.add_command(label="How text editing works", command=self.help_text)
        h.add_command(label="About digital signatures", command=self.help_sign)

    def _bind_keys(self):
        self.bind("<Control-o>", lambda e: self.open_dialog())
        self.bind("<Control-s>", lambda e: self.save())
        self.bind("<Control-S>", lambda e: self.save_as())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.bind("<Prior>", lambda e: self.goto(self.page_no - 1))
        self.bind("<Next>", lambda e: self.goto(self.page_no + 1))
        self.bind("<Escape>", lambda e: (self.tool.set("select"), self._tool_changed()))

    # ---- file ops
    def open_dialog(self):
        p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if p:
            self.open_file(p)

    def open_file(self, path):
        if self.model and self.model.dirty and not self._confirm_discard():
            return
        try:
            model = PdfModel(path)
        except Exception as ex:
            messagebox.showerror("PDF Editor", f"Could not open:\n{ex}"); return
        if model.doc.needs_pass:
            pw = simpledialog.askstring("Password", "This PDF is protected. Password:", show="•")
            if pw is None or not model.doc.authenticate(pw):
                messagebox.showerror("PDF Editor", "Wrong or missing password."); return
        self.model = model
        self.search_hits = []
        self.signed_warned = False
        self.after(300, self._check_signed)
        self.page_no = 0
        self.title(f"PDF Editor - {Path(path).name}")
        self.after(50, self.fit_width)
        self.refresh_thumbs()
        self.set_status(f"Opened {Path(path).name} - {len(self.model)} page(s)")

    def _confirm_discard(self):
        r = messagebox.askyesnocancel("Unsaved changes", "Save changes first?")
        if r is None:
            return False
        if r:
            return self.save()
        return True

    def save(self) -> bool:
        if not self.model:
            return False
        if not self.model.path:
            return self.save_as()
        if not messagebox.askyesno("Overwrite?", f"Overwrite the original file?\n\n{self.model.path}\n\n"
                                   "Choose No to save a copy instead."):
            return self.save_as()
        return self._do_save(self.model.path)

    def save_as(self) -> bool:
        if not self.model:
            return False
        init = self.model.path.with_name(self.model.path.stem + "_edited.pdf").name if self.model.path else "document.pdf"
        p = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=init, filetypes=[("PDF", "*.pdf")])
        return bool(p) and self._do_save(p)

    def save_password(self):
        if not self.model:
            return
        pw = simpledialog.askstring("Password", "Password to open the PDF:", show="•")
        if not pw:
            return
        p = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile="protected.pdf", filetypes=[("PDF", "*.pdf")])
        if p:
            self._do_save(p, pw)

    def _do_save(self, path, pw="") -> bool:
        try:
            self.model.save(path, user_pw=pw)
            self.title(f"PDF Editor - {Path(path).name}")
            self.set_status(f"Saved {path}")
            return True
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex)); return False

    def export_png(self):
        if not self.model:
            return
        p = filedialog.asksaveasfilename(defaultextension=".png", initialfile=f"page{self.page_no+1}.png")
        if p:
            self.model.export_png(self.page_no, p); self.set_status(f"Exported {p}")

    def extract_text(self):
        if not self.model:
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile=(self.model.path.stem if self.model.path else "text") + ".txt")
        if p:
            Path(p).write_text("\n\n".join(self.model.page_text(i) for i in range(len(self.model))), encoding="utf-8")
            self.set_status(f"Text saved to {p}")

    def edit_in_word(self):
        if not self.model or not self.model.path:
            return messagebox.showinfo("PDF Editor", "Save the PDF first.")
        soffice = _find_soffice()
        if not soffice:
            return messagebox.showerror("PDF Editor", "LibreOffice is needed for this (winget install TheDocumentFoundation.LibreOffice).")
        if self.model.dirty and not messagebox.askyesno("PDF Editor", "Unsaved edits won't be included. Continue with the saved file?"):
            return
        self.set_status("Converting to Word…"); self.update()
        out_dir = self.model.path.parent
        r = subprocess.run([soffice, "--headless", "--norestore", "--infilter=writer_pdf_import",
                            "--convert-to", "docx:MS Word 2007 XML", "--outdir", str(out_dir), str(self.model.path)],
                           capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        docx = out_dir / (self.model.path.stem + ".docx")
        if r.returncode != 0 or not docx.exists():
            return messagebox.showerror("PDF Editor", "Conversion failed:\n" + (r.stderr or r.stdout)[-400:])
        _open_file(docx)
        self.set_status(f"Opened {docx.name} - edit it, then use Toolkit 'Office → PDF' (or Save as PDF in Word) to get a PDF back.")

    def edit_metadata(self):
        if not self.model:
            return
        md = self.model.metadata()
        dlg = tk.Toplevel(self); dlg.title("Document properties"); dlg.transient(self); dlg.grab_set()
        vars_ = {}
        for i, k in enumerate(("title", "author", "subject", "keywords")):
            ttk.Label(dlg, text=k.title()).grid(row=i, column=0, sticky="e", padx=6, pady=3)
            v = tk.StringVar(value=md.get(k) or ""); vars_[k] = v
            ttk.Entry(dlg, textvariable=v, width=50).grid(row=i, column=1, padx=6, pady=3)

        def ok():
            md.update({k: v.get() for k, v in vars_.items()})
            self.model.set_metadata(md); dlg.destroy(); self.set_status("Properties updated")
        ttk.Button(dlg, text="OK", command=ok).grid(row=4, column=1, sticky="e", padx=6, pady=6)

    def help_text(self):
        messagebox.showinfo("Editing text in a PDF",
            "PDFs store glyphs at fixed positions, not editable paragraphs, so there are three ways to change text:\n\n"
            "• Fix word - click a word or line; the original is deleted and your replacement is written on the same "
            "baseline. Best for dates, names, prices.\n\n"
            "• Edit paragraph - click a paragraph; it is removed and your new text is re-laid inside the same box "
            "(font is reset to Helvetica/Times/Courier and shrinks if needed).\n\n"
            "• Edit text in Word (File menu) - converts the PDF to Word via LibreOffice for full reflow editing, "
            "then convert back with Toolkit. Layout can drift on complex pages.\n\n"
            "Everything can be undone with Ctrl+Z until you save.")

    # ---- rendering
    def render(self):
        if not self.model:
            return
        pix = self.model.render(self.page_no, self.zoom)
        self.photo = tk.PhotoImage(data=pix.tobytes("ppm"))
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo, tags="page")
        self.canvas.configure(scrollregion=(0, 0, pix.width, pix.height))
        # form field hints
        for w in self.model.page(self.page_no).widgets():
            x0, y0, x1, y1 = self.model.rect_to_canvas(self.page_no, w.rect, self.zoom)
            self.canvas.create_rectangle(x0, y0, x1, y1, outline="#3b82f6", dash=(2, 2), tags="hint")
        for (p, r) in self.search_hits:
            if p == self.page_no:
                x0, y0, x1, y1 = self.model.rect_to_canvas(p, r, self.zoom)
                self.canvas.create_rectangle(x0, y0, x1, y1, outline="#f59e0b", width=2, tags="hit")
        self.page_entry.delete(0, "end"); self.page_entry.insert(0, str(self.page_no + 1))
        self.page_total.configure(text=f"/ {len(self.model)}")
        self.highlight_thumb()

    def refresh_thumbs(self):
        if not self.model:
            return
        self.thumb_canvas.delete("all"); self.thumbs = []
        y = 8
        for i in range(len(self.model)):
            pg = self.model.page(i)
            z = 120 / max(pg.rect.width, 1)
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            ph = tk.PhotoImage(data=pix.tobytes("ppm")); self.thumbs.append(ph)
            self.thumb_canvas.create_rectangle(18, y - 2, 22 + pix.width, y + pix.height + 2, outline="#999", tags=f"frame{i}")
            self.thumb_canvas.create_image(20, y, anchor="nw", image=ph, tags=("thumb", f"p{i}"))
            self.thumb_canvas.create_text(80, y + pix.height + 10, text=str(i + 1), font=("Segoe UI", 8))
            y += pix.height + 26
        self.thumb_canvas.configure(scrollregion=(0, 0, 160, y))
        self.render()

    def highlight_thumb(self):
        for i in range(len(self.thumbs)):
            self.thumb_canvas.itemconfigure(f"frame{i}", outline="#2563eb" if i == self.page_no else "#999",
                                            width=3 if i == self.page_no else 1)

    def _thumb_index(self, event):
        y = self.thumb_canvas.canvasy(event.y)
        for item in self.thumb_canvas.find_overlapping(20, y, 140, y):
            for t in self.thumb_canvas.gettags(item):
                if t.startswith("p") and t[1:].isdigit():
                    return int(t[1:])
        return None

    def thumb_click(self, e):
        i = self._thumb_index(e)
        if i is not None:
            self.goto(i)

    def thumb_menu(self, e):
        i = self._thumb_index(e)
        if i is None:
            return
        self.goto(i)
        m = tk.Menu(self, tearoff=0)
        for label, op, arg in (("Rotate left", "rotate", -90), ("Rotate right", "rotate", 90), ("Move up", "move", -1),
                               ("Move down", "move", 1), ("Duplicate", "dup", None), ("Delete", "delete", None),
                               ("Insert blank after", "blank", None), ("Insert PDF after…", "insert", None),
                               ("Extract to new PDF…", "extract", None)):
            m.add_command(label=label, command=lambda op=op, arg=arg: self.page_op(op, arg))
        m.tk_popup(e.x_root, e.y_root)

    def goto(self, i):
        if self.model and 0 <= i < len(self.model):
            self.page_no = i; self.render(); self.canvas.yview_moveto(0)

    def set_zoom(self, z):
        self.zoom = max(0.2, min(6, z)); self.render()

    def fit_width(self):
        if self.model:
            avail = max(self.canvas.winfo_width() - 20, 200)
            self.zoom = avail / self.model.page(self.page_no).rect.width; self.render()

    def set_status(self, s):
        self.status.configure(text=s)

    def _tool_changed(self):
        self.set_status(TIPS.get(self.tool.get(), ""))
        self.canvas.configure(cursor={"pan": "fleur", "pen": "pencil", "select": "arrow"}.get(self.tool.get(), "crosshair"))

    def pick_color(self):
        c = colorchooser.askcolor(self.color, title="Colour")[1]
        if c:
            self.color = c; self.color_btn.configure(bg=c)

    def on_wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ---- page ops
    def page_op(self, op, arg=None):
        if not self.model:
            return
        i = self.page_no
        try:
            if op == "rotate":
                self.model.rotate(i, arg)
            elif op == "move":
                self.model.move_page(i, i + arg); self.page_no = max(0, min(len(self.model) - 1, i + arg))
            elif op == "dup":
                self.model.duplicate_page(i)
            elif op == "delete":
                if messagebox.askyesno("Delete page", f"Delete page {i+1}?"):
                    self.model.delete_page(i); self.page_no = min(i, len(self.model) - 1)
            elif op == "blank":
                self.model.insert_blank(i)
            elif op == "insert":
                p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
                if p:
                    self.model.insert_pdf(i, p)
            elif op == "extract":
                p = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=f"page{i+1}.pdf")
                if p:
                    self.model.extract_pages([i], p); self.set_status(f"Saved {p}")
        except Exception as ex:
            messagebox.showerror("PDF Editor", str(ex))
        self.refresh_thumbs()

    def undo(self):
        if self.model and self.model.undo():
            self.page_no = min(self.page_no, len(self.model) - 1); self.refresh_thumbs(); self.set_status("Undone")

    def redo(self):
        if self.model and self.model.redo():
            self.page_no = min(self.page_no, len(self.model) - 1); self.refresh_thumbs(); self.set_status("Redone")

    # ---- search
    def find_next(self):
        if not self.model:
            return
        q = self.search_var.get().strip()
        if not q:
            return
        if not self.search_hits or getattr(self, "_last_q", None) != q:
            self.search_hits = self.model.search(q); self._last_q = q; self.search_idx = -1
            if not self.search_hits:
                self.set_status(f"'{q}' not found"); self.render(); return
        self.search_idx = (self.search_idx + 1) % len(self.search_hits)
        p, r = self.search_hits[self.search_idx]
        self.page_no = p; self.render()
        x0, y0, *_ = self.model.rect_to_canvas(p, r, self.zoom)
        h = max(self.canvas.bbox("page")[3], 1)
        self.canvas.yview_moveto(max(0, (y0 - 100) / h))
        self.set_status(f"Match {self.search_idx+1} of {len(self.search_hits)}")

    # ---- mouse
    def _cxy(self, e):
        return self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)

    def on_press(self, e):
        if not self.model:
            return
        x, y = self._cxy(e); t = self.tool.get()
        self.drag = (x, y)
        if t == "pan":
            self.canvas.scan_mark(e.x, e.y); return
        if t == "pen":
            self.ink_points = [(x, y)]; return
        pt = self.model.to_page(self.page_no, x, y, self.zoom)
        if t == "select":
            w = self.model.widget_at(self.page_no, pt)
            if w:
                self.fill_widget(w)
        elif t == "text":
            self.place_text(pt)
        elif t == "note":
            txt = _ask_multiline(self, "Sticky note", "")
            if txt:
                self.model.add_note(self.page_no, pt, txt, hex_to_rgb("#f5c400")); self.render()
        elif t == "whiteout":
            self.fix_word(pt)
        elif t == "block":
            self.edit_block(pt)

    def on_drag(self, e):
        if not self.model or not self.drag:
            return
        x, y = self._cxy(e); t = self.tool.get()
        if t == "pan":
            self.canvas.scan_dragto(e.x, e.y, gain=1); return
        self.canvas.delete("temp")
        x0, y0 = self.drag
        if t == "pen":
            self.ink_points.append((x, y))
            self.canvas.create_line(*sum(self.ink_points, ()), fill=self.color, width=self.width.get(), tags="temp",
                                    capstyle="round", smooth=True)
        elif t in ("line", "arrow"):
            self.canvas.create_line(x0, y0, x, y, fill=self.color, width=self.width.get(), tags="temp",
                                    arrow="last" if t == "arrow" else "none")
        elif t == "ellipse":
            self.canvas.create_oval(x0, y0, x, y, outline=self.color, width=self.width.get(), tags="temp")
        elif t in ("highlight", "underline", "strikeout", "rect", "image", "signature", "redact", "link", "crop", "dsign"):
            col = {"redact": "#000", "crop": "#0a0", "link": "#36f", "dsign": "#7c3aed"}.get(t, self.color)
            kw = {"dash": (4, 2)} if t in ("crop", "link", "image", "signature", "dsign") else {}
            self.canvas.create_rectangle(x0, y0, x, y, outline=col, width=2, tags="temp", **kw)

    def on_release(self, e):
        if not self.model or not self.drag:
            return
        x, y = self._cxy(e); x0, y0 = self.drag; t = self.tool.get()
        self.drag = None; self.canvas.delete("temp")
        if t in ("pan", "select", "text", "note", "whiteout", "block"):
            return
        i, z = self.page_no, self.zoom
        col = hex_to_rgb(self.color); wd = self.width.get()
        tiny = abs(x - x0) < 3 and abs(y - y0) < 3
        try:
            if t == "pen":
                if len(self.ink_points) > 1:
                    pts = [self.model.to_page(i, px, py, z) for px, py in self.ink_points]
                    self.model.add_ink(i, [pts], col, wd)
            elif tiny:
                return
            elif t in ("highlight", "underline", "strikeout"):
                self.model.add_markup(i, t, self.model.rect_to_page(i, x0, y0, x, y, z), col)
            elif t in ("rect", "ellipse"):
                self.model.add_rect(i, self.model.rect_to_page(i, x0, y0, x, y, z), col, wd, ellipse=(t == "ellipse"))
            elif t in ("line", "arrow"):
                self.model.add_line(i, self.model.to_page(i, x0, y0, z), self.model.to_page(i, x, y, z), col, wd, arrow=(t == "arrow"))
            elif t == "image":
                p = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All", "*.*")])
                if p:
                    self.model.add_image(i, self.model.rect_to_page(i, x0, y0, x, y, z), p)
            elif t == "signature":
                self.place_signature(self.model.rect_to_page(i, x0, y0, x, y, z))
            elif t == "redact":
                if messagebox.askyesno("Redact", "Permanently remove everything in this area?\n(Undo is available until you save.)"):
                    self.model.redact(i, self.model.rect_to_page(i, x0, y0, x, y, z))
            elif t == "link":
                uri = simpledialog.askstring("Link", "URL:", initialvalue="https://")
                if uri and uri != "https://":
                    self.model.add_link(i, self.model.rect_to_page(i, x0, y0, x, y, z), uri)
            elif t == "crop":
                if messagebox.askyesno("Crop", "Crop this page to the selected area?"):
                    self.model.crop(i, self.model.rect_to_page(i, x0, y0, x, y, z))
                    self.refresh_thumbs(); return
            elif t == "dsign":
                self.digital_sign(self.model.rect_to_page(i, x0, y0, x, y, z)); return
        except Exception as ex:
            messagebox.showerror("PDF Editor", str(ex))
        self.render()

    def on_right(self, e):
        if not self.model:
            return
        x, y = self._cxy(e); pt = self.model.to_page(self.page_no, x, y, self.zoom)
        a = self.model.annot_at(self.page_no, pt)
        m = tk.Menu(self, tearoff=0)
        if a:
            m.add_command(label=f"Delete {a.type[1]}", command=lambda: (self.model.delete_annot(self.page_no, a), self.render()))
            if a.type[0] in (fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FREE_TEXT):
                m.add_command(label="Edit text…", command=lambda: self.edit_annot_text(a))
        else:
            m.add_command(label="Paste text here…", command=lambda: self.place_text(pt))
            m.add_command(label="Fix word here…", command=lambda: self.fix_word(pt))
        m.add_separator()
        m.add_command(label="Undo", command=self.undo)
        m.tk_popup(e.x_root, e.y_root)

    # ---- tool actions
    def fill_widget(self, w):
        i = self.page_no
        if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
            cur = w.field_value not in (False, None, "Off", "")
            self.model.set_widget(i, w, not cur)
        elif w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
            v = _ask_choice(self, w.field_name or "Choose", w.choice_values or [], w.field_value)
            if v is not None:
                self.model.set_widget(i, w, v)
        elif w.field_type == fitz.PDF_WIDGET_TYPE_TEXT:
            v = _ask_multiline(self, w.field_name or "Text field", w.field_value or "")
            if v is not None:
                self.model.set_widget(i, w, v)
        else:
            return
        self.render()

    def place_text(self, pt):
        txt = _ask_multiline(self, "Text", "")
        if txt:
            self.model.add_textbox(self.page_no, pt, txt, self.font_size.get(), hex_to_rgb(self.color),
                                   FONTS[self.font_name.get()])
            self.render()

    def fix_word(self, pt):
        span = self.model.span_at(self.page_no, pt)
        if not span:
            return self.set_status("No text under the cursor - use 'Text box' to add new text.")
        color = fitz.sRGB_to_pdf(span.get("color", 0))
        res = TextEditDialog(self, self.model, self.page_no, "Replace text", _normalise(span["text"]),
                             span.get("font", ""), span.get("flags", 0), span["size"], single=True).result
        if not res:
            return
        self.model.whiteout_retype(self.page_no, span, res["text"], res["size"], color,
                                   fontname=res["fontname"], fontbuffer=res["fontbuffer"])
        self.render(); self.set_status(f"Replaced using {res['font_desc']}. Ctrl+Z to undo.")

    def edit_block(self, pt):
        blk = self.model.block_at(self.page_no, pt)
        if not blk:
            return self.set_status("No paragraph under the cursor.")
        res = TextEditDialog(self, self.model, self.page_no, "Rewrite paragraph", blk["text"],
                             blk.get("font", ""), blk.get("flags", 0), blk["size"], align=blk["align"]).result
        if not res:
            return
        color = fitz.sRGB_to_pdf(blk["color"])
        try:
            used = self.model.replace_block(self.page_no, blk, res["text"], res["size"], color,
                                            fontname=res["fontname"], fontbuffer=res["fontbuffer"], align=res["align"])
        except Exception as e:
            self.render(); return messagebox.showerror("Edit paragraph", str(e))
        note = "" if abs(used - res["size"]) < 0.01 else f" (shrunk to {used}pt to fit)"
        self.render(); self.set_status(f"Paragraph replaced using {res['font_desc']}{note}. Ctrl+Z to undo.")

    def edit_annot_text(self, a):
        new = _ask_multiline(self, "Edit text", a.info.get("content", ""))
        if new is not None:
            self.model.set_annot_text(self.page_no, a, new); self.render()

    def place_signature(self, rect):
        sig = _load_signature()
        if sig:
            label = f"typed '{sig['name']}'" if sig["type"] == "typed" else "hand-drawn"
            choice = messagebox.askyesnocancel("Signature", f"Use your saved {label} signature?\n\nYes = use it\nNo = make a new one")
            if choice is None:
                return
            if not choice:
                sig = None
        if not sig:
            sig = SignatureDialog(self).result
            if not sig:
                return
            _save_signature(sig)
        colour = hex_to_rgb(sig.get("colour", "#1a237e"))
        if sig["type"] == "typed":
            self.model.add_typed_signature(self.page_no, rect, sig["name"], sig.get("font"), colour)
            return
        strokes = sig["strokes"]
        # fit strokes (in 0..1 units) into rect
        xs = [p[0] for s in strokes for p in s]; ys = [p[1] for s in strokes for p in s]
        sw, sh = max(xs) - min(xs) or 1, max(ys) - min(ys) or 1
        scale = min(rect.width / sw, rect.height / sh)
        ox = rect.x0 + (rect.width - sw * scale) / 2; oy = rect.y0 + (rect.height - sh * scale) / 2
        pts = [[fitz.Point(ox + (x - min(xs)) * scale, oy + (y - min(ys)) * scale) for x, y in s] for s in strokes]
        self.model.add_ink(self.page_no, pts, colour, max(1.5, rect.height / 40))

    def clear_signature(self):
        if SIGNATURE_FILE.exists():
            SIGNATURE_FILE.unlink(); self.set_status("Saved signature removed.")

    def on_close(self):
        if self.model and self.model.dirty and not self._confirm_discard():
            return
        self.destroy()

    # ---- digital signatures
    def _signing(self):
        try:
            import signing
            return signing
        except ImportError:
            messagebox.showerror("PDF Editor", "signing.py is missing from the Toolkit folder."); return None

    def _signature_count(self):
        if not self.model:
            return 0
        return sum(1 for i in range(len(self.model)) for w in self.model.page(i).widgets()
                   if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and w.field_value)

    def _check_signed(self):
        n = self._signature_count()
        if n and not self.signed_warned:
            self.signed_warned = True
            self.set_status(f"This document carries {n} digital signature(s). Any edit will invalidate them - use Sign > Verify to check them.")

    def _warn_if_signed(self) -> bool:
        if self._signature_count() and not messagebox.askyesno(
                "Already signed", "This document is already digitally signed. Signing again adds a second signature "
                "(fine), but any other edits you made will have broken the earlier one.\n\nContinue?"):
            return False
        return True

    def create_certificate(self):
        signing = self._signing()
        if not signing:
            return
        prof = signing.load_profile()
        dlg = tk.Toplevel(self); dlg.title("Create my signing certificate"); dlg.transient(self); dlg.grab_set()
        fields_ = [("Full name *", "name", prof.get("name", "")), ("Email", "email", prof.get("email", "")),
                   ("Organisation", "org", prof.get("org", "")), ("Country code (e.g. GB)", "country", prof.get("country", "GB")),
                   ("Password *", "pw", ""), ("Repeat password *", "pw2", "")]
        vars_ = {}
        for r, (label, key, init) in enumerate(fields_):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="e", padx=8, pady=3)
            v = tk.StringVar(value=init); vars_[key] = v
            ttk.Entry(dlg, textvariable=v, width=40, show="•" if key.startswith("pw") else "").grid(row=r, column=1, padx=8, pady=3)
        ttk.Label(dlg, text=f"Saved to {signing.DEFAULT_PFX}\nA shareable .cer is written beside it. Valid 5 years.",
                  foreground="#666", justify="left").grid(row=len(fields_), column=0, columnspan=2, padx=8, pady=(4, 2), sticky="w")

        def ok():
            v = {k: s.get() for k, s in vars_.items()}
            if v["pw"] != v["pw2"]:
                return messagebox.showwarning("Certificate", "Passwords don't match.")
            if signing.DEFAULT_PFX.exists() and not messagebox.askyesno(
                    "Replace certificate?", "You already have a certificate. Replace it?\n"
                    "(Documents signed with the old one stay valid, but recipients will need your new .cer.)"):
                return
            try:
                pfx, cer = signing.create_certificate(v["name"], v["email"], v["org"], v["country"], v["pw"])
            except Exception as e:
                return messagebox.showerror("Certificate", str(e))
            signing.save_profile(name=v["name"], email=v["email"], org=v["org"], country=v["country"])
            dlg.destroy()
            messagebox.showinfo("Certificate created",
                                f"Certificate: {pfx}\nShareable copy: {cer}\n\nSend the .cer to people who receive your "
                                "signed PDFs so their viewer can show the signature as trusted.")
        bar = ttk.Frame(dlg); bar.grid(row=len(fields_) + 1, column=0, columnspan=2, sticky="e", padx=8, pady=8)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(bar, text="Create", command=ok).pack(side="right", padx=4)

    def export_certificate(self):
        signing = self._signing()
        if not signing:
            return
        if not signing.DEFAULT_CER.exists():
            return messagebox.showinfo("Certificate", "No certificate yet - use Sign > Create my certificate.")
        p = filedialog.asksaveasfilename(defaultextension=".cer", initialfile="my_certificate.cer",
                                         filetypes=[("Certificate", "*.cer")])
        if p:
            shutil.copy(signing.DEFAULT_CER, p); self.set_status(f"Certificate exported to {p}")

    def open_trusted_folder(self):
        signing = self._signing()
        if signing:
            signing.TRUSTED_DIR.mkdir(parents=True, exist_ok=True); _open_file(signing.TRUSTED_DIR)

    def digital_sign(self, rect):
        """rect: unrotated page rect (PyMuPDF, origin top-left) for the visible block, or None for invisible."""
        signing = self._signing()
        if not self.model or not signing:
            return
        if not signing.DEFAULT_PFX.exists():
            if messagebox.askyesno("No certificate", "You don't have a signing certificate yet. Create one now?"):
                self.create_certificate()
            return
        if not self._warn_if_signed():
            return
        prof = signing.load_profile()
        dlg = tk.Toplevel(self); dlg.title("Sign digitally"); dlg.transient(self); dlg.grab_set()
        pfx_var = tk.StringVar(value=prof.get("pfx", str(signing.DEFAULT_PFX)))
        pw_var = tk.StringVar(); reason_var = tk.StringVar(value=prof.get("reason", "I approve this document"))
        loc_var = tk.StringVar(value=prof.get("location", "")); ts_var = tk.BooleanVar(value=True)
        img_var = tk.BooleanVar(value=bool(rect) and SIGNATURE_FILE.exists())
        cert_var = tk.StringVar(value="No - ordinary signature")
        rows = [("Certificate (.pfx)", pfx_var), ("Password", pw_var), ("Reason", reason_var), ("Location", loc_var)]
        for r, (label, var) in enumerate(rows):
            ttk.Label(dlg, text=label).grid(row=r, column=0, sticky="e", padx=8, pady=3)
            ttk.Entry(dlg, textvariable=var, width=44, show="•" if var is pw_var else "").grid(row=r, column=1, padx=8, pady=3, sticky="w")
        ttk.Button(dlg, text="…", width=3, command=lambda: pfx_var.set(filedialog.askopenfilename(
            filetypes=[("Certificate", "*.pfx *.p12")]) or pfx_var.get())).grid(row=0, column=2)
        r = len(rows)
        ttk.Checkbutton(dlg, text="Add a trusted timestamp (needs internet; proves signing time)", variable=ts_var).grid(row=r, column=1, sticky="w", padx=8)
        if rect:
            ttk.Checkbutton(dlg, text="Show my saved signature in the block", variable=img_var,
                            state="normal" if SIGNATURE_FILE.exists() else "disabled").grid(row=r + 1, column=1, sticky="w", padx=8)
        ttk.Label(dlg, text="Certify (lock)").grid(row=r + 2, column=0, sticky="e", padx=8, pady=3)
        ttk.Combobox(dlg, textvariable=cert_var, state="readonly", width=42,
                     values=["No - ordinary signature", "Allow form filling only", "Lock - no changes allowed"]).grid(row=r + 2, column=1, sticky="w", padx=8)
        ttk.Label(dlg, text="The signed copy is saved as a NEW file; the document you are editing is left as it is.",
                  foreground="#666").grid(row=r + 3, column=0, columnspan=3, padx=8, pady=(6, 0), sticky="w")

        def ok():
            pw = pw_var.get()
            try:
                info = signing.certificate_info(Path(pfx_var.get()), pw)
            except Exception as e:
                return messagebox.showerror("Sign", str(e))
            if info["expired"] and not messagebox.askyesno("Sign", "This certificate has expired. Sign anyway?"):
                return
            default = (self.model.path.with_name(self.model.path.stem + "_signed.pdf").name if self.model.path else "signed.pdf")
            out = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=default, filetypes=[("PDF", "*.pdf")])
            if not out:
                return
            dlg.destroy()
            opts = signing.SignOptions(pfx_path=Path(pfx_var.get()), password=pw, reason=reason_var.get().strip(),
                                       location=loc_var.get().strip(), timestamp=ts_var.get(),
                                       certify={"Allow form filling only": "forms", "Lock - no changes allowed": "lock"}.get(cert_var.get(), "no"))
            if rect:
                pg = self.model.page(self.page_no)
                # pyHanko wants PDF user space (origin bottom-left); PyMuPDF's inverse transformation matrix does that
                r = fitz.Rect(rect) * ~pg.transformation_matrix
                opts.page = self.page_no
                opts.box = (min(r.x0, r.x1), min(r.y0, r.y1), max(r.x0, r.x1), max(r.y0, r.y1))
                if img_var.get():
                    opts.image = self._signature_png()
            self.set_status("Signing…"); self.update()
            try:
                rep = signing.sign_pdf(self.model.doc.tobytes(garbage=3, deflate=True), out, opts)
            except Exception as e:
                return messagebox.showerror("Sign failed", str(e))
            signing.save_profile(reason=opts.reason, location=opts.location, pfx=str(opts.pfx_path))
            msg = f"Signed as {rep['signer']}\nField: {rep['field']}\nSaved: {rep['output']}"
            msg += f"\nTimestamped by {rep['tsa']}" if rep.get("timestamped") else ("\n" + rep.get("warning", ""))
            if messagebox.askyesno("Signed", msg + "\n\nOpen the signed file now?"):
                self.model.dirty = False
                self.open_file(out)
        bar = ttk.Frame(dlg); bar.grid(row=r + 4, column=0, columnspan=3, sticky="e", padx=8, pady=8)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right")
        ttk.Button(bar, text="Sign and save as…", command=ok).pack(side="right", padx=4)

    def _signature_png(self):
        """Render the saved signature (drawn or typed) to PNG bytes, for the digital-signature block."""
        try:
            sig = _load_signature()
            if not sig:
                return None
            return _render_signature_png(sig)
        except Exception:
            return None

    def verify_signatures(self):
        signing = self._signing()
        if not self.model or not signing:
            return
        try:
            data = self.model.path.read_bytes() if (self.model.path and not self.model.dirty) else self.model.doc.tobytes()
            text = signing.describe(signing.verify_pdf(data))
        except Exception as e:
            text = f"Could not verify: {e}"
        dlg = tk.Toplevel(self); dlg.title("Signatures"); dlg.transient(self)
        t = tk.Text(dlg, width=90, height=16, wrap="word"); t.pack(padx=10, pady=10); t.insert("1.0", text); t.configure(state="disabled")
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 10))

    def help_sign(self):
        messagebox.showinfo("Digital signatures",
            "A digital signature is a cryptographic seal, not a picture. Any viewer (Acrobat, Edge, Chrome) can check "
            "who signed and whether the file was changed afterwards.\n\n"
            "1. Sign > Create my certificate - once. Makes a self-signed certificate protected by a password.\n"
            "2. Finish all edits, then Sign > Sign digitally (or the 'Digital sign' tool to place a visible block).\n"
            "3. The signed PDF is saved as a new file. Don't edit it again - that breaks the seal.\n\n"
            "Recipients see 'validity unknown' until they trust your certificate once: send them the .cer file "
            "(Sign > Export my certificate). To see other people's signatures as trusted, drop their .cer files into "
            "the trusted-certificates folder.\n\n"
            "Timestamps come from free public servers and prove when you signed, even after the certificate expires.")


# ---------------------------------------------------------------- dialogs

# ---- signature storage: {"type": "ink", "strokes": [...]} or {"type": "typed", "name": ..., "font": path}
SCRIPT_FONT_HINTS = ["segoescript", "segoeprint", "inkfree", "gabriola", "lucidahandwriting", "brushscript",
                     "freestylescript", "frenchscript", "mistral", "vladimirscript", "edwardianscript", "kunstler",
                     "palacescript", "monotypecorsiva", "bradleyhand", "snellroundhand", "zapfino", "pristina",
                     "vivaldi", "rage", "harlow", "greatvibes", "dancingscript", "pacifico", "satisfy", "caveat"]


def _load_signature():
    try:
        data = json.loads(SIGNATURE_FILE.read_text())
    except Exception:
        return None
    if isinstance(data, list):                      # legacy format: bare stroke list
        return {"type": "ink", "strokes": data} if data else None
    if isinstance(data, dict) and data.get("type") in ("ink", "typed"):
        return data
    return None


def _save_signature(sig: dict):
    CONFIG_DIR.mkdir(exist_ok=True); SIGNATURE_FILE.write_text(json.dumps(sig))


def _script_fonts() -> list[tuple[str, str]]:
    """(label, path) for handwriting-style fonts installed on this PC."""
    idx = _system_font_index()
    found = {}
    for key, path in idx.items():
        if any(h in key for h in SCRIPT_FONT_HINTS) and not any(x in key for x in ("bold", "italic", "semibold")):
            found.setdefault(path, Path(path).stem)
    items = sorted(found.items(), key=lambda kv: kv[1].lower())
    return [(label, path) for path, label in items]


def _render_signature_png(sig: dict, W: int = 600, H: int = 240) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (W, H), (255, 255, 255, 0)); d = ImageDraw.Draw(im)
    rgb = tuple(int(round(c * 255)) for c in hex_to_rgb(sig.get("colour", "#1a237e"))) + (255,)
    if sig["type"] == "ink":
        for s in sig["strokes"]:
            pts = [(x * W, y * H) for x, y in s]
            if len(pts) > 1:
                d.line(pts, fill=rgb, width=5, joint="curve")
    else:
        name = sig["name"]; size = H
        while size > 8:
            try:
                font = ImageFont.truetype(sig["font"], size) if sig.get("font") else ImageFont.load_default(size)
            except Exception:
                font = ImageFont.load_default()
            x0, y0, x1, y1 = d.textbbox((0, 0), name, font=font)
            if x1 - x0 <= W - 20 and y1 - y0 <= H - 20:
                break
            size = int(size * 0.85)
        d.text(((W - (x1 - x0)) / 2 - x0, (H - (y1 - y0)) / 2 - y0), name, font=font, fill=rgb)
    bio = io.BytesIO(); im.save(bio, "PNG"); return bio.getvalue()


class SignatureDialog(tk.Toplevel):
    """Draw a signature, or type a name rendered in a handwriting font.  .result = signature dict or None."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Your signature"); self.transient(master); self.grab_set()
        self.result = None; self.strokes = []; self.cur = None; self.preview_img = None
        self.colour = "#1a237e"
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=8, pady=8)

        # --- draw tab
        draw = ttk.Frame(nb); nb.add(draw, text="Draw")
        self.c = tk.Canvas(draw, width=520, height=200, bg="white", cursor="pencil"); self.c.pack(padx=4, pady=4)
        self.c.create_line(30, 160, 490, 160, fill="#ccc", dash=(4, 4))
        self.c.bind("<ButtonPress-1>", self._down); self.c.bind("<B1-Motion>", self._move); self.c.bind("<ButtonRelease-1>", self._up)
        ttk.Button(draw, text="Clear", command=self._clear).pack(anchor="w", padx=4, pady=(0, 4))

        # --- type tab
        typed = ttk.Frame(nb); nb.add(typed, text="Type")
        row = ttk.Frame(typed); row.pack(fill="x", padx=4, pady=6)
        ttk.Label(row, text="Name").pack(side="left")
        self.name_var = tk.StringVar()
        try:
            import signing; self.name_var.set(signing.load_profile().get("name", ""))
        except Exception:
            pass
        ent = ttk.Entry(row, textvariable=self.name_var, width=32); ent.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Style").pack(side="left")
        self.fonts = _script_fonts()
        labels = [l for l, _ in self.fonts] or ["(no handwriting fonts found - italic serif)"]
        self.font_var = tk.StringVar(value=labels[0])
        cb = ttk.Combobox(row, textvariable=self.font_var, values=labels, state="readonly", width=26); cb.pack(side="left", padx=4)
        self.preview = tk.Canvas(typed, bg="white", width=520, height=120, highlightthickness=1, highlightbackground="#bbb")
        self.preview.pack(padx=4, pady=4)
        self.name_var.trace_add("write", lambda *a: self._preview()); cb.bind("<<ComboboxSelected>>", lambda e: self._preview())
        self._preview()

        bar = ttk.Frame(self); bar.pack(fill="x", padx=8, pady=(0, 8))
        self.col_btn = tk.Button(bar, text="Ink colour", bg=self.colour, fg="white", command=self._pick_colour); self.col_btn.pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="Use signature", command=self._ok).pack(side="right", padx=4)
        self.nb = nb
        self.wait_window()

    # draw
    def _down(self, e):
        self.cur = [(e.x, e.y)]

    def _move(self, e):
        if self.cur is not None:
            self.cur.append((e.x, e.y))
            self.c.create_line(*self.cur[-2], e.x, e.y, width=3, fill=self.colour, capstyle="round", tags="ink")

    def _up(self, e):
        if self.cur and len(self.cur) > 1:
            self.strokes.append(self.cur)
        self.cur = None

    def _clear(self):
        self.strokes = []; self.c.delete("ink")

    def _pick_colour(self):
        c = colorchooser.askcolor(self.colour, title="Ink colour")[1]
        if c:
            self.colour = c; self.col_btn.configure(bg=c); self.c.itemconfigure("ink", fill=c); self._preview()

    # type
    def _font_path(self):
        for label, path in self.fonts:
            if label == self.font_var.get():
                return path
        return None

    def _typed_sig(self):
        name = self.name_var.get().strip()
        return {"type": "typed", "name": name, "font": self._font_path(), "colour": self.colour} if name else None

    def _preview(self):
        sig = self._typed_sig()
        self.preview.delete("all")
        if not sig:
            self.preview.create_text(260, 60, text="Type your name above", fill="#999"); return
        try:
            png = _render_signature_png(sig, 520, 120)
            self.preview_img = tk.PhotoImage(data=png)
            self.preview.create_image(0, 0, anchor="nw", image=self.preview_img)
        except Exception as e:
            self.preview.create_text(260, 60, text=f"(preview unavailable: {e})", fill="#999")

    def _ok(self):
        if self.nb.index(self.nb.select()) == 1:
            self.result = self._typed_sig()
            if not self.result:
                return messagebox.showinfo("Signature", "Type your name first.")
        elif self.strokes:
            self.result = {"type": "ink", "colour": self.colour,
                           "strokes": [[(x / 520, y / 200) for x, y in s] for s in self.strokes]}
        else:
            return messagebox.showinfo("Signature", "Draw your signature first, or switch to the Type tab.")
        self.destroy()


class TextEditDialog(tk.Toplevel):
    """Edit text with the original font / size / alignment detected and shown; any can be overridden.
    .result = dict(text, size, fontname, fontbuffer, font_desc, align) or None."""

    ALIGNS = ["Left", "Centre", "Right", "Justify"]

    def __init__(self, master, model, page_no, title, text, span_font, flags, size, single=False, align=None):
        super().__init__(master)
        self.title(title); self.transient(master); self.grab_set()
        self.model, self.page_no, self.span_font, self.flags = model, page_no, span_font, flags
        self.result = None
        try:
            self.resolved = model.resolve_font(page_no, span_font, flags, text)   # (name, buf, desc)
        except Exception as e:
            self.resolved = ("helv", None, f"built-in Helvetica (font lookup failed: {e})")
        try:
            self._build(text, size, single, align)
        except Exception as e:
            import traceback
            self.destroy()
            messagebox.showerror("Edit text", "Could not open the editor dialog:\n\n" + traceback.format_exc()[-1500:])
            return
        master.wait_window(self)

    def _build(self, text, size, single, align):
        frm = ttk.Frame(self); frm.pack(fill="both", expand=True, padx=10, pady=10)
        if single:
            self.var = tk.StringVar(value=text)
            ent = ttk.Entry(frm, textvariable=self.var, width=70); ent.pack(fill="x"); ent.focus(); ent.select_range(0, "end")
            ent.bind("<Return>", lambda e: self.ok())
            self.get = self.var.get
        else:
            self.txt = tk.Text(frm, width=78, height=9, wrap="word"); self.txt.pack(fill="both", expand=True)
            self.txt.insert("1.0", text); self.txt.focus()
            self.txt.bind("<Control-Return>", lambda e: self.ok())
            self.get = lambda: self.txt.get("1.0", "end-1c")

        opts = ttk.Frame(frm); opts.pack(fill="x", pady=(8, 0))
        ttk.Label(opts, text="Font").pack(side="left")
        orig_label = f"Original - {self.resolved[2]}"
        self.font_var = tk.StringVar(value=orig_label)
        self.font_choices = [orig_label] + list(FONTS)
        ttk.Combobox(opts, textvariable=self.font_var, values=self.font_choices, state="readonly", width=46).pack(side="left", padx=(4, 12))
        ttk.Label(opts, text="Size").pack(side="left")
        self.size_var = tk.StringVar(value=f"{size:.1f}".rstrip("0").rstrip("."))
        ttk.Spinbox(opts, from_=4, to=96, increment=0.5, textvariable=self.size_var, width=5).pack(side="left", padx=(4, 12))
        self.align_var = tk.StringVar(value=self.ALIGNS[align] if align is not None else "Left")
        if align is not None:
            ttk.Label(opts, text="Align").pack(side="left")
            ttk.Combobox(opts, textvariable=self.align_var, values=self.ALIGNS, state="readonly", width=8).pack(side="left", padx=4)

        bar = ttk.Frame(frm); bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text="Original font detected: " + (_font_family(self.span_font) or "unknown") +
                  ("" if single else "   |   Ctrl+Enter = OK"), foreground="#666").pack(side="left")
        ttk.Button(bar, text="OK", command=self.ok).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        self.bind("<Escape>", lambda e: self.destroy())

    def ok(self):
        text = self.get()
        try:
            size = float(self.size_var.get())
        except ValueError:
            return messagebox.showwarning("Size", "Enter a number for the size.")
        choice = self.font_var.get()
        if choice == self.font_choices[0]:
            # re-resolve for the final text (glyph coverage may differ after editing)
            name, buf, desc = self.model.resolve_font(self.page_no, self.span_font, self.flags, _normalise(text))
        else:
            name, buf, desc = FONTS[choice], None, choice
        self.result = dict(text=text, size=size, fontname=name, fontbuffer=buf, font_desc=desc,
                           align=self.ALIGNS.index(self.align_var.get()) if self.align_var.get() in self.ALIGNS else 0)
        self.destroy()


def _ask_multiline(master, title, initial, single=False):
    dlg = tk.Toplevel(master); dlg.title(title); dlg.transient(master); dlg.grab_set()
    result = {"v": None}
    if single:
        var = tk.StringVar(value=initial)
        ent = ttk.Entry(dlg, textvariable=var, width=60); ent.pack(padx=10, pady=10); ent.focus(); ent.select_range(0, "end")
        get = var.get
        ent.bind("<Return>", lambda e: ok())
    else:
        txt = tk.Text(dlg, width=60, height=6, wrap="word"); txt.pack(padx=10, pady=10); txt.insert("1.0", initial); txt.focus()
        get = lambda: txt.get("1.0", "end-1c")
        txt.bind("<Control-Return>", lambda e: ok())
    bar = ttk.Frame(dlg); bar.pack(fill="x", padx=10, pady=(0, 10))

    def ok():
        result["v"] = get(); dlg.destroy()
    ttk.Button(bar, text="OK", command=ok).pack(side="right")
    ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)
    if not single:
        ttk.Label(bar, text="Ctrl+Enter = OK", foreground="#777").pack(side="left")
    dlg.bind("<Escape>", lambda e: dlg.destroy())
    master.wait_window(dlg)
    return result["v"]


def _ask_choice(master, title, values, current):
    dlg = tk.Toplevel(master); dlg.title(title); dlg.transient(master); dlg.grab_set()
    var = tk.StringVar(value=current or (values[0] if values else ""))
    ttk.Combobox(dlg, textvariable=var, values=values, state="readonly", width=40).pack(padx=10, pady=10)
    res = {"v": None}

    def ok():
        res["v"] = var.get(); dlg.destroy()
    ttk.Button(dlg, text="OK", command=ok).pack(pady=(0, 10))
    master.wait_window(dlg)
    return res["v"]


# ---------------------------------------------------------------- helpers

def _find_soffice():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import tools
        return tools.find_soffice()
    except Exception:
        return shutil.which("soffice")


def _open_file(path):
    if IS_WIN:
        os.startfile(path)
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def main():
    if not HAS_TK:
        sys.exit("The old PDF Editor window needs Tk, which this Python doesn't have - use app.py instead.")
    if fitz is None:
        root = tk.Tk(); root.withdraw()
        if messagebox.askyesno("PDF Editor - first run", "PyMuPDF is not installed. Install it now?\n(runs: pip install pymupdf)"):
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "pymupdf"], capture_output=True, text=True)
            if r.returncode == 0:
                messagebox.showinfo("PDF Editor", "Installed. Please open PDF Editor again."); return
            messagebox.showerror("PDF Editor", "Install failed:\n" + r.stderr[-1200:])
        return
    path = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".pdf") else None
    Editor(path).mainloop()


if __name__ == "__main__":
    main()
