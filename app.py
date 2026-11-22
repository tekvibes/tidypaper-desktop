#!/usr/bin/env python3
"""
The pywebview app (its name comes from brand.json).  The window shows ui/index.html; everything the page does goes
through Bridge (exposed to JavaScript as window.pywebview.api.*).

    py app.py [file]              open the app (and that PDF, Word file or picture in the editor)
    py app.py --serve [port]      developer mode: serve ui/ + the bridge on http://127.0.0.1:port
                                  so the page can be opened in an ordinary browser

Bridge rules: every public method returns a JSON-serialisable dict and never raises.
Failures come back as {"error": "one plain-English sentence"}; the traceback goes to ~/.toolkit/log.txt.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import itertools
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import brand  # noqa: E402
import convert  # noqa: E402
from bridge_util import log, nice_time, safe, unique  # noqa: E402
from pdf_editor import PdfModel, fitz  # noqa: E402
from bridge_ai import AiMixin  # noqa: E402
from bridge_edit import EditMixin  # noqa: E402
from bridge_fill import FillMixin  # noqa: E402
from bridge_sign import SignMixin  # noqa: E402
from bridge_more import MoreMixin  # noqa: E402
from bridge_read import ReadMixin  # noqa: E402
from bridge_notes import NotesMixin  # noqa: E402
from bridge_pictures import PicturesMixin  # noqa: E402
from bridge_open import OpenMixin  # noqa: E402
from bridge_scanwords import ScanWordsMixin  # noqa: E402
from bridge_scanedit import ScanEditMixin  # noqa: E402
from bridge_share import ShareMixin  # noqa: E402
from bridge_kept import KeptMixin  # noqa: E402
from bridge_extract import ExtractMixin  # noqa: E402
from bridge_translate import TranslateMixin  # noqa: E402
from scanner import ScanMixin  # noqa: E402
from taskrunner import TaskMixin  # noqa: E402

IS_WIN = platform.system() == "Windows"
CONFIG_DIR = Path.home() / ".toolkit"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOG_FILE = CONFIG_DIR / "log.txt"
UI_DIR = HERE / "ui"
MAX_RECENT = 8

def _setup_logging():
    CONFIG_DIR.mkdir(exist_ok=True)
    h = logging.FileHandler(LOG_FILE, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


# ------------------------------------------------------------------ settings

def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(s: dict):
    try:
        CONFIG_DIR.mkdir(exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception:
        log.exception("could not write settings")


def _saved_name(src: Path) -> Path:
    """Where 'Save' puts an edited copy: next to the original, with _edited, never overwriting anything."""
    stem = src.stem
    if stem.endswith("_edited"):
        stem = stem[: -len("_edited")]
    target = src.with_name(f"{stem}_edited.pdf")
    n = 2
    while target.exists():
        target = src.with_name(f"{stem}_edited ({n}).pdf")
        n += 1
    return target


# ------------------------------------------------------------------ bridge

class Tab:
    """One open document - a tab across the top of the window.  The Bridge's per-document attributes (_model,
    _source, _target... see TAB_STATE) are this object's, for the tab being worked on: the page names its tab in
    every call (tab_call), and a job's threads stay with the tab they were started for (Bridge._thread), so
    switching tabs mid-job can't send the wrong document's pages."""
    _ids = itertools.count(1)

    def __init__(self):
        self.id = f"t{next(Tab._ids)}"
        self.model: PdfModel | None = None
        self.source: Path | None = None      # the file the user opened (never overwritten)
        self.target: Path | None = None      # where Save writes in this session
        self.password: str | None = None     # the open file's password: saved copies stay protected with it
        self.converted: dict | None = None   # set when the editor shows a PDF copy of a Word file, picture...
        self.doc_key: str | None = None      # its fingerprint: what's remembered about it (bridge_read)
        self.kept_key: str | None = None     # unsaved changes kept under this key (bridge_kept)
        self.kept_timer: threading.Timer | None = None
        self.kept_offered = False
        self.garbled_pages: set = set()      # pages in an old font's junk letters (bridge_ai._texts)
        self.xtables = None                  # the table finder's last look (bridge_extract)
        self.ai_chat: list = []              # the Ask panel's conversation (bridge_ai)
        self.ai_turns: list = []
        self.ai_chat_doc = None
        self.scanjob: dict = {"running": False}   # making a scan's words selectable (bridge_scanwords)
        self.scanspot: dict = {}             # the word of a scan being changed, and its page's words (bridge_scanedit)


# the Bridge attributes that belong to the tab being worked on: Bridge._x is Tab.x
TAB_STATE = ("_model", "_source", "_target", "_password", "_converted", "_doc_key", "_kept_key", "_kept_timer",
             "_kept_offered", "_garbled_pages", "_xtables", "_ai_chat", "_ai_turns", "_ai_chat_doc", "_scanjob",
             "_scanspot")


class Bridge(KeptMixin, ExtractMixin, TranslateMixin, EditMixin, SignMixin, ShareMixin, AiMixin, FillMixin, TaskMixin, MoreMixin, ScanMixin, ReadMixin, NotesMixin, PicturesMixin, OpenMixin, ScanWordsMixin, ScanEditMixin):
    """Everything the web page can ask Python to do.  Names starting with _ are not exposed."""

    def __init__(self, start_files: list | None = None):
        self._window = None
        self._lock = threading.RLock()       # PyMuPDF documents are not thread-safe
        self._tabs: dict[str, Tab] = {}      # the open documents, in the order they were opened
        self._active: str | None = None      # the tab on screen
        self._blank = Tab()                  # "no document": what the per-tab attributes are with none open
        self._pin = threading.local()        # .tab: the tab this thread works on (a call from the page, a job)
        self._start_files = list(start_files or [])
        self._pending: PdfModel | None = None  # opened but waiting for a password

    # ---- tabs
    @property
    def _tab(self) -> Tab:
        """The tab this thread is working on: the one its call or job is for, else the one on screen."""
        t = getattr(self._pin, "tab", None)
        if t is not None:
            return t
        return self._tabs.get(self._active) or self._blank

    def _pinned(self, fn, tab: Tab | None = None):
        """fn, run for this tab whichever tab is on screen by then."""
        tab = tab or self._tab

        def go(*a, **kw):
            prev = getattr(self._pin, "tab", None)
            self._pin.tab = tab
            try:
                return fn(*a, **kw)
            finally:
                self._pin.tab = prev
        return go

    def _thread(self, fn, *args) -> threading.Thread:
        """A job's thread (not started): it keeps working on the tab it was made for."""
        return threading.Thread(target=self._pinned(fn), args=args, daemon=True)

    def tab_call(self, tab: str | None, name: str, args: list | None = None):
        """Every call from the page comes through here, naming the tab it's about (None: no document / the one on
        screen).  A tab closed meanwhile -> {"error", "closed": True}."""
        fn = getattr(self, name, None) if isinstance(name, str) else None
        if not fn or name.startswith("_") or name == "tab_call" or not callable(fn):
            return {"error": "Unknown action."}
        t = None
        if tab:
            t = self._tabs.get(tab)
            if t is None:
                return {"error": "That document was closed.", "closed": True}
        return self._pinned(fn, t)(*(args or [])) if t else fn(*(args or []))

    def _tab_list(self) -> list[dict]:
        out = []
        for t in self._tabs.values():
            m = t.model
            out.append({"tab": t.id, "name": (t.target or t.source or (m.path if m else None) or Path("Untitled.pdf")).name,
                        "dirty": bool(m and m.dirty)})
        return out

    @safe
    def tabs(self):
        """The open documents: [{tab, name, dirty}] and which is on screen."""
        return {"tabs": self._tab_list(), "active": self._active}

    @safe
    def tab_switch(self, tab: str):
        """Show another open document: its document_info, and what's remembered about it."""
        t = self._tabs.get(tab)
        if t is None:
            return {"error": "That document was closed.", "closed": True}
        if t is not self._tab:
            self._keep_now()                 # the tab being left: its unsaved changes are kept now (bridge_kept)
        self._active = t.id
        if getattr(self._pin, "tab", None) is not None:
            self._pin.tab = t
        with self._lock:
            mem = self._memory().load(t.doc_key) if t.doc_key else {}
            return {**self._info(), "memory": mem, "remembers": bool(t.doc_key)}

    def _tab_for(self, path: Path) -> Tab | None:
        """The open tab showing this file (as opened, or as saved since), if there is one."""
        try:
            want = path.resolve()
        except OSError:
            return None
        for t in self._tabs.values():
            for q in (t.source, t.target):
                try:
                    if q and Path(q).resolve() == want:
                        return t
                except OSError:
                    pass
        return None

    def _unsaved_tabs(self) -> list[Tab]:
        return [t for t in self._tabs.values() if t.model and t.model.dirty]

    def _keep_all(self):
        """Keep every open document's unsaved changes (closing the window, restarting for an update)."""
        for t in list(self._tabs.values()):
            self._pinned(self._keep_now, t)()

    # settings access for the mixins (module functions, so tests can redirect SETTINGS_FILE)
    def _load_settings(self):
        return load_settings()

    def _save_settings(self, s: dict):
        save_settings(s)

    # ---- app
    @safe
    def startup(self):
        s = load_settings()
        return {"theme": s.get("theme", "system"), "look": _look(s), "recent": self._recent(),
                "start_files": self._start_files, "version": brand.VERSION, "dev": self._window is None,
                "name": brand.NAME, "ai_name": brand.AI_NAME, "openable": sorted(convert.OPENABLE | {".pdf"}),
                "mac": platform.system() == "Darwin", "carry_on": self._carry_on_card()}

    @safe
    def theme_get(self):
        return {"theme": load_settings().get("theme", "system")}

    @safe
    def theme_set(self, theme: str):
        if theme not in ("system", "light", "dark"):
            return {"error": "Unknown theme."}
        s = load_settings(); s["theme"] = theme; save_settings(s)
        return {"theme": theme}

    @safe
    def look_set(self, look: str):
        """Settings › Look: one of the trial looks (ui/looks.css), or "classic" for the app as it was."""
        if look not in LOOKS:
            return {"error": "Unknown look."}
        s = load_settings(); s["look"] = look; save_settings(s)
        if self._window is not None:
            _mac_dock_icon(look)
        return {"look": look}

    def _recent(self):
        out = []
        for p in load_settings().get("recent", []):
            q = Path(p)
            if q.exists():
                st = q.stat()
                out.append({"path": str(q), "name": q.name, "folder": str(q.parent),
                            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y")})
        return out

    def _remember(self, path: Path):
        s = load_settings()
        rec = [p for p in s.get("recent", []) if Path(p) != path]
        s["recent"] = [str(path)] + rec[: MAX_RECENT - 1]
        save_settings(s)

    @safe
    def recent(self):
        return {"recent": self._recent(), "carry_on": self._carry_on_card()}

    @safe
    def forget_recent(self, path: str):
        s = load_settings()
        s["recent"] = [p for p in s.get("recent", []) if Path(p) != Path(path)]
        save_settings(s)
        return {"recent": self._recent()}

    # ---- files & folders
    @safe
    def pick_files(self, kind: str = "pdf", multiple: bool = False):
        if not self._window:
            return {"error": "File pickers only work in the app window."}
        import webview
        docs = ";".join("*" + e for e in sorted(convert.OPENABLE | {".pdf"}))
        types = {"pdf": ("PDF files (*.pdf)",), "any": ("All files (*.*)",),
                 "doc": (f"Documents and pictures ({docs})", "PDF files (*.pdf)", "All files (*.*)"),
                 "image": ("Pictures (*.png;*.jpg;*.jpeg;*.gif;*.bmp;*.webp;*.tif;*.tiff)",)}.get(kind, ("All files (*.*)",))
        res = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=multiple, file_types=types)
        return {"paths": list(res or [])}

    @safe
    def pick_folder(self):
        if not self._window:
            return {"error": "Folder pickers only work in the app window."}
        import webview
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return {"path": res[0] if res else None}

    @safe
    def show_in_folder(self, path: str):
        p = Path(path)
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return {"ok": True}

    @safe
    def open_external(self, path: str):
        """Open a file in its default program."""
        if IS_WIN:
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open" if platform.system() == "Darwin" else "xdg-open", path])
        return {"ok": True}

    @safe
    def file_info(self, paths: list):
        """Name / type for dropped files, so the home screen can suggest tasks."""
        out = []
        for p in paths:
            q = Path(p)
            out.append({"path": str(q), "name": q.name, "ext": q.suffix.lower(), "is_dir": q.is_dir(),
                        "exists": q.exists()})
        return {"files": out}

    # ---- document
    @safe
    def open_file(self, path: str | None = None):
        if not path:
            r = self.pick_files("doc")
            if "error" in r:
                return r
            if not r["paths"]:
                return {"cancelled": True}
            path = r["paths"][0]
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(path)
        open_already = self._tab_for(p)
        if open_already:                     # already open in a tab: show that one (with its unsaved changes)
            r = self.tab_switch(open_already.id)
            return {**r, "already": True, "status": f"{r.get('name', p.name)} is already open."}
        if p.suffix.lower() != ".pdf":
            if not convert.can_open(p):
                return {"error": f"{brand.NAME} can't open {p.suffix or 'this kind of'} files. "
                                 "Drop it on the home screen to see what it can do with it."}
            if self._window:
                self._window.evaluate_js(f"window.progress && window.progress({json.dumps('Opening ' + p.name + '…')})")
            c = convert.to_pdf(p, CONFIG_DIR / "opened")    # PDF copies live a few days; UserError -> {"error"}
            with self._lock:
                m = PdfModel(str(c["pdf"]))
                return self._adopt(m, source=p, converted={"from": p.name, "kind": c["kind"], "via": c["via"],
                                                            "text_only": c["text_only"]})
        with self._lock:
            m = PdfModel(str(p))
            if m.doc.needs_pass:
                self._pending = m
                return {"needs_password": True, "name": p.name}
            return self._adopt(m)

    @safe
    def unlock(self, password: str):
        with self._lock:
            m = self._pending
            if not m:
                return {"error": "There's no locked file waiting - open it again."}
            if not m.doc.authenticate(password or ""):
                return {"needs_password": True, "wrong": True, "name": m.path.name}
            self._pending = None
            return self._adopt(m, password or "")

    def _adopt(self, m: PdfModel, password: str | None = None, source: Path | None = None, converted: dict | None = None):
        """A document opened: in a new tab, which goes on screen (the others stay open as they are)."""
        self._keep_now()                     # the tab being left: its unsaved changes are kept now (bridge_kept)
        tab = Tab()
        self._tabs[tab.id] = tab
        self._active = tab.id
        if getattr(self._pin, "tab", None) is not None:
            self._pin.tab = tab               # the rest of this call is about the new tab
        self._model, self._source, self._target = m, source or m.path, None
        self._password = password or None
        self._converted = converted
        self._remember(self._source)
        memory = self._read_opened()
        kept = self._kept_after_open()
        log.info("opened %s (%d pages)%s", self._source, len(m), f" as a PDF copy via {converted['via']}" if converted else "")
        return {**self._info(), "memory": memory, "remembers": bool(self._doc_key), "kept": kept, "tabs": len(self._tabs)}

    def _on_disk(self) -> Path | None:
        """The PDF that's really on disk for this document (for email, checking signatures): the last save, else the
        original - but never the Word file / picture behind a PDF copy."""
        if self._target and self._target.exists():
            return self._target
        return None if self._converted else self._source

    def _need_doc(self):
        if not self._model:
            raise ValueError("No document is open.")
        return self._model

    def _signature_count(self) -> int:
        """Signed signature fields.  (PyMuPDF reports field_value '' for them, so look for the /V entry.)"""
        m = self._model
        n = 0
        for pg in m.doc:
            for w in pg.widgets() or []:
                if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and m.doc.xref_get_key(w.xref, "V")[0] != "null":
                    n += 1
        return n

    def _info(self):
        m = self._need_doc()
        pages = [{"w": round(pg.rect.width, 2), "h": round(pg.rect.height, 2), "rot": pg.rotation} for pg in m.doc]
        if m.dirty:
            self._kept_soon()             # keep the unsaved changes in a few seconds (bridge_kept)
        return {"tab": self._tab.id,
                "name": (self._target or self._source or m.path).name if (self._source or m.path) else "Untitled.pdf",
                "path": str(m.path) if m.path else None,
                "source": str(self._source) if self._source else None,
                "saved_to": str(self._target) if self._target else None,
                "pages": pages, "dirty": m.dirty,
                "can_undo": bool(m.undo_stack), "can_redo": bool(m.redo_stack),
                "signatures": self._signature_count(),
                "locked": bool(self._password),
                "has_forms": any(True for pg in m.doc for _ in (pg.widgets() or [])),
                "title": (m.doc.metadata or {}).get("title") or "",
                "converted": self._converted}

    @safe
    def document_info(self):
        with self._lock:
            return self._info()

    @safe
    def close_document(self):
        """Close this tab's document (its unsaved changes are kept unless "Don't save" threw them away).  Its jobs
        stop when they next need a page.  -> the tabs left; none is on screen until the page switches to one."""
        self._keep_now()
        with self._lock:
            t = self._tab
            if t is not self._blank:
                if t.model:
                    try:
                        t.model.doc.close()
                    except Exception:
                        pass
                t.model = t.source = t.target = t.password = t.converted = None
                t.doc_key = t.kept_key = t.xtables = None
                t.scanjob["stop"] = True
                self._tabs.pop(t.id, None)
                if self._active == t.id:
                    self._active = None
            return {"ok": True, "tabs": self._tab_list()}

    @safe
    def render_page(self, i: int, zoom: float):
        """Page i as a PNG data-URL, `zoom` device pixels per PDF point."""
        zoom = max(0.05, min(float(zoom), 8.0))
        with self._lock:
            m = self._need_doc()
            if not 0 <= i < len(m):
                return {"error": "That page doesn't exist."}
            png = m.render(i, zoom).tobytes("png")
        return {"page": i, "zoom": zoom, "src": "data:image/png;base64," + base64.b64encode(png).decode()}

    @safe
    def thumbnail(self, i: int, width: int = 120):
        with self._lock:
            m = self._need_doc()
            if not 0 <= i < len(m):
                return {"error": "That page doesn't exist."}
            w = m.page(i).rect.width or 1
            pix = m.render(i, width / w)
            data = pix.tobytes("jpeg", jpg_quality=80)
        return {"page": i, "src": "data:image/jpeg;base64," + base64.b64encode(data).decode()}

    @safe
    def signature_status(self):
        """Who signed the opened file (reads the file on disk, so call it right after opening)."""
        with self._lock:
            src = self._on_disk()
            if not src or not self._model or not self._signature_count():
                return {"signatures": []}
        import signing
        out = []
        for r in signing.verify_pdf(src):
            signer = r.get("signer", "")
            cn = next((part.split(":", 1)[1].strip() for part in signer.split(",") if part.strip().lower().startswith("common name")), signer)
            ok = r.get("intact") and r.get("modified_after") in ("NONE", "LTA_UPDATES", None)
            out.append({"signer": cn or "unknown", "when": nice_time(r.get("signed_at")), "ok": bool(ok),
                        "trusted": bool(r.get("trusted")), "error": r.get("error")})
        return {"signatures": out}

    # ---- edits
    @safe
    def undo(self):
        with self._lock:
            ok = self._need_doc().undo()
            return {**self._info(), "status": "Undone." if ok else "Nothing to undo."}

    @safe
    def redo(self):
        with self._lock:
            ok = self._need_doc().redo()
            return {**self._info(), "status": "Redone." if ok else "Nothing to redo."}

    @safe
    def page_op(self, op: str, i: int, arg=None):
        with self._lock:
            m = self._need_doc()
            n = len(m)
            if not 0 <= i < n:
                return {"error": "That page doesn't exist."}
            if op == "rotate":
                deg = int(arg or 90)
                m.rotate(i, deg)
                status = f"Page {i + 1} turned {'right' if deg > 0 else 'left'}."
            elif op == "delete":
                m.delete_page(i)
                status = f"Page {i + 1} deleted."
            elif op == "duplicate":
                m.duplicate_page(i)
                status = f"Page {i + 1} copied - the copy is page {i + 2}."
            elif op == "move":
                to = max(0, min(int(arg), n - 1))
                if to == i:
                    return {**self._info(), "status": ""}
                m.move_page(i, to)
                status = f"Page {i + 1} moved to position {to + 1}."
            elif op == "blank":
                m.insert_blank(i)
                status = f"Blank page added after page {i + 1}."
            elif op == "extract":
                src = self._source or m.path
                target = unique(src.with_name(f"{src.stem}_page{i + 1}.pdf"))
                m.extract_pages([i], str(target))
                return {**self._info(), "status": f"Page {i + 1} saved as {target.name}.", "saved": str(target)}
            else:
                return {"error": "Unknown page action."}
            if op in ("delete", "duplicate", "blank", "move"):
                self._bookmarks_after(op, i, to if op == "move" else None)
            return {**self._info(), "status": status}

    # ---- saving
    @safe
    def save(self):
        """Save an edited copy next to the original (Name_edited.pdf).  The original is never overwritten."""
        with self._lock:
            m = self._need_doc()
            if not self._target:
                base = self._source
                if base and CONFIG_DIR in base.parents:     # the sample tour's copy: not in the app's hidden folder
                    docs = Path.home() / "Documents"
                    base = (docs if docs.is_dir() else Path.home()) / base.name
                self._target = _saved_name(base) if base else None
            if not self._target:
                return self.save_as()
            m.save(self._target, user_pw=self._password or "")
            self._remember(self._target)
            self._read_saved(self._target)
            self._kept_saved(self._target)
            log.info("saved %s", self._target)
            note = " It still needs the same password to open." if self._password else ""
            return {**self._info(), "saved": str(self._target), "status": f"Saved as {self._target.name}.{note}"}

    @safe
    def save_as(self, path: str | None = None):
        with self._lock:
            m = self._need_doc()
            if not path:
                if not self._window:
                    return {"error": "Save As needs the app window."}
                import webview
                start = self._target or (_saved_name(self._source) if self._source else Path.home() / "Document.pdf")
                res = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(start.parent),
                                                      save_filename=start.name, file_types=("PDF files (*.pdf)",))
                if not res:
                    return {"cancelled": True}
                path = res if isinstance(res, str) else res[0]
            p = Path(path)
            if p.suffix.lower() != ".pdf":
                p = p.with_suffix(".pdf")
            m.save(p, user_pw=self._password or "")
            self._target = p
            self._remember(p)
            self._read_saved(p)
            self._kept_saved(p)
            note = " It still needs the same password to open." if self._password else ""
            return {**self._info(), "saved": str(p), "status": f"Saved as {p.name}.{note}"}

    # ---- window
    def _unsaved_message(self, what: str) -> str | None:
        """Closing the window / restarting with unsaved changes: one question, naming the documents."""
        dirty = self._unsaved_tabs()
        if not dirty:
            return None
        names = [(t.target or t.source or t.model.path).name for t in dirty]
        if len(names) == 1:
            msg = f"You have changes to {names[0]} that aren't saved. {what} anyway?"
        else:
            shown = names[:6] + ([f"{len(names) - 6} more"] if len(names) > 6 else [])
            msg = (f"You have changes that aren't saved in {len(names)} documents: " + ", ".join(shown[:-1])
                   + f" and {shown[-1]}. {what} anyway?")
        kept = [t for t in dirty if t.kept_key and not t.password]
        if kept:
            msg += ("\n\nThey're kept: open " + ("this document" if len(dirty) == 1 else "them")
                    + " again to put them back." if len(kept) == len(dirty) else
                    "\n\nChanges to documents without a password are kept: open them again to put them back.")
        return msg

    def _confirm_close(self) -> bool:
        """Called by the window's closing event.  True = let it close."""
        msg = self._unsaved_message(f"Close {brand.NAME}")
        ok = not msg or not self._window or bool(self._window.create_confirmation_dialog("Unsaved changes", msg))
        if ok:
            self._keep_all()
            self.update_on_exit()      # a downloaded update installs once the app has quit (bridge_more / updater.py)
        return ok

    def _on_drop(self, e):
        files = [f.get("pywebviewFullPath") for f in e.get("dataTransfer", {}).get("files", [])]
        files = [f for f in files if f]
        if files and self._window:
            self._window.evaluate_js(f"window.onFilesDropped && window.onFilesDropped({json.dumps(files)})")

    def _open_from_os(self, files: list[str]):
        """Files the system asked us to open while running (Mac: Finder's Open / Open With) - same path as a drop."""
        files = [str(Path(f).resolve()) for f in files if f]
        log.info("asked to open %s", ", ".join(Path(f).name for f in files))
        if not self._window:
            self._start_files += files
            return
        try:
            self._window.restore()
        except Exception:
            pass
        self._window.evaluate_js(f"window.onFilesOpened && window.onFilesOpened({json.dumps(files)})")


def _tab_attr(name: str) -> property:
    attr = name[1:]
    return property(lambda self: getattr(self._tab, attr), lambda self, v: setattr(self._tab, attr, v))


for _name in TAB_STATE:
    setattr(Bridge, _name, _tab_attr(_name))


def _window_size() -> tuple[int, int]:
    """1320 x 860, or on a Mac with a bigger screen, as much as fits (up to 1500 x 1100) - long lists like
    More tools then fit without scrolling."""
    w, h = 1320, 860
    if platform.system() == "Darwin":
        try:
            from AppKit import NSScreen
            f = NSScreen.mainScreen().visibleFrame()
            w = int(min(1500, max(w, f.size.width * 0.75)))
            h = int(min(1100, max(h, f.size.height - 60)))
        except Exception:
            log.exception("couldn't read the screen size")
    return w, h


LOOKS = ("classic", "highlighter", "teal", "midnight")
# the desk colour behind the page while the window loads, light / dark (ui/looks.css --desk)
LOOK_DESK = {"classic": ("#eceae6", "#17181a"), "highlighter": ("#f6f7fb", "#0e1526"),
             "teal": ("#eef5f2", "#0d1a18"), "midnight": ("#f4f2ff", "#0f1024")}


def _look(s: dict) -> str:
    return s.get("look") if s.get("look") in LOOKS else "classic"


def _mac_dock_icon(look: str):
    """On a Mac, the Dock shows the look's own icon (ui/looks/<look>-dock.png) while the app runs; "classic" puts the
    app's real icon back.  Finder and Launchpad keep the real icon - that's in the app bundle.  (Windows: no change.)"""
    if platform.system() != "Darwin":
        return
    try:
        import AppKit
        from PyObjCTools import AppHelper

        def change():
            img = None
            if look != "classic":
                img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(UI_DIR / "looks" / f"{look}-dock.png"))
            AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        AppHelper.callAfter(change)
    except Exception:
        log.exception("couldn't change the Dock icon")


def _mac_open_files(bridge: "Bridge"):
    """macOS hands files opened from Finder to the app delegate (the first ones at launch arrive in sys.argv via the
    build's argv_emulation).  pywebview's delegate doesn't take them, so add the method to it."""
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def application_openFiles_(self, app, filenames):
            threading.Thread(target=bridge._open_from_os, args=([str(f) for f in filenames],), daemon=True).start()
            app.replyToOpenOrPrint_(0)             # NSApplicationDelegateReplySuccess

        objc.classAddMethods(BrowserView.AppDelegate, [
            objc.selector(application_openFiles_, selector=b"application:openFiles:", signature=b"v@:@@")])
    except Exception:
        log.exception("opening files from Finder while running is unavailable")


def _mac_close_key():
    """⌘W: pywebview closes the window itself, before the page sees the key.  With documents open in tabs it closes
    the tab on screen instead - the page decides (ui/tabs.js Tabs.closeKey); the window closes only when nothing is
    open, as before."""
    try:
        import AppKit
        import objc
        from webview.platforms.cocoa import BrowserView
        host = BrowserView.WebKitHost
        before = host.keyDown_.callable            # pywebview's own (⌘C, ⌘V, ⌘Q...)
        others = AppKit.NSEventModifierFlagShift | AppKit.NSEventModifierFlagOption | AppKit.NSEventModifierFlagControl

        def keyDown_(self, event):
            flags = event.modifierFlags()
            if (flags & AppKit.NSEventModifierFlagCommand and not flags & others
                    and (event.charactersIgnoringModifiers() or "").lower() == "w"):
                win = self.window()

                def answer(handled, error):
                    if not handled:
                        win.performClose_(None)
                self.evaluateJavaScript_completionHandler_("!!(window.Tabs && Tabs.closeKey())", answer)
                return
            before(self, event)

        objc.classAddMethods(host, [objc.selector(keyDown_, selector=b"keyDown:", signature=b"v@:@")])
    except Exception:
        log.exception("⌘W for tabs is unavailable")


# ------------------------------------------------------------------ dev server

def serve(bridge: Bridge, port: int = 8765):
    """Developer mode: the same page in a normal browser; JS posts to /api/<method>."""
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(UI_DIR), **kw)

        def log_message(self, *a):
            pass

        def do_POST(self):
            name = self.path.rsplit("/", 1)[-1]
            fn = getattr(bridge, name, None)
            if not self.path.startswith("/api/") or name.startswith("_") or not callable(fn):
                self.send_error(404); return
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"[]")
            body = json.dumps(fn(*args)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"{brand.NAME} dev server on http://127.0.0.1:{port}/index.html  (Ctrl+C to stop)")
    srv.serve_forever()


# ------------------------------------------------------------------ main

def _forget_old_page():
    """pywebview serves the page from the same address every time (127.0.0.1:42001 - so what the page keeps in
    localStorage lasts), and WebView2 kept an old app.js / app.css from it despite the no-cache headers: on 6 Nov a
    run from the code showed 0.2.7's page.  After an update the new app could show the old version's page the same
    way.  So WebView2's HTTP and script caches are emptied before the window opens - only this app's own pages are in
    them; settings, sign-ins and localStorage are elsewhere in the folder and stay."""
    base = CONFIG_DIR / "webview" / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache"):
        try:
            shutil.rmtree(base / name)
        except FileNotFoundError:
            pass
        except OSError:                          # in use by another copy (a run from the code beside the app)
            log.info("couldn't empty the page cache %s", name)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    _setup_logging()
    if argv and argv[0] == "--serve":
        port = int(argv[1]) if len(argv) > 1 else 8765
        serve(Bridge(), port)
        return

    files = [str(Path(a).resolve()) for a in argv if not a.startswith("-")]
    # one copy at a time (instance.py): a second copy hands its files to the running one and quits.  The built app
    # always; a run from the code only with TOOLKIT_SINGLE=1 (so it can be tried next to the installed app)
    single = getattr(sys, "frozen", False) or os.environ.get("TOOLKIT_SINGLE") == "1"
    if single:
        import instance
        if instance.hand_off(files):
            return

    import webview
    from webview.dom import DOMEventHandler

    _forget_old_page()
    bridge = Bridge(files)
    settings = load_settings()
    theme = settings.get("theme", "system")
    look = _look(settings)
    dark = theme == "dark"
    if theme == "system":
        try:
            import darkdetect
            dark = bool(darkdetect.isDark())
        except Exception:
            pass
    width, height = _window_size()
    window = webview.create_window(
        brand.NAME, url=str(UI_DIR / "index.html"), js_api=bridge, width=width, height=height, min_size=(900, 600),
        background_color=LOOK_DESK[look][1 if dark else 0], text_select=False)
    bridge._window = window

    window.events.closing += bridge._confirm_close

    def on_loaded():
        try:
            window.dom.document.events.drop += DOMEventHandler(bridge._on_drop, True, True)
        except Exception:
            log.exception("drag-and-drop unavailable")

    window.events.loaded += on_loaded
    if platform.system() == "Darwin":
        _mac_open_files(bridge)
        _mac_close_key()
        if look != "classic":
            window.events.shown += lambda: _mac_dock_icon(look)
    listener = None
    if single:
        def from_another_copy(more: list[str]):
            if more:
                bridge._open_from_os(more)
            _to_front(window)
        try:
            listener = instance.Listener(from_another_copy).start()
        except Exception:
            log.exception("couldn't listen for other copies")
    try:
        webview.start(debug="--debug" in argv, private_mode=False, storage_path=str(CONFIG_DIR / "webview"))
    finally:
        if listener:
            listener.stop()


def _to_front(window):
    """Bring the window forward (another copy asked): un-minimise, show, and briefly keep it on top."""
    for step in (lambda: window.restore(), lambda: window.show()):
        try:
            step()
        except Exception:
            pass
    try:
        window.on_top = True
        threading.Timer(0.6, lambda: setattr(window, "on_top", False)).start()
    except Exception:
        pass


if __name__ == "__main__":
    main()
