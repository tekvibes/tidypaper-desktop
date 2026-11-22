"""
Per-document memory: what the app remembers about one document (the page you were on, the zoom, your bookmarks...).

A document is recognised by a fingerprint of its contents (SHA-256 of the whole file), so it is still recognised after
it has been moved or renamed; a changed file gets a fresh memory.  One small JSON per document in <config>/docs/;
only the most recent MAX_DOCS are kept.  Nothing is kept for password-protected documents (the memory would give away
which pages of a locked file someone bookmarked).

The document's Ask conversation is kept beside it in <key>.ask.json, and its translations in <key>.translate.json
(they can be long; they go when the memory goes).

    key = fingerprint(path)
    mem = DocMemory(config_dir)
    mem.load(key)                     -> {} the first time
    mem.update(key, page=11, zoom=1.25)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

MAX_DOCS = 200
MAX_NOTES = 20000          # characters of the document's notes box
MAX_TURNS = 40             # questions and answers kept per document
MAX_LANGUAGES = 4          # languages a document's translations are kept in (the most recent)
MAX_TRANSLATED = 2000      # pages kept per language
# what the page may store (anything else is ignored, so a bad call can't fill the file with junk)
FIELDS = {"page": int, "zoom": (int, float), "fit": (str, type(None)), "bookmarks": list, "spread": bool, "notes": str}
MAIN = re.compile(r"^[0-9a-f]+\.json$")


def fingerprint(path: str | Path, chunk: int = 1 << 20) -> str:
    """SHA-256 of the file's bytes, read a megabyte at a time."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


class DocMemory:
    def __init__(self, config_dir: str | Path):
        self.dir = Path(config_dir) / "docs"

    def _file(self, key: str) -> Path:
        if not key or not all(c in "0123456789abcdef" for c in key):
            raise ValueError("bad document key")
        return self.dir / f"{key}.json"

    def load(self, key: str | None) -> dict:
        if not key:
            return {}
        try:
            return json.loads(self._file(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, key: str | None, **values) -> dict:
        """Merge the allowed fields into the document's memory (None removes one) and save it."""
        if not key:
            return {}
        d = self.load(key)
        for k, v in values.items():
            if k not in FIELDS:
                continue
            if v is None and k != "fit":
                d.pop(k, None)
            elif isinstance(v, FIELDS[k]) and not (k != "spread" and isinstance(v, bool)):
                if k == "bookmarks":
                    v = sorted({int(p) for p in v if isinstance(p, int) and not isinstance(p, bool) and 0 <= p < 100000})
                if k == "notes":
                    v = v[:MAX_NOTES]
                    if not v.strip():
                        d.pop(k, None)
                        continue
                d[k] = v
        d["seen"] = time.time()
        self._write(self._file(key), d)
        self._prune()
        return d

    def _write(self, f: Path, d):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, f)

    # ---- the document's Ask conversation
    def _chat_file(self, key: str) -> Path:
        return self._file(key).with_name(f"{key}.ask.json")

    def load_chat(self, key: str | None) -> list[dict]:
        """[{mode, label, prompt, answer, meta, think, at}], oldest first ([] if none)."""
        if not key:
            return []
        try:
            turns = json.loads(self._chat_file(key).read_text(encoding="utf-8")).get("turns", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [t for t in turns if isinstance(t, dict) and isinstance(t.get("prompt"), str) and isinstance(t.get("answer"), str)]

    def save_chat(self, key: str | None, turns: list[dict]):
        if not key:
            return
        if not turns:
            return self.forget_chat(key)
        self._write(self._chat_file(key), {"turns": turns[-MAX_TURNS:]})
        self.update(key)                  # keeps the pair together: the memory is "seen" now, so it isn't pruned

    def forget_chat(self, key: str | None):
        if key:
            try:
                self._chat_file(key).unlink()
            except (OSError, ValueError):
                pass

    # ---- the document's translations (Translate)
    def _tr_file(self, key: str) -> Path:
        return self._file(key).with_name(f"{key}.translate.json")

    def load_translations(self, key: str | None, language: str) -> dict[int, dict]:
        """{page index: {"h": the page text's fingerprint, "t": the translation}} in this language ({} if none)."""
        if not key:
            return {}
        try:
            pages = json.loads(self._tr_file(key).read_text(encoding="utf-8"))["languages"][language]["pages"]
            return {int(k): v for k, v in pages.items() if isinstance(v, dict) and isinstance(v.get("t"), str) and isinstance(v.get("h"), str)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def save_translation(self, key: str | None, language: str, page: int, h: str, text: str):
        """Keep one page's translation (the most recent MAX_LANGUAGES languages)."""
        if not key or not text.strip():
            return
        f = self._tr_file(key)
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            langs = d["languages"] if isinstance(d.get("languages"), dict) else {}
        except (OSError, ValueError, KeyError, AttributeError):
            langs = {}
        entry = langs.pop(language, None)
        entry = entry if isinstance(entry, dict) and isinstance(entry.get("pages"), dict) else {"pages": {}}
        entry["pages"][str(int(page))] = {"h": h, "t": text}
        if len(entry["pages"]) > MAX_TRANSLATED:
            entry["pages"] = dict(list(entry["pages"].items())[-MAX_TRANSLATED:])
        entry["at"] = time.time()
        langs[language] = entry
        keep = sorted(langs, key=lambda k: (langs[k] or {}).get("at", 0) if isinstance(langs[k], dict) else 0)[-MAX_LANGUAGES:]
        self._write(f, {"languages": {k: langs[k] for k in keep}})
        self.update(key)                  # seen now, so it isn't pruned

    def forget_translations(self, key: str | None):
        if key:
            try:
                self._tr_file(key).unlink()
            except (OSError, ValueError):
                pass

    def copy(self, src: str | None, dst: str | None):
        """A saved copy starts with the original's memory (same pages, same bookmarks)."""
        if src and dst and src != dst:
            d = self.load(src)
            if d:
                self.update(dst, **{k: v for k, v in d.items() if k in FIELDS})
            chat = self.load_chat(src)
            if chat:
                self.save_chat(dst, chat)
            try:                          # pages whose words didn't change keep their translation
                tr = self._tr_file(src).read_bytes()
                self.dir.mkdir(parents=True, exist_ok=True)
                self._tr_file(dst).write_bytes(tr)
            except (OSError, ValueError):
                pass

    def forget(self, key: str | None):
        if key:
            try:
                self._file(key).unlink()
            except (OSError, ValueError):
                pass
            self.forget_chat(key)
            self.forget_translations(key)

    def _prune(self):
        """Keep the most recently used MAX_DOCS documents (each with its conversation and translations)."""
        try:
            files = sorted((p for p in self.dir.iterdir() if MAIN.match(p.name)), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for old in files[MAX_DOCS:]:
            for f in (old, old.with_name(old.stem + ".ask.json"), old.with_name(old.stem + ".translate.json")):
                try:
                    f.unlink()
                except OSError:
                    pass
