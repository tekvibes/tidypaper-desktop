"""Open things from the web (Batch 4 of the Folio features): a link to a PDF is downloaded and opened; a link to a
webpage can become a PDF (webpage_pdf.py).

Rules (the iPad's WebLink.swift follows the same ones):
- only web addresses (http / https); "example.com/x.pdf" means https://example.com/x.pdf;
- an http:// link is tried as https:// first (the desktop falls back to http if there's no secure version);
- an address on your own network (this computer, a router, 192.168..., a printer's page) is only opened after asking -
  every redirect is checked too, so a public link can't quietly lead there;
- at most 100 MB; a file is a PDF only if it starts with %PDF, whatever it's called;
- the PDF is saved in Downloads (never over another file), named from the server's file name, else the link.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

import brand
from bridge_util import UserError, unique

MAX_BYTES = 100 << 20
TIMEOUT = 30
PEEK = 256 << 10            # how much of a webpage is read to find its title


class PrivateAddress(Exception):
    """The link leads to an address on this computer or its network: ask first."""

    def __init__(self, host: str):
        super().__init__(host)
        self.host = host


def normalise(text: str) -> str:
    """What was pasted -> a web address, or a plain-English UserError."""
    t = (text or "").strip().strip("<>\"'")
    if not t:
        raise UserError("Paste a web address first.")
    if re.match(r"^[a-z][a-z0-9+.-]*:", t, re.I) and not re.match(r"^https?://", t, re.I):
        if re.match(r"^[a-z0-9.-]+:\d+", t, re.I):          # "example.com:8080/x" is a host and port
            t = "https://" + t
        else:
            raise UserError("Only web addresses (starting https://) can be opened this way.")
    if not re.match(r"^https?://", t, re.I):
        t = "https://" + t
    p = urllib.parse.urlsplit(t)
    if not p.hostname or " " in p.netloc or "." not in p.hostname and p.hostname != "localhost" and not _is_ip(p.hostname):
        raise UserError("That doesn't look like a web address - check it and try again.")
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc, p.path or "/", p.query, ""))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_private(host: str) -> bool:
    """True for this computer and its own network (and anything that can't be looked up as public)."""
    h = (host or "").strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h.endswith(".lan") or h.endswith(".home"):
        return True
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(h, None)}
    except OSError:
        return False                     # can't be looked up: the download fails with its own message
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


class _Checked(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but stop at one that leads onto this computer's own network."""

    def __init__(self, allow_private: bool):
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if not self.allow_private and is_private(host):
            raise PrivateAddress(host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, allow_private: bool):
    host = urllib.parse.urlsplit(url).hostname or ""
    if not allow_private and is_private(host):
        raise PrivateAddress(host)
    opener = urllib.request.build_opener(_Checked(allow_private))
    req = urllib.request.Request(url, headers={"User-Agent": f"{brand.NAME}/{brand.VERSION} (+PDF reader)",
                                               "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5"})
    return opener.open(req, timeout=TIMEOUT)


def _filename(resp, url: str) -> str:
    cd = resp.headers.get("Content-Disposition") or ""
    name = ""
    if cd:
        m = Message()
        m["content-disposition"] = cd
        name = m.get_filename() or ""
    if not name:
        name = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip(" .")
    if not name:
        name = urllib.parse.urlsplit(url).hostname or "Download"
    if not name.lower().endswith(".pdf"):
        name = Path(name).stem + ".pdf" if Path(name).suffix.lower() in (".php", ".aspx", ".asp", ".jsp", ".cgi", ".html", ".htm") else name + ".pdf"
    return name[:150]


def page_title(html: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return ""
    import html as h
    t = h.unescape(m.group(1).decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", t).strip()[:120]


def downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    if not d.is_dir():
        d = Path.home() / "Documents"
    if not d.is_dir():
        d = Path.home()
    return d


def fetch(text: str, dest_dir: Path | None = None, allow_private: bool = False, max_bytes: int = MAX_BYTES) -> dict:
    """Look at what a link is.  A PDF is downloaded -> {"kind": "pdf", "path", "url"}; a webpage ->
    {"kind": "webpage", "url", "title"}.  Raises UserError (or PrivateAddress: ask, then call again)."""
    url = normalise(text)
    tries = [url]
    if url.startswith("http://"):
        tries = ["https://" + url[len("http://"):], url]      # the secure version first
    resp, last = None, None
    for u in tries:
        try:
            resp = _open(u, allow_private)
            break
        except PrivateAddress:
            raise
        except urllib.error.HTTPError as e:
            last = e
            if u != tries[-1] and e.code >= 500:
                continue
            raise UserError(_http_message(e.code)) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            last = e
            continue
    if resp is None:
        reason = getattr(last, "reason", last)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
            raise UserError("The website didn't answer in time - try again later.")
        if isinstance(reason, socket.gaierror):
            raise UserError("That website can't be found - check the address, and that this computer is online.")
        raise UserError("That link couldn't be opened - check the address, and that this computer is online.")
    with resp:
        final = resp.geturl()
        length = int(resp.headers.get("Content-Length") or 0)
        ctype = (resp.headers.get_content_type() or "").lower()
        head = resp.read(1024)
        if b"%PDF" in head:
            if length > max_bytes:
                raise UserError(f"That PDF is too big to download here ({length >> 20} MB; the limit is {max_bytes >> 20} MB).")
            folder = dest_dir or downloads_dir()
            folder.mkdir(parents=True, exist_ok=True)
            target = unique(folder / _filename(resp, final))
            part = target.with_name(target.name + ".part")
            got = len(head)
            try:
                with open(part, "wb") as f:
                    f.write(head)
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > max_bytes:
                            raise UserError(f"That PDF is too big to download here (the limit is {max_bytes >> 20} MB).")
                        f.write(chunk)
                part.replace(target)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            return {"kind": "pdf", "path": str(target), "url": final, "bytes": got}
        if ctype in ("text/html", "application/xhtml+xml") or head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            body = head + resp.read(PEEK)
            return {"kind": "webpage", "url": final, "title": page_title(body) or (urllib.parse.urlsplit(final).hostname or "Webpage")}
    what = {"image/jpeg": "a photo", "image/png": "a picture", "application/zip": "a zip file",
            "application/msword": "a Word file",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "a Word file"}.get(ctype, "")
    raise UserError(f"That link is {what or 'a file'}, not a PDF or a webpage. Download it, then open it with Choose a file.")


def _http_message(code: int) -> str:
    if code in (401, 403):
        return "That website won't share the file without signing in - download it in your browser, then open it here."
    if code == 404:
        return "There's nothing at that address (the website says it can't be found)."
    if code == 429:
        return "The website is busy - try again in a minute."
    return f"The website couldn't give us that file (it answered {code})."
