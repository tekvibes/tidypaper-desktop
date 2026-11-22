#!/usr/bin/env python3
"""
Toolkit - a small window of everyday file utilities for Windows.

Pick a tool on the left, add files (or drop them on Toolkit.bat / use the right-click menu),
set a couple of options, press Run.  All the real work is in tools.py.
"""

import os
import platform
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402

IS_WIN = platform.system() == "Windows"

# ----------------------------------------------------------------- registry
# Option spec: (key, kind, label, default[, choices])   kinds: text, int, choice, check, folder, password
# pick: "files" | "folders" | "none"   (what the Add button chooses)

def open_pdf_editor(files, opts, log):
    """Launch the PDF editor window (separate process) for each selected PDF."""
    editor = Path(__file__).resolve().parent / "pdf_editor.py"
    pdfs = [f for f in files if f.suffix.lower() == ".pdf"] or [None]
    for f in pdfs:
        cmd = [sys.executable, str(editor)] + ([str(f)] if f else [])
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        log(f"opened editor{' for ' + f.name if f else ''}")
    return []


def sign_pdfs(files, opts, log):
    """Digitally sign every selected PDF (invisible signature) with your certificate."""
    import signing
    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
    if not pdfs:
        raise tools.ToolError("Select PDF files.")
    pfx = Path(opts.get("pfx") or signing.DEFAULT_PFX)
    if not pfx.exists():
        raise tools.ToolError("No certificate found. Open the PDF Editor > Sign > Create my certificate first.")
    signing.certificate_info(pfx, opts.get("password", ""))  # raises on wrong password
    out = []
    for f in pdfs:
        target = tools.out_path(f, opts, suffix="_signed", ext=".pdf")
        rep = signing.sign_pdf(f, target, signing.SignOptions(
            pfx_path=pfx, password=opts.get("password", ""), reason=opts.get("reason", ""),
            location=opts.get("location", ""), timestamp=bool(opts.get("timestamp", True))))
        log(f"{f.name} -> {target.name}" + ("" if rep.get("timestamped") else "  (no timestamp)"))
        out.append(target)
    return out


def verify_pdfs(files, opts, log):
    import signing
    for f in (x for x in files if x.suffix.lower() == ".pdf"):
        log(f"--- {f.name}")
        for line in signing.describe(signing.verify_pdf(f)).splitlines():
            log("   " + line)
    return []


TOOLS = [
    # --- PDF
    dict(group="PDF", name="Sign PDFs digitally (batch)", fn=sign_pdfs, exts={".pdf"}, pick="files",
         desc="Add an invisible cryptographic signature to each PDF using your certificate (create one in the PDF Editor, Sign menu).",
         options=[("pfx", "text", "Certificate (.pfx) - blank = default", ""), ("password", "password", "Password", ""),
                  ("reason", "text", "Reason", "I approve this document"), ("location", "text", "Location", ""),
                  ("timestamp", "check", "Add trusted timestamp (internet)", True)]),
    dict(group="PDF", name="Verify signatures", fn=verify_pdfs, exts={".pdf"}, pick="files", no_outdir=True,
         desc="Check who signed each PDF, when, and whether it was changed afterwards.", options=[]),
    dict(group="PDF", name="PDF Editor (annotate, fill, fix text)", fn=open_pdf_editor, exts={".pdf"}, pick="files",
         no_outdir=True,
         desc="Open the PDF editor: highlight, draw, add text, fill forms, sign, redact, fix words, rearrange pages. "
              "Select PDFs (or none for a blank document) and press Run.", options=[]),
    dict(group="PDF", name="Office → PDF", fn=tools.office_to_pdf, exts=tools.OFFICE_EXTS, pick="files",
         desc="Convert Word, Excel, PowerPoint (and .odt/.rtf/.txt/.csv) to PDF.", options=[]),
    dict(group="PDF", name="Merge PDFs", fn=tools.pdf_merge, exts={".pdf"}, pick="files",
         desc="Combine several PDFs into one, in the order listed (use ▲▼ to reorder).",
         options=[("name", "text", "Output file name", "merged")]),
    dict(group="PDF", name="Split / extract pages", fn=tools.pdf_split, exts={".pdf"}, pick="files",
         desc="Every page to its own file, or pull out a page range.",
         options=[("mode", "choice", "Mode", "Pages", ["Pages", "Every page to its own file"]),
                  ("ranges", "text", "Pages (e.g. 1-3, 7, 10-)", "")]),
    dict(group="PDF", name="Compress PDF", fn=tools.pdf_compress, exts={".pdf"}, pick="files",
         desc="Shrink PDFs by recompressing their images. Good for scans; text-only PDFs barely change.",
         options=[("quality", "int", "Image quality (1-95)", 60), ("max_px", "int", "Max image size (px)", 1600)]),
    dict(group="PDF", name="Watermark", fn=tools.pdf_watermark, exts={".pdf"}, pick="files",
         desc="Stamp diagonal text such as DRAFT or CONFIDENTIAL on every page.",
         options=[("text", "text", "Text", "DRAFT"), ("size", "int", "Font size", 60),
                  ("opacity", "text", "Opacity (0-1)", "0.15")]),
    dict(group="PDF", name="Page numbers", fn=tools.pdf_page_numbers, exts={".pdf"}, pick="files",
         desc="Add 'Page n of total' to every page.",
         options=[("format", "text", "Format", "Page {n} of {total}"),
                  ("position", "choice", "Position", "Bottom centre",
                   ["Bottom centre", "Bottom right", "Bottom left", "Top centre", "Top right", "Top left"]),
                  ("size", "int", "Font size", 10)]),
    dict(group="PDF", name="PDF → Word / Excel", fn=tools.pdf_to_office, exts={".pdf"}, pick="files",
         desc="Turn a text-based PDF back into an editable file (via LibreOffice). Scanned PDFs need OCR first.",
         options=[("format", "choice", "Convert to", "Word (.docx)", ["Word (.docx)", "Excel (.xlsx)", "PowerPoint (.pptx)"])]),
    # --- Images
    dict(group="Images", name="Convert / resize images", fn=tools.image_convert, exts=tools.IMAGE_EXTS, pick="files",
         desc="HEIC/PNG/WebP → JPG (or PNG/WebP), optionally resized.",
         options=[("format", "choice", "Format", "JPG", ["JPG", "PNG", "WEBP", "BMP", "TIFF"]),
                  ("max_px", "int", "Max width/height (0 = keep)", 0), ("quality", "int", "Quality (JPG/WebP)", 85)]),
    dict(group="Images", name="Shrink photos for email", fn=tools.image_compress, exts=tools.IMAGE_EXTS, pick="files",
         desc="Resize to 1600px and save as JPG at a modest quality.",
         options=[("max_px", "int", "Max width/height (px)", 1600), ("quality", "int", "Quality", 75)]),
    dict(group="Images", name="Images → one PDF", fn=tools.images_to_pdf, exts=tools.IMAGE_EXTS, pick="files",
         desc="Turn scans or photos into a single PDF, one image per page, in the order listed.",
         options=[("name", "text", "Output file name", "images")]),
    dict(group="Images", name="OCR (text from image / scan)", fn=tools.ocr, exts=tools.IMAGE_EXTS | {".pdf"}, pick="files",
         desc="Extract text from images, or make a searchable PDF. Needs Tesseract (winget install UB-Mannheim.TesseractOCR).",
         options=[("output", "choice", "Output", "Text file (.txt)", ["Text file (.txt)", "Searchable PDF"]),
                  ("lang", "text", "Language code", "eng")]),
    dict(group="Images", name="QR code", fn=tools.qr_code, exts={".txt"}, pick="files",
         desc="Make a QR code PNG from a URL or text (or from .txt files).",
         options=[("text", "text", "Text / URL", ""), ("size", "int", "Size (px)", 600)]),
    # --- Spreadsheets
    dict(group="Spreadsheets", name="Excel → CSV", fn=tools.excel_to_csv, exts={".xlsx", ".xlsm"}, pick="files",
         desc="Every sheet to its own CSV file.", options=[]),
    dict(group="Spreadsheets", name="CSV → Excel", fn=tools.csv_to_excel, exts={".csv", ".tsv"}, pick="files",
         desc="One workbook per CSV, or all CSVs as sheets of one workbook.",
         options=[("combine", "check", "Combine into one workbook (a sheet per file)", False),
                  ("name", "text", "Combined file name", "combined")]),
    # --- Files
    dict(group="Files", name="Bulk rename", fn=tools.bulk_rename, exts=None, pick="files", preview=True,
         desc="Rename many files at once. Press Preview first - it shows exactly what will change.",
         options=[("mode", "choice", "Mode", "Add prefix",
                   ["Add prefix", "Add suffix", "Replace text", "Number sequentially", "Prefix with file date",
                    "Lowercase", "Spaces to underscores"]),
                  ("text", "text", "Text / prefix / find", ""), ("replace", "text", "Replace with", ""),
                  ("start", "int", "Start number", 1)], no_outdir=True),
    dict(group="Files", name="Tidy a folder", fn=tools.tidy_folder, exts=None, pick="none", no_outdir=True,
         desc="Sort loose files in a folder (Downloads by default) into Images / Documents / Installers / … sub-folders.",
         options=[("folder", "folder", "Folder", str(Path.home() / "Downloads")),
                  ("older_days", "int", "Only files older than (days, 0 = all)", 0),
                  ("dry_run", "check", "Preview only (don't move anything)", True)]),
    dict(group="Files", name="Find duplicates", fn=tools.find_duplicates, exts=None, pick="folders", no_outdir=True,
         desc="Find identical files (by content) in the chosen folders. Writes a report; optionally moves extras to a '_duplicates' folder for review.",
         options=[("move", "check", "Move extras to '_duplicates' folder", False)]),
    dict(group="Files", name="Zip", fn=tools.zip_files, exts=None, pick="files",
         desc="Zip the selected files/folders. Add a password if you've installed pyzipper (pip install pyzipper).",
         options=[("name", "text", "Archive name", "archive"), ("password", "password", "Password (optional)", "")]),
    dict(group="Files", name="Unzip", fn=tools.unzip_files, exts={".zip"}, pick="files",
         desc="Extract each zip into a folder named after it.",
         options=[("password", "password", "Password (if any)", "")]),
    # --- Media
    dict(group="Media", name="Convert video / audio", fn=tools.media_convert, exts=tools.MEDIA_EXTS, pick="files",
         desc="To MP4, MP3, WAV, M4A or GIF. Needs ffmpeg (winget install Gyan.FFmpeg).",
         options=[("format", "choice", "Convert to", "MP4 (video)",
                   ["MP4 (video)", "MP3 (audio only)", "M4A (audio only)", "WAV (audio only)", "GIF (short clips)"])]),
    dict(group="Media", name="Trim video / audio", fn=tools.media_trim, exts=tools.MEDIA_EXTS, pick="files",
         desc="Cut a clip between two times without re-encoding. Needs ffmpeg.",
         options=[("start", "text", "Start (e.g. 0:30)", ""), ("end", "text", "End (e.g. 2:15)", "")]),
    # --- Text
    dict(group="Text", name="Clipboard / text cleaner", fn=None, exts=None, pick="none", text_tool=True,
         desc="Paste text, pick a transform, copy the result. Strips tracking from links, fixes case, dedupes lines…",
         options=[("mode", "choice", "Transform", "Clean URL (strip tracking)",
                   ["Clean URL (strip tracking)", "Plain text (strip formatting/extra spaces)", "UPPER CASE",
                    "lower case", "Title Case", "Sentence case", "Remove line breaks", "Remove blank lines",
                    "Sort lines", "Remove duplicate lines", "Count words"])]),
]


def tool_for_files(paths):
    exts = {Path(p).suffix.lower() for p in paths if Path(p).is_file()}
    office_to_pdf = next(t for t in TOOLS if t["fn"] is tools.office_to_pdf)
    if not exts or exts <= tools.OFFICE_EXTS:
        return office_to_pdf
    if exts == {".pdf"}:
        return next(t for t in TOOLS if t["fn"] is open_pdf_editor)
    for t in TOOLS:
        if t["exts"] and exts <= t["exts"]:
            return t
    return office_to_pdf


# ---------------------------------------------------------------------- GUI

class Toolkit(tk.Tk):
    def __init__(self, initial):
        super().__init__()
        self.title("Toolkit")
        self.geometry("960x620")
        self.minsize(820, 520)
        self.files: list[Path] = []
        self.vars: dict[str, tk.Variable] = {}
        self.tool = None
        self.busy = False
        self.last_outputs: list[Path] = []
        self._build()
        first = tool_for_files(initial)
        self.select_tool(first)
        self.add_paths(initial)

    # ---- layout
    def _build(self):
        style = ttk.Style(self)
        if IS_WIN:
            try:
                style.theme_use("vista")
            except tk.TclError:
                pass

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(paned, width=220)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.pack(fill="both", expand=True)
        for group in dict.fromkeys(t["group"] for t in TOOLS):
            gid = self.tree.insert("", "end", text=group, open=True)
            for t in (x for x in TOOLS if x["group"] == group):
                t["iid"] = self.tree.insert(gid, "end", text=t["name"])
        self.tree.bind("<<TreeviewSelect>>", self._on_tree)
        paned.add(left, weight=0)

        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        self.title_lbl = ttk.Label(right, font=("Segoe UI", 13, "bold"))
        self.title_lbl.pack(anchor="w")
        self.desc_lbl = ttk.Label(right, wraplength=640, foreground="#555")
        self.desc_lbl.pack(anchor="w", pady=(0, 6))

        # files
        self.files_frame = ttk.LabelFrame(right, text="Files")
        self.files_frame.pack(fill="both", expand=True)
        bar = ttk.Frame(self.files_frame); bar.pack(fill="x", padx=6, pady=4)
        self.add_btn = ttk.Button(bar, text="Add files…", command=self.pick); self.add_btn.pack(side="left")
        ttk.Button(bar, text="Add folder…", command=self.pick_folder).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=4)
        ttk.Button(bar, text="▲", width=3, command=lambda: self.move(-1)).pack(side="right")
        ttk.Button(bar, text="▼", width=3, command=lambda: self.move(1)).pack(side="right", padx=2)
        lf = ttk.Frame(self.files_frame); lf.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.listbox = tk.Listbox(lf, selectmode="extended", height=7)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview); self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")

        # text tool pane (hidden unless a text tool is selected)
        self.text_frame = ttk.LabelFrame(right, text="Text")
        tf = ttk.Frame(self.text_frame); tf.pack(fill="both", expand=True, padx=6, pady=6)
        self.text_in = tk.Text(tf, height=7, wrap="word"); self.text_in.pack(fill="both", expand=True)
        tb = ttk.Frame(tf); tb.pack(fill="x", pady=4)
        ttk.Button(tb, text="Paste", command=self.paste_in).pack(side="left")
        ttk.Button(tb, text="Transform ↓", command=self.transform).pack(side="left", padx=4)
        ttk.Button(tb, text="Copy result", command=self.copy_out).pack(side="left")
        self.text_out = tk.Text(tf, height=7, wrap="word", background="#f6f6f6"); self.text_out.pack(fill="both", expand=True)

        # options
        self.opts_frame = ttk.LabelFrame(right, text="Options")
        self.opts_frame.pack(fill="x", pady=6)

        # output folder
        self.out_frame = ttk.Frame(right); self.out_frame.pack(fill="x")
        self.same_folder = tk.BooleanVar(value=True)
        self.outdir = tk.StringVar(value="")
        ttk.Radiobutton(self.out_frame, text="Save next to each file", variable=self.same_folder, value=True,
                        command=self._toggle_out).pack(side="left")
        ttk.Radiobutton(self.out_frame, text="Save to:", variable=self.same_folder, value=False,
                        command=self._toggle_out).pack(side="left", padx=(10, 2))
        self.out_entry = ttk.Entry(self.out_frame, textvariable=self.outdir, state="disabled")
        self.out_entry.pack(side="left", fill="x", expand=True)
        self.out_btn = ttk.Button(self.out_frame, text="Browse…", command=self.pick_out, state="disabled")
        self.out_btn.pack(side="left", padx=4)

        # run + log
        rb = ttk.Frame(right); rb.pack(fill="x", pady=(8, 2))
        self.run_btn = ttk.Button(rb, text="Run", command=self.run); self.run_btn.pack(side="left")
        self.preview_btn = ttk.Button(rb, text="Preview", command=self.preview); self.preview_btn.pack(side="left", padx=4)
        self.open_btn = ttk.Button(rb, text="Open output folder", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left")
        self.progress = ttk.Progressbar(rb, mode="indeterminate", length=160); self.progress.pack(side="right")
        self.log_box = tk.Text(right, height=7, state="disabled", background="#1e1e1e", foreground="#d4d4d4",
                               font=("Consolas", 9))
        self.log_box.pack(fill="x")

    # ---- tool switching
    def _on_tree(self, _):
        sel = self.tree.selection()
        if not sel:
            return
        t = next((x for x in TOOLS if x.get("iid") == sel[0]), None)
        if t:
            self.select_tool(t)

    def select_tool(self, t):
        self.tool = t
        if self.tree.selection() != (t.get("iid"),) and t.get("iid"):
            self.tree.selection_set(t["iid"]); self.tree.see(t["iid"])
        self.title_lbl.configure(text=t["name"])
        self.desc_lbl.configure(text=t["desc"])
        # options
        for w in self.opts_frame.winfo_children():
            w.destroy()
        self.vars = {}
        for i, spec in enumerate(t["options"]):
            key, kind, label, default = spec[:4]
            ttk.Label(self.opts_frame, text=label).grid(row=i, column=0, sticky="w", padx=6, pady=2)
            if kind == "check":
                v = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(self.opts_frame, variable=v).grid(row=i, column=1, sticky="w")
            elif kind == "choice":
                v = tk.StringVar(value=default)
                ttk.Combobox(self.opts_frame, textvariable=v, values=spec[4], state="readonly", width=38
                             ).grid(row=i, column=1, sticky="w")
            elif kind == "folder":
                v = tk.StringVar(value=default)
                ttk.Entry(self.opts_frame, textvariable=v, width=48).grid(row=i, column=1, sticky="ew")
                ttk.Button(self.opts_frame, text="Browse…",
                           command=lambda v=v: v.set(filedialog.askdirectory() or v.get())).grid(row=i, column=2, padx=4)
            else:
                v = tk.StringVar(value=str(default))
                ttk.Entry(self.opts_frame, textvariable=v, width=40,
                          show="•" if kind == "password" else "").grid(row=i, column=1, sticky="w")
            self.vars[key] = v
        self.opts_frame.columnconfigure(1, weight=1)
        if not t["options"]:
            ttk.Label(self.opts_frame, text="No options - just add files and press Run.").grid(row=0, column=0, padx=6, pady=4)

        # panes
        text_tool = t.get("text_tool", False)
        self.files_frame.pack_forget(); self.text_frame.pack_forget()
        (self.text_frame if text_tool else self.files_frame).pack(fill="both", expand=True,
                                                                  before=self.opts_frame)
        if t.get("no_outdir") or text_tool:
            self.out_frame.pack_forget()
        else:
            self.out_frame.pack(fill="x", after=self.opts_frame)
        self.add_btn.configure(text="Add folders…" if t["pick"] == "folders" else "Add files…",
                               state="disabled" if t["pick"] == "none" else "normal")
        self.preview_btn.configure(state="normal" if t.get("preview") else "disabled")
        self.run_btn.configure(text="Transform" if text_tool else "Run")
        self._refresh_list()

    # ---- files
    def add_paths(self, paths):
        for p in paths:
            p = Path(p)
            if p.exists() and p not in self.files:
                self.files.append(p)
        self._refresh_list()

    def _refresh_list(self):
        self.listbox.delete(0, "end")
        exts = self.tool["exts"] if self.tool else None
        for f in self.files:
            ok = f.is_dir() or exts is None or f.suffix.lower() in exts
            self.listbox.insert("end", str(f))
            if not ok:
                self.listbox.itemconfig(self.listbox.size() - 1, fg="#999")  # greyed: this tool will skip it

    def pick(self):
        if self.tool["pick"] == "folders":
            return self.pick_folder()
        exts = self.tool["exts"]
        ft = [("Supported", " ".join("*" + e for e in sorted(exts))), ("All files", "*.*")] if exts else [("All files", "*.*")]
        self.add_paths(filedialog.askopenfilenames(filetypes=ft))

    def pick_folder(self):
        d = filedialog.askdirectory()
        if not d:
            return
        if self.tool["pick"] == "folders" or self.tool["exts"] is None:
            self.add_paths([d])
        else:
            rec = messagebox.askyesno("Sub-folders", "Include files in sub-folders too?")
            it = Path(d).rglob("*") if rec else Path(d).glob("*")
            self.add_paths(sorted(p for p in it if p.is_file() and p.suffix.lower() in self.tool["exts"]))

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            del self.files[i]
        self._refresh_list()

    def clear(self):
        self.files.clear(); self._refresh_list()

    def move(self, d):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        if d < 0 and sel[0] == 0 or d > 0 and sel[-1] == len(self.files) - 1:
            return
        for i in (sel if d < 0 else reversed(sel)):
            self.files[i], self.files[i + d] = self.files[i + d], self.files[i]
        self._refresh_list()
        for i in sel:
            self.listbox.selection_set(i + d)

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.outdir.set(d)

    def _toggle_out(self):
        st = "disabled" if self.same_folder.get() else "normal"
        self.out_entry.configure(state=st); self.out_btn.configure(state=st)

    # ---- text tool
    def paste_in(self):
        try:
            self.text_in.delete("1.0", "end"); self.text_in.insert("1.0", self.clipboard_get())
        except tk.TclError:
            pass

    def transform(self):
        src = self.text_in.get("1.0", "end-1c")
        out = tools.transform_text(src, self.vars["mode"].get())
        self.text_out.delete("1.0", "end"); self.text_out.insert("1.0", out)

    def copy_out(self):
        self.clipboard_clear(); self.clipboard_append(self.text_out.get("1.0", "end-1c"))
        self.log("Copied to clipboard.")

    # ---- running
    def opts(self):
        o = {k: v.get() for k, v in self.vars.items()}
        if not self.tool.get("no_outdir") and not self.same_folder.get():
            o["outdir"] = self.outdir.get()
        return o

    def log(self, msg):
        def _do():
            self.log_box.configure(state="normal"); self.log_box.insert("end", msg + "\n")
            self.log_box.see("end"); self.log_box.configure(state="disabled")
        self.after(0, _do)

    def preview(self):
        if not self.files:
            return messagebox.showinfo("Toolkit", "Add some files first.")
        pairs = tools.rename_preview(self.files, self.opts())
        self.log("--- preview ---")
        for old, new in pairs:
            self.log(f"{old.name}  ->  {new.name}" + ("   (no change)" if old == new else ""))

    def run(self):
        if self.tool.get("text_tool"):
            return self.transform()
        if self.busy:
            return
        if self.tool["pick"] != "none" and not self.files:
            return messagebox.showinfo("Toolkit", "Add some files first.")
        if not self.tool.get("no_outdir") and not self.same_folder.get() and not self.outdir.get():
            return messagebox.showinfo("Toolkit", "Choose an output folder.")
        self.busy = True
        self.run_btn.configure(state="disabled"); self.progress.start(12)
        self.log(f"=== {self.tool['name']} ===")
        threading.Thread(target=self._worker, args=(self.tool, list(self.files), self.opts()), daemon=True).start()

    def _worker(self, tool, files, opts):
        try:
            outputs = tool["fn"](files, opts, self.log) or []
            self.last_outputs = [Path(p) for p in outputs]
            self.log(f"Done - {len(outputs)} item(s) produced." if outputs else "Done.")
        except tools.ToolError as e:
            self.log(f"! {e}")
            self.after(0, messagebox.showwarning, tool["name"], str(e))
        except Exception as e:  # anything unexpected: show it, don't vanish
            import traceback
            self.log(traceback.format_exc())
            self.after(0, messagebox.showerror, tool["name"], f"Unexpected error:\n{e}")
        finally:
            self.after(0, self._finished)

    def _finished(self):
        self.busy = False
        self.run_btn.configure(state="normal"); self.progress.stop()
        self.open_btn.configure(state="normal" if self.last_outputs else "disabled")

    def open_output(self):
        if not self.last_outputs:
            return
        folder = self.last_outputs[0] if self.last_outputs[0].is_dir() else self.last_outputs[0].parent
        if IS_WIN:
            os.startfile(folder)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])


def main():
    missing = []
    for mod, pkg in (("pypdf", "pypdf"), ("PIL", "pillow"), ("openpyxl", "openpyxl"), ("reportlab", "reportlab")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        root = tk.Tk(); root.withdraw()
        if messagebox.askyesno("Toolkit - first run",
                               "Some Python libraries are missing:\n  " + ", ".join(missing) +
                               "\n\nInstall them now? (runs: pip install ...)"):
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", *missing],
                               capture_output=True, text=True)
            if r.returncode != 0:
                messagebox.showerror("Toolkit", "Install failed:\n" + r.stderr[-1500:]); return
        else:
            return
        root.destroy()
    Toolkit(sys.argv[1:]).mainloop()


if __name__ == "__main__":
    main()
