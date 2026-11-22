"""Scan a paper document -> PDF.  Mixed into app.Bridge.

Two ways in, one page tray:
  * the PC's scanner - Windows' own WIA scan dialog, driven by a tiny PowerShell script (no extra Python
    packages); it blocks, so it runs as a background job and the page polls  scan_status
  * the phone's camera - a small HTTP server on this PC's Wi-Fi address with an unguessable token in the
    URL (shown as a QR code).  The phone page uploads each photo; nothing on disk is ever served.

Pages live in a temp folder until  scan_make_pdf  writes  "Scan <date time>.pdf"  (Documents by default).
"""

from __future__ import annotations

import io
import json
import locale
import os
import platform
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge_util import UserError, data_url, fitz, here, log, safe, unique

IS_WIN = platform.system() == "Windows"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

MAX_UPLOAD = 25 << 20            # bytes per photo
DRAIN_CAP = 120 << 20            # oversized uploads up to this size are read and discarded so the "too big" reply arrives
IDLE_STOP = 20 * 60              # phone server stops after this long without a visit or upload
MAX_PAGES = 200
THUMB_PX = 220
PAGE_LONG_PX = 2200              # long side of a page image in the PDF (~200 dpi on A4)
LETTER_COUNTRIES = {"US", "CA", "MX", "PH", "CL", "CO", "VE", "GT", "CR", "PA", "DO", "PR", "SV", "NI", "HN",
                    "united states", "canada", "mexico", "philippines", "chile", "colombia", "venezuela",
                    "guatemala", "costa rica", "panama", "puerto rico", "dominican republic"}
PAPER = {"a4": (595.28, 841.89), "letter": (612.0, 792.0)}
PAGE_MARGIN = 18                 # points of white border around a fitted photo

try:  # iPhone photos
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass


# ------------------------------------------------------------------ the scanner (WIA via PowerShell)

WIA_NO_DEVICE = -2145320939      # 0x80210015 WIA_S_NO_DEVICE_AVAILABLE
WIA_BUSY = -2145320954           # 0x80210006 WIA_ERROR_BUSY
WIA_OFFLINE = -2145320955        # 0x80210005 WIA_ERROR_OFFLINE
WIA_PAPER = -2145320957          # 0x80210003 WIA_ERROR_PAPER_EMPTY
WIA_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"   # every WIA scanner can deliver BMP; Pillow converts


def wia_script(target: Path) -> str:
    """PowerShell that shows Windows' scan dialog and saves one page to  target.  Prints one status word."""
    t = str(target).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  $dlg = New-Object -ComObject WIA.CommonDialog\n"
        # DeviceType 1 = scanner, Intent 1 = colour, Bias 0 = best quality, AlwaysSelectDevice, UseCommonUI, CancelError
        f"  $img = $dlg.ShowAcquireImage(1, 1, 0, '{WIA_BMP}', $false, $true, $false)\n"
        "  if ($img -eq $null) { 'CANCELLED'; exit 0 }\n"
        f"  $img.SaveFile('{t}')\n"
        "  'OK'\n"
        "} catch {\n"
        "  $h = $_.Exception.HResult\n"
        "  if ($_.Exception.InnerException) { $h = $_.Exception.InnerException.HResult }\n"
        "  'ERROR ' + $h + ' ' + $_.Exception.Message\n"
        "}\n")


def parse_wia(stdout: str, returncode: int) -> tuple[str, str | None]:
    """-> (state, message)  state: ok | cancelled | nodevice | error"""
    lines = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    if last == "OK":
        return "ok", None
    if last == "CANCELLED":
        return "cancelled", None
    if last.startswith("ERROR"):
        parts = last.split(" ", 2)
        try:
            code = int(parts[1])
        except (IndexError, ValueError):
            code = 0
        detail = parts[2] if len(parts) > 2 else ""
        if code == WIA_NO_DEVICE or "no device" in detail.lower():
            return "nodevice", "No scanner was found. Check it's switched on and connected - or use your phone's camera instead."
        if code == WIA_BUSY:
            return "error", "The scanner is busy. Wait a moment and try again."
        if code == WIA_OFFLINE:
            return "error", "The scanner is switched off or disconnected. Switch it on and try again."
        if code == WIA_PAPER:
            return "error", "The scanner's document feeder is empty. Put the page in and try again."
        if "80040154" in detail or "class not registered" in detail.lower():
            return "error", "Windows' scanning feature isn't available on this PC - use your phone's camera instead."
        return "error", "The scanner didn't finish the page. Try again, or use your phone's camera instead."
    if returncode != 0:
        return "error", "The scanner didn't finish the page. Try again, or use your phone's camera instead."
    return "cancelled", None


# ------------------------------------------------------------------ image helpers

def _open_image(path: Path):
    from PIL import Image, ImageOps
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
    return im


def _paper_box(im):
    """Bounding box of a sheet of paper photographed on a darker surface - or None when it isn't clear.
    Deliberately conservative: only crops when the bright region is a solid block surrounded by darker edges."""
    from PIL import ImageFilter
    small = im.convert("L")
    small.thumbnail((360, 360))
    small = small.filter(ImageFilter.MedianFilter(5))
    w, h = small.size
    hist = small.histogram()
    total = w * h
    # Otsu threshold
    sum_all = sum(i * c for i, c in enumerate(hist))
    wb = sb = 0
    best, thr = -1.0, 128
    for t in range(256):
        wb += hist[t]
        if wb == 0:
            continue
        wf = total - wb
        if wf == 0:
            break
        sb += t * hist[t]
        mb, mf = sb / wb, (sum_all - sb) / wf
        between = wb * wf * (mb - mf) ** 2
        if between > best:
            best, thr = between, t
    if thr > 215 or thr < 60:            # no clear paper/background split
        return None
    mask = small.point(lambda v: 255 if v > thr else 0)
    px = mask.load()

    def bright_frac(x0, y0, x1, y1):
        n = s = 0
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                n += 1
                s += px[x, y] > 0
        return s / n if n else 0

    # trim rows/columns from each side while they're mostly dark
    def edge(rng, along, is_row):
        for k in rng:
            line = [px[i, k] if is_row else px[k, i] for i in range(0, along, 2)]
            if sum(v > 0 for v in line) / len(line) > 0.5:
                return k
        return None
    top = edge(range(h), w, True)
    bottom = edge(range(h - 1, -1, -1), w, True)
    left = edge(range(w), h, False)
    right = edge(range(w - 1, -1, -1), h, False)
    if None in (top, bottom, left, right) or right - left < w * 0.4 or bottom - top < h * 0.4:
        return None
    area = (right - left) * (bottom - top) / total
    if not 0.3 < area < 0.95:            # paper fills (almost) everything already, or it's a tiny scrap
        return None
    if bright_frac(left, top, right, bottom) < 0.8:
        return None
    outside_n = outside_s = 0
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            if left <= x <= right and top <= y <= bottom:
                continue
            outside_n += 1
            outside_s += px[x, y] > 0
    if outside_n and outside_s / outside_n > 0.35:
        return None
    sx, sy = im.width / w, im.height / h
    pad = 2
    return (max(0, int((left + pad) * sx)), max(0, int((top + pad) * sy)),
            min(im.width, int((right + 1 - pad) * sx)), min(im.height, int((bottom + 1 - pad) * sy)))


def clean_page(im):
    """'Like a scan': crop to the paper (if obvious), greyscale, even out the paper to white, crisp text."""
    from PIL import ImageFilter, ImageOps
    box = _paper_box(im)
    if box:
        im = im.crop(box)
    g = ImageOps.autocontrast(im.convert("L"), cutoff=(1, 2))
    # push the light paper tone to white and keep the ink: a gentle levels curve
    hist = g.histogram()
    total = sum(hist)
    acc, paper = 0, 255
    for v in range(255, -1, -1):        # the tone that 40% of the page is lighter than = paper
        acc += hist[v]
        if acc >= total * 0.4:
            paper = v
            break
    white = max(150, min(250, paper - 8))
    g = g.point(lambda v: 255 if v >= white else int(v * 255 / white))
    return g.filter(ImageFilter.UnsharpMask(radius=1.4, percent=60, threshold=3))


def default_paper() -> str:
    """A4 for most of the world, Letter for North America and a few others (from Windows' region)."""
    names = []
    try:
        names += [locale.getlocale()[0] or ""]
    except Exception:
        pass
    try:
        if IS_WIN:
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetLocaleInfoW(0x0400, 0x5A, buf, 85):     # LOCALE_SISO3166CTRYNAME
                names.append(buf.value)
    except Exception:
        pass
    for n in names:
        n = n or ""
        cc = n.split("_")[-1].split(".")[0] if "_" in n else n
        if cc.upper() in LETTER_COUNTRIES or cc.lower() in LETTER_COUNTRIES:
            return "letter"
    return "a4"


# ------------------------------------------------------------------ session state

class _Page:
    def __init__(self, path: Path, source: str):
        self.id = uuid.uuid4().hex[:10]
        self.path = path
        self.source = source
        self.rot = 0
        im = _open_image(path)
        self.w, self.h = im.size
        im.close()
        self._thumb = {}

    def thumb(self) -> str:
        if self.rot not in self._thumb:
            from PIL import Image
            im = _open_image(self.path)
            im.thumbnail((THUMB_PX, THUMB_PX))
            if self.rot:
                im = im.rotate(-self.rot, expand=True, resample=Image.BICUBIC)
            bio = io.BytesIO(); im.convert("RGB").save(bio, "JPEG", quality=78)
            self._thumb[self.rot] = data_url(bio.getvalue(), "image/jpeg")
        return self._thumb[self.rot]

    def info(self, with_thumb: bool):
        d = {"id": self.id, "rot": self.rot, "source": self.source, "key": f"{self.id}:{self.rot}",
             "landscape": (self.w > self.h) != (self.rot in (90, 270))}
        if with_thumb:
            d["thumb"] = self.thumb()
        return d


class _Session:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="toolkit_scan_"))
        self.pages: list[_Page] = []
        self.lock = threading.RLock()
        self.scanner = {"state": "idle", "error": None}
        self.phone: _PhoneServer | None = None
        self.phone_error: str | None = None
        self.notice: str | None = None

    def add(self, path: Path, source: str) -> _Page:
        with self.lock:
            if len(self.pages) >= MAX_PAGES:
                raise UserError(f"That's {MAX_PAGES} pages already - make this PDF first, then scan the rest.")
            p = _Page(path, source)
            self.pages.append(p)
            return p

    def close(self):
        if self.phone:
            self.phone.stop()
            self.phone = None
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------ phone upload server

PHONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send photos to Toolkit</title>
<style>
:root { color-scheme: light dark; --bg:#f4f3f0; --card:#fff; --text:#1f2328; --muted:#6b7280; --accent:#111827; --accent-text:#fff; --ok:#166534; --bad:#b91c1c; }
@media (prefers-color-scheme: dark) { :root { --bg:#17181a; --card:#222326; --text:#e8e8ea; --muted:#9a9ca4; --accent:#f3f4f6; --accent-text:#111827; --ok:#86efac; --bad:#f87171; } }
* { box-sizing: border-box; }
body { margin:0; font: 17px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
main { max-width: 480px; margin: 0 auto; padding: 28px 20px 40px; }
h1 { font-size: 24px; margin: 0 0 6px; letter-spacing: -.01em; }
p { margin: 0 0 18px; color: var(--muted); }
.big { display:flex; align-items:center; justify-content:center; gap:10px; width:100%; min-height:64px; border:0; border-radius:16px;
       background:var(--accent); color:var(--accent-text); font-family: inherit; font-size: 19px; font-weight: 600; line-height: 1.2;
       cursor:pointer; padding: 0 16px; text-align: center; }
.big.secondary { background: var(--card); color: var(--text); border: 1.5px solid rgba(127,127,127,.3); margin-top: 12px; }
.big svg { width:24px; height:24px; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
input[type=file] { display:none; }
.status { margin-top: 22px; padding: 16px; border-radius: 14px; background: var(--card); min-height: 56px; }
.status strong { display:block; font-size: 18px; }
.ok { color: var(--ok); } .bad { color: var(--bad); }
.row { display:flex; gap:8px; flex-wrap:wrap; margin-top: 12px; }
.row img { width: 64px; height: 64px; object-fit: cover; border-radius: 8px; }
.tip { font-size: 14px; margin-top: 18px; }
</style></head>
<body><main>
<h1>Send photos to Toolkit</h1>
<p>Lay the page flat in good light, fill the screen with it, and take a photo. Each photo appears on the PC straight away.</p>
<label class="big" for="cam"><svg viewBox="0 0 24 24"><path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></svg>Take a photo</label>
<input id="cam" type="file" accept="image/*" capture="environment">
<label class="big secondary" for="pick">Choose photos already taken</label>
<input id="pick" type="file" accept="image/*" multiple>
<div class="status" id="status" aria-live="polite"><strong>Ready</strong><span id="detail">Nothing sent yet.</span></div>
<div class="row" id="row"></div>
<p class="tip">When you've sent every page, go back to the PC and press <b>Make PDF</b>.</p>
</main>
<script>
const base = location.pathname.replace(/\\/$/, "");
let sent = 0;
const st = (title, detail, cls) => { const s = document.getElementById("status");
  s.firstElementChild.textContent = title; s.firstElementChild.className = cls || ""; document.getElementById("detail").textContent = detail; };
async function send(files) {
  for (const f of files) {
    if (!f.type.startsWith("image/")) { st("Not a photo", f.name + " isn't a picture, so it wasn't sent.", "bad"); continue; }
    st("Sending…", "Keep this page open until it says Sent.", "");
    try {
      const r = await fetch(base + "/upload", { method: "POST", body: f, headers: { "Content-Type": f.type || "image/jpeg", "X-File-Name": encodeURIComponent(f.name || "photo.jpg") } });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { st("Not sent", j.error || "The PC didn't accept that photo.", "bad"); continue; }
      sent++;
      st("Sent", sent + (sent === 1 ? " photo" : " photos") + " on the PC so far. Take the next page, or go back to the PC.", "ok");
      const img = document.createElement("img"); img.src = URL.createObjectURL(f); img.alt = "Page " + sent;
      document.getElementById("row").append(img);
    } catch (e) { st("Not sent", "This phone lost touch with the PC. Check you're on the same Wi-Fi and the scan window is still open.", "bad"); }
  }
}
for (const id of ["cam", "pick"]) document.getElementById(id).addEventListener("change", e => { send([...e.target.files]); e.target.value = ""; });
</script></body></html>
"""

IMAGE_TYPES = ("image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/gif", "image/bmp", "image/tiff")


def lan_ip() -> str | None:
    """This PC's address on the local network (no packets are sent)."""
    for probe in ("192.168.1.1", "10.255.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 1))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
        except OSError:
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


class _PhoneServer:
    def __init__(self, session: _Session, host: str | None = None):
        self.session = session
        self.token = secrets.token_urlsafe(18)
        self.host = host or lan_ip()
        if not self.host:
            raise UserError("This PC doesn't seem to be on a Wi-Fi or home network, so the phone can't reach it.")
        self.last = time.time()
        self.uploads = 0
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj).encode())

            def _route(self):
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if len(parts) < 2 or parts[0] != "s" or not secrets.compare_digest(parts[1], server.token):
                    return None
                return "/".join(parts[2:])

            def do_GET(self):
                r = self._route()
                if r is None:
                    return self._json(403, {"error": "This link has expired. Scan the code on the PC again."})
                if r == "":
                    server.last = time.time()
                    return self._send(200, here(PHONE_PAGE).encode(), "text/html; charset=utf-8")
                self._json(404, {"error": "Not found."})

            def do_POST(self):
                r = self._route()
                if r is None:
                    self._drain()
                    return self._json(403, {"error": "This link has expired. Scan the code on the PC again."})
                if r != "upload":
                    self._drain()
                    return self._json(404, {"error": "Not found."})
                try:
                    n = int(self.headers.get("Content-Length") or -1)
                except ValueError:
                    n = -1
                if n < 0:
                    self.close_connection = True
                    return self._json(411, {"error": "The phone didn't say how big the photo is."})
                if n > MAX_UPLOAD:
                    # read (and throw away) the upload first: replying mid-upload makes Windows reset the connection,
                    # and the phone would show a network error instead of this message
                    if n <= DRAIN_CAP:
                        left = n
                        while left > 0:
                            chunk = self.rfile.read(min(left, 1 << 20))
                            if not chunk:
                                break
                            left -= len(chunk)
                    self.close_connection = True
                    return self._json(413, {"error": "That photo is too big (over 25 MB)."})
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in IMAGE_TYPES:
                    self._drain(n)
                    return self._json(415, {"error": "Only photos can be sent."})
                data = self.rfile.read(n)
                try:
                    from PIL import Image
                    with Image.open(io.BytesIO(data)) as im:
                        im.verify()
                    ext = {"image/png": ".png", "image/heic": ".heic", "image/heif": ".heif", "image/webp": ".webp",
                           "image/gif": ".gif", "image/bmp": ".bmp", "image/tiff": ".tif"}.get(ctype, ".jpg")
                    target = server.session.dir / f"phone_{uuid.uuid4().hex[:8]}{ext}"
                    target.write_bytes(data)
                    server.session.add(target, "phone")
                except UserError as e:
                    return self._json(409, {"error": str(e)})
                except Exception:
                    return self._json(415, {"error": "That file isn't a photo the PC can read."})
                server.last = time.time()
                server.uploads += 1
                self._json(200, {"ok": True, "pages": len(server.session.pages)})

            def _drain(self, n=None):
                try:
                    n = int(self.headers.get("Content-Length") or 0) if n is None else n
                    if 0 < n <= MAX_UPLOAD:
                        self.rfile.read(n)
                    else:
                        self.close_connection = True
                except Exception:
                    self.close_connection = True

        self.httpd = ThreadingHTTPServer((self.host, 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://{self.host}:{self.port}/s/{self.token}/"
        self.running = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while self.running:
            time.sleep(2)
            if time.time() - self.last > IDLE_STOP:
                self.session.notice = "The phone connection was closed after 20 minutes without photos. Show the code again to carry on."
                self.stop()

    def stop(self):
        if not getattr(self, "running", False):
            return
        self.running = False
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        if self.session.phone is self:
            self.session.phone = None


def _qr_data_url(text: str) -> str:
    import tools
    tmp = Path(tempfile.mkdtemp(prefix="toolkit_qr_")) / "qr.png"
    try:
        tools.make_qr(text, tmp, 360)
        return data_url(tmp.read_bytes(), "image/png")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


# ------------------------------------------------------------------ the mixin

def _documents() -> Path:
    for p in (Path.home() / "Documents", Path.home() / "OneDrive" / "Documents", Path.home()):
        if p.is_dir():
            return p
    return Path.home()


class ScanMixin:
    """Bridge methods for "Scan a paper document"."""

    def _scan_session(self, create=True) -> _Session:
        s = getattr(self, "_scan", None)
        if s is None and create:
            s = self._scan = _Session()
        if s is None:
            raise UserError("Start a scan first.")
        return s

    @safe
    def scan_start(self):
        """A fresh session (any previous one is discarded)."""
        old = getattr(self, "_scan", None)
        if old:
            old.close()
        self._scan = _Session()
        return {"ok": True, "paper": default_paper(), "folder": str(_documents())}

    @safe
    def scan_stop(self):
        s = getattr(self, "_scan", None)
        if s:
            s.close()
        self._scan = None
        return {"ok": True}

    @safe
    def scan_device(self):
        """Show Windows' scan dialog in the background; the page polls scan_status."""
        s = self._scan_session()
        if not IS_WIN:
            raise UserError("Scanning from a scanner works on Windows only - use your phone's camera instead.")
        with s.lock:
            if s.scanner["state"] == "running":
                return {"ok": True}
            s.scanner = {"state": "running", "error": None}
        threading.Thread(target=self._scan_run_wia, args=(s,), daemon=True).start()
        return {"ok": True}

    def _scan_run_wia(self, s: _Session):
        target = s.dir / f"scan_{uuid.uuid4().hex[:8]}.bmp"
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", wia_script(target)],
                               capture_output=True, text=True, timeout=900, creationflags=NO_WINDOW)
            state, msg = parse_wia(r.stdout, r.returncode)
            if state == "ok" and target.exists():
                png = target.with_suffix(".png")
                _open_image(target).save(png, "PNG")        # BMPs are huge; keep a PNG
                target.unlink(missing_ok=True)
                s.add(png, "scanner")
                s.scanner = {"state": "done", "error": None}
            elif state == "ok":
                s.scanner = {"state": "error", "error": "The scanner didn't send a page. Try again."}
            else:
                s.scanner = {"state": state, "error": msg}
            if state not in ("ok", "cancelled"):
                log.info("scanner: %s %s", state, (r.stdout or "").strip()[-300:])
        except subprocess.TimeoutExpired:
            s.scanner = {"state": "error", "error": "The scanner took too long. Try again."}
        except UserError as e:
            s.scanner = {"state": "error", "error": str(e)}
        except Exception:
            log.exception("scanner failed")
            s.scanner = {"state": "error", "error": "The scanner didn't finish the page. Try again, or use your phone's camera instead."}

    @safe
    def scan_phone_start(self):
        s = self._scan_session()
        if s.phone and s.phone.running:
            p = s.phone
        else:
            p = s.phone = _PhoneServer(s)
            log.info("phone scan server on %s:%s", p.host, p.port)
        s.notice = None
        return {"url": p.url, "address": f"{p.host}:{p.port}", "qr": _qr_data_url(p.url)}

    @safe
    def scan_phone_stop(self):
        s = getattr(self, "_scan", None)
        if s and s.phone:
            s.phone.stop()
        return {"ok": True}

    @safe
    def scan_status(self, known=None):
        """Pages in order (thumbnails only for ones the page hasn't seen), scanner and phone state."""
        s = self._scan_session(create=False)
        known = set(known or [])
        with s.lock:
            pages = [p.info(f"{p.id}:{p.rot}" not in known) for p in s.pages]
        phone = s.phone
        out = {"pages": pages, "scanner": dict(s.scanner),
               "phone": {"running": bool(phone and phone.running), "uploads": phone.uploads if phone else 0}}
        if s.notice:
            out["notice"] = s.notice
        if s.scanner["state"] in ("done", "cancelled", "nodevice", "error"):
            s.scanner = {"state": "idle", "error": None}      # report once
        return out

    @safe
    def scan_page(self, op: str, page_id: str, arg=None):
        s = self._scan_session(create=False)
        with s.lock:
            k = next((n for n, p in enumerate(s.pages) if p.id == page_id), None)
            if k is None:
                raise UserError("That page isn't in the tray any more.")
            if op == "rotate":
                s.pages[k].rot = (s.pages[k].rot + (90 if int(arg or 90) > 0 else 270)) % 360
            elif op == "delete":
                p = s.pages.pop(k)
                p.path.unlink(missing_ok=True)
            elif op == "move":
                to = max(0, min(int(arg), len(s.pages) - 1))
                s.pages.insert(to, s.pages.pop(k))
            else:
                raise UserError("Unknown page action.")
        return self.scan_status([f"{p.id}:{p.rot}" for p in s.pages])

    @safe
    def scan_make_pdf(self, opts=None):
        """opts: clean (bool, default True), paper (auto|a4|letter|image), folder (default Documents)"""
        from PIL import Image
        opts = opts or {}
        s = self._scan_session(create=False)
        with s.lock:
            pages = list(s.pages)
        if not pages:
            raise UserError("Scan or photograph at least one page first.")
        clean = opts.get("clean", True)
        paper = opts.get("paper") or "auto"
        if paper == "auto":
            paper = default_paper()
        folder = Path(opts.get("folder") or _documents())
        if not folder.is_dir():
            raise UserError("That folder doesn't exist any more - choose another.")
        doc = fitz.open()
        try:
            for p in pages:
                im = _open_image(p.path)
                if p.rot:
                    im = im.rotate(-p.rot, expand=True, resample=Image.BICUBIC)
                if clean:
                    im = clean_page(im)
                im.thumbnail((PAGE_LONG_PX, PAGE_LONG_PX), Image.LANCZOS)
                bio = io.BytesIO()
                im.save(bio, "JPEG", quality=82, optimize=True)
                iw, ih = im.size
                if paper == "image":
                    pw, ph = iw * 72 / 200, ih * 72 / 200          # 200 dpi
                    page = doc.new_page(width=pw, height=ph)
                    page.insert_image(page.rect, stream=bio.getvalue())
                else:
                    pw, ph = PAPER.get(paper, PAPER["a4"])
                    if iw > ih:
                        pw, ph = ph, pw
                    page = doc.new_page(width=pw, height=ph)
                    m = PAGE_MARGIN if not clean else PAGE_MARGIN / 2
                    page.insert_image(fitz.Rect(m, m, pw - m, ph - m), stream=bio.getvalue(), keep_proportion=True)
            target = unique(folder / f"Scan {datetime.now():%Y-%m-%d %H.%M}.pdf")
            tmp = target.with_suffix(".pdf.tmp")
            doc.save(str(tmp), garbage=3, deflate=True)
            os.replace(tmp, target)
        finally:
            doc.close()
        log.info("scan -> %s (%d pages)", target, len(pages))
        n = len(pages)
        return {"path": str(target), "name": target.name, "folder": str(folder), "pages": n,
                "status": f"Saved {n} page{'s' if n != 1 else ''} as {target.name}."}
