"""Remove personal details from many PDFs at once (Home › More tools › PDF) - the user's decisions of 21 Nov 2026,
in NEXT_SESSION.md.

The person ticks kinds, not single finds.  Every file is looked through by the rules of Find personal details
(bridge_fill.find_personal - nothing is sent anywhere); what is found of the ticked kinds is taken out of a copy for
good and blacked over, as the check before emailing does (bridge_share.black_out).  The originals are never changed:
the copies go into a new folder beside them, with a report.  The words say what was done ("Removed 14 email
addresses and 6 phone numbers from 12 files") - never that a file is now free of personal details, and the report
gives counts by kind, never the details themselves.  Run by taskrunner (the task "redact_many")."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from pathlib import Path

import pymupdf as fitz

import bridge_fill
import scanwords
from bridge_share import black_out
from bridge_util import unique

log = logging.getLogger("toolkit")

# the kinds the person ticks: option key, words on the sheet, the finder's kinds, ticked to begin with
KINDS = [
    ("k_email", "Email addresses", ("email",), True),
    ("k_phone", "Phone numbers", ("phone",), True),
    ("k_card", "Card numbers", ("card",), True),
    ("k_bank", "Bank details (IBAN, sort code, account number)", ("iban", "sort_code", "account"), True),
    ("k_ids", "National Insurance and NHS numbers", ("ni_number", "nhs_number", "ssn"), True),
    ("k_birth", "Dates of birth", ("birth_date",), True),
    ("k_yours", "Your saved details (My details)", ("yours",), True),
    ("k_address", "Addresses", ("address", "postcode"), False),
    ("k_amount", "Amounts of money", ("amount",), False),
    ("k_person", "People's names", ("person",), False),
]
NAMES_WORDS = ("TidyPaper will try to find people's names and remove as many as it can. "
               "It will miss some - look through anything important yourself.")
HINTS = {
    "k_address": "Found from their postcode - some are missed, or only partly found.",
    "k_amount": "A figure with £, € or $, or with pounds, euros or dollars.",
    "k_person": NAMES_WORDS,
}
# how each finder kind is counted in the words: one, more than one
SAY = {"email": ("email address", "email addresses"), "phone": ("phone number", "phone numbers"),
       "card": ("card number", "card numbers"), "iban": ("IBAN", "IBANs"), "sort_code": ("sort code", "sort codes"),
       "account": ("bank account number", "bank account numbers"),
       "ni_number": ("National Insurance number", "National Insurance numbers"), "nhs_number": ("NHS number", "NHS numbers"),
       "ssn": ("Social Security number", "Social Security numbers"), "birth_date": ("date of birth", "dates of birth"),
       "yours": ("of your saved details", "of your saved details"), "address": ("address", "addresses"),
       "postcode": ("postcode", "postcodes"), "amount": ("amount of money", "amounts of money"), "person": ("name", "names"),
       "words": ("match", "matches")}
# a document's properties, as the report names them (their values are never written)
PROPS = {"author": "author", "title": "title", "subject": "subject", "keywords": "keywords", "creator": "the program that made it",
         "producer": "the program that saved it"}
ORDER = [k for _, _, ks, _ in KINDS for k in ks] + ["words"]
REPORT_NAME = "TidyPaper report.txt"


class Stopped(Exception):
    pass


def _counted(counts: dict) -> str:
    """{"email": 14, "phone": 6} -> "14 email addresses and 6 phone numbers"."""
    parts = [f"{n} {SAY[k][n != 1]}" for k in ORDER if (n := counts.get(k))]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def _pages(nums: list[int]) -> str:
    """[1, 2, 3, 7] (0-based) -> "pages 2-4 and 8"."""
    runs, out = [], []
    for n in sorted(nums):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    for a, b in runs:
        out.append(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}")
    word = "page" if len(nums) == 1 else "pages"
    return f"{word} {out[0]}" if len(out) == 1 else f"{word} {', '.join(out[:-1])} and {out[-1]}"


def pdfs_in(paths: list[Path], deeper: bool) -> list[tuple[Path, Path, Path]]:
    """The PDFs to work on: (file, the folder it was chosen through - or its own folder, where it sits under the
    copies' folder).  A chosen folder gives its PDFs (and its folders' with [deeper])."""
    out, seen = [], set()
    for p in paths:
        if p.is_dir():
            found = sorted(p.rglob("*") if deeper else p.iterdir(), key=lambda q: str(q).lower())
            items = [(q, p, q.relative_to(p)) for q in found if q.is_file() and q.suffix.lower() == ".pdf"]
        else:
            items = [(p, p.parent, Path(p.name))]
        for q, base, rel in items:
            key = str(q.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append((q, base, rel))
    return out


def copies_folder(paths: list[Path], save_to: str = "") -> Path:
    """A new folder for the copies, beside the originals: "Letters - copies 21 Nov 2026" - beside a chosen folder, or
    in the chosen files' own folder (or in 'Save results in')."""
    first = paths[0]
    base = first if first.is_dir() else first.parent
    where = Path(save_to) if save_to else (base.parent if first.is_dir() else base)
    d = date.today()
    return unique(where / f"{base.name or 'PDFs'} - copies {d.day} {d.strftime('%b %Y')}")


def _signed(doc) -> bool:
    for pg in doc:
        for w in pg.widgets() or []:
            if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE and doc.xref_get_key(w.xref, "V")[0] != "null":
                return True
    return False


def _read_scans(doc, engine, stop) -> None:
    """Read the words of the pages that are pictures of text and write them in (as Make words selectable does)."""
    for pg in doc:
        if stop():
            raise Stopped()
        if not scanwords.is_scan(pg):
            continue
        try:
            png, _z, size = scanwords.picture(pg)
            lines = scanwords.read_picture(png, size, engine)
            if lines:
                scanwords.write(pg, scanwords.to_page(pg, lines, size))
        except Exception:
            log.exception("redact many: reading the words on page %d", pg.number + 1)


def words_finder(terms: list[str], whole: bool, anycase: bool):
    """Black out words: each word or phrase typed, found in the page's text - a phrase may run over a line's end;
    [whole]: not inside a longer word ("Ann" isn't found in "Annual")."""
    pats = []
    for t in terms:
        ws = t.split()
        if not ws:
            continue
        body = r"\s+".join(re.escape(w) for w in ws)
        pats.append(re.compile((r"(?<!\w)" + body + r"(?!\w)") if whole else body, re.I if anycase else 0))

    def find(doc) -> list[dict]:
        out = []
        for pg in doc:
            lines = bridge_fill.page_lines(pg)
            text, starts = bridge_fill._page_text(lines)
            for rx in pats:
                for m in rx.finditer(text):
                    p = bridge_fill._placed(m.start(), m.end(), text, lines, starts)
                    if p:
                        out.append({"page": pg.number, "kind": "words", "label": "Words", "text": m.group(0), "rect": p[0][0],
                                    "conf": 1.0, "line": p[1], "more": p[0][1:]})
        return out
    return find


def clear_properties(doc) -> list[str]:
    """Empty a document's properties (author, title, subject, keywords, the programs) and its XMP copy of them.
    -> the names of the ones that had something in them."""
    had = [k for k in PROPS if (doc.metadata or {}).get(k)]
    doc.set_metadata({})
    try:
        if doc.get_xml_metadata():
            doc.del_xml_metadata()
    except Exception:
        log.exception("clearing a document's XMP properties")
    return had


def take_out(doc, finds: list[dict]):
    """Take these finds out of the document for good.  -> (the document - a new one if a page had to be put back -,
    the finds that couldn't be taken out).  Each page whole; when one fails, the document is put back as it was and
    what had gone is done again, then that page's finds are tried one at a time."""
    good = doc.tobytes(garbage=0, deflate=False)
    done: list[list] = []
    stuck = []

    def attempt(d, i, rects):
        try:
            black_out(d[i], rects)
            return d, True
        except Exception:
            log.warning("redact many: page %d refused", i + 1, exc_info=True)
            d.close()
            d = fitz.open("pdf", good)
            for p, rs in done:
                black_out(d[p], rs)
            return d, False

    pages: dict[int, list] = {}
    for f in finds:
        pages.setdefault(f["page"], []).append(f)
    for i in sorted(pages):
        rects = [r for f in pages[i] for r in bridge_fill._boxes(f)]
        doc, ok = attempt(doc, i, rects)
        if ok:
            done.append([i, rects])
            continue
        for f in pages[i]:
            doc, ok = attempt(doc, i, bridge_fill._boxes(f))
            if ok:
                done.append([i, bridge_fill._boxes(f)])
            else:
                stuck.append(f)
    return doc, stuck


def one_file(src: Path, dst: Path, find, read: bool, engine, stop, props: bool = False) -> dict:
    """One PDF: a row of the report.  [find]: the document -> its finds (None: nothing is looked for - Clear document
    properties).  [props]: its properties are emptied too.
    {"file", "copy", "skipped", "counts", "stuck", "not_looked", "stopped_reading", "props"}"""
    row = {"file": str(src), "copy": None, "skipped": None, "counts": {}, "stuck": 0, "not_looked": [], "stopped_reading": False,
           "props": []}
    try:
        doc = fitz.open(str(src))
    except Exception:
        log.exception("redact many: couldn't open %s", src)
        row["skipped"] = "it wouldn't open - it may be damaged or not really a PDF"
        return row
    try:
        if doc.needs_pass:
            row["skipped"] = "it's locked with a password"
            return row
        if _signed(doc):
            row["skipped"] = ("it's digitally signed - a copy with things taken out would lose its signature" if find
                              else "it's digitally signed - a copy with its properties changed would lose its signature")
            return row
        # as it will be read: form answers, typed text and notes made part of the pages (nothing can be got back)
        flat = fitz.open("pdf", doc.tobytes(garbage=3, deflate=True))
    except Exception:
        log.exception("redact many: couldn't read %s", src)
        row["skipped"] = "it couldn't be read"
        return row
    finally:
        doc.close()
    try:
        if find:
            flat.bake(annots=True, widgets=True)
            if read and engine:
                try:
                    _read_scans(flat, engine, stop)
                except Stopped:
                    row["stopped_reading"] = True
            row["not_looked"] = [pg.number for pg in flat if scanwords.is_scan(pg)]
            finds = find(flat)
            flat, stuck = take_out(flat, finds)
            for f in finds:
                if not any(f is x for x in stuck):
                    row["counts"][f["kind"]] = row["counts"].get(f["kind"], 0) + 1
            row["stuck"] = len(stuck)
            flat.scrub(attached_files=True, clean_pages=False, embedded_files=True, hidden_text=False, javascript=True,
                       metadata=False, redactions=False, redact_images=0, remove_links=False, reset_fields=False,
                       reset_responses=False, thumbnails=True, xml_metadata=False)
        if props:
            row["props"] = clear_properties(flat)
        dst.parent.mkdir(parents=True, exist_ok=True)
        target = unique(dst)
        tmp = target.with_name(target.name + ".tmp")
        flat.save(str(tmp), garbage=4, deflate=True)
        tmp.replace(target)
        row["copy"] = str(target)
    except Exception:
        log.exception("redact many: %s", src)
        row["skipped"] = "something went wrong with it - no copy was made"
    finally:
        flat.close()
    return row


def describe(row: dict, mode: str = "kinds") -> str:
    """One line of the report, for one file - counts, never the details (nor the properties' values)."""
    name = Path(row["file"]).name
    if row["skipped"]:
        return f"{name} - skipped: {row['skipped']}."
    if row.get("not_done"):
        return f"{name} - not done: stopped before it."
    bits = []
    if mode != "props":
        bits.append(f"removed {_counted(row['counts'])}" if row["counts"]
                    else "nothing of these kinds found" if mode == "kinds" else "none of the words found")
    if row["props"]:
        names = [PROPS[k] for k in row["props"]]
        bits.append("cleared its properties (" + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]) + ")")
    elif mode == "props":
        bits.append("it had no properties to clear")
    if row["stuck"]:
        bits.append(f"{row['stuck']} more found that couldn't be taken out - look at the copy")
    if row["not_looked"]:
        why = "reading was stopped" if row["stopped_reading"] else "scans whose words couldn't be read"
        bits.append(f"{_pages(row['not_looked'])} not looked at ({why})")
    return f"{name} - " + "; ".join(bits) + "."


def summary(rows: list[dict], stopped: bool, which: str = "the report says which", mode: str = "kinds") -> str:
    """The sentence at the end: what was done.  Never "removed personal details" or "safe to share"."""
    total: dict = {}
    files = 0
    for r in rows:
        if r["counts"]:
            files += 1
            for k, n in r["counts"].items():
                total[k] = total.get(k, 0) + n
    copied = sum(1 for r in rows if r["copy"])
    cleared = sum(1 for r in rows if r["props"])

    def fs(n):
        return f"{n} file{'s' if n != 1 else ''}"
    if mode == "props":
        s = (f"Cleared the properties of {fs(cleared)}." if cleared else
             f"None of the {fs(copied)} had properties to clear." if copied else "No copies were made.")
        if cleared and copied > cleared:
            s += f" {copied - cleared} had none."
    elif total:
        s = f"Removed {_counted(total)} from {fs(files)}."
    elif copied:
        s = (f"Nothing of the kinds ticked was found in {fs(copied)}, so nothing was removed." if mode == "kinds"
             else f"None of the words were found in {fs(copied)}, so nothing was removed.")
    else:
        s = "No copies were made."
    if mode != "props" and cleared:
        s += f" Cleared the properties of {fs(cleared)}."
    more = []
    skipped = sum(1 for r in rows if r["skipped"])
    pages = sum(len(r["not_looked"]) for r in rows if r["copy"])
    stuck = sum(r["stuck"] for r in rows)
    if skipped:
        more.append(f"{skipped} file{'s were' if skipped != 1 else ' was'} skipped")
    if pages:
        more.append(f"{pages} page{'s were' if pages != 1 else ' was'} not looked at")
    if stuck:
        more.append(f"{stuck} thing{'s' if stuck != 1 else ''} found couldn't be taken out")
    not_done = sum(1 for r in rows if r.get("not_done"))
    if stopped and not_done:
        more.append(f"it was stopped before {not_done} file{'s' if not_done != 1 else ''}")
    if more:
        tail = more[0] if len(more) == 1 else ", ".join(more[:-1]) + " and " + more[-1]
        s += " " + tail[0].upper() + tail[1:] + f" - {which}."
    return s


TITLES = {"kinds": "Remove personal details", "words": "Black out words", "props": "Clear document properties"}


def run(paths, opts: dict, say, job=None, mode: str = "kinds") -> list:
    """A task: paths (files and folders) -> [the copies' folder, the report].  [mode]: "kinds" - Remove personal
    details from many PDFs (kinds ticked); "words" - Black out words (the words typed); "props" - Clear document
    properties.  [job]: taskrunner's Job - its .stop is the person's Stop; .extra gets the summary and the report's
    rows for the end screen."""
    stop = (lambda: bool(getattr(job, "stop", False)))
    only = set()
    if mode == "kinds":
        chosen = [key for key, *_ in KINDS if opts.get(key)]
        if not chosen:
            raise _tool_error("Tick at least one kind to take out.")
        only = {k for key, _, ks, _ in KINDS if key in chosen for k in ks}
        own = bridge_fill.own_details(bridge_fill.load_details()) if "yours" in only else []
        also = tuple(k for k in ("person", "amount") if k in only)

        def find(d):
            return bridge_fill.find_personal(d, own, also=also, only=only)
        looked = "Looked for: " + ", ".join((w if w.startswith("National") else w[0].lower() + w[1:])
                                            for key, label, *_ in KINDS if key in chosen for w in [label.split(" (")[0]]) + "."
    elif mode == "words":
        terms = [t.strip() for t in str(opts.get("words") or "").splitlines() if t.strip()]
        if not terms:
            raise _tool_error("Type the words to black out, one to a line.")
        find = words_finder(terms, bool(opts.get("whole", True)), bool(opts.get("anycase", True)))
        n = len(terms)
        looked = f"Looked for: {n} word{'s' if n != 1 else ''} or phrase{'s' if n != 1 else ''} you typed (not repeated here)."
    else:
        find = None
        looked = "Cleared: author, title, subject, keywords and the programs that made and saved each file."
    props = mode == "props" or bool(opts.get("clear_props"))
    paths = [Path(p) for p in paths]
    files = pdfs_in(paths, bool(opts.get("deeper")))
    if not files:
        raise _tool_error("There are no PDFs in what was chosen" + ("." if opts.get("deeper") else
                          " - tick “Include folders inside” under More options to look deeper."))
    read = bool(opts.get("read_scans")) and find is not None
    engine = scanwords.default_engine() if read else None
    out = copies_folder(paths, (opts.get("save_to") or "").strip())
    out.mkdir(parents=True)
    rows, stopped = [], False
    started = time.time()
    for n, (src, _base, rel) in enumerate(files):
        if stop():
            stopped = True
            rows += [{"file": str(s_), "copy": None, "skipped": None, "counts": {}, "stuck": 0, "not_looked": [],
                      "stopped_reading": False, "props": [], "not_done": True} for s_, _, _ in files[n:]]
            break
        say(f"File {n + 1} of {len(files)}: {src.name}")
        rows.append(one_file(src, out / rel, find, read, engine, stop, props))
        if rows[-1]["stopped_reading"]:
            stopped = True
    said = summary(rows, stopped, mode=mode)
    report = [f"{TITLES[mode]} - {datetime.now():%d %b %Y, %H:%M}",
              f"From: {', '.join(str(p) for p in paths)}", looked,
              "", summary(rows, stopped, "see below", mode), ""]
    if "person" in only:
        report += [NAMES_WORDS, ""]
    report += [describe(r, mode) for r in rows]
    report += ["", "The original files haven't been changed." + (" What was taken out of the copies is gone for good and blacked over."
                                                                  if find else "")]
    rp = out / REPORT_NAME
    rp.write_text("\n".join(report) + "\n", encoding="utf-8")
    log.info("%s: %d files in %.1fs, %s", mode, len(files), time.time() - started, said)
    if job is not None:
        job.extra = {"summary": said, "rows": [{"name": Path(r["file"]).name, "line": describe(r, mode)} for r in rows],
                     "names": NAMES_WORDS if "person" in only else ""}
    return [out, rp]


def _tool_error(msg):
    import tools
    return tools.ToolError(msg)
