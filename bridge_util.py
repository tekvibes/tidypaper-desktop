"""Shared helpers for the Bridge mixins (app.py, bridge_edit.py, bridge_sign.py)."""

from __future__ import annotations

import base64
import functools
import logging
import platform
import re
import traceback
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

log = logging.getLogger("toolkit")


class UserError(Exception):
    """A failure whose message is already one plain-English sentence for the user."""


def friendly(ex: Exception) -> str:
    """Turn an exception into one sentence a non-technical person can act on."""
    msg = str(ex)
    if isinstance(ex, UserError):
        return msg
    if isinstance(ex, FileNotFoundError):
        return "That file can't be found - it may have been moved or deleted."
    if isinstance(ex, PermissionError):
        return "Windows won't let us write there - the file may be open in another program. Close it and try again."
    if isinstance(ex, getattr(fitz, "FileDataError", ())):
        return "This file doesn't look like a PDF we can read - it may be damaged."
    if type(ex).__name__ in ("SigningError", "ToolError") and msg:
        return msg
    if isinstance(ex, ValueError) and msg:
        return msg  # PdfModel raises ValueError with user-facing text
    return "Something went wrong. Details were saved to the log file."


IS_MAC = platform.system() == "Darwin"
_PC = re.compile(r"\b(this|This|the|The|your|Your) PC\b")
_WORDS = {"status", "error", "progress", "message", "note", "detail"}     # the sentences people read


def here(text: str) -> str:
    """Messages are written for Windows ("this PC"); on a Mac they say "this Mac"."""
    return _PC.sub(r"\1 Mac", text) if IS_MAC and text else text


def _mac_words(r):
    """On a Mac, "this PC" -> "this Mac" in the messages a bridge call returns (only those keys, never document text)."""
    if isinstance(r, dict):
        for k, v in r.items():
            if isinstance(v, str):
                if k in _WORDS:
                    r[k] = here(v)
            elif isinstance(v, (dict, list)):
                _mac_words(v)
    elif isinstance(r, list):
        for v in r:
            _mac_words(v)
    return r


def safe(fn):
    """Bridge decorator: never raise into JavaScript."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            r = fn(self, *a, **kw)
        except UserError as ex:
            log.info("%s: %s", fn.__name__, ex)          # an expected, explained refusal - not a crash
            r = {"error": str(ex)}
        except Exception as ex:
            log.error("%s failed:\n%s", fn.__name__, traceback.format_exc())
            r = {"error": friendly(ex)}
        return _mac_words(r) if IS_MAC else r
    return wrapper


def unique(p: Path) -> Path:
    """p, or 'name (2).ext', 'name (3).ext'... - whichever doesn't exist yet."""
    n, q = 2, p
    while q.exists():
        q = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return q


def nice_time(iso: str | None) -> str | None:
    """'2026-10-28 23:30+00:00' -> '28 Oct 2026, 23:30' in local time"""
    if not iso or iso == "?":
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M").lstrip("0")
    except ValueError:
        return iso


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def hex_rgb(h: str | None, default=(0, 0, 0)):
    """'#1a237e' -> (r, g, b) floats 0..1"""
    if not h:
        return default
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[k:k + 2], 16) / 255 for k in (0, 2, 4))
    except ValueError:
        return default


def rgb_hex(c) -> str:
    """PyMuPDF sRGB int or (r,g,b) floats -> '#rrggbb'"""
    if isinstance(c, int):
        return "#%06x" % c
    if not c:
        return "#000000"
    return "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c[:3])
