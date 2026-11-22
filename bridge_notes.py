"""
Notes for app.Bridge: a note on a highlight (kept in the PDF, as its comment - other PDF apps show it too), a highlight's
colour, the document's own notes box (kept in the per-document memory, not in the PDF), and exporting all of it as
Markdown or JSON.  Mixed into app.Bridge (relies on EditMixin's _page_ok/_annot/_done/_D, ReadMixin's _memory/_doc_key).

The export format is the same on the iPad/iPhone (ios/TidyPaper/NotesExport.swift).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import brand
from bridge_edit import ANNOT_NAMES
from bridge_util import UserError, fitz, hex_rgb, safe, unique

MARKUP = {fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKE_OUT, fitz.PDF_ANNOT_SQUIGGLY}
# what goes in an export: marks that carry words (the words they cover, or what was typed)
EXPORTED = MARKUP | {fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FREE_TEXT}
# names for colours in an export ("Highlight (green)"); the nearest one wins
COLOURS = {"yellow": "#ffd60a", "green": "#86efac", "pink": "#f9a8d4", "blue": "#93c5fd", "red": "#e11d48",
           "dark blue": "#2563eb", "black": "#111827", "dark green": "#16a34a", "orange": "#f97316", "navy": "#1a237e"}


def rgb_hex(c) -> str:
    if not c or len(c) < 3:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c[:3])


def colour_name(hexstr: str) -> str:
    if not hexstr:
        return ""
    r, g, b = hex_rgb(hexstr)
    best = min(COLOURS.items(), key=lambda kv: sum((x - y) ** 2 for x, y in zip((r, g, b), hex_rgb(kv[1]))))
    return best[0]


def words_under(pg, annot) -> str:
    """The words a highlight / underline / strike-through covers (word centres inside its quads)."""
    v = annot.vertices or []
    quads = [fitz.Quad(v[k:k + 4]).rect for k in range(0, len(v) - 3, 4)] or [annot.rect]
    out = []
    for w in pg.get_text("words"):
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        if any(q.x0 - 1 <= cx <= q.x1 + 1 and q.y0 - 1 <= cy <= q.y1 + 1 for q in quads):
            out.append(w[4])
    return " ".join(out)


def notes_markdown(d: dict) -> str:
    """The export as Markdown (d from NotesMixin._notes_data)."""
    out = [f"# Notes: {d['document']}", "", f"Exported from {d['app']} on {d['exported_label']}.", ""]
    if d["notes"]:
        out += ["## My notes", "", d["notes"].strip(), ""]
    if d["bookmarks"]:
        out += ["## Bookmarked pages", "", ", ".join(str(p) for p in d["bookmarks"]), ""]
    for g in d["pages"]:
        out += [f"## Page {g['page']}", ""]
        for it in g["items"]:
            kind = it["kind"].capitalize() + (f" ({it['colour']})" if it.get("colour") and it["kind"] != "note" else "")
            if it["kind"] in ("note", "text box"):
                out.append(f"- {kind}: {one_line(it['text'])}")
            else:
                out.append(f"- {kind}: “{one_line(it['text'])}”")
            if it.get("note"):
                out.append(f"  - Note: {one_line(it['note'])}")
        out.append("")
    if not (d["notes"] or d["bookmarks"] or d["pages"]):
        out += ["(No notes, highlights or bookmarks yet.)", ""]
    return "\n".join(out).rstrip() + "\n"


def one_line(s: str) -> str:
    return " ".join((s or "").split())


class NotesMixin:

    # ---- a highlight's note and colour
    @safe
    def annot_note(self, i: int, xref: int, text: str):
        """Add, change or (empty text) remove the note on a highlight / underline / strike-through."""
        with self._lock:
            self._page_ok(i)
            a = self._annot(i, xref)
            if a.type[0] not in MARKUP:
                raise UserError("Only highlights, underlines and strike-throughs have notes - use Edit text for this.")
            text = (text or "").strip()
            had = bool((a.info.get("content") or "").strip())
            if text == (a.info.get("content") or "").strip():
                return self._done(None, "Nothing changed.")
            self._model.set_annot_text(i, a, text)
            return self._done(i, "Note removed." if not text else "Note changed." if had else
                              "Note added. Point at the highlight to read it.")

    @safe
    def annot_colour(self, i: int, xref: int, colour: str):
        """Give a highlight / underline / strike-through another colour (one undo step)."""
        with self._lock:
            m = self._page_ok(i)
            a = self._annot(i, xref)
            if a.type[0] not in MARKUP:
                raise UserError("Only highlights, underlines and strike-throughs can change colour.")
            m.snapshot()
            a = self._annot(i, xref)                 # re-fetch after snapshot
            a.set_colors(stroke=hex_rgb(colour, (1, 0.85, 0.1)))
            a.update()
            return self._done(i, "Colour changed.")

    # ---- the document's notes box
    @safe
    def doc_notes(self, text: str | None = None):
        """Read (text=None) or keep the document's notes.  Nothing is kept for password-protected documents."""
        if not self._doc_key:
            return {"notes": "", "remembers": False}
        if text is not None:
            d = self._memory().update(self._doc_key, notes=str(text))
        else:
            d = self._memory().load(self._doc_key)
        return {"notes": d.get("notes", ""), "remembers": True}

    # ---- export
    def _notes_data(self) -> dict:
        with self._lock:
            m = self._need_doc()
            pages = []
            for i in range(len(m)):
                pg = m.page(i)
                items = []
                for a in pg.annots() or []:
                    t = a.type[0]
                    if t not in EXPORTED or a.flags & fitz.PDF_ANNOT_IS_HIDDEN:
                        continue
                    content = (a.info.get("content") or "").strip()
                    if t in MARKUP:
                        item = {"kind": ANNOT_NAMES.get(t, "mark"), "text": words_under(pg, a), "note": content}
                    else:
                        if not content:
                            continue
                        item = {"kind": ANNOT_NAMES.get(t, "note"), "text": content}
                    c = rgb_hex(a.colors.get("stroke") or a.colors.get("fill"))
                    if c and t != fitz.PDF_ANNOT_FREE_TEXT:
                        item["colour"] = colour_name(c)
                    items.append(((a.rect.y0, a.rect.x0), item))
                items.sort(key=lambda yi: yi[0])          # reading order on the page
                if items:
                    pages.append({"page": i + 1, "items": [it for _, it in items]})
            name = (self._source or m.path or Path("Document.pdf")).name
        mem = self._memory().load(self._doc_key) if self._doc_key else {}
        now = datetime.now()
        return {"app": brand.NAME, "document": name, "exported": now.isoformat(timespec="seconds"),
                "exported_label": f"{now.day} {now:%b %Y}", "notes": mem.get("notes", ""),
                "bookmarks": [p + 1 for p in mem.get("bookmarks", [])], "pages": pages}

    @safe
    def export_notes(self, fmt: str = "md", path: str | None = None):
        """Save the document's notes, highlights and bookmarks next to it as '<name> - notes.md' (or .json)."""
        fmt = "json" if fmt == "json" else "md"
        d = self._notes_data()
        if fmt == "json":
            body = json.dumps({k: v for k, v in d.items() if k != "exported_label"}, indent=2, ensure_ascii=False) + "\n"
        else:
            body = notes_markdown(d)
        if path:
            target = Path(path)
        else:
            src = self._source or self._model.path or (Path.home() / "Documents" / "Document.pdf")
            stem = src.stem[:-len("_edited")] if src.stem.endswith("_edited") else src.stem
            target = unique(src.with_name(f"{stem} - notes.{fmt}"))
        try:
            target.write_text(body, encoding="utf-8")
        except OSError:
            target = unique(Path.home() / "Documents" / target.name)
            target.write_text(body, encoding="utf-8")
        items = [it for g in d["pages"] for it in g["items"]]
        marks = sum(it["kind"] not in ("note", "text box") for it in items)
        notes = len(items) - marks
        parts = [f"{k} {w}{'s' if k != 1 else ''}" for k, w in ((marks, "highlight"), (notes, "note")) if k]
        what = " and ".join(parts) if parts else "your notes"
        return {"saved": str(target), "count": len(items), "status": f"Exported {what} as {target.name}."}
