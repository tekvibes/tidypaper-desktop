"""Pull out data: a PDF's details and rows into a table to check, edit, copy or save as CSV / Excel.

Two ways in:
    Tables as they are   tables in a real-text PDF, read on this computer by PyMuPDF's table finder - free, exact,
                         nothing sent anywhere
    A template           bank statement, invoice or receipt, payslip, or fields the person names: the AI answers in a
                         fixed JSON shape (structured output), every value with the page it is on; long documents go a
                         few pages at a time, a few parts at once (at_once; one at a time to an AI on this
                         computer), and the parts are joined in page order

Then checks where the numbers allow (transactions add up to the closing balance, net + VAT = total, a payslip's
deductions) so a misread figure is flagged rather than trusted.  Templates, the prompt and the shape are in
prompts.json ("extract"), shared with the iPad/iPhone app (ios/TidyPaper/Extract.swift) - the checks' sentences too
are the same on both, word for word (ios/TidyPaperTests/Fixtures/make_extract.py writes the desktop's answers for
the iPad's tests).
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path

import bridge_ai as ai
from bridge_util import UserError, fitz, log, safe

SPEC = ai.PROMPTS["extract"]
ALL = SPEC["all"]
TEMPLATES = {t["id"]: t for t in SPEC["templates"]}
RELAY_JOB_HEADER = "X-TidyPaper-Job"         # tells a relay this is Pull out data (it may use its model for that)
BUSY_TRIES = 5                                # waits for one part before giving up


def busy_wait() -> float:
    """Seconds until a relay's next minute (its per-minute count starts again), with a little to spare."""
    return 62 - time.time() % 60


# ------------------------------------------------------------------ templates and the answer's shape

def slug(label: str, taken: set[str], n: int) -> str:
    """A JSON key for a field the person named ("Policy number" -> "policy_number")."""
    k = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or f"f{n}"
    base, i = k, 2
    while k in taken:
        k, i = f"{base}_{i}", i + 1
    taken.add(k)
    return k


def template(tid: str, own: dict | None = None) -> dict:
    """A template with every part filled in.  own (tid "own"): {"labels": [...], "list": bool, "one": "item"} -
    the labels are single values, or (list) the columns of a row that repeats."""
    if tid == "all":                             # everything in it: the AI finds and names the tables and values
        return {"id": "all", "label": ALL["label"], "fields": [], "rows": None}
    if tid != "own":
        if tid not in TEMPLATES:
            raise UserError("Choose what to pull out.")
        t = json.loads(json.dumps(TEMPLATES[tid]))
    else:
        labels = [" ".join(str(x).split())[:80] for x in (own or {}).get("labels") or []]
        labels = list(dict.fromkeys(x for x in labels if x))[:30]
        if not labels:
            raise UserError("Type the names of what to pull out, one on each line.")
        taken: set[str] = set()
        items = [{"key": slug(x, taken, i + 1), "label": x} for i, x in enumerate(labels)]
        one = " ".join(str((own or {}).get("one") or "").split())[:40] or SPEC["own_rows_default"]
        t = {"id": "own", "label": "Your own fields", "fields": [], "rows": None}
        if (own or {}).get("list"):
            t["rows"] = {"label": f"{one[:1].upper()}{one[1:]}s" if not one.endswith("s") else one.capitalize(),
                         "one": one, "columns": items,
                         "hint": f"One row for each {one}, in the order printed."}
        else:
            t["fields"] = items
    for f in t.get("fields") or []:
        f.setdefault("type", "text")
        f.setdefault("merge", "first")
    if t.get("rows"):
        for c in t["rows"]["columns"]:
            c.setdefault("type", "text")
    t.setdefault("fields", [])
    t.setdefault("rows", None)
    return t


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema_all() -> dict:
    """Everything in it: one shape for any document - values, and tables of rows of cells."""
    s, i = {"type": "string"}, {"type": "integer"}
    strings = {"type": "array", "items": s}
    return _obj({"document_type": s,
                 "values": {"type": "array", "items": _obj({"section": s, "label": s, "value": s, "unit": s, "page": i})},
                 "tables": {"type": "array", "items": _obj({"name": s, "note": s, "columns": strings,
                                                            "rows": {"type": "array", "items": _obj({"cells": strings, "marks": s, "page": i})},
                                                            "footnotes": {"type": "array", "items": _obj({"mark": s, "text": s})}})},
                 "doubts": strings})


def schema(t: dict) -> dict:
    """The JSON shape the AI must answer in (strict: every key present, nothing else)."""
    if t["id"] == "all":
        return schema_all()
    props, req = {}, []
    if t["fields"]:
        vals = {f["key"]: {"type": "object", "additionalProperties": False, "required": ["value", "page"],
                           "properties": {"value": {"type": "string", "description": f["label"]},
                                          "page": {"type": "integer"}}} for f in t["fields"]}
        props["fields"] = {"type": "object", "additionalProperties": False, "required": [f["key"] for f in t["fields"]],
                           "properties": vals}
        req.append("fields")
    if t["rows"]:
        cols = {c["key"]: {"type": "string", "description": c["label"]} for c in t["rows"]["columns"]}
        cols["page"] = {"type": "integer"}
        props["rows"] = {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                    "required": [c["key"] for c in t["rows"]["columns"]] + ["page"],
                                                    "properties": cols}}
        req.append("rows")
    props["doubts"] = {"type": "array", "items": {"type": "string"}}
    req.append("doubts")
    return {"type": "object", "additionalProperties": False, "required": req, "properties": props}


def example(t: dict) -> str:
    """The shape written out, for a service that can't be given it as a schema."""
    out = {}
    if t["fields"]:
        out["fields"] = {f["key"]: {"value": f"<{f['label']}>", "page": 1} for f in t["fields"]}
    if t["rows"]:
        out["rows"] = [{**{c["key"]: f"<{c['label']}>" for c in t["rows"]["columns"]}, "page": 1}]
    out["doubts"] = []
    return json.dumps(out, ensure_ascii=False)


def _strings(xs: list[str]) -> str:
    """["", "Q3 2026"] written the same way on the iPad (no escapes but \\ and \")."""
    return "[" + ", ".join('"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"' for x in xs) + "]"


def prompt(t: dict, pages: list[int], count: int, shape_in_prompt: bool = False, known: list[dict] | None = None,
           again: bool = False) -> str:
    """What to pull out of these pages (0-based page numbers).  known (everything in it): the tables found on earlier
    pages, [{"name", "columns"}], so one carried over keeps its name and columns.  again: the second look at a page."""
    if t["id"] == "all":
        lines = [ALL["prompt"]]
        if known:
            lines += ["", ALL["known"]] + [f"- {k['name']}: {_strings(k['columns'])}" for k in known[-20:]]
        if again:
            lines += ["", ALL["again"]]
        if count > len(pages):
            lines += ["", SPEC["pages_note"].format(pages=ai.page_ranges(pages), count=count)]
        if shape_in_prompt:
            lines += ["", "Answer with only a JSON object (no other words) in exactly this shape:", ALL["example"]]
        return "\n".join(lines)
    lines = [f"Pull out: {t['label']}."]
    if t["fields"]:
        lines += ["", "Single values (fields):"] + [f"- {f['key']}: {f['label']}" for f in t["fields"]]
    if t["rows"]:
        r = t["rows"]
        lines += ["", f"{r['label']} (rows): {r['hint']}", "Columns:"] + [f"- {c['key']}: {c['label']}" for c in r["columns"]]
    if count > len(pages):
        lines += ["", SPEC["pages_note"].format(pages=ai.page_ranges(pages), count=count)]
    if shape_in_prompt:
        lines += ["", "Answer with only a JSON object (no other words) in exactly this shape:", example(t)]
    return "\n".join(lines)


def parse_answer(text: str) -> dict:
    """The AI's JSON (the object itself, or one wrapped in ```json fences or a sentence)."""
    s = (text or "").strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        try:
            v = json.loads(s[a:b + 1])
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    raise ValueError("not JSON")


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def _page(v, pages: list[int]) -> int:
    """A 1-based page number from the answer, kept only when it is one of the pages sent (0 = not known)."""
    try:
        p = int(v)
    except (TypeError, ValueError):
        return 0
    return p if p - 1 in pages else (pages[0] + 1 if len(pages) == 1 and p else 0)


def normalise(t: dict, ans: dict, pages: list[int]) -> dict:
    """The answer in a tidy form: {"fields": {key: {"value", "page"}}, "rows": [{"cells": {key: text}, "page"}],
    "doubts": [...]}.  Anything missing is empty; unknown keys are dropped."""
    fields = {}
    got = ans.get("fields") if isinstance(ans.get("fields"), dict) else {}
    for f in t["fields"]:
        v = got.get(f["key"])
        if isinstance(v, dict):
            val = _text(v.get("value"))
            fields[f["key"]] = {"value": val, "page": _page(v.get("page"), pages) if val else 0}
        else:
            val = _text(v)
            fields[f["key"]] = {"value": val, "page": pages[0] + 1 if val and len(pages) == 1 else 0}
    rows = []
    if t["rows"]:
        for r in ans.get("rows") if isinstance(ans.get("rows"), list) else []:
            if not isinstance(r, dict):
                continue
            cells = {c["key"]: _text(r.get(c["key"])) for c in t["rows"]["columns"]}
            if any(cells.values()):
                rows.append({"cells": cells, "page": _page(r.get("page"), pages)})
    doubts = [_text(d) for d in ans.get("doubts") or [] if _text(d)] if isinstance(ans.get("doubts"), list) else []
    return {"fields": fields, "rows": rows, "doubts": doubts}


def merge(t: dict, parts: list[dict]) -> dict:
    """Join the parts of a long document (in page order): a single value comes from the first part that has it
    (or the last, for closing balances and totals); rows follow one another."""
    fields = {}
    for f in t["fields"]:
        have = [p["fields"][f["key"]] for p in parts if p["fields"].get(f["key"], {}).get("value")]
        fields[f["key"]] = dict((have[-1] if f.get("merge") == "last" else have[0]) if have else {"value": "", "page": 0})
    rows = [r for p in parts for r in p["rows"]]
    doubts = list(dict.fromkeys(d for p in parts for d in p["doubts"]))
    return {"fields": fields, "rows": rows, "doubts": doubts}


# ------------------------------------------------------------------ everything in it (no template)

def _list(v) -> list:
    return v if isinstance(v, list) else []


_MARK = re.compile(r"\(\w{1,3}\)|[*†‡§¶#¹²³⁴⁵⁶⁷⁸⁹⁰]+")


def unmark(cells: list[str], marks: str) -> list[str]:
    """A row's cells without the footnote marks it already lists in marks, when the AI left one at a cell's start or
    end as well ("(3) Outstanding at September 30", "480 (4)") - a mark is never part of a label or a figure."""
    found, out = _MARK.findall(marks or ""), []
    for c in cells:
        before = None
        while before != c:                           # "Note (1)(2)": each mark in turn, whatever order they're listed in
            before = c
            for m in found:
                if c != m and c.endswith(m) and re.search(r"\w", c[:-len(m)]):
                    c = c[:-len(m)].rstrip()
                if c != m and c.startswith(m) and re.search(r"\w", c[len(m):]):
                    c = c[len(m):].lstrip()
        out.append(c)
    return out


def normalise_all(ans: dict, pages: list[int]) -> dict:
    """An everything-in-it answer, tidied: {"document_type", "values": [{"section", "label", "value", "unit", "page"}],
    "tables": [{"name", "note", "columns": [..], "rows": [{"cells": [..], "marks", "page"}], "footnotes": [{"mark",
    "text"}]}], "doubts"}.  Every row has as many cells as there are columns; empty values, rows and tables are dropped."""
    values = []
    for v in _list(ans.get("values")):
        if isinstance(v, dict) and _text(v.get("value")):
            values.append({"section": _text(v.get("section")), "label": _text(v.get("label")), "value": _text(v.get("value")),
                           "unit": _text(v.get("unit")), "page": _page(v.get("page"), pages)})
    tables = []
    for tb in _list(ans.get("tables")):
        if not isinstance(tb, dict):
            continue
        cols = [_text(c) for c in _list(tb.get("columns"))]
        rows = []
        for r in _list(tb.get("rows")):
            cells = [_text(c) for c in _list(r.get("cells"))] if isinstance(r, dict) else []
            marks = _text(r.get("marks")) if isinstance(r, dict) else ""
            cells = unmark(cells, marks) if marks else cells
            if any(cells):
                rows.append({"cells": cells, "marks": marks, "page": _page(r.get("page"), pages)})
        width = max([len(cols)] + [len(r["cells"]) for r in rows])
        cols += [""] * (width - len(cols))
        for r in rows:
            r["cells"] = (r["cells"] + [""] * width)[:width]
        rows = [r for r in rows if not (any(cols) and r["cells"] == cols)]     # the heading row given again as a row
        notes = [{"mark": _text(f.get("mark")), "text": _text(f.get("text"))} for f in _list(tb.get("footnotes"))
                 if isinstance(f, dict) and _text(f.get("text"))]
        if rows:
            tables.append({"name": _text(tb.get("name")) or "Table", "note": _text(tb.get("note")), "columns": cols,
                           "rows": rows, "footnotes": notes})
    doubts = [_text(d) for d in _list(ans.get("doubts")) if _text(d)]
    return {"document_type": _text(ans.get("document_type")), "values": values, "tables": tables, "doubts": doubts}


def _name_key(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def merge_all(parts: list[dict]) -> dict:
    """Join the parts of a long document: values follow one another (a repeat - same section, label and value - is
    left out); a table carried over (same name, and the same columns or none) is joined to the one before - unless it
    starts with the same row as that one (then it is another table of the same kind: the same summary for another
    date, say)."""
    out = {"document_type": "", "values": [], "tables": [], "doubts": []}
    seen = set()
    for p in parts:
        out["document_type"] = out["document_type"] or p["document_type"]
        for v in p["values"]:
            k = (v["section"].lower(), v["label"].lower(), v["value"])
            if k not in seen:
                seen.add(k)
                out["values"].append(dict(v))
        for tb in p["tables"]:
            prev = next((x for x in reversed(out["tables"]) if _name_key(x["name"]) == _name_key(tb["name"])
                         and len(x["columns"]) == len(tb["columns"]) and (x["columns"] == tb["columns"] or not any(tb["columns"]))), None)
            if prev and tb["rows"][0]["cells"][0] and tb["rows"][0]["cells"][0] == prev["rows"][0]["cells"][0]:
                prev = None                          # starts as that one did: another table of the same kind
            if prev:
                rows = tb["rows"][1:] if tb["rows"][0]["cells"] == prev["columns"] else tb["rows"]
                prev["rows"] += [dict(r, cells=list(r["cells"])) for r in rows]
                prev["note"] = prev["note"] or tb["note"]
                prev["footnotes"] += [f for f in tb.get("footnotes") or [] if f not in prev["footnotes"]]
            else:
                out["tables"].append(dict(tb, columns=list(tb["columns"]), rows=[dict(r, cells=list(r["cells"])) for r in tb["rows"]],
                                          footnotes=[dict(f) for f in tb.get("footnotes") or []]))
        out["doubts"] += [d for d in p["doubts"] if d not in out["doubts"]]
    out["tables"] = [tb for tb in out["tables"] if tb["rows"]]
    return out


def known_tables(parts: list[dict]) -> list[dict]:
    """The tables found so far, for the next part's prompt."""
    return [{"name": tb["name"], "columns": tb["columns"]} for tb in merge_all(parts)["tables"]] if parts else []


def nil(s: str) -> bool:
    """A dash standing for nothing / zero in a column of figures ("—", "–", "-", "$ —", "(—)")."""
    return bool(re.fullmatch(r"\(?\s*(?:[£$€¥]|[A-Z]{3})?\s*[-‐‑‒–—―−]+\s*\)?", (s or "").strip()))


def _numeric(cells: list[str], heading: str) -> bool:
    """A column of figures: most of its cells are amounts (and none is a percentage).  Dashes don't count either way
    (a column of mostly "—" with a few amounts is still figures), and a column of nothing but dashes is figures too
    (all nothing: nulls)."""
    have = [c for c in cells if c and not nil(c)]
    if "%" in heading or any("%" in c for c in have):
        return False
    if not have:
        return any(nil(c) for c in cells if c)
    return sum(1 for c in have if cents(c) is not None) * 10 >= len(have) * 6


def expand_all(res: dict) -> list[dict]:
    """An everything-in-it result as results the table screen already shows: the values (a "found_values" result,
    sections and units kept) and one "found" result per table (columns of figures marked as numbers)."""
    out = []
    if res["values"]:
        taken: set[str] = set()
        fields, vals = [], {}
        for i, v in enumerate(res["values"]):
            k = slug(v["label"] or v["section"] or "value", taken, i + 1)
            fields.append({"key": k, "label": v["label"] or "Value", "section": v["section"], "unit": v["unit"],
                           "type": "text", "merge": "first"})
            vals[k] = {"value": v["value"], "page": v["page"]}
        out.append({"template": {"id": "found_values", "label": ALL["values_label"], "fields": fields, "rows": None},
                    "fields": vals, "rows": [], "doubts": []})
    for tb in res["tables"]:
        taken = set()
        cols = []
        for j, c in enumerate(tb["columns"]):
            k = slug(c or ("label" if j == 0 else f"column {j + 1}"), taken, j + 1)
            kind = "number" if _numeric([r["cells"][j] for r in tb["rows"]], c) else "text"
            cols.append({"key": k, "label": c or ("" if j == 0 else f"Column {j + 1}"), "type": kind})
        marks = any(r.get("marks") for r in tb["rows"])
        if marks:                                    # the footnote marks printed by a row: a column of their own
            cols.append({"key": slug(ALL["footnote_label"], taken, len(cols) + 1), "label": ALL["footnote_label"], "type": "text"})
        t = {"id": "found", "label": tb["name"], "note": tb["note"], "fields": [],
             "rows": {"label": tb["name"], "one": "row", "columns": cols}, "footnotes": list(tb.get("footnotes") or [])}
        rows = [{"cells": {c["key"]: (r["cells"][j] if j < len(r["cells"]) else r.get("marks", "")) for j, c in enumerate(cols)},
                 "page": r["page"]} for r in tb["rows"]]
        out.append({"template": t, "fields": {}, "rows": rows, "doubts": []})
    if out:
        out[0]["doubts"] = list(res["doubts"])
        for r in out:
            r["document_type"] = res["document_type"]
    return out


# ------------------------------------------------------------------ checks

def cents(s: str) -> int | None:
    """An amount as whole pence/cents: "£1,234.50" -> 123450, "(12.00)" / "12.00-" / "12.00 DR" -> -1200,
    "12,50" -> 1250.  None when it isn't an amount."""
    t = re.sub(r"^[£$€¥]\s*", "", (s or "").strip())      # "$ (2,974)": the sign before the brackets
    if not t:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg, t = True, t[1:-1].strip()
    up = t.upper()
    if up.endswith("DR") or up.endswith("CR"):
        neg = neg or up.endswith("DR")
        t = t[:-2].strip()
    if t.endswith("-"):
        neg, t = True, t[:-1].strip()
    if t[:1] in ("-", "−", "–"):
        neg, t = True, t[1:].strip()
    if re.search(r"[A-Za-z]", re.sub(r"^[A-Z]{3}\s*|\s*[A-Z]{3}$", "", t)):
        return None                                  # words, not an amount (a currency code is fine)
    t = re.sub(r"[^\d.,]", "", t)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if re.search(r",\d{1,2}$", t) and t.count(",") == 1 else t.replace(",", "")
    if t.count(".") > 1:
        return None
    whole, _, frac = t.partition(".")
    whole = whole or "0"
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return None
    c = int(whole) * 100 + int((frac + "00")[:2])
    if len(frac) > 2 and frac[2] >= "5":
        c += 1
    return -c if neg else c


def money(c: int) -> str:
    """123450 -> "1,234.50" (no currency sign: the document's own currency isn't always known)."""
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100:,}.{c % 100:02d}"


def _check(level: str, text: str, rows=(), fields=()) -> dict:
    return {"level": level, "text": text, "rows": sorted(set(rows)), "fields": list(fields)}


def _field(res: dict, key: str) -> int | None:
    return cents((res["fields"].get(key) or {}).get("value", ""))


def _col(row: dict, key: str) -> int | None:
    return cents(row["cells"].get(key, ""))


def _unreadable(t: dict, res: dict) -> list[dict]:
    """Money and number cells that aren't amounts at all."""
    out = []
    keys = [c["key"] for c in (t["rows"] or {}).get("columns", []) if c["type"] in ("money", "number")]
    bad = [i for i, r in enumerate(res["rows"]) if any(r["cells"].get(k) and cents(r["cells"][k]) is None for k in keys)]
    if bad:
        out.append(_check("bad", f"{len(bad)} row{'s' if len(bad) > 1 else ''} with an amount that isn't a number - "
                                 "check the flagged rows.", bad))
    fkeys = [f["key"] for f in t["fields"] if f["type"] in ("money", "number")
             and res["fields"].get(f["key"], {}).get("value") and _field(res, f["key"]) is None]
    if fkeys:
        names = ", ".join(f["label"] for f in t["fields"] if f["key"] in fkeys)
        out.append(_check("bad", f"Not a number: {names}.", fields=fkeys))
    return out


def _running(rows: list[dict], start: int | None) -> list[int]:
    """Rows whose balance doesn't follow from the one before (indexes into rows as given)."""
    bad, run = [], start
    for i, r in enumerate(rows):
        if run is not None:
            run += (_col(r, "money_in") or 0) - (_col(r, "money_out") or 0)
        bal = _col(r, "balance")
        if bal is not None:
            if run is not None and run != bal:
                bad.append(i)
            run = bal
    return bad


def check_statement(res: dict) -> list[dict]:
    out = []
    rows = res["rows"]
    opening, closing = _field(res, "opening_balance"), _field(res, "closing_balance")
    if rows and opening is not None and closing is not None:
        got_in = sum(_col(r, "money_in") or 0 for r in rows)
        got_out = sum(_col(r, "money_out") or 0 for r in rows)
        calc = opening + got_in - got_out
        if calc == closing:
            out.append(_check("ok", f"The transactions add up: opening balance {money(opening)} + money in {money(got_in)} "
                                    f"- money out {money(got_out)} = closing balance {money(closing)}."))
        else:
            out.append(_check("bad", f"The transactions don't add up: opening balance {money(opening)} + money in "
                                     f"{money(got_in)} - money out {money(got_out)} = {money(calc)}, but the closing balance "
                                     f"is {money(closing)} ({money(abs(calc - closing))} apart). A row may be missing or misread.",
                              fields=["closing_balance"]))
    if sum(1 for r in rows if _col(r, "balance") is not None) >= 2:
        forward = _running(rows, opening)
        back = _running(rows[::-1], opening)         # a statement printed newest first, read oldest first
        back = [len(rows) - 1 - i for i in back]
        newest_first = len(back) < len(forward)
        bad = back if newest_first else forward
        if bad:
            n = len(bad)
            verb = "balances don't" if n > 1 else "balance doesn't"
            out.append(_check("bad", f"{n} {verb} follow from the row before - "
                                     "check the flagged rows.", bad))
        else:
            out.append(_check("ok", "Each balance follows from the row before"
                                    + (" (newest first)." if newest_first else ".")))
    return out


def _per100(x: int) -> int:
    """x / 100 rounded half away from zero, in whole numbers (the same on the iPad: no float rounding rules)."""
    return (1 if x >= 0 else -1) * ((abs(x) + 50) // 100)


def check_invoice(res: dict) -> list[dict]:
    out = []
    net, vat, total = _field(res, "net"), _field(res, "vat"), _field(res, "total")
    if net is not None and vat is not None and total is not None:
        if net + vat == total:
            out.append(_check("ok", f"Net {money(net)} + VAT {money(vat)} = total {money(total)}."))
        else:
            out.append(_check("bad", f"Net {money(net)} + VAT {money(vat)} = {money(net + vat)}, but the total is "
                                     f"{money(total)}.", fields=["net", "vat", "total"]))
    amounts = [_col(r, "amount") for r in res["rows"]]
    if any(a is not None for a in amounts):
        s = sum(a or 0 for a in amounts)
        if net is not None and s == net:
            out.append(_check("ok", f"The items add up to the net {money(net)}."))
        elif total is not None and s == total:
            out.append(_check("ok", f"The items add up to the total {money(total)}."))
        elif net is not None or total is not None:
            what, v = ("net", net) if net is not None else ("total", total)
            out.append(_check("warn", f"The items add up to {money(s)}, but the {what} is {money(v)} - there may be a "
                                      "discount or delivery charge, or an item was misread."))
    bad = []
    for i, r in enumerate(res["rows"]):
        q, u, a = _col(r, "quantity"), _col(r, "unit_price"), _col(r, "amount")
        if q is not None and u is not None and a is not None and q > 0 and abs(_per100(q * u) - a) > 1:
            bad.append(i)
    if bad:
        out.append(_check("warn", f"{len(bad)} item{'s' if len(bad) > 1 else ''} where quantity × unit price isn't "
                                  "the amount - check the flagged rows.", bad))
    return out


def check_payslip(res: dict) -> list[dict]:
    out = []
    gross, deductions, net = _field(res, "gross_pay"), _field(res, "total_deductions"), _field(res, "net_pay")
    pays = [_col(r, "payment") for r in res["rows"]]
    deds = [_col(r, "deduction") for r in res["rows"]]
    if gross is not None and any(p is not None for p in pays):
        s = sum(p or 0 for p in pays)
        out.append(_check("ok", f"The payments add up to the gross pay {money(gross)}.") if s == gross else
                   _check("warn", f"The payments add up to {money(s)}, but the gross pay is {money(gross)}."))
    if any(d is not None for d in deds):
        s = sum(d or 0 for d in deds)
        if deductions is not None:
            out.append(_check("ok", f"The deductions add up to the total {money(deductions)}.") if s == deductions else
                       _check("warn", f"The deductions add up to {money(s)}, but the total deductions are {money(deductions)}."))
        else:
            deductions = s
    if gross is not None and deductions is not None and net is not None:
        if gross - deductions == net:
            out.append(_check("ok", f"Gross pay {money(gross)} - deductions {money(deductions)} = net pay {money(net)}."))
        else:
            out.append(_check("bad", f"Gross pay {money(gross)} - deductions {money(deductions)} = {money(gross - deductions)}, "
                                     f"but the net pay is {money(net)}.", fields=["net_pay"]))
    return out


_TOTAL = re.compile(r"^(sub-?\s?)?totals?\b|\btotals?$", re.I)
_LESS = re.compile(r"^less\b", re.I)


def _run_match(seen: list[int], v: int, tol: int) -> int:
    """How many of the last amounts add up to v (0: none)."""
    run = 0
    for n, x in enumerate(reversed(seen), 1):
        run += x
        if abs(run - v) <= tol:
            return n
    return 0


def check_totals(t: dict, res: dict) -> list[dict]:
    """A table found with no template: does each Total / Subtotal row add up from the rows above it?  Rows are
    taken as they come; a total stands in for the rows it adds up (so "Total assets" can be "Total current assets"
    plus the rows after it), and it may add up any run of the rows just above it.  A row that is the sum of the rows
    just above it may be a subtotal not called one ("Net carrying amount") - tried both ways; a "Less ..." row shown
    as a positive figure is taken away.  Whole-number columns (figures in thousands or millions) may be 1 out, for
    rounding."""
    cols = t["rows"]["columns"]
    nums = [c["key"] for c in cols if c["type"] == "number"]
    lab = next((c["key"] for c in cols if c["type"] != "number"), None)
    if not nums or lab is None:
        return []
    whole = {k: all((cents(r["cells"].get(k, "")) or 0) % 100 == 0 for r in res["rows"]) for k in nums}
    plain: dict[str, list[int]] = {k: [] for k in nums}      # the amounts so far, a total standing for its rows
    sub: dict[str, list[int]] = {k: [] for k in nums}        # the same, with unnamed subtotals standing for theirs
    good, bad, rounded = 0, [], False
    for i, r in enumerate(res["rows"]):
        label = r["cells"].get(lab, "")
        vals = {k: cents(r["cells"].get(k, "")) for k in nums}
        if _LESS.search(label):
            vals = {k: -v if v is not None and v > 0 else v for k, v in vals.items()}
        if not _TOTAL.search(label):
            for k, v in vals.items():
                if v is None:
                    continue
                n = _run_match(sub[k], v, 0)
                if n >= 2:
                    sub[k][-n:] = [v]
                else:
                    sub[k].append(v)
                plain[k].append(v)
            continue
        tested = wrong = False
        for k, v in vals.items():
            if v is None or not plain[k]:
                continue
            tested = True
            tol = 100 if whole[k] else 0
            for seen in (plain[k], sub[k]):
                n = _run_match(seen, v, tol)
                if n:
                    rounded = rounded or sum(seen[-n:]) != v
                    seen[-n:] = [v]
                    plain[k], sub[k] = list(seen), list(seen)
                    break
            else:
                wrong = True
                plain[k].append(v)
                sub[k].append(v)
        if tested:
            if wrong:
                bad.append(i)
            else:
                good += 1
    if bad:
        n = len(bad)
        return [_check("warn", f"{n} total{'s' if n > 1 else ''} {'don' if n > 1 else 'doesn'}'t add up from the rows above - "
                               "check the flagged rows (or a row may be missing).", bad)]
    if good:
        return [_check("ok", (f"The {good} totals add up" if good > 1 else "The total adds up")
                       + (" (to within rounding)." if rounded else "."))]
    return []


_AMOUNT = re.compile(r"^[(\-−–]?\s*(?:[£$€¥]|[A-Z]{3})?\s*[(\-−–]?\s*(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+(?=,)|\d+)"
                     r"(?:[.,]\d+)?\s*\)?\s*(?:[A-Z]{3})?$")


_SCALE = {"thousand": 10 ** 3, "k": 10 ** 3, "million": 10 ** 6, "mn": 10 ** 6, "m": 10 ** 6, "billion": 10 ** 9,
          "bn": 10 ** 9, "b": 10 ** 9, "trillion": 10 ** 12, "tn": 10 ** 12}


def scaled_amount(s: str) -> float | None:
    """A value written as an amount with its scale in words - "$500 million", "£1.2bn", "USD 3.5 billion",
    "(12.4) million" - as a plain number (500000000).  None for anything else (a percentage, a sentence, a year)."""
    m = re.fullmatch(r"([(\-−–]?)\s*(?:[£$€¥]|[A-Z]{3})?\s*([(\-−–]?)\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*\)?\s*"
                     r"(thousand|million|billion|trillion|mn|bn|tn|[kmb])\.?\s*\)?", (s or "").strip(), re.I)
    if not m or not (re.search(r"[£$€¥]|[A-Z]{3}", s) or len(m.group(5)) > 2):
        return None                                  # "5 m" alone could be metres: a currency or a whole word
    from decimal import Decimal
    d = Decimal(m.group(3).replace(",", "") + (m.group(4) or "")) * _SCALE[m.group(5).lower()]   # exact: 1.2 bn
    n = int(d) if d == d.to_integral_value() else float(d)
    return -n if (m.group(1) or m.group(2)) else n


def amount(s: str) -> int | None:
    """A value that is plainly an amount ("$4,210", "(12.5)", "-3.00"), in cents - not a year, a phone number
    ("(408) 486-2000"), an ID, a date or a percentage: one figure, with a separator, sign, bracket or currency."""
    s = (s or "").strip()
    if "%" in s or not re.search(r"[,.()£$€¥]|^[-−–]", s) or not _AMOUNT.match(s):
        return None
    return cents(s)


def _label_key(s: str) -> str:
    return re.sub(r"^total ", "", " ".join(re.sub(r"[^0-9a-z]+", " ", s.lower()).split()))


def check_found_values(t: dict, res: dict, tables: list[dict]) -> list[dict]:
    """Everything in it: a figure given on its own and on a table's line of the same name ("Revenue" in the
    highlights and in the income statement) should be the same."""
    lines: dict[str, list[tuple[str, list[str]]]] = {}
    for tb in tables:
        cols = tb["template"]["rows"]["columns"]
        lab = next((c["key"] for c in cols if c["type"] != "number"), None)
        if lab is None:
            continue
        for r in tb["rows"]:
            figs = [r["cells"].get(c["key"], "") for c in cols if c["type"] == "number" and r["cells"].get(c["key"])]
            if figs:
                lines.setdefault(_label_key(r["cells"].get(lab, "")), []).append((tb["template"]["label"], figs))
    out, same = [], 0
    for f in t["fields"]:
        v = res["fields"].get(f["key"], {}).get("value", "")
        c = amount(v)
        found = lines.get(_label_key(f["label"])) if c is not None and f["label"] else None
        if not found:
            continue
        if any(cents(x) == c for _, figs in found for x in figs):
            same += 1
            continue
        name, figs = found[0]
        out.append(_check("warn", f"{f['label']} is {v} here, but the line \"{f['label']}\" in {name} shows {', '.join(figs)} - "
                                  "one may be misread, or they're in different units.", fields=[f["key"]]))
    if same and not out:
        out.insert(0, _check("ok", f"{same} figure{'s' if same > 1 else ''} here match{'' if same > 1 else 'es'} the same "
                                   f"line in a table."))
    return out


def checks(t: dict, res: dict, tables: list[dict] | None = None) -> list[dict]:
    """Where the numbers allow, do they add up?  [{"level": ok/warn/bad, "text", "rows": [i], "fields": [key]}]
    tables: for everything in it's values, the tables found with them ([{"template", "rows"}])."""
    if t["id"] == "found":
        return check_totals(t, res)
    if t["id"] == "found_values":
        return check_found_values(t, res, tables or [])
    out = _unreadable(t, res)
    out += {"bank_statement": check_statement, "invoice": check_invoice, "payslip": check_payslip}.get(t["id"], lambda r: [])(res)
    return out


# ------------------------------------------------------------------ did it miss anything?
# Every check above looks only at what the AI gave back.  These look at what it left out: on real-text pages, the
# figures printed on a page (or in a table the finder sees) that turn up nowhere in the results.  Rough - a common
# figure ("500") anywhere counts as found - but a dropped column or table shows.

MISSED = SPEC["missed"]
_FIG_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)"
# a figure on a page: grouped thousands or decimals ("21,634", "1.50", "$ 5,524"), or a bracketed one ("(165)");
# not a date, an ID, a phone number, a percentage, or a figure in a sentence ("$11.8 billion", "$1.7\nbillion")
_PAGE_FIGURE = re.compile(r"(?<![\w.,/-])(?:\(\s?[£$€¥]?\s?(?:" + _FIG_NUM + r"|\d+)\s?\)|[-−–]?[£$€¥]?\s?" + _FIG_NUM
                          + r"|[£$€¥]\s?\d+)(?![\w%/]|[.,]\d|[ \t]*[A-Za-z]|\s*[a-z]|[ \t]?\d{3}-\d)")


def page_figures(text: str) -> list[int]:
    """The figures printed on a page, in cents, without their signs."""
    out = []
    for m in _PAGE_FIGURE.finditer(text or ""):
        c = cents(m.group().strip())
        if c is not None:
            out.append(abs(c))
    return out


def result_figures(results: list[dict]) -> set[int]:
    """Every figure anywhere in the results (values and table cells), in cents, without their signs."""
    have = set()
    for r in results:
        for v in (r.get("fields") or {}).values():
            c = cents((v or {}).get("value", ""))
            if c is not None:
                have.add(abs(c))
        for row in r.get("rows") or []:
            for v in row["cells"].values():
                c = cents(v)
                if c is not None:
                    have.add(abs(c))
    return have


def _table_figures(tb: dict) -> list[int]:
    """The figures in a table the finder found (not its first column, which holds the line names).  None for a
    table of contents: only small whole numbers in one column (page numbers)."""
    figs, cols, small = [], set(), True
    for r in tb["rows"]:
        for j, c in enumerate(r["cells"]):
            s = (c or "").strip()
            if j == 0 or "%" in s or not _AMOUNT.match(s):
                continue
            v = cents(s)
            if v is None:
                continue
            figs.append(abs(v))
            cols.add(j)
            small = small and s.isdigit() and int(s) < 1000
    return [] if small and len(cols) <= 1 else figs


def _where(a: int, b: int) -> tuple[str, str]:
    return (f"page {a}", "that page") if a == b else (f"pages {a}-{b}", "those pages")


def missed(results: list[dict], tables: list[dict], texts: list[str], pages: list[int]) -> list[dict]:
    """Warnings for what an AI run seems to have left out of real-text pages (0-based `pages`, read as text): each
    table the finder found there (desktop; [] on the iPad) whose figures are mostly not in the results, then each
    page whose figures are mostly not.  [{"level": "warn", "text", "page", "rows": [], "fields": []}]"""
    have = result_figures(results)
    out, flagged = [], set()
    for tb in tables:
        a, b = tb["pages"]
        figs = _table_figures(tb)
        got = sum(1 for v in figs if v in have)
        if len(figs) >= MISSED["table_min"] and got < MISSED["table_share"] * len(figs):
            where, that = _where(a, b)
            out.append(dict(_check("warn", MISSED["table"].format(where=where, got=got, count=len(figs), that=that)), page=a,
                            pages=list(range(a, b + 1))))
            flagged.update(range(a, b + 1))
    for i in sorted(pages):
        if i + 1 in flagged:
            continue
        figs = page_figures(texts[i])
        got = sum(1 for v in figs if v in have)
        if len(figs) >= MISSED["page_min"] and got < MISSED["page_share"] * len(figs):
            out.append(dict(_check("warn", MISSED["page"].format(page=i + 1, got=got, count=len(figs))), page=i + 1,
                            pages=[i + 1]))
    return sorted(out, key=lambda c: c["page"])


_ANY_NUMBER = re.compile(r"\d(?:[\d,]*\d)?(?:\.\d+)?")


def printed_numbers(text: str) -> set[int]:
    """Every number printed in a text - any digits, "480" and the "4" of a "(4)" too - in cents, without signs."""
    out = set()
    for m in _ANY_NUMBER.finditer(text or ""):
        c = cents(m.group())
        if c is not None:
            out.add(abs(c))
    return out


def not_printed(results: list[dict], texts: list[str], pages: list[int]) -> list[dict]:
    """Figures in the results that are printed nowhere on their page or the pages either side (0-based `pages`: the
    pages read as text) - a misread, or a footnote mark run into a figure ("480 (4)" read as 4804).  Looks at the
    columns of figures of every table and the single values that are plainly amounts.
    [{"level": "warn", "text", "page", "rows": [], "fields": []}], the first not_printed_max, then a count."""
    text_pages = set(pages)
    near: dict[int, set[int]] = {}

    def on_page(c: int, page: int) -> bool:
        if page not in near:
            near[page] = set().union(*(printed_numbers(texts[i]) for i in (page - 2, page - 1, page)
                                       if 0 <= i < len(texts)))
        return abs(c) in near[page]

    found = []
    for r in results:
        t = r["template"]
        for f in t.get("fields") or []:
            v = (r.get("fields") or {}).get(f["key"]) or {}
            c = cents(v.get("value", "")) if f.get("type") in ("money", "number") else amount(v.get("value", ""))
            pg = v.get("page") or 0
            if c is not None and pg - 1 in text_pages and not on_page(c, pg):
                found.append((pg, v["value"], f.get("label") or f["key"]))
        cols = (t.get("rows") or {}).get("columns") or []
        figs = [c for c in cols if c.get("type") in ("money", "number")]
        lab = next((c["key"] for c in cols if c.get("type") not in ("money", "number")), None)
        for row in r.get("rows") or []:
            pg = row.get("page") or 0
            if pg - 1 not in text_pages:
                continue
            for c in figs:
                v = row["cells"].get(c["key"], "")
                n = cents(v) if v and _AMOUNT.match(v.strip()) else None
                if n is not None and not on_page(n, pg):
                    line = row["cells"].get(lab, "") if lab else ""
                    where = f'{t.get("label") or "a table"}, "{line}"' if line else (t.get("label") or "a table")
                    found.append((pg, v, where))
    out = [dict(_check("warn", MISSED["not_printed"].format(value=v, where=w, page=pg)), page=pg)
           for pg, v, w in found[:MISSED["not_printed_max"]]]
    if len(found) > MISSED["not_printed_max"]:
        out.append(dict(_check("warn", MISSED["not_printed_more"].format(count=len(found) - MISSED["not_printed_max"])),
                        page=0))
    return out


# ------------------------------------------------------------------ the second look (everything in it)

def second_look_pages(flags: list[dict], text_pages: list[int]) -> list[int]:
    """The pages (0-based) to read once more on their own: those the missed check flags, among the pages read as
    text, at most second_look of them (the first ones)."""
    want = sorted({p - 1 for c in flags for p in c.get("pages") or ([c["page"]] if c.get("page") else [])} & set(text_pages))
    return want[:MISSED["second_look"]]


def _page_rows(part: dict, keep) -> dict:
    """An everything-in-it part with only its values and rows whose page passes keep(page)."""
    tables = []
    for tb in part["tables"]:
        rows = [r for r in tb["rows"] if keep(r["page"])]
        if rows:
            tables.append(dict(tb, rows=rows))
    return dict(part, values=[v for v in part["values"] if keep(v["page"])], tables=tables)


def covered(part: dict, text: str, page: int) -> int:
    """How many of a page's printed figures (page_figures) a part's values and rows from that page (1-based) hold."""
    have = set()
    for v in part["values"]:
        if v["page"] == page:
            c = cents(v["value"])
            if c is not None:
                have.add(abs(c))
    for tb in part["tables"]:
        for r in tb["rows"]:
            if r["page"] == page:
                for x in r["cells"]:
                    c = cents(x)
                    if c is not None:
                        have.add(abs(c))
    return sum(1 for f in page_figures(text) if f in have)


def splice(parts: list[tuple[list[int], dict]], again: dict[int, dict], texts: list[str]) -> list[dict]:
    """The parts in page order with the second look's readings put in: a page's new reading (again: 0-based page ->
    part) replaces what the first reading had from that page when it holds more of the page's figures - the part it
    was in is cut there, so the order of the rows stays the page order."""
    out = []
    for pages, part in parts:
        rest = part
        for i in sorted(p for p in again if p in pages):
            new, page = again[i], i + 1
            if covered(new, texts[i], page) <= covered(part, texts[i], page):
                continue                             # the first reading was as good: keep it
            out.append(_page_rows(rest, lambda pg: 0 < pg < page or (pg == 0 and rest is part)))
            out.append(new)
            rest = dict(_page_rows(rest, lambda pg: pg > page), document_type="", doubts=[])
        out.append(rest)
    return [p for p in out if p["values"] or p["tables"] or p.get("doubts") or p.get("document_type")]


# ------------------------------------------------------------------ which pages go together

def plan_parts(texts: list[str], pics: set[int], t: dict) -> list[list[int]]:
    """Pages in order, a few at a time: each part's answer has to fit in one reply (rows are long), and each request
    stays small.  Empty pages are left out."""
    per = ALL["pages_per_part"] if t["id"] == "all" else SPEC["pages_per_part"] if t["rows"] else 12
    budget = SPEC["chars_per_part"]
    parts, cur, size = [], [], 0
    for i, text in enumerate(texts):
        if i not in pics and not text.strip():
            continue
        n = 3000 if i in pics else len(text)
        if cur and (len(cur) >= per or size + n > budget):
            parts.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        parts.append(cur)
    return parts


def estimate(parts: list[list[int]], texts: list[str], pics: set[int], see: bool, t: dict, prices: dict | None,
             rate: float) -> dict:
    """Roughly what a job sends and costs: characters, pictures, requests, and pounds when the relay gives prices."""
    chars = sum(len(texts[i]) for p in parts for i in p if i not in pics)
    n_pics = sum(1 for p in parts for i in p if i in pics)
    tokens_in = chars / 3.5 + len(parts) * 900 + (n_pics * 1600 if see else n_pics * 700)
    tokens_out = sum(min(SPEC["answer_tokens"], 250 + (1200 * len(p) if t["rows"] or t["id"] == "all" else 0)) for p in parts)
    out = {"parts": len(parts), "chars": chars, "pictures": n_pics if see else 0, "read_here": 0 if see else n_pics,
           "pages": sum(len(p) for p in parts)}
    if prices and prices.get("in") is not None:
        usd = (tokens_in * float(prices.get("in") or 0) + tokens_out * float(prices.get("out") or 0)) / 1_000_000
        out["gbp"] = round(usd * rate, 2)
    return out


def at_once(provider: str, base: str) -> int:
    """Parts sent at the same time (one to an AI on this computer)."""
    return ai.at_once(provider, base, SPEC.get("at_once", 4))


def waves(parts: list[list[int]], every: bool) -> list[list[list[int]]]:
    """The order the parts go in: a template's all together; everything in it's first part alone, then the rest
    together (each told the tables the first part found, so one carried over keeps its name and columns)."""
    if every and len(parts) > 1:
        return [parts[:1], parts[1:]]
    return [parts] if parts else []


def stopped_note(pages: list[int], problem: str) -> str:
    """The doubt shown when a part failed after others were read (0-based pages not read)."""
    s = "s" if len(pages) > 1 else ""
    return f"Stopped before reading page{s} {ai.page_ranges(pages)} - {problem} What was read is below."


# ------------------------------------------------------------------ tables as they are (no AI)

def _cell(c) -> str:
    return " ".join(str(c or "").split())


def _rows_from_words(table, words) -> list[list[str]]:
    """The table's cells filled from the page's own words (the finder's own cell text can lose spaces and split
    numbers - bridge_ai._table_markdown does the same)."""
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
            cells.append(" ".join(w[4] for w in ws))
        rows.append(cells)
    return rows


def find_tables(doc, pages: list[int] | None = None) -> list[dict]:
    """Every table PyMuPDF finds on the real-text pages; a table carried on from the page before (same columns, same
    headings or none) is joined to it.  -> [{"label", "pages": [first, last], "columns": [..], "rows": [{"cells": [..],
    "page"}]}] (pages 1-based)."""
    out = []
    for i in pages if pages is not None else range(len(doc)):
        pg = doc[i]
        if len(pg.get_text().strip()) < 25:
            continue
        try:
            found = [t for t in pg.find_tables().tables if t.row_count >= 2 and t.col_count >= 2]
        except Exception:
            log.exception("table finder failed on page %d", i + 1)
            continue
        words = pg.get_text("words") if found else []
        for tb in found:
            data = _rows_from_words(tb, words)
            data = [r for r in data if any(r)]
            if len(data) < 2:
                continue
            external = bool(tb.header and getattr(tb.header, "external", False))
            if external:
                names = [_cell(n) for n in tb.header.names]
            elif any(amount(c) is not None for c in data[0]):
                names = [""] * len(data[0])              # the first row holds figures: data, no headings
            else:
                names, data = data[0], data[1:]          # the first row is the headings
            prev = out[-1] if out else None
            if (prev and prev["pages"][1] == i and len(prev["columns"]) == len(names)
                    and (names == prev["columns"] or not any(names))):
                prev["rows"] += [{"cells": r, "page": i + 1} for r in data]
                prev["pages"][1] = i + 1
                continue
            out.append({"columns": [n or f"Column {k + 1}" for k, n in enumerate(names)], "pages": [i + 1, i + 1],
                        "rows": [{"cells": r, "page": i + 1} for r in data]})
    for k, tb in enumerate(out):
        a, b = tb["pages"]
        tb["label"] = f"Table {k + 1} · page{'s' if b > a else ''} {a}{f'-{b}' if b > a else ''}"
    return [tb for tb in out if tb["rows"]]


# ------------------------------------------------------------------ saving

def _number(v: str, kind: str):
    if kind in ("money", "number"):
        c = cents(v)
        return None if c is None else (c / 100 if kind == "money" or c % 100 else c // 100)
    if kind == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or ""):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def to_csv(sheet: dict) -> str:
    """The table (with a Page column) - or, with no rows, the details as Field, Value, Page."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    if sheet.get("rows"):
        w.writerow([c["label"] for c in sheet["columns"]] + ["Page"])
        for r in sheet["rows"]:
            w.writerow(list(r["cells"]) + [r.get("page") or ""])
    else:
        fields = sheet.get("fields") or []
        if _sectioned(fields):
            w.writerow(["Section", "Field", "Value", "Unit", "Page"])
            for f in fields:
                w.writerow([f.get("section") or "", f["label"], f["value"], f.get("unit") or "", f.get("page") or ""])
        else:
            w.writerow(["Field", "Value", "Page"])
            for f in fields:
                w.writerow([f["label"], f["value"], f.get("page") or ""])
    return buf.getvalue()


def _sectioned(fields: list[dict]) -> bool:
    """Values found with no template carry the heading they're under and a unit: the details show both."""
    return any(f.get("section") or f.get("unit") for f in fields)


def _json_value(v: str, kind: str):
    if not v:
        return None
    if kind in ("money", "number"):
        if nil(v):                                   # "—" in a column of figures: nothing (null), not the text
            return None
        n = _number(v, kind)
        return v if n is None else n
    return v


def to_json(sheets: list[dict], document: str, pages: int, when: str, text: list[dict] | None = None) -> dict:
    """Everything shown, as the person left it, for a program to read: the values (by key: label, section, value,
    unit, page - amounts as numbers, and an "amount" beside a value printed as text that is plainly one, "$500
    million" included), every table (columns with keys; rows by column key, figures as numbers, a dash as null; its
    footnotes), the checks and doubts - and, for everything in it, the document's own words page by page (text:
    [{"page", "text"}], straight from the PDF, so the commentary the tables leave out is there too)."""
    out = {"document": document, "pages": pages, "extracted": when, "made_with": "TidyPaper"}
    kind = (sheets[0].get("kind") if sheets else "") or ""
    out["kind"] = {"found": "everything", "found_values": "everything", "table": "tables"}.get(kind, kind)
    dtype = next((s.get("document_type") for s in sheets if s.get("document_type")), "")
    if dtype:
        out["document_type"] = dtype
    values, tables, checks_, doubts = {}, [], [], []
    taken: set[str] = set()
    for s in sheets:
        checks_ += [{"level": c.get("level", "warn"), "text": c["text"], "about": "", "page": c.get("page") or None}
                    for c in s.get("missed") or []]               # what the run seems to have left out
        for f in s.get("fields") or []:
            k = slug(f.get("key") or f["label"], taken, len(taken) + 1)
            v = {"label": f["label"]}
            if f.get("section"):
                v["section"] = f["section"]
            v["value"] = _json_value(f["value"], f.get("type", "text"))
            if f.get("type", "text") == "text" and amount(f["value"]) is not None:
                v["amount"] = amount(f["value"]) / 100
            elif f.get("type", "text") == "text" and scaled_amount(f["value"]) is not None:
                v["amount"] = scaled_amount(f["value"])          # "$500 million" -> 500000000
            if f.get("unit"):
                v["unit"] = f["unit"]
            v["page"] = f.get("page") or None
            values[k] = v
        if s.get("columns"):
            ctaken: set[str] = set()
            cols = [{"key": slug(c.get("key") or c["label"] or f"column {j + 1}", ctaken, j + 1), "label": c["label"]}
                    for j, c in enumerate(s["columns"])]
            tb = {"name": s.get("name") or "Table"}
            if s.get("note"):
                tb["note"] = s["note"]
            tb["columns"] = cols
            tb["rows"] = [{**{c["key"]: _json_value(r["cells"][j] if j < len(r["cells"]) else "", s["columns"][j].get("type", "text"))
                              for j, c in enumerate(cols)}, "page": r.get("page") or None} for r in s.get("rows") or []]
            if s.get("footnotes"):
                tb["footnotes"] = [{"mark": f.get("mark", ""), "text": f["text"]} for f in s["footnotes"]]
            tables.append(tb)
        checks_ += [{"level": c.get("level", "ok"), "text": c["text"], "about": s.get("name") or ""} for c in s.get("checks") or []]
        doubts += [d for d in s.get("doubts") or [] if d not in doubts]
    out.update(values=values, tables=tables, checks=checks_, doubts=doubts)
    if text:
        out["text"] = [{"page": t["page"], "text": t["text"]} for t in text]
    return out


def write_xlsx(path: Path, sheets: list[dict], text: list[dict] | None = None):
    """One sheet per table (amounts as numbers, a dash as an empty cell, dates as dates; its footnotes under it), a
    Details sheet with the single values and checks, and a Text sheet with the document's words page by page (text)."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used = set()

    def title(s):
        s = re.sub(r"[\[\]:*?/\\]", " ", s)[:31].strip() or "Sheet"
        base, n = s, 2
        while s.lower() in used:
            s = f"{base[:28]} {n}"; n += 1
        used.add(s.lower())
        return s

    details = []
    for sh in sheets:
        if sh.get("rows"):
            ws = wb.create_sheet(title(sh.get("name") or "Data"))
            cols = sh["columns"]
            ws.append([c["label"] for c in cols] + ["Page"])
            for c in ws[1]:
                c.font = Font(bold=True)
            for r in sh["rows"]:
                vals = []
                for c, v in zip(cols, r["cells"]):
                    n = _number(v, c.get("type", "text"))
                    vals.append(n if n is not None else None if c.get("type") in ("money", "number") and nil(v) else v)
                ws.append(vals + [r.get("page") or None])
            for k, c in enumerate(cols, 1):
                if c.get("type") == "money":
                    for cell in ws.iter_rows(min_row=2, min_col=k, max_col=k):
                        cell[0].number_format = "#,##0.00"
                elif c.get("type") == "date":
                    for cell in ws.iter_rows(min_row=2, min_col=k, max_col=k):
                        if isinstance(cell[0].value, date):
                            cell[0].number_format = "yyyy-mm-dd"
                width = max([len(str(c["label"]))] + [len(str(r["cells"][k - 1])) for r in sh["rows"][:500]
                                                      if k - 1 < len(r["cells"])])
                ws.column_dimensions[get_column_letter(k)].width = min(60, max(9, width + 2))
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            if sh.get("footnotes"):                  # under the table, after a gap (outside the filter)
                ws.append([])
                for f in sh["footnotes"]:
                    ws.append([f"{f.get('mark', '')} {f['text']}".strip()])
        details += [(f["label"], f["value"], f.get("page") or None, f.get("type", "text"), f.get("section") or "",
                     f.get("unit") or "") for f in sh.get("fields") or []]
        details += [("Check", c["text"], c.get("page") or None, "text", "", "") for c in sh.get("missed") or []]
        details += [("Check", c["text"], None, "text", "", "") for c in sh.get("checks") or []]
    if details or not wb.sheetnames:
        ws = wb.create_sheet(title("Details"), 0 if details else None)
        wide = any(d[4] or d[5] for d in details)           # values found with no template: their section and unit
        ws.append(["Section", "Field", "Value", "Unit", "Page"] if wide else ["Field", "Value", "Page"])
        for c in ws[1]:
            c.font = Font(bold=True)
        for label, value, page, kind, section, unit in details:
            n = _number(value, kind)
            ws.append([section, label, n if n is not None else value, unit, page] if wide
                      else [label, n if n is not None else value, page])
            vcol = 3 if wide else 2
            if kind == "money" and n is not None:
                ws.cell(ws.max_row, vcol).number_format = "#,##0.00"
            elif kind == "date" and n is not None:
                ws.cell(ws.max_row, vcol).number_format = "yyyy-mm-dd"
        widths = {"A": 22, "B": 30, "C": 50, "D": 16, "E": 8} if wide else {"A": 28, "B": 60}
        for col, wd in widths.items():
            ws.column_dimensions[col].width = wd
    if text:
        from openpyxl.styles import Alignment
        ws = wb.create_sheet(title("Text"))
        ws.append(["Page", "Text"])
        for c in ws[1]:
            c.font = Font(bold=True)
        for t in text:
            ws.append([t["page"], t["text"][:32000]])           # a cell holds at most 32,767 characters
            ws.cell(ws.max_row, 2).alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(ws.max_row, 1).alignment = Alignment(vertical="top")
        ws.column_dimensions["A"].width = 6
        ws.column_dimensions["B"].width = 120
        ws.freeze_panes = "A2"
    wb.save(path)


# ------------------------------------------------------------------ bridge

class ExtractMixin:
    """Mixed into app.Bridge (uses AiMixin's settings, page texts, pictures and provider calls)."""

    def _extract_jobs(self) -> dict:
        if not hasattr(self, "_xjobs"):
            self._xjobs = {}
        return self._xjobs

    def _extract_target(self, has_pics: bool):
        """(provider, preset, base, model, key, relay config) for Pull out data: through a relay its model for this
        (if it has one), or for scans, else the answer model."""
        cfg = self._ai_cfg()
        p, pre, base, model, key = self._ai_target(cfg)
        conf = self._relay_cfg(base, key) if pre["kind"] == "relay" else None
        if conf:
            x = conf.get("extract_model")
            if x and (not has_pics or ai.can_see_pictures(pre, p, x) or not conf.get("scan_model")):
                model = x
            elif has_pics and conf.get("scan_model") and conf.get("pictures", True):
                model = conf["scan_model"]
        return cfg, p, pre, base, model, key, conf

    @safe
    def extract_options(self):
        """What Pull out data can offer for the open document: the templates, how many tables the table finder sees
        (real-text pages, no AI), and - when AI is set up - roughly what each template would send."""
        with self._lock:
            m = self._need_doc()
            doc = m.doc
            n = len(m)
            text_pages = [i for i in range(n) if ai._looks_tabular(m.page(i).get_text())]
        tables = find_tables(doc, text_pages[:60]) if text_pages else []
        self._xtables = (id(doc), len(m.undo_stack), tables, set(text_pages[:60]))
        out = {"templates": [{"id": t["id"], "label": t["label"], "about": t["about"]} for t in SPEC["templates"]],
               "all": {"id": "all", "label": ALL["label"], "about": ALL["about"]},
               "tables": len(tables), "table_rows": sum(len(t["rows"]) for t in tables), "page_count": n,
               "ai": False, "provider": "", "estimates": {}, "month": None, "consent": False}
        cfg = self._ai_cfg()
        if not cfg["enabled"]:
            out["ai_note"] = "Turn on the AI assistant in Settings to pull out statements, invoices and payslips."
            return out
        try:
            texts, _ = self._texts()
            pics = self._picture_pages(texts)
            cfg, p, pre, base, model, key, conf = self._extract_target(bool(pics))
        except UserError as e:
            out["ai_note"] = str(e)
            return out
        see = bool(pics) and ai.can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        prices = ((conf or {}).get("prices") or {}).get(model)
        rate = float((conf or {}).get("usd_to_gbp") or 0.79)
        for t in [template(x) for x in ["all", *TEMPLATES]] + [template("own", {"labels": ["a", "b", "c"]}),
                                                     template("own", {"labels": ["a", "b", "c"], "list": True})]:
            key_ = t["id"] if t["id"] != "own" else ("own_list" if t["rows"] else "own")
            out["estimates"][key_] = estimate(plan_parts(texts, pics, t), texts, pics, see, t, prices, rate)
        out.update(ai=True, provider=ai.short_name(p, base), model=model, consent=bool(cfg["consent"].get(p)),
                   local=p == "ollama", relay=pre["kind"] == "relay", month=(conf or {}).get("month"))
        return out

    def _finder_tables(self, pages: list[int]) -> list[dict]:
        """The table finder's tables on these pages (0-based) - those it found when the dialog opened, plus any on
        pages it didn't look at then."""
        with self._lock:
            m = self._need_doc()
            doc, undo = m.doc, len(m.undo_stack)
            tab = [i for i in pages if ai._looks_tabular(m.page(i).get_text())]
        cached = getattr(self, "_xtables", None)
        if cached and len(cached) > 3 and cached[0] == id(doc) and cached[1] == undo:
            tables, rest = list(cached[2]), [i for i in tab if i not in cached[3]]
        else:
            tables, rest = [], tab
        if rest:
            tables += find_tables(doc, rest)
        want = {i + 1 for i in tab}
        return [tb for tb in tables if any(q in want for q in range(tb["pages"][0], tb["pages"][1] + 1))]

    @safe
    def extract_tables(self):
        """Tables as they are (no AI)."""
        with self._lock:
            m = self._need_doc()
            cached = getattr(self, "_xtables", None)
            if cached and cached[0] == id(m.doc) and cached[1] == len(m.undo_stack):
                tables = cached[2]
            else:
                tables = None
            doc = m.doc
        if tables is None:
            tables = find_tables(doc)
        if not tables:
            raise UserError("No tables were found on this document's pages. Try a template instead.")
        out = []
        for k, tb in enumerate(tables):
            cols = [{"key": f"c{j + 1}", "label": c, "type": "text"} for j, c in enumerate(tb["columns"])]
            t = {"id": "table", "label": tb["label"], "fields": [], "rows": {"label": tb["label"], "one": "row", "columns": cols}}
            rows = [{"cells": {f"c{j + 1}": (r["cells"][j] if j < len(r["cells"]) else "") for j in range(len(cols))},
                     "page": r["page"]} for r in tb["rows"]]
            out.append({"template": t, "fields": {}, "rows": rows, "doubts": [], "checks": [], "provider": ""})
        return {"results": out, "status": f"{len(out)} table{'s' if len(out) > 1 else ''} found - read on this "
                                          f"{'Mac' if ai.platform.system() == 'Darwin' else 'PC'}, nothing sent."}

    @safe
    def extract_start(self, tid: str, own: dict | None = None):
        """Start pulling out data with a template (polled with extract_status)."""
        cfg = self._ai_cfg()
        if not cfg["enabled"]:
            raise UserError("The AI assistant is switched off. Turn it on in Settings.")
        t = template(tid, own)
        texts, name = self._texts()
        pics = self._picture_pages(texts)
        if not any(x.strip() for x in texts) and not pics:
            raise UserError("This document has no text or pictures of text to read.")
        cfg, p, pre, base, model, key, conf = self._extract_target(bool(pics))
        if not cfg["consent"].get(p):
            cfg["consent"][p] = True
            self._ai_save(cfg)
        see = bool(pics) and ai.can_see_pictures(pre, p, model) and self._relay_pictures_ok(pre, base, key)
        parts = plan_parts(texts, pics, t)
        job_id = uuid.uuid4().hex[:12]
        job = {"done": False, "cancel": False, "progress": "Starting…", "result": None, "problem": None,
               "part": 0, "parts": len(parts), "started": time.time()}
        self._extract_jobs()[job_id] = job
        headers = {RELAY_JOB_HEADER: "extract"} if pre["kind"] == "relay" else None
        every = t["id"] == "all"
        system = ALL["system"] if every else SPEC["system"]
        who = ai.short_name(p, base)
        workers = at_once(p, base)
        lock, model_lock = threading.Lock(), threading.Lock()
        state = {"model": model, "shape_in_prompt": False, "pause": 0.0, "waits": 0, "error": None, "done": 0,
                 "left": len(parts), "again": False}
        got: dict[tuple, dict] = {}                # each part read: its pages -> its answer, tidied
        again: dict[int, dict] = {}                # the second look: a page read once more on its own -> its answer
        too_long: list[int] = []                   # pages with more than fits in one answer
        queue: list[list[int]] = []
        flying: list[list[int]] = []
        cancelled = lambda: job["cancel"]          # noqa: E731
        texts = list(texts)

        def in_order() -> list[dict]:
            return [got[k] for k in sorted(got)]

        def ask(pages: list[int], known: list[dict] | None = None) -> dict:
            text_pages = [i for i in pages if i not in pics or not see]
            pic_pages = [i for i in pages if i in pics and see]
            for i in pages:
                if i in pics and not see and not texts[i].strip():
                    job["progress"] = f"Reading page {i + 1} on this computer…"
                    texts[i] = self._ocr_page(i)
            doc = ai.document_block(texts, text_pages, name) if text_pages else ""
            images = [(i, self._page_jpeg(i)) for i in pic_pages] or None
            while True:
                buf = []
                shape, m = state["shape_in_prompt"], state["model"]
                msg = [{"role": "user", "content": prompt(t, pages, len(texts), shape, known, state["again"])}]
                fmt = None if shape else schema(t)
                kind, url = ai.route(pre, base, m)
                try:
                    if kind == "anthropic":
                        r = ai.stream_claude(key, m, url, system, doc, msg, buf.append, cancelled,
                                             relay=pre["kind"] == "relay", images=images,
                                             output_format={"type": "json_schema", "schema": fmt} if fmt else None,
                                             max_tokens=SPEC["answer_tokens"], effort=SPEC.get("effort", "medium"),
                                             headers=headers, timeout=300)
                    else:
                        r = ai.stream_openai(url, key, m, system + ("\n\n" + doc if doc else ""), msg,
                                             buf.append, cancelled, p, timeout=300, images=images,
                                             response_format={"type": "json_schema", "json_schema": {
                                                 "name": "pulled_out", "strict": True, "schema": fmt}} if fmt else None,
                                             max_tokens=SPEC["answer_tokens"], headers=headers)
                except ai.FormatRefused:
                    if shape:
                        raise
                    state["shape_in_prompt"] = True        # this service can't be given the shape: say it in words
                    continue
                except ai.RelayModelChanged:
                    with model_lock:                       # the relay changed its model: ask it again (once per change)
                        if state["model"] == m:
                            self._relay_cfg(base, key, force=True)
                            m2 = self._extract_target(bool(pics))[4]
                            if m2 == m:
                                raise
                            state["model"] = m2
                    continue
                text = "".join(buf)
                if r.get("stop") in ("max_tokens", "length"):
                    raise _TooLong()
                try:
                    ans = parse_answer(text)
                    return normalise_all(ans, pages) if every else normalise(t, ans, pages)
                except ValueError:
                    if not shape and not text.strip():
                        raise UserError(ai.empty_answer_message(r.get("stop"), who, pre["kind"] == "relay", False))
                    raise UserError(f"{who} didn't answer in the expected form. Try again.")

        def say():
            """The progress line (call with the lock held)."""
            total = state["done"] + state["left"]
            job["part"], job["parts"] = min(total, state["done"] + 1), total
            if not flying:
                return
            pages = sorted(i for q in flying for i in q)
            job["progress"] = (f"{'Looking again at' if state['again'] else 'Reading'} page{'s' if len(pages) > 1 else ''} "
                               f"{ai.page_ranges(pages)}"
                               + (f" ({state['done']} of {total} parts read)" if total > 1 else "") + "…")

        def worker(known: list[dict] | None):
            """Takes the next part until there are none (several of these run at once).  Busy: every worker waits for
            the next minute once; too long: the part goes back as two halves; any other problem stops new parts (those
            under way finish)."""
            while True:
                while time.time() < state["pause"] and not job["cancel"] and not state["error"]:
                    time.sleep(0.25)
                with lock:
                    if job["cancel"] or state["error"] or not queue:
                        return
                    pages = queue.pop(0)
                    flying.append(pages)
                    say()
                try:
                    k = known if known is not None else (known_tables(in_order()) if every else None)
                    res = ask(pages, k)
                except ai.Busy as e:
                    with lock:
                        flying.remove(pages)
                        queue.insert(next((n for n, q in enumerate(queue) if q[0] > pages[0]), len(queue)), pages)
                        if time.time() >= state["pause"]:          # the first part to hear it: wait once for all
                            state["waits"] += 1
                            if state["waits"] > BUSY_TRIES:
                                state["error"] = state["error"] or e
                                return
                            state["pause"] = time.time() + busy_wait()
                        job["progress"] = (f"Waiting a moment - {who} takes only so many requests a minute "
                                           f"({state['done']} of {state['done'] + state['left']} parts read)…")
                    continue
                except _TooLong:
                    with lock:
                        flying.remove(pages)
                        if len(pages) > 1:                 # too many rows for one answer: half as many pages at a time
                            h = len(pages) // 2
                            queue[:0] = [pages[:h], pages[h:]]
                            state["left"] += 1
                        else:
                            if not state["again"]:         # a second look that runs out of room: the first stays
                                too_long.append(pages[0])
                            state["done"] += 1
                            state["left"] -= 1
                    continue
                except BaseException as e:                 # stopped, refused, failed: no new parts
                    with lock:
                        flying.remove(pages)
                        state["error"] = state["error"] or e
                    return
                with lock:
                    flying.remove(pages)
                    if state["again"]:
                        again[pages[0]] = res
                    else:
                        got[tuple(pages)] = res
                    state["done"] += 1
                    state["left"] -= 1
                    state["waits"] = 0
                    say()

        def second_look() -> list[dict]:
            """Everything in it: the text pages the missed check flags, each read once more on its own (all at once,
            told the tables found); a reading with more of its page's figures replaces the first.  Its problems
            (busy, refused...) just leave the first reading."""
            results = in_order()
            text_read = sorted(i for k in got for i in k if i not in pics)
            if not every or not text_read or job["cancel"] or state["error"]:
                return results
            out = expand_all(merge_all(results))
            look = second_look_pages(missed(out, self._finder_tables(text_read), texts, text_read), text_read)
            if not look:
                return results
            job["progress"] = f"Looking again at page{'s' if len(look) > 1 else ''} {ai.page_ranges(look)}…"
            state.update(again=True, done=0, left=len(look), waits=0)
            queue.extend([i] for i in look)
            known = known_tables(results)
            threads = [self._thread(worker, known) for _ in range(min(workers, len(look)))]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            if state["error"] is not None:
                log.info("extract: second look stopped - %s", state["error"])
                state["error"] = None
            queue.clear()
            if job["cancel"]:
                raise ai._Cancelled()
            better = splice([(list(k), got[k]) for k in sorted(got)], again, texts)
            log.info("extract: second look at pages %s, %d read again", ai.page_ranges(look), len(again))
            return better

        def run():
            try:
                for n, wave in enumerate(waves(parts, every)):
                    if job["cancel"] or state["error"]:
                        break
                    queue.extend(list(x) for x in wave)
                    # everything in it: the first part alone (each half told what the one before found, if it's split),
                    # then the rest at once, all told the tables the first part found
                    known = known_tables(in_order()) if every and n > 0 else None
                    threads = [self._thread(worker, known)
                               for _ in range(min(1 if every and n == 0 and len(parts) > 1 else workers, len(queue)))]
                    for th in threads:
                        th.start()
                    for th in threads:
                        th.join()
                if job["cancel"]:
                    raise ai._Cancelled()
                read = sorted(i for k in got for i in k)
                results = second_look()
                problems = [f"Page {i + 1} had more than fits in one answer - some rows from it may be missing."
                            for i in sorted(too_long)]
                e = state["error"]
                if e is not None:
                    if not results or isinstance(e, ai.FormatRefused) or not isinstance(e, UserError):
                        raise e
                    unread = sorted({i for q in parts for i in q} - set(read) - set(too_long))
                    problems.append(stopped_note(unread, str(e)))
                if every:
                    found = merge_all(results)
                    found["doubts"] = problems + found["doubts"]
                    out = expand_all(found)
                    if not out:
                        raise UserError(f"{who} found no tables or values to pull out of this document.")
                    tables = [r for r in out if r["template"]["id"] == "found"]
                    for r in out:
                        r.update(checks=checks(r["template"], r, tables), provider=who, model=state["model"],
                                 parts=len(results))
                else:
                    res = merge(t, results)
                    res["doubts"] = problems + res["doubts"]
                    res.update(template=t, checks=checks(t, res), provider=who, model=state["model"],
                               parts=len(results))
                    out = [res]
                text_read = [i for i in read if i not in pics]
                if (every or t["id"] == "bank_statement") and text_read and not job["cancel"]:
                    job["progress"] = "Checking nothing was left out…"         # did it miss a table or a page?
                    out[0]["missed"] = missed(out, self._finder_tables(text_read), texts, text_read) + \
                        not_printed(out, texts, text_read)              # or put in a figure the page doesn't have?
                job["results"] = out
                job["result"] = out[0]
            except ai._Cancelled:
                job["problem"] = "Stopped."
                job["cancelled"] = True
            except UserError as e:
                job["problem"] = str(e)
            except Exception:
                log.exception("pull out data failed")
                job["problem"] = "Pulling out the data failed. Details were saved to the log file."
            finally:
                job["done"] = True
                log.info("extract %s %s template=%s parts=%d at_once=%d rows=%s missed=%d problem=%s %.1fs", p,
                         state["model"], t["id"], len(parts), workers,
                         sum(len(r.get("rows") or []) for r in job.get("results") or []),
                         len(((job.get("results") or [{}])[0]).get("missed") or []), bool(job["problem"]),
                         time.time() - job["started"])

        self._thread(run).start()                    # stays with this tab's document
        return {"job": job_id, "parts": len(parts), "provider": ai.short_name(p, base), "model": model}

    @safe
    def extract_status(self, job_id: str):
        job = self._extract_jobs().get(job_id)
        if not job:
            raise UserError("That job is no longer available.")
        return {k: job.get(k) for k in ("done", "progress", "result", "results", "problem", "part", "parts", "cancelled")}

    @safe
    def extract_cancel(self, job_id: str):
        job = self._extract_jobs().get(job_id)
        if job:
            job["cancel"] = True
        return {"ok": True}

    @safe
    def extract_check(self, t: dict, fields: dict, rows: list, tables: list | None = None):
        """The checks again, after the person changed values (tables: everything in it's tables, for its values)."""
        return {"checks": checks(t, {"fields": fields, "rows": rows}, tables)}

    def _extract_page_text(self) -> list[dict]:
        """The open document's words page by page, [{"page", "text"}] - pages with no text (scans) and pages in an old
        non-Unicode font (junk letters) left out."""
        out = []
        with self._lock:
            m = self._model
            for i in range(len(m) if m else 0):
                raw = m.page(i).get_text()
                if raw.strip() and not ai.garbled_text(raw):
                    out.append({"page": i + 1, "text": page_text(raw)})
        return out

    @safe
    def extract_save(self, fmt: str, sheets: list, path: str | None = None):
        """Save as CSV (the table shown), Excel (every table, plus the details and checks) or JSON (all of it)."""
        if fmt not in ("csv", "xlsx", "json") or not sheets:
            raise UserError("Nothing to save.")
        src = self._source or (self._model.path if self._model else None) or (Path.home() / "Documents" / "Document.pdf")
        src = Path(src)
        stem = src.stem[:-len("_edited")] if src.stem.endswith("_edited") else src.stem
        from bridge_util import unique
        start = unique(src.with_name(f"{stem} - data.{fmt}"))
        if not path:
            if not getattr(self, "_window", None):
                return {"error": "Saving needs the app window."}
            import webview
            res = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=str(start.parent), save_filename=start.name,
                                                  file_types=({"xlsx": "Excel workbooks (*.xlsx)", "csv": "CSV files (*.csv)",
                                                               "json": "JSON files (*.json)"}[fmt],))
            if not res:
                return {"cancelled": True}
            path = res if isinstance(res, str) else res[0]
        p = Path(path)
        if p.suffix.lower() != f".{fmt}":
            p = p.with_suffix(f".{fmt}")
        everything = any(sh.get("kind") in ("found", "found_values") for sh in sheets)
        text = self._extract_page_text() if everything and fmt != "csv" else None
        if fmt == "csv":
            p.write_text(to_csv(sheets[0]), encoding="utf-8-sig")     # the BOM makes Excel read £ and accents right
        elif fmt == "json":
            from datetime import datetime, timezone
            pages = len(self._model) if self._model else 0
            when = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            p.write_text(json.dumps(to_json(sheets, src.name, pages, when, text), ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
        else:
            write_xlsx(p, sheets, text)
        return {"saved": str(p), "status": f"Saved {p.name}."}


def page_text(raw: str) -> str:
    """A page's words for the saved data: as the PDF has them, line by line, without trailing spaces or blank runs."""
    lines = [ln.rstrip() for ln in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class _TooLong(Exception):
    """The answer ran out of room (too many rows for one reply)."""
