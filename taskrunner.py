"""
taskrunner.py - the home-screen tasks and "More tools": runs tools.py utilities with plain-language
options, as background jobs the page polls.  Mixed into app.Bridge (methods = window.pywebview.api.*).

    task_list()                    every task, grouped, for the UI
    task_prepare(id, paths)        check the files, fill in sensible defaults, add-on status
    task_pick(id)                  file / folder picker filtered for the task
    task_run(id, paths, opts)      start a job -> {"job": id}
    task_status(job)               {"state": running|done|error, "progress", "log", "outputs", "error"?}
    rename_preview(paths, opts)    what bulk rename would do
    text_transform(text, mode)     the text cleaner
    addons() / addon_install(key)  LibreOffice, Tesseract, FFmpeg (install = winget job, polled like a task)

Rules (same as the Bridge): return JSON dicts, never raise, errors are {"error": "one plain sentence"}.
Results never overwrite anything: tools write into a temp folder, then each result is moved next to its
source file (or the chosen folder) under a name that doesn't exist yet.
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import msoffice
import tools

log = logging.getLogger("toolkit")
IS_WIN = platform.system() == "Windows"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

PDF = {".pdf"}
IMAGES = set(tools.IMAGE_EXTS)
OFFICE = set(tools.OFFICE_EXTS)
MEDIA = set(tools.MEDIA_EXTS)
EXCEL = {".xlsx", ".xlsm"}
CSVS = {".csv", ".tsv"}


# ------------------------------------------------------------------ add-ons

ADDONS = {
    "libreoffice": dict(name="LibreOffice", find=tools.find_soffice, winget="TheDocumentFoundation.LibreOffice",
                        url="https://www.libreoffice.org/download/", size="about 350 MB",
                        why="It turns Word, Excel and PowerPoint files into PDFs and back."),
    "tesseract": dict(name="Tesseract", find=tools.find_tesseract, winget="UB-Mannheim.TesseractOCR",
                      url="https://tesseract-ocr.github.io/tessdoc/Installation.html" if tools.IS_MAC
                      else "https://github.com/UB-Mannheim/tesseract/wiki", size="about 50 MB",
                      why="It reads the words in photos and scanned pages."),
    "ffmpeg": dict(name="FFmpeg", find=tools.find_ffmpeg, winget="Gyan.FFmpeg",
                   url="https://ffmpeg.org/download.html#build-mac" if tools.IS_MAC
                   else "https://www.gyan.dev/ffmpeg/builds/", size="about 100 MB",
                   why="It converts and cuts video and audio files."),
}


def _refresh_path():
    """Pick up programs installed since Toolkit started (winget updates PATH only for new processes)."""
    extra = []
    if IS_WIN:
        try:
            import winreg
            for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        v, _ = winreg.QueryValueEx(k, "Path")
                        extra += os.path.expandvars(v).split(";")
                except OSError:
                    pass
        except ImportError:
            pass
        extra.append(str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"))
    cur = os.environ.get("PATH", "").split(os.pathsep)
    add = [p for p in extra if p and p not in cur and Path(p).is_dir()]
    if add:
        os.environ["PATH"] = os.pathsep.join(cur + add)


_refresh_path()


def _addon_status(key: str) -> dict:
    a = ADDONS[key]
    try:
        installed = bool(a["find"]())
    except Exception:
        installed = False
    return {"key": key, "name": a["name"], "installed": installed, "why": a["why"], "size": a["size"],
            "url": a["url"], "can_install": bool(shutil.which("winget")) if IS_WIN else False}


def _needs_addon(spec) -> bool:
    if not spec.addon:
        return False
    try:
        return not (spec.addon_unless and spec.addon_unless())
    except Exception:
        return True


# ------------------------------------------------------------------ helpers

def _opt(key, kind, label, default="", choices=None, advanced=False, hint=None, placeholder=None, when=None):
    """One option for the page.  when={"other_key": [values]} shows it only while that option has one of them."""
    return {k: v for k, v in dict(key=key, kind=kind, label=label, default=default, choices=choices, advanced=advanced,
                                  hint=hint, placeholder=placeholder, when=when).items() if v is not None}


SAVE_TO = _opt("save_to", "folder", "Save results in", "", advanced=True, placeholder="Next to the original files")


def _size_text(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return ""


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    n = 2
    while True:
        q = p.with_name(f"{p.stem} ({n}){p.suffix}") if p.suffix and not p.is_dir() else p.with_name(f"{p.name} ({n})")
        if not q.exists():
            return q
        n += 1


def _as_int(v, default):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _truthy(v) -> bool:
    return v in (True, 1, "1", "true", "True", "on", "yes")


# ------------------------------------------------------------------ custom runners

STRENGTH = {"Balanced (recommended)": (60, 1600), "Smallest file": (40, 1200), "Better quality": (80, 2200)}


def _smaller(files, o, log_):
    q, px = STRENGTH.get(o.get("strength"), STRENGTH["Balanced (recommended)"])
    out = []
    pdfs = [f for f in files if f.suffix.lower() in PDF]
    pics = [f for f in files if f.suffix.lower() in IMAGES]
    if pdfs:
        out += tools.pdf_compress(pdfs, {"quality": q, "max_px": px, "outdir": o["outdir"]}, log_)
    if pics:
        out += tools.image_compress(pics, {"quality": q, "max_px": px, "outdir": o["outdir"]}, log_)
    return out


def _windows_reader() -> bool:
    """Windows reads words itself (Windows.Media.Ocr) - "Make words selectable" uses it; then no add-on is needed."""
    import scanwords
    return scanwords.windows_available()


def _ocr(files, o, log_):
    tess = tools.find_tesseract()
    if not tess:
        if _windows_reader():
            return _ocr_windows(files, o, log_)
        raise tools.ToolError("Reading text needs the free Tesseract add-on.")
    lang = (o.get("lang") or "eng").strip() or "eng"
    searchable = str(o.get("output", "")).startswith("Searchable")
    out = []
    pics = [f for f in files if f.suffix.lower() in IMAGES]
    if pics:
        out += tools.ocr(pics, {"lang": lang, "output": "Searchable PDF" if searchable else "Text file (.txt)",
                                "outdir": o["outdir"]}, log_)
    for f in (x for x in files if x.suffix.lower() in PDF):
        out.append(_ocr_pdf(f, tess, lang, searchable, Path(o["outdir"]), log_))
    return out


def _ocr_windows(files, o, log_):
    """Without Tesseract: Windows' own reader, as "Make words selectable" reads a scan.  A searchable PDF keeps each
    page's picture as it is and has its words written in, invisibly (scanwords.write) - the same file names as
    Tesseract's way."""
    import pymupdf as fitz
    import scanwords
    searchable = str(o.get("output", "")).startswith("Searchable")
    outdir = Path(o["outdir"])
    out = []

    def words(lines) -> str:
        return "\n".join(" ".join(t.strip() for t, _ in line if t.strip()) for line in lines if line)

    for f in files:
        if f.suffix.lower() in IMAGES:
            with fitz.open(str(f)) as img:                    # a picture: a page of its own first
                doc = fitz.open("pdf", img.convert_to_pdf())
        else:
            doc = fitz.open(str(f))
        try:
            texts, read = [], 0
            for i, pg in enumerate(doc):
                log_(f"Reading page {i + 1} of {len(doc)} of {f.name}…")
                if not scanwords.is_scan(pg):                  # real text already
                    texts.append(pg.get_text().strip())
                    continue
                png, _z, size = scanwords.picture(pg)
                try:
                    lines = scanwords.read_picture(png, size, "windows")
                except scanwords.ScanError as e:
                    raise tools.ToolError(str(e))
                if not lines:
                    texts.append("")
                    continue
                read += 1
                if searchable:
                    scanwords.write(pg, scanwords.to_page(pg, lines, size))
                else:
                    texts.append(words(lines))
            if searchable:
                t = outdir / f"{f.stem}_searchable.pdf"
                doc.save(str(t), garbage=3, deflate=True)
            else:
                t = outdir / f"{f.stem}_text.txt"
                t.write_text("\n\n".join(texts).strip() + "\n", encoding="utf-8")
        finally:
            doc.close()
        log_(f"{f.name} -> {t.name}" + ("" if read else " (no words could be read in its pictures)"))
        out.append(t)
    return out


def _ocr_pdf(f: Path, tess: str, lang: str, searchable: bool, outdir: Path, log_):
    """Scanned PDF -> text file or searchable PDF, page by page (pages that already have text are kept)."""
    import pymupdf as fitz
    src = fitz.open(str(f))
    texts, merged = [], fitz.open() if searchable else None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for i, pg in enumerate(src):
                log_(f"Reading page {i + 1} of {len(src)} of {f.name}…")
                existing = pg.get_text().strip()
                if len(existing) > 40:                       # real text already: no OCR needed
                    texts.append(existing)
                    if merged is not None:
                        merged.insert_pdf(src, from_page=i, to_page=i)
                    continue
                png = Path(tmp) / f"p{i}.png"
                pg.get_pixmap(dpi=300).save(str(png))
                base = Path(tmp) / f"p{i}"
                r = tools._run([tess, str(png), str(base), "-l", lang] + (["pdf"] if searchable else []))
                if r.returncode != 0:
                    raise tools.ToolError(f"Tesseract couldn't read page {i + 1} of {f.name}"
                                          + (f" - is the language '{lang}' installed?" if "lang" in (r.stderr or "").lower() else "."))
                if searchable:
                    with fitz.open(str(base.with_suffix(".pdf"))) as one:
                        merged.insert_pdf(one)
                else:
                    texts.append(base.with_suffix(".txt").read_text(encoding="utf-8", errors="ignore").strip())
            if searchable:
                t = outdir / f"{f.stem}_searchable.pdf"
                merged.save(str(t), garbage=3, deflate=True)
            else:
                t = outdir / f"{f.stem}_text.txt"
                t.write_text("\n\n".join(texts) + "\n", encoding="utf-8")
    finally:
        src.close()
        if merged is not None:
            merged.close()
    log_(f"{f.name} -> {t.name}")
    return t


def _split(files, o, log_):
    every = str(o.get("mode", "")).startswith("Every")
    out = []
    for f in files:
        if every:
            sub = Path(o["outdir"]) / f"{f.stem}_pages"
            out += tools.pdf_split([f], {"mode": "Every page to its own file", "outdir": str(sub)}, log_)
        else:
            ranges = (o.get("ranges") or "").strip()
            if not ranges:
                raise tools.ToolError("Type which pages to save, for example 1-3, 7.")
            import pymupdf as fitz
            with fitz.open(str(f)) as d:
                n = len(d)
            try:
                tools.parse_ranges(ranges, n)
            except (tools.ToolError, ValueError):
                raise tools.ToolError(f"{f.name} has {n} page{'s' if n != 1 else ''} - check the page numbers "
                                      "(for example 1-3, 7).")
            out += tools.pdf_split([f], {"mode": "Pages", "ranges": ranges, "outdir": o["outdir"]}, log_)
    return out


def _qr(files, o, log_):
    text = (o.get("text") or "").strip()
    if not text:
        raise tools.ToolError("Type the web address or text to put in the QR code.")
    import re
    name = re.sub(r"[^A-Za-z0-9]+", "_", text.split("//")[-1])[:30].strip("_") or "code"
    t = Path(o["outdir"]) / f"QR code - {name}.png"
    tools.make_qr(text, t, _as_int(o.get("size"), 600))
    log_(f"-> {t.name}")
    return [t]


OPACITY = {"Light": 0.12, "Medium": 0.2, "Strong": 0.35}
OFFICE_FORMATS = {"Word document (.docx)": "Word (.docx)", "Excel spreadsheet (.xlsx)": "Excel (.xlsx)",
                  "PowerPoint slides (.pptx)": "PowerPoint (.pptx)"}
PIC_SIZES = {"Keep the same size": 0, "Large (2400 px)": 2400, "Medium (1600 px)": 1600, "Small (1024 px)": 1024}
MEDIA_FORMATS = {"MP4 video (plays everywhere)": "MP4 (video)", "MP3 audio only": "MP3 (audio only)",
                 "M4A audio only": "M4A (audio only)", "WAV audio only": "WAV (audio only)",
                 "GIF animation (short clips)": "GIF (short clips)"}
RENAME_MODES = ["Add text at the start", "Add text at the end", "Replace text", "Number them 001, 002…",
                "Put the file date first", "Make lowercase", "Spaces to underscores"]
RENAME_MAP = dict(zip(RENAME_MODES, ["Add prefix", "Add suffix", "Replace text", "Number sequentially",
                                     "Prefix with file date", "Lowercase", "Spaces to underscores"]))
TEXT_MODES = ["Clean URL (strip tracking)", "Plain text (strip formatting/extra spaces)", "UPPER CASE", "lower case",
              "Title Case", "Sentence case", "Remove line breaks", "Remove blank lines", "Sort lines",
              "Remove duplicate lines", "Count words"]
TEXT_LABELS = ["Remove tracking from links", "Plain text (tidy spaces)", "UPPER CASE", "lower case", "Title Case",
               "Sentence case", "Join into one line", "Remove blank lines", "Sort lines A–Z",
               "Remove duplicate lines", "Count words"]


def _rename_opts(o):
    return {"mode": RENAME_MAP.get(o.get("mode"), "Add prefix"), "text": o.get("text", ""),
            "replace": o.get("replace", ""), "start": _as_int(o.get("start"), 1)}


# ------------------------------------------------------------------ registry

@dataclass
class Task:
    id: str
    title: str
    desc: str
    group: str
    icon: str
    tint: str
    run: Callable
    exts: set | None = None             # accepted extensions (None = anything)
    accepts: str = "files"              # plain words for the accepted kind, used in messages
    pick: str = "files"                 # files | folders | none | text
    options: list = field(default_factory=list)
    verb: str = "Start"                 # {n} = file count, {s} = plural s
    min_files: int = 1
    reorder: bool = False
    addon: str | None = None
    addon_unless: Callable | None = None   # () -> True when something else does the add-on's job (Microsoft Office)
    wrap: bool = True                   # run into a temp folder, then move results next to the sources
    suffix: str = ""                    # added when a result would have the source's own name
    suffix_when: str = "same_ext"       # same_ext | always
    home: bool = False
    defaults: Callable | None = None    # files -> {option key: default}
    dest: Callable | None = None        # where results go when there are no source files
    picker: str = "Files"               # description for the file dialog filter (letters and spaces only)
    folders_too: bool = False           # a folder may be chosen too (its files are found when it runs)
    stoppable: bool = False             # a Stop button while it runs; run gets the Job as opts["_job"]
    adjust: Callable | None = None      # options -> options, each time the sheet is filled in (what is saved now)

    def meta(self):
        return {"id": self.id, "title": self.title, "desc": self.desc, "group": self.group, "icon": self.icon,
                "tint": self.tint, "pick": self.pick, "verb": self.verb, "min_files": self.min_files,
                "reorder": self.reorder, "addon": self.addon, "home": self.home, "accepts": self.accepts,
                "exts": sorted(self.exts) if self.exts else None, "folders_too": self.folders_too, "stoppable": self.stoppable}

    def options_for(self, files):
        dyn = self.defaults(files) if self.defaults and files else {}
        out = []
        for o in self.options + ([SAVE_TO] if self.wrap and self.pick in ("files", "folders") else []):
            o = dict(o)
            if o["key"] in dyn:
                o["default"] = dyn[o["key"]]
            out.append(o)
        return self.adjust(out) if self.adjust else out


def _desktop():
    d = Path.home() / "Desktop"
    return d if d.is_dir() else Path.home()


def _first_stem(suffix):
    return lambda files: {"name": f"{files[0].stem}{suffix}"}


def _redact_options():
    import redact_many as rm
    first = rm.KINDS[0][0]
    out = [dict(_opt(key, "check", label, on, hint=rm.HINTS.get(key)), section="Take out" if key == first else None)
           for key, label, _, on in rm.KINDS]
    return out + _copies_options(scans=True, props=True)


SCANS_OPT = dict(_opt("read_scans", "check", "Read the words in scanned pages first (slower)", True,
                      hint="A scan is a picture of the paper: without this its pages are listed as not looked at."), section="Scans")
PROPS_OPT = dict(_opt("clear_props", "check", "Clear each copy's properties too (author, title and the like)", True,
                      hint="Hidden in the file, they can hold a name."), section="Also")


def _copies_options(scans: bool, props: bool):
    """What every 'copies in a new folder' task ends with (redact_many.py)."""
    return ([SCANS_OPT] if scans else []) + ([PROPS_OPT] if props else []) + [
        _opt("deeper", "check", "Include folders inside the chosen folders", False, advanced=True),
        _opt("save_to", "folder", "Put the copies' folder in", "", advanced=True, placeholder="Beside the original files")]


def _redact_adjust(options):
    """Your saved details: only offered ticked when there are some."""
    import bridge_fill
    if bridge_fill.own_details(bridge_fill.load_details()):
        return options
    for o in options:
        if o["key"] == "k_yours":
            o["default"] = False
            o["hint"] = "None saved yet - open any PDF, then More › My details."
    return options


TASK_LIST = [
    # ---- home cards
    Task("combine", "Combine PDFs", "Join several PDFs into one file, in the order you choose.", "PDF", "combine", "amber",
         lambda f, o, l: tools.pdf_merge(f, {"name": o.get("name") or "Combined", "outdir": o["outdir"]}, l),
         exts=PDF, accepts="PDFs", options=[_opt("name", "text", "Name of the new PDF", "Combined")],
         verb="Combine {n} PDFs", min_files=2, reorder=True, home=True, defaults=_first_stem(" combined"), picker="PDF files"),
    Task("convert", "Convert to PDF", "Turn Word, Excel or PowerPoint files into PDFs.", "PDF", "convert", "rose",
         lambda f, o, l: tools.office_to_pdf(f, {"outdir": o["outdir"]}, l),
         exts=OFFICE, accepts="Word, Excel, PowerPoint or text files", verb="Convert {n} file{s} to PDF",
         addon="libreoffice", addon_unless=msoffice.any_available, home=True, picker="Office files"),
    Task("smaller", "Make smaller for email", "Shrink PDFs or photos so they attach easily.", "PDF", "compress", "teal",
         _smaller, exts=PDF | IMAGES, accepts="PDFs or pictures",
         options=[_opt("strength", "choice", "How small", "Balanced (recommended)", list(STRENGTH))],
         verb="Make {n} file{s} smaller", home=True, suffix="_small", suffix_when="always", picker="PDFs and pictures"),
    Task("photos", "Photos → PDF", "Put pictures or scans into one tidy PDF.", "Pictures", "image", "amber",
         lambda f, o, l: tools.images_to_pdf(f, {"name": o.get("name") or "Photos", "outdir": o["outdir"]}, l),
         exts=IMAGES, accepts="pictures", options=[_opt("name", "text", "Name of the new PDF", "Photos")],
         verb="Make a PDF from {n} picture{s}", reorder=True, home=True, defaults=_first_stem(" photos"), picker="Pictures"),
    Task("ocr", "Pull text from a scan", "Copy the words out of a photo or scanned page.", "Pictures", "scan", "violet",
         _ocr, exts=PDF | IMAGES, accepts="pictures or scanned PDFs",
         options=[_opt("output", "choice", "What you get", "Text you can copy (.txt)",
                       ["Text you can copy (.txt)", "Searchable PDF"]),
                  _opt("lang", "text", "Language code", "eng", advanced=True,
                       hint="eng = English. Other languages must be ticked when installing Tesseract. "
                            "Without Tesseract, Windows reads in the languages it has installed.")],
         verb="Get the text", addon="tesseract", addon_unless=_windows_reader, home=True, picker="Pictures and PDFs"),

    # ---- PDF
    Task("split", "Take pages out of a PDF", "Save some pages as a new PDF, or every page as its own file.", "PDF",
         "scissors", "blue", _split, exts=PDF, accepts="PDFs",
         options=[_opt("mode", "choice", "What to save", "Just some pages", ["Just some pages", "Every page as its own file"]),
                  _opt("ranges", "text", "Which pages", "", placeholder="for example 1-3, 7, 10-",
                       hint="Leave the last number off to go to the end (10- means page 10 onwards).",
                       when={"mode": ["Just some pages"]})],
         verb="Save the pages", picker="PDF files"),
    Task("watermark", "Stamp a watermark", "Write DRAFT, COPY or CONFIDENTIAL across every page.", "PDF", "stamp", "rose",
         lambda f, o, l: tools.pdf_watermark(f, {"text": o.get("text") or "DRAFT", "size": _as_int(o.get("size"), 60),
                                                 "opacity": OPACITY.get(o.get("opacity"), 0.15), "outdir": o["outdir"]}, l),
         exts=PDF, accepts="PDFs",
         options=[_opt("text", "text", "Words to stamp", "DRAFT"),
                  _opt("opacity", "choice", "How strong", "Light", list(OPACITY), advanced=True),
                  _opt("size", "int", "Letter size", 60, advanced=True)],
         verb="Stamp {n} PDF{s}", picker="PDF files"),
    Task("unwatermark", "Take a watermark off", "Remove a DRAFT or COPY stamp from a PDF you stamped. The words under it stay.",
         "PDF", "stamp", "rose",
         lambda f, o, l: tools.pdf_remove_watermark(f, {"outdir": o["outdir"]}, l),
         exts=PDF, accepts="PDFs", verb="Take it off {n} PDF{s}", picker="PDF files"),
    Task("numbers", "Number the pages", "Add 'Page 1 of 10' to the bottom of every page.", "PDF", "hash", "blue",
         lambda f, o, l: tools.pdf_page_numbers(f, {"format": o.get("format") or "Page {n} of {total}",
                                                    "position": o.get("position") or "Bottom centre",
                                                    "size": _as_int(o.get("size"), 10), "outdir": o["outdir"]}, l),
         exts=PDF, accepts="PDFs",
         options=[_opt("position", "choice", "Where", "Bottom centre",
                       ["Bottom centre", "Bottom right", "Bottom left", "Top centre", "Top right", "Top left"]),
                  _opt("format", "text", "Wording", "Page {n} of {total}", advanced=True,
                       hint="{n} becomes the page number, {total} the number of pages."),
                  _opt("size", "int", "Letter size", 10, advanced=True)],
         verb="Number {n} PDF{s}", picker="PDF files"),
    Task("to_office", "PDF → Word or Excel", "Turn a PDF back into a file you can edit. Best for PDFs made from documents, not scans.",
         "PDF", "convert", "violet",
         lambda f, o, l: tools.pdf_to_office(f, {"format": OFFICE_FORMATS.get(o.get("format"), "Word (.docx)"),
                                                 "outdir": o["outdir"]}, l),
         exts=PDF, accepts="PDFs",
         options=[_opt("format", "choice", "Turn it into", "Word document (.docx)", list(OFFICE_FORMATS))],
         verb="Convert {n} PDF{s}", addon="libreoffice", addon_unless=lambda: msoffice.available("word"),
         picker="PDF files"),

    Task("redact_many", "Remove personal details from many PDFs",
         "Take email addresses, phone numbers, card and bank numbers and more out of copies of many PDFs at once. "
         "The originals aren't changed.", "PDF", "redact", "rose",
         lambda f, o, l: __import__("redact_many").run(f, o, l, o.pop("_job", None)),
         exts=PDF, accepts="PDFs or folders of PDFs", wrap=False, folders_too=True, stoppable=True,
         options=_redact_options(), adjust=_redact_adjust, verb="Make the copies", picker="PDF files"),

    Task("redact_words", "Black out words in many PDFs",
         "Type names, numbers or any words: they're blacked out of copies of many PDFs at once. The originals aren't changed.",
         "PDF", "redact", "rose",
         lambda f, o, l: __import__("redact_many").run(f, o, l, o.pop("_job", None), mode="words"),
         exts=PDF, accepts="PDFs or folders of PDFs", wrap=False, folders_too=True, stoppable=True,
         options=[_opt("words", "lines", "Words to black out", "", placeholder="One to a line, for example\nAda Lovelace\nCase 2026/1187",
                       hint="A name or phrase is found even where it runs onto the next line."),
                  _opt("whole", "check", "Whole words only", True, hint="“Ann” isn't found inside “Annual”."),
                  _opt("anycase", "check", "Capitals don't matter", True, hint="“lovelace” finds “Lovelace” and “LOVELACE”.")]
         + _copies_options(scans=True, props=True),
         verb="Make the copies", picker="PDF files"),
    Task("clear_props", "Clear document properties",
         "Take the author, title and other hidden properties out of copies of many PDFs. The originals aren't changed.",
         "PDF", "redact", "rose",
         lambda f, o, l: __import__("redact_many").run(f, o, l, o.pop("_job", None), mode="props"),
         exts=PDF, accepts="PDFs or folders of PDFs", wrap=False, folders_too=True, stoppable=True,
         options=_copies_options(scans=False, props=False), verb="Make the copies", picker="PDF files"),

    # ---- Pictures
    Task("pictures", "Change picture format or size", "Turn iPhone HEIC photos into JPG, or make pictures smaller.",
         "Pictures", "image", "teal",
         lambda f, o, l: tools.image_convert(f, {"format": o.get("format") or "JPG",
                                                 "max_px": PIC_SIZES.get(o.get("max_px"), 0),
                                                 "quality": _as_int(o.get("quality"), 85), "outdir": o["outdir"]}, l),
         exts=IMAGES, accepts="pictures",
         options=[_opt("format", "choice", "Save as", "JPG", ["JPG", "PNG", "WEBP", "TIFF", "BMP"]),
                  _opt("max_px", "choice", "Size", "Keep the same size", list(PIC_SIZES)),
                  _opt("quality", "int", "JPG quality (1-95)", 85, advanced=True)],
         verb="Convert {n} picture{s}", suffix="_converted", picker="Pictures"),
    Task("qr", "Make a QR code", "Turn a web address or some text into a QR code picture.", "Pictures", "qr", "blue",
         _qr, pick="none",
         options=[_opt("text", "text", "Web address or text", "", placeholder="https://"),
                  _opt("size", "int", "Picture size (pixels)", 600, advanced=True)],
         verb="Make the QR code", dest=_desktop),

    # ---- Spreadsheets
    Task("excel_csv", "Excel → CSV", "Save every sheet of a workbook as its own CSV file.", "Spreadsheets", "table", "green",
         lambda f, o, l: tools.excel_to_csv(f, {"outdir": o["outdir"]}, l),
         exts=EXCEL, accepts="Excel workbooks (.xlsx)", verb="Convert {n} workbook{s}", picker="Excel files"),
    Task("csv_excel", "CSV → Excel", "Turn CSV files into proper Excel workbooks.", "Spreadsheets", "table", "green",
         lambda f, o, l: tools.csv_to_excel(f, {"combine": _truthy(o.get("combine")), "name": o.get("name") or "Combined",
                                                "outdir": o["outdir"]}, l),
         exts=CSVS, accepts="CSV files",
         options=[_opt("combine", "check", "Put them all in one workbook (a sheet each)", False),
                  _opt("name", "text", "Name of the combined workbook", "Combined", when={"combine": [True]})],
         verb="Convert {n} file{s}", picker="CSV files"),

    # ---- Files & folders
    Task("rename", "Rename many files", "Add text, number them or tidy their names - you see the result first.",
         "Files & folders", "rename", "amber",
         lambda f, o, l: tools.bulk_rename(f, _rename_opts(o), l),
         accepts="files", wrap=False,
         options=[_opt("mode", "choice", "How", RENAME_MODES[0], RENAME_MODES),
                  _opt("text", "text", "Text", "", when={"mode": RENAME_MODES[:4]}),
                  _opt("replace", "text", "Replace with", "", when={"mode": ["Replace text"]}),
                  _opt("start", "int", "First number", 1, advanced=True, when={"mode": [RENAME_MODES[3]]})],
         verb="Rename {n} file{s}"),
    Task("tidy", "Tidy a folder", "Sort loose files into Pictures, Documents, Installers and more.", "Files & folders",
         "folder", "teal",
         lambda f, o, l: tools.tidy_folder([], {"folder": o.get("folder"), "dry_run": _truthy(o.get("dry_run")),
                                                "older_days": _as_int(o.get("older_days"), 0)}, l),
         pick="none", wrap=False,
         options=[_opt("folder", "folder", "Folder to tidy", str(Path.home() / "Downloads")),
                  _opt("dry_run", "check", "Just show me what would move", True),
                  _opt("older_days", "int", "Only files older than (days, 0 = all)", 0, advanced=True)],
         verb="Tidy the folder"),
    Task("duplicates", "Find duplicate files", "Find identical copies taking up space. Nothing is deleted.",
         "Files & folders", "copies", "violet",
         lambda f, o, l: tools.find_duplicates(f, {"move": _truthy(o.get("move"))}, l),
         pick="folders", accepts="folders", wrap=False,
         options=[_opt("move", "check", "Move the extra copies into a '_duplicates' folder", False, advanced=True)],
         verb="Look for duplicates"),
    Task("zip", "Zip files", "Pack files or folders into one .zip to send.", "Files & folders", "zip", "amber",
         lambda f, o, l: tools.zip_files(f, {"name": o.get("name") or "Archive", "password": o.get("password", ""),
                                             "outdir": o["outdir"]}, l),
         accepts="files or folders",
         options=[_opt("name", "text", "Name of the zip", "Archive")],
         verb="Zip {n} item{s}", defaults=lambda files: {"name": files[0].stem if len(files) == 1 else "Archive"}),
    Task("unzip", "Unzip", "Unpack a .zip into a folder next to it.", "Files & folders", "unzip", "amber",
         lambda f, o, l: tools.unzip_files(f, {"password": o.get("password", ""), "outdir": o["outdir"]}, l),
         exts={".zip"}, accepts="zip files",
         options=[_opt("password", "password", "Password (only if it has one)", "", advanced=True)],
         verb="Unzip {n} file{s}", picker="Zip files"),

    # ---- Video & audio
    Task("media_convert", "Convert video or audio", "Make a video play anywhere (MP4), or keep just the sound (MP3).",
         "Video & audio", "film", "rose",
         lambda f, o, l: tools.media_convert(f, {"format": MEDIA_FORMATS.get(o.get("format"), "MP4 (video)"),
                                                 "outdir": o["outdir"]}, l),
         exts=MEDIA, accepts="video or audio files",
         options=[_opt("format", "choice", "Turn it into", list(MEDIA_FORMATS)[0], list(MEDIA_FORMATS))],
         verb="Convert {n} file{s}", addon="ffmpeg", suffix="_converted", picker="Video and audio"),
    Task("media_trim", "Cut a clip", "Keep just part of a video or song, without losing quality.", "Video & audio",
         "clip", "rose",
         lambda f, o, l: tools.media_trim(f, {"start": o.get("start", ""), "end": o.get("end", ""), "outdir": o["outdir"]}, l),
         exts=MEDIA, accepts="video or audio files",
         options=[_opt("start", "text", "Start at", "", placeholder="for example 0:30"),
                  _opt("end", "text", "End at", "", placeholder="for example 2:15",
                       hint="Leave either empty to keep the beginning or the end.")],
         verb="Cut {n} clip{s}", addon="ffmpeg", picker="Video and audio"),

    # ---- Text
    Task("text", "Clean up text", "Remove tracking from links, fix capitals, sort or de-duplicate lines.", "Text",
         "type", "blue", lambda f, o, l: [], pick="text", wrap=False,
         options=[_opt("mode", "choice", "What to do", TEXT_LABELS[0], TEXT_LABELS)], verb="Clean up"),
]
TASKS = {t.id: t for t in TASK_LIST}
GROUPS = ["PDF", "Pictures", "Spreadsheets", "Files & folders", "Video & audio", "Text"]

try:
    import pyzipper  # noqa: F401
    TASKS["zip"].options.append(_opt("password", "password", "Password (optional)", "", advanced=True))
except ImportError:
    pass


# ------------------------------------------------------------------ errors

def _friendly(ex: Exception) -> str:
    if isinstance(ex, tools.ToolError):
        return " ".join(str(ex).split())
    name = type(ex).__name__
    msg = str(ex).lower()
    if isinstance(ex, PermissionError):
        return "Windows won't let Toolkit write there - a file may be open in another program. Close it and try again."
    if isinstance(ex, FileNotFoundError):
        return "A file can't be found - it may have been moved or deleted."
    if "decrypt" in msg or "password" in msg or name in ("FileNotDecryptedError", "DependencyError"):
        return "One of the files is locked with a password. Open it with Edit a PDF and save a copy first."
    if name in ("PdfReadError", "PdfStreamError", "EmptyFileError"):
        return "One of the PDFs looks damaged, so it couldn't be read."
    if name == "UnidentifiedImageError":
        return "One of the pictures couldn't be read - it may be damaged or not really a picture."
    if isinstance(ex, zipfile.BadZipFile):
        return "That zip file is damaged, or isn't really a zip."
    if isinstance(ex, subprocess.TimeoutExpired):
        return "That took too long and was stopped. Try fewer or smaller files."
    return "Something went wrong while working on your files. Details were saved to the log file."


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception as ex:
            log.error("%s failed:\n%s", fn.__name__, traceback.format_exc())
            return {"error": _friendly(ex)}
    return wrapper


# ------------------------------------------------------------------ jobs

class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "running"
        self.lines: list[str] = []
        self.progress = "Starting…"
        self.outputs: list[dict] = []
        self.error: str | None = None
        self.started = time.time()
        self.stop = False                       # the person pressed Stop (tasks that can stop)
        self.extra: dict = {}                   # what a task has to say at the end besides its files

    def log(self, line: str):
        line = str(line).rstrip()
        if line:
            self.lines.append(line)
            self.progress = line

    def snapshot(self):
        d = {"job": self.id, "state": self.state, "progress": self.progress, "log": self.lines[-300:],
             "outputs": self.outputs, "elapsed": round(time.time() - self.started, 1)}
        if self.error:
            d["error"] = self.error
        if self.extra:
            d["extra"] = self.extra
        return d


_JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def _start(job: Job, target, *args):
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        if len(_JOBS) > 50:   # forget old finished jobs
            for k in [k for k, j in _JOBS.items() if j.state != "running"][: len(_JOBS) - 50]:
                _JOBS.pop(k, None)
    threading.Thread(target=target, args=(job, *args), daemon=True).start()
    return {"job": job.id}


def _out_entry(p: Path, src: Path | None = None, sizes: bool = False):
    """One result for the page; sizes=True adds 'before -> after' (for Make smaller)."""
    d = {"path": str(p), "name": p.name, "is_dir": p.is_dir(), "folder": str(p.parent)}
    if p.is_file():
        d["size"] = _size_text(p.stat().st_size)
        if sizes and src is not None and src.is_file():
            d["note"] = f"{_size_text(src.stat().st_size)} → {_size_text(p.stat().st_size)}"
    return d


def _source_for(entry: Path, files: list[Path]) -> Path | None:
    """The source a result came from: the file whose name starts the result's name, else the first file."""
    best = None
    for f in files:
        stem = f.stem if f.is_file() else f.name
        if entry.stem.startswith(stem) or entry.name.startswith(stem):
            if best is None or len(stem) > len(best.stem if best.is_file() else best.name):
                best = f
    return best or (files[0] if files else None)


def _execute(job: Job, spec: Task, files: list[Path], opts: dict):
    log.info("task %s started on %d item(s)", spec.id, len(files))
    try:
        save_to = (opts.pop("save_to", "") or "").strip()
        if save_to and not Path(save_to).is_dir():
            raise tools.ToolError("The folder chosen under 'Save results in' doesn't exist any more.")
        if not spec.wrap:
            if spec.stoppable:
                opts["_job"] = job
                if save_to:
                    opts["save_to"] = save_to
            made = spec.run(files, opts, job.log) or []
            job.outputs = [_out_entry(Path(p)) for p in made if Path(p).exists()]
        else:
            with tempfile.TemporaryDirectory(prefix="toolkit_") as tmp:
                spec.run(files, dict(opts, outdir=tmp), job.log)
                entries = sorted(Path(tmp).iterdir(), key=lambda p: p.name.lower())
                outs = []
                for e in entries:
                    src = _source_for(e, files)
                    dest_dir = Path(save_to) if save_to else (src.parent if src else (spec.dest() if spec.dest else Path.home()))
                    name = e.name
                    if spec.suffix and src is not None and e.is_file() and e.stem == src.stem and (
                            spec.suffix_when == "always" or e.suffix.lower() == src.suffix.lower()):
                        name = f"{e.stem}{spec.suffix}{e.suffix}"
                    target = _unique(dest_dir / name)
                    shutil.move(str(e), str(target))
                    outs.append(_out_entry(target, src, sizes=spec.id == "smaller"))
                job.outputs = outs
        failed = [ln for ln in job.lines if ln.startswith("FAILED")]
        if not job.outputs and failed and spec.wrap:
            raise tools.ToolError(f"It didn't work for {len(failed)} file{'s' if len(failed) != 1 else ''}. "
                                  "The file may be damaged or protected - see Details.")
        job.progress = "Done."
        job.state = "done"
        log.info("task %s done: %d output(s)", spec.id, len(job.outputs))
    except Exception as ex:
        if isinstance(ex, tools.ToolError):
            log.info("task %s stopped: %s", spec.id, ex)
        else:
            log.error("task %s failed:\n%s", spec.id, traceback.format_exc())
        job.error = _friendly(ex)
        job.state = "error"


def _install(job: Job, key: str):
    a = ADDONS[key]
    job.progress = f"Downloading and installing {a['name']} - this can take a few minutes…"
    try:
        cmd = ["winget", "install", "-e", "--id", a["winget"], "--accept-package-agreements",
               "--accept-source-agreements", "--disable-interactivity"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", creationflags=NO_WINDOW)
        for line in p.stdout:
            line = line.strip().strip("-\\|/ ").strip()
            if len(line) > 3 and not set(line) <= set("█▒░ .%0123456789KMBGi/"):
                job.lines.append(line)
        rc = p.wait()
        _refresh_path()
        if _addon_status(key)["installed"]:
            job.progress = f"{a['name']} is installed."
            job.state = "done"
            log.info("installed add-on %s", key)
            return
        log.error("winget install %s exited %s: %s", a["winget"], rc, "\n".join(job.lines[-20:]))
        job.error = (f"{a['name']} didn't finish installing. Try again, or download it from {a['url']}"
                     " and then press Check again.")
    except FileNotFoundError:
        job.error = f"Install {a['name']} from {a['url']}, then press Check again."
    except Exception:
        log.error("add-on install failed:\n%s", traceback.format_exc())
        job.error = f"{a['name']} couldn't be installed automatically. Download it from {a['url']}."
    job.state = "error"


# ------------------------------------------------------------------ mixin

def _check_files(spec: Task, paths) -> tuple[list[Path], str | None]:
    """Accepted files (deduplicated, in the given order) and one sentence about anything left out."""
    ok, bad, locked, missing = [], [], [], []
    seen = set()
    for p in paths or []:
        q = Path(p)
        key = str(q.resolve()).lower() if q.exists() else str(q).lower()
        if key in seen:
            continue
        seen.add(key)
        if not q.exists():
            missing.append(q.name); continue
        if spec.pick == "folders":
            good = q.is_dir()
        elif spec.folders_too and q.is_dir():
            ok.append(q); continue
        elif spec.id == "zip":
            good = True
        else:
            good = q.is_file() and (spec.exts is None or q.suffix.lower() in spec.exts)
        if not good:
            bad.append(q.name); continue
        # (a task that reports each file says itself which were locked or damaged)
        if q.suffix.lower() == ".pdf" and spec.id not in ("zip", "rename") and not spec.folders_too:
            try:
                import pymupdf as fitz
                with fitz.open(str(q)) as d:
                    if d.needs_pass:
                        locked.append(q.name); continue
            except Exception:
                bad.append(q.name); continue
        ok.append(q)
    msgs = []
    if bad:
        msgs.append(f"{_names(bad)} {'was' if len(bad) == 1 else 'were'} left out - this works with {spec.accepts}.")
    if locked:
        msgs.append(f"{_names(locked)} {'is' if len(locked) == 1 else 'are'} locked with a password - "
                    "open it with Edit a PDF and save a copy first.")
    if missing:
        msgs.append(f"{_names(missing)} can't be found any more.")
    return ok, " ".join(msgs) or None


def _names(names):
    if len(names) == 1:
        return names[0]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + " and " + names[-1]
    return f"{len(names)} files"


class TaskMixin:
    """Home-screen task methods for app.Bridge."""

    @_safe
    def task_list(self):
        return {"groups": GROUPS, "tasks": [t.meta() for t in TASK_LIST]}

    @_safe
    def task_prepare(self, task_id: str, paths=None):
        spec = TASKS.get(task_id)
        if not spec:
            return {"error": "That task doesn't exist."}
        files, note = _check_files(spec, paths)
        return {"task": spec.meta(), "rejected": note,
                "files": [{"path": str(f), "name": f.name, "is_dir": f.is_dir(),
                           "size": _size_text(f.stat().st_size) if f.is_file() else "folder"} for f in files],
                "options": spec.options_for(files),
                "addon": _addon_status(spec.addon) if _needs_addon(spec) else None}

    @_safe
    def task_pick(self, task_id: str, folder: bool = False):
        spec = TASKS.get(task_id)
        window = getattr(self, "_window", None)
        if not spec:
            return {"error": "That task doesn't exist."}
        if not window:
            return {"error": "File pickers only work in the app window."}
        import webview
        if spec.pick == "folders" or (folder and spec.folders_too):
            res = window.create_file_dialog(webview.FileDialog.FOLDER)
            return {"paths": list(res or [])}
        types = ("All files (*.*)",)
        if spec.exts:
            types = (f"{spec.picker} ({';'.join('*' + e for e in sorted(spec.exts))})", "All files (*.*)")
        res = window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=types)
        return {"paths": list(res or [])}

    @_safe
    def task_run(self, task_id: str, paths=None, opts=None):
        spec = TASKS.get(task_id)
        if not spec:
            return {"error": "That task doesn't exist."}
        opts = dict(opts or {})
        files, _ = _check_files(spec, paths)
        if spec.pick in ("files", "folders"):
            if not files:
                return {"error": f"Add some {spec.accepts} first."}
            if len(files) < spec.min_files:
                return {"error": f"Add at least {spec.min_files} {spec.accepts} - you have {len(files)}."}
        if _needs_addon(spec) and not _addon_status(spec.addon)["installed"]:
            return {"error": f"This needs the free {ADDONS[spec.addon]['name']} add-on - install it first."}
        for o in spec.options_for(files):           # fill anything the page didn't send
            opts.setdefault(o["key"], o["default"])
        return _start(Job(spec.id), _execute, spec, files, opts)

    @_safe
    def task_stop(self, job: str):
        j = _JOBS.get(job)
        if j:
            j.stop = True
        return {"ok": True}

    @_safe
    def task_status(self, job: str):
        j = _JOBS.get(job)
        if not j:
            return {"error": "That job has finished and been forgotten - run it again."}
        return j.snapshot()

    @_safe
    def rename_preview(self, paths=None, opts=None):
        files, note = _check_files(TASKS["rename"], paths)
        pairs = tools.rename_preview(files, _rename_opts(opts or {}))
        seen, rows = set(), []
        for old, new in pairs:
            clash = old != new and (new.exists() or new in seen)
            seen.add(new)
            rows.append({"old": old.name, "new": new.name, "changed": old != new, "clash": bool(clash)})
        return {"rows": rows, "changes": sum(1 for r in rows if r["changed"] and not r["clash"]), "rejected": note}

    @_safe
    def text_transform(self, text: str = "", mode: str = ""):
        real = TEXT_MODES[TEXT_LABELS.index(mode)] if mode in TEXT_LABELS else mode
        return {"text": tools.transform_text(text or "", real)}

    @_safe
    def addons(self):
        return {"addons": [_addon_status(k) for k in ADDONS]}

    @_safe
    def addon_install(self, key: str):
        if key not in ADDONS:
            return {"error": "Unknown add-on."}
        st = _addon_status(key)
        if st["installed"]:
            return {"installed": True}
        if not st["can_install"]:
            return {"error": f"Install {st['name']} from {st['url']}, then press Check again."}
        return _start(Job("install:" + key), _install, key)
