"""Makes ui/sample/Welcome to TidyPaper.pdf - the short tour opened by "Try a sample" on Home (desktop and iPad/iPhone;
Batch 4 of the Folio features, "sample").  The iPad app bundles the same file.  Run from the repo root:
    .venv/bin/python3 build/make_sample.py
It has: a contents list (the PDF's own outline), words to highlight and to change, made-up details to redact, form
boxes, places to sign, a picture, and facts to ask about.  Everything in it is made up.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import brand  # noqa: E402

OUT = ROOT / "ui" / "sample" / "Welcome to TidyPaper.pdf"
NAME = brand.NAME
INK = "#1a237e"
CSS = f"""
* {{ font-family: sans-serif; }}
body {{ font-size: 11pt; line-height: 1.45; color: #1f2430; }}
h1 {{ font-size: 24pt; color: {INK}; margin: 0 0 4pt 0; }}
h2 {{ font-size: 16pt; color: {INK}; margin: 0 0 6pt 0; }}
p {{ margin: 0 0 8pt 0; }}
.lead {{ font-size: 12.5pt; color: #3c4252; }}
.try {{ background-color: #eef0fb; padding: 6pt 8pt; margin: 4pt 0 10pt 0; }}
.small {{ font-size: 9.5pt; color: #5b6275; }}
li {{ margin-bottom: 3pt; }}
"""
W, H = 595, 842
M = 60


def page(doc, html, top=M, bottom=H - M):
    p = doc.new_page(width=W, height=H)
    p.insert_htmlbox(fitz.Rect(M, top, W - M, bottom), html, css=CSS)
    p.insert_text((M, H - 30), f"{NAME} - a short tour · page {doc.page_count}", fontsize=8, color=(0.45, 0.47, 0.55))
    return p


def make() -> fitz.Document:
    doc = fitz.open()
    # 1. welcome
    page(doc, f"""
<h1>Welcome to {NAME}</h1>
<p class="lead">This short document is for trying things out. Nothing you do here changes anything else, and nothing
is kept until you choose <b>Save</b> - then it goes in a copy, never over this one.</p>
<h2>What's in this tour</h2>
<ol>
<li><b>Reading</b> - this page: the contents list, finding words, bookmarks.</li>
<li><b>Marking up</b> - highlights, notes, changing words, removing details for good.</li>
<li><b>Filling in and signing</b> - boxes to type in, and places to sign.</li>
<li><b>Pictures and questions</b> - a picture to look at, and facts to ask about.</li>
</ol>
<div class="try"><b>Try this:</b> open the pages panel and choose <b>Contents</b> - the four parts of this tour are
listed there. Then search for the word <b>kettle</b>: it's hidden on page 4.</div>
<div class="try"><b>Try this:</b> bookmark this page (the ribbon on its thumbnail), then go to page 3 and back with the
<b>Bookmarked</b> filter.</div>
<p class="small">On a computer, press F for focus mode (Esc to come back). On an iPad or iPhone, focus mode is in the
More (•••) menu.</p>
""")
    # 2. marking up
    page(doc, """
<h2>2. Marking up</h2>
<p>Select this sentence and choose Highlight - then click or tap the highlight to give it a colour and a note.</p>
<p>Notes you add to highlights are kept inside the PDF, so other PDF apps show them too. Your own notes about the
whole document go in the <b>Notes</b> panel, and <b>Export notes</b> makes a tidy list of everything you marked.</p>
<div class="try"><b>Try this:</b> use <b>Edit text</b> on the next line to change its words. The old words are really
taken out of the page, not just covered up.</div>
<p>The meeting room is on the second floor, next to the lifts.</p>
<div class="try"><b>Try this:</b> use <b>Redact</b> on the account number below. After Remove permanently, it's gone
from the file for good - search for it and see.</div>
<p>Account name: Sam Sample<br/>Account number: 12345678<br/>Sort code: 00-11-22</p>
<p class="small">These details are made up.</p>
""")
    # 3. filling in and signing
    p3 = page(doc, """
<h2>3. Filling in and signing</h2>
<p>The boxes below are a real form: click or tap one and type. Tick boxes tick.</p>
<p>&nbsp;</p><p>Your name:</p><p>&nbsp;</p><p>Send me a copy by email:</p>
<p>&nbsp;</p>
<div class="try"><b>Try this:</b> on a computer choose <b>Sign</b> - the places to sign below get a <b>Sign here</b>
box. On an iPad or iPhone choose <b>Sign and fill in › Where to sign…</b>. Your signature sits on the line; you can
still move it.</div>
""", bottom=300)
    # the form boxes go beside their labels (found on the page), with a border so they show everywhere
    name = p3.search_for("Your name:")[0]
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "Your name"
    w.rect = fitz.Rect(name.x1 + 8, name.y0 - 4, name.x1 + 258, name.y1 + 4)
    w.text_fontsize = 11
    w.border_color, w.border_width, w.fill_color = (0.55, 0.58, 0.68), 0.8, (0.97, 0.97, 1)
    p3.add_widget(w)
    copy = p3.search_for("Send me a copy by email:")[0]
    t = fitz.Widget()
    t.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    t.field_name = "Send me a copy"
    side = copy.height + 2
    t.rect = fitz.Rect(copy.x1 + 8, copy.y0 - 1, copy.x1 + 8 + side, copy.y0 - 1 + side)
    t.border_color, t.border_width, t.fill_color = (0.55, 0.58, 0.68), 0.8, (0.97, 0.97, 1)
    p3.add_widget(t)
    p3.insert_text((M, 330), "Signature: ______________________________", fontsize=11)
    p3.insert_text((M, 380), "Date: ________________", fontsize=11)
    p3.insert_text((M, 430), "Initials: ____________", fontsize=11)
    p3.insert_text((M, 490), "Signed by the witness:", fontsize=11)
    # 4. pictures and questions
    p4 = page(doc, """
<h2>4. Pictures and questions</h2>
<p>Click or tap the picture to see it full size, copy it, save it, or put another picture in its place.</p>
""", bottom=200)
    icon = ROOT / "build" / "icon.png"
    if icon.exists():
        p4.insert_image(fitz.Rect(M, 150, M + 120, 270), filename=str(icon))
    p4.insert_htmlbox(fitz.Rect(M, 290, W - M, H - M), """
<h3>Your visit to the Sample Centre</h3>
<p>Your appointment is on <b>Tuesday 14 March at 10:30</b>, with Dr Riley. Please arrive ten minutes early.</p>
<p>Bring: your passport or driving licence, a recent bill with your address on it, and this letter.</p>
<p>Parking is free for two hours. There's a café on the ground floor, and a kettle in the waiting room.</p>
<p>To change the time, ring 01234 567890 before Friday 10 March.</p>
<div class="try"><b>Try this:</b> choose <b>Ask</b> and try <i>When is the appointment?</i>, <i>What do I need to
bring?</i> or <i>Make a to-do list</i>. Each answer shows which page it came from - tap the page to see the words.
(Ask needs your invitation to the Shared AI - see Settings.)</div>
""", css=CSS)
    doc.set_toc([[1, "1. Reading", 1], [1, "2. Marking up", 2], [1, "3. Filling in and signing", 3],
                 [1, "4. Pictures and questions", 4]])
    doc.set_metadata({"title": f"Welcome to {NAME}", "author": NAME, "subject": "A short tour to try things out",
                      "creator": NAME, "producer": NAME})
    return doc


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    d = make()
    d.save(OUT, garbage=3, deflate=True)
    print("written", OUT, f"{OUT.stat().st_size // 1024} KB")
