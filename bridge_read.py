"""
Reading comfort for app.Bridge: per-document memory (page, zoom, bookmarks), the contents list, search with options and
results by page, and the document's details.  Mixed into app.Bridge; every public method returns a dict (see app.py).
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from bridge_util import log, safe
from docmemory import DocMemory, fingerprint
from pdf_editor import fitz

# named paper sizes, in points (portrait), for the details box
PAPER = {"A3": (842, 1191), "A4": (595, 842), "A5": (420, 595), "Letter": (612, 792), "Legal": (612, 1008),
         "Tabloid": (792, 1224)}


def paper_name(w: float, h: float) -> str:
    """'A4 (210 × 297 mm)', 'Letter (8.5 × 11 in)', or just the size."""
    a, b = sorted((w, h))
    for name, (pw, ph) in PAPER.items():
        if abs(a - pw) <= 3 and abs(b - ph) <= 3:
            land = " landscape" if w > h else ""
            if name in ("Letter", "Legal", "Tabloid"):
                return f"{name}{land} ({w / 72:.4g} × {h / 72:.4g} in)"
            return f"{name}{land} ({round(w / 72 * 25.4)} × {round(h / 72 * 25.4)} mm)"
    return f"{round(w / 72 * 25.4)} × {round(h / 72 * 25.4)} mm"


def pdf_date(s: str) -> str:
    """'D:20231105143000+01'00'' -> '5 Nov 2023, 14:30' ('' when missing or unreadable)."""
    m = re.match(r"(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?", s or "")
    if not m:
        return ""
    try:
        parts = [int(g) if g else d for g, d in zip(m.groups(), (0, 1, 1, 0, 0))]
        t = datetime(*parts)
    except ValueError:
        return ""
    return f"{t.day} {t:%b %Y}" + (f", {t:%H:%M}" if m.group(4) else "")


def file_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return ""


# ---------------------------------------------------------------------------------------------------- search
def page_chars(pg) -> tuple[str, list]:
    """The page's words as one string, and for each character (line id, box) - None for the space put between lines."""
    raw = pg.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP)
    text, boxes, line = [], [], 0
    for b in raw.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            line += 1
            for sp in ln.get("spans", []):
                for c in sp.get("chars", []):
                    text.append(c["c"])
                    boxes.append((line, c["bbox"]))
            text.append(" ")
            boxes.append(None)
    return "".join(text), boxes


def query_pattern(q: str, match_case: bool, whole_words: bool) -> re.Pattern:
    """Spaces in the query match any run of spaces / line breaks."""
    body = r"\s+".join(re.escape(w) for w in q.split())
    if whole_words:
        if re.match(r"\w", q):
            body = r"(?<!\w)" + body
        if re.search(r"\w$", q):
            body += r"(?!\w)"
    return re.compile(body, 0 if match_case else re.IGNORECASE)


def snippet(text: str, a: int, b: int, around: int = 40) -> dict:
    """The words around a match: {'before', 'match', 'after'} on one line, cut at word edges."""
    clean = lambda s: re.sub(r"\s+", " ", s)
    before, after = text[max(0, a - around):a], text[b:b + around]
    if a - around > 0 and " " in before:
        before = "…" + before[before.index(" ") + 1:]
    if b + around < len(text) and " " in after:
        after = after[:after.rindex(" ")] + "…"
    return {"before": clean(before).lstrip(), "match": clean(text[a:b]), "after": clean(after).rstrip()}


def match_rects(boxes: list, a: int, b: int) -> list:
    """One rectangle per line the match touches (spaces left out: their boxes can be odd)."""
    lines: dict[int, fitz.Rect] = {}
    for k in range(a, b):
        e = boxes[k]
        if e is None:
            continue
        ln, bb = e
        r = fitz.Rect(bb)
        if r.is_empty and r.width <= 0:
            continue
        lines[ln] = lines[ln] | r if ln in lines else r
    return list(lines.values())


class ReadMixin:
    _mem: DocMemory | None = None
    _doc_key: str | None = None

    def _memory(self) -> DocMemory:
        import app   # CONFIG_DIR is read at call time, so the selftests' temp home is used
        if self._mem is None or self._mem.dir.parent != app.CONFIG_DIR:
            self._mem = DocMemory(app.CONFIG_DIR)
        return self._mem

    def _read_opened(self) -> dict:
        """Called when a document is adopted: work out its fingerprint (not for password-protected files) and
        return what's remembered about it."""
        self._doc_key = None
        src = self._source
        if self._password or not src or not Path(src).is_file():
            return {}
        try:
            self._doc_key = fingerprint(src)
        except OSError:
            log.exception("fingerprint")
            return {}
        mem = self._memory().load(self._doc_key)
        self._carry_on(mem)            # the card on Home is about the last document opened
        return mem

    def _read_saved(self, path: Path):
        """A saved copy starts with the same memory (unless it's locked)."""
        if self._doc_key and not self._password:
            try:
                self._memory().copy(self._doc_key, fingerprint(path))
            except OSError:
                log.exception("copy memory")

    def _bookmarks_after(self, op: str, i: int, to: int | None = None):
        """Keep bookmarks on the same pages when pages are deleted, copied, added or moved."""
        mem = self._memory().load(self._doc_key) if self._doc_key else {}
        marks = mem.get("bookmarks")
        if not marks:
            return
        out = []
        for p in marks:
            if op == "delete":
                if p == i:
                    continue
                p -= p > i
            elif op in ("duplicate", "blank"):
                p += p > i
            elif op == "move" and to is not None:
                if p == i:
                    p = to
                elif i < p <= to:
                    p -= 1
                elif to <= p < i:
                    p += 1
            out.append(p)
        self._memory().update(self._doc_key, bookmarks=out)

    @safe
    def doc_remember(self, values: dict):
        """The page saves what it wants kept about this document: {page, zoom, fit, bookmarks, spread}."""
        if not self._doc_key or not isinstance(values, dict):
            return {"remembered": False}
        d = self._memory().update(self._doc_key, **values)
        if "page" in values and self._source:
            self._carry_on(d)
        return {"remembered": True, "memory": d}

    @safe
    def doc_memory(self):
        """What's remembered about the open document ({} if nothing, or it's password-protected)."""
        return {"memory": self._memory().load(self._doc_key) if self._doc_key else {}, "remembers": bool(self._doc_key)}

    @safe
    def fullscreen(self, on: bool, now_full: bool = False):
        """Focus mode: the window fills the screen.  pywebview only toggles and doesn't say which way it is, so the
        page says whether the window fills the screen now (it may have been changed with the window's own button).
        Nothing to do in developer mode, which has no window."""
        w = getattr(self, "_window", None)
        if w is not None and bool(on) != bool(now_full):
            w.toggle_fullscreen()
        return {"fullscreen": bool(on)}

    def _carry_on(self, mem: dict):
        """Home's "Carry on reading" card: the last document read, the page, and how to tell the file is unchanged."""
        src = Path(self._source)
        try:
            st = src.stat()
        except OSError:
            return
        s = self._load_settings()
        s["carry_on"] = {"path": str(src), "name": src.name, "page": int(mem.get("page", 0)),
                         "pages": len(self._model) if self._model else 0, "size": st.st_size, "mtime": st.st_mtime,
                         "when": time.time()}
        self._save_settings(s)

    def _carry_on_card(self) -> dict | None:
        c = self._load_settings().get("carry_on")
        if not c or c.get("page", 0) < 1:
            return None
        p = Path(c.get("path", ""))
        try:
            st = p.stat()
        except OSError:
            return None
        if st.st_size != c.get("size") or abs(st.st_mtime - c.get("mtime", 0)) > 1:
            return None
        return {"path": str(p), "name": c["name"], "page": c["page"], "pages": c.get("pages", 0)}

    @safe
    def forget_carry_on(self):
        s = self._load_settings()
        s.pop("carry_on", None)
        self._save_settings(s)
        return {"ok": True}

    # ---- contents list
    @safe
    def outline(self):
        """The PDF's own contents list: [{level, title, page (0-based) or None, y (displayed-page points) or None}]."""
        with self._lock:
            m = self._need_doc()
            out = []
            for lvl, title, pno, dest in m.doc.get_toc(simple=False):
                page = pno - 1 if 1 <= pno <= len(m) else None
                y = None
                to = dest.get("to") if isinstance(dest, dict) else None
                if page is not None and to is not None and dest.get("kind") == fitz.LINK_GOTO:
                    pg = m.doc[page]
                    q = fitz.Point(to) * pg.rotation_matrix
                    y = round(max(0.0, min(q.y, pg.rect.height)), 1)
                out.append({"level": max(1, int(lvl)), "title": (title or "").strip() or "(untitled)",
                            "page": page, "y": y})
            return {"outline": out}

    # ---- search
    @safe
    def search(self, text: str, options: dict | None = None):
        """Every match as {page, rect, rects, snippet}; rects in displayed-page points (rotation applied).
        options: {match_case, whole_words}."""
        text = (text or "").strip()
        if not text:
            return {"hits": []}
        o = options or {}
        case, whole = bool(o.get("match_case")), bool(o.get("whole_words"))
        pat = query_pattern(text, case, whole)
        hits: list[dict] = []
        with self._lock:
            m = self._need_doc()
            for i, pg in enumerate(m.doc):
                quick = pg.search_for(text)       # fast, ignores case: finds the pages worth reading closely
                if not quick:
                    continue
                s, boxes = page_chars(pg)
                found = []
                for mt in pat.finditer(s):
                    rects = match_rects(boxes, mt.start(), mt.end())
                    if rects:
                        found.append((rects, snippet(s, mt.start(), mt.end())))
                if not found and not case and not whole:
                    found = [([r], None) for r in quick]     # MuPDF found it in a way the words don't show
                for rects, snip in found:
                    cv = [[round(v, 2) for v in m.rect_to_canvas(i, r, 1.0)] for r in rects]
                    hits.append({"page": i, "rect": cv[0], "rects": cv, "snippet": snip})
                if len(hits) >= 2000:
                    break
        return {"hits": hits[:2000]}

    # ---- details
    @safe
    def details(self):
        """Everything worth knowing about the open document, as label/value rows for a small dialog."""
        with self._lock:
            m = self._need_doc()
            md = m.doc.metadata or {}
            src = Path(self._source) if self._source else m.path
            disk = self._on_disk()
            rows = [("File name", src.name if src else "")]
            if self._converted:
                rows.append(("Opened as", f"A PDF copy of {self._converted['from']}"))
            for label, key in (("Title", "title"), ("Author", "author"), ("Subject", "subject"), ("Keywords", "keywords")):
                if (md.get(key) or "").strip():
                    rows.append((label, md[key].strip()))
            n = len(m)
            rows.append(("Pages", str(n)))
            sizes = {(round(pg.rect.width), round(pg.rect.height)) for pg in m.doc}
            first = m.doc[0].rect
            rows.append(("Page size", paper_name(first.width, first.height) + (" (some pages differ)" if len(sizes) > 1 else "")))
            if disk and Path(disk).is_file():
                rows.append(("File size", file_size(Path(disk).stat().st_size)))
            for label, key in (("Created", "creationDate"), ("Modified", "modDate")):
                if pdf_date(md.get(key, "")):
                    rows.append((label, pdf_date(md[key])))
            if (md.get("creator") or "").strip():
                rows.append(("Made with", md["creator"].strip()))
            if (md.get("producer") or "").strip():
                rows.append(("PDF made by", md["producer"].strip()))
            if md.get("format"):
                rows.append(("PDF version", md["format"].replace("PDF ", "")))
            rows.append(("Password", "Yes - needs a password to open" if self._password
                         else ("Has restrictions" if md.get("encryption") else "No")))
            sigs = self._signature_count()
            rows.append(("Digitally signed", f"Yes ({sigs} signature{'s' if sigs != 1 else ''})" if sigs else "No"))
            if any(True for pg in m.doc for _ in (pg.widgets() or [])):
                rows.append(("Form", "Has boxes to fill in"))
            if src:
                rows.append(("Folder", str(src.parent)))
            return {"rows": [{"label": a, "value": b} for a, b in rows]}
