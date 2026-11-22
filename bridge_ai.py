"""AI assistant (opt-in): Ask this document, Summarise, Explain - with the provider the person chooses.

Nothing is sent anywhere until AI is switched on in Settings and the person has agreed to send the document's
text to that provider.  Keys live in Windows Credential Manager, never in settings.json.

Providers
    anthropic   Claude through the official `anthropic` SDK (streaming, prompt caching, server-side fallback)
    everything else   any OpenAI-compatible /chat/completions endpoint (OpenRouter, Inception Mercury, OpenAI,
                      Google Gemini, Ollama on this PC, or a custom address) over plain HTTP

Only the pages that matter are sent: the text is extracted on this PC and, for long documents, the pages that best
match the question are picked locally (faster, cheaper and more private on every provider).
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import brand
import mac_keychain
import bridge_card
from bridge_util import UserError, log, safe

IS_WIN = platform.system() == "Windows"
APP_NAME = brand.NAME
AI_NAME = brand.AI_NAME           # what the app calls an invitation-based relay ("Shared AI")
NOT_ACCEPTED = (f"{AI_NAME} didn't accept your invitation. Ask for a new one and paste it in "
                "Settings → AI assistant.")
CANT_REACH = f"Can't reach {AI_NAME} - check the internet connection and try again."

PRESETS = {
    "anthropic": {"short": "Claude", "label": "Anthropic (Claude)", "kind": "anthropic", "base_url": "",
                  "default_model": "claude-opus-5-5",
                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
                  "key_url": "https://console.anthropic.com/settings/keys", "needs_key": True,
                  "note": "Claude reads long documents carefully and explains them clearly."},
    "openrouter": {"short": "OpenRouter", "label": "OpenRouter (one key, many models)", "kind": "openai",
                   "base_url": "https://openrouter.ai/api/v1", "default_model": "", "models": [],
                   "key_url": "https://openrouter.ai/keys", "needs_key": True,
                   "note": "One key gives access to models from many companies, including Claude and Mercury."},
    "inception": {"short": "Mercury", "label": "Inception (Mercury - very fast)", "kind": "openai",
                  "base_url": "https://api.inceptionlabs.ai/v1", "default_model": "mercury-3", "models": ["mercury-3"],
                  "key_url": "https://platform.inceptionlabs.ai/", "needs_key": True,
                  "note": "Mercury writes whole answers at once, so replies appear almost instantly."},
    "openai": {"short": "OpenAI", "label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "default_model": "",
               "models": [], "key_url": "https://platform.openai.com/api-keys", "needs_key": True, "note": ""},
    "google": {"short": "Gemini", "label": "Google (Gemini)", "kind": "openai",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "default_model": "",
               "models": [], "key_url": "https://aistudio.google.com/apikey", "needs_key": True, "note": ""},
    "ollama": {"short": "Ollama", "label": "On this PC (Ollama - free and private)", "kind": "openai",
               "base_url": "http://localhost:11434/v1", "default_model": "", "models": [],
               "key_url": "https://ollama.com/download", "needs_key": False,
               "note": "Runs on this computer, so nothing leaves it. Needs Ollama installed and a model downloaded; slower on older PCs."},
    "custom": {"short": "", "label": "Other (OpenAI-compatible address)", "kind": "openai", "base_url": "", "default_model": "",
               "models": [], "key_url": "", "needs_key": False, "note": "For any service that offers an OpenAI-compatible API."},
    "relay": {"short": AI_NAME, "label": f"From my invitation ({AI_NAME})", "kind": "relay", "base_url": "",
              "default_model": "claude-opus-5-5", "models": [], "key_url": "", "needs_key": True, "key_label": "Your code",
              "note": "Paste the invitation you were sent into “Got an invitation?” above - it fills in the address and your code."},
}
SUPPORT_EMAIL = "support@tekvibes.com"
REPORTABLE = {"answer": "An Ask answer", "translation": "A translation", "data": "Pulled-out data"}
DECIDE_PRESETS = {
    "none": {"label": "Not used", "endpoint": ""},
    "openrouter": {"label": "OpenRouter Decisions (Jev)", "endpoint": "https://openrouter.ai/api/alpha/decisions",
                   "default_model": "typesafe/jev-1.13", "key_from": "openrouter"},
    # endpoint is relative to the relay address saved for the "relay" preset; the relay code is the key
    "relay": {"label": f"{AI_NAME} (Decisions)", "endpoint": "/decisions", "base_from": "relay",
              "default_model": "typesafe/jev-1.13", "key_from": "relay"},
}
# server-side fallback (beta) is recommended for these Claude models; others don't take it
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1"}
CLAUDE_NO_EFFORT = {"claude-haiku-4-5"}


def _load_prompts() -> dict:
    """prompts.json: what the AI is told, shared with the iPad/iPhone app (ios/) so answers match everywhere.
    Bundled next to ui/ in the built app; next to this file when run from source."""
    import sys
    for base in (getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent):
        if base and (Path(base) / "prompts.json").is_file():
            return json.loads((Path(base) / "prompts.json").read_text(encoding="utf-8"))
    raise RuntimeError("prompts.json is missing")


PROMPTS = _load_prompts()
MAX_IMAGES = PROMPTS["max_images"]           # page pictures per question (scanned documents)
IMAGE_EDGE = PROMPTS["image_edge"]           # longest side in pixels - plenty for reading, small to send
TEXT_ONLY_MODELS = ("mercury",)   # names of models known not to read pictures
MAX_CHARS = PROMPTS["max_chars"]             # document text sent per question (~30k tokens): plenty for most letters and contracts
ANSWER_TOKENS = PROMPTS["answer_tokens"]
HISTORY = PROMPTS["history_messages"]        # earlier questions and answers sent along with a follow-up
CARD = PROMPTS["card"]                       # "What does it want?" - the letter card (bridge_card.py)
TURN_LABELS = {"summary": "Summarise this document", "todo": "What do I need to do?", "dates": "Dates, deadlines & money",
               "card": CARD["label"]}
STOP = set(PROMPTS["stop_words"].split())
SYSTEM_PROMPT = PROMPTS["system"]
MODE_PROMPTS = PROMPTS["modes"]


# ------------------------------------------------------------------ secret storage

class _Secrets:
    """API keys in Windows Credential Manager / the Mac's Keychain (per user).
    TOOLKIT_SECRETS_DIR switches to files (tests only)."""

    PREFIX = "Toolkit/ai/"

    def __init__(self):
        self.dir = os.environ.get("TOOLKIT_SECRETS_DIR")

    def _path(self, name):
        return Path(self.dir) / (re.sub(r"[^a-z0-9_-]", "_", name.lower()) + ".key")

    def get(self, name: str) -> str | None:
        if self.dir:
            p = self._path(name)
            return p.read_text(encoding="utf-8") if p.exists() else None
        if mac_keychain.IS_MAC:
            try:
                data = mac_keychain.get(self.PREFIX + name)
            except mac_keychain.KeychainError as e:
                log.warning("keychain read %s: %s", name, e)
                return None
            return data.decode("utf-8") if data is not None else None
        if not IS_WIN:
            return None
        cred = ctypes.POINTER(_CREDENTIAL)()
        if not _advapi.CredReadW(self.PREFIX + name, 1, 0, ctypes.byref(cred)):
            return None
        try:
            c = cred.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        finally:
            _advapi.CredFree(cred)

    def set(self, name: str, value: str):
        if self.dir:
            Path(self.dir).mkdir(parents=True, exist_ok=True)
            self._path(name).write_text(value, encoding="utf-8")
            return
        if mac_keychain.IS_MAC:
            try:
                mac_keychain.set(self.PREFIX + name, value.encode("utf-8"),
                                 label=f"{APP_NAME} - {AI_NAME + ' code' if name == 'relay' else name + ' key'}",
                                 comment=f"Saved by {APP_NAME} for its AI assistant")
            except mac_keychain.KeychainError as e:
                log.warning("keychain save %s: %s", name, e)
                raise UserError(f"Your Mac's Keychain wouldn't save it ({e.reason}). Try again.")
            return
        if not IS_WIN:
            raise UserError("Saving keys is only supported on Windows and Mac in this version.")
        blob = value.encode("utf-8")
        buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
        c = _CREDENTIAL()
        c.Type = 1                       # CRED_TYPE_GENERIC
        c.TargetName = self.PREFIX + name
        c.Comment = f"{APP_NAME} AI assistant key"
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
        c.Persist = 2                    # CRED_PERSIST_LOCAL_MACHINE: this user, this PC, survives sign-out
        c.UserName = APP_NAME
        if not _advapi.CredWriteW(ctypes.byref(c), 0):
            raise UserError("Windows wouldn't save the key. Try again, or restart the PC.")

    def delete(self, name: str):
        if self.dir:
            p = self._path(name)
            if p.exists():
                p.unlink()
            return
        if mac_keychain.IS_MAC:
            try:
                mac_keychain.delete(self.PREFIX + name)
            except mac_keychain.KeychainError as e:
                raise UserError(f"Your Mac's Keychain wouldn't remove it ({e.reason}). Try again.")
        if IS_WIN:
            _advapi.CredDeleteW(self.PREFIX + name, 1, 0)


if IS_WIN:
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _advapi.CredFree.argtypes = [ctypes.c_void_p]
else:  # pragma: no cover
    _CREDENTIAL = None


# ------------------------------------------------------------------ choosing what to send

def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9£$€][a-z0-9'£$€.,-]*", text.lower()) if len(w) > 2 and w not in STOP]


def pick_pages(texts: list[str], query: str, mode: str, budget: int = MAX_CHARS) -> tuple[list[int], bool]:
    """Which pages to send -> (page indexes in reading order, partial?).  All of them when they fit."""
    sizes = [len(t) for t in texts]
    if sum(sizes) <= budget:
        return [i for i, t in enumerate(texts) if t.strip()], False
    chosen, used = [], 0

    def take(i):
        nonlocal used
        if i not in chosen and sizes[i] and used + sizes[i] <= budget:
            chosen.append(i); used += sizes[i]

    take(0)
    if mode in ("summary", "todo", "dates", "card") or not query.strip():
        step = max(1, len(texts) // max(1, budget // max(1, sum(sizes) // len(texts))))
        for i in range(1, len(texts), step):
            take(i)
    else:
        q = set(_words(query))
        df = {}
        page_words = [_words(t) for t in texts]
        for ws in page_words:
            for w in set(ws):
                df[w] = df.get(w, 0) + 1
        n = len(texts)
        scores = []
        for i, ws in enumerate(page_words):
            counts = {}
            for w in ws:
                if w in q:
                    counts[w] = counts.get(w, 0) + 1
            s = sum((1 + math.log(c)) * math.log(1 + n / df[w]) for w, c in counts.items())
            scores.append((s, i))
        for s, i in sorted(scores, reverse=True):
            if s <= 0:
                break
            take(i)
            if i + 1 < n:
                take(i + 1)          # clauses often run onto the next page
    return sorted(chosen), True


def _looks_tabular(text: str) -> bool:
    """Cheap first look: tables come out as many short lines (cells); prose as long ones.  Saves running the table
    finder (~70 ms a page) on ordinary letters and contracts."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return sum(1 for ln in lines if len(ln) <= 30) >= 8


def _table_markdown(table, words) -> str:
    """One table as Markdown rows, each cell filled from the page's own words (keeps the spaces that the table
    finder's own cell text can lose)."""
    from bridge_util import fitz
    rows = []
    for row in table.rows:
        cells = []
        for c in row.cells:
            if c is None:                        # part of a merged cell
                cells.append("")
                continue
            r = fitz.Rect(c)
            ws = sorted((w for w in words if r.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))),
                        key=lambda w: (round(w[1] / 3), w[0]))
            cells.append(" ".join(w[4] for w in ws).replace("|", "/"))
        if any(cells):
            rows.append(cells)
    if len(rows) < 2:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


_ODD_ASCII = set("\\|+#<>~^`")
_FINE_MARKS = set("‘’“”–—•…")


def garbled_text(text: str) -> bool:
    """Text drawn with an old non-Unicode font - many Indian-language PDFs (Telugu 'Anu' fonts such as Priyaanka,
    Hindi Kruti Dev...): the PDF's text is Latin letters and symbols, not the words on the page ('‘Ó\\+>±D' where the
    page shows తెలంగాణ).  Such a page is read as a picture instead.  The sign: most words have a symbol inside them
    (6 Nov 2026: a Telugu textbook's pages 0.6-0.9; ordinary documents, maths notes included, 0.15 at most)."""
    import unicodedata
    words = [w for w in text.split() if len(w) >= 2 and any(c.isalpha() for c in w)]
    if len(words) < 8:
        return False
    def odd(w: str) -> bool:
        core = w.strip(".,;:!?'\"()[]-‘’“”«»*")[1:-1]
        return any(c in _ODD_ASCII or (ord(c) > 127 and c not in _FINE_MARKS and unicodedata.category(c)[0] in "SP")
                   for c in core)
    return sum(map(odd, words)) >= 0.35 * len(words)


def page_text_for_ai(page) -> str:
    """A page's text for the AI, with tables as Markdown rows.  Plain text extraction follows the PDF's own order, and
    many reports write a table column by column ("Flat 1..8", then every service charge...) - the AI then puts values
    under the wrong flat.  Pages without a table come out exactly as before."""
    text = page.get_text()
    if not _looks_tabular(text):
        return text
    try:
        tables = [t for t in page.find_tables().tables if t.row_count >= 2 and t.col_count >= 2]
    except Exception:
        log.exception("table finder failed")
        return text
    if not tables:
        return text
    from bridge_util import fitz
    words = page.get_text("words")
    rects = [fitz.Rect(t.bbox) for t in tables]
    md = [_table_markdown(t, words) for t in tables]
    out, placed = [], set()
    for b in page.get_text("blocks"):            # (x0, y0, x1, y1, text, block_no, type)
        r = fitz.Rect(b[:4])
        k = next((k for k, tr in enumerate(rects) if md[k] and r.intersects(tr)
                  and (r & tr).get_area() >= 0.5 * max(r.get_area(), 1e-6)), None)
        if k is None:
            out.append(b[4].strip())
        elif k not in placed:                    # the table goes where its first piece was
            placed.add(k)
            out.append(md[k])
    out += [md[k] for k in range(len(tables)) if md[k] and k not in placed]
    return "\n".join(x for x in out if x)


def document_block(texts: list[str], pages: list[int], name: str) -> str:
    parts = [f'<document name="{name}" pages="{len(texts)}">']
    for i in pages:
        parts.append(f'<page number="{i + 1}">\n{texts[i].strip()}\n</page>')
    parts.append("</document>")
    return "\n".join(parts)


def card_shape() -> str:
    """The card's JSON shape as an example, for a service that can't be given it as a fixed answer shape."""
    return json.dumps({"from": {"text": "", "page": 0}, "about": {"text": "", "page": 0}, "asks": [{"text": "", "page": 0}],
                       "deadline": {"date": "YYYY-MM-DD", "text": "", "page": 0}, "amount": {"text": "", "page": 0},
                       "references": [{"label": "", "value": "", "page": 0}], "contact": {"text": "", "page": 0},
                       "next_step": {"text": "", "page": 0}})


def at_once(provider: str, base: str, n: int) -> int:
    """Requests sent at the same time for one job (Translate's pages, Pull out data's parts): one to an AI on this
    computer (it does one thing at a time anyway)."""
    local = provider == "ollama" or base.startswith(("http://127.0.0.1", "http://localhost"))
    return 1 if local else max(1, int(n))


def page_ranges(pages: list[int]) -> str:
    """[0,1,2,5] -> '1-3, 6'"""
    out, run = [], []
    for p in pages + [None]:
        if run and (p is None or p != run[-1] + 1):
            out.append(f"{run[0] + 1}" if len(run) == 1 else f"{run[0] + 1}-{run[-1] + 1}")
            run = []
        if p is not None:
            run.append(p)
    return ", ".join(out)


# ------------------------------------------------------------------ provider calls

RELAY_CONFIG_TTL = 300        # seconds the app trusts the relay's settings before asking again


class RelayModelChanged(UserError):
    """The relay now uses a different model (or route) - fetch its settings again and retry."""


def fetch_relay_config(base: str, code: str) -> dict | None:
    """The relay's choices for this person: model, route, pictures, allowance.  None = an older relay without them."""
    req = urllib.request.Request(base.rstrip("/") + "/toolkit/config", headers={"Authorization": f"Bearer {code}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise UserError(_friendly_http(e.code, e.read().decode("utf-8", "replace"), "relay", base))
    except urllib.error.URLError:
        raise UserError(CANT_REACH)
    except ValueError:
        raise UserError(f"That address isn't a {AI_NAME} service - ask for a new invitation.")


class PicturesRefused(UserError):
    """The model can't take page pictures (raised so the caller can fall back to reading the pages locally)."""


class FormatRefused(UserError):
    """The model turned down the fixed answer shape (structured output) - ask again with the shape in the prompt."""


class Busy(UserError):
    """Too many requests for now: the relay's per-minute limit, or a service's own rate limit (429) - a long job
    (Pull out data) waits and tries again.  An allowance or daily limit used up is a plain UserError."""


def _busy(code: int, body, provider: str) -> bool:
    if code != 429:
        return False
    return provider != "relay" or "in a minute" in (_body_message(body) or "")


def short_name(provider: str, base_url: str = "") -> str:
    """'Claude', 'Mercury', or the host of a custom address - for sentences like 'Send this to Claude?'"""
    pre = PRESETS.get(provider, {})
    if pre.get("short"):
        return pre["short"]
    from urllib.parse import urlparse
    return urlparse(base_url).hostname or "the AI service"


class _Cancelled(Exception):
    pass


def can_see_pictures(pre: dict, provider: str, model: str) -> bool:
    """Can this provider/model read page images itself?  Otherwise scanned pages are read with Tesseract first."""
    m = model.lower()
    if any(t in m for t in TEXT_ONLY_MODELS):
        return False
    if pre["kind"] == "anthropic" or (pre["kind"] == "relay" and m.startswith("claude-")):
        return True
    return provider in ("openai", "google", "openrouter") or (provider == "relay" and not m.startswith("claude-"))


def picture_blocks(images: list[tuple[int, str]], style: str) -> list[dict]:
    """[(page index, base64 jpeg)] -> message content parts, each picture labelled with its page number."""
    parts = []
    for i, b64 in images:
        parts.append({"type": "text", "text": PROMPTS["picture_label"].format(page=i + 1)})
        if style == "anthropic":
            parts.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        else:
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    parts.append({"type": "text", "text": PROMPTS["pictures_note"]})
    return parts


PICTURES_ACK = PROMPTS["pictures_ack"]


def empty_answer_message(stop, name: str, relay: bool, read_locally: bool) -> str:
    """One sentence saying why no answer came back, from how the reply ended (replaces "No answer came back")."""
    if stop in ("length", "max_tokens"):
        return (f"{name} ran out of room before it could write an answer. "
                "Ask a narrower question, or about fewer pages.")
    if stop == "content_filter":
        return f"{name} held this answer back (its safety filter). Try asking in a different way."
    if read_locally:
        return ("This AI reads text only, and the words read from this scan on this PC didn't make sense to it - "
                "handwriting often can't be read this way. "
                + (f"Ask the person who runs {AI_NAME} about a model that reads page pictures." if relay
                   else "Choose Claude in Settings → AI assistant - it reads the page pictures itself."))
    if stop is None:
        return "The answer stopped arriving - the connection may have dropped. Try again."
    return f"{name} sent back an empty answer. Try again, or ask the question a different way."


def can_think(pre: dict, provider: str, model: str, relay_conf: dict | None) -> bool:
    """Is "Think it through" worth offering?  Through the relay only when its admin allows thinking."""
    if model in CLAUDE_NO_EFFORT:
        return False
    if pre["kind"] == "relay":
        return bool(relay_conf) and relay_conf.get("thinking", "off") in ("light", "thorough")
    return pre["kind"] == "anthropic" or provider == "openrouter"


def route(pre: dict, base: str, model: str) -> tuple[str, str]:
    """Which client to use and where -> ('anthropic', base_url) or ('openai', base_url).
    A family relay serves Claude at <relay>/anthropic (official SDK) and everything else at <relay>/v1."""
    if pre["kind"] == "relay":
        b = base.rstrip("/")
        return ("anthropic", b + "/anthropic") if model.startswith("claude-") else ("openai", b + "/v1")
    return pre["kind"], base


def _body_message(body) -> str:
    try:
        err = (json.loads(body) if isinstance(body, str) else body or {}).get("error", {})
        return (err.get("message") if isinstance(err, dict) else str(err)) or ""
    except Exception:
        return ""


def _friendly_http(code: int, body: str, provider: str, base_url: str = "") -> str:
    name = short_name(provider, base_url)
    name = name[:1].upper() + name[1:]
    if provider == "relay":
        if code == 401:
            return NOT_ACCEPTED
        msg = _body_message(body)
        if code in (403, 409, 413, 429) and msg:
            return msg                      # the relay's own sentence (model not offered, too big, allowance used up)
    if code in (401, 403):
        return f"{name} refused the key. Check it in Settings → AI assistant."
    if code == 404:
        return f"{name} doesn't recognise that model. Pick another one in Settings."
    if code == 429:
        return f"{name} is busy or your usage limit was reached. Wait a minute and try again."
    if code == 402:
        return f"{name} says the account has no credit left."
    if code >= 500:
        return f"{name} is having problems right now. Try again in a few minutes."
    try:
        msg = json.loads(body).get("error", {})
        msg = msg.get("message") if isinstance(msg, dict) else str(msg)
    except Exception:
        msg = ""
    return f"{name} couldn't answer: {msg[:160]}" if msg else f"{name} couldn't answer (error {code})."


def thinking_param(provider: str, think: bool) -> dict | None:
    """The OpenRouter-style `reasoning` setting, for the services known to take it (others get nothing).
    Thinking is off unless asked for: hidden thinking can use up the whole answer allowance and leave an empty reply.
    Through the relay, {"enabled": true} asks for thinking and the relay decides how much."""
    if provider == "relay":
        return {"enabled": bool(think)}
    if provider == "openrouter":
        return {"effort": "medium", "exclude": True} if think else {"enabled": False}
    return None


def stream_openai(base_url: str, key: str | None, model: str, system: str, messages: list[dict], on_text,
                  cancelled, provider: str, timeout: float = 120, images: list | None = None, think: bool = False,
                  response_format: dict | None = None, max_tokens: int | None = None, headers: dict | None = None):
    """Stream a chat completion from an OpenAI-compatible endpoint; calls on_text(chunk).
    images: [(page index, base64 jpeg)] sent as a first user turn (scanned documents).
    response_format: a fixed answer shape (Pull out data); a service that refuses it raises FormatRefused.
    -> {"stop": finish reason or None if the stream ended without one, "thinking_tokens", "out_tokens", "thought_chars"}"""
    url = base_url.rstrip("/") + "/chat/completions"
    lead = [{"role": "user", "content": picture_blocks(images, "openai")}, {"role": "assistant", "content": PICTURES_ACK}] if images else []
    body = {"model": model, "stream": True, "messages": [{"role": "system", "content": system}] + lead + messages}
    tp = thinking_param(provider, think)
    if tp is not None:
        body["reasoning"] = tp
    if provider == "openrouter":
        body["usage"] = {"include": True}          # the last chunk then reports tokens, including thinking
    if response_format:
        body["response_format"] = response_format
    if max_tokens:
        body["max_tokens"] = max_tokens
    extra, headers = headers, {"Content-Type": "application/json", "Accept": "text/event-stream"}
    headers.update(extra or {})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if "openrouter.ai" in url:
        headers["X-Title"] = APP_NAME
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        if provider == "relay" and e.code == 409:
            raise RelayModelChanged(_friendly_http(e.code, body_txt, provider, base_url))
        if images and e.code in (400, 415, 422):
            raise PicturesRefused(_friendly_http(e.code, body_txt, provider, base_url))
        if response_format and e.code in (400, 422):
            raise FormatRefused(_friendly_http(e.code, body_txt, provider, base_url))
        if _busy(e.code, body_txt, provider):
            raise Busy(_friendly_http(e.code, body_txt, provider, base_url))
        raise UserError(_friendly_http(e.code, body_txt, provider, base_url))
    except urllib.error.URLError as e:
        if provider == "ollama":
            raise UserError("Ollama isn't running on this PC. Start it (or install it from ollama.com) and try again.")
        if provider == "relay":
            raise UserError(CANT_REACH) from e
        raise UserError(f"Can't reach {short_name(provider, base_url)} - check the internet connection.") from e
    finish, usage, thought = None, {}, 0
    with resp:
        for raw in resp:
            if cancelled():
                raise _Cancelled()
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if ev.get("error"):
                err = ev["error"]
                raise UserError(_friendly_http(int(err.get("code") or 500) if str(err.get("code", "")).isdigit() else 500,
                                               json.dumps({"error": err}), provider, base_url))
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
            for ch in ev.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    on_text(piece)
                for k in ("reasoning", "reasoning_content"):
                    if isinstance(delta.get(k), str):
                        thought += len(delta[k])
                finish = ch.get("finish_reason") or finish
    return {"stop": finish, "thought_chars": thought, "out_tokens": usage.get("completion_tokens"),
            "thinking_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}


def stream_claude(key: str, model: str, base_url: str | None, system: str, doc: str, messages: list[dict],
                  on_text, cancelled, timeout: float = 120, relay: bool = False, images: list | None = None,
                  think: bool = False, output_format: dict | None = None, max_tokens: int | None = None,
                  effort: str | None = None, headers: dict | None = None):
    """Stream from Claude with the official SDK.  The document (text block, or page pictures for scans) is cached so
    follow-up questions are cheaper.  think=True ("Think it through") raises the effort; a relay may hold it lower.
    output_format: a fixed answer shape (structured output, Pull out data); refused -> FormatRefused."""
    import anthropic
    client = anthropic.Anthropic(api_key=key, base_url=base_url or None, timeout=timeout, max_retries=2)
    sys_blocks = [{"type": "text", "text": system}]
    if doc and doc.strip():
        sys_blocks.append({"type": "text", "text": doc, "cache_control": {"type": "ephemeral"}})
    if images:
        parts = picture_blocks(images, "anthropic")
        parts[-1]["cache_control"] = {"type": "ephemeral"}     # the pictures stay the same across follow-ups
        messages = [{"role": "user", "content": parts}, {"role": "assistant", "content": PICTURES_ACK}] + messages
    kw = dict(model=model, max_tokens=max_tokens or ANSWER_TOKENS, system=sys_blocks, messages=messages)
    oc = {}
    if model not in CLAUDE_NO_EFFORT:
        oc["effort"] = effort or ("high" if think else "low")   # low: short plain-English answers, fast, quality holds
    if output_format:
        oc["format"] = output_format
    if oc:
        kw["output_config"] = oc
    if headers:
        kw["extra_headers"] = headers
    try:
        if model in CLAUDE_FALLBACK_MODELS:
            ctx = client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        else:
            ctx = client.messages.stream(**kw)
        with ctx as stream:
            for text in stream.text_stream:
                if cancelled():
                    raise _Cancelled()
                on_text(text)
            final = stream.get_final_message()
    except anthropic.AuthenticationError:
        if relay:
            raise UserError(NOT_ACCEPTED)
        raise UserError("Anthropic refused the key. Check it in Settings → AI assistant.")
    except anthropic.PermissionDeniedError as e:
        if relay and _body_message(e.body):
            raise UserError(_body_message(e.body))
        raise UserError("This Anthropic key isn't allowed to use that model.")
    except anthropic.NotFoundError:
        raise UserError("Anthropic doesn't recognise that model. Pick another one in Settings.")
    except anthropic.RateLimitError as e:
        if relay and _body_message(e.body):
            msg = _body_message(e.body)
            raise (Busy if "in a minute" in msg else UserError)(msg)
        raise Busy("Claude is busy or your usage limit was reached. Wait a minute and try again.")
    except anthropic.BadRequestError as e:
        if output_format:
            raise FormatRefused(f"Claude couldn't take this request: {e.message[:160]}")
        raise UserError(f"Claude couldn't take this request: {e.message[:160]}")
    except anthropic.APIStatusError as e:
        if relay and e.status_code == 409:
            raise RelayModelChanged(_body_message(e.body) or f"{AI_NAME} has changed its model - try again.")
        if relay and e.status_code == 413 and _body_message(e.body):
            raise UserError(_body_message(e.body))
        if e.status_code >= 500:
            raise UserError((f"{AI_NAME} couldn't reach Claude" if relay else "Claude is having problems right now")
                            + ". Try again in a few minutes.")
        raise UserError(f"Claude couldn't answer (error {e.status_code}).")
    except anthropic.APIConnectionError:
        raise UserError(CANT_REACH if relay
                        else "Can't reach Anthropic - check the internet connection.")
    if final.stop_reason == "refusal":
        raise UserError("Claude declined to answer this. Try asking it differently.")
    return {"stop": final.stop_reason, "out_tokens": getattr(final.usage, "output_tokens", None),
            "cached": getattr(final.usage, "cache_read_input_tokens", 0) or 0}


def list_models(kind: str, base_url: str, key: str | None, provider: str) -> list[str]:
    if kind == "anthropic":
        import anthropic
        try:
            client = anthropic.Anthropic(api_key=key, base_url=base_url or None, timeout=20, max_retries=1)
            return [m.id for m in client.models.list()]
        except anthropic.AuthenticationError:
            raise UserError("Anthropic refused the key. Check it and try again.")
        except anthropic.APIConnectionError:
            raise UserError("Can't reach Anthropic - check the internet connection.")
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise UserError(_friendly_http(e.code, e.read().decode("utf-8", "replace"), provider))
    except urllib.error.URLError:
        raise UserError("Ollama isn't running on this PC." if provider == "ollama" else "Can't reach the service - check the address and the internet connection.")
    items = data.get("data") or data.get("models") or []
    return sorted({(m.get("id") or m.get("name")) for m in items if isinstance(m, dict) and (m.get("id") or m.get("name"))})


# ------------------------------------------------------------------ bridge

def open_mail(url: str) -> bool:
    """Hand a mailto: address to the person's own email app.  False: there wasn't one to open."""
    try:
        if IS_WIN:
            os.startfile(url)  # noqa: S606
            return True
        import subprocess
        return subprocess.run(["open" if platform.system() == "Darwin" else "xdg-open", url],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        log.exception("opening the email app")
        return False


class AiMixin:
    """Mixed into app.Bridge; relies on self._lock, self._model, self._source, self._load_settings/_save_settings."""

    _secrets = _Secrets()

    # ---- settings
    def _ai_cfg(self) -> dict:
        cfg = self._load_settings().get("ai") or {}
        cfg.setdefault("enabled", False)
        cfg.setdefault("provider", "anthropic")
        cfg.setdefault("models", {})
        cfg.setdefault("base_urls", {})
        cfg.setdefault("consent", {})
        cfg.setdefault("show_sent", False)
        cfg.setdefault("warn_scans", True)      # warn before scanned pages are read as text for a text-only AI
        cfg.setdefault("decide", {"provider": "none", "model": ""})
        return cfg

    def _ai_save(self, cfg: dict):
        s = self._load_settings(); s["ai"] = cfg; self._save_settings(s)

    def _relay_cfg(self, base: str, code: str, force: bool = False) -> dict | None:
        st = self._ai_state()
        cache = st.__dict__.setdefault("_relay_cache", {})
        hit = cache.get((base, code))
        if hit and not force and time.time() - hit[0] < RELAY_CONFIG_TTL:
            return hit[1]
        conf = fetch_relay_config(base, code)
        cache[(base, code)] = (time.time(), conf)
        return conf

    def _ai_target(self, cfg: dict, provider: str | None = None, resolve: bool = True, force: bool = False):
        p = provider or cfg["provider"]
        if p not in PRESETS:
            raise UserError("Choose an AI provider in Settings.")
        pre = PRESETS[p]
        base = (cfg["base_urls"].get(p) or pre["base_url"]).strip()
        model = (cfg["models"].get(p) or pre["default_model"]).strip()
        key = self._secrets.get(p)
        if pre["kind"] in ("openai", "relay") and not base:
            raise UserError("Paste your invitation in Settings → AI assistant." if pre["kind"] == "relay"
                            else "Type the service's address in Settings → AI assistant.")
        if pre["needs_key"] and not key:
            raise UserError("Paste your invitation in Settings → AI assistant." if pre["kind"] == "relay"
                            else f"Add your {pre['label']} key in Settings → AI assistant.")
        if pre["kind"] == "relay" and resolve:
            conf = self._relay_cfg(base, key, force)
            if conf:
                if conf.get("paused"):
                    raise UserError(f"{AI_NAME} is paused at the moment - ask the person who invited you.")
                model = conf.get("answer_model") or model      # the relay decides; it also enforces this server-side
        if not model and not (pre["kind"] == "relay" and not resolve):
            raise UserError("Choose a model in Settings → AI assistant.")
        return p, pre, base, model, key

    def _relay_pictures_ok(self, pre, base, key) -> bool:
        if pre["kind"] != "relay":
            return True
        conf = self._relay_cfg(base, key)
        return not conf or conf.get("pictures", True)

    def _picture_model(self, pre, base, key, model: str, plan: dict) -> str:
        """Through the relay, a question that sends page pictures goes to the relay's model for scans, if it has one
        (e.g. Claude for handwriting while typed documents stay on a cheap text model).  Otherwise: unchanged."""
        if pre["kind"] != "relay" or not plan["pic_pages"]:
            return model
        conf = self._relay_cfg(base, key)
        if conf and conf.get("scan_model") and conf.get("pictures", True):
            return conf["scan_model"]
        return model

    @safe
    def ai_join(self, invitation: str):
        """Use a family-relay invitation: 'TKI1.<base64url {u: address, c: code, n: name}>' or the whole invitation link.
        Checks it with the relay, then saves the code (Credential Manager) and switches the AI assistant to the relay."""
        import base64
        text = re.sub(r"\s+", "", invitation or "")
        m = re.search(r"TKI1\.[A-Za-z0-9_-]+", text)
        if not m:
            raise UserError(f"That isn't a {APP_NAME} invitation - copy it again from the invitation page (the “Copy my invitation” button).")
        raw = m.group(0)[5:]
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
            base, code, name = str(data["u"]).rstrip("/"), str(data["c"]), str(data.get("n") or "")
        except Exception:
            raise UserError("That invitation looks cut short - copy the whole thing again.")
        if not re.match(r"^https://[^\s/]+", base) and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base):
            raise UserError("That invitation points somewhere unexpected, so it wasn't used.")
        conf = fetch_relay_config(base, code)          # proves the address and the code before anything is saved
        self._secrets.set("relay", code)
        cfg = self._ai_cfg()
        cfg["provider"], cfg["enabled"] = "relay", True
        cfg["base_urls"]["relay"] = base
        self._ai_save(cfg)
        st = self._ai_state()
        st.__dict__.setdefault("_relay_cache", {})[(base, code)] = (time.time(), conf)
        who = (conf or {}).get("person") or name
        return {**self.ai_settings_get(), "person": who,
                "status": f"Connected{' as ' + who if who else ''}. Open any PDF and press Ask to try it."}

    @safe
    def ai_report(self, what: str, send: bool = False):
        """Report this answer: an email in the person's own email app (Microsoft Store policy 11.16).  It never has
        any of the document or the answer in it - they copy in what they're happy to share, and press Send there."""
        if what not in REPORTABLE:
            raise UserError("There's nothing to report here.")
        from urllib.parse import quote
        subject = f"{APP_NAME}: report an AI answer"
        where = {"Darwin": "Mac", "Windows": "Windows"}.get(platform.system(), platform.system())
        body = f"What was wrong or inappropriate:\n\n\n---\n{REPORTABLE[what]} · {APP_NAME} {brand.VERSION} · {where}"
        out = {"address": SUPPORT_EMAIL, "subject": subject, "body": body}
        if send:
            url = f"mailto:{SUPPORT_EMAIL}?subject={quote(subject)}&body={quote(body.replace(chr(10), chr(13) + chr(10)))}"
            out["opened"] = bool(open_mail(url))
        return out

    @safe
    def ai_relay_info(self):
        """What the relay has decided for this person (model, allowance) - for Settings and the Ask panel."""
        cfg = self._ai_cfg()
        pre = PRESETS["relay"]
        base = (cfg["base_urls"].get("relay") or "").strip()
        key = self._secrets.get("relay")
        if not base or not key:
            raise UserError("Paste your invitation first (Settings → AI assistant).")
        conf = self._relay_cfg(base, key, force=True)
        if conf is None:
            return {"old_relay": True}
        return {"old_relay": False, **{k: conf.get(k) for k in ("person", "answer_model", "route", "pictures", "decisions",
                                                                 "currency", "month", "paused", "scan_model")}}

    @safe
    def ai_settings_get(self):
        cfg = self._ai_cfg()
        has_key = {p: bool(self._secrets.get(p)) for p in PRESETS if PRESETS[p]["needs_key"] or p == "custom"}
        return {"cfg": cfg, "presets": PRESETS, "decide_presets": DECIDE_PRESETS, "has_key": has_key,
                "ready": self._ai_ready(cfg)}

    def _ai_ready(self, cfg) -> bool:
        try:
            self._ai_target(cfg, resolve=False)
            return bool(cfg["enabled"])
        except UserError:
            return False

    @safe
    def ai_settings_set(self, patch: dict):
        cfg = self._ai_cfg()
        for k in ("enabled", "provider", "show_sent", "warn_scans"):
            if k in patch:
                cfg[k] = patch[k]
        if cfg["provider"] not in PRESETS:
            raise UserError("Unknown AI provider.")
        for k in ("models", "base_urls", "consent"):
            if k in patch:
                cfg[k].update({a: (b or "") if k != "consent" else bool(b) for a, b in patch[k].items()})
        if "decide" in patch:
            d = patch["decide"]
            if d.get("provider", "none") not in DECIDE_PRESETS:
                raise UserError("Unknown quick-decisions service.")
            cfg["decide"] = {"provider": d.get("provider", "none"), "model": (d.get("model") or "").strip()}
        for url in cfg["base_urls"].values():
            if url and not re.match(r"^https?://", url):
                raise UserError("The address must start with https:// (or http:// for this PC).")
        self._ai_save(cfg)
        return self.ai_settings_get()

    @safe
    def ai_key_set(self, provider: str, key: str):
        if provider not in PRESETS:
            raise UserError("Unknown AI provider.")
        key = (key or "").strip()
        if len(key) < 8 or any(c.isspace() for c in key):
            raise UserError("That doesn't look like a key - copy the whole key from the provider's website and paste it.")
        self._secrets.set(provider, key)
        what = "Code" if PRESETS[provider]["kind"] == "relay" else "Key"
        return {**self.ai_settings_get(), "status": f"{what} saved for {PRESETS[provider]['label']} (in Windows Credential Manager)."}

    @safe
    def ai_key_clear(self, provider: str):
        self._secrets.delete(provider)
        return {**self.ai_settings_get(), "status": "Key removed from this PC."}

    @safe
    def ai_models(self, provider: str):
        cfg = self._ai_cfg()
        pre = PRESETS.get(provider)
        if not pre:
            raise UserError("Unknown AI provider.")
        base = (cfg["base_urls"].get(provider) or pre["base_url"]).strip()
        key = self._secrets.get(provider)
        if pre["needs_key"] and not key:
            raise UserError("Save the key first, then load the list of models.")
        if pre["kind"] == "relay":
            if not base:
                raise UserError("Paste your invitation first (Settings → AI assistant).")
            conf = self._relay_cfg(base, key, force=True)
            if conf:
                return {"models": [conf["answer_model"]], "chosen_by_relay": True}
            return {"models": list_models("openai", base.rstrip("/") + "/v1", key, provider)}   # an older relay lists what it offers
        return {"models": list_models(pre["kind"], base, key, provider)}

    @safe
    def ai_test(self, provider: str | None = None):
        """A tiny real request: proves the key, the model and the connection work, and how fast."""
        cfg = self._ai_cfg()
        p, pre, base, model, key = self._ai_target(cfg, provider)
        out, t0, first = [], time.perf_counter(), [None]

        def on_text(s):
            if first[0] is None:
                first[0] = time.perf_counter()
            out.append(s)
        msgs = [{"role": "user", "content": "Reply with just the word OK."}]
        kind, url = route(pre, base, model)
        if kind == "anthropic":
            stream_claude(key, model, url, "You are a connection test.", "(no document)", msgs, on_text, lambda: False,
                          timeout=40, relay=pre["kind"] == "relay")
        else:
            stream_openai(url, key, model, "You are a connection test.", msgs, on_text, lambda: False, p, timeout=40)
        total = time.perf_counter() - t0
        reply = "".join(out).strip()
        if not reply:
            raise UserError("The service answered with nothing - try a different model.")
        relay = pre["kind"] == "relay"                 # the relay's model stays on its admin page
        return {"ok": True, "model": None if relay else model, "reply": reply[:40],
                "first_s": round((first[0] or time.perf_counter()) - t0, 2), "total_s": round(total, 2),
                "status": f"Works - {short_name(p, base)}{'' if relay else f' ({model})'} answered in {total:.1f} s."}

    # ---- asking
    def _ai_state(self):
        if not hasattr(self, "_ai_jobs"):
            self._ai_jobs, self._ai_chat, self._ai_chat_doc, self._ocr_cache = {}, [], None, {}
            self._ai_turns = []
        return self

    def _ai_doc_id(self) -> str:
        return getattr(self, "_doc_key", None) or str(self._source)

    def _ai_conversation(self):
        """The open document's conversation: what the AI has been told (_ai_chat) and what the panel shows
        (_ai_turns).  Kept per document (beside its memory), so it is there again when the document is reopened -
        not for password-protected documents, whose conversation lasts only while they are open."""
        st = self._ai_state()
        doc_id = self._ai_doc_id()
        if st._ai_chat_doc != doc_id:
            key = getattr(self, "_doc_key", None)
            st._ai_turns = self._memory().load_chat(key) if key else []
            st._ai_chat = [m for t in st._ai_turns for m in ({"role": "user", "content": t["prompt"]},
                                                              {"role": "assistant", "content": t["answer"]})]
            st._ai_chat_doc = doc_id
        return st

    @safe
    def ai_history(self):
        """The open document's earlier questions and answers, for the panel."""
        if self._model is None:
            return {"turns": [], "kept": False}
        st = self._ai_conversation()
        keep = ("mode", "label", "answer", "pages", "partial", "provider", "think", "at")
        turns = []
        for t in st._ai_turns:
            out = {k: t.get(k) for k in keep}
            if t.get("mode") == "card" and isinstance(t.get("card"), dict):
                out["card"] = t["card"]             # the letter card, drawn again as a card
            turns.append(out)
        return {"turns": turns, "kept": bool(getattr(self, "_doc_key", None))}

    def _texts(self) -> tuple[list[str], str]:
        with self._lock:
            m = self._need_doc()
            st = self._ai_state()
            cache = st.__dict__.setdefault("_page_text_cache", {})
            if len(cache) > 5000:
                cache.clear()
            texts, garbled = [], set()
            for i in range(len(m)):
                pg = m.page(i)
                raw = pg.get_text()
                key = (i, hash(raw))              # an edited page is looked at again
                if key not in cache:              # None: an old font's junk letters (garbled_text) - a picture
                    cache[key] = None if garbled_text(raw) else re.sub(r"[ \t]+", " ", page_text_for_ai(pg)).strip()
                if cache[key] is None:
                    garbled.add(i)
                texts.append(cache[key] or "")
            st._garbled_pages = garbled
            name = (self._target or self._source or m.path or Path("Document.pdf")).name
        return texts, name

    def _picture_pages(self, texts) -> set[int]:
        """Pages that are pictures of text: scans / photos (almost no text, at least one image), and pages whose text
        is an old font's junk letters (garbled_text - their text is left out, the AI reads the picture)."""
        with self._lock:
            m = self._need_doc()
            garbled = getattr(self, "_garbled_pages", set())
            return {i for i, t in enumerate(texts) if len(t) < 25 and (i in garbled or m.page(i).get_images())}

    def _ai_plan(self, mode: str, question: str, selection: str | None, page: int | None):
        texts, name = self._texts()
        pics = self._picture_pages(texts)
        if not any(t.strip() for t in texts) and not pics:
            raise UserError("This document has no text or pictures of text to read.")
        query = " ".join(x for x in (question, selection) if x)
        if pics and len(pics) == len(texts):          # a whole scan: no text to match a question against
            pages = list(range(min(len(texts), MAX_IMAGES)))
            partial = len(texts) > MAX_IMAGES
        else:
            pages, partial = pick_pages(texts, query, mode)
            pages = sorted(set(pages) | {i for i in pics if i < MAX_IMAGES})
        if page is not None and 0 <= page < len(texts) and page not in pages:
            pages = sorted(set(pages[:-1] if partial else pages) | {page})
        if mode == "explain":
            if not (selection or "").strip():
                raise UserError("Select some text first, then choose Explain.")
            prompt = PROMPTS["explain"].format(page=(page or 0) + 1, selection=selection.strip()[:4000])
            if question.strip():
                prompt += PROMPTS["explain_question"].format(question=question.strip())
        elif mode in MODE_PROMPTS:
            prompt = MODE_PROMPTS[mode]
        elif mode == "card":
            prompt = CARD["prompt"]
        else:
            if not question.strip():
                raise UserError("Type a question first.")
            prompt = question.strip()[:4000]
        text_pages = [i for i in pages if i not in pics]
        pic_pages = [i for i in pages if i in pics]
        doc = document_block(texts, text_pages, name) if text_pages else ""
        return {"doc": doc, "prompt": prompt, "pages": pages, "partial": partial, "chars": len(doc),
                "page_count": len(texts), "name": name, "pic_pages": pic_pages, "texts": texts}

    # ---- scanned pages: pictures for services that can read them, Tesseract text for the rest
    def _page_jpeg(self, i: int) -> str:
        import base64
        from bridge_util import fitz
        with self._lock:
            pg = self._need_doc().page(i)
            z = min(4.0, IMAGE_EDGE / max(pg.rect.width, pg.rect.height))
            pix = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            return base64.b64encode(pix.tobytes("jpeg", jpg_quality=80)).decode()

    def _ocr_page(self, i: int) -> str:
        import subprocess, tempfile
        from bridge_util import fitz
        st = self._ai_state()
        key = (str(self._source), i, len(self._model.undo_stack))
        if key in st._ocr_cache:
            return st._ocr_cache[key]
        import tools
        exe = tools.find_tesseract()
        if not exe:
            raise UserError(f"This document is a scan. To read it with this AI service, {APP_NAME} needs the free Tesseract add-on "
                            "(Pull text from a scan → Install) - or choose Claude, which can read the page pictures itself.")
        with self._lock:
            pg = self._need_doc().page(i)
            z = min(4.0, 2400 / max(pg.rect.width, pg.rect.height))
            png = pg.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "page.png"
            src.write_bytes(png)
            r = subprocess.run([exe, str(src), "stdout", "-l", "eng"], capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = r.stdout.decode("utf-8", "replace").strip()
        st._ocr_cache[key] = text
        return text

    @safe
    def ai_preview(self, mode: str = "ask", question: str = "", selection: str | None = None, page: int | None = None):
        """Exactly what would be sent - shown before the first question (and on request)."""
        cfg = self._ai_cfg()
        p, pre, base, model, key = self._ai_target(cfg)
        plan = self._ai_plan(mode, question, selection, page)
        model = self._picture_model(pre, base, key, model, plan)
        pics = plan["pic_pages"]
        see = can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        shown = plan["doc"][:20000] + ("\n…" if plan["chars"] > 20000 else "")
        if pics:
            shown = (f"[{'Pictures' if see else 'Text read on this PC from the pictures'} of page{'s' if len(pics) > 1 else ''} "
                     f"{page_ranges(pics)}]\n\n" + shown).strip()
        return {"provider": short_name(p, base), "provider_id": p, "model": model, "pages": page_ranges(plan["pages"]),
                "partial": plan["partial"], "chars": plan["chars"], "page_count": plan["page_count"],
                "consent": bool(cfg["consent"].get(p)), "local": p == "ollama",
                "pictures": page_ranges(pics) if pics and see else "", "ocr": page_ranges(pics) if pics and not see else "",
                "relay": pre["kind"] == "relay", "warn_scans": bool(cfg.get("warn_scans", True)),
                "picture_mb": round(len(pics) * 0.25, 1) if pics and see else 0,
                "text": shown, "prompt": plan["prompt"]}

    @safe
    def ai_ask(self, mode: str = "ask", question: str = "", selection: str | None = None, page: int | None = None,
               remember_consent: bool = True, think: bool = False):
        """Start an answer (polled with ai_status).  think=True is "Think it through": the same question again with
        thinking, replacing the quick answer in the conversation."""
        cfg = self._ai_cfg()
        if not cfg["enabled"]:
            raise UserError("The AI assistant is switched off. Turn it on in Settings.")
        card = mode == "card"
        think = think and not card
        p, pre, base, model, key = self._ai_target(cfg)
        if not cfg["consent"].get(p):
            if not remember_consent:
                pass                      # one-time consent given in the dialog for this question
            else:
                cfg["consent"][p] = True
                self._ai_save(cfg)
        plan = self._ai_plan(mode, question, selection, page)
        model = self._picture_model(pre, base, key, model, plan)
        st = self._ai_conversation()
        doc_id, doc_key = st._ai_chat_doc, getattr(self, "_doc_key", None)
        if mode == "ask":
            label = question.strip()[:4000]
        elif mode == "explain":
            sel = " ".join((selection or "").split())
            label = f"Explain: “{sel[:140] + '…' if len(sel) > 140 else sel}”"
        else:
            label = TURN_LABELS.get(mode, mode)
        # thinking it through replaces the quick answer to the same question, rather than asking it twice
        redo = bool(think and len(st._ai_chat) >= 2 and st._ai_chat[-2]["content"] == plan["prompt"])
        history = (st._ai_chat[:-2] if redo else st._ai_chat)[-HISTORY:]
        messages = history + [{"role": "user", "content": plan["prompt"]}]
        job_id = uuid.uuid4().hex[:12]
        relay_conf = self._relay_cfg(base, key) if pre["kind"] == "relay" else None
        job = {"text": [], "done": False, "error": None, "cancel": False, "pages": page_ranges(plan["pages"]),
               "partial": plan["partial"], "provider": short_name(p, base), "model": model, "started": time.time(),
               "first_s": None, "stop": None, "progress": "", "think": bool(think), "retried": False,
               "can_think": can_think(pre, p, model, relay_conf), "info": {}}
        see = can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        read_locally = [False]
        st._ai_jobs[job_id] = job
        kind, url = route(pre, base, model)
        system = SYSTEM_PROMPT if kind == "anthropic" else SYSTEM_PROMPT + "\n\n" + plan["doc"]

        def on_text(s):
            if job["first_s"] is None:
                job["first_s"] = round(time.time() - job["started"], 2)
            job["text"].append(s)

        def ocr_doc():
            """Read scanned pages with Tesseract and return the document text with them included."""
            texts = list(plan["texts"])
            read_locally[0] = True
            for n, i in enumerate(plan["pic_pages"], 1):
                if job["cancel"]:
                    raise _Cancelled()
                job["progress"] = f"Reading page {i + 1} on this PC ({n} of {len(plan['pic_pages'])})…"
                texts[i] = self._ocr_page(i)
            job["progress"] = ""
            return document_block(texts, plan["pages"], plan["name"])

        shape_in_words = [False]

        def run():
            nonlocal system
            try:
                images, doc = None, plan["doc"]
                if plan["pic_pages"] and see:
                    job["progress"] = f"Preparing {len(plan['pic_pages'])} page picture{'s' if len(plan['pic_pages']) > 1 else ''}…"
                    images = [(i, self._page_jpeg(i)) for i in plan["pic_pages"]]
                    job["progress"] = "Reading the pages…"
                elif plan["pic_pages"]:
                    doc = ocr_doc()
                    if not doc.strip() or doc == document_block([""] * len(plan["texts"]), plan["pages"], plan["name"]):
                        raise UserError("No text could be read from these pages - the pictures may be too blurry.")
                    if kind != "anthropic":
                        system = SYSTEM_PROMPT + "\n\n" + doc
                def send(model, kind, url, think):
                    # the card: a fixed answer shape - or, for a service that can't take one, the shape in words
                    fixed = card and not shape_in_words[0]
                    msgs = messages if not (card and shape_in_words[0]) else messages[:-1] + [
                        {"role": "user", "content": plan["prompt"] + "\n\nThe JSON, in exactly this shape:\n" + card_shape()}]
                    extra = {"max_tokens": CARD["answer_tokens"]} if card else {}
                    if kind == "anthropic":
                        if fixed:
                            extra["output_format"] = {"type": "json_schema", "schema": CARD["schema"]}
                        return stream_claude(key, model, url, SYSTEM_PROMPT, doc, msgs, on_text, lambda: job["cancel"],
                                             relay=pre["kind"] == "relay", images=images, think=think, **extra)
                    if fixed:
                        extra["response_format"] = {"type": "json_schema", "json_schema": {
                            "name": "letter_card", "strict": True, "schema": CARD["schema"]}}
                    return stream_openai(url, key, model, system if kind != "anthropic" else SYSTEM_PROMPT, msgs, on_text,
                                         lambda: job["cancel"], p, images=images, think=think, **extra)

                def attempt(think):
                    nonlocal system, doc
                    try:
                        try:
                            try:
                                return send(job["model"], *route(pre, base, job["model"]), think)
                            except FormatRefused:
                                if not card or shape_in_words[0]:
                                    raise
                                shape_in_words[0] = True          # say the card's shape in words instead
                                job["text"].clear()
                                return send(job["model"], *route(pre, base, job["model"]), think)
                        except RelayModelChanged:
                            _, _, _, new_model, _ = self._ai_target(cfg, force=True)
                            new_model = self._picture_model(pre, base, key, new_model, plan)
                            new_kind, _ = route(pre, base, new_model)
                            if new_kind != "anthropic" and kind == "anthropic":
                                system = SYSTEM_PROMPT + ("\n\n" + doc if doc else "")
                            job["model"] = new_model
                            return send(new_model, *route(pre, base, new_model), think)
                    except PicturesRefused:
                        # this model turned the pictures down - read the pages here instead and ask again
                        doc = ocr_doc()
                        system = SYSTEM_PROMPT + "\n\n" + doc
                        return stream_openai(url, key, job["model"], system, messages, on_text, lambda: job["cancel"], p,
                                             think=think)

                r = attempt(think)
                if think and not "".join(job["text"]).strip() and r.get("stop") in ("length", "max_tokens"):
                    # the thinking used up the whole allowance - answer without it rather than not at all
                    job["retried"], job["progress"] = True, "That needed too much thinking - answering without it…"
                    r = attempt(False)
                job["stop"], job["info"] = r.get("stop"), r
                answer = "".join(job["text"])
                if card and answer.strip():
                    if job["stop"] in ("max_tokens", "length"):
                        raise UserError(f"{job['provider']}'s answer was cut short. Try again.")
                    try:
                        # checked against every page's own text (not only those sent); scanned pages have none
                        job["card"] = bridge_card.normalise(bridge_card.parse(answer), plan["texts"])
                    except ValueError:
                        raise UserError(f"{job['provider']} didn't answer in the expected form. Try again.")
                if answer.strip():
                    turn = {"mode": mode, "label": label, "prompt": plan["prompt"], "answer": answer,
                            "pages": job["pages"], "partial": plan["partial"], "provider": job["provider"],
                            "think": bool(think), "at": round(time.time())}
                    if card:
                        turn["card"] = job["card"]
                    if st._ai_chat_doc == doc_id:
                        if redo:
                            st._ai_chat = st._ai_chat[:-2]
                            st._ai_turns = st._ai_turns[:-1]
                        st._ai_chat += [{"role": "user", "content": plan["prompt"]}, {"role": "assistant", "content": answer}]
                        st._ai_turns = st._ai_turns + [turn]
                        turns = st._ai_turns
                    else:                     # another document was opened while this one was answered
                        turns = self._memory().load_chat(doc_key) + [turn] if doc_key else []
                    if doc_key:
                        try:
                            self._memory().save_chat(doc_key, turns)
                        except OSError:
                            log.exception("keep conversation")
                else:
                    job["error"] = empty_answer_message(job["stop"], job["provider"], pre["kind"] == "relay", read_locally[0])
            except _Cancelled:
                job["stop"] = "cancelled"
            except UserError as e:
                job["error"] = str(e)
            except Exception:
                log.exception("AI request failed")
                job["error"] = "The AI request failed. Details were saved to the log file."
            finally:
                job["done"] = True
                info = job["info"]
                log.info("ai %s %s mode=%s pages=%s chars=%d stop=%s answer=%d think=%s retried=%s out_tokens=%s "
                         "thinking_tokens=%s thought_chars=%s read_locally=%s%s", p, job["model"], mode, job["pages"],
                         plan["chars"], job["stop"], len("".join(job["text"])), job["think"], job["retried"],
                         info.get("out_tokens"), info.get("thinking_tokens"), info.get("thought_chars"), read_locally[0],
                         " empty" if job["error"] and not job["text"] else "")

        self._thread(run).start()                    # stays with this tab's document
        return {"job": job_id, "pages": job["pages"], "partial": plan["partial"], "provider": short_name(p, base), "model": model}

    @safe
    def ai_status(self, job_id: str):
        job = self._ai_state()._ai_jobs.get(job_id)
        if not job:
            raise UserError("That answer is no longer available.")
        out = {"text": "".join(job["text"]), "done": job["done"], "problem": job["error"], "pages": job["pages"],
               "partial": job["partial"], "provider": job["provider"], "model": job["model"], "first_s": job["first_s"],
               "truncated": job["stop"] in ("max_tokens", "length"), "cancelled": job["stop"] == "cancelled",
               "progress": job.get("progress", ""), "think": job["think"], "retried": job["retried"],
               "can_think": job["can_think"]}
        if job.get("card") is not None:
            out["card"] = job["card"]
        if job["done"]:
            out["elapsed_s"] = round(time.time() - job["started"], 1)
        return out

    @safe
    def ai_card_calendar(self, card: dict):
        """Add to calendar: the letter card's deadline as an all-day event (.ics), opened in the calendar app."""
        import tempfile
        if not (card or {}).get("deadline", {}).get("date"):
            raise UserError("This letter has no deadline to add.")
        try:
            name = (self._target or self._source or Path("Document.pdf")).name
        except AttributeError:
            name = "Document.pdf"
        ics = bridge_card.calendar_file(card, name)
        folder = Path(tempfile.mkdtemp(prefix="tidypaper-calendar-"))
        path = folder / (re.sub(r'[\\/:*?"<>|]+', " ", Path(name).stem).strip()[:60] + " - deadline.ics")
        path.write_text(ics, encoding="utf-8")
        self.open_external(str(path))
        return {"ok": True, "path": str(path)}

    @safe
    def ai_cancel(self, job_id: str):
        job = self._ai_state()._ai_jobs.get(job_id)
        if job:
            job["cancel"] = True
        return {"ok": True}

    @safe
    def ai_reset_chat(self):
        """New conversation: forget this document's questions and answers (also the kept copy)."""
        st = self._ai_state()
        st._ai_chat, st._ai_turns = [], []
        st._ai_chat_doc = self._ai_doc_id() if self._model is not None else None
        key = getattr(self, "_doc_key", None)
        if key:
            self._memory().forget_chat(key)
        return {"ok": True}
