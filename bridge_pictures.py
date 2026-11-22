"""Pictures and signing (Batch 3 of the Folio features) for the Bridge.

Pictures: the pictures drawn on a page (page_layout gives their boxes), each taken out of the PDF at its own
quality (`picture_get`), copied to the clipboard (`picture_copy`), saved (`picture_save`) or replaced by another
PNG / JPEG in the same place (`picture_replace`, one undo step, this page only - the new picture keeps its own shape
inside the old box).  Turned or mirrored pictures come out the way they look on the page.

Where to sign (`sign_places`): empty signature fields, and labels such as "Signature:", "Signed", "Sign here" or
"Initials" next to a line of underscores or a drawn line (or, after a colon, with room beside them).  The iPad does
the same with the same rules (ios/TidyPaper/SignPlaces.swift); both are tested on
ios/TidyPaperTests/Fixtures/pictures-sign.pdf (made by make_pictures_sign.py).
"""

from __future__ import annotations

import io
import platform
import re
from pathlib import Path

import brand
from bridge_util import UserError, data_url, fitz, log, safe, unique

TINY_PT = 12                 # pictures smaller than this (either side) are decoration - not offered
PAGE_SIZED = 0.85            # a picture covering this much of the page is the page itself (a scan) - not picked by a click
VIEW_MAX_BYTES = 12 << 20    # bigger pictures are shown smaller in the viewer (Copy / Save still use the full one)
VIEW_MAX_PX = 4000
REPLACE_DPI = 300            # a new picture is scaled down to this for its box (never up)

# ------------------------------------------------------------------ where to sign: the shared rules
SIG_W, SIG_H = 200, 40       # a place for a signature: at most this wide, this tall (points)
INI_W, INI_H = 80, 24
LABEL_MAX_WORDS = 8          # a line with more words than this is a sentence, not a label
RULE_MIN = 50                # a drawn line shorter than this isn't a signing line
RULE_MAX_SHARE = 0.75        # nor is one wider than this share of the page (a page rule / separator)
ABOVE_GAP, BELOW_GAP = 24, 30
ROOM_RIGHT = 100             # room needed after "Signed:" for a signature
_LABEL = re.compile(r"^(signature|signatures|signed|sign|initials|initial)$", re.I)
_BLANK = re.compile(r"_{4,}|\.{8,}|…{3,}")


def label_kind(words: list[str]) -> tuple[int, str] | None:
    """(index of the label word, 'signature' | 'initials') for a short line that names a place to sign."""
    plain = [re.sub(r"[^\w]", "", w).lower() for w in words]
    for k, w in enumerate(plain):
        if not _LABEL.match(w):
            continue
        if w == "sign" and not (k + 1 < len(plain) and plain[k + 1] in ("here", "below", "above")) and not words[k].endswith(":"):
            continue          # "sign" on its own is a verb in a sentence; "Sign here" / "Sign:" is a label
        return k, ("initials" if w.startswith("initial") else "signature")
    return None


def _overlap(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def find_places(lines, rules, fields, signed, page_w) -> list[dict]:
    """The shared rules.  lines: [[(x0,y0,x1,y1,text), ...] per text line, in reading order]; rules: drawn
    horizontal lines [(x0, y, x1)]; fields: empty signature fields [(x0,y0,x1,y1, name)]; signed: boxes of
    signatures / initials / drawings already on the page.  -> [{kind, rect, label, source}] (display points)."""
    blanks = [(x0, y, x1, "rule") for x0, y, x1 in rules if RULE_MIN <= x1 - x0 <= RULE_MAX_SHARE * page_w]
    words_all, text_lines = [], []
    for ln in lines:
        tl = []
        for w in ln:
            m = _BLANK.search(w[4])
            if m and len(w[4]) >= 4:
                n = len(w[4])            # underscores glued to a word ("Name:______"): their share of the box
                x0 = w[0] + (w[2] - w[0]) * m.start() / n
                x1 = w[0] + (w[2] - w[0]) * m.end() / n
                blanks.append((x0, w[3], x1, "underscores"))
                rest = (w[4][:m.start()] + w[4][m.end():]).strip()
                if rest.strip(" :.…_"):
                    tl.append((w[0], w[1], x0, w[3], rest) if m.start() > 0 else (x1, w[1], w[2], w[3], rest))
            else:
                tl.append(w)
        words_all += tl
        text_lines.append(tl)
    used: set[int] = set()
    out: list[dict] = []

    def spot(kind, x0, y_line, x1, top_limit=None):
        wmax, h = (INI_W, INI_H) if kind == "initials" else (SIG_W, SIG_H)
        x1 = min(x1, x0 + wmax)
        top = y_line - h if top_limit is None else max(y_line - h, top_limit)
        return [round(x0, 1), round(top, 1), round(x1, 1), round(y_line + 3, 1)]

    for text_words in text_lines:
        names = [w[4] for w in text_words]
        if not names or len(names) > LABEL_MAX_WORDS:
            continue
        lab = label_kind(names)
        if not lab:
            continue
        k, kind = lab
        lw = text_words[k]
        lx0, ly0 = min(w[0] for w in text_words), min(w[1] for w in text_words)
        lx1, ly1 = max(w[2] for w in text_words), max(w[3] for w in text_words)
        label = " ".join(names).strip()
        best = None
        # 1. a blank on the same line, after the label
        for j, b in enumerate(blanks):
            if j in used or b[0] < lw[2] - 2:
                continue
            if ly0 - 2 <= b[1] <= ly1 + 4 and (best is None or b[0] < blanks[best][0]):
                best = j
        place = None
        if best is not None:
            b = blanks[best]; place = spot(kind, b[0], b[1], b[2]); src = "line"
        else:
            # 2. a blank just above the label ("______" over "Signature of witness"), 3. just below it
            for j, b in enumerate(blanks):
                if j in used or not (b[0] < lx1 + 10 and b[2] > lx0 - 10):
                    continue
                if ly0 - ABOVE_GAP <= b[1] <= ly0 + 2:
                    best = j; place = spot(kind, b[0], b[1], b[2]); break
            if place is None:
                for j, b in enumerate(blanks):
                    if j in used or not (b[0] < lx1 + 10 and b[2] > lx0 - 10):
                        continue
                    if ly1 + 4 <= b[1] <= ly1 + BELOW_GAP:
                        best = j; place = spot(kind, b[0], b[1], b[2], top_limit=ly1 + 1); break
            src = "line"
            # 4. "Signed:" with room after it
            if place is None and names[-1].endswith(":"):
                x0 = lx1 + 6
                if page_w - 18 - x0 >= (INI_W if kind == "initials" else ROOM_RIGHT):
                    place = spot(kind, x0, ly1, page_w - 18); src = "label"
                    if any(_overlap(place, w[:4]) > 1 for w in words_all if w not in text_words):
                        place = None
        if place is None:
            continue
        if best is not None and src == "line":
            used.add(best)
        out.append({"kind": kind, "rect": place, "label": label.rstrip(":"), "source": src})

    places = [{"kind": "signature", "rect": [round(v, 1) for v in f[:4]], "label": f[4] or "Signature", "source": "field"}
              for f in fields]
    for p in out:
        area = (p["rect"][2] - p["rect"][0]) * (p["rect"][3] - p["rect"][1])
        if any(_overlap(p["rect"], q["rect"]) > 0.3 * area for q in places):
            continue
        places.append(p)
    keep = []
    for p in places:
        area = (p["rect"][2] - p["rect"][0]) * (p["rect"][3] - p["rect"][1])
        if any(_overlap(p["rect"], s) > 0.25 * area for s in signed):
            continue                      # signed already
        keep.append(p)
    keep.sort(key=lambda p: (round(p["rect"][1] / 4), p["rect"][0]))
    return keep


# ------------------------------------------------------------------ pictures: orientation and clipboard

def orientation(m) -> tuple[bool, bool, bool]:
    """(transpose, flip_lr, flip_tb) that turn the stored picture into the way the page shows it.
    m: the picture's matrix in display space (unit square -> page; (0,0) = the picture's top-left)."""
    a, b, c, d = m.a, m.b, m.c, m.d
    if abs(a) + abs(d) >= abs(b) + abs(c):
        return False, a < 0, d < 0
    return True, c < 0, b < 0


def orient(im, how, inverse=False):
    from PIL import Image
    t, lr, tb = how
    if not inverse:
        if t:
            im = im.transpose(Image.Transpose.TRANSPOSE)
        if lr:
            im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if tb:
            im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        return im
    if tb:
        im = im.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if lr:
        im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if t:
        im = im.transpose(Image.Transpose.TRANSPOSE)
    return im


def windows_dib(png: bytes) -> bytes:
    """CF_DIB data for the Windows clipboard: a BMP without its 14-byte file header (white behind see-through parts)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255)); bg.paste(im, mask=im.split()[-1]); im = bg
    else:
        im = im.convert("RGB")
    bio = io.BytesIO(); im.save(bio, "BMP")
    return bio.getvalue()[14:]


def copy_png_to_clipboard(png: bytes):
    """Put a picture on the system clipboard (Mac: PNG + TIFF; Windows: PNG + a bitmap for older programs)."""
    system = platform.system()
    if system == "Darwin":
        from AppKit import NSImage, NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF
        from Foundation import NSData
        data = NSData.dataWithBytes_length_(png, len(png))
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setData_forType_(data, NSPasteboardTypePNG)
        img = NSImage.alloc().initWithData_(data)
        if img is not None and img.TIFFRepresentation() is not None:
            pb.setData_forType_(img.TIFFRepresentation(), NSPasteboardTypeTIFF)
        return
    if system == "Windows":
        import ctypes
        from ctypes import wintypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]; k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]; k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]; u32.SetClipboardData.restype = wintypes.HANDLE
        u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]; u32.RegisterClipboardFormatW.restype = wintypes.UINT

        def put(fmt, data: bytes):
            h = k32.GlobalAlloc(0x0002, len(data))          # GMEM_MOVEABLE
            if not h:
                raise OSError("GlobalAlloc failed")
            p = k32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(fmt, h):
                k32.GlobalFree(h)                           # the clipboard didn't take it, so it's still ours
                raise OSError("SetClipboardData failed")

        for _ in range(10):                                  # another program may hold the clipboard for a moment
            if u32.OpenClipboard(None):
                break
            k32.Sleep(50)
        else:
            raise UserError("The clipboard is busy in another program - try again.")
        try:
            u32.EmptyClipboard()
            put(8, windows_dib(png))                        # CF_DIB
            put(u32.RegisterClipboardFormatW("PNG"), png)   # keeps see-through parts for Office and browsers
        finally:
            u32.CloseClipboard()
        return
    raise UserError("Copying pictures isn't available on this computer - use Save picture instead.")


# ------------------------------------------------------------------ the mixin
class PicturesMixin:
    """Mixed into app.Bridge; relies on self._lock, self._page_ok(), self._done(), self._D(), self._model."""

    # ---- what's on the page
    def _pictures(self, i: int) -> list[dict]:
        m = self._model
        pg = m.page(i)
        try:
            infos = pg.get_image_info(xrefs=True)
        except Exception:                                   # a damaged picture shouldn't stop the page loading
            log.exception("image info, page %d", i)
            return []
        pa = abs(pg.rect)
        out = []
        for k, info in enumerate(infos):
            bb = fitz.Rect(info["bbox"]) & pg.rect
            if bb.is_empty or bb.width < TINY_PT or bb.height < TINY_PT:
                continue
            out.append({"k": k, "xref": info.get("xref", 0), "rect": self._D(i, bb),
                        "w": info.get("width", 0), "h": info.get("height", 0),
                        "page_sized": abs(bb) >= PAGE_SIZED * pa})
        return out

    def _picture(self, i: int, k: int, xref: int | None = None) -> dict:
        pg = self._model.page(i)
        infos = pg.get_image_info(xrefs=True)
        if not 0 <= int(k) < len(infos) or (xref is not None and infos[int(k)].get("xref", 0) != int(xref)):
            raise UserError("That picture is no longer on the page.")
        return infos[int(k)]

    def _display_matrix(self, i: int, info) -> fitz.Matrix:
        pg = self._model.page(i)
        return fitz.Matrix(info["transform"]) * pg.rotation_matrix

    def _picture_image(self, i: int, info, full: bool = True):
        """-> (PIL image as shown on the page, original bytes or None, mime of the original)."""
        from PIL import Image
        doc = self._model.doc
        xref = info.get("xref", 0)
        how = orientation(self._display_matrix(i, info))
        raw, mime, im = None, "image/png", None
        if xref:
            try:
                x = doc.extract_image(xref)
                ext = (x.get("ext") or "").lower()
                if x.get("smask"):
                    pix = fitz.Pixmap(doc, xref)
                    mask = fitz.Pixmap(doc, x["smask"])
                    if pix.alpha:
                        pix = fitz.Pixmap(pix, 0)
                    if pix.colorspace and pix.colorspace.n not in (1, 3):
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    pix = fitz.Pixmap(pix, mask)
                    im = Image.open(io.BytesIO(pix.tobytes("png")))
                else:
                    im = Image.open(io.BytesIO(x["image"]))
                    im.load()
                    if ext in ("jpeg", "jpg", "png") and im.mode in ("RGB", "L", "RGBA", "LA", "P") and how == (False, False, False):
                        raw, mime = x["image"], "image/jpeg" if ext in ("jpeg", "jpg") else "image/png"
                    if im.mode not in ("RGB", "L", "RGBA", "LA"):
                        pix = fitz.Pixmap(doc, xref)
                        if pix.colorspace and pix.colorspace.n not in (1, 3):
                            pix = fitz.Pixmap(fitz.csRGB, pix)
                        im = Image.open(io.BytesIO(pix.tobytes("png")))
            except Exception:
                log.exception("picture %s on page %d - drawing it from the page instead", xref, i)
                im, raw = None, None
        if im is None:                                     # an inline picture or one we can't read: draw it from the page
            pg = self._model.page(i)
            bb = fitz.Rect(info["bbox"]) & pg.rect
            scale = max(2.0, min(6.0, (info.get("width") or 0) / max(bb.width, 1)))
            pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=bb, alpha=False, annots=False)
            im = Image.open(io.BytesIO(pix.tobytes("png")))
            how, mime = (False, False, False), "image/png"
            if pg.rotation:
                im = im.rotate(-pg.rotation, expand=True)
        else:
            im = orient(im, how)
        return im, raw, mime

    def _picture_bytes(self, i, k, xref=None) -> tuple[bytes, str, tuple[int, int]]:
        info = self._picture(i, k, xref)
        im, raw, mime = self._picture_image(i, info)
        if raw:
            return raw, mime, im.size
        bio = io.BytesIO()
        im.save(bio, "PNG", optimize=False)
        return bio.getvalue(), "image/png", im.size

    # ---- view / copy / save
    @safe
    def picture_get(self, i: int, k: int, xref: int | None = None):
        """The picture at full quality for the viewer (shown smaller if it's very big)."""
        with self._lock:
            self._page_ok(i)
            data, mime, (w, h) = self._picture_bytes(i, k, xref)
            info = self._picture(i, k, xref)
            smaller = False
            if len(data) > VIEW_MAX_BYTES or max(w, h) > VIEW_MAX_PX:
                from PIL import Image
                im = Image.open(io.BytesIO(data)); im.thumbnail((VIEW_MAX_PX, VIEW_MAX_PX))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGBA")
                bio = io.BytesIO()
                if im.mode == "RGBA":
                    im.save(bio, "PNG"); vm = "image/png"
                else:
                    im.save(bio, "JPEG", quality=90); vm = "image/jpeg"
                view, smaller = data_url(bio.getvalue(), vm), True
            else:
                view = data_url(data, mime)
            dm = self._display_matrix(i, info)
            dpi = round(w / max(abs(dm.a) + abs(dm.c), 1) * 72)
            return {"src": view, "w": w, "h": h, "smaller": smaller, "mime": mime, "bytes": len(data),
                    "dpi": dpi, "inline": not info.get("xref")}

    @safe
    def picture_copy(self, i: int, k: int, xref: int | None = None):
        with self._lock:
            self._page_ok(i)
            data, mime, (w, h) = self._picture_bytes(i, k, xref)
        if mime != "image/png":
            from PIL import Image
            bio = io.BytesIO(); Image.open(io.BytesIO(data)).save(bio, "PNG"); data = bio.getvalue()
        copy_png_to_clipboard(data)
        return {"status": f"Picture copied ({w} × {h} pixels). Paste it into a message, a document or a photo editor."}

    @safe
    def picture_save(self, i: int, k: int, xref: int | None = None, path: str | None = None):
        """Save the picture as a PNG / JPEG file (asks where unless path is given)."""
        with self._lock:
            self._page_ok(i)
            data, mime, _ = self._picture_bytes(i, k, xref)
            ext = ".jpg" if mime == "image/jpeg" else ".png"
            src = self._source or self._model.path or (Path.home() / "Documents" / "Document.pdf")
            stem = src.stem[:-len("_edited")] if src.stem.endswith("_edited") else src.stem
            n = sum(1 for p in self._pictures(i) if p["k"] <= int(k))
            name = f"{stem} - picture p{int(i) + 1}-{max(n, 1)}{ext}"
        if not path:
            if not getattr(self, "_window", None):
                return {"error": "Saving a picture needs the app window."}
            import webview
            start = unique(src.with_name(name))
            res = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(start.parent), save_filename=start.name,
                                                  file_types=("JPEG pictures (*.jpg)" if ext == ".jpg" else "PNG pictures (*.png)",))
            if not res:
                return {"cancelled": True}
            path = res if isinstance(res, str) else res[0]
        p = Path(path)
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            p = p.with_suffix(ext)
        if (p.suffix.lower() == ".png") != (mime == "image/png"):  # they typed the other kind: convert
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            bio = io.BytesIO()
            if p.suffix.lower() == ".png":
                im.save(bio, "PNG")
            else:
                im.convert("RGB").save(bio, "JPEG", quality=92)
            data = bio.getvalue()
        p.write_bytes(data)
        return {"saved": str(p), "status": f"Picture saved as {p.name}."}

    # ---- replace
    def _set_ref(self, holder: int, path: list[str], value: str):
        """xref_set_key through indirect objects (PyMuPDF can't set a reference through an indirect step)."""
        doc = self._model.doc
        cur = holder
        for j, key in enumerate(path[:-1]):
            t, v = doc.xref_get_key(cur, key)
            if t == "xref":
                cur = int(v.split()[0])
            elif t == "dict":
                rest = path[j:]
                # a direct dict on the way: set the reference inside it as text
                t2, v2 = doc.xref_get_key(cur, "/".join(rest[:-1]))
                if t2 == "dict":
                    new = re.sub(r"/" + re.escape(rest[-1]) + r"\s+\d+\s+\d+\s+R", f"/{rest[-1]} {value}", v2)
                    doc.xref_set_key(cur, "/".join(rest[:-1]), new)
                    return
                raise UserError("This picture is stored in a way that can't be swapped.")
            else:
                raise UserError("This picture is stored in a way that can't be swapped.")
        doc.xref_set_key(cur, path[-1], value)

    @safe
    def picture_replace(self, i: int, k: int, path: str, xref: int | None = None):
        """Put another picture in this one's place on this page (it keeps its own shape inside the old box)."""
        from PIL import Image, ImageOps
        with self._lock:
            m = self._page_ok(i)
            info = self._picture(i, k, xref)
            old = info.get("xref", 0)
            if not old:
                raise UserError("This picture is drawn straight into the page, so it can't be swapped. "
                                "Cover it with Text › Picture instead.")
            try:
                new = Image.open(path)
                new = ImageOps.exif_transpose(new)
                new.load()
            except Exception:
                raise UserError("That file isn't a picture we can read - try a PNG or JPG.")
            pg = m.page(i)
            refs = [x for x in pg.get_images(full=True) if x[0] == old]
            if not refs:
                raise UserError("This picture is stored in a way that can't be swapped.")
            # the same picture can be drawn under several names: take the one drawn in this box
            want = fitz.Rect(info["bbox"])

            def off(item):
                try:
                    r = pg.get_image_bbox(item)
                    return sum(abs(u - v) for u, v in zip(r, want))
                except Exception:
                    return 1e9
            item = min(refs, key=off)
            name, referencer = item[7], item[9]
            # the box as shown: fit the new picture inside it, keeping its shape, at up to 300 dpi
            dm = self._display_matrix(i, info)
            box_w, box_h = abs(dm.a) + abs(dm.c), abs(dm.b) + abs(dm.d)
            how = orientation(dm)
            tw = max(1, round(box_w / 72 * REPLACE_DPI)); th = max(1, round(box_h / 72 * REPLACE_DPI))
            nw, nh = new.size
            s = min(tw / nw, th / nh, 1.0)
            fw, fh = max(1, round(nw * s)), max(1, round(nh * s))
            # the canvas keeps the old box's shape; the picture sits in its middle
            if abs((nw / nh) / (box_w / box_h) - 1) < 0.02:
                cw, ch = fw, fh
            elif nw / nh > box_w / box_h:
                cw, ch = fw, max(1, round(fw * box_h / box_w))
            else:
                cw, ch = max(1, round(fh * box_w / box_h)), fh
            jpeg_ok = new.mode in ("RGB", "L", "CMYK") and (cw, ch) == (fw, fh)
            pic = new.convert("RGB" if jpeg_ok else "RGBA").resize((fw, fh), Image.LANCZOS) if (fw, fh) != new.size else \
                new.convert("RGB" if jpeg_ok else "RGBA")
            if (cw, ch) != (fw, fh):
                canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
                canvas.paste(pic, ((cw - fw) // 2, (ch - fh) // 2))
                pic = canvas
            pic = orient(pic, how, inverse=True)
            bio = io.BytesIO()
            if jpeg_ok:
                pic.save(bio, "JPEG", quality=90)
            else:
                pic.save(bio, "PNG")
            m.snapshot()
            pg = m.page(i)
            new_xref = pg.insert_image(pg.rect, stream=bio.getvalue())
            m.doc.update_stream(pg.get_contents()[-1], b" ")    # keep the picture, not the drawing insert_image added
            extra = [x[7] for x in pg.get_images(full=True) if x[0] == new_xref and x[9] == 0]
            holder = referencer or pg.xref
            self._set_ref(holder, ["Resources", "XObject", name], f"{new_xref} 0 R")
            for nm in extra:                                   # the name insert_image added is no longer drawn
                if nm != name or referencer:
                    try:
                        self._set_ref(pg.xref, ["Resources", "XObject", nm], "null")
                    except UserError:
                        pass
            pg = m.page(i)
            times = sum(1 for x in pg.get_image_info(xrefs=True) if x.get("xref") == new_xref)
            shared = referencer and any(
                any(x[9] == referencer for x in m.page(j).get_images(full=True)) for j in range(len(m)) if j != int(i))
            msg = "Picture replaced."
            if times > 1:
                msg = f"Picture replaced - it was used {times} times on this page, so all {times} changed."
            if shared:
                msg += " It's part of a design used on other pages too, so it changed there as well."
            if (cw, ch) != (fw, fh):
                msg += " It kept its own shape, so there's a little space beside it."
            return self._done(i, msg)

    # ---- where to sign
    @safe
    def sign_places(self):
        """Places in the document that look like they want a signature or initials."""
        with self._lock:
            m = self._need_doc()
            out = []
            for i in range(len(m)):
                out += [{"page": i, **p} for p in self._sign_places_on(i)]
            return {"places": out[:40]}

    def _sign_places_on(self, i: int) -> list[dict]:
        m = self._model
        pg = m.page(i)
        lines: dict[tuple, list] = {}
        for w in pg.get_text("words"):
            r = self._D(i, w[:4])
            lines.setdefault((w[5], w[6]), []).append((*r, w[4]))
        rules = []
        for dr in pg.get_drawings():
            for it in dr["items"]:
                if it[0] == "l":
                    a, b = it[1], it[2]
                    if abs(a.y - b.y) < 1:
                        r = self._D(i, fitz.Rect(min(a.x, b.x), a.y, max(a.x, b.x), a.y + 0.01))
                        rules.append((r[0], r[1], r[2]))
                elif it[0] == "re":
                    rr = fitz.Rect(it[1])
                    if rr.height <= 2.5 and rr.width >= RULE_MIN:
                        r = self._D(i, rr)
                        rules.append((r[0], (r[1] + r[3]) / 2, r[2]))
        fields, signed = [], []
        for w in pg.widgets() or []:
            if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and not (w.field_flags & 1):
                if m.doc.xref_get_key(w.xref, "V")[0] == "null":
                    fields.append((*self._D(i, w.rect), w.field_label or w.field_name or "Signature"))
                else:
                    signed.append(self._D(i, w.rect))
        for a in pg.annots() or []:
            if a.type[0] in (fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_INK):
                signed.append(self._D(i, a.rect))
        return find_places(list(lines.values()), rules, fields, signed, self._D(i, pg.rect)[2])
