"""Open Word, Excel, PowerPoint, text files, photos and e-books in the editor by making a PDF copy.

    to_pdf(src, cache_dir) -> {"pdf": Path, "kind": "Word document", "via": "word|libreoffice|text|image|mupdf",
                               "text_only": bool}

Office files go through Microsoft Office when it's installed (best match to how the file really looks), then
LibreOffice, then - for .docx/.xlsx/.pptx/.odt and text files - a plain copy of the words, so Ask still works on a PC
with neither.  Copies are cached by path + size + modified time, so reopening a file is instant.  The original file
is only ever read.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import msoffice
import tools
from bridge_util import UserError
from pdf_editor import fitz

TEXT = {".txt", ".md", ".log", ".text"}
CSV = {".csv", ".tsv"}
IMAGES = set(tools.IMAGE_EXTS)
MUPDF = {".epub", ".xps", ".oxps", ".fb2", ".mobi", ".cbz", ".svg"}
OFFICE = msoffice.WORD | msoffice.EXCEL | msoffice.POWERPOINT
WORDS_ONLY = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm", ".odt"}     # formats readable without Office
OPENABLE = OFFICE | TEXT | CSV | IMAGES | MUPDF

KIND = {"word": "Word document", "excel": "spreadsheet", "powerpoint": "presentation"}
KEEP_DAYS = 3
MAX_TEXT_PAGES = 400
MAX_ROWS = 2000


def can_open(path) -> bool:
    return Path(path).suffix.lower() in OPENABLE


def kind_label(path) -> str:
    ext = Path(path).suffix.lower()
    if ext in CSV:
        return "spreadsheet"
    if ext in OFFICE:
        return KIND[msoffice.kind_of(path)]
    return "picture" if ext in IMAGES else "e-book" if ext in MUPDF - {".svg"} else "drawing" if ext == ".svg" else "text file"


# ------------------------------------------------------------------ the one entry point

def to_pdf(src, cache_dir) -> dict:
    src = Path(src)
    ext = src.suffix.lower()
    if ext not in OPENABLE:
        raise UserError(f"{src.name} isn't a kind of file this app can open.")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _tidy(cache_dir)
    office_kind = msoffice.kind_of(src) if ext in OFFICE and ext not in CSV else None
    routes = _routes(ext, office_kind)
    st = src.stat()
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{','.join(routes)}".encode()).hexdigest()[:20]
    pdf, meta = cache_dir / f"{key}.pdf", cache_dir / f"{key}.json"
    if pdf.exists() and meta.exists():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            pdf.touch()
            return {**info, "pdf": pdf}
        except Exception:
            pass
    problems = []
    for route in routes:
        tmp = cache_dir / f"{key}.part.pdf"
        tmp.unlink(missing_ok=True)
        try:
            ROUTES[route](src, tmp, office_kind)
            with fitz.open(tmp) as d:
                if not len(d):
                    raise UserError(f"{src.name} came out empty.")
        except (UserError, msoffice.OfficeError, tools.ToolError) as e:
            problems.append(str(e))
            if "password-protected" in str(e):
                break                                  # every other route would fail the same way
            continue
        except Exception as e:                        # a broken file: try the next way, report plainly at the end
            tools_log(f"{route} could not convert {src.name}: {e!r}")
            problems.append(f"{src.name} couldn't be read - it may be damaged.")
            continue
        tmp.replace(pdf)
        info = {"kind": kind_label(src), "via": route, "text_only": route == "text" and ext in WORDS_ONLY}
        meta.write_text(json.dumps(info), encoding="utf-8")
        return {**info, "pdf": pdf}
    if not routes:
        raise UserError(_needs_converter(src, office_kind))
    pw = next((p for p in problems if "password-protected" in p), None)
    raise UserError(pw or (problems[-1] if problems else _needs_converter(src, office_kind)))


def _routes(ext: str, office_kind: str | None) -> list[str]:
    if ext in IMAGES:
        return ["image"]
    if ext in MUPDF:
        return ["mupdf"]
    if ext in TEXT or ext in CSV:
        return ["text"]
    out = []
    if msoffice.available(office_kind):
        out.append("office")
    if tools.find_soffice():
        out.append("libreoffice")
    if ext in WORDS_ONLY:
        out.append("text")
    return out


def _needs_converter(src: Path, office_kind: str | None) -> str:
    app = msoffice.LABEL.get(office_kind, "Office")
    return (f"Opening {src.suffix.lower()} files needs Microsoft {app} or the free LibreOffice add-on. "
            "On the home screen choose Convert to PDF to install LibreOffice.")


def _tidy(cache_dir: Path):
    """Forget copies nobody has opened for a few days."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for f in cache_dir.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def tools_log(msg: str):
    try:
        from bridge_util import log
        log.info(msg)
    except Exception:
        pass


# ------------------------------------------------------------------ routes

def _via_office(src: Path, out: Path, kind):
    msoffice.to_pdf(src, out, kind)


def _via_libreoffice(src: Path, out: Path, kind):
    soffice = tools.find_soffice()
    if not soffice:
        raise UserError("LibreOffice isn't installed.")
    with tempfile.TemporaryDirectory() as tmp:
        # a private profile: works even while the person has LibreOffice open
        profile = Path(tmp, "profile").as_uri()
        try:
            r = subprocess.run([soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                                "--convert-to", "pdf", "--outdir", tmp, str(src)],
                               capture_output=True, text=True, timeout=180,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise UserError(f"LibreOffice took too long to open {src.name}.")
        made = Path(tmp) / f"{src.stem}.pdf"
        if r.returncode != 0 or not made.exists():
            raise UserError(f"LibreOffice couldn't convert {src.name}.")
        shutil.move(str(made), str(out))


def _via_image(src: Path, out: Path, kind):
    """Each picture (or each frame of a multi-page TIFF) becomes a page about the size of A4."""
    from PIL import Image, ImageSequence
    img = tools._open_image(src)
    doc = fitz.open()
    frames = [f.copy() for f in ImageSequence.Iterator(img)] if getattr(img, "n_frames", 1) > 1 else [img]
    for fr in frames:
        if fr.mode not in ("RGB", "L"):
            fr = fr.convert("RGBA")
            bg = Image.new("RGB", fr.size, "white")
            bg.paste(fr, mask=fr.split()[-1])
            fr = bg
        w, h = fr.size
        scale = 842 / max(w, h)                       # long side = A4 height in points
        buf = io.BytesIO()
        if src.suffix.lower() == ".png" or fr.mode == "L":
            fr.save(buf, "PNG", optimize=True)
        else:
            fr.save(buf, "JPEG", quality=90)
        page = doc.new_page(width=w * scale, height=h * scale)
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(out, garbage=3, deflate=True)


def _via_mupdf(src: Path, out: Path, kind):
    with fitz.open(src) as d:
        pdf = d.convert_to_pdf()
    Path(out).write_bytes(pdf)


def _via_text(src: Path, out: Path, kind):
    ext = src.suffix.lower()
    if ext in TEXT:
        body = _paras(_read_text(src).split("\n"), pre=True)
    elif ext in CSV:
        body = _csv_html(src)
    elif ext in (".docx", ".docm"):
        body = _docx_html(src)
    elif ext in (".pptx", ".pptm"):
        body = _pptx_html(src)
    elif ext in (".xlsx", ".xlsm"):
        body = _xlsx_html(src)
    elif ext == ".odt":
        body = _odt_html(src)
    else:
        raise UserError(f"{src.name} can't be read without Office.")
    if not re.sub(r"<[^>]+>", "", body).strip():
        raise UserError(f"{src.name} has no words that can be read without Office.")
    _story_pdf(body, out)


ROUTES = {"office": _via_office, "libreoffice": _via_libreoffice, "image": _via_image, "mupdf": _via_mupdf,
          "text": _via_text}


# ------------------------------------------------------------------ words-only copies

CSS = """
* { font-family: sans-serif; }
body { font-size: 10.5pt; line-height: 1.4; color: #1f2328; }
h1 { font-size: 16pt; margin: 10pt 0 4pt 0; } h2 { font-size: 13pt; margin: 10pt 0 4pt 0; }
h3 { font-size: 11.5pt; margin: 8pt 0 3pt 0; }
p { margin: 0 0 5pt 0; } p.pre { font-family: monospace; font-size: 9.5pt; margin: 0; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 4pt 0 8pt 0; } td { border: 0.5pt solid #9ca3af; padding: 2pt 4pt; font-size: 9.5pt; }
.slide { color: #6b7280; font-size: 9pt; margin-top: 12pt; }
"""


def _story_pdf(body: str, out: Path):
    """Laid out in memory and written once by Python: on Windows PyMuPDF's DocumentWriter can keep its file open after
    close() (found 5 Nov with Translate's PDF - "being used by another process")."""
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    page = fitz.paper_rect("a4")
    area = page + (54, 54, -54, -54)
    story = fitz.Story(html=f"<body>{body}</body>", user_css=CSS)
    more, n = 1, 0
    while more and n < MAX_TEXT_PAGES:
        dev = writer.begin_page(page)
        more, filled = story.place(area)
        story.draw(dev)
        writer.end_page()
        n += 1
        if more and fitz.Rect(filled).is_empty:                  # something can't fit on any page: stop rather than loop
            break
    writer.close()
    Path(out).write_bytes(buf.getvalue())


def _read_text(src: Path) -> str:
    raw = src.read_bytes()[:20_000_000]
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _paras(lines, pre=False) -> str:
    cls = ' class="pre"' if pre else ""
    return "".join(f"<p{cls}>{html.escape(t) or '&nbsp;'}</p>" for t in lines)


def _table(rows) -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""
    return "<table>" + "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                               for r in rows) + "</table>"


def _csv_html(src: Path) -> str:
    text = _read_text(src)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if src.suffix.lower() == ".tsv" else csv.excel
    rows = []
    for i, r in enumerate(csv.reader(io.StringIO(text), dialect)):
        if i >= MAX_ROWS:
            rows.append([f"… {src.name} has more rows than this copy shows."])
            break
        rows.append(r[:30])
    return _table(rows)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_html(src: Path) -> str:
    with zipfile.ZipFile(src) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    out = []

    def para_text(p):
        bits = []
        for node in p.iter():
            if node.tag == f"{W}t":
                bits.append(node.text or "")
            elif node.tag == f"{W}tab":
                bits.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                bits.append("\n")
        return "".join(bits)

    def para_html(p):
        text = para_text(p)
        style = p.find(f"{W}pPr/{W}pStyle")
        sid = (style.get(f"{W}val") if style is not None else "") or ""
        esc = html.escape(text).replace("\n", "<br/>")
        if not text.strip():
            return ""
        m = re.match(r"(?i)heading\s*(\d)|title", sid)
        if m:
            lvl = min(3, int(m.group(1) or 1))
            return f"<h{lvl}>{esc}</h{lvl}>"
        if p.find(f"{W}pPr/{W}numPr") is not None or re.match(r"(?i)list", sid):
            return f"<p>• {esc}</p>"
        return f"<p>{esc}</p>"

    for child in list(body) if body is not None else []:
        if child.tag == f"{W}p":
            out.append(para_html(child))
        elif child.tag == f"{W}tbl":
            rows = [[" ".join(para_text(p) for p in tc.iter(f"{W}p")) for tc in tr.findall(f"{W}tc")]
                    for tr in child.findall(f"{W}tr")]
            out.append(_table(rows))
        elif child.tag == f"{W}sdt":                 # content controls wrap ordinary paragraphs
            out += [para_html(p) for p in child.iter(f"{W}p")]
    return "".join(out)


A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx_html(src: Path) -> str:
    with zipfile.ZipFile(src) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        out = []
        for i, n in enumerate(names, 1):
            root = ET.fromstring(z.read(n))
            lines = ["".join(t.text or "" for t in p.iter(f"{A}t")) for p in root.iter(f"{A}p")]
            out.append(f'<p class="slide">Slide {i}</p>' + _paras([ln for ln in lines if ln.strip()]))
    return "".join(out)


def _xlsx_html(src: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_ROWS:
                    rows.append([f"… this sheet has more rows than this copy shows."])
                    break
                rows.append(["" if v is None else v for v in r[:30]])
            t = _table(rows)
            if t:
                out.append(f"<h2>{html.escape(ws.title)}</h2>{t}")
    finally:
        wb.close()
    return "".join(out)


def _odt_html(src: Path) -> str:
    T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    with zipfile.ZipFile(src) as z:
        root = ET.fromstring(z.read("content.xml"))
    out = []
    for node in root.iter():
        if node.tag in (f"{T}p", f"{T}h"):
            text = "".join(node.itertext()).strip()
            if text:
                out.append(f"<h2>{html.escape(text)}</h2>" if node.tag == f"{T}h" else f"<p>{html.escape(text)}</p>")
    return "".join(out)
