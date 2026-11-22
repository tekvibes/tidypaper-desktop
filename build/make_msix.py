"""The Microsoft Store's package (MSIX), made from the app folder build_installer.bat has already built.

    py build\\make_msix.py                       a package to try on this PC (made-up identity)
    py build\\make_msix.py --register            ... and put it in on this PC from its folder (needs Developer Mode)
    py build\\make_msix.py --unregister          take that tried copy out again
    py build\\make_msix.py --name 12345Tekvibes.TidyPaper --publisher "CN=..." --publisher-name "Tekvibes"
                                                the package for the Store - the three values are on Partner Center's
                                                Product identity page for the app

Nothing is rebuilt and nothing of the normal build changes: build\\dist\\<App> is copied to build\\msix\\layout, and
only the copy's brand.json gets "store": "microsoft" (the Store updates it, so the app never looks for new versions
itself) and no update address.  The package is left unsigned - the Store signs it.  It is written to
build\\dist\\<App>-<version>.msix.

What the package has that our installer has too: the Start menu entry, and "Open with" for PDFs and the other files
the app opens.  What it hasn't: the right-click "Edit with" / "Open in" entries (a packaged app can only add those
with a COM add-in of its own), the desktop shortcut and the add-ons offered while installing (the app still offers
them when a task needs one).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import brand  # noqa: E402
import convert  # noqa: E402

LOCAL = {"name": "TidyPaper.LocalTry", "publisher": "CN=TidyPaper Local Try", "publisher_name": "TidyPaper"}
MIN_WINDOWS = "10.0.17763.0"            # Windows 10 1809 - WebView2 and MSIX's full-trust apps
TESTED_WINDOWS = "10.0.26100.0"

# "Open with" - the kinds of file the app opens, in groups (a group is one entry in Windows' Default apps list)
GROUPS = [("pdf", "PDF document", {".pdf"}),
          ("pictures", "Picture", {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff",
                                   ".webp"}),
          ("office", "Office document", {".doc", ".docm", ".docx", ".dot", ".dotx", ".odp", ".ods", ".odt", ".pps",
                                         ".ppsx", ".ppt", ".pptm", ".pptx", ".rtf", ".wpd", ".wps", ".xls", ".xlsb",
                                         ".xlsm", ".xlsx"}),
          ("books", "E-book", {".cbz", ".epub", ".fb2", ".mobi"}),
          ("xps", "XPS document", {".oxps", ".xps"}),
          ("text", "Text file", {".csv", ".log", ".md", ".text", ".tsv", ".txt"})]


def msix_version(v: str) -> str:
    """0.2.17 -> 1.2.17.0.  The Store refuses a first number of 0 and keeps the fourth for itself (0), so the first is
    always one more than the app's: 1.0.0 will be 2.0.0.0.  Don't change this rule once a package is published - the
    Store only takes versions that go up."""
    major, minor, patch = ([int(x) for x in v.split(".")] + [0, 0])[:3]
    return f"{major + 1}.{minor}.{patch}.0"


def find_makeappx() -> Path:
    kits = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    found = sorted(kits.glob("10.*/x64/makeappx.exe"), key=lambda p: [int(x) for x in p.parent.parent.name.split(".")])
    if not found:
        raise SystemExit("makeappx.exe wasn't found - it comes with the Windows SDK (Windows Kits\\10).")
    return found[-1]


def logos(icon: Path, assets: Path):
    """The pictures the package needs, from build\\icon.png: square ones with the icon filling them, the wide tile
    with it in the middle; "unplated" = the taskbar and Start show it on nothing, not on a coloured square."""
    from PIL import Image
    src = Image.open(icon).convert("RGBA")
    assets.mkdir(parents=True, exist_ok=True)

    def square(size, name, pad=0.0):
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        inner = max(1, round(size * (1 - 2 * pad)))
        canvas.alpha_composite(src.resize((inner, inner), Image.LANCZOS), ((size - inner) // 2, (size - inner) // 2))
        canvas.save(assets / name)

    square(44, "Square44x44Logo.png")
    for t in (16, 24, 32, 48, 256):
        square(t, f"Square44x44Logo.targetsize-{t}_altform-unplated.png")
    square(44, "Square44x44Logo.targetsize-44_altform-unplated.png")
    square(150, "Square150x150Logo.png", pad=0.18)
    square(50, "StoreLogo.png")
    wide = Image.new("RGBA", (310, 150), (0, 0, 0, 0))
    wide.alpha_composite(src.resize((100, 100), Image.LANCZOS), (105, 25))
    wide.save(assets / "Wide310x150Logo.png")


def manifest(name: str, publisher: str, publisher_name: str, version: str, exe: str) -> str:
    opens = convert.OPENABLE | {".pdf"}
    grouped = set().union(*(g[2] for g in GROUPS))
    missing = sorted(opens - grouped)
    if missing:                       # a kind added to convert.OPENABLE: put it in a group above
        raise SystemExit(f"make_msix.py: put {', '.join(missing)} in one of GROUPS")
    ftas = []
    for key, label, exts in GROUPS:
        types = "".join(f"<uap:FileType>{e}</uap:FileType>" for e in sorted(exts & opens))
        if types:
            ftas.append(f"""
        <uap:Extension Category="windows.fileTypeAssociation">
          <uap3:FileTypeAssociation Name="{key}" Parameters="&quot;%1&quot;">
            <uap:DisplayName>{escape(label)}</uap:DisplayName>
            <uap:Logo>Assets\\StoreLogo.png</uap:Logo>
            <uap:SupportedFileTypes>{types}</uap:SupportedFileTypes>
          </uap3:FileTypeAssociation>
        </uap:Extension>""")
    app_name = escape(brand.NAME)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
         IgnorableNamespaces="uap uap3 rescap">
  <Identity Name={quoteattr(name)} Publisher={quoteattr(publisher)} Version="{version}" ProcessorArchitecture="x64"/>
  <Properties>
    <DisplayName>{app_name}</DisplayName>
    <PublisherDisplayName>{escape(publisher_name)}</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="{MIN_WINDOWS}" MaxVersionTested="{TESTED_WINDOWS}"/>
  </Dependencies>
  <Resources>
    <Resource Language="en-gb"/>
  </Resources>
  <Applications>
    <Application Id="App" Executable={quoteattr(exe)} EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="{app_name}" Description={quoteattr(brand.TAGLINE)} BackgroundColor="transparent"
                          Square150x150Logo="Assets\\Square150x150Logo.png" Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="Assets\\Wide310x150Logo.png"/>
      </uap:VisualElements>
      <Extensions>{"".join(ftas)}
      </Extensions>
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust"/>
  </Capabilities>
</Package>
"""


def powershell(cmd: str) -> int:
    return subprocess.call(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd])


def main():
    ap = argparse.ArgumentParser(description="Make the Microsoft Store package (MSIX) from the built app folder.")
    ap.add_argument("--name", default=LOCAL["name"], help="Package/Identity/Name from Partner Center")
    ap.add_argument("--publisher", default=LOCAL["publisher"], help="Package/Identity/Publisher (CN=...)")
    ap.add_argument("--publisher-name", default=LOCAL["publisher_name"], help="Package/Properties/PublisherDisplayName")
    ap.add_argument("--register", action="store_true", help="put it in on this PC from its folder (Developer Mode)")
    ap.add_argument("--unregister", action="store_true", help="take the tried copy out of this PC")
    a = ap.parse_args()

    if a.unregister:
        sys.exit(powershell(f"Get-AppxPackage -Name '{a.name}' | Remove-AppxPackage"))

    stem = brand.EXE_STEM
    built = ROOT / "build" / "dist" / stem
    if not (built / f"{stem}.exe").exists():
        raise SystemExit(f"Build the app first (build\\build_installer.bat) - {built} has no {stem}.exe.")
    bundled = json.loads((built / "_internal" / "brand.json").read_text(encoding="utf-8"))
    app_version = bundled.get("version", brand.VERSION)
    version = msix_version(app_version)

    layout = ROOT / "build" / "msix" / "layout"
    if a.register:                    # a registered copy runs from the layout itself: take the old one out first
        powershell(f"Get-AppxPackage -Name '{a.name}' | Remove-AppxPackage")
    shutil.rmtree(layout, ignore_errors=True)
    shutil.copytree(built, layout)
    bundled.update(store="microsoft", update_url="")
    (layout / "_internal" / "brand.json").write_text(json.dumps(bundled, indent=2), encoding="utf-8")
    logos(ROOT / "build" / "icon.png", layout / "Assets")
    (layout / "AppxManifest.xml").write_text(manifest(a.name, a.publisher, a.publisher_name, version, f"{stem}.exe"),
                                             encoding="utf-8")

    out = ROOT / "build" / "dist" / f"{stem}-{app_version}.msix"
    out.unlink(missing_ok=True)
    r = subprocess.run([str(find_makeappx()), "pack", "/d", str(layout), "/p", str(out), "/o"],
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-3000:], r.stderr[-2000:])
        raise SystemExit("makeappx couldn't make the package - see above.")
    print(f"Package: {out}  ({out.stat().st_size / 1e6:.0f} MB, version {version}, identity {a.name})")
    if a.name == LOCAL["name"]:
        print("  (a made-up identity, for trying on this PC - the Store's needs --name/--publisher from Partner Center)")

    if a.register:
        code = powershell(f"Add-AppxPackage -Register '{layout / 'AppxManifest.xml'}'")
        if code:
            raise SystemExit("It couldn't be put in - is Developer Mode on? (Settings > System > For developers)")
        print(f"Put in on this PC: {brand.NAME} is in the Start menu.  Take it out with --unregister.")


if __name__ == "__main__":
    main()
