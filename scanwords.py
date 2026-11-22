"""Words in a scan (Batch 5, `ocrlayer`) - the desktop side of the iPad's ScanWords.swift.

A scanned page is only a picture, so its words can't be selected, copied, highlighted or found.  They're read here on
this computer - Apple's text reader (Vision) on a Mac, the Tesseract add-on on Windows (or on a Mac without Vision;
without it, Windows' own reader) -
and written into the page as invisible text over the picture, the way scanners do: each word stretched over the word
in the picture, keeping the space after it, so copied words come out with spaces.

    is_scan(page)                  a picture of text: no fonts at all (or almost no words) and not blank
    read(page) -> lines            [[Word, ...], ...]: each word's text and corners in page space
    write(page, lines) -> int      the words written in, invisible; how many
"""
from __future__ import annotations

import io
import math
import subprocess
import sys
from dataclasses import dataclass

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

READ_EDGE = 2400            # the long side of the picture read, in pixels (as the iPad and Ask)
FEW_WORDS = 25              # fewer letters than this: a picture of text (as the iPad's PageReader)
ARIAL = ["/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf",
         "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf"]


class ScanError(Exception):
    """Something to tell the person."""


@dataclass
class Word:
    """A word as it sits in the picture, in the space of the page as shown (points, y down - a page stored on its
    side is shown turned): its bottom-left, bottom-right and top-left corners (a skewed scan's words lean).  `text`
    may end in a space."""
    text: str
    bl: tuple[float, float]
    br: tuple[float, float]
    tl: tuple[float, float]


# ---------------------------------------------------------------- which pages

def is_scan(page) -> bool:
    """A picture of text: no fonts at all (or fewer than FEW_WORDS letters) and not blank."""
    try:
        fonts = page.get_fonts(full=True)
    except Exception:
        fonts = []
    if fonts and len(page.get_text().strip()) >= FEW_WORDS:
        return False
    return not looks_blank(page)


def looks_blank(page) -> bool:
    """A small picture of the page is (nearly) all one colour."""
    z = 150 / max(page.rect.width, page.rect.height, 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY, alpha=False)
    data = pix.samples
    if not data:
        return True
    dark = sum(1 for b in data if b < 200)
    return dark / len(data) < 0.004


def pictures(page) -> list:
    """The page's pictures: (xref, matrix from the picture's unit square - (0, 0) its top-left - to the page as
    shown, what PyMuPDF says of it)."""
    out = []
    turn = page.rotation_matrix
    listed = None
    for info in page.get_image_info(xrefs=True):
        m = fitz.Matrix(info["transform"]) * turn
        if abs(m.a * m.d - m.b * m.c) <= 1e-9:
            continue
        xref = info.get("xref", 0)
        if not xref:
            # PyMuPDF knows a picture by a fingerprint of its pixels, taken once: after the picture has been changed
            # (a word of a scan, scanedit.py) it no longer says which it is.  The page's one picture of that size.
            if listed is None:
                listed = page.get_images(full=True)
            same = {it[0] for it in listed if it[2] == info.get("width") and it[3] == info.get("height")}
            if len(same) == 1:
                xref = same.pop()
        out.append((xref, m, info))
    return out


def picture_under(page, pt) -> bool:
    """Is there a picture under a point of the page (as shown)?  Words there may be the picture's."""
    for _, m, _ in pictures(page):
        q = fitz.Point(pt) * ~m
        if 0 <= q.x <= 1 and 0 <= q.y <= 1:
            return True
    return False


# ---------------------------------------------------------------- reading

def picture(page, annots: bool = True):
    """The page as shown (turned pages too), long side READ_EDGE px: (PNG bytes, zoom).  `annots` False: without its
    notes, highlights and signatures (for changing a word in a scan: only the picture is read)."""
    z = min(4.0, READ_EDGE / max(page.rect.width, page.rect.height, 1))
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False, annots=annots)
    return pix.tobytes("png"), z, (pix.width, pix.height)


def read(page, engine: str | None = None) -> list[list[Word]]:
    """Read the words in the page (reading order; each line's words).  Call with the document's lock held only for
    picture(); the reading itself can run outside it - see read_picture()."""
    png, z, size = picture(page)
    return to_page(page, read_picture(png, size, engine), size)


def read_picture(png: bytes, size, engine: str | None = None, reach: bool = True):
    """Lines of (text, [bl, br, tl]) with corners 0...1 across and up the picture (y up, as Vision gives them).
    `reach` False: each word's own box, not reaching on to the next word (for changing a word in a scan)."""
    engine = engine or default_engine()
    if engine == "vision":
        return _vision(png, reach)
    if engine == "tesseract":
        out = _tesseract(png, size, reach)
        if not out and windows_available():
            # Tesseract finds nothing at all on some pages a person reads easily (a photo of a letter: tilted,
            # shaded, grainy - Nov 2026); Windows' own reader has a look before the page is called wordless
            try:
                out = _windows(png, reach)
            except ScanError:
                pass
        return out
    if engine == "windows":
        return _windows(png, reach)
    raise ScanError("Reading the words in a scan needs the free Tesseract add-on.")


def default_engine() -> str | None:
    if sys.platform == "darwin" and vision_available():
        return "vision"
    import tools
    if tools.find_tesseract():
        return "tesseract"
    return "windows" if windows_available() else None       # Windows' own reader: nothing to install


def windows_available() -> bool:
    """Windows 10 and 11 can read words themselves (Windows.Media.Ocr), in the languages Windows has installed."""
    if sys.platform != "win32":
        return False
    try:
        from winrt.windows.media.ocr import OcrEngine
        import winrt.windows.graphics.imaging  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:
        return False


def vision_available() -> bool:
    try:
        import Vision  # noqa: F401
        import Quartz  # noqa: F401
        return True
    except Exception:
        return False


def to_page(page, lines, size) -> list[list[Word]]:
    """Corners 0...1 of the picture (y up) -> the page as shown (write() takes them from there to the page's own
    space, which for a page stored on its side is turned)."""
    w, h = page.rect.width, page.rect.height

    def pt(q):
        return (q[0] * w, (1 - q[1]) * h)
    return [[Word(t, pt(c[0]), pt(c[1]), pt(c[2])) for t, c in line] for line in lines if line]


def _vision(png: bytes, reach: bool = True):
    import Quartz
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png, len(png))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None
    if cg is None:
        raise ScanError("The page couldn't be read.")
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    if hasattr(req, "setAutomaticallyDetectsLanguage_"):
        req.setAutomaticallyDetectsLanguage_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise ScanError("The words in the page couldn't be read.")
    out = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        s = str(cand.string())
        line = [_xy(obs.bottomLeft()), _xy(obs.bottomRight()), _xy(obs.topLeft())]

        def box(r, cand=cand, s=s):
            # Vision counts in UTF-16 units
            a = len(s[:r[0]].encode("utf-16-le")) // 2
            n = len(s[r[0]:r[1]].encode("utf-16-le")) // 2
            try:
                res = cand.boundingBoxForRange_error_((a, n), None)
            except Exception:
                return None
            o = res[0] if isinstance(res, tuple) else res
            if o is None:
                return None
            return [_xy(o.bottomLeft()), _xy(o.bottomRight()), _xy(o.topLeft())]
        words = words_in(s, box, line, reach)
        if words:
            out.append(words)
    return out


def _xy(p):
    return (float(p.x), float(p.y))


def _tesseract(png: bytes, size, reach: bool = True):
    """Tesseract's words (TSV), grouped in lines, with their boxes."""
    import tools
    exe = tools.find_tesseract()
    if not exe:
        raise ScanError("Reading the words in a scan needs the free Tesseract add-on.")
    flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}     # no console window
    r = subprocess.run([exe, "stdin", "stdout", "tsv"], input=png, capture_output=True, timeout=180, **flags)
    if r.returncode != 0:
        raise ScanError("Tesseract couldn't read the page.")
    W, H = size
    lines: dict[tuple, list] = {}
    order = []
    for row in r.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        key = (f[2], f[3], f[4])
        x, y, w, h = (int(v) for v in f[6:10])
        if key not in lines:
            lines[key] = []
            order.append(key)
        # corners 0...1, y up
        lines[key].append((f[11].strip(), [(x / W, 1 - (y + h) / H), ((x + w) / W, 1 - (y + h) / H), (x / W, 1 - y / H)]))
    out = []
    for key in order:
        ws = lines[key]
        text = " ".join(t for t, _ in ws)
        line = [ws[0][1][0], ws[-1][1][1], ws[0][1][2]]
        boxes = iter([c for _, c in ws])
        out.append(words_in(text, lambda r: next(boxes, None), line, reach))
    return out


def _windows(png: bytes, reach: bool = True):
    """Windows' own reading: each line's words with their boxes (plain rectangles, even on a page that is turned a
    little - the angle comes with the reading, and the boxes are turned back by it)."""
    import asyncio
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise ScanError("Windows has no language installed that it can read words in.")
    pix = fitz.Pixmap(png)
    if pix.alpha or pix.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix, 0) if pix.n != 3 or pix.colorspace != fitz.csRGB else fitz.Pixmap(pix, 0)
    W, H = pix.width, pix.height
    if max(W, H) > OcrEngine.max_image_dimension:
        raise ScanError("The page is too large to read.")
    rgb = memoryview(pix.samples)
    bgra = bytearray(W * H * 4)
    step = pix.stride
    for c, k in ((2, 0), (1, 1), (0, 2)):                    # blue, green, red
        for y in range(H):
            bgra[y * W * 4 + c:(y + 1) * W * 4:4] = rgb[y * step + k:y * step + W * 3:3]
    bgra[3::4] = b"\xff" * (W * H)
    writer = DataWriter()
    writer.write_bytes(bytes(bgra))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, W, H, BitmapAlphaMode.PREMULTIPLIED)

    async def go():
        return await engine.recognize_async(bitmap)
    try:
        res = asyncio.run(go())
    except Exception:
        raise ScanError("The words in the page couldn't be read.")
    # the boxes are in the picture as Windows straightened it: turned back by the angle it found, about the middle
    turn = math.radians(res.text_angle or 0.0)
    ca, sa, cx, cy = math.cos(turn), math.sin(turn), W / 2, H / 2

    def at(x, y):
        dx, dy = x - cx, y - cy
        return ((cx + dx * ca - dy * sa) / W, 1 - (cy + dx * sa + dy * ca) / H)
    out = []
    for ln in res.lines:
        ws = []
        for w in ln.words:
            r = w.bounding_rect
            if w.text.strip() and r.width > 0 and r.height > 0:
                ws.append((w.text.strip(), [at(r.x, r.y + r.height), at(r.x + r.width, r.y + r.height), at(r.x, r.y)]))
        if not ws:
            continue
        text = " ".join(t for t, _ in ws)
        boxes = iter([c for _, c in ws])
        out.append(words_in(text, lambda r: next(boxes, None), [ws[0][1][0], ws[-1][1][1], ws[0][1][2]], reach))
    return out


def words_in(s: str, box, line, reach: bool = True):
    """Split a line into words, each with its corners (0...1, y up) - the iPad's ScanWords.wordsIn.  A word keeps the
    space after it and reaches to where the next word starts.  When there are no word boxes (or the whole line's box
    for every word) the line is shared out by letters."""
    ranges = []
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not s[j].isspace():
            j += 1
        ranges.append((i, j))
        i = j
    if not ranges:
        return []
    line_len = _dist(line[0], line[1])
    boxes = [box(r) for r in ranges]
    trusted = (all(b is not None for b in boxes) and line_len > 0
               and (len(ranges) == 1 or all(_dist(b[0], b[1]) < line_len * 0.9 for b in boxes))
               and all(_along(b2[0], line) >= _along(b1[0], line) - 0.002 for b1, b2 in zip(boxes, boxes[1:])))
    if not trusted:
        total = max(1, len(s))
        up = (line[2][0] - line[0][0], line[2][1] - line[0][1])
        boxes = []
        for a, b in ranges:
            bl, br = _lerp(line[0], line[1], a / total), _lerp(line[0], line[1], b / total)
            boxes.append([bl, br, (bl[0] + up[0], bl[1] + up[1])])
    out = []
    for k, (a, b) in enumerate(ranges):
        c = list(boxes[k])
        text = s[a:b]
        if reach and k + 1 < len(ranges):
            nxt = boxes[k + 1][0]
            dx, dy = c[1][0] - c[0][0], c[1][1] - c[0][1]
            ln = max(1e-6, math.hypot(dx, dy))
            reach = ((nxt[0] - c[0][0]) * dx + (nxt[1] - c[0][1]) * dy) / ln
            if reach > ln:
                c[1] = (c[0][0] + dx / ln * reach, c[0][1] + dy / ln * reach)
                text += " "
        out.append((text, c))
    return out


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _along(p, line):
    dx, dy = line[1][0] - line[0][0], line[1][1] - line[0][1]
    return ((p[0] - line[0][0]) * dx + (p[1] - line[0][1]) * dy) / max(1e-9, dx * dx + dy * dy)


# ---------------------------------------------------------------- writing

def write(page, lines: list[list[Word]]) -> int:
    """Write the words into the page, invisible, over the picture.  Returns how many were written."""
    words = [w for line in lines for w in line if w.text.strip()]
    if not words:
        return 0
    everything = "".join(w.text for w in words)
    try:
        everything.encode("cp1252")
        font, name = fitz.Font("helv"), "helv"
    except UnicodeEncodeError:
        path = next((p for p in ARIAL if _exists(p)), None)
        if not path:
            raise ScanError("There's no font here that can write these words.")
        font, name = fitz.Font(fontfile=path), "TPScan"
        page.insert_font(fontname=name, fontfile=path)
        words = [Word("".join(ch for ch in w.text if font.has_glyph(ord(ch))), w.bl, w.br, w.tl) for w in words]
    asc, desc = font.ascender, abs(font.descender)
    shape = page.new_shape()                              # all the words in one go: one drawing added to the page
    made = 0
    # PyMuPDF writes (and finds) words in the page's own space; a page stored on its side is shown turned.  (Until
    # Nov 2026 the words of such a page were written as if it weren't: sideways, in the wrong place.)
    own = page.derotation_matrix

    def stored(p):
        q = fitz.Point(p) * own
        return (q.x, q.y)
    for w in words:
        if not w.text.strip():
            continue
        w = Word(w.text, stored(w.bl), stored(w.br), stored(w.tl))
        along = (w.br[0] - w.bl[0], w.br[1] - w.bl[1])
        up = (w.tl[0] - w.bl[0], w.tl[1] - w.bl[1])           # y down: up points to smaller y on an upright page
        ln, h = math.hypot(*along), math.hypot(*up)
        if ln < 0.5 or h < 0.5:
            continue
        em = h / max(0.1, asc + desc)
        natural = font.text_length(w.text, fontsize=em)
        if natural <= 0:
            continue
        ux, uy = up[0] / h, up[1] / h
        # the baseline sits `desc` above the word's bottom edge
        origin = fitz.Point(w.bl[0] + ux * desc * em, w.bl[1] + uy * desc * em)
        # written along +x at `origin`, then turned and stretched about it: x -> along the word, y (down) -> -up.
        # `morph` turns the PDF's way (y up), so the two middle numbers change sign.
        sx = ln / natural
        mat = fitz.Matrix(along[0] / ln * sx, -along[1] / ln * sx, ux, -uy, 0, 0)
        shape.insert_text(origin, w.text, fontsize=em, fontname=name, render_mode=3, morph=(origin, mat))
        made += 1
    if made:
        shape.commit(overlay=True)
    return made


def _exists(p: str) -> bool:
    import os
    return os.path.exists(p)
