"""
tools.py - the logic behind every Toolkit tool.  No GUI code in here, so each
function can also be used from the command line or another script.

Every tool follows the same shape:

    run(files, opts, log) -> list[Path]   # returns files it produced

* files  : list[Path] the user selected
* opts   : dict of option values from the window
* log    : callable(str) for progress lines

Third-party libraries used (installed by setup.bat): pypdf, Pillow, openpyxl, reportlab.
Optional extras: pillow-heif (iPhone HEIC photos), pytesseract + Tesseract (OCR), ffmpeg (video/audio).
"""

from __future__ import annotations

import csv
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
# programs shipped inside the built app (the Mac build bundles Tesseract and FFmpeg): <bundle>/bin, <bundle>/tessdata
BUNDLED = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else None
# a Mac app opened from Finder doesn't get Homebrew's folders on its PATH
MAC_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"] if IS_MAC else []
if BUNDLED and (BUNDLED / "tessdata").is_dir():
    os.environ.setdefault("TESSDATA_PREFIX", str(BUNDLED / "tessdata"))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}
OFFICE_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp", ".rtf", ".txt", ".csv"}
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


# ------------------------------------------------------------------ helpers

class ToolError(Exception):
    """Raised with a message the user should read (missing program, bad input)."""


def _run(cmd, timeout=600):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def _which(*names, extra=()):
    if BUNDLED:
        for n in names:
            exe = BUNDLED / "bin" / (n + (".exe" if IS_WIN else ""))
            if exe.is_file():
                return str(exe)
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for c in [*extra, *(Path(d) / n for d in MAC_DIRS for n in names)]:
        if Path(c).exists():
            return str(c)
    return None


def find_soffice():
    extra = []
    if IS_WIN:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if root:
                extra += [Path(root) / "LibreOffice/program/soffice.exe",
                          Path(root) / "Programs/LibreOffice/program/soffice.exe"]
    else:
        extra += ["/Applications/LibreOffice.app/Contents/MacOS/soffice",
                  "/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice", "/snap/bin/libreoffice"]
    return _which("soffice", "libreoffice", extra=extra)


def find_tesseract():
    extra = []
    if IS_WIN:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LOCALAPPDATA", "")):
            if root:
                extra += [Path(root) / "Tesseract-OCR/tesseract.exe",
                          Path(root) / "Programs/Tesseract-OCR/tesseract.exe"]
    return _which("tesseract", extra=extra)


def find_ffmpeg():
    return _which("ffmpeg")


def out_path(src: Path, opts, suffix="", ext=None, stem=None) -> Path:
    """Where to put a result: next to the source, or in opts['outdir'] if set."""
    folder = Path(opts["outdir"]) if opts.get("outdir") else src.parent
    folder.mkdir(parents=True, exist_ok=True)
    name = (stem or src.stem) + suffix + (ext if ext is not None else src.suffix)
    target = folder / name
    n = 1
    while target.exists() and target.resolve() != src.resolve():
        target = folder / f"{(stem or src.stem)}{suffix} ({n}){ext if ext is not None else src.suffix}"
        n += 1
    return target


def parse_ranges(text: str, n_pages: int) -> list[int]:
    """'1-3, 7, 10-' -> zero-based page indexes."""
    pages = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a = int(a) if a else 1
            b = int(b) if b else n_pages
        else:
            a = b = int(part)
        if a < 1 or b > n_pages or a > b:
            raise ToolError(f"Page range '{part}' is outside 1-{n_pages}")
        pages += range(a - 1, b)
    return pages


def _open_image(path: Path):
    from PIL import Image
    if path.suffix.lower() in {".heic", ".heif"}:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise ToolError("HEIC photos need the 'pillow-heif' package:  pip install pillow-heif")
    img = Image.open(path)
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)  # honour phone rotation
    except Exception:
        pass
    return img


# ------------------------------------------------------------------ PDF tools

def pdf_merge(files, opts, log):
    from pypdf import PdfWriter
    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
    if len(pdfs) < 2:
        raise ToolError("Select at least two PDF files.")
    w = PdfWriter()
    for f in pdfs:
        log(f"adding {f.name}")
        w.append(str(f))
    target = out_path(pdfs[0], opts, stem=opts.get("name") or "merged", ext=".pdf")
    with open(target, "wb") as fh:
        w.write(fh)
    log(f"-> {target}")
    return [target]


def pdf_split(files, opts, log):
    from pypdf import PdfReader, PdfWriter
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        r = PdfReader(str(f))
        n = len(r.pages)
        if opts.get("mode") == "Every page to its own file":
            folder = Path(opts["outdir"]) if opts.get("outdir") else f.parent / f"{f.stem}_pages"
            folder.mkdir(parents=True, exist_ok=True)
            for i, page in enumerate(r.pages, 1):
                w = PdfWriter(); w.add_page(page)
                t = folder / f"{f.stem}_p{i:03d}.pdf"
                with open(t, "wb") as fh:
                    w.write(fh)
                produced.append(t)
            log(f"{f.name}: {n} pages -> {folder}")
        else:
            idx = parse_ranges(opts.get("ranges", "") or "", n)
            if not idx:
                raise ToolError("Enter the pages to extract, e.g.  1-3, 7, 10-")
            w = PdfWriter()
            for i in idx:
                w.add_page(r.pages[i])
            t = out_path(f, opts, suffix="_extract", ext=".pdf")
            with open(t, "wb") as fh:
                w.write(fh)
            produced.append(t)
            log(f"{f.name}: {len(idx)} page(s) -> {t.name}")
    return produced


def pdf_compress(files, opts, log):
    from pypdf import PdfReader, PdfWriter
    quality = int(opts.get("quality", 60))
    max_px = int(opts.get("max_px", 1600))
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        r = PdfReader(str(f))
        w = PdfWriter(clone_from=r)
        n_img = 0
        for page in w.pages:
            for img in page.images:
                try:
                    pil = img.image
                    if pil is None:
                        continue
                    if max(pil.size) > max_px:
                        pil.thumbnail((max_px, max_px))
                    if pil.mode not in ("RGB", "L"):
                        pil = pil.convert("RGB")
                    img.replace(pil, quality=quality)
                    n_img += 1
                except Exception:
                    continue
            page.compress_content_streams()
        w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        t = out_path(f, opts, suffix="_small", ext=".pdf")
        with open(t, "wb") as fh:
            w.write(fh)
        before, after = f.stat().st_size, t.stat().st_size
        log(f"{f.name}: {before/1e6:.2f} MB -> {after/1e6:.2f} MB  ({n_img} images recompressed)")
        produced.append(t)
    return produced


def _overlay_pdf(width, height, draw):
    """Build a one-page PDF (as pypdf page) of the given size, drawn by draw(canvas)."""
    from io import BytesIO
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    draw(c)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


def pdf_watermark(files, opts, log):
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.colors import Color
    text = (opts.get("text") or "DRAFT").strip()
    size = int(opts.get("size", 60))
    opacity = float(opts.get("opacity", 0.15))
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        w = PdfWriter(clone_from=PdfReader(str(f)))
        for page in w.pages:
            pw, ph = float(page.mediabox.width), float(page.mediabox.height)

            def draw(c, pw=pw, ph=ph):
                c.saveState()
                c.setFillColor(Color(0.5, 0.5, 0.5, alpha=opacity))
                c.setFont("Helvetica-Bold", size)
                c.translate(pw / 2, ph / 2)
                c.rotate(45)
                c.drawCentredString(0, 0, text)
                c.restoreState()
            page.merge_page(_overlay_pdf(pw, ph, draw))
        t = out_path(f, opts, suffix="_watermarked", ext=".pdf")
        with open(t, "wb") as fh:
            w.write(fh)
        log(f"{f.name} -> {t.name}")
        produced.append(t)
    return produced


# ---- taking a watermark back off
_SHOWS = (b"Tj", b"TJ", b"'", b'"')


def _wm_blocks(ops, page):
    """The q...Q groups in a page's drawing instructions that look like a stamp laid over the page, smallest first:
    a few instructions that write a word or two (or draw one picture), see-through and/or written on a slant.
    -> [(start, end, signature, strong, kind, letter size)]"""
    alphas = {}
    try:
        for name, gs in (page["/Resources"].get("/ExtGState") or {}).items():
            gs = gs.get_object()
            alphas[str(name)] = min(float(gs.get("/ca", 1)), float(gs.get("/CA", 1)))
    except Exception:
        pass
    xobjects = {}
    try:
        for name, x in (page["/Resources"].get("/XObject") or {}).items():
            x = x.get_object()
            xobjects[str(name)] = (str(x.get("/Subtype")), str(x.get("/BBox") or (x.get("/Width"), x.get("/Height"))))
    except Exception:
        pass
    found, stack = [], []
    for k, (operands, op) in enumerate(ops):
        if op == b"q":
            stack.append(k)
        elif op == b"Q" and stack:
            start = stack.pop()
            if k - start > 60:
                continue
            shows, dos, alpha, slant, size = [], [], 1.0, False, 0.0
            for operands2, op2 in ops[start:k + 1]:
                if op2 in _SHOWS:
                    shows.append(repr(operands2[-1] if operands2 else ""))
                elif op2 == b"Do" and operands2:
                    dos.append(xobjects.get(str(operands2[0]), ("?", str(operands2[0]))))
                elif op2 == b"gs" and operands2:
                    alpha = min(alpha, alphas.get(str(operands2[0]), 1.0))
                elif op2 in (b"cm", b"Tm") and len(operands2) == 6:
                    try:
                        a, b2 = abs(float(operands2[0])), abs(float(operands2[1]))
                    except (TypeError, ValueError):
                        continue
                    hyp = (a * a + b2 * b2) ** 0.5
                    if hyp and a > 0.2 * hyp and b2 > 0.2 * hyp:
                        slant = True
                elif op2 == b"Tf" and len(operands2) == 2:
                    try:
                        size = max(size, float(operands2[1]))
                    except (TypeError, ValueError):
                        pass
            if not (shows or dos) or len(shows) > 4 or len(dos) > 2 or (shows and dos):
                continue
            if alpha >= 1 and not slant:
                continue
            kind = "words" if shows else "picture"
            found.append((start, k, (kind, tuple(shows), tuple(dos)), alpha < 1 and slant, kind, size))
    found.sort(key=lambda b: b[1] - b[0])
    return found


def _wm_artifacts(ops, page):
    """Stretches marked as a watermark by the program that added it (Word, Acrobat): /Artifact ... /Watermark BDC ... EMC."""
    props = {}
    try:
        props = page["/Resources"].get("/Properties") or {}
    except Exception:
        pass
    out, k = [], 0
    while k < len(ops):
        operands, op = ops[k]
        if op == b"BDC" and len(operands) == 2 and str(operands[0]) == "/Artifact":
            d = operands[1]
            try:
                d = props[d].get_object() if not hasattr(d, "get") else d
                marked = str(d.get("/Subtype")) == "/Watermark"
            except Exception:
                marked = False
            if marked:
                depth, j = 1, k + 1
                while j < len(ops) and depth:
                    if ops[j][1] in (b"BDC", b"BMC"):
                        depth += 1
                    elif ops[j][1] == b"EMC":
                        depth -= 1
                    j += 1
                if not depth:
                    out.append((k, j - 1))
                    k = j
                    continue
        k += 1
    return out


def pdf_remove_watermark(files, opts, log):
    """Take a stamp such as DRAFT or COPY back off a PDF: the watermark's own drawing instructions are cut out of
    each page, so the words under it stay as they were.  Found three ways: marked as a watermark by the program that
    added it; a watermark comment; or a see-through and/or slanted word (or picture) laid over the pages - on most
    pages of the file, or unmistakable (see-through AND slanted; or large).  A watermark that is part of a scanned
    picture can't be taken out, and the file is left alone with a message saying so."""
    from math import ceil
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, ContentStream
    produced, nothing = [], []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        w = PdfWriter(clone_from=PdfReader(str(f)))
        pages = list(w.pages)
        parsed = []
        for page in pages:
            try:
                contents = page.get_contents()
                cs = ContentStream(contents, w) if contents is not None else None
            except Exception:
                cs = None
            ops = list(cs.operations) if cs is not None else []
            parsed.append((cs, ops, _wm_blocks(ops, page) if ops else [], _wm_artifacts(ops, page) if ops else []))
        seen = {}
        for _, _, blocks, _ in parsed:
            for sig in {b[2] for b in blocks}:
                seen[sig] = seen.get(sig, 0) + 1
        need = max(2, ceil(0.6 * len(pages)))
        changed = 0
        for page, (cs, ops, blocks, artifacts) in zip(pages, parsed):
            cut = list(artifacts)
            for start, end, sig, strong, kind, size in blocks:
                often = len(pages) > 1 and seen.get(sig, 0) >= need
                sure = strong if kind == "words" else (strong and often)
                if kind == "words" and not sure and not often and size >= 36:
                    sure = True                                  # one big slanted or see-through word on a page
                if not (often or sure) or (kind == "picture" and not often and len(pages) > 1):
                    continue
                if any(s <= start and end <= e or start <= s and e <= end for s, e in cut):
                    continue
                cut.append((start, end))
            marks = 0
            if "/Annots" in page:
                keep = ArrayObject()
                for a in page["/Annots"]:
                    try:
                        if str(a.get_object().get("/Subtype")) == "/Watermark":
                            marks += 1
                            continue
                    except Exception:
                        pass
                    keep.append(a)
                if marks:
                    page[page_key("/Annots")] = keep
            if cut:
                gone = set()
                for s, e in cut:
                    gone.update(range(s, e + 1))
                cs.operations = [o for k, o in enumerate(ops) if k not in gone]
                page.replace_contents(cs)
            if cut or marks:
                changed += 1
        if not changed:
            nothing.append(f.name)
            log(f"{f.name}: no watermark that can be taken off was found")
            continue
        t = out_path(f, opts, suffix="_no_watermark", ext=".pdf")
        with open(t, "wb") as fh:
            w.write(fh)
        log(f"{f.name} -> {t.name} (taken off {changed} page{'s' if changed != 1 else ''})")
        produced.append(t)
    if not produced:
        which = nothing[0] if len(nothing) == 1 else "these files"
        raise ToolError(f"No watermark that can be taken off was found in {which}. If the pages are scans or photos, "
                        "the watermark is part of the picture and can't be removed without damaging the page.")
    return produced


def page_key(name: str):
    from pypdf.generic import NameObject
    return NameObject(name)


def pdf_page_numbers(files, opts, log):
    from pypdf import PdfReader, PdfWriter
    fmt = opts.get("format") or "Page {n} of {total}"
    pos = opts.get("position") or "Bottom centre"
    size = int(opts.get("size", 10))
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        w = PdfWriter(clone_from=PdfReader(str(f)))
        total = len(w.pages)
        for i, page in enumerate(w.pages, 1):
            pw, ph = float(page.mediabox.width), float(page.mediabox.height)
            label = fmt.replace("{n}", str(i)).replace("{total}", str(total))

            def draw(c, pw=pw, ph=ph, label=label):
                c.setFont("Helvetica", size)
                y = ph - 30 if pos.startswith("Top") else 20
                if pos.endswith("left"):
                    c.drawString(36, y, label)
                elif pos.endswith("right"):
                    c.drawRightString(pw - 36, y, label)
                else:
                    c.drawCentredString(pw / 2, y, label)
            page.merge_page(_overlay_pdf(pw, ph, draw))
        t = out_path(f, opts, suffix="_numbered", ext=".pdf")
        with open(t, "wb") as fh:
            w.write(fh)
        log(f"{f.name}: {total} pages numbered -> {t.name}")
        produced.append(t)
    return produced


def pdf_to_office(files, opts, log):
    """PDF -> Word/Excel/PowerPoint: Microsoft Word for .docx when it's installed, otherwise LibreOffice.
    Works for text PDFs, not scans."""
    import msoffice
    soffice = find_soffice()
    fmt = opts.get("format") or "Word (.docx)"
    if fmt.startswith("Word") and msoffice.available("word"):
        produced = []
        for f in (x for x in files if x.suffix.lower() == ".pdf"):
            t = out_path(f, opts, ext=".docx")
            try:
                msoffice.pdf_to_docx(f, t)
            except msoffice.OfficeError as e:
                if not soffice:
                    log(f"FAILED {f.name}: {e}")
                    continue
                produced += pdf_to_office([f], dict(opts, _no_word=True), log)
                continue
            log(f"{f.name} -> {t.name} (Word)")
            produced.append(t)
        if not opts.get("_no_word"):
            return produced
    if not soffice:
        raise ToolError("Turning PDFs into Excel or PowerPoint files needs the free LibreOffice add-on: "
                        "https://www.libreoffice.org/download/")
    target_fmt = {"Word (.docx)": ("docx", "writer_pdf_import", "docx:MS Word 2007 XML"),
                  "Excel (.xlsx)": ("xlsx", "calc_pdf_import", "xlsx:Calc MS Excel 2007 XML"),
                  "PowerPoint (.pptx)": ("pptx", "impress_pdf_import", "pptx:Impress MS PowerPoint 2007 XML")}
    ext, infilter, conv = target_fmt.get(opts.get("format") or "Word (.docx)", target_fmt["Word (.docx)"])
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run([soffice, "--headless", "--norestore", f"--infilter={infilter}",
                      "--convert-to", conv, "--outdir", tmp, str(f)])
            made = Path(tmp) / f"{f.stem}.{ext}"
            if r.returncode != 0 or not made.exists():
                log(f"FAILED {f.name}: {(r.stderr or r.stdout).strip()[-200:]}")
                continue
            t = out_path(f, opts, ext=f".{ext}")
            shutil.move(str(made), str(t))
        log(f"{f.name} -> {t.name}")
        produced.append(t)
    return produced


def office_to_pdf(files, opts, log):
    """Word/Excel/PowerPoint/text -> PDF: Microsoft Office when it's installed (closest to the original), else LibreOffice."""
    import msoffice
    soffice = find_soffice()
    produced = []
    for f in (x for x in files if x.suffix.lower() in OFFICE_EXTS):
        kind = msoffice.kind_of(f)
        if msoffice.available(kind):
            t = out_path(f, opts, ext=".pdf")
            try:
                msoffice.to_pdf(f, t, kind)
                log(f"{f.name} -> {t.name} ({msoffice.LABEL[kind]})")
                produced.append(t)
                continue
            except msoffice.OfficeError as e:
                if not soffice or "password-protected" in str(e):
                    log(f"FAILED {f.name}: {e}")
                    continue
        if not soffice:
            raise ToolError("Converting this file needs Microsoft Office or the free LibreOffice add-on: "
                            "https://www.libreoffice.org/download/")
        with tempfile.TemporaryDirectory() as tmp:
            r = _run([soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmp, str(f)])
            made = Path(tmp) / f"{f.stem}.pdf"
            if r.returncode != 0 or not made.exists():
                log(f"FAILED {f.name}: {(r.stderr or r.stdout).strip()[-200:]}")
                continue
            t = out_path(f, opts, ext=".pdf")
            shutil.move(str(made), str(t))
        log(f"{f.name} -> {t.name}")
        produced.append(t)
    return produced


# ---------------------------------------------------------------- image tools

def image_convert(files, opts, log):
    from PIL import Image
    fmt = (opts.get("format") or "JPG").upper()
    ext = {"JPG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif"}[fmt]
    pil_fmt = {"JPG": "JPEG", "TIFF": "TIFF"}.get(fmt, fmt)
    max_px = int(opts.get("max_px") or 0)
    quality = int(opts.get("quality", 85))
    produced = []
    for f in (x for x in files if x.suffix.lower() in IMAGE_EXTS):
        img = _open_image(f)
        if max_px and max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)
        if pil_fmt == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        t = out_path(f, opts, ext=ext)
        save_kw = {"quality": quality, "optimize": True} if pil_fmt in ("JPEG", "WEBP") else {}
        img.save(t, pil_fmt, **save_kw)
        log(f"{f.name} -> {t.name}  ({img.size[0]}x{img.size[1]})")
        produced.append(t)
    return produced


def images_to_pdf(files, opts, log):
    imgs = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
    if not imgs:
        raise ToolError("Select some images.")
    pages = []
    for f in imgs:
        img = _open_image(f)
        if img.mode != "RGB":
            img = img.convert("RGB")
        pages.append(img)
        log(f"page {len(pages)}: {f.name}")
    target = out_path(imgs[0], opts, stem=opts.get("name") or "images", ext=".pdf")
    pages[0].save(target, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    log(f"-> {target}")
    return [target]


def image_compress(files, opts, log):
    """Shrink photos for email: resize and re-save as JPG at a chosen quality."""
    opts = dict(opts, format="JPG")
    return image_convert(files, opts, log)


# ----------------------------------------------------------------- rename

def rename_preview(files, opts):
    """Return [(old, new)] without touching anything."""
    mode = opts.get("mode") or "Add prefix"
    text = opts.get("text") or ""
    repl = opts.get("replace") or ""
    start = int(opts.get("start") or 1)
    pairs = []
    for i, f in enumerate(sorted(files, key=lambda p: p.name.lower())):
        stem, ext = f.stem, f.suffix
        if mode == "Add prefix":
            new = f"{text}{stem}{ext}"
        elif mode == "Add suffix":
            new = f"{stem}{text}{ext}"
        elif mode == "Replace text":
            new = f"{stem.replace(text, repl)}{ext}" if text else f.name
        elif mode == "Number sequentially":
            new = f"{text}{start + i:03d}{ext}"
        elif mode == "Prefix with file date":
            d = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
            new = f"{d} {stem}{ext}"
        elif mode == "Lowercase":
            new = f.name.lower()
        elif mode == "Spaces to underscores":
            new = f.name.replace(" ", "_")
        else:
            new = f.name
        pairs.append((f, f.parent / new))
    return pairs


def bulk_rename(files, opts, log):
    pairs = rename_preview(files, opts)
    seen = set()
    done = []
    for old, new in pairs:
        if old == new:
            continue
        if new.exists() or new in seen:
            log(f"skip {old.name}: '{new.name}' already exists")
            continue
        old.rename(new)
        seen.add(new)
        log(f"{old.name}  ->  {new.name}")
        done.append(new)
    return done


# ------------------------------------------------------------- spreadsheets

def excel_to_csv(files, opts, log):
    import openpyxl
    produced = []
    for f in (x for x in files if x.suffix.lower() in {".xlsx", ".xlsm"}):
        wb = openpyxl.load_workbook(str(f), read_only=True, data_only=True)
        for ws in wb.worksheets:
            suffix = f"_{ws.title}" if len(wb.worksheets) > 1 else ""
            safe = re.sub(r"[^\w\- ]", "_", suffix)
            t = out_path(f, opts, suffix=safe, ext=".csv")
            with open(t, "w", newline="", encoding="utf-8-sig") as fh:
                wr = csv.writer(fh)
                for row in ws.iter_rows(values_only=True):
                    wr.writerow(["" if v is None else v for v in row])
            log(f"{f.name} [{ws.title}] -> {t.name}")
            produced.append(t)
    return produced


def csv_to_excel(files, opts, log):
    import openpyxl
    csvs = [f for f in files if f.suffix.lower() in {".csv", ".tsv"}]
    if not csvs:
        raise ToolError("Select CSV files.")
    combine = opts.get("combine") in (True, "1", 1)
    produced = []

    def fill(ws, f):
        with open(f, newline="", encoding="utf-8-sig") as fh:
            sample = fh.read(4096); fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            for row in csv.reader(fh, dialect):
                ws.append([_coerce(v) for v in row])

    if combine:
        wb = openpyxl.Workbook(); wb.remove(wb.active)
        for f in csvs:
            ws = wb.create_sheet(title=f.stem[:31])
            fill(ws, f); log(f"sheet {ws.title}")
        t = out_path(csvs[0], opts, stem=opts.get("name") or "combined", ext=".xlsx")
        wb.save(t); produced.append(t); log(f"-> {t}")
    else:
        for f in csvs:
            wb = openpyxl.Workbook(); fill(wb.active, f)
            t = out_path(f, opts, ext=".xlsx"); wb.save(t)
            produced.append(t); log(f"{f.name} -> {t.name}")
    return produced


def _coerce(v: str):
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            return v
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return v


# --------------------------------------------------------------------- OCR

def ocr(files, opts, log):
    tess = find_tesseract()
    if not tess:
        if IS_MAC:
            raise ToolError("OCR needs Tesseract. Install it with Homebrew:  brew install tesseract   then try again.")
        raise ToolError("OCR needs Tesseract. Install with:  winget install UB-Mannheim.TesseractOCR\n"
                        "(or https://github.com/UB-Mannheim/tesseract/wiki) then restart Toolkit.")
    lang = opts.get("lang") or "eng"
    mode = opts.get("output") or "Text file (.txt)"
    produced = []
    for f in files:
        ext = f.suffix.lower()
        if ext == ".pdf":
            ocrmypdf = shutil.which("ocrmypdf")
            if not ocrmypdf:
                log(f"skip {f.name}: PDF OCR needs 'ocrmypdf' (pip install ocrmypdf) - images work now.")
                continue
            t = out_path(f, opts, suffix="_ocr", ext=".pdf")
            r = _run([ocrmypdf, "-l", lang, "--skip-text", str(f), str(t)])
            log(f"{f.name} -> {t.name}" if r.returncode == 0 else f"FAILED {f.name}: {r.stderr[-200:]}")
            if r.returncode == 0:
                produced.append(t)
            continue
        if ext not in IMAGE_EXTS:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            src = f
            if ext in {".heic", ".heif", ".webp", ".gif"}:
                src = Path(tmp) / "img.png"; _open_image(f).save(src)
            base = Path(tmp) / "out"
            if mode.startswith("Searchable"):
                r = _run([tess, str(src), str(base), "-l", lang, "pdf"])
                made, t = base.with_suffix(".pdf"), out_path(f, opts, suffix="_ocr", ext=".pdf")
            else:
                r = _run([tess, str(src), str(base), "-l", lang])
                made, t = base.with_suffix(".txt"), out_path(f, opts, ext=".txt")
            if r.returncode != 0 or not made.exists():
                log(f"FAILED {f.name}: {r.stderr.strip()[-200:]}"); continue
            shutil.move(str(made), str(t))
        log(f"{f.name} -> {t.name}")
        produced.append(t)
    return produced


# ------------------------------------------------------------- housekeeping

TIDY_CATEGORIES = {
    "Images": IMAGE_EXTS | {".svg", ".ico"},
    "Documents": OFFICE_EXTS | {".pdf", ".md", ".epub"},
    "Spreadsheets": {".xlsx", ".xls", ".csv", ".ods"},
    "Installers": {".exe", ".msi", ".msix", ".dmg", ".pkg", ".deb", ".appx"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"},
    "Audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "Video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".sql", ".sh", ".bat", ".ps1"},
}


def tidy_folder(files, opts, log):
    folder = Path(opts.get("folder") or (Path.home() / "Downloads"))
    if not folder.is_dir():
        raise ToolError(f"Folder not found: {folder}")
    dry = opts.get("dry_run") in (True, "1", 1)
    older_days = int(opts.get("older_days") or 0)
    moved = []
    cutoff = datetime.now().timestamp() - older_days * 86400
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.name.startswith(".") or f.suffix.lower() in {".crdownload", ".part", ".tmp"}:
            continue
        if older_days and f.stat().st_mtime > cutoff:
            continue
        cat = next((c for c, exts in TIDY_CATEGORIES.items() if f.suffix.lower() in exts), "Other")
        dest = folder / cat / f.name
        n = 1
        while dest.exists():
            dest = folder / cat / f"{f.stem} ({n}){f.suffix}"; n += 1
        log(f"{'[preview] ' if dry else ''}{f.name}  ->  {cat}/{dest.name}")
        if not dry:
            dest.parent.mkdir(exist_ok=True)
            shutil.move(str(f), str(dest))
            moved.append(dest)
    log("Preview only - nothing moved. Untick 'Preview' to apply." if dry else f"{len(moved)} file(s) moved.")
    return moved


def _hash(path: Path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def find_duplicates(files, opts, log):
    """Report groups of identical files; writes a report and (optionally) moves extras to a '_duplicates' folder."""
    folders = [f for f in files if f.is_dir()] or ([Path(opts["folder"])] if opts.get("folder") else [])
    if not folders:
        raise ToolError("Choose one or more folders to scan.")
    by_size: dict[int, list[Path]] = {}
    for d in folders:
        for f in d.rglob("*"):
            if f.is_file() and "_duplicates" not in f.parts:
                by_size.setdefault(f.stat().st_size, []).append(f)
    groups = []
    for size, same in by_size.items():
        if len(same) < 2 or size == 0:
            continue
        by_hash: dict[str, list[Path]] = {}
        for f in same:
            by_hash.setdefault(_hash(f), []).append(f)
        groups += [g for g in by_hash.values() if len(g) > 1]
    report = folders[0] / "duplicates_report.txt"
    wasted = 0
    with open(report, "w", encoding="utf-8") as fh:
        for g in groups:
            g.sort(key=lambda p: (len(str(p)), str(p)))
            wasted += g[0].stat().st_size * (len(g) - 1)
            fh.write("KEEP   " + str(g[0]) + "\n")
            for extra in g[1:]:
                fh.write("DUP    " + str(extra) + "\n")
            fh.write("\n")
    log(f"{len(groups)} duplicate group(s), {wasted/1e6:.1f} MB recoverable. Report: {report}")
    if opts.get("move") in (True, "1", 1) and groups:
        dest_root = folders[0] / "_duplicates"
        for g in groups:
            for extra in g[1:]:
                dest = dest_root / extra.relative_to(folders[0]) if extra.is_relative_to(folders[0]) else dest_root / extra.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(extra), str(dest))
        log(f"Extras moved to {dest_root} - review, then delete that folder when happy.")
    return [report]


# ------------------------------------------------------------------- text

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid",
                   "mc_cid", "mc_eid", "igshid", "ref", "ref_src", "si", "feature", "spm", "_hsenc", "_hsmi"}


def clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.scheme:
        return url
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


def transform_text(text: str, mode: str) -> str:
    if mode == "Clean URL (strip tracking)":
        return "\n".join(clean_url(line) for line in text.splitlines())
    if mode == "Plain text (strip formatting/extra spaces)":
        return re.sub(r"[ \t]+", " ", re.sub(r"\r\n?", "\n", text)).strip()
    if mode == "UPPER CASE":
        return text.upper()
    if mode == "lower case":
        return text.lower()
    if mode == "Title Case":
        return text.title()
    if mode == "Sentence case":
        return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text.lower())
    if mode == "Remove line breaks":
        return " ".join(text.split())
    if mode == "Remove blank lines":
        return "\n".join(l for l in text.splitlines() if l.strip())
    if mode == "Sort lines":
        return "\n".join(sorted(text.splitlines(), key=str.lower))
    if mode == "Remove duplicate lines":
        seen, out = set(), []
        for l in text.splitlines():
            if l not in seen:
                seen.add(l); out.append(l)
        return "\n".join(out)
    if mode == "Count words":
        return f"{len(text.split())} words, {len(text)} characters, {len(text.splitlines())} lines"
    return text


def make_qr(text: str, target: Path, size_px: int = 600) -> Path:
    from PIL import Image, ImageDraw
    from reportlab.graphics.barcode import qrencoder
    code = qrencoder.QRCode(None, qrencoder.QRErrorCorrectLevel.M)
    code.addData(text)
    code.make()
    n = code.getModuleCount()
    quiet = 4
    cell = max(1, size_px // (n + 2 * quiet))
    px = cell * (n + 2 * quiet)
    img = Image.new("L", (px, px), 255)
    draw = ImageDraw.Draw(img)
    for r in range(n):
        for c in range(n):
            if code.isDark(r, c):
                x, y = (c + quiet) * cell, (r + quiet) * cell
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=0)
    img.save(target, "PNG")
    return target


def qr_code(files, opts, log):
    text = (opts.get("text") or "").strip()
    produced = []
    if text:
        folder = Path(opts.get("outdir") or Path.home() / "Desktop")
        folder.mkdir(parents=True, exist_ok=True)
        t = folder / f"qr_{re.sub(r'[^A-Za-z0-9]+', '_', text)[:30].strip('_') or 'code'}.png"
        make_qr(text, t, int(opts.get("size") or 600))
        log(f"-> {t}"); produced.append(t)
    for f in (x for x in files if x.suffix.lower() == ".txt"):
        content = f.read_text(encoding="utf-8", errors="ignore").strip()
        if content:
            t = out_path(f, opts, suffix="_qr", ext=".png")
            make_qr(content, t, int(opts.get("size") or 600)); log(f"{f.name} -> {t.name}"); produced.append(t)
    if not produced:
        raise ToolError("Type some text or a URL, or select .txt files.")
    return produced


# --------------------------------------------------------------- archives

def zip_files(files, opts, log):
    if not files:
        raise ToolError("Select files or folders to zip.")
    password = opts.get("password") or ""
    name = opts.get("name") or (files[0].stem if len(files) == 1 else "archive")
    target = out_path(files[0], opts, stem=name, ext=".zip")
    if password:
        try:
            import pyzipper
        except ImportError:
            raise ToolError("Password-protected zips need:  pip install pyzipper")
        zf = pyzipper.AESZipFile(target, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES)
        zf.setpassword(password.encode())
    else:
        zf = zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED)
    with zf:
        for f in files:
            if f.is_dir():
                for p in f.rglob("*"):
                    if p.is_file():
                        zf.write(p, Path(f.name) / p.relative_to(f))
            else:
                zf.write(f, f.name)
            log(f"added {f.name}")
    log(f"-> {target}")
    return [target]


def unzip_files(files, opts, log):
    password = (opts.get("password") or "").encode() or None
    produced = []
    for f in (x for x in files if x.suffix.lower() == ".zip"):
        dest = Path(opts["outdir"]) / f.stem if opts.get("outdir") else f.parent / f.stem
        dest.mkdir(parents=True, exist_ok=True)
        try:
            import pyzipper
            opener = pyzipper.AESZipFile
        except ImportError:
            opener = zipfile.ZipFile
        import safety
        got = []
        try:
            with opener(f) as zf:
                if password:
                    zf.setpassword(password)
                for member in zf.infolist():
                    # guard against zip-slip paths
                    mp = Path(member.filename)
                    if mp.is_absolute() or ".." in mp.parts:
                        continue
                    got.append(Path(zf.extract(member, dest)))
        finally:
            safety.keep_mark(f, got)      # also when it stops half way: what is out is marked
        log(f"{f.name} -> {dest}")
        produced.append(dest)
    return produced


# ------------------------------------------------------------------ media

def _ffmpeg():
    ff = find_ffmpeg()
    if not ff:
        if IS_MAC:
            raise ToolError("Needs FFmpeg. Install it with Homebrew:  brew install ffmpeg   then try again.")
        raise ToolError("Needs ffmpeg. Install with:  winget install Gyan.FFmpeg   then restart Toolkit.")
    return ff


def _hms(t: str) -> str:
    t = t.strip()
    if not t:
        return ""
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return t
    if re.fullmatch(r"(\d+:)?\d{1,2}:\d{1,2}(\.\d+)?", t):
        return t
    raise ToolError(f"Time '{t}' should look like 1:23 or 0:01:23 or 83 (seconds)")


FF_LOCAL = ["-protocol_whitelist", "file"]      # a "video" can be a playlist of web addresses: files only


def media_convert(files, opts, log):
    ff = _ffmpeg()
    target = opts.get("format") or "MP4 (video)"
    produced = []
    for f in (x for x in files if x.suffix.lower() in MEDIA_EXTS):
        if target.startswith("MP4"):
            t = out_path(f, opts, ext=".mp4")
            args = ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-movflags", "+faststart"]
        elif target.startswith("MP3"):
            t = out_path(f, opts, ext=".mp3"); args = ["-vn", "-c:a", "libmp3lame", "-q:a", "2"]
        elif target.startswith("WAV"):
            t = out_path(f, opts, ext=".wav"); args = ["-vn"]
        elif target.startswith("GIF"):
            t = out_path(f, opts, ext=".gif")
            args = ["-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0"]
        else:
            t = out_path(f, opts, ext=".m4a"); args = ["-vn", "-c:a", "aac", "-b:a", "192k"]
        log(f"converting {f.name} ...")
        r = _run([ff, "-y", "-hide_banner", "-loglevel", "error", *FF_LOCAL, "-i", str(f), *args, str(t)], timeout=3600)
        if r.returncode != 0:
            log(f"FAILED {f.name}: {r.stderr.strip()[-300:]}"); continue
        log(f"-> {t.name}"); produced.append(t)
    return produced


def media_trim(files, opts, log):
    ff = _ffmpeg()
    start, end = _hms(opts.get("start") or ""), _hms(opts.get("end") or "")
    if not start and not end:
        raise ToolError("Enter a start and/or end time, e.g. 0:30 and 2:15")
    produced = []
    for f in (x for x in files if x.suffix.lower() in MEDIA_EXTS):
        t = out_path(f, opts, suffix="_trimmed")
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error"]
        if start:
            cmd += ["-ss", start]
        if end:
            cmd += ["-to", end]
        cmd += [*FF_LOCAL, "-i", str(f), "-c", "copy", str(t)]
        r = _run(cmd, timeout=3600)
        if r.returncode != 0:
            log(f"FAILED {f.name}: {r.stderr.strip()[-300:]}"); continue
        log(f"{f.name} -> {t.name}"); produced.append(t)
    return produced
