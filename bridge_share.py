"""Getting a document out of the app: print, make final (flatten), password-protect, email - and the check before
emailing for card, bank, National Insurance and NHS numbers, with a copy that has them blacked out.

Every copy goes next to the original with a clear suffix and never overwrites anything.
"""

from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

from bridge_fill import find_private
from bridge_util import UserError, data_url, fitz, log, safe, unique

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
PASTE = "⌘V" if IS_MAC else "Ctrl+V"
# Mac mail apps that start a new message with the file attached when it's opened with them (open -a App file)
MAC_ATTACHERS = {"com.apple.mail": "Mail", "com.microsoft.outlook": "Outlook"}


def _stem(p: Path) -> str:
    s = p.stem
    for suffix in ("_edited", "_final", "_protected", "_unlocked", "_redacted"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


# ------------------------------------------------------------------ email (Windows)

def _mapi_client() -> str | None:
    """The default email program that speaks Simple MAPI (Outlook, Thunderbird...), or None."""
    if not IS_WIN:
        return None
    import winreg
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"Software\Clients\Mail") as k:
                name = winreg.QueryValueEx(k, "")[0]
                if name:
                    return name
        except OSError:
            continue
    return None


def _mapi_send(path: Path, subject: str) -> int:
    """Open a new message in the default MAPI mail program with `path` attached.  Returns the MAPI code (0 = ok, 1 = user cancelled)."""
    class MapiFileDesc(ctypes.Structure):
        _fields_ = [("ulReserved", ctypes.c_ulong), ("flFlags", ctypes.c_ulong), ("nPosition", ctypes.c_ulong),
                    ("lpszPathName", ctypes.c_char_p), ("lpszFileName", ctypes.c_char_p), ("lpFileType", ctypes.c_void_p)]

    class MapiMessage(ctypes.Structure):
        _fields_ = [("ulReserved", ctypes.c_ulong), ("lpszSubject", ctypes.c_char_p), ("lpszNoteText", ctypes.c_char_p),
                    ("lpszMessageType", ctypes.c_char_p), ("lpszDateReceived", ctypes.c_char_p),
                    ("lpszConversationID", ctypes.c_char_p), ("flFlags", ctypes.c_ulong), ("lpOriginator", ctypes.c_void_p),
                    ("nRecipCount", ctypes.c_ulong), ("lpRecips", ctypes.c_void_p), ("nFileCount", ctypes.c_ulong),
                    ("lpFiles", ctypes.POINTER(MapiFileDesc))]

    enc = "mbcs"
    f = MapiFileDesc(0, 0, 0xFFFFFFFF, str(path).encode(enc), path.name.encode(enc), None)
    msg = MapiMessage(0, subject.encode(enc), b"", None, None, None, 0, None, 0, None, 1, ctypes.pointer(f))
    mapi = ctypes.WinDLL("mapi32.dll")
    MAPI_LOGON_UI, MAPI_DIALOG = 0x1, 0x8
    return mapi.MAPISendMail(0, 0, ctypes.byref(msg), MAPI_LOGON_UI | MAPI_DIALOG, 0)


def dropfiles_bytes(paths: list[Path]) -> bytes:
    """CF_HDROP payload: DROPFILES header + double-NUL-terminated UTF-16 path list (lets you paste files)."""
    import struct
    files = ("\0".join(str(p) for p in paths) + "\0\0").encode("utf-16-le")
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + files       # pFiles=20, pt=(0,0), fNC=0, fWide=1


def copy_files_to_clipboard(paths: list[Path]) -> bool:
    """Put files on the Windows clipboard so Ctrl+V attaches them in a web mail or chat app."""
    if not IS_WIN:
        return False
    data = dropfiles_bytes(paths)
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    GMEM_MOVEABLE, CF_HDROP = 0x0002, 15
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        return False
    ptr = k32.GlobalLock(h)
    ctypes.memmove(ptr, data, len(data))
    k32.GlobalUnlock(h)
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        return bool(u32.SetClipboardData(CF_HDROP, h))
    finally:
        u32.CloseClipboard()


# ------------------------------------------------------------------ email (Mac)

def mac_mail_app() -> tuple[str, str, str] | None:
    """The default email program on a Mac: (bundle id, name, path to the .app), or None if there isn't one."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSBundle, NSURL
        u = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.URLWithString_("mailto:"))
        if u is None:
            return None
        bid = str(NSBundle.bundleWithURL_(u).bundleIdentifier() or "").lower()
        return bid, MAC_ATTACHERS.get(bid) or Path(str(u.path())).stem, str(u.path())
    except Exception:
        log.exception("couldn't find the default mail app")
        return None


def mac_copy_files(paths: list[Path]) -> bool:
    """Put files on the Mac pasteboard, so ⌘V attaches them in Mail, Messages or a web mail page."""
    try:
        from AppKit import NSPasteboard
        from Foundation import NSURL
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.writeObjects_([NSURL.fileURLWithPath_(str(p)) for p in paths]))
    except Exception:
        log.exception("couldn't copy the file")
        return False


def black_out(pg, rects):
    """Take the words under these boxes out of the page for good and black the boxes over (pictures under them too).
    A thin band through the middle of each box takes the letters out, so the lines just above and below keep theirs
    (MuPDF removes every letter whose font box touches a redaction - see pdf_editor._erase_spans)."""
    for r in map(fitz.Rect, rects):
        y, h = (r.y0 + r.y1) / 2, r.height
        pg.add_redact_annot(fitz.Rect(r.x0 + 0.25, y - 0.05 * h, max(r.x0 + 0.5, r.x1 - 0.25), y + 0.05 * h), fill=False)
    pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                        text=fitz.PDF_REDACT_TEXT_REMOVE)
    for r in map(fitz.Rect, rects):
        pg.add_redact_annot(r, fill=(0, 0, 0))
    pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, text=fitz.PDF_REDACT_TEXT_NONE)


class ShareMixin:
    """Mixed into app.Bridge; relies on self._lock, self._model, self._source, self._target, self._password."""

    def _out_dir_and_stem(self):
        src = self._source or (self._model.path if self._model else None) or Path.home() / "Documents" / "Document.pdf"
        return src.parent, _stem(src)

    def _signed_count(self) -> int:
        return self._signature_count() if self._model else 0

    # ---- print
    @safe
    def print_page(self, i: int, dpi: int = 200):
        """One page as an image for the print layout (annotations included, hidden ones left out)."""
        with self._lock:
            m = self._need_doc()
            if not 0 <= int(i) < len(m):
                raise UserError("That page doesn't exist.")
            pg = m.page(int(i))
            pix = pg.get_pixmap(dpi=max(72, min(int(dpi), 300)), alpha=False)
            png = pix.tobytes("png")
            return {"page": i, "src": data_url(png, "image/png"), "w": round(pg.rect.width, 2), "h": round(pg.rect.height, 2)}

    # ---- make final
    @safe
    def final_copy(self, force: bool = False):
        """A copy where form answers, notes, drawings and signatures pictures become part of the page."""
        with self._lock:
            m = self._need_doc()
            if self._signed_count() and not force:
                return {"needs_confirm": True,
                        "message": "This document is digitally signed. A final copy keeps the look but its digital "
                                   "signature won't check out any more."}
            folder, stem = self._out_dir_and_stem()
            target = unique(folder / f"{stem}_final.pdf")
            tmp = fitz.open("pdf", m.doc.tobytes(garbage=3, deflate=True))
            try:
                tmp.bake(annots=True, widgets=True)
                kw = {}
                if self._password:
                    kw = dict(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=self._password, owner_pw=self._password,
                              permissions=fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
                tmp.save(str(target), garbage=3, deflate=True, **kw)
            finally:
                tmp.close()
        log.info("final copy %s", target)
        return {"saved": str(target), "status": f"Final copy saved as {target.name} - nothing in it can be edited or removed."}

    # ---- passwords
    @safe
    def protect_copy(self, password: str):
        if len(password or "") < 4:
            raise UserError("Choose a password of at least 4 characters.")
        with self._lock:
            m = self._need_doc()
            folder, stem = self._out_dir_and_stem()
            target = unique(folder / f"{stem}_protected.pdf")
            tmp = target.with_suffix(".pdf.tmp")
            m.doc.save(str(tmp), garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_AES_256,
                       user_pw=password, owner_pw=password,
                       permissions=fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
            tmp.replace(target)
        return {"saved": str(target),
                "status": f"Saved {target.name}. It asks for the password when opened - tell the person it by phone, not in the same email."}

    @safe
    def unprotect_copy(self):
        with self._lock:
            m = self._need_doc()
            if not self._password:
                raise UserError("This document isn't password-protected.")
            folder, stem = self._out_dir_and_stem()
            target = unique(folder / f"{stem}_unlocked.pdf")
            tmp = target.with_suffix(".pdf.tmp")
            m.doc.save(str(tmp), garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
            tmp.replace(target)
        return {"saved": str(target), "status": f"Saved {target.name} without a password."}

    # ---- before emailing: numbers people keep private
    def _flat_copy(self, p: Path):
        """The PDF at p as it will be read: opened (with the open document's password if it has one), form answers,
        typed text and notes made part of the pages.  (doc, password) - or (None, None) if it can't be opened."""
        try:
            doc = fitz.open(str(p))
        except Exception:
            log.exception("couldn't open %s to check it", p)
            return None, None
        pw = None
        if doc.needs_pass:
            if not (self._password and doc.authenticate(self._password)):
                doc.close()
                return None, None
            pw = self._password
        flat = fitz.open("pdf", doc.tobytes(garbage=3, deflate=True))
        doc.close()
        flat.bake(annots=True, widgets=True)
        return flat, pw

    @safe
    def share_check(self, path: str):
        """Card, bank, National Insurance and NHS numbers in a PDF about to be emailed - found by the rules, on this
        computer (nothing is sent anywhere to check)."""
        p = Path(path)
        if p.suffix.lower() != ".pdf" or not p.exists():
            return {"items": []}
        doc, _ = self._flat_copy(p)
        if doc is None:
            return {"items": []}
        try:
            finds = find_private(doc)
        finally:
            doc.close()
        with self._lock:                 # the open document is this same file: its pages can show where each one is
            m = self._model
            disk = self._on_disk() if m else None
            here = bool(m and not m.dirty and disk and disk.resolve() == p.resolve() and len(m) > max([f["page"] for f in finds], default=-1))
            return {"items": [{"id": n, "page": f["page"], "kind": f["kind"], "label": f["label"], "text": f["text"],
                               "context": f["line"][:120], "rect": [round(v, 3) for v in f["rect"]],
                               "shown": self._D(f["page"], f["rect"]) if here else None} for n, f in enumerate(finds)]}

    @safe
    def share_redacted_copy(self, path: str, items: list):
        """A copy of the PDF with these numbers taken out for good and blacked over, saved beside it as
        name_redacted.pdf.  Flattened (form answers and notes become part of the page) so nothing can be got back."""
        if not items:
            raise UserError("Tick at least one number to black out.")
        p = Path(path)
        doc, pw = self._flat_copy(p)
        if doc is None:
            raise UserError(f"{p.name} couldn't be opened to make a copy without the numbers.")
        try:
            pages = {}
            for it in items:
                pages.setdefault(int(it["page"]), []).append(it["rect"])
            for i, rects in pages.items():
                if 0 <= i < len(doc):
                    black_out(doc[i], rects)
            doc.scrub(attached_files=True, clean_pages=False, embedded_files=True, hidden_text=False, javascript=True,
                      metadata=False, redactions=False, redact_images=0, remove_links=False, reset_fields=False,
                      reset_responses=False, thumbnails=True, xml_metadata=False)
            target = unique(p.parent / f"{_stem(p)}_redacted.pdf")
            kw = {}
            if pw:
                kw = dict(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw,
                          permissions=fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
            tmp = target.with_suffix(".pdf.tmp")
            doc.save(str(tmp), garbage=4, deflate=True, **kw)
            tmp.replace(target)
        finally:
            doc.close()
        n = len(items)
        log.info("copy without %d private number(s): %s", n, target)
        return {"saved": str(target),
                "status": f"Saved {target.name} with {n} number{'s' if n != 1 else ''} blacked out for good. Your document still has them."}

    # ---- email
    @safe
    def email_file(self, path: str | None = None):
        """Start an email with the file attached (Outlook / Thunderbird), or copy the file so Ctrl+V attaches it."""
        p = Path(path) if path else None
        if p is None:
            with self._lock:
                p = self._on_disk()
        if not p or not p.exists():
            raise UserError("Save the document first, then email it.")
        if IS_MAC:
            app = mac_mail_app()
            if app and app[0] in MAC_ATTACHERS:
                r = subprocess.run(["open", "-a", app[2], str(p)], capture_output=True, text=True, timeout=30)
                if r.returncode == 0:
                    return {"how": "app", "status": f"Opening a new email in {app[1]} with {p.name} attached…"}
                log.warning("open -a %s failed: %s", app[1], r.stderr.strip()[:300])
            return self._email_fallback(p, has_mail=app is not None)
        client = _mapi_client()
        if client and IS_WIN:
            def run():
                try:
                    code = _mapi_send(p, p.stem)
                    if code not in (0, 1):
                        log.warning("MAPISendMail returned %s", code)
                        self._email_fallback(p, notify=True)
                except Exception:
                    log.exception("MAPI email failed")
                    self._email_fallback(p, notify=True)
            threading.Thread(target=run, daemon=True).start()
            return {"how": "mapi", "status": f"Opening a new email in {client} with {p.name} attached…"}
        return self._email_fallback(p)

    def _email_fallback(self, p: Path, notify: bool = False, has_mail: bool = True):
        mailto = "mailto:?subject=" + quote(p.stem)
        if IS_MAC:
            copied = mac_copy_files([p])
            subprocess.run(["open", "-R", str(p)], capture_output=True, timeout=30)      # show it in Finder, to drag in
            opened = has_mail and subprocess.run(["open", mailto], capture_output=True, timeout=30).returncode == 0
        else:
            copied = copy_files_to_clipboard([p])
            try:
                os.startfile(mailto)  # noqa: S606
                opened = True
            except (OSError, AttributeError):
                opened = False
        msg = (f"Your email opened and the file is copied - click in the message and press {PASTE} to attach it."
               if opened and copied else
               f"The file is copied - open your email, start a message and press {PASTE} to attach it." if copied else
               "Open your email and attach the file - it's next to the original.")
        if notify and self._window:
            import json
            self._window.evaluate_js(f"window.toast && toast({json.dumps(msg)})")
        return {"how": "clipboard" if copied else "manual", "status": msg, "path": str(p)}
