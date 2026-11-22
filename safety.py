"""The app's guards against hostile files, web pages and folders, in one place (selftest_security.py tests each).

    settle()            first thing: programs are never looked for in the working folder (the folder of the file that
                        was double-clicked - often Downloads), and the working folder becomes the home folder
    guard_webview()     before the first window: only the app's own page may talk to Python, only through tab_call;
                        the app's window stays on that page; no page can open a window, a browser or another program
    page_port()         the port the page is served on: never one another program is already answering on
    local_request()     the developer server answers only its own page
    open_target()       what "Open" may hand to the system: web pages, folders and documents - never programs
    keep_mark()         files out of a downloaded zip keep the "came from the internet" mark
"""

from __future__ import annotations

import os
import platform
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from bridge_util import UserError, log

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

PAGE_PORT = 42001               # pywebview's own: the same each time, so what the page keeps (localStorage) lasts

# what "Open" hands to the program the person has for it.  An allow-list: a kind not here is shown in its folder.
OPEN_KINDS = {".pdf", ".txt", ".text", ".md", ".log", ".csv", ".tsv", ".json", ".rtf", ".ics", ".zip",
              ".doc", ".docx", ".dotx", ".xls", ".xlsx", ".ppt", ".pptx", ".ppsx", ".odt", ".ods", ".odp",
              ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
              ".epub", ".fb2", ".mobi", ".cbz", ".xps", ".oxps",
              ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
# "folders" that a Mac starts as programs
MAC_PROGRAMS = {".app", ".action", ".bundle", ".pkg", ".mpkg", ".prefpane", ".workflow", ".plugin", ".saver"}


def settle():
    """Windows looks for a program named without its folder (powershell, winget, ffmpeg...) in the working folder
    before System32, and a program started from a file has that file's folder as its working folder: a powershell.exe
    left in Downloads would run.  The variable stops CreateProcess and shutil.which looking there (and is passed on to
    what the app starts); moving to the home folder is the second lock.  Call it after relative paths are resolved."""
    if IS_WIN:
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    try:
        os.chdir(Path.home())
    except OSError:
        log.warning("couldn't move to the home folder")


# ------------------------------------------------------------------ the window

# what pywebview's own scripts send besides the app's one call
_FROM_PAGE = {"tab_call", "pywebviewEventHandler", "pywebviewAsyncCallback"}


def origin_of(url) -> str:
    try:
        u = urlsplit(str(url or ""))
    except ValueError:
        return ""
    return f"{u.scheme}://{u.netloc}".lower() if u.scheme in ("http", "https") and u.netloc else ""


def is_app_window(window) -> bool:
    """The app's window is the one given the Bridge; the hidden one that prints a web page is given nothing."""
    return getattr(window, "_js_api", None) is not None


def home_origin(window) -> str:
    """The app's own origin: the first address its window was given.  real_url can't be asked each time - pywebview
    changes it to wherever load_url sends the window."""
    home = getattr(window, "_tidy_home", "")
    if not home:
        home = origin_of(getattr(window, "real_url", None))
        if home:
            window._tidy_home = home
    return home


def is_app_page(window, url) -> bool:
    if not is_app_window(window):
        return False
    home = home_origin(window)
    return bool(home) and origin_of(url) == home


def may_call(window, name) -> bool:
    """pywebview follows dots in a name through any attribute ("_secrets.get"), so only these exact names get in."""
    return is_app_window(window) and isinstance(name, str) and name in _FROM_PAGE


def _guard_calls(module):
    if getattr(module.js_bridge_call, "guarded", False):
        return
    real = module.js_bridge_call

    def call(window, name, param, value_id):
        if not may_call(window, name):
            log.warning("a page's call was refused: %s", str(name)[:80])
            return
        return real(window, name, param, value_id)

    call.guarded = True
    module.js_bridge_call = call


def guard_webview():
    """pywebview hands its bridge to whatever page a window is showing, and opens any address a page asks for with
    os.startfile.  Both are closed here, in pywebview's platform module, before a window is made."""
    if IS_WIN:
        from webview.platforms import edgechromium as edge
        _guard_calls(edge)
        cls = edge.EdgeChrome
        if getattr(cls, "guarded", False):
            return
        notify, navigate = cls.on_script_notify, cls.on_navigation_start

        def on_script_notify(self, sender, args):
            if not is_app_page(self.pywebview_window, args.Source):      # the sender's address, from WebView2 itself
                log.warning("a message from a page that isn't the app's was refused")
                return
            return notify(self, sender, args)

        def on_navigation_start(self, sender, args):
            w = self.pywebview_window
            if is_app_window(w) and home_origin(w) and not is_app_page(w, args.Uri):
                args.Cancel = True
                log.warning("the app's window was kept on its own page")
                return
            return navigate(self, sender, args)

        def on_new_window_request(self, sender, args):
            args.set_Handled(True)                       # never a window, a browser or a program: the app opens
            log.info("a page's new window was refused")  # links itself, through open_external

        cls.on_script_notify = on_script_notify
        cls.on_navigation_start = on_navigation_start
        cls.on_new_window_request = on_new_window_request
        cls.guarded = True
    elif IS_MAC:
        import webview
        from webview.platforms import cocoa
        _guard_calls(cocoa)
        webview.settings["ALLOW_DOWNLOADS"] = False


def page_port(wanted: int = PAGE_PORT) -> int:
    """pywebview doesn't notice when its port is taken: the window would show, and trust, the other program's page."""
    try:
        socket.create_connection(("127.0.0.1", wanted), timeout=0.4).close()
    except OSError:
        return wanted                    # nobody answers there
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log.warning("port %d is another program's: the page is served on %d this time", wanted, port)
    return port


def local_request(headers, port: int) -> bool:
    """The developer server's calls: only from its own page (or a program on this computer that names it).  Another
    website's page sends its Origin; a name pointed at 127.0.0.1 shows in Host."""
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    if (headers.get("Host") or "").lower() not in hosts:
        return False
    origin = headers.get("Origin")
    return origin is None or origin.lower() in tuple(f"http://{h}" for h in hosts)


# ------------------------------------------------------------------ files

def open_target(target) -> str:
    """What open_external may open, as the system is to be given it.  UserError for anything that could be a program."""
    t = str(target or "").strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", t):          # (two letters or more: not a drive)
        if re.match(r"^https?://[^\s\\]+$", t, re.I) and origin_of(t):
            return t
        raise UserError("Only web addresses, folders and documents can be opened from here.")
    if not t:
        raise UserError("There's nothing to open.")
    p = Path(t).absolute()
    if p.is_dir():
        if IS_MAC and p.suffix.lower() in MAC_PROGRAMS:
            raise UserError("That's a program, so it isn't opened from here. Use Show in folder.")
        return os.path.join(str(p), "")                    # with the slash: never "Letters.exe" for "Letters"
    if p.suffix.lower() not in OPEN_KINDS or ":" in str(p)[len(p.drive):]:
        raise UserError(f"{p.name} could be a program, so it isn't opened from here. Use Show in folder.")
    if not p.is_file():
        raise UserError(f"{p.name} isn't there any more.")
    return str(p)


def keep_mark(source: Path, files: list):
    """Copy a downloaded file's "from the internet" mark (Windows: Zone.Identifier; Mac: quarantine) to what came
    out of it, so SmartScreen, Gatekeeper and Office's Protected View still see it."""
    files = [Path(f) for f in files if Path(f).is_file()]
    if not files:
        return
    if IS_WIN:
        try:
            with open(f"{Path(source).absolute()}:Zone.Identifier", "rb") as m:
                mark = m.read(64 * 1024)
        except OSError:
            return                       # not from the internet
        for f in files:
            try:
                with open(f"{f.absolute()}:Zone.Identifier", "wb") as out:
                    out.write(mark)
            except OSError:
                log.info("couldn't mark %s as from the internet", f.name)
    elif IS_MAC:
        key = "com.apple.quarantine"
        r = subprocess.run(["/usr/bin/xattr", "-p", key, str(Path(source).absolute())], capture_output=True, text=True)
        if r.returncode != 0:
            return
        for k in range(0, len(files), 200):
            subprocess.run(["/usr/bin/xattr", "-w", key, r.stdout.strip(), *[str(f.absolute()) for f in files[k:k + 200]]],
                           capture_output=True)
