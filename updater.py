"""Self-installing updates for the desktop app (Mac and Windows).

The update check (bridge_more.update_check) reads the relay's /version; when it names the new build's file with its
SHA-256 and size, and this copy of the app can replace itself, the file is downloaded quietly in the background to
~/.toolkit/updates, checked, and installed when the app closes ("Restart now" closes it straight away):

  Mac      the .dmg is mounted read-only and the .app copied out; it must be signed by the same team as the running
           app (codesign --verify --deep --strict, TeamIdentifier, bundle id, version).  After the app quits a small
           shell script swaps the bundle in place (the old one is put back if anything fails) and opens it again.
  Windows  the downloaded <App>Setup.exe runs after the app quits:  /SILENT /SUPPRESSMSGBOXES /NORESTART
           /CLOSEAPPLICATIONS /UPDATE=1 [/RELAUNCH=1]  (installer.iss skips the add-ons for /UPDATE=1 and opens the
           app again for /RELAUNCH=1).
Closing the app installs without opening it again; "Restart now" opens it again.

Only a built app that can replace itself does this: a Mac app in a folder you can write to (not on the .dmg, not
"translocated" from Downloads), or a Windows app put there by the installer (unins000.exe next to it).  Anything else
- a dev run, the portable zip - keeps the old "Get it" link.  Nothing here ever runs a file that failed its checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import brand
from bridge_util import log

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"
MAX_BYTES = 900_000_000           # an update bigger than this is refused (today's builds are 60-200 MB)
CHUNK = 1 << 20


def updates_dir() -> Path:
    return Path.home() / ".toolkit" / "updates"


def _version_tuple(v: str) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) > 1 and out[-1] == 0:
        out.pop()
    return tuple(out)


def address_ok(url: str) -> bool:
    """Updates come over https only (http://127.0.0.1 / localhost for the selftests)."""
    return str(url).startswith(("https://", "http://127.0.0.1:", "http://localhost:"))


# ------------------------------------------------------------------ where this copy of the app lives

def running_app() -> Path | None:
    """Mac: the running .app bundle;  Windows: the running .exe.  None in a dev run (python app.py)."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if IS_MAC:
        app = exe.parents[2] if len(exe.parents) > 2 else None        # <App>.app/Contents/MacOS/<App>
        return app if app and app.suffix == ".app" else None
    return exe


def can_install(app: Path | None = None) -> tuple[bool, str]:
    """(True, "") when this copy can put a new version in its own place; otherwise (False, why) for the log."""
    app = app or running_app()
    if app is None:
        return False, "not a built app (a dev run)"
    if IS_MAC:
        s = str(app)
        if "/AppTranslocation/" in s:
            return False, "opened straight from Downloads (macOS runs it from a hidden copy) - drag it to Applications"
        if s.startswith("/Volumes/"):
            return False, "running from the disk image or another disk"
        if not (os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)):
            return False, f"can't write to {app.parent}"
        return True, ""
    if IS_WIN:
        if not (app.parent / "unins000.exe").exists():
            return False, "not installed by the installer (the portable copy)"
        return True, ""
    return False, "this system isn't supported"


# ------------------------------------------------------------------ Mac checks

def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def team_id(app: Path) -> str | None:
    """The signing team of a .app (None when it's signed ad hoc or not at all)."""
    try:
        r = _run(["/usr/bin/codesign", "-dv", "--verbose=2", str(app)], 60)
    except Exception:
        return None
    for line in (r.stderr + r.stdout).splitlines():
        if line.startswith("TeamIdentifier="):
            t = line.split("=", 1)[1].strip()
            return None if not t or t == "not set" else t
    return None


def bundle_info(app: Path) -> dict:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def check_mac_app(new: Path, current: Path, version: str) -> str:
    """'' when `new` may replace `current`; otherwise why not."""
    info, cur = bundle_info(new), bundle_info(current)
    if not info:
        return "the update has no Info.plist"
    if info.get("CFBundleIdentifier") != cur.get("CFBundleIdentifier"):
        return f"it's a different app ({info.get('CFBundleIdentifier')})"
    if _version_tuple(info.get("CFBundleShortVersionString", "")) != _version_tuple(version):
        return f"it says version {info.get('CFBundleShortVersionString')}, not {version}"
    try:
        r = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(new)], 300)
    except Exception as ex:
        return f"its signature couldn't be checked ({ex})"
    if r.returncode != 0:
        return "its signature doesn't verify: " + (r.stderr.strip().splitlines() or ["?"])[-1]
    mine, theirs = team_id(current), team_id(new)
    if not mine:
        return "this copy isn't signed by a developer, so an update can't be matched to it"
    if theirs != mine:
        return f"it's signed by a different developer ({theirs or 'nobody'})"
    return ""


def extract_dmg(dmg: Path, dest: Path) -> Path:
    """Copy the .app out of a .dmg into dest (made fresh).  Returns the copied .app."""
    mnt = Path(tempfile.mkdtemp(prefix="tp-update-"))
    try:
        r = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-noverify",
                  "-mountpoint", str(mnt), str(dmg)], 180)
        if r.returncode != 0:
            raise RuntimeError("the disk image wouldn't open: " + r.stderr.strip()[-200:])
        try:
            apps = [p for p in mnt.iterdir() if p.suffix == ".app" and not p.is_symlink()]
            if len(apps) != 1:
                raise RuntimeError(f"expected one app on the disk image, found {len(apps)}")
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            out = dest / apps[0].name
            r = _run(["/usr/bin/ditto", str(apps[0]), str(out)], 600)
            if r.returncode != 0:
                raise RuntimeError("couldn't copy the app off the disk image: " + r.stderr.strip()[-200:])
            return out
        finally:
            _run(["/usr/bin/hdiutil", "detach", str(mnt), "-force"], 60)
    finally:
        try:
            mnt.rmdir()
        except OSError:
            pass


# the swap, run by /bin/sh after the app has quit: $1 pid, $2 new .app, $3 the app's place, $4 where the old one goes.
# The app is opened again afterwards (whichever version is in place) unless TP_NO_RELAUNCH=1: closing the app just
# installs; "Restart now" opens it again.
MAC_SWAP = r'''#!/bin/sh
pid="$1"; new="$2"; app="$3"; old="$4"
again() { [ -n "$TP_NO_RELAUNCH" ] || open "$app"; }
echo "$(date '+%Y-%m-%d %H:%M:%S') waiting for $pid to quit"
i=0
while kill -0 "$pid" 2>/dev/null; do
  i=$((i + 1)); [ "$i" -gt 600 ] && { echo "the app didn't quit - not updating"; exit 1; }
  sleep 0.1
done
[ -d "$new" ] || { echo "the new version is missing"; again; exit 1; }
rm -rf "$old"
if ! mv "$app" "$old"; then echo "couldn't move the old version aside"; again; exit 1; fi
if mv "$new" "$app"; then
  echo "updated: $app"
  rm -rf "$old"
else
  echo "couldn't put the new version in place - putting the old one back"
  rm -rf "$app"; mv "$old" "$app"
fi
touch "$app"
again
'''


def windows_command(setup: Path, pid: int, logfile: Path, relaunch: bool = True) -> list[str]:
    """PowerShell: wait for the app (pid) to quit, then run the installer quietly (/UPDATE=1: no add-ons);
    with relaunch it opens the app again itself (/RELAUNCH=1)."""
    q = lambda s: "'" + str(s).replace("'", "''") + "'"
    args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/UPDATE=1"] + (["/RELAUNCH=1"] if relaunch else [])
    args.append(f'/LOG="{logfile}"')
    script = (f"Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue; "
              f"Start-Process -FilePath {q(setup)} -ArgumentList {','.join(q(a) for a in args)}")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-Command", script]


# ------------------------------------------------------------------ the updater

class Updater:
    """One per app.  start(info) downloads + checks in a thread; status() for the page; install_on_exit() when
    the app is closing."""

    def __init__(self, current_version: str = brand.VERSION, app: Path | None = None, folder: Path | None = None):
        self.current = current_version
        self.app = app if app is not None else running_app()
        self.folder = folder or updates_dir()
        self._lock = threading.Lock()
        self._state: dict = {"state": "idle"}
        self._thread: threading.Thread | None = None
        self._ready: dict | None = None
        self._spawned = False

    # ---- what the page sees
    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state = kw

    def possible(self) -> tuple[bool, str]:
        return can_install(self.app) if self.app is not None else (False, "not a built app (a dev run)")

    # ---- download + check
    def start(self, info: dict) -> dict:
        """info = {version, file, sha256, size}.  Starts (or carries on with) getting that version."""
        version, url = str(info.get("version") or ""), str(info.get("file") or "")
        sha, size = str(info.get("sha256") or "").lower(), info.get("size")
        ok, why = self.possible()
        if not ok:
            return {"state": "unavailable", "why": why}
        if not (version and address_ok(url) and len(sha) == 64 and isinstance(size, int) and 0 < size <= MAX_BYTES):
            return {"state": "unavailable", "why": "the relay gave no file to install"}
        if _version_tuple(version) <= _version_tuple(self.current):
            return {"state": "unavailable", "why": "not newer"}
        with self._lock:
            st = self._state
            if st.get("version") == version and st.get("state") in ("downloading", "checking", "ready"):
                return dict(st)
            if self._thread and self._thread.is_alive():
                return dict(st)
            self._state = {"state": "downloading", "version": version, "done": 0, "total": size}
            self._thread = threading.Thread(target=self._get, args=(version, url, sha, size), daemon=True, name="update")
            self._thread.start()
            return dict(self._state)

    def _get(self, version: str, url: str, sha: str, size: int):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._tidy(keep=version)
            kind = "mac" if IS_MAC else "windows"
            target = self.folder / (f"{brand.EXE_STEM}-{version}.dmg" if kind == "mac" else f"{brand.EXE_STEM}Setup-{version}.exe")
            ready = self._read_ready()
            if not (ready and ready.get("version") == version and ready.get("sha256") == sha and Path(ready.get("install", "")).exists()):
                if not (target.exists() and target.stat().st_size == size and _sha256(target) == sha):
                    self._download(url, target, sha, size, version)
                self._set(state="checking", version=version)
                if kind == "mac":
                    app = extract_dmg(target, self.folder / version)
                    why = check_mac_app(app, self.app, version)
                    if why:
                        shutil.rmtree(self.folder / version, ignore_errors=True)
                        raise RuntimeError(why)
                    install = app
                else:
                    install = target
                ready = {"version": version, "sha256": sha, "kind": kind, "install": str(install), "file": str(target)}
                (self.folder / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
            with self._lock:
                self._ready = ready
                self._state = {"state": "ready", "version": version}
            log.info("update %s ready to install (%s)", version, ready["install"])
        except Exception as ex:
            log.warning("update %s not ready: %s", version, ex)
            self._set(state="failed", version=version, error=str(ex)[:300])

    def _download(self, url: str, target: Path, sha: str, size: int, version: str):
        part = target.with_name(target.name + ".part")
        h = hashlib.sha256()
        got = 0
        req = urllib.request.Request(url, headers={"User-Agent": f"{brand.EXE_STEM}/{self.current} (update)"})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    raise RuntimeError("the download is bigger than the relay said")
                h.update(chunk); f.write(chunk)
                self._set(state="downloading", version=version, done=got, total=size)
        if got != size:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"the download stopped early ({got} of {size} bytes)")
        if h.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise RuntimeError("the download doesn't match its SHA-256 - not installing it")
        os.replace(part, target)

    def _read_ready(self) -> dict | None:
        try:
            return json.loads((self.folder / "ready.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def _tidy(self, keep: str = ""):
        """Remove downloads of other versions (and anything left from a version that's now installed)."""
        if not self.folder.exists():
            return
        ready = self._read_ready()
        if ready and ready.get("version") != keep:
            (self.folder / "ready.json").unlink(missing_ok=True)
        for p in self.folder.iterdir():
            if p.name in ("ready.json", "install.log") or (keep and keep in p.name and not p.name.endswith(".part")):
                continue
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except OSError:
                pass

    def startup(self):
        """At start: an update to this version (or older) is done with - remove its files; one that was downloaded
        and checked but never installed (the app was stopped) is ready again."""
        r = self._read_ready()
        if not r:
            return
        if _version_tuple(r.get("version", "")) <= _version_tuple(self.current) or not self.possible()[0]:
            self._tidy()
        elif Path(r.get("install", "")).exists() and r.get("kind") == ("mac" if IS_MAC else "windows"):
            with self._lock:
                self._ready = r
                self._state = {"state": "ready", "version": r["version"]}

    # ---- install
    def ready(self) -> dict | None:
        with self._lock:
            return dict(self._ready) if self._ready else None

    def install_on_exit(self, pid: int | None = None, relaunch: bool = True) -> bool:
        """Start the installer helper, which waits for this process to quit.  Once only.  True = started."""
        r = self.ready()
        if not r or self._spawned:
            return False
        pid = pid or os.getpid()
        self.folder.mkdir(parents=True, exist_ok=True)
        logf = self.folder / "install.log"
        try:
            if r["kind"] == "mac":
                script = self.folder / "install.sh"
                script.write_text(MAC_SWAP, encoding="utf-8")
                old = self.folder / ("old-" + self.app.name)
                env = dict(os.environ)
                if not relaunch:
                    env["TP_NO_RELAUNCH"] = "1"
                with open(logf, "a", encoding="utf-8") as out:
                    subprocess.Popen(["/bin/sh", str(script), str(pid), r["install"], str(self.app), str(old)],
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                     close_fds=True, env=env)
            else:
                # NEW_PROCESS_GROUP | NO_WINDOW.  Not DETACHED_PROCESS: PowerShell with no console and -WindowStyle
                # Hidden quits at once without running anything (so no update ever installed on Windows, 0.2.4-0.2.7)
                flags = 0x00000200 | 0x08000000
                subprocess.Popen(windows_command(Path(r["install"]), pid, logf, relaunch), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except Exception:
            log.exception("couldn't start installing the update")
            return False
        self._spawned = True
        log.info("installing update %s once the app has quit", r["version"])
        return True


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
