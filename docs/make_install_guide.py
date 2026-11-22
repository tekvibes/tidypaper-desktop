"""Builds "How to install <App>.pdf" - the plain-English guide to send with a download or invitation link: how to install
it (pages 1-2), then everything it can do.  The name comes from brand.json (and BRAND_NAME for a one-off build, like the
rest of the build).

    py docs\\make_install_guide.py [output folder]          (default: docs\\)
    python3 docs/make_install_guide.py --mac [output folder]   the Mac edition: "How to install <App> on a Mac.pdf"

The "More tools" list and the add-ons are read from taskrunner.py, so a tool added or renamed there shows up in the next
build's guide by itself.  The rest (install steps, editor, Ask) is written below - update it when those change.
Screenshots live in docs/img (retake them when the app's look changes).  Layout uses PyMuPDF's HTML Story, which supports
simple HTML/CSS (tables for layout, no flexbox).
"""

import html
import io
import sys
from pathlib import Path

import pymupdf as fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import brand  # noqa: E402
import taskrunner  # noqa: E402

B = brand.for_build()
MAC = "--mac" in sys.argv
OUT = HERE / f"How to install {brand.file_stem(B['name'])}{' on a Mac' if MAC else ''}.pdf"

CSS = """
* { font-family: sans-serif; }
body { font-size: 10.5pt; line-height: 1.45; color: #1f2328; }
h1 { font-size: 22pt; margin: 0 0 4pt 0; }
.lead { color: #4b5563; font-size: 11pt; margin: 0 0 12pt 0; }
h2 { font-size: 13pt; margin: 14pt 0 4pt 0; }
h3 { font-size: 11pt; margin: 10pt 0 2pt 0; color: #374151; }
p { margin: 3pt 0 5pt 0; }
.box { background-color: #fffbeb; color: #78350f; padding: 6pt 8pt; margin: 6pt 0; }
.ok { background-color: #ecfdf5; color: #14532d; padding: 6pt 8pt; margin: 6pt 0; }
.muted { color: #6b7280; font-size: 9.5pt; }
.shot { border: 1px solid #d1d5db; }
td { vertical-align: top; }
.list td { padding: 3pt 6pt 3pt 0; border-bottom: 1px solid #e5e7eb; }
.name { font-weight: bold; }
.list td.group { font-weight: bold; font-size: 11pt; color: #374151; padding-top: 9pt; border-bottom: 1px solid #9ca3af; }
.help { border: 1px solid #d1d5db; padding: 8pt; margin-top: 10pt; }
"""

HTML = """
<h1>Getting started with {app}</h1>
<p class="lead">{app} is a free app for Windows that opens, signs, fills in and fixes PDFs and Word documents - and can explain any
document in plain English. Pages 1 and 2 show how to install it; after that, everything it can do.</p>

<h2>1. Download {app}</h2>
<p>Open the link you were sent. A <b>download link</b> starts the download straight away; an <b>invitation link</b> opens a page -
click <b>Download {app}</b> there. Either way you get <b>{stem}Setup.exe</b> (about 60 MB) in your <b>Downloads</b> folder.</p>
<p class="box">Your browser may say the file <b>"isn't commonly downloaded"</b>. Choose <b>Keep</b> (in Edge: point at the download,
click <b>...</b>, then <b>Keep</b>). It says this because {app} is new and doesn't come from a big software company.</p>

<h2>2. Install it</h2>
<p>Open your <b>Downloads</b> folder and double-click <b>{stem}Setup.exe</b>. Follow the steps - the choices it starts with are fine.
{app} then appears in the Start menu.</p>
<p class="box">Windows may say <b>"Windows protected your PC"</b>. Click <b>More info</b>, then <b>Run anyway</b>. It's only shown once.</p>
<p>Setup also offers some <b>free add-ons</b> (see the end of this guide). You don't need them now - {app} offers each one the
first time a tool needs it.</p>
<p class="muted">Got a zip file instead ({stem}-portable.zip)? Right-click it, choose <b>Extract All</b>, pick your <b>Documents</b>
folder, then open the new <b>{stem}</b> folder and double-click <b>{stem}</b>.
Tip: while {app} is open, right-click its icon on the taskbar and choose <b>Pin to taskbar</b>.</p>

<h2>3. Connect {ai} (if you have an invitation)</h2>
<p>On your invitation page click <b>Copy my invitation</b>. In {app}, click <b>Settings</b> at the top right:</p>
<p><img class="shot" src="home-settings.png" width="300"/></p>
<p>Paste it (<b>Ctrl + V</b>) into <b>Got an invitation?</b> and click <b>Use invitation</b>:</p>
<p><img class="shot" src="invite-box.png" width="380"/></p>
<p class="ok">{app} says <b>Connected as</b> your name and shows how much of this month's allowance is left.</p>
<p class="muted">No invitation? Everything except Ask works without one.</p>

<!--PAGE-->
<h2>4. Try it</h2>
<table><tr>
<td width="56%">
<p>Open a PDF or a Word document - drag it onto the {app} window, or click <b>Choose a file...</b>. Then click <b>Ask</b> at the top and
choose <b>Summarise</b>, <b>What do I need to do?</b> or <b>Dates &amp; money</b> - or type your own question.</p>
<p>Each answer shows page numbers like <b>p.2</b>. Click one to jump to that page and see the exact words - AI can
get things wrong, so check anything important.</p>
<p>For a harder question, click <b>Think it through</b> under the answer: it takes a little longer and looks more carefully.</p>
<p>The first time, {app} shows you exactly which pages will be sent before anything leaves your PC.</p>
</td>
<td width="44%"><img class="shot" src="ask-answer.png" width="200"/></td>
</tr></table>

<h2>Good to know</h2>
<p>- Your invitation is <b>personal</b>. Please don't share it - if it's ever lost, ask for a new one (the old one then stops working).</p>
<p>- A document's text is only sent when <b>you</b> ask about it, and never anywhere except {ai}.</p>
<p>- If {app} says <b>"This month's AI allowance is used up"</b>, it starts again on the 1st. Everything else in {app} keeps working.</p>
<p>- {app} never overwrites your original files - edited, signed or smaller copies are saved next to them with a clear name.</p>
<p>- When there's a new version, {app} tells you with a <b>Get it</b> button. Run the new {stem}Setup.exe - it updates {app} in place.</p>

<div class="help"><b>Stuck? Ask:</b> ______________________________ &nbsp;&nbsp; <b>Phone:</b> ____________________</div>

<!--PAGE-->
<h1>What you can do with {app}</h1>
<p class="lead">Everything here works on your PC, without the internet - only Ask needs {ai}.</p>

<h2>The home screen</h2>
<p><img class="shot" src="home.jpg" width="400"/></p>
<p><b>Open a file:</b> drag it onto the window, or click <b>Choose a file...</b>. {app} opens PDFs, Word, Excel and PowerPoint
files, text and CSV files, photos and e-books. Anything that isn't a PDF opens as a PDF copy - when you save, you get
<b>Name_edited.pdf</b> next to the original.</p>
<p><b>From File Explorer:</b> right-click a file and choose <b>Open in {app}</b>, or <b>Edit with {app}</b> for a PDF.
(On Windows 11 these are under <b>Show more options</b>.)</p>
<p>The <b>eight big buttons</b> are the everyday jobs; <b>More tools</b> has the rest (listed at the end of this guide).
<b>Recent files</b> brings back what you opened last, and <b>Auto / Light / Dark</b> at the top changes the colours.</p>

<h2>Scan a paper document</h2>
<p><img class="shot" src="scan.png" width="330"/></p>
<p>Click <b>Scan a paper document</b> on the home screen. <b>Use my scanner</b> works with a scanner or printer-scanner connected to
your PC. <b>Use my phone's camera</b> shows a QR code: point your phone's camera at it, take a photo of each page, and the pages
arrive in {app} (your phone needs to be on the same Wi-Fi). Put the pages in order, then make one PDF.</p>

<!--PAGE-->
<h2>The editor</h2>
<p>Open a document and it fills the window: the pages on the left, the tools along the top.</p>
<p><img class="shot" src="editor.jpg" width="499"/></p>
<p><img class="shot" src="toolbar.png" width="499"/></p>
<table class="list" width="100%">
<tr><td class="name" width="22%">Select</td><td>Click a box on a form to type in it (Tab moves to the next box). Pick out words to copy them.</td></tr>
<tr><td class="name">Mark up</td><td>Highlight, underline or strike through words.</td></tr>
<tr><td class="name">Draw</td><td>A pen, boxes, lines and arrows.</td></tr>
<tr><td class="name">Text</td><td>Add a text box, a sticky note, a picture or a link.</td></tr>
<tr><td class="name">Edit text</td><td>Change the document's own words - fix a typo, a name or a date.</td></tr>
<tr><td class="name">Redact</td><td>Remove words or areas for good - they're gone, not just covered up.</td></tr>
<tr><td class="name">Sign</td><td>Add your signature, drawn or typed - or a digital signature.</td></tr>
<tr><td class="name">Pages</td><td>Show or hide the page list. Drag a page to move it. Right-click a page to turn, copy, crop or delete it,
add a blank page, put in pages from another file, or save one page as a PDF or a picture.</td></tr>
<tr><td class="name">Ask</td><td>Ask {ai} about the document (see below).</td></tr>
<tr><td class="name">Small buttons</td><td>Undo and redo (Ctrl+Z, Ctrl+Y), find words (Ctrl+F), a list of every comment and mark, and print (Ctrl+P).</td></tr>
<tr><td class="name">Save</td><td>Save your changes (Ctrl+S).</td></tr>
<tr><td class="name">Zoom</td><td>The - and + buttons beside the page. Click the percentage to fit the page to the window or to
show two pages side by side.</td></tr>
</table>

<!--PAGE-->
<h2>The ... menu (top right)</h2>
<table><tr>
<td width="62%">
<p><b>Save a copy as...</b> - save under a new name or in another folder.</p>
<p><b>Print...</b> and <b>Email this document...</b> - email opens your email program with the PDF already attached.</p>
<p><b>Save a final copy (can't be changed)</b> - for sending a finished document.</p>
<p><b>Protect with a password</b> - only people with the password can open the copy. A locked PDF you've opened can be saved
without its password.</p>
<p><b>Fill with my details</b> - fills your name, address and so on into a form. Set them once in <b>Settings</b>, <b>My details</b>; they stay on your PC.</p>
<p><b>Find personal details to remove</b> - finds things like phone numbers and email addresses so you can redact them before sharing.</p>
<p><b>Check signatures</b> - shows whether the digital signatures in a PDF are genuine. <b>My signing identity</b> - your digital
signature and your drawn or typed signature.</p>
</td>
<td width="38%"><img class="shot" src="more-menu.png" width="170"/></td>
</tr></table>

<h2>Ask about a document ({ai})</h2>
<p>Click <b>Ask</b> in the editor. Pick <b>Summarise</b>, <b>What do I need to do?</b> or <b>Dates &amp; money</b>, or type any question,
such as "Can I cancel early?". Answers point to their pages (<b>p.2</b>) - click one to see the words yourself.</p>
<p><b>Think it through</b> under an answer asks again more carefully. To <b>copy</b> an answer, select its words and press Ctrl+C, or
right-click it and choose <b>Copy this answer</b>.</p>
<p>The top of the panel shows how much of this month's allowance is left. It needs an invitation (step 3), or your own AI account
in Settings.</p>

<h2>More tools</h2>
<p>On the home screen, click <b>More tools</b>, pick one and choose your files. The result is saved next to them - your files
aren't changed.</p>
{tools}

<h2>Free add-ons</h2>
<p>A few tools need a free program from elsewhere. Tick them when you install {app}, or let {app} add one the first time a tool asks.</p>
{addons}
"""

# ---- the Mac edition: its own install pages (1-2), and Mac words in the rest
MAC_INSTALL = """
<h1>Getting started with {app} on a Mac</h1>
<p class="lead">{app} is a free app for Mac that opens, signs, fills in and fixes PDFs and Word documents - and can explain any
document in plain English. Pages 1 and 2 show how to install it; after that, everything it can do.</p>
<p class="muted">It needs a Mac with an Apple chip (M1 or newer) and macOS 13 Ventura or later (Apple menu, <b>About This Mac</b>).</p>

<h2>1. Download {app}</h2>
<p>Open the link you were sent. A <b>download link</b> starts the download straight away; an <b>invitation link</b> opens a page -
click <b>Download the Mac version</b> there. Either way you get <b>{stem}-....dmg</b> (about 100 MB) in your <b>Downloads</b> folder.</p>

<h2>2. Install it</h2>
<p>Double-click the <b>.dmg</b> file in <b>Downloads</b>. A window opens with {app} and an <b>Applications</b> folder:
<b>drag {app} onto Applications</b>. Then close that window and, in Finder's sidebar, click the eject button next to <b>{app}</b>.</p>
<p>Open <b>Applications</b> and double-click <b>{app}</b>. The first time, your Mac asks <b>"Are you sure you want to open it?"</b> -
click <b>Open</b>.</p>
<p class="box">If it says <b>"{app}" can't be opened</b> instead: click <b>Done</b>, open the Apple menu, <b>System Settings</b>,
<b>Privacy &amp; Security</b>, scroll down and click <b>Open Anyway</b> next to {app}. It's only asked once.</p>
<p class="muted">Tip: while {app} is open, right-click its icon in the Dock and choose <b>Options</b>, <b>Keep in Dock</b>.
Reading scanned pages and the video and audio tools come built in - nothing else to install.</p>

<h2>3. Connect {ai} (if you have an invitation)</h2>
<p>On your invitation page click <b>Copy my invitation</b>. In {app}, click <b>Settings</b> at the top right:</p>
<p><img class="shot" src="home-settings.png" width="300"/></p>
<p>Paste it (<b>Command + V</b>) into <b>Got an invitation?</b> and click <b>Use invitation</b>:</p>
<p><img class="shot" src="invite-box.png" width="380"/></p>
<p class="ok">{app} says <b>Connected as</b> your name and shows how much of this month's allowance is left.</p>
<p class="muted">Your Mac may ask whether {app} can use your <b>keychain</b> (where it keeps the invitation safely):
type your Mac password and click <b>Always Allow</b>. No invitation? Everything except Ask works without one.</p>
"""

MAC_SWAPS = [
    ("before anything leaves your PC", "before anything leaves your Mac"),
    ("Run the new {stem}Setup.exe - it updates {app} in place.",
     "Open the new .dmg and drag {app} onto Applications again - choose <b>Replace</b>."),
    ("Everything here works on your PC", "Everything here works on your Mac"),
    ("""<p><b>From File Explorer:</b> right-click a file and choose <b>Open in {app}</b>, or <b>Edit with {app}</b> for a PDF.
(On Windows 11 these are under <b>Show more options</b>.)</p>""",
     """<p><b>From Finder:</b> right-click a file, choose <b>Open With</b> and pick <b>{app}</b>. (To always open PDFs in {app}:
choose <b>Get Info</b>, pick {app} under <b>Open with</b> and click <b>Change All</b>.)</p>"""),
    ("""<b>Use my scanner</b> works with a scanner or printer-scanner connected to
your PC. <b>Use my phone's camera</b> shows a QR code:""", """<b>Use my phone's camera</b> shows a QR code:"""),
    ("Undo and redo (Ctrl+Z, Ctrl+Y), find words (Ctrl+F), a list of every comment and mark, and print (Ctrl+P).",
     "Undo and redo (Command+Z, Shift+Command+Z), find words (Command+F), a list of every comment and mark, and print (Command+P)."),
    ("Save your changes (Ctrl+S).", "Save your changes (Command+S)."),
    ("email opens your email program with the PDF already attached.", "email opens Mail (or Outlook) with the PDF already attached."),
    ("they stay on your PC.", "they stay in your Mac's keychain."),
    ("select its words and press Ctrl+C", "select its words and press Command+C"),
    ("""<p>A few tools need a free program from elsewhere. Tick them when you install {app}, or let {app} add one the first time a tool asks.</p>
{addons}""", """<p>Reading scanned pages (Tesseract) and the video and audio tools (FFmpeg) come built into the Mac version. Opening Word,
Excel and PowerPoint files with their layout and pictures needs the free <b>LibreOffice</b> (libreoffice.org, about 350 MB) -
without it, {app} shows just their words.</p>"""),
]


def mac_html(text: str) -> str:
    """The Windows guide with the Mac install pages and Mac words.  Every swap must find its text (so edits to HTML
    above that break one are noticed at build time)."""
    first, rest = text.split("<!--PAGE-->", 1)
    for a, b in MAC_SWAPS:
        if a not in rest and a not in first:
            raise SystemExit(f"Mac guide: couldn't find the text to swap: {a[:60]!r}")
        rest = rest.replace(a, b)
    return MAC_INSTALL + "<!--PAGE-->" + rest


def _esc(s) -> str:
    return html.escape(str(s or "")).replace("→", "to")      # the PDF's built-in font has no arrow: "Photos to PDF"


def addons_html() -> str:
    rows = "".join(f'<tr><td class="name" width="22%">{_esc(a["name"])}</td><td>{_esc(a["why"])} '
                   f'<span class="muted">({_esc(a["size"])})</span></td></tr>' for a in taskrunner.ADDONS.values())
    return f'<table class="list" width="100%">{rows}</table>'


def tools_html() -> str:
    """The More tools list, grouped as in the app - read from taskrunner.TASK_LIST (the home-screen buttons are left out)."""
    rows = []                                   # one table, so the columns line up across the groups
    for g in taskrunner.GROUPS:
        tasks = [t for t in taskrunner.TASK_LIST if t.group == g and not t.home]
        if not tasks:
            continue
        rows.append(f'<tr><td class="group" colspan="2">{_esc(g)}</td></tr>')
        for t in tasks:
            need = ""
            if t.addon in taskrunner.ADDONS:
                name = taskrunner.ADDONS[t.addon]["name"]
                need = (f' <span class="muted">Needs the free add-on {_esc(name)}'
                        + (" if Microsoft Office isn't installed" if t.addon_unless else "") + ".</span>")
            rows.append(f'<tr><td class="name" width="30%">{_esc(t.title)}</td><td>{_esc(t.desc)}{need}</td></tr>')
    return f'<table class="list" width="100%">{"".join(rows)}</table>'


def page_html() -> str:
    """The guide with the app's name in it: {app} = name, {stem} = how it appears in file names, {ai} = the AI's name."""
    text = ((mac_html(HTML) if MAC else HTML).replace("{app}", html.escape(B["name"])).replace("{stem}", html.escape(brand.file_stem(B["name"])))
            .replace("{ai}", html.escape(B["ai_name"])))
    return text.replace("{addons}", addons_html()).replace("{tools}", tools_html())


def build(out: Path = OUT) -> Path:
    buf = io.BytesIO()                          # built in memory, written once at the end (compressed)
    writer = fitz.DocumentWriter(buf)
    page = fitz.paper_rect("a4")
    area = page + (48, 44, -48, -44)
    for part in page_html().split("<!--PAGE-->"):          # each part starts on a fresh page
        story = fitz.Story(html=part, user_css=CSS, archive=fitz.Archive(str(HERE / "img")))
        for _ in range(10):                          # a part never needs more than a few pages
            dev = writer.begin_page(page)
            more, _ = story.place(area)
            story.draw(dev)
            writer.end_page()
            if not more:
                break
    writer.close()
    # the Story stores PNG screenshots uncompressed (6 MB+): compress every stream - same pictures, a fraction of the size
    with fitz.open(stream=buf.getvalue(), filetype="pdf") as d:
        out.write_bytes(d.tobytes(garbage=3, deflate=True, deflate_images=True))
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--mac"]
    p = build(Path(args[0]) / OUT.name if args else OUT)
    with fitz.open(p) as d:
        print(f"{p.name}: {len(d)} page(s)")
