"""Translate's "Save PDF": the translation as a new, tidy A4 PDF - "<document> - <language>.pdf".

Each translated page starts a new page of the PDF, under a quiet "Page N" marker (the original page's number), laid
out as the AI marked it: the prompt
asks for "# " / "## " before headings, "- " before bullet points and "cell | cell" table rows - these become real
headings, bullet lists and tables; other lines as they came back; right to left where the words are (Arabic, Farsi,
Urdu...); a lone number at the end (the page's own page number) is left out.  The page's pictures come along
(`page_pictures` / `picture_places`): one above all the page's words (a letterhead's logo) at the top of its
translated page, others (a signature, a photo) after its words - on the same side, at the same size; a picture
covering most of the page (a scan) is left out.  "<document> - <language> · translated
by AI" and "N of M" at the foot of every page.  `plain` turns the marks into plain
text for Copy and Save text.

Laid out with PyMuPDF's Story (MuPDF's HTML layout): it shapes every script the languages list has - Arabic and
Farsi joined right to left, Devanagari / Bengali / Tamil / Gujarati / Gurmukhi conjuncts, Thai, Chinese, Japanese,
Korean - from fonts built into PyMuPDF, so it looks the same on a Mac and a PC whatever fonts are installed, and needs
no window (unlike webpage_pdf.py, whose browser printing was tried first: its text layer is no better for these
scripts and on Windows it depends on WebView2's printing and the PC's fonts).  The words can be selected; for the
joined scripts (Arabic, the Indian scripts, Thai) a copied word can come out wrong - Copy all / Save as text give the
exact text.

`blocks` and `html` are ported to the iPad/iPhone (ios/TidyPaper/TranslatePDF.swift, which lays the same HTML out
with UIKit's printing); TranslateTests compares them with Fixtures/translate.json, written by this code.
"""

from __future__ import annotations

import html as _html
import io
import re
import unicodedata
from datetime import date as _date
from pathlib import Path

import pymupdf as fitz

from brand import NAME as APP_NAME

A4 = fitz.paper_rect("a4")
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 56, 56, 64
FOOT = 34                       # the footer line's baseline, from the bottom edge

# The same on both platforms (TranslatePDF.swift): MuPDF has no Georgia and falls back to its own serif.
CSS = """
body { font-family: Georgia, serif; font-size: 11pt; line-height: 1.45; color: #1d1d1f; font-variant-numeric: lining-nums; }
p.pg { font-family: Helvetica, Arial, sans-serif; font-size: 8pt; color: #86868b; text-align: right;
       border-bottom: 0.5pt solid #e5e5ea; padding-bottom: 2pt; margin: 0 0 10pt 0; page-break-before: always; }
p.pg.first { page-break-before: auto; }
h2 { font-family: Helvetica, Arial, sans-serif; font-size: 15pt; font-weight: bold; line-height: 1.3; margin: 8pt 0 5pt 0; }
h3 { font-family: Helvetica, Arial, sans-serif; font-size: 12pt; font-weight: bold; line-height: 1.3; margin: 8pt 0 4pt 0; }
p { margin: 0 0 7pt 0; }
p.pic { margin: 0 0 8pt 0; line-height: 1; }
table.pics { margin: 0 0 8pt 0; }
table.pics td { border: none; padding: 0; vertical-align: top; line-height: 1; }
p.note { font-family: Helvetica, Arial, sans-serif; font-size: 9pt; color: #6e6e73; }
ul { margin: 0 0 7pt 0; padding-left: 18pt; }
li { margin: 0 0 3pt 0; }
p.li { margin: 0 0 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 2pt 0 8pt 0; }
td { border: 0.75pt solid #d2d2d7; padding: 2pt 4pt; font-size: 10pt; vertical-align: top; }
"""

_RTL = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")      # Hebrew, Arabic, Syriac, Thaana, N'Ko...
_CUT = "[The translation of this page was cut short"                  # bridge_translate's own note


def is_rtl(s: str) -> bool:
    """Right to left when the first letter is (as a browser's dir="auto")."""
    for ch in s:
        if unicodedata.category(ch) in ("Lu", "Ll", "Lt", "Lm", "Lo"):
            return bool(_RTL.match(ch))
    return False


def _cells(line: str) -> list[str] | None:
    """A table row the way the prompt asks for one ("cell | cell"), else None."""
    if " | " not in line and not (line.startswith("|") and line.endswith("|") and line.count("|") > 2):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells if len(cells) > 1 else None


_HEADING = re.compile(r"(#{1,6})(?:\s+(.*))?")      # a lone "#" is dropped
_BULLET = re.compile(r"[-*•–]\s+(.*)")
_PAGE_NO = re.compile(r"[0-9]{1,4}")


def _line(raw: str) -> str:
    """A line with its spaces tidied and Markdown's bold / underline marks taken off (the prompt asks for none)."""
    return " ".join(raw.replace("**", "").replace("__", "").split())


def blocks(text: str) -> list[dict]:
    """A page's translation as blocks, each with "rtl":
    {"kind": "heading", "level": 1 or 2, "text"} ("# " / "## " and deeper),
    {"kind": "list", "items": [...]} (lines starting "- ", "* ", "• " or "– ", one after another),
    {"kind": "table", "rows": [[cell, ...], ...]} ("cell | cell" lines; a row of only dashes is left out),
    {"kind": "lines", "lines": [...]} (a paragraph: other lines as they came; a blank line ends it)."""
    out: list[dict] = []
    cur: dict | None = None
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _line(raw)
        if not line:
            cur = None
            continue
        h = _HEADING.fullmatch(line)
        b = None if h else _BULLET.fullmatch(line)
        cells = None if h or b else _cells(line)
        if h:
            if h[2]:
                out.append({"kind": "heading", "level": 1 if len(h[1]) == 1 else 2, "text": h[2]})
            cur = None
        elif b:
            if cur is None or cur["kind"] != "list":
                cur = {"kind": "list", "items": []}
                out.append(cur)
            cur["items"].append(b[1])
        elif cells is not None:
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells):
                continue
            if cur is None or cur["kind"] != "table":
                cur = {"kind": "table", "rows": []}
                out.append(cur)
            cur["rows"].append(cells)
        else:
            if cur is None or cur["kind"] != "lines":
                cur = {"kind": "lines", "lines": []}
                out.append(cur)
            cur["lines"].append(line)
    out = _paragraphs(_real_tables(out))
    if out and out[-1]["kind"] == "lines" and len(out[-1]["lines"]) == 1 and _PAGE_NO.fullmatch(out[-1]["lines"][0]):
        out.pop()                     # the page's own page number, which the AI was asked to leave out
    for b in out:
        words = {"heading": lambda: b["text"], "list": lambda: " ".join(b["items"]), "lines": lambda: " ".join(b["lines"]),
                 "table": lambda: " ".join(" ".join(r) for r in b["rows"])}[b["kind"]]()
        b["rtl"] = is_rtl(words)
    return out


PARAGRAPH = 80          # characters: a line this long is a paragraph of its own (the AI may leave out the blank lines)


def _paragraphs(out: list[dict]) -> list[dict]:
    """Lines one after another: each long one (a paragraph written on one line) is a paragraph of its own; short ones
    side by side (a date, "To:", an address) stay together."""
    res: list[dict] = []
    for b in out:
        if b["kind"] != "lines":
            res.append(b)
            continue
        group: list[str] = []
        for line in b["lines"]:
            if len(line) >= PARAGRAPH:
                if group:
                    res.append({"kind": "lines", "lines": group})
                    group = []
                res.append({"kind": "lines", "lines": [line]})
            else:
                group.append(line)
        if group:
            res.append({"kind": "lines", "lines": group})
    return res


def _real_tables(out: list[dict]) -> list[dict]:
    """A "table" is only a real one with 2 rows or more, most of them (2 in 3) with the same number of cells - a line
    of small print with | between items (a letterhead: "Tekvibes Limited | Registered in England and Wales | Company
    no. …") stays as its lines of text."""
    res: list[dict] = []
    for b in out:
        if b["kind"] == "table":
            counts = [len(r) for r in b["rows"]]
            common = max(counts.count(c) for c in counts)
            if len(counts) < 2 or common * 3 < len(counts) * 2:
                b = {"kind": "lines", "lines": [" | ".join(r) for r in b["rows"]]}
        res.append(b)
    return res


def plain(text: str) -> str:
    """The translation as plain text (Copy, Save text): headings without their "#", bullet points with "• "."""
    out = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _line(raw)
        h = _HEADING.fullmatch(line)
        b = None if h else _BULLET.fullmatch(line)
        out.append((h[2] or "") if h else "• " + b[1] if b else line)
    return "\n".join(out).strip()


def pages_phrase(pages: list[int]) -> str:
    """0-based indexes -> "pages 1-3, 5 and 8" (1-based)."""
    nums = sorted({i + 1 for i in pages})
    runs: list[str] = []
    k = 0
    while k < len(nums):
        j = k
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        runs.append(str(nums[k]) if j == k else f"{nums[k]}-{nums[j]}")
        k = j + 1
    if not runs:
        return ""
    word = "page" if len(nums) == 1 else "pages"
    return f"{word} {runs[0]}" if len(runs) == 1 else f"{word} {', '.join(runs[:-1])} and {runs[-1]}"


def long_date(d: _date | None = None) -> str:
    d = d or _date.today()
    return f"{d.day} {d:%B} {d.year}"


def title(name: str) -> str:
    """The document's name without its ".pdf" (or "_edited") - as the file name is made."""
    name = (name or "").strip() or "Document"
    stem = name.rsplit(".", 1)[0] if re.search(r"\.[A-Za-z0-9]{1,5}$", name) else name
    return stem[:-len("_edited")] if stem.endswith("_edited") else stem


CONTENT_W = A4.width - 2 * MARGIN_X - 24          # the width a picture may take (the Story's body has 12pt sides)


def picture_places(page_w: float, page_h: float, text_top: float | None, rects: list) -> list[dict]:
    """Where a page's pictures go in its translation (shared with the iPad: TranslatePDF.picturePlaces).  rects:
    (x0, y0, x1, y1) from the page's top-left, in points; text_top: the top of its first line of words (None: none).
    -> [{"rect", "where": "top" | "end", "align": "left" | "center" | "right", "width", "height"}] in reading order:
    "top" when it is wholly above the words (a letterhead), else "end" (after them).  Pictures under 12pt a side or
    400 square points are left out (rules, dots), and so is one covering half the page or more (a scan); at most 8."""
    out = []
    for r in sorted((tuple(float(v) for v in r) for r in rects), key=lambda r: (round(r[1], 1), round(r[0], 1))):
        x0, y0, x1, y1 = r
        w, h = x1 - x0, y1 - y0
        if min(w, h) < 12 or w * h < 400 or w * h >= 0.5 * page_w * page_h:
            continue
        cx = (x0 + x1) / 2
        scale = min(1.0, CONTENT_W / w)              # wider than the text: as wide as the text, in the middle
        align = "center" if scale < 1 or abs(cx - page_w / 2) < page_w * 0.12 else "left" if cx < page_w / 2 else "right"
        out.append({"rect": [round(v, 2) for v in r], "where": "top" if text_top is None or y1 <= text_top + 2 else "end",
                    "align": align, "width": round(w * scale, 1), "height": round(h * scale, 1)})
        if len(out) == 8:
            break
    return out


def page_pictures(page: "fitz.Page", index: int) -> list[dict]:
    """A page's pictures for its translation: picture_places + "src" and "png" (the picture as it looks on the page,
    at 200 dpi)."""
    words = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
    text_top = min((b[1] for b in words), default=None)
    rects = [tuple(i["bbox"]) for i in page.get_image_info()]
    out = []
    for k, place in enumerate(picture_places(page.rect.width, page.rect.height, text_top, rects), 1):
        clip = fitz.Rect(place["rect"]) & page.rect
        if clip.is_empty:
            continue
        zoom = min(200 / 72, 2400 / max(clip.width, clip.height))
        place["png"] = page.get_pixmap(clip=clip, matrix=fitz.Matrix(zoom, zoom), alpha=False).tobytes("png")
        place["src"] = f"p{index}-{k}.png"
        out.append(place)
    return out


def _img(pic: dict) -> str:
    return f'<img src="{pic["src"]}" style="width:{pic["width"]}pt; height:{pic["height"]}pt">'


def _pictures(pics: list[dict]) -> list[str]:
    """Pictures side by side on the page (their heights overlap) share a row: one alone is a line of its own on its
    side; several are a row of left / centre / right cells."""
    rows: list[list[dict]] = []
    for x in pics:
        if rows and x["rect"][1] < max(r["rect"][3] for r in rows[-1]) - 2:
            rows[-1].append(x)
        else:
            rows.append([x])
    out = []
    for row in rows:
        if len(row) == 1:
            out.append(f'<p class="pic" style="text-align:{row[0]["align"]}">{_img(row[0])}</p>')
        else:
            cells = "".join(f'<td style="text-align:{a}">' + " ".join(_img(x) for x in row if x["align"] == a) + "</td>"
                            for a in ("left", "center", "right"))
            out.append(f'<table class="pics"><tr>{cells}</tr></table>')
    return out


def html(name: str, language: str, when: str, pages: dict[int, str], total: int, pictures: dict | None = None) -> str:
    """The body of the translated PDF (the same on both platforms; CSS above).  pages: 0-based index -> its
    translation; blank ones are left out.  (name, when, total: kept for the callers; the PDF's own top line went -
    the user asked for none; the footer says it's an AI translation.)"""
    e = lambda s: _html.escape(s, quote=True)  # noqa: E731
    done = {i: t for i, t in sorted(pages.items()) if (t or "").strip()}
    parts = []
    pictures = pictures or {}
    for i, text in done.items():
        parts.append(f'<p class="pg{"" if parts else " first"}">Page {i + 1}</p>')       # each starts a new page
        parts += _pictures([x for x in pictures.get(i, []) if x["where"] == "top"])
        for b in blocks(text):
            d = ' dir="rtl"' if b["rtl"] else ""
            if b["kind"] == "heading":
                tag = "h2" if b["level"] == 1 else "h3"
                parts.append(f"<{tag}{d}>{e(b['text'])}</{tag}>")
            elif b["kind"] == "list":
                if b["rtl"]:          # MuPDF keeps a list's bullets on the left: paragraphs starting with one instead
                    parts.append("".join(f'<p class="li" dir="rtl">• {e(x)}</p>' for x in b["items"]))
                else:
                    parts.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in b["items"]) + "</ul>")
            elif b["kind"] == "table":
                # right to left: the first cell on the right (MuPDF doesn't turn a table round for dir="rtl")
                width = max(len(r) for r in b["rows"])
                rows = [r + [""] * (width - len(r)) for r in b["rows"]]
                rows = [r[::-1] for r in rows] if b["rtl"] else rows
                parts.append("<table>" + "".join("<tr>" + "".join(f"<td{d}>{e(c)}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
            else:
                note = len(b["lines"]) == 1 and b["lines"][0].startswith(_CUT)
                c = ' class="note"' if note else d
                parts.append(f"<p{c}>" + "<br>".join(e(x) for x in b["lines"]) + "</p>")
        parts += _pictures([x for x in pictures.get(i, []) if x["where"] == "end"])
    return "\n".join(parts)


def footer(name: str, language: str) -> str:
    return f"{title(name)} - {language} · translated by AI"


def file_name(name: str, language: str) -> str:
    """"<document> - <language>.pdf", without characters a file name can't have."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", f"{title(name)} - {language}").strip(" .")
    return (s or "Translation")[:150] + ".pdf"


def make_pdf(out: Path, name: str, language: str, pages: dict[int, str], total: int, when: str | None = None,
             pictures: dict | None = None) -> Path:
    """Write the translated PDF to `out`.  Made in memory and written once by Python: on Windows, PyMuPDF's
    DocumentWriter kept its file open after close(), so a temporary file beside it couldn't be deleted ("being used
    by another process" - 5 Nov, the first try on a PC)."""
    body = html(name, language, when or long_date(), pages, total, pictures)
    if 'class="pg' not in body:
        raise ValueError("nothing translated")
    out = Path(out)
    archive = fitz.Archive()
    for pics in (pictures or {}).values():
        for x in pics:
            archive.add(x["png"], x["src"])
    story = fitz.Story(body, user_css=CSS, archive=archive)
    where = fitz.Rect(MARGIN_X, MARGIN_TOP, A4.width - MARGIN_X, A4.height - MARGIN_BOTTOM)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more = 1
    while more:
        dev = writer.begin_page(A4)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    doc = fitz.open("pdf", buf.getvalue())
    try:
        foot = _html.escape(footer(name, language))
        n = doc.page_count
        css = "* { font-family: Helvetica, Arial, sans-serif; font-size: 8pt; color: #86868b; }"
        for k, page in enumerate(doc):
            y = A4.height - FOOT
            page.insert_htmlbox(fitz.Rect(MARGIN_X, y - 10, A4.width - MARGIN_X - 60, y + 4), foot, css=css)
            page.insert_htmlbox(fitz.Rect(A4.width - MARGIN_X - 60, y - 10, A4.width - MARGIN_X, y + 4),
                                f'<p style="text-align:right">{k + 1} of {n}</p>', css=css)
        doc.set_metadata({"title": f"{title(name)} - {language}", "creator": APP_NAME, "producer": APP_NAME,
                          "subject": f"Translated into {language}"})
        data = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    out.write_bytes(data)
    return out
