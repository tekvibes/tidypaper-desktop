"""One copy of the desktop app at a time.

pywebview serves the page on a fixed local port (so the page's own storage survives restarts), so a second copy
started next to a running one would show the first copy's page and break when that one closes.  Instead the second
copy hands its files to the running copy - which opens them and comes to the front - and quits.

The running copy listens on 127.0.0.1 (a port the system picks) and writes {port, token, pid} to
~/.toolkit/instance.json (readable by you only).  Only a message carrying that token is accepted, and it can only ask
to open files that exist.  A file left behind by a copy that crashed is noticed (nothing answers) and replaced.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from pathlib import Path

from bridge_util import log

MAX_MESSAGE = 64 * 1024
MAX_FILES = 50


def info_file() -> Path:
    return Path.home() / ".toolkit" / "instance.json"


def hand_off(files: list[str], timeout: float = 2.0) -> bool:
    """Another copy is running: give it these files (none = just come to the front).  True = it took them."""
    try:
        info = json.loads(info_file().read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
    except Exception:
        return False
    if os.name == "nt":                     # let the running copy bring its window to the front (Windows' rule)
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)      # ASFW_ANY
        except Exception:
            pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps({"token": token, "files": [str(f) for f in files][:MAX_FILES]}) + "\n").encode("utf-8"))
            reply = s.makefile("r", encoding="utf-8").readline()
    except OSError:
        return False
    if reply.strip() == "ok":
        log.info("handed %d file(s) to the copy that's already running (pid %s)", len(files), info.get("pid"))
        return True
    return False


class Listener:
    """The running copy's side: on_files(list of existing paths) is called for each message from another copy
    (an empty list = just come to the front)."""

    def __init__(self, on_files):
        self.on_files = on_files
        self.token = secrets.token_hex(16)
        self.port = 0
        self._sock: socket.socket | None = None

    def start(self) -> "Listener":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self._sock, self.port = s, s.getsockname()[1]
        f = info_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}).encode("utf-8")
        tmp = f.with_name(f.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)        # yours only
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, f)
        threading.Thread(target=self._serve, daemon=True, name="instance").start()
        return self

    def _serve(self):
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket):
        with conn:
            try:
                conn.settimeout(3)
                buf = b""
                while b"\n" not in buf and len(buf) < MAX_MESSAGE:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                msg = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
                if not secrets.compare_digest(str(msg.get("token", "")), self.token):
                    log.warning("ignored a message from another program (wrong token)")
                    return
                files = [str(Path(p).resolve()) for p in msg.get("files", [])[:MAX_FILES]
                         if isinstance(p, str) and p and Path(p).exists()]
                conn.sendall(b"ok\n")
            except Exception as ex:
                log.info("instance message not understood: %s", ex)
                return
        try:
            self.on_files(files)
        except Exception:
            log.exception("opening files from another copy")

    def stop(self):
        """Closing: stop listening and remove instance.json - if it's still ours."""
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        try:
            f = info_file()
            if json.loads(f.read_text(encoding="utf-8")).get("token") == self.token:
                f.unlink()
        except Exception:
            pass
