"""Changes not saved yet are kept (keptmarks.py) and offered back when the same file is opened again.

When: a few seconds after a change (so a crash loses little), and at once when another document is opened, the
document is closed, or the app is quit. Saved, or "Throw away": forgotten. Never for password-protected files.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from bridge_util import log, safe
from docmemory import fingerprint
from keptmarks import KeptMarks

KEEP_AFTER = 6.0          # seconds after the last change


def when_label(t: float, now: float | None = None) -> str:
    """'today 09:12', 'yesterday 21:30', '4 Nov 21:30'."""
    d, today = datetime.fromtimestamp(t), datetime.fromtimestamp(now or time.time())
    hm = d.strftime("%H:%M")
    days = (today.date() - d.date()).days
    if days == 0:
        return f"today {hm}"
    if days == 1:
        return f"yesterday {hm}"
    return f"{d.day} {d.strftime('%b')} {hm}"


class KeptMixin:
    _kept_key: str | None = None
    _kept_timer: threading.Timer | None = None
    _kept_offered = False         # last time's changes are being offered: not replaced until they're put back or not

    def _kept(self) -> KeptMarks:
        import app   # CONFIG_DIR is read at call time, so the selftests' temp home is used
        return KeptMarks(app.CONFIG_DIR)

    def _kept_soon(self):
        """A change was made: keep it in a few seconds (one write for a burst of changes)."""
        if not self._kept_key or self._password:
            return
        if self._kept_timer:
            self._kept_timer.cancel()
        t = threading.Timer(KEEP_AFTER, self._pinned(self._keep_now))     # this tab's, whichever is on screen then
        t.daemon = True
        self._kept_timer = t
        t.start()

    def _keep_now(self):
        """Keep the open document's unsaved changes (if it has any) - before it's closed or replaced."""
        if self._kept_timer:
            self._kept_timer.cancel()
            self._kept_timer = None
        with self._lock:
            m = getattr(self, "_model", None)
            key = self._kept_key
            if not key or self._password or self._kept_offered or not m:
                return
            if not m.dirty:
                return
            try:
                data = m.doc.tobytes(garbage=1, deflate=True)
                name = (self._source or m.path or Path("Document.pdf")).name
                self._kept().keep(key, data, name)
            except Exception:
                log.exception("keeping unsaved changes")

    def _kept_after_open(self) -> dict | None:
        """Just opened: work out the key and say whether there are changes to offer back."""
        self._kept_key = None if self._password else getattr(self, "_doc_key", None)
        info = self._kept().info(self._kept_key)
        self._kept_offered = bool(info)
        if not info:
            return None
        return {"when": when_label(info["time"]), "name": info["name"]}

    def _kept_saved(self, path: Path):
        """Saved: what was kept is in the file now. Later changes are kept under the saved file's key."""
        self._kept().forget(self._kept_key)
        self._kept_offered = False
        if self._kept_timer:
            self._kept_timer.cancel()
            self._kept_timer = None
        try:
            self._kept_key = None if self._password else fingerprint(path)
            self._kept().forget(self._kept_key)
        except OSError:
            self._kept_key = None

    @safe
    def kept_restore(self):
        """Put back the changes kept for this file. Undo goes back to the file as it is on disk."""
        with self._lock:
            m = self._need_doc()
            data = self._kept().data(self._kept_key)
            if not data:
                return {"error": "Those changes aren't kept any more."}
            m.undo_stack.append(m.doc.tobytes(garbage=0, deflate=False))
            m.redo_stack.clear()
            m._restore(data)
            self._kept_offered = False
            return {**self._info(), "restored": True,
                    "status": "Your unsaved changes are back. Save to keep them - Undo goes back to the file as it is."}

    @safe
    def kept_discard(self, unsaved_too: bool = False):
        """Throw away what's kept for this file; `unsaved_too` ("Don't save" on leaving): the changes on screen as
        well, so they aren't kept again as the document closes."""
        if self._kept_timer:
            self._kept_timer.cancel()
            self._kept_timer = None
        with self._lock:
            self._kept().forget(self._kept_key)
            self._kept_offered = False
            if unsaved_too and getattr(self, "_model", None):
                self._model.dirty = False
        return {"ok": True}
