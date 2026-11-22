"""What does this letter want? - the letter card in Ask.

The AI fills in a fixed shape (prompts.json "card": who it's from, what it's about, what it asks, the deadline, the
amount, reference numbers, how to contact them, the next step), each with its page.  `normalise` tidies the answer and
checks what can be checked against the pages' own text: the amount, the deadline and the reference numbers must be
printed in the document.  One that isn't stays on the card marked `found: False` (the card says "not found in the
letter - check it"), and a page number that's wrong is corrected to the page the words are on.  When some pages are
scans (no text to look in), a value that isn't in the text is `found: None` - it couldn't be checked.

The iPad/iPhone app does the same (LetterCard.swift), checked against this file's results for the same answer and
letter (ios/TidyPaperTests/Fixtures/card.json, made by make_card.py).
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

MAX_ASKS = 4
MAX_REFS = 4
LIMITS = {"from": 120, "about": 240, "ask": 200, "deadline": 80, "amount": 60, "label": 60, "value": 60,
          "contact": 200, "next_step": 240}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def parse(text: str) -> dict:
    """The AI's JSON (the object itself, or one wrapped in ```json fences or a sentence) - bridge_extract's way."""
    s = (text or "").strip()
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if s.find("{") >= 0 else ""):
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    raise ValueError("not JSON")


def words(s: str) -> str:
    """Text reduced to what matters for finding a value in it: compatibility forms (ﬁ -> fi), lower case, thousands
    commas taken out of numbers, and everything but letters, digits and marks (any script: é, ß, Telugu vowel signs),
    £ $ € and decimal points turned into single spaces - so "£1,677.60" and "CT-4471-2290" are found however the
    lines and spaces fall."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)
    s = re.sub(r"\.(?!\d)|(?<!\d)\.", " ", s)              # a full stop, not a decimal point
    s = "".join(c if c in "£$€." or unicodedata.category(c)[0] in "LNM" else " " for c in s)
    return " ".join(s.split())


def found_on(needle: str, pages: list[str]) -> list[int]:
    """The pages (1-based) whose text has these words, as whole words."""
    n = words(needle)
    if not n:
        return []
    return [i + 1 for i, t in enumerate(pages) if f" {n} " in f" {t} "]


def date_forms(iso: str) -> list[str]:
    """Ways a date may be printed: 2 December 2026, 2 Dec 2026, 2nd December 2026, December 2, 2026, 02/12/2026,
    2/12/2026, 02.12.2026, 2026-12-02 (the year left off too: 2 December)."""
    d = date.fromisoformat(iso)
    month, mon = MONTHS[d.month - 1], MONTHS[d.month - 1][:3]
    nth = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    out = []
    for day in (str(d.day), f"{d.day}{nth}"):
        for m in (month, mon):
            out += [f"{day} {m} {d.year}", f"{m} {day} {d.year}", f"{day} {m}"]
    out += [f"{d.day:02d}/{d.month:02d}/{d.year}", f"{d.day}/{d.month}/{d.year}", f"{d.day:02d}.{d.month:02d}.{d.year}",
            f"{d.day}.{d.month}.{d.year}", iso]
    return out


def _clip(v, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _page(v, count: int) -> int:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return 0
    return p if 1 <= p <= count else 0


def _part(v, key: str, count: int) -> dict:
    v = v if isinstance(v, dict) else {}
    text = _clip(v.get("text"), LIMITS[key])
    return {"text": text, "page": _page(v.get("page"), count) if text else 0}


def _checked(value: str, page: int, pages: list[str], also: list[str] | None = None) -> tuple[int, bool | None]:
    """(page, found): the page it's printed on - the one the AI said if it's there, else the first that has it.
    found None: not in the text, but some pages have none to look in (scans) - it couldn't be checked."""
    where: list[int] = []
    for v in [value, *(also or [])]:
        where = found_on(v, pages)
        if where:
            break
    if not where:
        return page, (None if any(not t for t in pages) else False)
    return (page if page in where else where[0]), True


def normalise(ans: dict, pages: list[str]) -> dict:
    """The AI's card (`ans`), tidied and checked against the document's pages (their text, in order)."""
    ans = ans if isinstance(ans, dict) else {}
    count = len(pages)
    texts = [words(t) for t in pages]
    card = {k: _part(ans.get(k), k, count) for k in ("from", "about", "contact", "next_step")}

    asks = []
    for a in ans.get("asks") if isinstance(ans.get("asks"), list) else []:
        p = _part(a, "ask", count)
        if p["text"] and p["text"] not in [x["text"] for x in asks]:
            asks.append(p)
    card["asks"] = asks[:MAX_ASKS]

    a = _part(ans.get("amount"), "amount", count)
    if a["text"]:
        a["page"], a["found"] = _checked(a["text"], a["page"], texts)
    card["amount"] = a

    dl = ans.get("deadline") if isinstance(ans.get("deadline"), dict) else {}
    text = _clip(dl.get("text"), LIMITS["deadline"])
    iso = str(dl.get("date") or "").strip()
    try:
        forms = date_forms(iso) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso) else []
    except ValueError:                                 # 2026-02-30
        forms = []
    if not forms:
        iso = ""
    deadline = {"date": iso, "text": text or (_human(iso) if iso else ""), "page": _page(dl.get("page"), count)}
    if deadline["text"]:
        deadline["page"], deadline["found"] = _checked(text, deadline["page"], texts, forms)
    else:
        deadline["page"] = 0
    card["deadline"] = deadline

    refs = []
    for r in ans.get("references") if isinstance(ans.get("references"), list) else []:
        if not isinstance(r, dict):
            continue
        value = _clip(r.get("value"), LIMITS["value"])
        if not value or value in [x["value"] for x in refs]:
            continue
        ref = {"label": _clip(r.get("label"), LIMITS["label"]) or "Reference", "value": value,
               "page": _page(r.get("page"), count)}
        ref["page"], ref["found"] = _checked(value, ref["page"], texts)
        refs.append(ref)
    card["references"] = refs[:MAX_REFS]
    card["empty"] = not any([card["from"]["text"], card["about"]["text"], asks, a["text"], deadline["text"], refs])
    return card


def _human(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def calendar_file(card: dict, name: str) -> str:
    """An .ics event for the card's deadline (all day), for "Add to calendar"."""
    d = date.fromisoformat(card["deadline"]["date"])
    who = card["from"]["text"]
    title = f"Deadline: {card['asks'][0]['text'] if card['asks'] else card['about']['text'] or name}"
    notes = [f"From {who}" if who else "", f"Amount: {card['amount']['text']}" if card["amount"]["text"] else ""]
    notes += [f"{r['label']}: {r['value']}" for r in card["references"]]
    notes.append(f"Document: {name}")

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    stamp = date.today().strftime("%Y%m%d")
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TidyPaper//Letter card//EN", "BEGIN:VEVENT",
        f"UID:{d.isoformat()}-{abs(hash(title)) % 10**8}@tidypaper", f"DTSTAMP:{stamp}T000000Z",
        f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}", f"SUMMARY:{esc(_clip(title, 120))}",
        f"DESCRIPTION:{esc(chr(10).join(n for n in notes if n))}",
        "BEGIN:VALARM", "TRIGGER:-P2D", "ACTION:DISPLAY", f"DESCRIPTION:{esc(_clip(title, 120))}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", ""])
