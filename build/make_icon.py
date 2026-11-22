"""Generate build/icon.ico - the Toolkit brand mark (dark rounded square, white document glyph).

    py build/make_icon.py

Drawn at 1024 px and downsampled, so every size in the .ico is crisp.  Matches the
`i-file-pdf` symbol used on the home screen (24-unit grid, 2-unit stroke).
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
BG = (17, 24, 39, 255)        # #111827
FG = (255, 255, 255, 255)
S = 1024                      # master size
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def master() -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=BG)

    # glyph on a 24-unit grid, centred in the square with ~18% padding
    g = S * 0.64 / 24                     # px per grid unit
    ox = oy = (S - 24 * g) / 2
    P = lambda x, y: (ox + x * g, oy + y * g)  # noqa: E731
    w = max(2, int(round(2.0 * g)))       # stroke width (2 units, like the SVG)

    def line(*pts):
        d.line([P(*p) for p in pts], fill=FG, width=w, joint="curve")
        for p in (pts[0], pts[-1]):       # round caps
            cx, cy = P(*p); r = w / 2
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=FG)

    # page outline: rounded rectangle 5..19 x 3..21 (radius 2) with the top-right corner folded
    (x0, y0), (x1, y1) = P(5, 3), P(19, 21)
    h = w / 2
    d.rounded_rectangle((x0 - h, y0 - h, x1 + h, y1 + h), radius=2 * g + h, outline=FG, width=w)
    # cut the corner beyond the diagonal (14,3)-(19,8), then draw the diagonal and the fold
    big = 3 * g
    d.polygon([P(14, 3), (x1 + big, y0 - big), P(19, 8)], fill=BG)
    d.polygon([P(14, 3 - 2), P(21, 3 - 2), P(21, 10), P(19, 8), P(14, 3)], fill=BG)
    line((14, 3), (19, 8))
    line((14, 3), (14, 8), (19, 8))
    # text lines: M9 13 h6  M9 17 h4
    line((9, 13), (15, 13))
    line((9, 17), (13, 17))
    return im


def main():
    big = master()
    out = HERE / "icon.ico"
    big.resize((256, 256), Image.LANCZOS).save(out, sizes=[(s, s) for s in SIZES])
    big.resize((256, 256), Image.LANCZOS).save(HERE / "icon.png")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
