"""More editor features for the Bridge: the comments list, inserting pages from files, saving a page as a
picture, and the update check.  Mixed into app.Bridge (relies on self._lock, self._model, self._source,
self._need_doc(), self._info(), self._page_ok(), self._D() and self.annot_delete() from EditMixin).
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import urllib.request
from urllib.parse import urljoin
from pathlib import Path

import brand
from bridge_edit import ANNOT_NAMES, STAMP_KINDS, picture_png
from bridge_notes import MARKUP, rgb_hex, words_under
from bridge_util import UserError, fitz, log, safe, unique

APP_VERSION = brand.VERSION
OWN_AUTHORS = {"Toolkit", brand.NAME}   # marks the app put on its own stamps (older copies said Toolkit)
IS_MAC = platform.system() == "Darwin"
UPDATE_URL = brand.UPDATE_URL   # brand.json "update_url" (the relay's /version); settings "update_url" overrides
UPDATE_EVERY = 24 * 3600         # seconds between checks
SNOOZE = 7 * 24 * 3600           # "Not now" hides that version for a week
SKIP = {fitz.PDF_ANNOT_LINK, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}


# ------------------------------------------------------------------ settings (same file as app.py's)

def _settings_file() -> Path:
    return Path.home() / ".toolkit" / "settings.json"


def _load() -> dict:
    try:
        return json.loads(_settings_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _update(**kw):
    """Merge keys into settings.json (read-modify-write so other settings survive)."""
    try:
        s = _load(); s.update(kw)
        f = _settings_file(); f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception:
        log.exception("could not write settings")


def _version_tuple(v: str) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _padded(t: tuple) -> tuple:
    """So 0.1 and 0.1.0 count as the same version."""
    return tuple(t) + (0,) * (4 - len(t))


class MoreMixin:

    # ---- comments & marks list
    @safe
    def comments(self):
        with self._lock:
            m = self._need_doc()
            pages, n = [], 0
            for i in range(len(m)):
                pg = m.page(i)
                items = []
                for a in pg.annots() or []:
                    t = a.type[0]
                    if t in SKIP or a.flags & fitz.PDF_ANNOT_IS_HIDDEN:
                        continue
                    kind = (STAMP_KINDS.get(a.info.get("subject"), "stamp") if t == fitz.PDF_ANNOT_STAMP
                            else ANNOT_NAMES.get(t, a.type[1].lower()))
                    note = ""
                    if t in MARKUP:
                        text = words_under(pg, a)
                        note = (a.info.get("content") or "").strip()     # the note on a highlight
                    else:
                        text = a.info.get("content", "") or ""
                    items.append({"xref": a.xref, "kind": kind, "text": text.strip(), "rect": self._D(i, a.rect),
                                  "note": note, "color": rgb_hex(a.colors.get("stroke")) if t in MARKUP else "",
                                  "author": a.info.get("title", "") if a.info.get("title") not in OWN_AUTHORS else ""})
                for ln in pg.get_links():
                    if ln.get("uri") and ln.get("xref"):
                        items.append({"xref": ln["xref"], "kind": "link", "text": ln["uri"], "rect": self._D(i, ln["from"]),
                                      "author": ""})
                items.sort(key=lambda it: (it["rect"][1], it["rect"][0]))       # reading order on the page
                if items:
                    pages.append({"page": i, "items": items})
                    n += len(items)
        return {"pages": pages, "count": n}

    @safe
    def comment_delete(self, i: int, xref: int, kind: str = ""):
        """Delete a listed comment/mark; links aren't annotations to PyMuPDF, so they're removed separately."""
        if kind != "link":
            return self.annot_delete(i, xref)
        with self._lock:
            m = self._page_ok(i)
            link = next((ln for ln in m.page(i).get_links() if ln.get("xref") == int(xref)), None)
            if link is None:
                raise UserError("That link is no longer on the page.")
            m.snapshot()
            pg = m.page(i)
            pg.delete_link(next(ln for ln in pg.get_links() if ln.get("xref") == int(xref)))
            return self._done(i, "Link deleted.")

    # ---- insert pages from other files
    @safe
    def insert_from_files(self, i: int, paths: list):
        """Insert PDFs (all their pages) and pictures (a page each, fitted to this page's size) after page i,
        in the order given - one undo step."""
        if not paths:
            raise UserError("Choose at least one file.")
        with self._lock:
            m = self._page_ok(i)
            base = m.page(i).rect                     # displayed size of the page we insert after
            pw, ph = base.width, base.height
            parts = []                                # validate everything before touching the document
            try:
                for p in map(Path, paths):
                    if not p.exists():
                        raise FileNotFoundError(p)
                    ext = p.suffix.lower()
                    if ext == ".pdf":
                        other = fitz.open(str(p))
                        if other.needs_pass:
                            other.close()
                            raise UserError(f"{p.name} is password-protected - open it and remove the password first.")
                        parts.append(("pdf", other))
                    elif ext in IMAGE_EXTS:
                        png, aspect = picture_png(p)
                        parts.append(("image", (png, aspect)))
                    else:
                        raise UserError(f"{p.name} isn't a PDF or a picture.")
                m.snapshot()
                pos, added = i + 1, 0
                try:
                    for kind, obj in parts:
                        if kind == "pdf":
                            n = len(obj)
                            m.doc.insert_pdf(obj, start_at=pos)
                        else:
                            png, aspect = obj
                            # turn the page to match the picture (landscape photo -> landscape page)
                            w, h = (pw, ph) if (aspect >= 1) == (pw >= ph) else (ph, pw)
                            page = m.doc.new_page(pno=pos, width=w, height=h)
                            page.insert_image(page.rect, stream=png, keep_proportion=True)
                            n = 1
                        pos += n; added += n
                except Exception:
                    m.undo()                          # never leave half an insert behind
                    raise
            finally:
                for kind, obj in parts:
                    if kind == "pdf":
                        obj.close()
            m._held_page = None
            files = f"{len(paths)} files" if len(paths) > 1 else Path(paths[0]).name
            return {**self._info(), "changed": None, "first": i + 1, "added": added,
                    "status": f"Added {added} page{'s' if added != 1 else ''} from {files} after page {i + 1}."}

    # ---- one page as a picture
    @safe
    def page_picture(self, i: int, dpi: int = 200):
        with self._lock:
            m = self._page_ok(i)
            src = self._source or m.path or (Path.home() / "Documents" / "Document.pdf")
            stem = src.stem[:-len("_edited")] if src.stem.endswith("_edited") else src.stem
            target = unique(src.with_name(f"{stem}_page{i + 1}.png"))
            m.page(i).get_pixmap(dpi=int(max(50, min(dpi, 600)))).save(str(target))
        return {"saved": str(target), "status": f"Page {i + 1} saved as {target.name}."}

    # ---- update check (and self-installing updates: updater.py)
    def _upd(self):
        u = getattr(self, "_updater", None)
        if u is None:
            import updater
            u = self._updater = updater.Updater(APP_VERSION)
            try:
                u.startup()
            except Exception:
                log.exception("update folder")
        return u

    @staticmethod
    def _auto_on() -> bool:
        return _load().get("update_auto", True) is not False

    @safe
    def update_check(self, force: bool = False):
        """{enabled: False} when no update address is set; otherwise {newer, version, url, notes} and, when this copy
        can install it by itself, auto: True + the download's state (the page then follows update_status).
        Also says once whether the last self-install worked (updated / update_failed).
        Checks at most once a day unless force; network problems just report newer: False."""
        s = _load()
        after = self._after_update(s)
        # TOOLKIT_UPDATE_URL (even empty) replaces the built-in address: tests and dev runs never reach the real relay
        url = (s.get("update_url") or os.environ.get("TOOLKIT_UPDATE_URL", UPDATE_URL) or "").strip()
        if not url:
            return {"enabled": False, **after}
        now = time.time()
        if not force and now - float(s.get("update_checked", 0) or 0) < UPDATE_EVERY:
            ready = self._upd().ready() if self._auto_on() else None     # downloaded last time, not installed yet
            if ready:
                return {"enabled": True, "newer": True, "version": ready["version"], "current": APP_VERSION, "url": "",
                        "notes": str(s.get("update_notes", ""))[:200], "auto": True, "update": self._upd().status(), **after}
            return {"enabled": True, "newer": False, "skipped": True, **after}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{brand.EXE_STEM}/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                info = json.loads(r.read(64 * 1024).decode("utf-8"))
        except Exception as ex:
            log.info("update check failed: %s", ex)
            return {"enabled": True, "newer": False, "offline": True, **after}
        _update(update_checked=now)
        if IS_MAC:      # the Mac build has its own version, link and notes ("notes" are Windows's - a relay older than
                        # 7 Nov 2026 sends no mac_notes: then none, rather than the Windows build's); no Mac build = nothing
            info = {**info, "version": info.get("mac_version") if info.get("mac_url") else "", "url": info.get("mac_url"),
                    "file": info.get("mac_file"), "sha256": info.get("mac_sha256"), "size": info.get("mac_size"),
                    "notes": info.get("mac_notes") or ""}
        version = str(info.get("version") or "").strip()
        newer = bool(version) and _padded(_version_tuple(version)) > _padded(_version_tuple(APP_VERSION))
        snoozed = s.get("update_snoozed", {})
        if newer and not force and snoozed.get("version") == version and now - float(snoozed.get("at", 0)) < SNOOZE:
            newer = False
        rel = str(info.get("url") or "")
        link = urljoin(url, rel) if rel else ""                  # the relay says "download": next to /version
        notes = str(info.get("notes", ""))[:200]
        out = {"enabled": True, "newer": newer, "version": version, "current": APP_VERSION,
               "url": link if link.startswith(("https://", "http://127.0.0.1")) else "", "notes": notes, **after}
        if newer and self._auto_on():
            st = self._upd().start({"version": version, "file": info.get("file"), "sha256": info.get("sha256"),
                                    "size": info.get("size")})
            if st.get("state") != "unavailable":
                _update(update_notes=notes)
                out.update(auto=True, update=st)
            else:
                log.info("update %s: not installing it by itself - %s", version, st.get("why"))
        return out

    def _after_update(self, s: dict) -> dict:
        """Once, after a self-install: {updated: version, notes} or {update_failed: version}."""
        last = s.get("update_installing")
        if not isinstance(last, dict):
            return {}
        _update(update_installing=None)
        v = str(last.get("version") or "")
        if not v or time.time() - float(last.get("at", 0) or 0) > 3 * 24 * 3600:
            return {}
        if _padded(_version_tuple(APP_VERSION)) >= _padded(_version_tuple(v)):
            return {"updated": v, "updated_notes": str(last.get("notes") or "")[:200]}
        return {"update_failed": v}

    @safe
    def update_status(self):
        """The self-installing update: {state: idle|downloading|checking|ready|failed, version, done, total, error}."""
        return self._upd().status()

    def update_on_exit(self, relaunch: bool = False) -> bool:
        """The window is closing: start installing a downloaded update (it waits for the app to quit).
        relaunch: open the app again afterwards ("Restart now"); a plain close just installs."""
        try:
            u = getattr(self, "_updater", None)
            r = u.ready() if u else None
            if not r or not self._auto_on():
                return False
            if u.install_on_exit(relaunch=relaunch):
                _update(update_installing={"version": r["version"], "from": APP_VERSION, "at": time.time(),
                                           "notes": _load().get("update_notes", "")})
                return True
        except Exception:
            log.exception("update on exit")
        return False

    @safe
    def update_restart(self):
        """'Restart now': close (asking about unsaved changes first), install, open again."""
        if not self._upd().ready():
            raise UserError("The new version isn't ready yet.")
        win = getattr(self, "_window", None)
        msg = self._unsaved_message(f"Close {brand.NAME} and update")      # every open document (app.py)
        if msg and win and not win.create_confirmation_dialog("Unsaved changes", msg):
            return {"cancelled": True}
        self._keep_all()                  # unsaved changes are kept for when it opens again (bridge_kept)
        if not self.update_on_exit(relaunch=True):
            raise UserError("The update couldn't be started - it will be tried again when you close the app.")
        if win:
            threading.Timer(0.2, win.destroy).start()          # let this call return to the page first
        return {"ok": True}

    @safe
    def update_settings_get(self):
        ok, why = self._upd().possible()
        return {"auto": self._auto_on(), "possible": ok, "why": why, "current": APP_VERSION, "update": self._upd().status(),
                "enabled": bool((_load().get("update_url") or os.environ.get("TOOLKIT_UPDATE_URL", UPDATE_URL) or "").strip())}

    @safe
    def update_settings_set(self, auto: bool):
        _update(update_auto=bool(auto))
        return self.update_settings_get()

    @safe
    def update_snooze(self, version: str):
        """'Not now': don't mention this version again for a week."""
        _update(update_snoozed={"version": str(version), "at": time.time()})
        return {"ok": True}
