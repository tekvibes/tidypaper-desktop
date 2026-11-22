"""Changes not saved yet, kept so they can be put back ("You have unsaved changes from yesterday 21:30 -
Put them back / Throw away").

Nothing changes in the file until Save, as before: this only means that closing the app (or a crash, or opening
another document) doesn't lose highlights, notes and other changes for good. The whole document as it was being
changed is kept in <config>/kept/<key>.pdf beside <key>.json ({"name", "time"}), where key is the fingerprint of the
file on disk (docmemory.fingerprint), so the offer appears for that file only, and not once it has changed. Kept for
MAX_DAYS, the newest MAX_KEPT. Nothing is kept for password-protected documents (a kept copy would be readable
without the password).

    kept = KeptMarks(config_dir)
    kept.keep(key, data, "Lease.pdf")
    kept.info(key)   -> {"name": "Lease.pdf", "time": 1762300000.0} or None
    kept.data(key)   -> bytes or None
    kept.forget(key)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_DAYS = 30
MAX_KEPT = 20


class KeptMarks:
    def __init__(self, config_dir: str | Path):
        self.dir = Path(config_dir) / "kept"

    def _files(self, key: str) -> tuple[Path, Path]:
        if not key or len(key) > 128 or not all(c in "0123456789abcdef" for c in key):
            raise ValueError("bad document key")
        return self.dir / f"{key}.pdf", self.dir / f"{key}.json"

    def keep(self, key: str | None, data: bytes, name: str):
        if not key or not data:
            return
        pdf, meta = self._files(key)
        self.dir.mkdir(parents=True, exist_ok=True)
        for target, body in ((pdf, data), (meta, json.dumps({"name": name, "time": time.time()}).encode("utf-8"))):
            tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
            tmp.write_bytes(body)
            os.replace(tmp, target)
        self._prune()

    def info(self, key: str | None) -> dict | None:
        if not key:
            return None
        try:
            pdf, meta = self._files(key)
            d = json.loads(meta.read_text(encoding="utf-8"))
            if not pdf.is_file() or time.time() - float(d.get("time", 0)) > MAX_DAYS * 86400:
                return None
            return {"name": str(d.get("name", "")), "time": float(d["time"])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def data(self, key: str | None) -> bytes | None:
        if not self.info(key):
            return None
        try:
            return self._files(key)[0].read_bytes()
        except (OSError, ValueError):
            return None

    def forget(self, key: str | None):
        if not key:
            return
        try:
            for f in self._files(key):
                f.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def _prune(self):
        try:
            metas = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        now = time.time()
        for n, meta in enumerate(metas):
            try:
                old = now - meta.stat().st_mtime > MAX_DAYS * 86400
            except OSError:
                continue
            if n >= MAX_KEPT or old:
                for f in (meta, meta.with_suffix(".pdf")):
                    try:
                        f.unlink()
                    except OSError:
                        pass
