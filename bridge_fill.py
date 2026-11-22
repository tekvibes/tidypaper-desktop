"""Fill with my details, and find personal details to remove (stage: AI-assisted forms).

My details are stored on this computer only: encrypted with Windows DPAPI (only this Windows user can read them),
or in the Mac's Keychain (mac_keychain.py).
Matching form boxes to details is done by label rules first; only boxes the rules can't place are sent to AI -
and then only their labels and nearby words, never the person's details.

The personal-details finder uses patterns with checksums (cards, IBAN, NHS numbers...) plus the person's own saved
details; AI is optional and only confirms doubtful finds.
"""

from __future__ import annotations

import ctypes
import json
import locale
import os
import platform
import re
import traceback
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import brand
import mac_keychain
import scanwords
from bridge_util import UserError, fitz, log, safe

IS_WIN = platform.system() == "Windows"
DETAILS_FILE = Path.home() / ".toolkit" / "mydetails.bin"
KEYCHAIN_ITEM = "Toolkit/mydetails"     # Mac: the Keychain item (fixed whatever the app is called)
WHERE = "in your Mac's Keychain" if mac_keychain.IS_MAC else "on this PC (encrypted for your Windows account)"

# key, label, group, sensitive
FIELDS = [
    ("title", "Title (Mr, Mrs, Ms, Dr…)", "Name", False),
    ("first_name", "First name", "Name", False),
    ("middle_name", "Middle name(s)", "Name", False),
    ("last_name", "Surname", "Name", False),
    ("date_of_birth", "Date of birth", "Name", True),
    ("email", "Email", "Contact", False),
    ("phone", "Phone", "Contact", False),
    ("mobile", "Mobile", "Contact", False),
    ("address_line1", "Address line 1", "Address", False),
    ("address_line2", "Address line 2", "Address", False),
    ("town", "Town or city", "Address", False),
    ("county", "County / state", "Address", False),
    ("postcode", "Postcode", "Address", False),
    ("country", "Country", "Address", False),
    ("ni_number", "National Insurance number", "Numbers", True),
    ("nhs_number", "NHS number", "Numbers", True),
    ("employer", "Employer", "Work", False),
    ("job_title", "Job title", "Work", False),
]
FIELD_KEYS = [f[0] for f in FIELDS]
DERIVED = {"full_name": "Full name", "address": "Full address", "today": "Today's date"}
DESCRIBE = {k: label for k, label, *_ in FIELDS} | DERIVED

# label rules, most specific first: (key, pattern on the normalised label)
RULES = [
    ("ni_number", r"national insurance|\bni (no|number)\b|\bnino\b"),
    ("nhs_number", r"\bnhs (no|number)\b"),
    ("date_of_birth", r"date of birth|\bdob\b|\bd o b\b|birth ?date|\bborn\b"),
    ("email", r"\be ?mail\b"),
    ("mobile", r"\bmobile\b|\bcell\b"),
    ("phone", r"\b(tele)?phone\b|\btel\b|contact (no|number)|daytime number"),
    ("postcode", r"post ?code|postal code|\bzip\b"),
    ("address_line2", r"address (line )?2\b"),
    ("address_line1", r"address (line )?1\b|\bstreet\b|house (name|number)"),
    ("address", r"^(home |your |current |postal )?address$"),
    ("town", r"\btown\b|\bcity\b"),
    ("county", r"\bcounty\b|\bstate\b|\bprovince\b"),
    ("country", r"\bcountry\b"),
    ("first_name", r"first name|forename|given name|christian name"),
    ("middle_name", r"middle name"),
    ("last_name", r"surname|last name|family name"),
    ("full_name", r"full name|^(your |applicant'?s? |print |printed )?name( in full)?$|name of applicant|print name"),
    ("title", r"^title$|mr ?/ ?mrs|salutation"),
    ("employer", r"\bemployer\b|company name|name of employer"),
    ("job_title", r"occupation|job title|\bposition\b"),
    ("today", r"^date$|date signed|today'?s date|^dated?$"),
]
SKIP = r"signature|signed by|office use|official use|reference|\bref\b|\bpolicy\b|\baccount\b|for staff"


def _norm(s: str) -> str:
    s = re.sub(r"[_\-–—.:*()\[\]]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def match_label(label: str) -> str | None:
    n = _norm(label)
    if not n or re.search(SKIP, n):
        return None
    for key, pat in RULES:
        if re.search(pat, n):
            return key
    return None


# ------------------------------------------------------------------ encrypted storage (DPAPI)

if IS_WIN:
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt = ctypes.WinDLL("crypt32")
    _k32 = ctypes.WinDLL("kernel32")

    def _dpapi(data: bytes, protect: bool) -> bytes:
        inp = _BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        fn = _crypt.CryptProtectData if protect else _crypt.CryptUnprotectData
        ok = fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))   # 0x1 = UI forbidden
        if not ok:
            raise UserError("Windows couldn't unlock your saved details." if not protect else "Windows couldn't protect your details.")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _k32.LocalFree(out.pbData)


def load_details() -> dict:
    plain = os.environ.get("TOOLKIT_SECRETS_DIR")
    try:
        if plain:
            p = Path(plain) / "mydetails.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if mac_keychain.IS_MAC:
            data = mac_keychain.get(KEYCHAIN_ITEM)
            return json.loads(data.decode("utf-8")) if data else {}
        if not DETAILS_FILE.exists() or not IS_WIN:
            return {}
        return json.loads(_dpapi(DETAILS_FILE.read_bytes(), False).decode("utf-8"))
    except UserError:
        raise
    except mac_keychain.KeychainError as e:
        log.warning("my details: %s", e)
        raise UserError(f"Your Mac's Keychain didn't let {brand.NAME} read your saved details ({e.reason}).")
    except Exception:
        log.exception("could not read my details")
        return {}


def save_details(d: dict):
    data = json.dumps(d).encode("utf-8")
    plain = os.environ.get("TOOLKIT_SECRETS_DIR")
    if plain:
        Path(plain).mkdir(parents=True, exist_ok=True)
        (Path(plain) / "mydetails.json").write_bytes(data)
        return
    if mac_keychain.IS_MAC:
        try:
            mac_keychain.set(KEYCHAIN_ITEM, data, label=f"{brand.NAME} - My details",
                             comment=f"Your details for filling in forms, saved by {brand.NAME}")
        except mac_keychain.KeychainError as e:
            log.warning("my details: %s", e)
            raise UserError(f"Your Mac's Keychain wouldn't save your details ({e.reason}). Try again.")
        return
    if not IS_WIN:
        raise UserError("Saving your details is only supported on Windows and Mac in this version.")
    DETAILS_FILE.parent.mkdir(exist_ok=True)
    tmp = DETAILS_FILE.with_suffix(".tmp")
    tmp.write_bytes(_dpapi(data, True))
    tmp.replace(DETAILS_FILE)


# ------------------------------------------------------------------ values for a box

def _us_dates() -> bool:
    loc = (locale.getlocale()[0] or "") + " " + (os.environ.get("LANG") or "")
    return "United States" in loc or "en_US" in loc


def format_date(iso: str, label: str = "", maxlen: int = 0) -> str:
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    n = _norm(label)
    if maxlen and maxlen <= 8:
        return d.strftime("%m%d%Y" if _us_dates() else "%d%m%Y")
    if "yyyy mm dd" in n:
        return d.isoformat()
    if "mm dd" in n or ("mm" in n and n.index("mm") < n.find("dd")) or _us_dates():
        return d.strftime("%m/%d/%Y")
    return d.strftime("%d/%m/%Y")


def value_for(key: str, details: dict, label: str = "", maxlen: int = 0) -> str:
    g = lambda k: (details.get(k) or "").strip()  # noqa: E731
    if key == "full_name":
        v = " ".join(x for x in (g("first_name"), g("middle_name"), g("last_name")) if x)
    elif key == "address":
        v = ", ".join(x for x in (g("address_line1"), g("address_line2"), g("town"), g("county"), g("postcode")) if x)
    elif key == "today":
        v = format_date(date.today().isoformat(), label, maxlen)
    elif key == "date_of_birth":
        v = format_date(g(key), label, maxlen)
    elif key == "ni_number":
        v = re.sub(r"\s+", "", g(key)).upper()
    else:
        v = g(key)
    if maxlen and len(v) > maxlen:
        v = v[:maxlen]
    return v


# ------------------------------------------------------------------ personal-details patterns

def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        s += d; alt = not alt
    return s % 10 == 0


def _nhs_ok(digits: str) -> bool:
    if len(digits) != 10:
        return False
    total = sum(int(digits[i]) * (10 - i) for i in range(9))
    check = 11 - total % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(digits[9])


def _iban_ok(s: str) -> bool:
    s = s.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    r = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in r)) % 97 == 1


PATTERNS = [
    # kind, label, regex, validator(match_text, line) -> bool, confidence
    ("email", "Email address", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", None, 0.95),
    ("card", "Card number", r"\b(?:\d[ -]?){13,19}\b", lambda t, l: 13 <= len(re.sub(r"\D", "", t)) <= 19 and _luhn(re.sub(r"\D", "", t)), 0.9),
    ("iban", "Bank account (IBAN)", r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b", lambda t, l: _iban_ok(t), 0.95),
    ("ni_number", "National Insurance number", r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b", None, 0.95),
    ("nhs_number", "NHS number", r"\b\d{3} ?\d{3} ?\d{4}\b", lambda t, l: _nhs_ok(re.sub(r"\D", "", t)), 0.85),
    ("sort_code", "Bank sort code", r"\b\d{2}-\d{2}-\d{2}\b", lambda t, l: bool(re.search(r"\bsort\b", l, re.I)), 0.88),
    ("account", "Bank account number", r"\b\d{8}\b", lambda t, l: bool(re.search(r"account|acc(t)?\b|a/c", l, re.I)), 0.88),
    ("ssn", "US Social Security number", r"\b\d{3}-\d{2}-\d{4}\b", None, 0.8),
    ("phone", "Phone number", r"(?:(?:\+|00)\d{1,3}[ -]?)?(?:\(?0\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}|\b07\d{3} ?\d{6}\b|\(\d{3}\) ?\d{3}-\d{4})",
     lambda t, l: 10 <= len(re.sub(r"\D", "", t)) <= 14, 0.8),
    ("postcode", "Postcode", r"\b(?:GIR ?0AA|[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]? ?\d[ABD-HJLNP-UW-Z]{2})\b", None, 0.7),
    ("birth_date", "Date of birth", r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}\b",
     lambda t, l: bool(re.search(r"birth|\bdob\b|\bborn\b", l, re.I)), 0.8),
]


# what the check before emailing or sharing looks for: numbers people keep private (not names or addresses - nearly
# every letter has those)
PRIVATE_KINDS = ("card", "iban", "ni_number", "nhs_number", "sort_code", "account")


def own_details(details: dict) -> list[tuple[str, str]]:
    """The person's own details to look for too (whole words): name, email, phone, address, numbers."""
    own = []
    for k in ("first_name", "last_name", "email", "phone", "mobile", "address_line1", "address_line2", "postcode",
              "ni_number", "nhs_number"):
        v = (details.get(k) or "").strip()
        if len(v) >= 3:
            own.append((k, v))
    full = value_for("full_name", details)
    if len(full.split()) > 1:
        own.insert(0, ("full_name", full))
    return own


def find_personal(pages, own: list[tuple[str, str]] = (), wider: bool = False, also: tuple = (), only=None) -> list[dict]:
    """Everything on these pages that looks like a personal detail, by the rules (nothing is sent anywhere): dicts of
    page, kind, label, text, rect (PDF space), conf, line, more - the most confident kept where finds overlap, in
    reading order.  [more]: the rest of a find that runs over several lines (an address), a rect a line.

    [wider] - Find personal details, not the check before sharing: whole addresses too, grown back from a postcode
    (iOS's and Android's `scanWider`, rules only - no recogniser here).  They are never ticked for the person (0.75,
    under the ticking line): the sender's address is found as readily as the reader's.
    [also] - "person" and / or "amount": people's names and amounts of money by the rules below (Remove personal
    details from many PDFs; they are guesses, offered only when asked for).
    [only] - the kinds wanted (None: all), left out before overlaps are settled, so a kind not wanted can't hide one
    that is."""
    wider = wider or "address" in (only or ())
    found = []
    for pg in pages:
        i = pg.number
        lines = page_lines(pg)
        for text, spans in lines:
            for kind, label, rx, ok, conf in PATTERNS:
                for mm in re.finditer(rx, text, 0 if kind in ("iban", "postcode", "ni_number") else re.I):
                    t = mm.group(0).strip()
                    if ok and not ok(t, text):
                        continue
                    r = _span_rect(spans, mm.start(), mm.end())
                    if r:
                        found.append({"page": i, "kind": kind, "label": label, "text": t, "rect": r, "conf": conf, "line": text, "more": []})
            for k, v in own:                 # whole words only; specific finds (email, card...) win overlaps
                for mm in re.finditer(r"(?<![\w@.])" + re.escape(v) + r"(?![\w@])", text, re.I):
                    r = _span_rect(spans, mm.start(), mm.end())
                    if r:
                        found.append({"page": i, "kind": "yours", "label": "Your " + DESCRIBE.get(k, k).lower(),
                                      "text": mm.group(0), "rect": r, "conf": 0.93, "line": text, "more": []})
        if wider:
            found += _addresses(lines, i)
        if "person" in also:
            found += _names(lines, i)
        if "amount" in also:
            found += _amounts(lines, i)
    if only is not None:
        found = [f for f in found if f["kind"] in only]
    # overlapping finds: keep the most confident (ties: the one found first)
    found.sort(key=lambda f: -f["conf"])
    kept = []
    for f in found:
        if not any(k["page"] == f["page"] and _covered(_boxes(f), _boxes(k)) for k in kept):
            kept.append(f)
    kept.sort(key=lambda f: (f["page"], round(f["rect"].y0, 2), f["rect"].x0))
    return kept


# ---- whole addresses, grown back from a postcode (Android's Personal.kt, which is iOS's FillDetails.swift put right
# for sentences - the words of each rule are the same there)

ADDRESS_CONF = 0.75          # over the postcode inside it (0.7), under the ticking line (0.85)
WIDER_KINDS = ("address",)   # what only the wider look finds - never ticked, and never given to the AI to judge


def _boxes(f) -> list:
    return [f["rect"], *f.get("more", ())]


def _covered(mine: list, theirs: list, share: float = 0.5) -> bool:
    """More than [share] of [mine]'s area lies under [theirs]."""
    area = sum(r.get_area() for r in mine)
    shared = sum((fitz.Rect(a) & b).get_area() for a in theirs for b in mine)
    return shared > share * area


_POSTCODE = re.compile(next(p[2] for p in PATTERNS if p[0] == "postcode"))
_FLAT_ABOVE = re.compile(r"\b(?:Flat|Flats|Apartment|Apt\.?|Unit|Suite|Room|Floor)\s+[\w-]+(?:\s*[-,]\s*\w+)?$", re.I)
_LABEL_END = re.compile(r"[:;]\s|[.!?]\s+(?=[^\W\d_])")          # (the letter after a full stop must be a capital)
_WORD = re.compile(r"\b[^\W\d_](?:[^\W\d_]|['’-])*\b")
# the words between the towns of "Stratford upon Avon" and the like - never the first word of an address
_SMALL_WORDS = {"of", "and", "the", "on", "upon", "under", "in", "le", "by", "c/o"}
_ADDRESS_WORD = re.compile(r"\d|\b(?:Flats?|Apartment|Apt|Unit|Suite|Room|Floor|Level|House|Cottage|Lodge|Court|Buildings?|Centre|Center|"
                           r"Offices?|Box|Road|Rd|Street|St|Lane|Avenue|Ave|Close|Drive|Way|Row|Square|Place|Crescent|Terrace|Gardens|"
                           r"Parade|Hill|Park|Grove|Rise|Walk|Mews|Wharf|Quay|Green|Yard|Estate|Mills?|Farm)\b", re.I)
_WHOSE = re.compile(r"\b(?:of|at|in|by)\s+")


def _capital_run(text: str, frm: int, to: int) -> int:
    """Where the run of capitalised words and numbers that ends a stretch begins ([to] when it ends with neither)."""
    start = at = to
    while at > frm:
        e = at
        while e > frm and text[e - 1].isspace():
            e -= 1
        b = e
        while b > frm and not text[b - 1].isspace():
            b -= 1
        if e == b:
            break
        word = text[b:e]
        first = next((c for c in word if c.isalnum()), None)
        if first is None or word.lower() in _SMALL_WORDS:
            pass                                             # small words wait for a capital before them
        elif first.isupper() or first.isdigit():
            start = b
        else:
            return start
        at = b
    return start


def _address_start(text: str, frm: int, to: int, town: bool) -> int:
    """Where the address begins in a stretch of a line, going back from its end a part at a time (the parts divided
    by commas or bullets).  A part that is all capitalised words and numbers belongs to it.  A part with ordinary
    words in it is a sentence: of that only the capitalised words at its end are the address, and only when they
    have a number or a word like Flat, House or Road ("the flat known as Flat 2" - not "The agent is Kestrel
    Lettings Ltd") - or are the town, in the part that reaches the postcode ([town]).  [to] when nothing is."""
    cuts = [frm - 1] + [k for k in range(frm, to) if text[k] in ",•"] + [to]
    start = to
    for p in range(len(cuts) - 2, -1, -1):
        a, b = cuts[p] + 1, cuts[p + 1]
        run = _capital_run(text, a, b)
        if not text[a:run].strip():                          # the whole part (or nothing in it: a comma at a line's end)
            if run < b:
                start = run
            continue
        # in a sentence "of" ends the name before it: "Pennine Builders' Merchants Ltd of Wakefield Road"
        s = run
        for m in _WHOSE.finditer(text, run, b):
            s = m.end()
        if s < b and ((town and p == len(cuts) - 2) or _ADDRESS_WORD.search(text, s, b)):
            start = s
        break
    return start


def _after_label(text: str, frm: int, to: int) -> int:
    """Where the address part of a line begins: after its last colon, semicolon or sentence end."""
    cut = frm
    for m in _LABEL_END.finditer(text, frm, to):
        if m.group(0)[0] in ":;" or text[m.end()].isupper():
            cut = m.end()
    return cut


def _before_postcodes(text: str, starts: list[int]) -> list[tuple[int, int]]:
    """Addresses grown back from a postcode: "Property: FLAT 1,58 HIGH ROAD,LONG DITTON," with "SURREY KT6 5LB" under
    it (capitals, no space after the commas), or "3rd Floor • 10 Mill Lane • London • EC3N 3AJ".  On the postcode's
    line: the parts before it divided by commas or bullets, back to a colon or a sentence's end, as far as they are
    an address - or, with no such divider, the capitalised words just before it (a town).  Then the lines above
    while each ends with a comma.  A postcode with nothing before it stays a postcode."""
    out = []
    for m in _POSTCODE.finditer(text):
        n = max((k for k, s in enumerate(starts) if s <= m.start()), default=-1)
        if n < 0:
            continue
        frm = _after_label(text, starts[n], m.start())
        if not any(c in ",•" for c in text[frm:m.start()]):        # no list: only the town's capitalised words
            caps = [(c.start(), c.end()) for c in _WORD.finditer(text, frm, m.start()) if c.group(0)[0].isupper()]
            words, at = 0, m.start()
            for s, e in reversed(caps):
                if words >= 3 or any(ch != " " for ch in text[e:at]):
                    break
                at, words = s, words + 1
            frm = at
        else:
            frm = _address_start(text, frm, m.start(), town=True)
        while frm < m.start() and text[frm] == " ":
            frm += 1
        # the lines above, while the address reaches its line's start and the one above ends with a comma
        line = n
        while line > 0 and n - line < 3 and frm == starts[line]:
            above_end = starts[line] - 1
            if not text[starts[line - 1]:above_end].strip(" \t").endswith(","):
                break
            s = _address_start(text, _after_label(text, starts[line - 1], above_end), above_end, town=False)
            while s < above_end and text[s] == " ":
                s += 1
            if s >= above_end:
                break
            frm, line = s, line - 1
        if frm < m.start():
            out.append((frm, m.end()))
    return out


def _addresses(lines, page: int) -> list[dict]:
    """One page's whole addresses.  The page is read as one text, a line of the page to a line, so an address may run
    over several; it becomes one find with a rect a line."""
    text = "\n".join(t for t, _ in lines)
    if not text:
        return []
    starts, at = [], 0
    for t, _ in lines:
        starts.append(at)
        at += len(t) + 1
    joined = []                                              # where two meet, they are one
    for a, b in sorted(_before_postcodes(text, starts)):
        if joined and a < joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], b))
        else:
            joined.append((a, b))
    found = []
    for frm, to in joined:
        # "… Flat 1-6" at the end of the line above belongs to it
        n = starts.index(frm) if frm in starts else -1
        if n > 0:
            fm = _FLAT_ABOVE.search(text, starts[n - 1], starts[n] - 1)
            if fm:
                frm = fm.start()
        rects, first = [], ""
        for k, (t, spans) in enumerate(lines):
            s, e = max(frm, starts[k]) - starts[k], min(to, starts[k] + len(t)) - starts[k]
            if e <= s:
                continue
            r = _span_rect(spans, s, e)
            if r:
                first = first or t
                rects.append(r)
        if not rects:
            continue
        words = ", ".join(w.strip(" \t") for w in text[frm:to].split("\n") if w.strip(" \t"))
        found.append({"page": page, "kind": "address", "label": "Address", "text": words, "rect": rects[0], "conf": ADDRESS_CONF,
                      "line": first, "more": rects[1:]})
    return found


# ---- people's names and amounts of money, by rules (the desktop has no recogniser and no model - 20 Nov).  For
# Remove personal details from many PDFs, where the person turns them on knowing they are guesses.  Measured on
# ios/TestSet before they were offered (NEXT_SESSION.md, 21 Nov).

PERSON_CONF, AMOUNT_CONF = 0.55, 0.6          # Android's: never ticked in a list
_UP = "A-ZÀ-ÖØ-Þ"
_LOW = "a-zß-öø-ÿ'’"
# Okafor, Llewellyn-Price, B., B - never a word with a colon after it ("Date:" is a label)
_TOK = rf"(?:(?:Mc|Mac|O['’])?[{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)*\b|[{_UP}]\.|[{_UP}]\b)(?![ \t]*:)"
_NAME = rf"{_TOK}(?:[ \t]+{_TOK}){{0,3}}"
_TITLE = r"(?:Mr|Mrs|Ms|Miss|Mx|Dr|Prof|Professor|Sir|Dame|Rev|Revd|Lady|Lord)\.?"
_NAME_LIST = rf"(?:{_TITLE}[ \t]+)?{_NAME}(?:[ \t]*(?:,|and|&)[ \t]*(?:{_TITLE}[ \t]+)?{_NAME})*"
_AFTER_TITLE = re.compile(rf"\b{_TITLE}[ \t]+({_NAME})")
_DEAR = re.compile(rf"\bDear[ \t]+(?:{_TITLE}[ \t]+)?({_NAME})")
_LABELLED = re.compile(rf"(?i:\b(?:(?:full|first|last|middle|given|family|account|customer|patient|contact|print|your)[ \t]+)?names?"
                       rf"|\bsurname|\bforenames?|\bknown[ \t]+as|\bsigned(?:[ \t]+by[^:\n]{{0,30}})?)[ \t]*:[ \t]*({_NAME_LIST})")
_SIGN_OFF = re.compile(r"(?im)^[ \t]*(?:yours[ \t]+(?:sincerely|faithfully|truly)|(?:kind|best|warm|with[ \t]+best)[ \t]+regards|regards"
                       r"|best[ \t]+wishes|many[ \t]+thanks|with[ \t]+thanks)[ \t]*,?[ \t]*$")
_WHOLE_NAME = re.compile(rf"[ \t]*({_TOK}(?:[ \t]+{_TOK}){{1,3}})[ \t]*$")
# words that are never a name by themselves, and words that make it an organisation's
_NOT_NAMES = {"client", "customer", "auctioneer", "tenant", "landlord", "applicant", "claimant", "defendant", "debtor", "creditor", "agent",
              "officer", "manager", "director", "occupier", "owner", "payee", "payer", "sir", "madam", "ref", "reference", "bill", "account",
              "member", "patient", "insured", "policyholder", "dear", "the", "team", "department", "service", "services", "sir/madam",
              "head", "of", "and", "customer", "colleague", "friend", "parent", "guardian", "resident", "householder", "all"}
_ORG_WORDS = {"ltd", "limited", "plc", "llp", "inc", "council", "bank", "building", "society", "trust", "nhs", "hospital", "clinic",
              "school", "college", "university", "group", "company", "insurance", "solicitors", "services", "water", "energy", "club",
              "association", "centre", "center", "office", "department", "ministry", "agency", "partners", "court", "street", "road"}
_AMOUNT = re.compile(r"(?:[£€$]|\b(?:GBP|EUR|USD)[ \t]?)[ \t]?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?(?!\d)"
                     r"|(?:[£€$]|\b(?:GBP|EUR|USD)[ \t]?)[ \t]?\d+(?:\.\d{1,2})?(?![\d,])"
                     r"|\b\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?[ \t]?(?:pounds?|pence|euros?|dollars?|GBP|EUR|USD)\b"
                     r"|\b\d{1,3}p\b", re.I)


def _page_text(lines):
    text = "\n".join(t for t, _ in lines)
    starts, at = [], 0
    for t, _ in lines:
        starts.append(at)
        at += len(t) + 1
    return text, starts


def _placed(frm: int, to: int, text: str, lines, starts) -> tuple[list, str] | None:
    """A stretch of the page's joined lines as rects, one a line, and the first line it is on."""
    rects, first = [], ""
    for k, (t, spans) in enumerate(lines):
        s, e = max(frm, starts[k]) - starts[k], min(to, starts[k] + len(t)) - starts[k]
        if e <= s:
            continue
        r = _span_rect(spans, s, e)
        if r:
            first = first or t
            rects.append(r)
    return (rects, first) if rects else None


def _a_name(words: str) -> bool:
    """Whether these words can be a person's name: not only initials, not a role ("Dear Customer"), not an
    organisation ("Brightwater Bank")."""
    ws = [w.strip(".,").lower() for w in words.split()]
    if not any(len(w) > 1 for w in ws) or all(w in _NOT_NAMES for w in ws) or any(w in _ORG_WORDS for w in ws):
        return False
    return not (len(ws) == 1 and ws[0] in _NOT_NAMES)


def _names(lines, page: int) -> list[dict]:
    """People's names by rules: after a title ("Mrs Eleanor Whitcombe", "Dear Mr Vaughan" - the title stays), after
    "Dear", after a label ("Name:", "Surname:", "Signed by the Tenant:", several with commas or "and"), and the line
    under "Yours sincerely,".  They will miss names said any other way."""
    text, starts = _page_text(lines)
    spans = []
    for rx in (_AFTER_TITLE, _DEAR):
        spans += [(m.start(1), m.end(1)) for m in rx.finditer(text)]
    for m in _LABELLED.finditer(text):
        # each name of the list, without its title
        for n in re.finditer(rf"(?:{_TITLE}[ \t]+)?({_NAME})", m.group(1)):
            spans.append((m.start(1) + n.start(1), m.start(1) + n.end(1)))
    for m in _SIGN_OFF.finditer(text):
        k = max(i for i, s in enumerate(starts) if s <= m.start())
        for j in range(k + 1, min(k + 4, len(lines))):              # the next line with words (a signature may be between)
            t = lines[j][0]
            if not t.strip():
                continue
            w = _WHOLE_NAME.match(t)
            if w:
                spans.append((starts[j] + w.start(1), starts[j] + w.end(1)))
            break
    found, seen = [], set()
    for a, b in sorted(spans):
        words = text[a:b].strip()
        # a title's name ends before a word that is the organisation's ("Dr Farah Siddiqui of ..." already stops at "of")
        if (a, b) in seen or not _a_name(words):
            continue
        seen.add((a, b))
        p = _placed(a, b, text, lines, starts)
        if p:
            found.append({"page": page, "kind": "person", "label": "Person's name", "text": words, "rect": p[0][0],
                          "conf": PERSON_CONF, "line": p[1], "more": p[0][1:]})
    return found


def _amounts(lines, page: int) -> list[dict]:
    """Amounts of money: a figure with its currency sign or word (£164.00, €12, 45p, 300 pounds) - Android's
    `isAmount`, as a rule."""
    found = []
    for text, spans in lines:
        for m in _AMOUNT.finditer(text):
            r = _span_rect(spans, m.start(), m.end())
            if r:
                found.append({"page": page, "kind": "amount", "label": "Amount of money", "text": m.group(0).strip(), "rect": r,
                              "conf": AMOUNT_CONF, "line": text, "more": []})
    return found


# Find personal details reads a scan's words itself before it looks (Android's words, Personal.kt)
SCANS_READ = "The scan's words were read first, to look through them - they can now be selected and found too. Save to keep them."
NOTHING_FOUND = "Nothing that looks like a personal detail was found in this document."


def scans_left(left: int, pages: int, stopped: bool = False) -> str | None:
    """What to say when [left] of the document's [pages] pages are still pictures with no words after Find personal
    details has read them - nothing could be read in them, or reading was [stopped]; None when none are."""
    if left <= 0:
        return None
    one = pages == 1 or left == 1
    why = ("that wasn't read" if one else "that weren't read") + " (reading was stopped)" if stopped else "whose words couldn't be read"
    if pages == 1:
        return f"This page is a scan {why}, so there was nothing to look through."
    if left >= pages:
        return f"These pages are scans {why}, so there was nothing to look through."
    if left == 1:
        return f"One page is a scan {why}, so it hasn't been looked through."
    return f"{left} pages are scans {why}, so they haven't been looked through."


def find_private(pages) -> list[dict]:
    """The check before emailing or sharing: card, bank, National Insurance and NHS numbers the rules are sure of."""
    return [f for f in find_personal(pages) if f["kind"] in PRIVATE_KINDS and f["conf"] >= 0.85]


def page_lines(pg) -> list[tuple[str, list]]:
    """[(line text, [(start, end, rect), ...])] in reading order, built from words (keeps exact positions)."""
    lines, cur, key = [], None, None
    for w in pg.get_text("words", sort=True):
        k = (w[5], w[6])
        if k != key:
            if cur:
                lines.append(cur)
            cur, key = ["", []], k
        start = len(cur[0]) + (1 if cur[0] else 0)
        cur[0] = (cur[0] + " " if cur[0] else "") + w[4]
        cur[1].append((start, start + len(w[4]), fitz.Rect(w[:4])))
    if cur:
        lines.append(cur)
    return [(t, spans) for t, spans in lines]


def _span_rect(spans, s, e):
    r = None
    for a, b, rect in spans:
        if b > s and a < e:
            r = fitz.Rect(rect) if r is None else r | rect
    return r


class FillMixin:
    """Mixed into app.Bridge; relies on EditMixin (_D, _R, _page_ok, _done) and AiMixin (_ai_cfg, _ai_target, _secrets)."""

    # ---- my details
    @safe
    def details_get(self):
        d = load_details()
        return {"details": {k: d.get(k, "") for k in FIELD_KEYS}, "custom": d.get("custom", []),
                "fields": [{"key": k, "label": label, "group": g, "sensitive": s} for k, label, g, s in FIELDS],
                "saved": any(d.get(k) for k in FIELD_KEYS)}

    @safe
    def details_set(self, details: dict, custom: list | None = None):
        clean = {k: str(details.get(k) or "").strip()[:200] for k in FIELD_KEYS}
        dob = clean.get("date_of_birth")
        if dob:
            try:
                date.fromisoformat(dob)
            except ValueError:
                raise UserError("Enter the date of birth with the date picker (day, month, year).")
        if clean.get("email") and "@" not in clean["email"]:
            raise UserError("That email address doesn't look right.")
        data = {**clean, "custom": [{"label": str(c.get("label", ""))[:60], "value": str(c.get("value", ""))[:200]}
                                    for c in (custom or []) if c.get("label") and c.get("value")]}
        save_details(data)
        return {**self.details_get(), "status": f"Your details are saved {WHERE}."}

    @safe
    def details_clear(self):
        plain = os.environ.get("TOOLKIT_SECRETS_DIR")
        if mac_keychain.IS_MAC and not plain:
            try:
                mac_keychain.delete(KEYCHAIN_ITEM)
            except mac_keychain.KeychainError as e:
                raise UserError(f"Your Mac's Keychain wouldn't delete your details ({e.reason}). Try again.")
        p = Path(plain) / "mydetails.json" if plain else DETAILS_FILE
        if p.exists():
            p.unlink()
        where = "from your Mac's Keychain" if mac_keychain.IS_MAC else "from this PC"
        return {**self.details_get(), "status": f"Your saved details were deleted {where}."}

    # ---- planning a fill
    def _near_text(self, pg, rect) -> str:
        """Words just left of / above a box on the page - usually its printed label."""
        left, above = [], []
        for w in pg.get_text("words"):
            r = fitz.Rect(w[:4])
            cy = (r.y0 + r.y1) / 2
            if r.x1 <= rect.x0 + 2 and rect.y0 - 4 <= cy <= rect.y1 + 4 and rect.x0 - r.x1 < 240:
                left.append((r.x0, w[4]))
            elif r.y1 <= rect.y0 + 1 and rect.y0 - r.y1 < 22 and r.x0 < rect.x1 and r.x1 > rect.x0 - 20:
                above.append((r.x0, w[4]))
        pick = left or above
        return " ".join(t for _, t in sorted(pick))[-80:]

    def _paper_slots(self, i, pg):
        """Printed labels like 'Name: ______' or 'Date of birth:' on forms without real boxes."""
        out = []
        words = pg.get_text("words")
        for text, spans in page_lines(pg):
            if len(text) > 70:
                continue
            m = re.match(r"^(.{2,40}?)\s*(?::|_{3,}|\.{4,})", text)
            label = m.group(1) if m else (text if len(text) < 30 else "")
            key = match_label(label)
            if not key:
                continue
            lab_end = m.end(1) if m else len(text)
            lr = _span_rect(spans, 0, lab_end) or spans[0][2]
            under = re.search(r"_{3,}|\.{4,}", text)
            if under:
                ur = _span_rect(spans, under.start(), under.end())
                x, top = ur.x0 + 2, ur.y0 - 1
            else:
                line_r = _span_rect(spans, 0, len(text))
                right = [fitz.Rect(w[:4]) for w in words if w[0] > line_r.x1 and abs((w[1] + w[3]) / 2 - (line_r.y0 + line_r.y1) / 2) < 4]
                if right and right[0].x0 - line_r.x1 < 60:
                    continue                  # something already written there
                x, top = line_r.x1 + 8, line_r.y0
                if x > pg.rect.width - 80:
                    x, top = lr.x0, line_r.y1 + 2
            size = max(8.0, min(12.0, lr.height * 0.82))
            out.append({"kind": "paper", "page": i, "label": label.strip(), "match": key,
                        "at": self._D(i, fitz.Rect(x, top, x + 1, top + 1))[:2], "size": round(size, 1)})
        return out

    @safe
    def fill_plan(self, use_ai: bool = True):
        details = load_details()
        items = []
        with self._lock:
            m = self._need_doc()
            for i in range(len(m)):
                pg = m.page(i)
                widgets = [w for w in (pg.widgets() or []) if w.field_type in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX)]
                for w in widgets:
                    if w.field_flags & 1:
                        continue
                    near = self._near_text(pg, w.rect)
                    tip = w.field_label or ""
                    name = w.field_name or ""
                    label = tip or near or name
                    key = match_label(near) or match_label(tip) or match_label(re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " "))
                    items.append({"kind": "field", "page": i, "xref": w.xref, "label": label, "near": near, "name": name,
                                  "match": key, "source": "rule" if key else None, "current": w.field_value or "",
                                  "maxlen": w.text_maxlen or 0, "rect": self._D(i, w.rect),
                                  "choices": [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]})
                if not widgets:
                    items += self._paper_slots(i, pg)
            doc_name = (self._source or m.path or Path("form.pdf")).name
        ai_used, ai_note = False, None
        unmatched = [it for it in items if it["kind"] == "field" and not it["match"] and (it["near"] or it["label"])]
        if use_ai and unmatched:
            try:
                got = self._ai_match_labels(unmatched, doc_name)
                if got is not None:
                    ai_used = True
                    for it in unmatched:
                        k = got.get(it["xref"])
                        if k:
                            it["match"], it["source"] = k, "ai"
            except UserError as e:
                ai_note = str(e)
        for it in items:
            if it["match"]:
                it["value"] = value_for(it["match"], details, it["label"], it.get("maxlen") or 0)
                if it["kind"] == "field" and it["choices"] and it["value"] not in it["choices"]:
                    it["value"] = next((c for c in it["choices"] if c.lower() == it["value"].lower()), "")
            else:
                it["value"] = ""
        return {"items": items, "details_saved": any(details.get(k) for k in FIELD_KEYS), "ai_used": ai_used,
                "ai_note": ai_note, "describe": DESCRIBE, "paper": any(it["kind"] == "paper" for it in items)}

    # ---- AI for the boxes the rules couldn't place (labels only - never the person's details)
    def _ai_match_labels(self, items, doc_name):
        cfg = self._ai_cfg()
        if not cfg.get("enabled"):
            return None
        options = {k: v for k, v in DESCRIBE.items()} | {"none": "None of these - leave it empty"}
        dec = cfg.get("decide") or {}
        if dec.get("provider") not in (None, "", "none"):
            return self._decide_match(items, options, dec, cfg)
        return self._llm_match(items, options, cfg)

    def _decide_endpoint(self, dec, cfg):
        p = dec["provider"]
        if p == "openrouter":
            return "https://openrouter.ai/api/alpha/decisions", self._secrets.get("openrouter")
        if p == "relay":
            base = (cfg.get("base_urls", {}).get("relay") or "").rstrip("/")
            return (base + "/decisions") if base else None, self._secrets.get("relay")
        return None, None

    def decide(self, questions: dict, state, cfg=None):
        """One request to the quick-decisions service -> {question_id: answer}.  Raises UserError on failure."""
        cfg = cfg or self._ai_cfg()
        dec = cfg.get("decide") or {}
        url, key = self._decide_endpoint(dec, cfg)
        if not url or not key:
            raise UserError("Quick decisions needs its key - add your OpenRouter key in Settings → AI assistant.")
        body = {"model": dec.get("model") or "typesafe/jev-1.13", "state": state, "questions": questions}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("answers") or {}
        except urllib.error.HTTPError as e:
            from bridge_ai import _friendly_http
            raise UserError(_friendly_http(e.code, e.read().decode("utf-8", "replace"), "openrouter"))
        except urllib.error.URLError:
            raise UserError("Can't reach the quick-decisions service - check the internet connection.")

    def _decide_match(self, items, options, dec, cfg):
        state = {"form": "a form", "boxes": {f"b{n}": {"label": it["label"], "printed_near_box": it["near"]}
                                                       for n, it in enumerate(items)}}
        questions = {f"b{n}": {"type": "choice",
                               "instructions": f"Which of my saved details belongs in form box b{n} (labelled “{it['label'][:60]}”)?",
                               "criteria": options} for n, it in enumerate(items[:400])}
        answers = self.decide(questions, state, cfg)
        out = {}
        for n, it in enumerate(items[:400]):
            a = answers.get(f"b{n}") or {}
            ch = a.get("choice")
            prob = (a.get("probabilities") or {}).get(ch, a.get("confidence", 0)) if ch else 0
            if ch and ch != "none" and ch in DESCRIBE and prob >= 0.6:
                out[it["xref"]] = ch
        return out

    def _llm_match(self, items, options, cfg):
        from bridge_ai import stream_claude, stream_openai
        p, pre, base, model, key = self._ai_target(cfg)
        boxes = [{"id": n, "label": it["label"][:80], "near": it["near"][:80]} for n, it in enumerate(items[:150])]
        prompt = ("These are the labels of boxes on a form. For each box, say which of my saved details belongs in it, "
                  "using only these keys: " + ", ".join(f"{k} ({v})" for k, v in options.items()) +
                  ".\nReply with JSON only, like {\"0\": \"first_name\", \"1\": \"none\"}.\n\nBoxes:\n" + json.dumps(boxes))
        out_text = []
        msgs = [{"role": "user", "content": prompt}]
        system = "You match form box labels to the names of saved personal details. You never see the details themselves."
        if pre["kind"] == "anthropic":
            stream_claude(key, model, base, system, "(form box labels are in the message)", msgs, out_text.append, lambda: False, timeout=60)
        else:
            stream_openai(base, key, model, system, msgs, out_text.append, lambda: False, p, timeout=60)
        txt = "".join(out_text)
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            return {}
        try:
            got = json.loads(m.group(0))
        except ValueError:
            return {}
        return {items[int(n)]["xref"]: k for n, k in got.items()
                if str(n).isdigit() and int(n) < len(items) and k in DESCRIBE}

    # ---- filling
    @safe
    def fill_apply(self, items: list):
        todo = [it for it in items if str(it.get("value") or "").strip()]
        if not todo:
            raise UserError("Nothing to fill - tick at least one box with a value.")
        with self._lock:
            m = self._need_doc()
            m.snapshot()                                   # one undo step for the whole fill
            n = 0
            for it in todo:
                i = int(it["page"])
                pg = m.page(i)
                m._held_page = pg
                if it.get("kind") == "field":
                    w = next((w for w in pg.widgets() if w.xref == int(it["xref"])), None)
                    if not w or w.field_flags & 1:
                        continue
                    w.field_value = str(it["value"])
                    w.update()
                    n += 1
                elif it.get("kind") == "paper":
                    x, y = it["at"]
                    size = float(it.get("size") or 10)
                    text = str(it["value"])
                    wdt = fitz.get_text_length(text, fontname="helv", fontsize=size) + 6
                    r = self._R(i, [x, y, x + wdt, y + size * 1.35 + 4])
                    a = pg.add_freetext_annot(r, text, fontsize=size, fontname="helv", text_color=(0.07, 0.09, 0.15))
                    a.update()
                    n += 1
            m._held_page = None
            return {**self._info(), "changed": None,
                    "status": f"Filled {n} box{'es' if n != 1 else ''}. Check them, then Save. {'⌘Z' if mac_keychain.IS_MAC else 'Ctrl+Z'} undoes the whole fill."}

    # ---- personal-details finder
    @safe
    def pii_scan(self, use_ai: bool = True, stopped: bool = False):
        """[stopped]: the scan's words were being read for this and the person stopped it - said in [left]."""
        own = own_details(load_details())
        with self._lock:
            m = self._need_doc()
            kept = find_personal((m.page(i) for i in range(len(m))), own, wider=True)
            # pages that are still pictures of the paper, with no words to look through: never passed over in silence
            scans = [i for i in range(len(m)) if scanwords.is_scan(m.page(i))]
            pages = len(m)
            out = [{"id": n, "page": f["page"], "kind": f["kind"], "label": f["label"], "text": f["text"],
                    "rect": self._D(f["page"], f["rect"]), "more": [self._D(f["page"], r) for r in f["more"]],
                    "conf": f["conf"], "context": f["line"][:120]} for n, f in enumerate(kept)]
        ai_used, ai_note = False, None
        doubtful = [o for o in out if o["conf"] < 0.85 and o["kind"] not in WIDER_KINDS]     # an address stays unticked
        cfg = self._ai_cfg()
        if use_ai and doubtful and cfg.get("enabled") and (cfg.get("decide") or {}).get("provider") not in (None, "", "none"):
            try:
                qs = {f"c{o['id']}": {"type": "noul",
                                      "instructions": f"Is “{o['text']}” in item c{o['id']} a piece of someone's personal, contact or financial information?",
                                      "criteria": {"true": "It identifies or contacts a person, or is their bank, card, health or ID number.",
                                                   "false": "It's something else, like a reference number, price, date of an event or a company detail."}}
                      for o in doubtful[:400]}
                ans = self.decide(qs, {"items": {f"c{o['id']}": o["context"] for o in doubtful[:400]}}, cfg)
                ai_used = True
                for o in doubtful[:400]:
                    a = ans.get(f"c{o['id']}") or {}
                    if isinstance(a.get("noul"), (int, float)):
                        o["conf"] = round(float(a["noul"]), 2)
                        o["ai"] = True
            except UserError as e:
                ai_note = str(e)
        return {"items": out, "ai_used": ai_used, "ai_note": ai_note, "details_saved": bool(own),
                "scans": scans, "pages": pages, "left": scans_left(len(scans), pages, stopped),
                "nothing_found": NOTHING_FOUND, "scans_read": SCANS_READ}

    @safe
    def pii_redact(self, items: list):
        if not items:
            raise UserError("Tick at least one item to remove.")
        with self._lock:
            m = self._need_doc()
            m.snapshot()
            before = m.undo_stack[-1]
            gone: list[tuple[int, list]] = []        # what has been taken out so far: done again after a failure
            why = ""

            def take(i: int, its: list) -> bool:
                """Take these items out of page i; False (and the document as it was before, less what had gone) when
                it can't be done - a failed try may have half changed the page."""
                nonlocal why
                rects = [self._R(i, r) for it in its for r in [it["rect"], *(it.get("more") or [])]]
                try:
                    pg = m.page(i)
                    for r in rects:
                        pg.add_redact_annot(r, fill=(0, 0, 0))
                    pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
                    gone.append((i, rects))
                    return True
                except Exception:
                    log.warning("pii_redact: page %d:\n%s", i + 1, traceback.format_exc())
                    why = "It couldn't be taken out of its page safely."
                    m._restore(before)
                    for p, done in gone:
                        pg = m.page(p)
                        for r in done:
                            pg.add_redact_annot(r, fill=(0, 0, 0))
                        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
                    return False

            pages: dict[int, list] = {}
            for it in items:
                pages.setdefault(int(it["page"]), []).append(it)
            stuck = []
            for i in sorted(pages):              # each page whole; when that fails, each of its items by itself
                if not take(i, pages[i]):
                    stuck += [it for it in pages[i] if not take(i, [it])]
            n = len(items)
            if not stuck:
                return {**self._info(), "changed": None,
                        "status": f"Removed {n} item{'s' if n != 1 else ''} for good - the text is gone from the file, not just covered. Save to keep it."}
            if len(stuck) == n:                  # nothing changed: no undo step for it
                m.undo_stack.pop()
                raise UserError(f"Nothing was removed. {why}")
            names = ", ".join(f"“{it['text']}”" for it in stuck[:3]) + (f" and {len(stuck) - 3} more" if len(stuck) > 3 else "")
            return {**self._info(), "changed": None, "stuck": [it.get("id") for it in stuck],
                    "status": f"Removed {n - len(stuck)} of {n} items for good. Still there: {names}. {why} Save to keep the rest."}

