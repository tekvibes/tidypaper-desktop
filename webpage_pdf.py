"""A webpage -> an A4 PDF with selectable text (Batch 4 of the Folio features: "webpage").

The page is loaded in a closed-off browser view: nothing it stores is kept (no cookies, no history), it has no link
to the app (no js_api), and it isn't shown.  Then the browser's own printing makes the pages (the site's print look,
paginated to A4 with 0.5 inch margins).
- Mac: an offscreen WKWebView with a non-persistent data store + NSPrintOperation saved to a file (on the main
  thread; the caller waits).  `python webpage_pdf.py URL OUT.pdf` runs it with its own event loop (selftests).
- Windows: a hidden pywebview window (WebView2, no js_api) + CoreWebView2.PrintToPdfAsync; afterwards the browsing
  data is cleared.  The first try on Windows (4 Nov) failed; rewritten to watch the print task instead of a .NET
  callback.  Try it on its own: `py webpage_pdf.py https://www.bbc.co.uk/news out.pdf`.
"""

from __future__ import annotations

import platform
import sys
import threading
import time
from pathlib import Path
import logging

log = logging.getLogger("toolkit")

A4 = (595, 842)
MARGIN = 36
SETTLE = 1.2           # seconds after the page says it has loaded (late pictures, fonts)
TIMEOUT = 60


class WebpageError(Exception):
    pass


# Run in the page before it is printed: the "we use cookies" box a website shows a first-time visitor (which every
# visit here is - nothing is kept) is left out of the PDF.  It is only hidden: nothing is clicked, so nothing is
# agreed to.  Three ways in (the third: from an Accept or Reject button up to the box holding it): the boxes the common consent tools make, by name; and anything pinned to the window
# (position fixed or sticky), marked as a dialog or named for cookies, of modest length, that talks about cookies or
# consent AND offers a choice (accept, reject, manage...) with something to press - so an article about cookies keeps
# its pinned heading and its list of contents.
# -> how many things were hidden.
HIDE_COOKIE_BOXES = r"""
(function () {
  var hidden = 0;
  function hide(el) {
    if (!el || el === document.body || el === document.documentElement || el.dataset.tidyHidden) return;
    el.dataset.tidyHidden = "1";
    el.style.setProperty("display", "none", "important");
    hidden++;
  }
  var known = ["#onetrust-consent-sdk", "#onetrust-banner-sdk", "#CybotCookiebotDialog", "#CybotCookiebotDialogBodyUnderlay",
    "[id^='sp_message_container']", ".qc-cmp2-container", "#qc-cmp2-container", "#didomi-host", "#usercentrics-root",
    ".fc-consent-root", ".cc-window", ".cc-banner", "#cookie-law-info-bar", "#truste-consent-track", ".truste_overlay",
    ".truste_box_overlay", "#cmpbox", "#cmpbox2", ".osano-cm-window", "#cookiescript_injected", ".cky-consent-container",
    ".cky-overlay", "#iubenda-cs-banner", "#gdpr-cookie-message", "#cookie-notice", "#cookieConsent", "#cookie-consent",
    "#cookie-banner", "#cookiebanner", ".cookie-banner", ".cookie-consent", ".cookie-notice", "#cookiePrompt",
    "[data-testid='cookie-banner']", "[aria-label*='cookie' i][role='dialog']", "[aria-label*='consent' i][role='dialog']"];
  known.forEach(function (sel) {
    try { document.querySelectorAll(sel).forEach(hide); } catch (e) {}
  });
  var about = /\bcookies?\b|\bconsent\b|\bgdpr\b/i;
  var choice = /\b(accept|agree|reject|decline|allow|got it|manage|choose|preferences|opt[ -]?out|opt[ -]?in)\b/i;
  var all = document.body ? document.body.getElementsByTagName("*") : [];
  for (var i = 0; i < all.length; i++) {
    var el = all[i], tag = el.tagName;
    if (tag === "SCRIPT" || tag === "STYLE" || tag === "HEADER" || tag === "NAV" || tag === "MAIN" || tag === "ARTICLE") continue;
    var cs = getComputedStyle(el);
    if (cs.display === "none") continue;
    var pinned = cs.position === "fixed" || cs.position === "sticky";
    var dialog = /^(dialog|alertdialog)$/.test(el.getAttribute("role") || "") || tag === "DIALOG";
    var named = about.test((el.id || "") + " " + (typeof el.className === "string" ? el.className : "") + " " + (el.getAttribute("aria-label") || ""));
    if (!pinned && !dialog && !named) continue;
    var words = (el.innerText || "").trim();
    if (!words || words.length > 2500 || !about.test(words) || !choice.test(words)) continue;
    if (!el.querySelector("button, [role='button'], a, input[type='button'], input[type='submit']")) continue;
    hide(el);
  }
  // from a button that accepts or refuses, up to the box it is in: a box laid in the page itself, with no name to
  // go by (a narrow window gets these).  The box is the largest thing round the button that is still short and
  // still about cookies - never the page's own main parts.
  var press = /^(accept|reject|refuse|decline|allow|agree to)\b[\w\s]{0,30}$/i;
  var keep = /^(BODY|HTML|MAIN|ARTICLE|HEADER|NAV|FOOTER)$/;
  var buttons = document.querySelectorAll("button, [role='button'], a, input[type='button'], input[type='submit']");
  for (var b = 0; b < buttons.length; b++) {
    var label = (buttons[b].innerText || buttons[b].value || "").trim();
    if (!press.test(label)) continue;
    var box = null;
    for (var up = buttons[b].parentElement; up && !keep.test(up.tagName); up = up.parentElement) {
      var said = (up.innerText || "").trim();
      if (said.length > 1200) break;
      if (about.test(said)) box = up;
    }
    if (box && getComputedStyle(box).display !== "none") hide(box);
  }
  // a page that stops scrolling while its box is up
  [document.documentElement, document.body].forEach(function (el) {
    if (el && hidden && /hidden|clip/.test(getComputedStyle(el).overflow)) el.style.setProperty("overflow", "visible", "important");
  });
  return hidden;
})()
"""


def make_pdf(url: str, out: Path, timeout: float = TIMEOUT) -> Path:
    out = Path(out).resolve()                   # WebView2 wants the whole path ("test.pdf" alone: ArgumentException)
    out.parent.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Darwin":
        _mac(url, out, timeout)
    elif system == "Windows":
        _windows_guarded(url, out, timeout)
    else:
        raise WebpageError("Making a PDF of a webpage isn't available on this computer.")
    if not out.exists() or out.stat().st_size < 200:
        raise WebpageError("The webpage couldn't be turned into a PDF.")
    return out


# ------------------------------------------------------------------ Mac

def _mac(url: str, out: Path, timeout: float):
    import AppKit
    from PyObjCTools import AppHelper
    if threading.current_thread() is threading.main_thread():
        raise WebpageError("Call from a worker thread (the page is made on the main thread).")
    if not AppKit.NSApp() or not AppKit.NSApp().isRunning():
        raise WebpageError("Making a PDF of a webpage needs the app window.")
    job = _MacJob(url, out)
    AppHelper.callAfter(job.start)
    if not job.done.wait(timeout):
        AppHelper.callAfter(job.cancel)
        raise WebpageError("The webpage took too long to load.")
    if job.error:
        raise WebpageError(job.error)


class _MacJob:
    """Everything here runs on the main thread except `done.wait`."""

    def __init__(self, url: str, out: Path):
        self.url, self.out = url, out
        self.done = threading.Event()
        self.error = ""
        self.win = self.wv = self.nav = None

    def start(self):
        import AppKit
        import Foundation
        import WebKit
        try:
            cfg = WebKit.WKWebViewConfiguration.alloc().init()
            cfg.setWebsiteDataStore_(WebKit.WKWebsiteDataStore.nonPersistentDataStore())   # nothing kept
            rect = Foundation.NSMakeRect(-6000, -6000, 1024, 1400)
            self.win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
            self.win.setReleasedWhenClosed_(False)
            self.wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(Foundation.NSMakeRect(0, 0, 1024, 1400), cfg)
            self.nav = _nav_delegate()(self)
            self.wv.setNavigationDelegate_(self.nav)
            self.win.setContentView_(self.wv)
            req = Foundation.NSURLRequest.requestWithURL_cachePolicy_timeoutInterval_(
                Foundation.NSURL.URLWithString_(self.url), 1, 45)        # 1 = ignore any cached copy
            self.wv.loadRequest_(req)
        except Exception as e:  # pragma: no cover - shown to the person as a plain message
            self.fail(f"The webpage couldn't be opened ({e}).")

    def loaded(self):
        from PyObjCTools import AppHelper
        AppHelper.callLater(SETTLE, self.tidy_then_print)

    def tidy_then_print(self):
        """Hide the cookie box (HIDE_COOKIE_BOXES), then print.  Printing goes ahead whether or not that worked."""
        from PyObjCTools import AppHelper
        if self.done.is_set():
            return
        try:
            self.wv.evaluateJavaScript_completionHandler_(HIDE_COOKIE_BOXES, None)
        except Exception as e:  # pragma: no cover
            log.info("webpage pdf: cookie boxes not looked for (%s)", e)
        AppHelper.callLater(0.3, self.print_it)

    def print_it(self):
        import AppKit
        import Foundation
        if self.done.is_set():
            return
        try:
            pi = AppKit.NSPrintInfo.sharedPrintInfo().copy()
            pi.setPaperSize_(Foundation.NSMakeSize(*A4))
            for f in ("setTopMargin_", "setBottomMargin_", "setLeftMargin_", "setRightMargin_"):
                getattr(pi, f)(MARGIN)
            pi.setHorizontalPagination_(AppKit.NSPrintingPaginationModeFit)
            pi.setVerticalPagination_(AppKit.NSPrintingPaginationModeAutomatic)
            pi.setVerticallyCentered_(False)
            pi.setJobDisposition_(AppKit.NSPrintSaveJob)
            pi.dictionary()[AppKit.NSPrintJobSavingURL] = Foundation.NSURL.fileURLWithPath_(str(self.out))
            op = self.wv.printOperationWithPrintInfo_(pi)
            op.setShowsPrintPanel_(False)
            op.setShowsProgressPanel_(False)
            op.view().setFrame_(Foundation.NSMakeRect(0, 0, A4[0] - 2 * MARGIN, A4[1] - 2 * MARGIN))
            op.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(
                self.win, self.nav, b"printOperationDidRun:success:contextInfo:", None)
        except Exception as e:  # pragma: no cover
            self.fail(f"The webpage couldn't be printed to a PDF ({e}).")

    def printed(self, ok: bool):
        if not ok:
            self.error = "The webpage couldn't be printed to a PDF."
        self.finish()

    def fail(self, msg: str):
        self.error = self.error or msg
        self.finish()

    def cancel(self):
        if self.wv is not None:
            self.wv.stopLoading()
        self.error = self.error or "Stopped."
        self.finish()

    def finish(self):
        if self.done.is_set():
            return
        self.done.set()
        from PyObjCTools import AppHelper
        AppHelper.callLater(0.5, self.release)       # not inside the print operation's own callback

    def release(self):
        """Let go of the browser on the main thread (WebKit objects must not be freed on another thread)."""
        if self.wv is not None:
            self.wv.stopLoading()
            self.wv.setNavigationDelegate_(None)
        if self.win is not None:
            self.win.orderOut_(None)
            self.win.setContentView_(None)
        self.win = self.wv = self.nav = None


_NAV = None


def _nav_delegate():
    """The WKNavigationDelegate class (made once: Objective-C classes can't be defined twice)."""
    global _NAV
    if _NAV is not None:
        return _NAV
    import Foundation
    import objc

    class TidyWebpageNav(Foundation.NSObject):
        def initWithJob_(self, job):
            self = objc.super(TidyWebpageNav, self).init()
            if self is not None:
                self.job = job
            return self

        def webView_didFinishNavigation_(self, wv, nav):
            self.job.loaded()

        def webView_didFailProvisionalNavigation_withError_(self, wv, nav, err):
            self.job.fail(_load_message(err))

        def webView_didFailNavigation_withError_(self, wv, nav, err):
            self.job.fail(_load_message(err))

        @objc.typedSelector(b"v@:@Z^v")          # (NSPrintOperation *, BOOL, void *): crashes without it
        def printOperationDidRun_success_contextInfo_(self, op, ok, ctx):
            self.job.printed(bool(ok))

    _NAV = lambda job: TidyWebpageNav.alloc().initWithJob_(job)  # noqa: E731
    return _NAV


def _load_message(err) -> str:
    try:
        text = str(err.localizedDescription()).rstrip(". ")
    except Exception:
        text = ""
    return f"The webpage couldn't be loaded{': ' + text if text else ''}."


# ------------------------------------------------------------------ Windows

def _windows_guarded(url: str, out: Path, timeout: float):
    """_windows with an end to the waiting.  Loading and printing have their own limits, but making the hidden
    window, handing the print to the window's thread and closing the window wait on that thread with none - if it is
    stuck, the person would see "Making a PDF..." for ever (16 Nov: three minutes, nothing in the log).  So the work
    runs on a thread of its own and is given up on after both limits and a little more."""
    box: dict = {}

    def work():
        try:
            _windows(url, out, timeout)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=work, name="webpage-pdf", daemon=True)
    t.start()
    t.join(2 * timeout + SETTLE + 15)
    if t.is_alive():
        log.warning("webpage pdf: gave up waiting (last step: %s) for %s", _step.get("at", "?"), url)
        raise WebpageError("Making the PDF stopped answering. Close the app, open it again and try once more.")
    if "error" in box:
        raise box["error"]


_step: dict = {}          # where the Windows work has got to, for the log


def _at(step: str):
    _step["at"] = step
    log.info("webpage pdf: %s", step)


def _windows(url: str, out: Path, timeout: float):
    """WebView2 in a hidden pywebview window, printed with PrintToPdfAsync.  The print is started on the window's
    thread and then watched from here (a .NET callback made from Python didn't work: the first try on Windows,
    4 Nov, said only "couldn't be printed").  Why it failed goes in the log."""
    import webview
    _at("making the hidden window")
    win = webview.create_window("Making a PDF", url=url, hidden=True, width=1024, height=1400)
    _at("loading the page")
    loaded = threading.Event()
    win.events.loaded += lambda: loaded.set()
    try:
        if not loaded.wait(timeout):
            raise WebpageError("The webpage took too long to load.")
        time.sleep(SETTLE)
        _at("hiding cookie boxes")
        try:
            n = win.evaluate_js(HIDE_COOKIE_BOXES)
            if n:
                log.info("webpage pdf: %s cookie box(es) left out", n)
                time.sleep(0.3)                              # the page settles without them
        except Exception as e:                               # the page is printed as it is
            log.info("webpage pdf: cookie boxes not looked for (%s)", e)
        form = win.native                                    # the WinForms form holding the WebView2
        core = _find_core(form)
        if core is None:
            log.warning("webpage pdf: no CoreWebView2 on %r", form)
            raise WebpageError("Making a PDF of a webpage isn't available on this PC.")
        from System import Action  # pythonnet, as pywebview uses on Windows
        box: dict = {}

        def start():
            try:
                settings = None
                try:
                    settings = core.Environment.CreatePrintSettings()
                    settings.PageWidth, settings.PageHeight = A4[0] / 72, A4[1] / 72          # inches
                    settings.MarginTop = settings.MarginBottom = MARGIN / 72
                    settings.MarginLeft = settings.MarginRight = MARGIN / 72
                    settings.ShouldPrintBackgrounds = True
                except Exception as e:                       # an older WebView2: its own page size
                    log.warning("webpage pdf: print settings not available (%s: %s)", type(e).__name__, e)
                    settings = None
                box["task"] = core.PrintToPdfAsync(str(out), settings)
            except Exception as e:
                box["error"] = f"{type(e).__name__}: {e}"

        _at("starting the print")
        form.Invoke(Action(start))                           # waits until start() has run on the window's thread
        _at("printing")
        task = box.get("task")
        if task is None:
            log.warning("webpage pdf: PrintToPdfAsync didn't start: %s", box.get("error"))
            raise WebpageError("The webpage couldn't be printed to a PDF.")
        deadline = time.time() + timeout
        while not task.IsCompleted:
            if time.time() > deadline:
                raise WebpageError("The webpage took too long to print.")
            time.sleep(0.2)
        if task.IsFaulted or task.IsCanceled:
            log.warning("webpage pdf: PrintToPdfAsync failed: %s", task.Exception)
            raise WebpageError("The webpage couldn't be printed to a PDF.")
        if not task.Result:
            log.warning("webpage pdf: PrintToPdfAsync said no (out=%s)", out)
            raise WebpageError("The webpage couldn't be printed to a PDF.")
        try:
            form.Invoke(Action(lambda: core.Profile.ClearBrowsingDataAsync()))          # nothing kept
        except Exception as e:
            log.info("webpage pdf: browsing data not cleared (%s)", e)
    finally:
        _at("closing the hidden window")
        try:
            win.destroy()
        except Exception:
            pass
        _at("done")


def _find_core(form):
    for path in (("browser", "webview", "CoreWebView2"), ("webview", "CoreWebView2")):
        o = form
        try:
            for a in path:
                o = getattr(o, a)
            if o is not None:
                return o
        except Exception:
            continue
    return None


# ------------------------------------------------------------------ on its own (selftests): its own event loop

def _cli(url: str, out: str) -> int:
    if platform.system() == "Windows":
        return _cli_windows(url, out)
    import AppKit
    from PyObjCTools import AppHelper
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)
    job = _MacJob(url, Path(out))
    original = job.finish

    def finish():       # stopping the loop can end the process with status 0: report first (callers check the file)
        original()
        if job.error:
            print(job.error, file=sys.stderr)
            sys.stderr.flush()
        AppHelper.callLater(1.0, AppHelper.stopEventLoop)   # after the browser has been let go
    job.finish = finish
    AppHelper.callAfter(job.start)
    AppHelper.callLater(TIMEOUT, job.cancel)
    AppHelper.runEventLoop(installInterrupt=True)
    return 1 if job.error else 0


def _cli_windows(url: str, out: str) -> int:
    """`py webpage_pdf.py URL OUT.pdf` on Windows: a hidden window to hold pywebview's loop, then the same code as
    the app.  Prints what went wrong (and the log lines) so it can be tried on its own."""
    import webview
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    holder = webview.create_window("Making a PDF", html="<p>Making a PDF</p>", hidden=True)
    status = {"code": 1}

    def work():
        try:
            make_pdf(url, Path(out))
            print(f"OK - {out} ({Path(out).stat().st_size} bytes)")
            status["code"] = 0
        except Exception as e:
            print(f"FAILED - {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            holder.destroy()
    webview.start(work)
    return status["code"]


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1], sys.argv[2]))
