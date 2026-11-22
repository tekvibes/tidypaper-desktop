"""Microsoft Word / Excel / PowerPoint as converters, when they're installed (Windows only).

Office is driven through COM from a hidden PowerShell process: paths travel in environment variables (no quoting
problems), every dialog is switched off, and a document with a password fails instead of waiting for someone to
type it.  **Macros are switched off while a file is open** (AutomationSecurity 3): automated Office's own default
is to run every one without asking - so a .docm from an email would run its code just by being opened here.  If the person already has Word open, their documents are left alone and Word stays open.

    available(kind)            kind = "word" | "excel" | "powerpoint"
    to_pdf(src, out, kind)     raises OfficeError with a plain-English sentence
    pdf_to_docx(src, out)      Word's own PDF reflow
"""

from __future__ import annotations

import base64
import html
import logging
import os
import platform
import re
import subprocess
from pathlib import Path

IS_WIN = platform.system() == "Windows"
log = logging.getLogger("toolkit")
TIMEOUT = 180                    # seconds; a first Word start on a slow PC can take a while

WORD = {".docx", ".doc", ".docm", ".dotx", ".dot", ".odt", ".rtf", ".wpd", ".wps"}
EXCEL = {".xlsx", ".xls", ".xlsm", ".xlsb", ".ods", ".csv"}
POWERPOINT = {".pptx", ".ppt", ".pptm", ".ppsx", ".pps", ".odp"}
PROG_ID = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}
LABEL = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}


class OfficeError(Exception):
    pass


def kind_of(path) -> str | None:
    ext = Path(path).suffix.lower()
    return "word" if ext in WORD else "excel" if ext in EXCEL else "powerpoint" if ext in POWERPOINT else None


def available(kind: str) -> bool:
    if not IS_WIN or kind not in PROG_ID:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, PROG_ID[kind] + r"\CLSID"):
            return True
    except OSError:
        return False


def any_available() -> bool:
    return any(available(k) for k in PROG_ID)


# A wrong password makes Office fail instead of asking (unprotected files ignore it).
_COMMON = r"""
$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'
$src = $env:TP_SRC; $out = $env:TP_OUT; $nopw = 'tp-no-password'
function Fail($m) { [Console]::Error.WriteLine($m); exit 3 }
function NoMacros($a) {
  $script:was = $a.AutomationSecurity
  $a.AutomationSecurity = 3
  if ([int]$a.AutomationSecurity -ne 3) { Fail 'MACROS' }
}
function MacrosAsBefore($a) { if ($null -ne $script:was) { $a.AutomationSecurity = $script:was } }
"""

_SCRIPTS = {
    "word": _COMMON + r"""
$app = New-Object -ComObject Word.Application
try {
  NoMacros $app
  $app.Visible = $false; $app.DisplayAlerts = 0
  try { $doc = $app.Documents.Open($src, $false, $true, $false, $nopw, $nopw) }
  catch { if ("$_" -match 'password') { Fail 'PASSWORD' } else { throw } }
  try { $doc.ExportAsFixedFormat($out, 17) } finally { $doc.Close(0) }
} finally { MacrosAsBefore $app; if ($app.Documents.Count -eq 0) { $app.Quit() } }
""",
    "excel": _COMMON + r"""
$app = New-Object -ComObject Excel.Application
try {
  NoMacros $app
  $app.Visible = $false; $app.DisplayAlerts = $false
  $missing = [System.Reflection.Missing]::Value
  try { $wb = $app.Workbooks.Open($src, 0, $true, $missing, $nopw, $nopw) }
  catch { if ("$_" -match 'password') { Fail 'PASSWORD' } else { throw } }
  try { $wb.ExportAsFixedFormat(0, $out) } finally { $wb.Close($false) }
} finally { MacrosAsBefore $app; if ($app.Workbooks.Count -eq 0) { $app.Quit() } }
""",
    "powerpoint": _COMMON + r"""
$app = New-Object -ComObject PowerPoint.Application
try {
  NoMacros $app
  try { $p = $app.Presentations.Open($src + '::' + $nopw + '::', -1, 0, 0) }
  catch { if ("$_" -match 'password') { Fail 'PASSWORD' } else { throw } }
  try { $p.SaveAs($out, 32) } finally { $p.Close() }
} finally { MacrosAsBefore $app; if ($app.Presentations.Count -eq 0) { $app.Quit() } }
""",
    "pdf2docx": _COMMON + r"""
$app = New-Object -ComObject Word.Application
try {
  NoMacros $app
  $app.Visible = $false; $app.DisplayAlerts = 0
  $doc = $app.Documents.Open($src, $false, $false, $false, $nopw, $nopw)
  try { $doc.SaveAs2($out, 16) } finally { $doc.Close(0) }
} finally { MacrosAsBefore $app; if ($app.Documents.Count -eq 0) { $app.Quit() } }
""",
}


def _run(script: str, src: Path, out: Path, what: str):
    env = dict(os.environ, TP_SRC=str(Path(src).resolve()), TP_OUT=str(Path(out).resolve()))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
                           capture_output=True, text=True, env=env, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        raise OfficeError(f"{what} took too long to open {Path(src).name}. Close any {what} messages and try again.")
    err = _plain(r.stderr or "")
    if "PASSWORD" in err:
        raise OfficeError(f"{Path(src).name} is password-protected. Open it in {what}, remove the password "
                          "(File → Info → Protect), save it and try again.")
    if r.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size == 0:
        log.warning("%s could not convert %s: %s", what, Path(src).name, err[:500])
        raise OfficeError(f"{what} couldn't convert {Path(src).name}.")


def _plain(err: str) -> str:
    """PowerShell reports errors as CLIXML when its output is captured - keep just the words."""
    if "<Objs" in err:
        err = "\n".join(re.findall(r'<S S="Error">(.*?)</S>', err, re.S))
        err = html.unescape(err.replace("_x000D_", "").replace("_x000A_", "\n"))
    return err.strip()


def to_pdf(src, out, kind: str | None = None):
    kind = kind or kind_of(src)
    if kind not in PROG_ID or not available(kind):
        raise OfficeError(f"Microsoft {LABEL.get(kind, 'Office')} isn't installed.")
    _run(_SCRIPTS[kind], Path(src), Path(out), LABEL[kind])


def pdf_to_docx(src, out):
    if not available("word"):
        raise OfficeError("Microsoft Word isn't installed.")
    _run(_SCRIPTS["pdf2docx"], Path(src), Path(out), "Word")
