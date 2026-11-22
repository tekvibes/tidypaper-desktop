"""Draw the trial looks' app icons (Settings › Look) from the Design canvas mockups.

    python3 build/make_look_icons.py          (needs rsvg-convert: brew install librsvg; and Pillow)

Writes, for each look:
  ui/looks/<look>-dock.png     512 px, the Mac's rounded square with Apple's margin - the Dock picture while that look is on
  ui/looks/<look>-mark.png     96 px rounded square - the mark beside the app's name on Home
  ios/TidyPaper/Assets.xcassets/AppIcon-<Look>.appiconset/AppIcon-1024.png   square, no transparency (iOS rounds it)

Since iOS build 30 the iPhone app's own icon (AppIcon.appiconset - the App Store's, and the Home Screen's until a
look is picked) is Highlighter's picture: after drawing, copy AppIcon-Highlighter's PNG over AppIcon's.  The old
outline is AppIcon-Classic (not drawn here).
"""
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# the glyph on a 168-unit square, the background colour drawn separately
GLYPHS = {
    "highlighter": ("#14213D", """
      <path d="M52 34h48l20 20v80a6 6 0 0 1-6 6H52a6 6 0 0 1-6-6V40a6 6 0 0 1 6-6z" fill="#FFFFFF"/>
      <path d="M100 34v14a6 6 0 0 0 6 6h14z" fill="#D5DAE6"/>
      <rect x="58" y="66" width="44" height="7" rx="3.5" fill="#B8C0D2"/>
      <rect x="52" y="82" width="64" height="15" rx="5" fill="#FFD447" transform="rotate(-4 84 89)"/>
      <rect x="58" y="86" width="50" height="7" rx="3.5" fill="#14213D"/>
      <rect x="58" y="106" width="36" height="7" rx="3.5" fill="#B8C0D2"/>"""),
    "teal": ("#0E5C57", """
      <path d="M50 32h46l22 22v78a8 8 0 0 1-8 8H50a8 8 0 0 1-8-8V40a8 8 0 0 1 8-8z" fill="#FFFFFF"/>
      <path d="M96 32v14a8 8 0 0 0 8 8h14z" fill="#CFE5DF"/>
      <rect x="56" y="66" width="44" height="8" rx="4" fill="#A9CFC6"/>
      <rect x="56" y="84" width="34" height="8" rx="4" fill="#A9CFC6"/>
      <circle cx="114" cy="118" r="26" fill="#E8664A"/>
      <path d="m102 118 8 8 15-16" fill="none" stroke="#FFFFFF" stroke-width="8" stroke-linecap="round" stroke-linejoin="round"/>"""),
    "midnight": ("#5B3FE0", """
      <rect x="52" y="30" width="72" height="92" rx="8" fill="#9C88FF" transform="rotate(10 88 76)"/>
      <rect x="48" y="38" width="72" height="92" rx="8" fill="#D6CDFF" transform="rotate(4 84 84)"/>
      <rect x="44" y="46" width="72" height="92" rx="8" fill="#FFFFFF"/>
      <rect x="56" y="66" width="40" height="7" rx="3.5" fill="#5B3FE0"/>
      <rect x="56" y="82" width="48" height="7" rx="3.5" fill="#C9C2EE"/>
      <rect x="56" y="98" width="32" height="7" rx="3.5" fill="#C9C2EE"/>"""),
}
IOS_NAMES = {"highlighter": "AppIcon-Highlighter", "teal": "AppIcon-Teal", "midnight": "AppIcon-Midnight"}


def svg(bg: str, glyph: str, rounded: bool, inset: float = 0) -> str:
    """inset: the margin around the square, in 168-units (the Mac's icon grid leaves ~10% each side)."""
    side = 168 - 2 * inset
    scale = side / 168
    rx = 38 * scale if rounded else 0
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="168" height="168" viewBox="0 0 168 168">'
            f'<rect x="{inset}" y="{inset}" width="{side}" height="{side}" rx="{rx}" fill="{bg}"/>'
            f'<g transform="translate({inset} {inset}) scale({scale})">{glyph}</g></svg>')


def render(text: str, size: int, out: Path, opaque: bool = False):
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(text)
    subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(out), f.name], check=True)
    Path(f.name).unlink()
    if opaque:                                        # App Store icons may not have an alpha channel
        Image.open(out).convert("RGB").save(out)


def main():
    for look, (bg, glyph) in GLYPHS.items():
        render(svg(bg, glyph, True, inset=168 * 100 / 1024), 512, ROOT / "ui" / "looks" / f"{look}-dock.png")
        render(svg(bg, glyph, True), 96, ROOT / "ui" / "looks" / f"{look}-mark.png")
        d = ROOT / "ios" / "TidyPaper" / "Assets.xcassets" / f"{IOS_NAMES[look]}.appiconset"
        render(svg(bg, glyph, False), 1024, d / "AppIcon-1024.png", opaque=True)
        (d / "Contents.json").write_text(json.dumps({
            "images": [{"filename": "AppIcon-1024.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
            "info": {"author": "xcode", "version": 1}}, indent=2) + "\n")
        print("drew", look)


if __name__ == "__main__":
    main()
