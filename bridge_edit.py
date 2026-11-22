"""Editor tools for the Bridge (stage 2): annotations, text editing, forms, signatures-as-ink.

Coordinates from JavaScript are *display points*: the page as shown (rotation applied), origin top-left,
1 unit = 1 PDF point.  They are converted to unrotated page space with PdfModel.to_page(..., zoom=1).
Every edit returns the fresh document info plus  status  (what just happened) and  changed  (page list).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import brand
import pdf_editor as pe
import scanwords
from bridge_util import UserError, data_url, fitz, hex_rgb, log, rgb_hex, safe

SIG_IMAGE_MAX = 1600            # px - uploaded signature images are scaled down to this
FONT_PREVIEW_MAX = 4 << 20       # bytes - bigger fonts are not sent to the page for previews

W = fitz  # widget constants live on the module
FIELD_KIND = {
    W.PDF_WIDGET_TYPE_TEXT: "text", W.PDF_WIDGET_TYPE_CHECKBOX: "check", W.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    W.PDF_WIDGET_TYPE_COMBOBOX: "combo", W.PDF_WIDGET_TYPE_LISTBOX: "list", W.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    W.PDF_WIDGET_TYPE_BUTTON: "button",
}
MOVABLE = {fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_STAMP}
TEXT_ANNOTS = {fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_TEXT}
RESIZABLE = {fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_FREE_TEXT}
STAMP_KINDS = {"Signature": "signature", "Initials": "initials", "Picture": "picture", "Mark": "mark"}  # stamp subjects
ANNOT_NAMES = {fitz.PDF_ANNOT_HIGHLIGHT: "highlight", fitz.PDF_ANNOT_UNDERLINE: "underline",
               fitz.PDF_ANNOT_STRIKE_OUT: "strike-through", fitz.PDF_ANNOT_SQUIGGLY: "squiggle",
               fitz.PDF_ANNOT_INK: "drawing", fitz.PDF_ANNOT_SQUARE: "box", fitz.PDF_ANNOT_CIRCLE: "ellipse",
               fitz.PDF_ANNOT_LINE: "line", fitz.PDF_ANNOT_FREE_TEXT: "text box", fitz.PDF_ANNOT_TEXT: "note",
               fitz.PDF_ANNOT_STAMP: "stamp"}
CSS_FONT = {"sans": "Arial, Helvetica, sans-serif", "serif": "'Times New Roman', Times, serif",
            "mono": "'Courier New', Courier, monospace"}
ALIGN_NAMES = ["left", "centred", "right", "justified"]
STANDARD_FONTS = ("helvetica", "times", "courier")


def _font_class(font: str, flags: int) -> str:
    f = font.lower()
    if flags & 8 or any(k in f for k in ("courier", "mono", "consol")):
        return "mono"
    if flags & 4 or pe.serif_name(font):
        return "serif"
    return "sans"


# ------------------------------------------------------------------ the page's font, for the edit dialog's preview

def web_font(buf: bytes) -> bytes | None:
    """A font taken out of a PDF, as a file a web page can load (the edit dialog shows the words in it).  TrueType /
    OpenType go as they are.  Bare CFF - how InDesign and other design programs embed fonts ("Type1C") - isn't a font
    file on its own, and browsers refuse it: it is wrapped in a minimal OpenType font (its letters' map and widths
    from MuPDF).  None when it can't be (old Type 1, CID-keyed fonts without a Unicode map)."""
    import struct
    if not buf or len(buf) < 4:
        return None
    if buf[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"wOFF", b"wOF2"):
        return buf
    if buf[0] != 1 or buf[1] != 0:              # not CFF (major version 1)
        return None
    try:
        f = fitz.Font(fontbuffer=buf)
        n = f.glyph_count
        cmap, adv = {}, {}
        for u in list(range(0x20, 0x250)) + list(range(0x2000, 0x2070)) + [0x20AC, 0x2122, 0x2212, 0xFB01, 0xFB02]:
            g = f.has_glyph(u)
            if g and 0 < g < n:
                cmap[u] = g
                adv[g] = round(f.glyph_advance(u) * 1000)
        if not cmap or n < 1:
            return None
    except Exception:
        return None
    upm = 1000
    bb = fitz.Rect(f.bbox)
    x0, y0, x1, y1 = (round(v * upm) for v in (bb.x0, bb.y0, bb.x1, bb.y1))
    asc, desc = round(f.ascender * upm), round(f.descender * upm)
    wmax = max(adv.values())
    i16 = lambda v: max(-32768, min(32767, int(v)))
    head = struct.pack(">IIIIHHqqhhhhHhhhh", 0x10000, 0x10000, 0, 0x5F0F3CF5, 0x000B, upm, 0, 0,
                       i16(x0), i16(y0), i16(x1), i16(y1), 0, 8, 2, 0, 0)
    hhea = struct.pack(">IhhhHhhhhhhhhhhhH", 0x10000, i16(asc), i16(desc), 0, wmax, 0, 0, i16(x1), 1, 0, 0,
                       0, 0, 0, 0, 0, n)
    hmtx = b"".join(struct.pack(">Hh", max(0, adv.get(g, 0)), 0) for g in range(n))
    maxp = struct.pack(">IH", 0x5000, n)
    codes = sorted(c for c in cmap if c <= 0xFFFF)
    seg = len(codes) + 1
    sr = 2 * (1 << (seg.bit_length() - 1))
    ends = codes + [0xFFFF]
    deltas = [(cmap[c] - c) % 65536 for c in codes] + [1]
    sub = struct.pack(">HHHHHH", 4, 0, 0, seg * 2, sr, (seg.bit_length() - 1)) + struct.pack(">H", seg * 2 - sr)
    sub += struct.pack(f">{seg}H", *ends) + b"\0\0" + struct.pack(f">{seg}H", *ends)
    sub += struct.pack(f">{seg}H", *deltas) + struct.pack(f">{seg}H", *([0] * seg))
    sub = sub[:2] + struct.pack(">H", len(sub)) + sub[4:]
    cmap_t = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    name_s = [(1, "TidyPaper Preview"), (2, "Regular"), (4, "TidyPaper Preview"), (6, "TidyPaperPreview")]
    recs, strs = b"", b""
    for nid, txt in name_s:
        e = txt.encode("utf-16-be")
        recs += struct.pack(">HHHHHH", 3, 1, 0x409, nid, len(e), len(strs))
        strs += e
    name = struct.pack(">HHH", 0, len(name_s), 6 + 12 * len(name_s)) + recs + strs
    os2 = struct.pack(">HhHHHhhhhhhhhhhh", 4, i16(sum(adv.values()) / max(len(adv), 1)), 400, 5, 0,
                      650, 600, 0, 75, 650, 600, 0, 350, 50, 250, 0)
    os2 += b"\0" * 10 + struct.pack(">IIII", 1, 0, 0, 0) + b"TKPV"
    os2 += struct.pack(">HHHhhhHHII", 0x40, min(codes), max(codes), i16(asc), i16(desc), 0,
                       max(0, i16(y1)), max(0, -i16(y0)), 1, 0)
    os2 += struct.pack(">hhHHH", 500, 700, 0, 32, 1)
    post = struct.pack(">IIhhIIIII", 0x30000, 0, -100, 50, 0, 0, 0, 0, 0)
    tables = sorted({b"CFF ": buf, b"OS/2": os2, b"cmap": cmap_t, b"head": head, b"hhea": hhea, b"hmtx": hmtx,
                     b"maxp": maxp, b"name": name, b"post": post}.items())
    def checksum(d):
        d = d + b"\0" * (-len(d) % 4)
        return sum(struct.unpack(f">{len(d) // 4}I", d)) & 0xFFFFFFFF
    nt = len(tables)
    es = nt.bit_length() - 1
    out = struct.pack(">4sHHHH", b"OTTO", nt, 16 << es, es, nt * 16 - (16 << es))
    off = 12 + 16 * nt
    body = b""
    for tag, d in tables:
        out += struct.pack(">4sIII", tag, checksum(d), off + len(body), len(d))
        body += d + b"\0" * (-len(d) % 4)
    font = out + body
    hoff = 12 + 16 * nt + sum(len(d) + (-len(d) % 4) for t, d in tables if t < b"head")
    adj = (0xB1B0AFBA - checksum(font)) & 0xFFFFFFFF
    return font[:hoff + 8] + struct.pack(">I", adj) + font[hoff + 12:]


# ------------------------------------------------------------------ signature storage (drawn / typed / image)

SLOTS = ("signature", "initials")


def slot_file(slot: str = "signature") -> Path:
    if slot not in SLOTS:
        raise UserError("Unknown signature kind.")
    return pe.SIGNATURE_FILE if slot == "signature" else pe.CONFIG_DIR / "initials.json"


def load_signature(slot: str = "signature") -> dict | None:
    try:
        data = json.loads(slot_file(slot).read_text())
        if isinstance(data, dict) and data.get("type") == "image" and data.get("png"):
            return data
    except Exception:
        pass
    if slot == "signature":
        return pe._load_signature()          # understands the older stored formats too
    try:
        data = json.loads(slot_file(slot).read_text())
        return data if isinstance(data, dict) and data.get("type") in ("ink", "typed") else None
    except Exception:
        return None


def save_signature(sig: dict, slot: str = "signature"):
    pe.CONFIG_DIR.mkdir(exist_ok=True)
    slot_file(slot).write_text(json.dumps(sig))


def mark_png(kind: str, colour: str) -> bytes:
    """A tick, cross or dot drawn smoothly (4x supersampled) on a transparent square."""
    from PIL import Image, ImageDraw
    S = 400
    im = Image.new("RGBA", (S * 4, S * 4), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    rgb = tuple(int(round(c * 255)) for c in hex_rgb(colour, (0.07, 0.09, 0.15))) + (255,)
    w = 64 * 4
    if kind == "tick":
        pts = [(60 * 4, 215 * 4), (160 * 4, 320 * 4), (345 * 4, 85 * 4)]
        d.line(pts, fill=rgb, width=w, joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=rgb)
    elif kind == "cross":
        for a, b in (((80, 80), (320, 320)), ((320, 80), (80, 320))):
            a, b = (a[0] * 4, a[1] * 4), (b[0] * 4, b[1] * 4)
            d.line([a, b], fill=rgb, width=w)
            for x, y in (a, b):
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=rgb)
    elif kind == "dot":
        d.ellipse((110 * 4, 110 * 4, 290 * 4, 290 * 4), fill=rgb)
    else:
        raise UserError("Unknown mark.")
    im = im.resize((S, S), Image.LANCZOS)
    bio = io.BytesIO(); im.save(bio, "PNG")
    return bio.getvalue()


def turn_png(png: bytes, page_rotation: int) -> bytes:
    """A picture for a stamp on a page turned by page_rotation (its /Rotate): what's drawn on a page is turned with it
    when shown, so the picture is turned the other way first and shows upright (a signature on a scan the phone
    stored sideways).  Negative = undo that.  0 = unchanged."""
    turn = int(page_rotation or 0) % 360
    if not turn:
        return png
    from PIL import Image
    with Image.open(io.BytesIO(png)) as im:
        out = io.BytesIO()
        im.rotate(turn, expand=True).save(out, "PNG")        # PIL turns anticlockwise; /Rotate is clockwise
        return out.getvalue()


def signature_png(sig: dict, w: int = 600, h: int = 240) -> bytes:
    if sig["type"] == "image":
        import base64
        return base64.b64decode(sig["png"])
    return pe._render_signature_png(sig, w, h)


def signature_art(sig: dict) -> tuple[bytes, float]:
    """The signature as a high-resolution transparent PNG cropped to the ink -> (png, width / height)."""
    from PIL import Image, ImageDraw
    if sig["type"] == "ink":
        W, H = 2080, 800                       # strokes are stored normalised to a 520 x 200 pad
        im = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        d = ImageDraw.Draw(im)
        rgb = tuple(int(round(c * 255)) for c in hex_rgb(sig.get("colour", "#1a237e"))) + (255,)
        for s in sig["strokes"]:
            pts = [(x * W, y * H) for x, y in s]
            if len(pts) > 1:
                d.line(pts, fill=rgb, width=14, joint="curve")
                for x, y in (pts[0], pts[-1]):
                    d.ellipse((x - 7, y - 7, x + 7, y + 7), fill=rgb)
    else:
        src = signature_png(sig, 1800, 600)
        im = Image.open(io.BytesIO(src)).convert("RGBA")
    box = im.getchannel("A").getbbox()
    if not box:
        raise UserError("That signature is empty - draw or type it again.")
    pad = max(4, int((box[3] - box[1]) * 0.06))
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
    im = im.crop(box)
    bio = io.BytesIO(); im.save(bio, "PNG")
    return bio.getvalue(), im.width / im.height


def picture_png(path: Path, max_px: int = 2400) -> tuple[bytes, float]:
    """Any picture -> PNG (orientation fixed, scaled down if huge) -> (png, width / height)."""
    from PIL import Image, ImageOps
    try:
        im = ImageOps.exif_transpose(Image.open(path))
    except Exception:
        raise UserError("That file isn't a picture we can read - try a PNG or JPG.")
    im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
    im.thumbnail((max_px, max_px))
    bio = io.BytesIO(); im.save(bio, "PNG")
    return bio.getvalue(), im.width / im.height



def text_redaction(pg, r):
    """For a rectangle over selected words: (bands, box) - a thin band through each line's letters, which is what
    takes the words out, and the box over their ink, which is blacked out (pictures and drawings under it included).

    Word boxes are the font's full height (about 1.35 times its size), so they reach into the lines above and below,
    and MuPDF's redaction takes any letter whose box is more than 10% covered: redacting one line of a paragraph with
    the word boxes also took the lines above and below.  A band from 0.55 to 0.1 of the size above the baseline covers
    a third of every letter's box on its line, and none of the next line's even when it's set solid.  The ink runs
    from 0.8 of the size above the baseline (capitals, accents) to 0.22 below (descenders).
    None when there are no letters to go by (or the text runs sideways): redact the rectangle as it is."""
    r = fitz.Rect(r)
    lines: dict[float, fitz.Rect] = {}
    box = None
    for b in pg.get_text("rawdict", flags=0).get("blocks", []):
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                size = sp.get("size") or 0
                for c in sp.get("chars", []):
                    cb = fitz.Rect(c["bbox"])
                    if not c["c"].strip() or not r.contains(fitz.Point((cb.x0 + cb.x1) / 2, (cb.y0 + cb.y1) / 2)):
                        continue
                    dx, dy = ln.get("dir", (1, 0))
                    if abs(dy) > 0.01 or dx <= 0:
                        return None
                    y = c["origin"][1]
                    band = fitz.Rect(cb.x0, y - 0.55 * size, cb.x1, y - 0.1 * size)
                    key = round(ln["bbox"][3], 1)
                    lines[key] = lines[key] | band if key in lines else band
                    ink = fitz.Rect(r.x0, y - 0.8 * size, r.x1, y + 0.22 * size)
                    box = ink if box is None else box | ink
    if box is None:
        return None
    box = fitz.Rect(r.x0, max(r.y0, box.y0), r.x1, min(r.y1, box.y1))
    return list(lines.values()), box

class EditMixin:
    """Mixed into app.Bridge; relies on self._lock, self._need_doc(), self._info()."""

    # ---- coordinate helpers
    def _P(self, i, x, y):
        return self._model.to_page(i, float(x), float(y), 1.0)

    def _R(self, i, r):
        return self._model.rect_to_page(i, *[float(v) for v in r[:4]], 1.0)

    def _D(self, i, rect):
        return [round(v, 2) for v in self._model.rect_to_canvas(i, fitz.Rect(rect), 1.0)]

    def _done(self, i, status, **extra):
        # drop the page we kept alive for annotation edits: a second live Page object for the same page
        # would write its own (stale) /Annots array and silently undo later additions such as links
        self._model._held_page = None
        return {**self._info(), "status": status, "changed": None if i is None else [i], **extra}

    def _page_ok(self, i):
        m = self._need_doc()
        if not 0 <= int(i) < len(m):
            raise UserError("That page doesn't exist.")
        return m

    # ---- what's on a page (for hover, selection, form overlays)
    @safe
    def page_layout(self, i: int):
        with self._lock:
            m = self._page_ok(i)
            pg = m.page(i)
            words = []
            for w in pg.get_text("words"):
                x0, y0, x1, y1 = self._D(i, w[:4])
                words.append([x0, y0, x1, y1, w[4], w[5], w[6]])
            blocks = [self._D(i, b[:4]) for b in pg.get_text("blocks") if b[6] == 0]
            annots = []
            for a in pg.annots() or []:
                t = a.type[0]
                if t in (fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP):
                    continue
                kind = STAMP_KINDS.get(a.info.get("subject"), ANNOT_NAMES.get(t, a.type[1].lower())) \
                    if t == fitz.PDF_ANNOT_STAMP else ANNOT_NAMES.get(t, a.type[1].lower())
                r = self._D(i, a.rect)
                markup = t in (fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKE_OUT,
                               fitz.PDF_ANNOT_SQUIGGLY)
                stroke = a.colors.get("stroke") or []
                annots.append({"xref": a.xref, "kind": kind, "rect": r,
                               "text": a.info.get("content", "") if t != fitz.PDF_ANNOT_STAMP else "",
                               "markup": markup, "note": (a.info.get("content") or "").strip() if markup else "",
                               "color": "#" + "".join(f"{round(v * 255):02x}" for v in stroke[:3]) if markup and len(stroke) >= 3 else "",
                               "movable": t in MOVABLE, "editable": t in TEXT_ANNOTS,
                               "resizable": t in RESIZABLE, "keep_shape": t == fitz.PDF_ANNOT_STAMP})
            fields = []
            for w in pg.widgets() or []:
                kind = FIELD_KIND.get(w.field_type, "other")
                if kind in ("button", "other"):
                    continue
                val = w.field_value
                f = {"xref": w.xref, "kind": kind, "name": w.field_label or w.field_name or "", "rect": self._D(i, w.rect),
                     "readonly": bool(w.field_flags & 1)}
                if kind in ("check", "radio"):
                    f["checked"] = val not in (False, None, "Off", "")
                elif kind == "signature":
                    f["signed"] = m.doc.xref_get_key(w.xref, "V")[0] != "null"
                else:
                    f["value"] = "" if val in (None, False) else str(val)
                    f["choices"] = [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]
                    f["multiline"] = bool(w.field_flags & (1 << 12))
                    f["size"] = w.text_fontsize or 0
                    f["maxlen"] = w.text_maxlen or 0
                fields.append(f)
            links = [{"rect": self._D(i, l["from"]), "uri": l.get("uri", "")} for l in pg.get_links() if l.get("uri")]
            images = self._pictures(i)
        return {"page": i, "words": words, "blocks": blocks, "annots": annots, "fields": fields, "links": links,
                "images": images}

    # ---- add things
    @safe
    def annotate(self, i: int, kind: str, geom: dict, style: dict | None = None):
        style = style or {}
        color = hex_rgb(style.get("color"), (0, 0, 0))
        width = float(style.get("width") or 1.5)
        with self._lock:
            m = self._page_ok(i)
            if kind in ("highlight", "underline", "strikeout"):
                rects = [self._R(i, r) for r in (geom.get("rects") or [geom["rect"]])]
                if kind == "highlight" and not style.get("color"):
                    color = (1, 0.85, 0.1)
                a = m.add_markup_rects(i, kind, rects, color)
                return self._done(i, {"highlight": "Highlighted.", "underline": "Underlined.", "strikeout": "Struck through."}[kind],
                                  xref=a.xref)
            if kind == "ink":
                strokes = [[self._P(i, x, y) for x, y in s] for s in geom["strokes"] if len(s) > 1]
                if not strokes:
                    raise UserError("Draw a little longer line.")
                m.add_ink(i, strokes, color, width)
                return self._done(i, "Drawing added.")
            if kind in ("rect", "ellipse"):
                m.add_rect(i, self._R(i, geom["rect"]), color, width, ellipse=kind == "ellipse")
                return self._done(i, "Box added." if kind == "rect" else "Ellipse added.")
            if kind in ("line", "arrow"):
                m.add_line(i, self._P(i, *geom["p1"]), self._P(i, *geom["p2"]), color, width, arrow=kind == "arrow")
                return self._done(i, "Arrow added." if kind == "arrow" else "Line added.")
            if kind == "textbox":
                text = (geom.get("text") or "").rstrip()
                if not text.strip():
                    raise UserError("Type some text first.")
                font = pe.FONTS.get(style.get("font") or "Helvetica", "helv")
                m.add_textbox(i, self._P(i, *geom["pt"]), pe._normalise(text), float(style.get("size") or 12), color, font)
                return self._done(i, "Text added.")
            if kind == "note":
                text = (geom.get("text") or "").strip()
                if not text:
                    raise UserError("Type a note first.")
                m.add_note(i, self._P(i, *geom["pt"]), text, hex_rgb(style.get("color"), (1, 0.8, 0)))
                return self._done(i, "Note added.")
            if kind == "image":
                path = Path(geom["path"])
                png, _ = picture_png(path)
                rect = self._image_rect(i, geom.get("rect"), geom.get("pt"), png)
                xref = self._add_stamp(i, rect, png, "Picture")
                return self._done(i, "Picture added. Click it with Select to move or resize it.", xref=xref)
            if kind == "link":
                uri = (geom.get("uri") or "").strip()
                if not uri:
                    raise UserError("Type the web address for the link.")
                if "@" in uri and "://" not in uri and not uri.startswith("mailto:"):
                    uri = "mailto:" + uri
                elif "://" not in uri and not uri.startswith("mailto:"):
                    uri = "https://" + uri
                m.add_link(i, self._R(i, geom["rect"]), uri)
                return self._done(i, "Link added.")
            if kind == "redact":
                rects = [self._R(i, r) for r in (geom.get("rects") or [geom["rect"]])]
                # "white": erased - the same removal, leaving blank paper where the black box would be
                fill = (1, 1, 1) if geom.get("white") else (0, 0, 0)
                gone = ("Erased - what was there is gone from the file, not just covered." if geom.get("white")
                        else "Removed for good - the hidden text is gone from the file, not just covered.")
                if geom.get("text"):              # selected words: only their letters, never the line above / below
                    parts = [text_redaction(m.page(i), r) for r in rects]
                    if all(parts):
                        m.redact_words(i, [b for bands, _ in parts for b in bands], [box for _, box in parts], fill)
                        return self._done(i, gone)
                m.redact_rects(i, rects, fill)
                return self._done(i, gone)
            if kind == "crop":
                m.crop(i, self._R(i, geom["rect"]))
                return self._done(i, f"Page {i + 1} cropped.")
            if kind == "signature":
                return self._place_signature(i, geom)
            if kind == "mark":
                r = geom.get("rect")
                if not r or abs(r[2] - r[0]) < 4 or abs(r[3] - r[1]) < 4:
                    x, y = geom.get("pt") or [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2]
                    s = float(style.get("size") or 14)
                    r = [x - s / 2, y - s / 2, x + s / 2, y + s / 2]
                else:                                  # dragged: a square inside the box
                    s = min(r[2] - r[0], r[3] - r[1]); cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                    r = [cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2]
                mk = geom.get("mark", "tick")
                xref = self._add_stamp(i, self._R(i, r), mark_png(mk, style.get("color") or "#111827"), "Mark")
                return self._done(i, {"tick": "Tick added.", "cross": "Cross added.", "dot": "Dot added."}.get(mk, "Added."),
                                  xref=xref)
            raise UserError("Unknown tool.")

    def _image_rect(self, i, rect, pt, data: bytes):
        """The dragged area, or (after a click) the picture at a sensible size centred on the click."""
        if rect and abs(rect[2] - rect[0]) > 6 and abs(rect[3] - rect[1]) > 6:
            return self._R(i, rect)
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        pg = self._model.page(i)
        scale = min(220 / w, 220 / h, (pg.rect.width * 0.8) / w, (pg.rect.height * 0.8) / h, 1)
        w, h = w * scale, h * scale
        x, y = pt or [(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2]
        return self._R(i, [x - w / 2, y - h / 2, x + w / 2, y + h / 2])

    def _add_stamp(self, i, rect, png: bytes, subject: str) -> int:
        """Snapshot + an image stamp annotation (moves and resizes later, prints with the page).
        png is the picture as it should be seen; on a turned page it's turned to match, so it shows upright."""
        m = self._model
        m.snapshot()
        pg = m.page(i)
        m._held_page = pg
        a = pg.add_stamp_annot(rect, stamp=turn_png(png, pg.rotation))
        a.set_info(title=brand.NAME, subject=subject, content="")
        a.set_flags(fitz.PDF_ANNOT_IS_PRINT)
        a.update()
        return a.xref

    def _slot_art(self, geom):
        slot = geom.get("slot") or "signature"
        sig = self._build_signature(geom["sig"]) if geom.get("sig") else load_signature(slot)
        if not sig:
            raise UserError("Set up your initials first." if slot == "initials" else "Set up your signature first.")
        return slot, signature_art(sig)

    def _place_signature(self, i, geom):
        slot, (png, aspect) = self._slot_art(geom)
        r = geom.get("rect")
        if not r or abs(r[2] - r[0]) < 8 or abs(r[3] - r[1]) < 8:      # a click: default size, centred
            x, y = geom.get("pt") or [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2]
            w = 60 if slot == "initials" else 160; h = w / aspect
            r = [x - w / 2, y - h / 2, x + w / 2, y + h / 2]
        xref = self._add_stamp(i, self._R(i, r), png, "Initials" if slot == "initials" else "Signature")
        what = "Initials" if slot == "initials" else "Signature"
        return self._done(i, f"{what} added. To move or resize, click it with Select.", xref=xref)

    @safe
    def place_on_every_page(self, i: int, xref: int):
        """Copy a placed signature / initials / mark to the same spot on every other page (one undo step)."""
        with self._lock:
            m = self._page_ok(i)
            a = self._annot(i, xref)
            if a.type[0] != fitz.PDF_ANNOT_STAMP:
                raise UserError("Only signatures, initials and marks can be copied to every page.")
            subject = a.info.get("subject") or "Initials"
            # the stamp as its page shows it (get_pixmap applies the page's turn); turned again to suit each other page
            png = a.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=True).tobytes("png")
            src = self._D(i, a.rect)
            p0 = m.page(i).rect
            m.snapshot()
            n = 0
            for k in range(len(m)):
                if k == i:
                    continue
                pk = m.page(k).rect
                sx, sy = pk.width / p0.width, pk.height / p0.height
                r = [src[0] * sx, src[1] * sy, src[0] * sx + (src[2] - src[0]), src[1] * sy + (src[3] - src[1])]
                pg = m.page(k)
                m._held_page = pg
                st = pg.add_stamp_annot(self._R(k, r), stamp=turn_png(png, pg.rotation))
                st.set_info(title=brand.NAME, subject=subject, content="")
                st.set_flags(fitz.PDF_ANNOT_IS_PRINT)
                st.update()
                n += 1
            what = STAMP_KINDS.get(subject, "item")
            return {**self._done(None, f"{what.capitalize()} added to {n} more page{'s' if n != 1 else ''}."), "changed": None}

    # ---- change existing annotations
    def _annot(self, i, xref):
        pg = self._model.page(i)
        self._model._held_page = pg
        a = next((a for a in pg.annots() if a.xref == int(xref)), None)
        if a is None:
            raise UserError("That item is no longer on the page.")
        return a

    @safe
    def annot_delete(self, i: int, xref: int):
        with self._lock:
            self._page_ok(i)
            a = self._annot(i, xref)
            name = ANNOT_NAMES.get(a.type[0], "item")
            self._model.delete_annot(i, SimpleNamespace(xref=int(xref)))
            return self._done(i, f"{name.capitalize()} deleted.")

    @safe
    def annot_text(self, i: int, xref: int, text: str):
        with self._lock:
            self._page_ok(i)
            self._annot(i, xref)
            self._model.set_annot_text(i, SimpleNamespace(xref=int(xref)), text)
            return self._done(i, "Text updated.")

    @safe
    def annot_move(self, i: int, xref: int, dx: float, dy: float):
        with self._lock:
            m = self._page_ok(i)
            a = self._annot(i, xref)
            if a.type[0] not in MOVABLE:
                raise UserError("This item can't be moved - delete it and add it again.")
            d = self._P(i, dx, dy) - self._P(i, 0, 0)
            m.snapshot()
            a = self._annot(i, xref)                 # re-fetch after snapshot
            a.set_rect(a.rect + (d.x, d.y, d.x, d.y))
            a.update()
            return self._done(i, "Moved.")

    @safe
    def annot_pickup(self, i: int, xref: int):
        """Start moving/resizing: returns the item's picture and hides it on the page (one undo step with annot_place)."""
        with self._lock:
            m = self._page_ok(i)
            a = self._annot(i, xref)
            if a.type[0] not in RESIZABLE:
                raise UserError("This item can't be resized - delete it and add it again.")
            png = a.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=True).tobytes("png")    # as the page shows it (turn applied)
            rect = self._D(i, a.rect)
            m.snapshot()
            a = self._annot(i, xref)
            a.set_flags(a.flags | fitz.PDF_ANNOT_IS_HIDDEN)
            a.update()
            return self._done(i, "Drag to move, pull a corner to resize.", image=data_url(png, "image/png"), rect=rect)

    @safe
    def annot_place(self, i: int, xref: int, rect: list):
        """Finish a move/resize started with annot_pickup."""
        with self._lock:
            self._page_ok(i)
            a = self._annot(i, xref)
            a.set_rect(self._R(i, rect))
            a.set_flags(a.flags & ~fitz.PDF_ANNOT_IS_HIDDEN)
            a.update()
            return self._done(i, "Moved and resized." if a.type[0] == fitz.PDF_ANNOT_STAMP else "Updated.")

    @safe
    def image_info(self, path: str):
        """Preview + shape of a picture before it's placed."""
        png, aspect = picture_png(Path(path), 900)
        return {"preview": data_url(png, "image/png"), "aspect": aspect}

    # ---- forms
    @safe
    def set_field(self, i: int, xref: int, value):
        with self._lock:
            m = self._page_ok(i)
            pg = m.page(i)
            w = next((w for w in pg.widgets() if w.xref == int(xref)), None)
            if w is None:
                raise UserError("That form box is no longer on the page.")
            if w.field_flags & 1:
                raise UserError("This box is locked by the form's author.")
            name = w.field_label or w.field_name or "the box"
            m.set_widget(i, w, value)
            return self._done(i, f"Filled in {name}." if not isinstance(value, bool) else
                              f"{'Ticked' if value else 'Unticked'} {name}.")

    # ---- edit existing text
    @safe
    def text_info(self, i: int, x: float, y: float):
        """The line (span) and paragraph (block) under a point, with the font that would be used to retype them."""
        with self._lock:
            m = self._page_ok(i)
            pt = self._P(i, x, y)
            span, blk = m.span_at(i, pt), m.block_at(i, pt)
            if not span and not blk:
                # words in a picture (a scan, a photo of a letter): bridge_scanedit changes those
                pg = m.page(i)
                return {"none": True, "scan": scanwords.picture_under(pg, pt * pg.rotation_matrix)}
            src = blk or span
            text = blk["text"] if blk else pe._normalise(span["text"])
            out = {"font": self._font_report(i, src.get("font", ""), src.get("flags", 0), text, src.get("fontxref"))}
            if span:
                out["span"] = {"text": pe._normalise(span["text"]).strip(), "bbox": self._D(i, span["bbox"]),
                               "size": round(span["size"], 2), "color": rgb_hex(span.get("color", 0))}
            if blk:
                out["block"] = {"text": blk["text"], "bbox": self._D(i, blk["bbox"]), "size": round(blk["size"], 2),
                                "align": blk["align"], "align_name": ALIGN_NAMES[blk["align"]],
                                "lineheight": round(blk["lineheight"], 2), "color": rgb_hex(blk["color"]),
                                "tracking": blk.get("tracking", 0.0)}
            return out

    def _font_report(self, i, font, flags, text, xref=None):
        name, buf, desc = self._model.resolve_font(i, font, flags, text, xref)
        flags = self._model.true_flags(i, font, flags, xref)
        kind = "embedded" if desc.startswith("original font") else "installed" if "installed" in desc else "builtin"
        fam = pe._font_family(font) or "unknown"
        if kind == "builtin" and fam.lower().replace("-", "").startswith(STANDARD_FONTS):
            kind = "standard"          # one of the 14 fonts every PDF viewer has: the built-in IS the original
        rep = {"family": fam, "desc": desc, "kind": kind, "fallback": CSS_FONT[_font_class(font, flags)],
               "bold": bool(flags & 16) or "bold" in font.lower(), "italic": bool(flags & 2) or "italic" in font.lower()}
        web = web_font(buf) if buf and len(buf) < FONT_PREVIEW_MAX else None
        if web:
            rep["url"] = data_url(web, "font/otf" if web[:4] == b"OTTO" else "font/ttf")
        return rep

    STAND_INS = {"helv": "Helvetica", "hebo": "Helvetica Bold", "heit": "Helvetica Italic", "hebi": "Helvetica Bold Italic",
                 "tiro": "Times", "tibo": "Times Bold", "tiit": "Times Italic", "tibi": "Times Bold Italic",
                 "cour": "Courier", "cobo": "Courier Bold", "coit": "Courier Italic", "cobi": "Courier Bold Italic"}

    def _stand_in(self, i, blk, text, desc):
        """When the page's own font can't write the new words - a PDF usually holds only the letters it uses -
        which font will, and the letters it hasn't got: {kind, name, missing} for the dialog, else None."""
        if desc.startswith("original font"):
            return None
        _, own, own_desc = self._model.resolve_font(i, blk.get("font", ""), blk.get("flags", 0), "", blk.get("fontxref"))
        if own is None or not own_desc.startswith("original font"):
            return None                            # the original wasn't usable to begin with: the chip says so already
        try:
            f = fitz.Font(fontbuffer=own)
            missing = sorted({c for c in pe._normalise(text) if not c.isspace() and not f.has_glyph(ord(c))})
        except Exception:
            missing = []
        code = desc.split(" ")[1] if desc.startswith("built-in ") else ""
        return {"kind": "installed" if "installed" in desc else "builtin",
                "name": self.STAND_INS.get(code) or desc.split(" (")[0], "missing": " ".join(missing[:8])}

    def _font_for(self, i, choice, src, text):
        if choice and choice != "original":
            if choice not in pe.FONTS:
                raise UserError("Unknown font.")
            return pe.FONTS[choice], None, choice
        return self._model.resolve_font(i, src.get("font", ""), src.get("flags", 0), pe._normalise(text), src.get("fontxref"))

    @safe
    def fix_text(self, i: int, x: float, y: float, text: str, size: float | None = None, font: str = "original"):
        with self._lock:
            m = self._page_ok(i)
            span = m.span_at(i, self._P(i, x, y))
            if not span:
                raise UserError("There's no text there any more.")
            name, buf, desc = self._font_for(i, font, span, text)
            m.whiteout_retype(i, span, text, float(size or span["size"]), fitz.sRGB_to_pdf(span.get("color", 0)),
                              fontname=name, fontbuffer=buf)
            return self._done(i, f"Line replaced using {desc}.")

    @safe
    def paragraph_fit(self, i: int, x: float, y: float, text: str, size: float, align: int | None = None,
                      font: str = "original"):
        """Dry run: will the new text fit the paragraph's box at this size?  -> {fits, used}"""
        with self._lock:
            m = self._page_ok(i)
            blk = m.block_at(i, self._P(i, x, y))
            if not blk:
                return {"fits": False, "used": None}
            name, buf, desc = self._font_for(i, font, blk, text)
            pg = m.page(i)
            base = fitz.Rect(blk["bbox"])
            size = float(size)
            lh = blk["lineheight"]
            turn = blk.get("turn", 0)
            used_font = self._stand_in(i, blk, text, desc) if font == "original" else None
            if not turn:                       # the same layout Replace uses (letter spacing, justified lines)
                try:
                    f = pe.font_object(name, buf)
                except Exception:
                    return {"fits": False, "used": None}
                al = blk["align"] if align is None else int(align)
                s = size
                while s >= 4:
                    laid = m.lay_out(blk, text, s, al, f, size, lh)
                    if laid:
                        return {"fits": abs(s - size) < 0.01, "used": round(s, 2), "spare_lines": laid[1], "font": used_font}
                    s -= 0.5
                return {"fits": False, "used": None, "font": used_font}
            box = pe.paragraph_box(base, size * lh * 0.6, turn)
            tmp = fitz.open()
            try:
                tp = tmp.new_page(width=pg.mediabox.width, height=pg.mediabox.height)
                if buf is not None:
                    tp.insert_font(fontname=name, fontbuffer=buf)
                s = size
                while s >= 4:
                    try:
                        rc = tp.insert_textbox(box, pe._normalise(text), fontsize=s, fontname=name, rotate=turn,
                                               align=blk["align"] if align is None else int(align), lineheight=lh)
                    except Exception:
                        return {"fits": False, "used": None}
                    if rc >= 0:
                        return {"fits": abs(s - size) < 0.01, "used": s, "spare_lines": int(rc // (s * lh)) if s else 0}
                    s -= 0.5
                return {"fits": False, "used": None}
            finally:
                tmp.close()

    @safe
    def edit_paragraph(self, i: int, x: float, y: float, text: str, size: float, align: int | None = None,
                       font: str = "original", keep_original: bool = False):
        with self._lock:
            m = self._page_ok(i)
            blk = m.block_at(i, self._P(i, x, y))
            if not blk:
                raise UserError("There's no paragraph there any more.")
            name, buf, desc = self._font_for(i, font, blk, text)
            used = m.replace_block(i, blk, text, float(size), fitz.sRGB_to_pdf(blk["color"]), fontname=name,
                                   fontbuffer=buf, align=None if align is None else int(align))
            if keep_original:          # part of the same undo step: no extra snapshot
                b = fitz.Rect(blk["bbox"])
                a = m.page(i).add_text_annot(fitz.Point(b.x1 + 2, b.y0), "Original wording:\n" + blk["text"], icon="Comment")
                a.set_colors(stroke=(1, 0.8, 0)); a.update()
            note = "" if abs(used - float(size)) < 0.01 else f" It was shrunk to {used:g} pt to fit."
            return self._done(i, f"Paragraph replaced using {desc}.{note}")

    # ---- the person's own signature (drawn / typed / picture)
    @safe
    def signature_get(self, slot: str = "signature"):
        sig = load_signature(slot)
        if not sig:
            return {"signature": None}
        try:
            png, aspect = signature_art(sig)
            prev = data_url(png, "image/png")
        except Exception:
            log.exception("signature preview")
            prev, aspect = None, 3
        name = sig.get("name") if sig["type"] == "typed" else None
        return {"signature": {"type": sig["type"], "colour": sig.get("colour", "#1a237e"), "name": name},
                "preview": prev, "aspect": aspect}

    @safe
    def signature_fonts(self):
        """Handwriting fonts installed on this PC, as data URLs so each style button can preview itself."""
        out = []
        for label, path in pe._script_fonts():
            p = Path(path)
            try:
                if p.stat().st_size > 2 << 20:
                    continue
                out.append({"label": label, "id": label, "url": data_url(p.read_bytes(), "font/ttf")})
            except OSError:
                continue
            if len(out) >= 8:
                break
        return {"fonts": out}

    @safe
    def signature_set(self, sig: dict, slot: str = "signature"):
        save_signature(self._build_signature(sig), slot)
        return self.signature_get(slot)

    def _build_signature(self, sig: dict) -> dict:
        """Validate a signature sent by the page (drawn / typed / picture) into the stored form."""
        t = (sig or {}).get("type")
        colour = sig.get("colour") or "#1a237e"
        if t == "ink":
            strokes = [[[min(1, max(0, float(x))), min(1, max(0, float(y)))] for x, y in s] for s in sig.get("strokes", []) if len(s) > 1]
            if not strokes:
                raise UserError("Draw your signature first.")
            data = {"type": "ink", "strokes": strokes, "colour": colour}
        elif t == "typed":
            name = (sig.get("name") or "").strip()
            if not name:
                raise UserError("Type your name first.")
            fonts = dict(pe._script_fonts())
            data = {"type": "typed", "name": name, "font": fonts.get(sig.get("font")), "colour": colour}
        elif t == "image":
            data = {"type": "image", "png": self._clean_signature_image(Path(sig["path"]))}
        else:
            raise UserError("Unknown signature type.")
        return data

    @safe
    def signature_preview(self, sig: dict):
        """Preview of an unsaved signature (for one-off use, or a picture before saving) -> {preview, aspect}"""
        png, aspect = signature_art(self._build_signature(sig))
        return {"preview": data_url(png, "image/png"), "aspect": aspect}

    @safe
    def signature_clear(self, slot: str = "signature"):
        f = slot_file(slot)
        if f.exists():
            f.unlink()
        return {"signature": None, "status": "Saved initials removed." if slot == "initials" else "Saved signature removed."}

    @staticmethod
    def _clean_signature_image(path: Path) -> str:
        """Photo/scan of a signature -> transparent PNG cropped to the ink (base64)."""
        import base64
        from PIL import Image, ImageOps
        try:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im).convert("RGBA")
        except Exception:
            raise UserError("That file isn't a picture we can read - try a PNG or JPG.")
        im.thumbnail((SIG_IMAGE_MAX, SIG_IMAGE_MAX))
        grey = im.convert("L")
        # paper -> transparent: anything close to the background brightness becomes clear
        hist = grey.histogram()
        bg = max(range(256), key=lambda v: hist[v])
        cut = max(60, bg - 60)
        alpha = grey.point(lambda v: 0 if v > cut else min(255, int((cut - v) * 255 / max(cut, 1)) + 80))
        im.putalpha(alpha)
        box = alpha.getbbox()
        if not box:
            raise UserError("No signature found in that picture - use dark ink on light paper.")
        im = im.crop(box)
        bio = io.BytesIO(); im.save(bio, "PNG")
        return base64.b64encode(bio.getvalue()).decode()
