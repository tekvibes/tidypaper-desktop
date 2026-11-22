"""Changing words in a scan - the picture work (scanedit.py does the page).  The desktop side of the iPad's
ScanPatch.swift: the same steps and the same numbers, with numpy for the pixels and PyMuPDF to draw the letters.

A scan's words are part of a picture, so they can't be retyped the way a page's own words are.  Here the old word is
painted over in the picture itself, in the paper's own colour (with its shading and grain), and the new words are
drawn into the picture in the nearest of the usual fonts - at the old word's size, tilt, lean, ink colour, weight and
softness.  It's a match by eye, not the original font: right for a date, a name or a number, not for a page of
rewriting.

Everything is found from the ink, by drawing the old words and comparing with what is in the picture:
 - how the line runs and how its letters lean (a page photographed at an angle): the slope and lean that make the
   ink fall into the sharpest rows and columns - the words are read with plain boxes that say neither;
 - the font: each of the six tried on the picked word and the words beside it, drawn as tall and as wide as the
   scan's ink - the one most alike, that needs least stretching;
 - softness (how sharp the edges are) and weight (how much ink there is), and the ink's colour, as dark as soft or
   thin strokes need to end as dark as the scan's.

The work is done on a "strip": the part of the picture round one line of words, straightened so the line runs left
to right with its letters upright.  A straight scan is cut exactly along its pixels (nothing is blended); only the
pixels under the old word and the new are ever written back.  Nothing here knows about PDFs.

    look(px, line, pick, nearby) -> Place       px: the picture, uint8 (rows, columns, 3), row 0 at the top
    target(place, "word" / "line") -> Target
    compose(place, target, text, face, size) -> Composed      (TooLong / Letters when it can't be)
    picture(place, composed, view(...)) -> uint8 array        how it will look
    write(composed, place.geometry, px) -> IntRect            into the picture itself
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

F32 = np.float32
LUM = np.array([0.299, 0.587, 0.114], dtype=F32)
DRAW_SCALE = 4              # letters are drawn this many times larger and averaged down: MuPDF sets small letters
                            # on whole pixels, and they must sit to a fraction of one


def lum(c):
    return np.asarray(c, dtype=F32) @ LUM


def _round(v: float) -> int:
    """Halves away from nought, as Swift's rounded()."""
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


class TooLong(Exception):
    """The new words don't fit before the next mark."""


class Letters(Exception):
    """Some of the new letters can't be drawn."""


# ---------------------------------------------------------------- shapes

@dataclass(frozen=True)
class IntRect:
    x: int
    y: int
    w: int
    h: int

    @property
    def max_x(self):
        return self.x + self.w

    @property
    def max_y(self):
        return self.y + self.h

    @property
    def empty(self):
        return self.w <= 0 or self.h <= 0

    @staticmethod
    def of(r: "Rect") -> "IntRect":
        x0, y0 = math.floor(r.x), math.floor(r.y)
        return IntRect(x0, y0, math.ceil(r.x + r.w) - x0, math.ceil(r.y + r.h) - y0)

    def grown(self, dx: int, dy: int) -> "IntRect":
        return IntRect(self.x - dx, self.y - dy, self.w + 2 * dx, self.h + 2 * dy)

    def clipped(self, width: int, height: int) -> "IntRect":
        x0, y0, x1, y1 = max(0, self.x), max(0, self.y), min(width, self.max_x), min(height, self.max_y)
        return IntRect(x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def union(self, o: "IntRect") -> "IntRect":
        if self.empty:
            return o
        if o.empty:
            return self
        x0, y0 = min(self.x, o.x), min(self.y, o.y)
        return IntRect(x0, y0, max(self.max_x, o.max_x) - x0, max(self.max_y, o.max_y) - y0)


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def max_x(self):
        return self.x + self.w

    @property
    def max_y(self):
        return self.y + self.h

    @property
    def mid_y(self):
        return self.y + self.h / 2


@dataclass
class Geometry:
    """Where a strip sits in its picture: the strip's point (u, v) is at origin + u·across + v·down (picture pixels,
    pixel i reaching from i to i + 1).  `across` runs along the line of words; `down` runs down the letters' own
    upright strokes, so it leans with them on a page photographed at an angle."""
    origin: tuple
    across: tuple
    down: tuple

    def picture(self, u, v):
        return (self.origin[0] + u * self.across[0] + v * self.down[0], self.origin[1] + u * self.across[1] + v * self.down[1])

    def strip(self, p):
        dx, dy = p[0] - self.origin[0], p[1] - self.origin[1]
        det = self.across[0] * self.down[1] - self.across[1] * self.down[0]
        return ((dx * self.down[1] - dy * self.down[0]) / det, (self.across[0] * dy - self.across[1] * dx) / det)


class Strip:
    """A piece of the picture, straightened.  RGB, row 0 at the top."""

    def __init__(self, rgb: np.ndarray):
        self.rgb = rgb                                   # uint8 (h, w, 3)
        self.height, self.width = rgb.shape[:2]
        self.lum = rgb.astype(F32) @ LUM                 # (h, w)


@dataclass
class Reading:
    """A word as it was read in the picture: its bottom-left, bottom-right and top-left corners (picture pixels)."""
    text: str
    corners: list


@dataclass(frozen=True)
class Face:
    key: str            # PyMuPDF's name for the built-in font
    name: str


FACES = [Face("helv", "Helvetica"), Face("hebo", "Helvetica Bold"), Face("tiro", "Times"), Face("tibo", "Times Bold"),
         Face("cour", "Courier"), Face("cobo", "Courier Bold")]
FACE = {f.key: f for f in FACES}
_fonts: dict = {}


def font(face: Face):
    f = _fonts.get(face.key)
    if f is None:
        f = _fonts[face.key] = fitz.Font(face.key)
    return f


@dataclass
class Plane:
    """The paper's colour across a patch: a flat sheet with a tilt (shading from a lamp or a fold)."""
    a: np.ndarray
    bu: np.ndarray = field(default_factory=lambda: np.zeros(3, F32))
    bv: np.ndarray = field(default_factory=lambda: np.zeros(3, F32))
    u0: float = 0.0
    v0: float = 0.0

    def at(self, u, v):
        u, v = np.asarray(u, F32), np.asarray(v, F32)
        return self.a + self.bu * (u - self.u0)[..., None] + self.bv * (v - self.v0)[..., None]


@dataclass
class Inked:
    """A word in the strip: where its ink is, and the paper and ink there."""
    text: str
    box: Rect               # as read (strip pixels)
    ink: IntRect            # the ink itself, tight
    paper: Plane
    colour: np.ndarray
    grain: np.ndarray       # how grainy the paper is (a standard deviation for each of red, green and blue)

    def cover(self, s: Strip, x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
        """How much ink is at each pixel of a part of the strip, 0...1 (nothing outside the strip)."""
        out = np.zeros((max(0, y1 - y0), max(0, x1 - x0)), F32)
        a0, a1, b0, b1 = max(x0, 0), min(x1, s.width), max(y0, 0), min(y1, s.height)
        if a0 >= a1 or b0 >= b1:
            return out
        u = np.arange(a0, a1, dtype=F32) + 0.5
        v = np.arange(b0, b1, dtype=F32) + 0.5
        p = self.paper.at(u[None, :], v[:, None])
        d = p - self.colour
        ln = (d * d).sum(axis=2)
        c = s.rgb[b0:b1, a0:a1].astype(F32)
        got = np.clip(((p - c) * d).sum(axis=2) / np.maximum(ln, 1e-6), 0, 1)
        out[b0 - y0:b1 - y0, a0 - x0:a1 - x0] = np.where(ln > 1, got, 0)
        return out


@dataclass
class Fit:
    """How the line is written: the font nearest to it, and how to draw that font so it looks like the scan."""
    face: Face
    size: float             # the font's size in strip pixels
    xscale: float           # letters this much wider (or narrower) than the font's own
    blur: float             # how soft the scan's letters are (a blur, in pixels)
    weight: float           # extra thickness of stroke, in hundredths of the size
    base_a: float           # the line the letters sit on: row = base_a + base_b · x
    base_b: float
    own: float | None       # the picked word's own line (the row its letters sit on), when it could be told
    depth: float            # how dark the middles of soft strokes get, 0...1 of the ink's real colour
    likeness: float         # how alike the old word and the font's drawing of it are, 0...1

    def baseline(self, x: float) -> float:
        return self.base_a + self.base_b * x


@dataclass
class Target:
    """What is to be changed: one word, or the whole line."""
    kind: str               # "word" / "line"
    text: str
    ink: IntRect            # the old ink to paint over
    papers: list            # [(x, Plane)]: words along the target (their middles) and the paper at each
    colour: np.ndarray      # the colour the old strokes are in the middle
    grain: np.ndarray
    clear: tuple            # clear paper on each side of the ink (left, right, up, down), pixels
    room: float             # new words must end before here

    def paper(self, u, v):
        u, v = np.asarray(u, F32), np.asarray(v, F32)
        u, v = np.broadcast_arrays(u, v)
        if len(self.papers) == 1:
            return self.papers[0][1].at(u, v)
        xs = [x for x, _ in self.papers]
        out = self.papers[-1][1].at(u, v).astype(F32).copy()
        done = u <= xs[0]
        out[done] = self.papers[0][1].at(u, v)[done]
        for k in range(1, len(xs)):
            here = ~done & (u <= xs[k])
            if here.any():
                t = ((u - xs[k - 1]) / max(1.0, xs[k] - xs[k - 1]))[..., None]
                mix = self.papers[k - 1][1].at(u, v) * (1 - t) + self.papers[k][1].at(u, v) * t
                out[here] = mix[here]
            done = done | here
        return out


@dataclass
class Place:
    """A line of a scan, ready for its words to be changed."""
    strip: Strip
    geometry: Geometry
    height: float           # the height of the line's words as read (pixels)
    words: list             # [Inked or None]
    pick: int
    fit: Fit
    scores: list            # [(Face, score)]: how each font did
    inside: tuple           # the strip's columns that are inside the picture (from, to)


# ---------------------------------------------------------------- looking

def _cut(px: np.ndarray, g: Geometry, u0, u1, v0, v1, whole: bool):
    """Cut a strip out of the picture: `g`'s points from (u0, v0) to (u1, v1).  `whole`: the strip's pixels are the
    picture's own, so it starts on a whole pixel."""
    origin = g.picture(u0, v0)
    if whole:
        origin = (float(_round(origin[0])), float(_round(origin[1])))
    at = Geometry(origin, g.across, g.down)
    width, height = min(9000, math.ceil(u1 - u0)), min(1500, math.ceil(v1 - v0))
    if width <= 8 or height <= 8:
        return None
    H, W = px.shape[:2]
    i = np.arange(width, dtype=np.float64) + 0.5
    j = np.arange(height, dtype=np.float64) + 0.5
    x = origin[0] + i[None, :] * at.across[0] + j[:, None] * at.down[0] - 0.5
    y = origin[1] + i[None, :] * at.across[1] + j[:, None] * at.down[1] - 0.5
    fx, fy = np.floor(x), np.floor(y)
    tx, ty = (x - fx).astype(F32), (y - fy).astype(F32)
    exact = (tx < 0.002) & (ty < 0.002)
    tx[exact] = 0
    ty[exact] = 0
    x0, y0 = fx.astype(np.int64), fy.astype(np.int64)
    xa, xb = np.clip(x0, 0, W - 1), np.clip(x0 + 1, 0, W - 1)
    ya, yb = np.clip(y0, 0, H - 1), np.clip(y0 + 1, 0, H - 1)
    tx, ty = tx[..., None], ty[..., None]
    top = px[ya, xa].astype(F32) * (1 - tx) + px[ya, xb].astype(F32) * tx
    bottom = px[yb, xa].astype(F32) * (1 - tx) + px[yb, xb].astype(F32) * tx
    out = top * (1 - ty) + bottom * ty
    return Strip(np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)), at


def _boxes(line, g: Geometry) -> list:
    """The words' boxes in a strip."""
    out = []
    for w in line:
        c = w.corners
        tr = (c[2][0] + c[1][0] - c[0][0], c[2][1] + c[1][1] - c[0][1])
        s = [g.strip(p) for p in (c[0], c[1], c[2], tr)]
        x0, y0 = min(p[0] for p in s), min(p[1] for p in s)
        out.append(Rect(x0, y0, max(p[0] for p in s) - x0, max(p[1] for p in s) - y0))
    return out


def _levels(values: np.ndarray):
    """level(q): the brightness that part q of these pixels are no brighter than."""
    n = values.size
    cum = np.cumsum(np.bincount(np.clip(values, 0, 255).astype(np.int64).ravel(), minlength=256))

    def level(q: float) -> float:
        return float(min(255, int(np.searchsorted(cum, int(n * q), side="right"))))
    return level


def _peaked(p: np.ndarray) -> float:
    """How much a count of ink along the line (or down it) jumps from one place to the next: most when strokes are
    lined up.  Smoothed first, so that sliding by exactly whole pixels doesn't win just for being whole."""
    if len(p) <= 6:
        return 0.0
    v = np.convolve(p, np.array([1, 4, 6, 4, 1]) / 16.0, mode="valid")
    d = np.diff(v)
    return float((d * d).sum())


class _Lay:
    """Some words of a line in a first, rough strip of them, with their ink - ready to say how the line really runs
    (the corners words are read with aren't exact, and a page photographed at an angle leans)."""

    def __init__(self, across, down, slope, ii, jj, dd, ic, jc, rw):
        self.across, self.down, self.slope = across, down, slope
        self._i, self._j, self._d, self._ic, self._jc, self._rw = ii, jj, dd, ic, jc, rw
        self.ink = len(ii)
        self._seen: dict = {}

    def upright(self, t: float) -> float:
        """How well the letters' upright strokes fall into columns if they lean by `t` (pixels to the left for each
        row down, the line straightened first)."""
        got = self._seen.get(t)
        if got is None:
            k = self.slope
            pad = int(abs(t) * (self._jc + abs(k) * self._ic)) + 3
            x = self._i + t * (self._j - k * (self._i - self._ic) - self._jc) + pad
            got = self._seen[t] = _peaked(_profile(x, self._d, self._rw + 2 * pad + 2))
        return got

    @property
    def run(self):
        """The way the line runs in the picture."""
        v = (self.across[0] + self.slope * self.down[0], self.across[1] + self.slope * self.down[1])
        ln = max(1e-9, math.hypot(*v))
        return (v[0] / ln, v[1] / ln)

    def lean(self, v):
        """The lean, in this strip, of strokes that run along `v` in the picture."""
        det = self.across[0] * self.down[1] - self.across[1] * self.down[0]
        a = (v[0] * self.down[1] - v[1] * self.down[0]) / det
        b = (self.across[0] * v[1] - self.across[1] * v[0]) / det
        d = self.slope * a - b
        return a / d if abs(d) > 1e-6 else None


def _profile(at: np.ndarray, d: np.ndarray, n: int) -> np.ndarray:
    f = np.floor(at)
    t = at - f
    k = f.astype(np.int64)
    return np.bincount(k, weights=d * (1 - t), minlength=n)[:n] + np.bincount(k + 1, weights=d * t, minlength=n)[:n]


def _lay(px: np.ndarray, words: list):
    if not words:
        return None
    first, last = words[0].corners, words[-1].corners
    ax, ay = last[1][0] - first[0][0], last[1][1] - first[0][1]
    ln = math.hypot(ax, ay)
    if ln <= 2:
        return None
    ax, ay = ax / ln, ay / ln
    # "up" is the side the words' tops are on
    lift = (first[2][0] - first[0][0], first[2][1] - first[0][1])
    down0 = (ay, -ax)
    if down0[0] * lift[0] + down0[1] * lift[1] > 0:
        down0 = (-ay, ax)
    heights = sorted(math.hypot(w.corners[2][0] - w.corners[0][0], w.corners[2][1] - w.corners[0][1]) for w in words)
    h = heights[len(heights) // 2]
    rough = Geometry(tuple(first[0]), (ax, ay), down0)
    bx = _boxes(words, rough)
    got = _cut(px, rough, min(b.x for b in bx) - 0.5 * h, max(b.max_x for b in bx) + 0.5 * h,
               min(b.y for b in bx) - 0.6 * h, max(b.max_y for b in bx) + 0.6 * h, False)
    if got is None:
        return None
    s, g0 = got
    bx = _boxes(words, g0)

    r = IntRect.of(bx[0])
    for b in bx:
        r = r.union(IntRect.of(b))
    r = r.grown(0, int(0.25 * h)).clipped(s.width, s.height)
    if r.w <= 4 or r.h <= 4:
        return None
    L = s.lum[r.y:r.max_y, r.x:r.max_x]
    level = _levels(L)
    mid, low, high = level(0.5), level(0.03), level(0.97)
    dark = mid - low >= high - mid
    contrast = mid - low if dark else high - mid
    if contrast < 28:
        return None
    # ink is darker than the paper beside it - not than the line's usual colour: a shadow across the line isn't ink.
    # The paper along the line: the lighter side of each stretch of it, joined up.
    stretch = max(8, int(2 * h))
    count = max(1, (r.w + stretch // 2) // stretch)
    papers = np.full(count, mid, dtype=F32)
    for b in range(count):
        x0, x1 = b * r.w // count, (b + 1) * r.w // count
        if x1 > x0:
            papers[b] = _levels(L[:, x0:x1])(0.7 if dark else 0.3)
    at = (np.arange(r.w, dtype=F32) + 0.5) * count / r.w - 0.5
    b0 = np.clip(np.floor(at).astype(np.int64), 0, count - 1)
    b1 = np.minimum(count - 1, b0 + 1)
    f = np.clip(at - b0, 0, 1)
    sheet = papers[b0] * (1 - f) + papers[b1] * f
    # only what is in the words' own boxes: a rule, a stamp or a picture beside them has edges of its own
    mine = np.zeros((r.h, r.w), bool)
    for b in bx:
        o = IntRect.of(b).grown(int(0.1 * h), int(0.2 * h))
        mine[max(0, o.y - r.y):max(0, o.max_y - r.y), max(0, o.x - r.x):max(0, o.max_x - r.x)] = True
    d = (sheet[None, :] - L) / contrast if dark else (L - sheet[None, :]) / contrast
    sel = mine & (d > 0.15)
    jj, ii = np.nonzero(sel)
    if len(ii) <= 20:
        return None
    dd = np.minimum(1.0, d[sel]).astype(np.float64)
    ii, jj = ii.astype(np.float64), jj.astype(np.float64)
    ic, jc = r.w / 2, r.h / 2

    def rows(k: float) -> float:
        """How sharply the ink falls into rows when each column is slid by `k` rows a pixel."""
        pad = int(abs(k) * ic) + 2
        return _peaked(_profile(jj - k * (ii - ic) + pad, dd, r.h + 2 * pad + 2))

    top = (0.0, rows(0) * 1.02)                    # level, unless a slope is clearly better
    v = -0.27
    while v <= 0.2701:
        sc = rows(v)
        if sc > top[1]:
            top = (v, sc)
        v += 0.01
    if top[0] != 0:
        start = top[0]
        v = start - 0.01
        while v <= start + 0.0101:
            sc = rows(v)
            if sc > top[1]:
                top = (v, sc)
            v += 0.001
    return _Lay((ax, ay), down0, top[0], ii, jj, dd, ic, jc, r.w)


def look(px: np.ndarray, line: list, pick: int, nearby: list = ()):
    """Look at a line of words in a picture; `pick` is the word that was picked.  `nearby`: other lines read in the
    picture - the ones round the picked word help to tell how the letters lean.  -> Place, or None."""
    if not 0 <= pick < len(line) or any(len(w.corners) != 3 for w in line):
        return None
    _drawn_cache.clear()
    heights = sorted(math.hypot(w.corners[2][0] - w.corners[0][0], w.corners[2][1] - w.corners[0][1]) for w in line)
    h = heights[len(heights) // 2]
    if not 7 <= h <= 300:
        return None

    # how the line runs is judged round the picked word: paper that was creased or curling when it was photographed
    # has lines that bend
    def middle(w):
        return ((w.corners[1][0] + w.corners[2][0]) / 2, (w.corners[1][1] + w.corners[2][1]) / 2)
    here = middle(line[pick])

    def far(w):
        m = middle(w)
        return math.hypot(m[0] - here[0], m[1] - here[1])
    local = [line[k] for k in range(len(line)) if abs(k - pick) <= 2 and far(line[k]) <= 6 * h]
    own = _lay(px, local)
    if own is None:
        return None
    first = local[0].corners
    down0 = own.down

    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1]

    def square(a):
        n = (-a[1], a[0])
        return (a[1], -a[0]) if dot(n, down0) < 0 else n
    run = own.run

    # how the letters lean: this line's words and the lines round them together - they lie on the same paper at the
    # same angle to the camera, and one line's few upright strokes can mislead.  Square to the line unless a lean is
    # clearly better.
    lays = [own]
    others = [l for l in nearby if l and all(len(w.corners) == 3 for w in l) and min(far(w) for w in l) <= 10 * h]
    others.sort(key=lambda l: min(far(w) for w in l))
    for l in others[:8]:
        other = _lay(px, l)
        if other is not None and dot(other.run, run) > 0.96:
            lays.append(other)
    upright = square(run)

    def way(turn):
        c, s_ = math.cos(turn), math.sin(turn)
        return (c * upright[0] + s_ * run[0], c * upright[1] + s_ * run[1])

    def score(l, turn):
        t = l.lean(way(turn))
        return l.upright(t) if t is not None else 0.0
    turns = []
    t = -0.46
    while t <= 0.4601:
        turns.append(t)
        t += 0.02
    tops = [max(1e-9, max(score(l, t) for t in turns)) for l in lays]
    total = float(sum(l.ink for l in lays))

    def together(turn):
        return sum(l.ink * score(l, turn) / tops[k] for k, l in enumerate(lays)) / total
    best = (0.0, together(0) * 1.03)
    for turn in turns:
        sc = together(turn)
        if sc > best[1]:
            best = (turn, sc)
    if best[0] != 0:
        start = best[0]
        t = start - 0.02
        while t <= start + 0.0201:
            sc = together(t)
            if sc > best[1]:
                best = (t, sc)
            t += 0.004
    stroke = way(best[0])
    across, n = run, square(run)
    shear = dot(stroke, across) / dot(stroke, n) if dot(stroke, n) > 0.2 else 0.0
    rest = 0.0
    whole = False
    if abs(shear) < 0.02:
        shear = 0.0
        # nearly straight (most scans): straight exactly, so pixels are copied as they are, not blended - and the
        # little slope that's left is drawn into the new words
        snapped = None
        if abs(run[1]) < 0.012:
            snapped = (-1.0 if run[0] < 0 else 1.0, 0.0)
        if abs(run[0]) < 0.012:
            snapped = (0.0, -1.0 if run[1] < 0 else 1.0)
        if snapped:
            across = snapped
            n = square(snapped)
            rest = dot(run, n) / dot(run, across)
            whole = True
    down = (n[0] + shear * across[0], n[1] + shear * across[1])

    # the strip: the whole line, with room either side and above and below
    g = Geometry(tuple(first[0]), across, down)
    bx = _boxes(line, g)
    mid = bx[pick].mid_y
    lo, hi = min(b.x for b in bx), max(b.max_x for b in bx)
    u0, u1 = lo - 2 * h, hi + 18 * h
    H, W = px.shape[:2]

    def inside(u):                                  # no further than the picture goes
        p = g.picture(u, mid)
        return 0 <= p[0] <= W and 0 <= p[1] <= H
    while u0 < lo and not inside(u0):
        u0 += 1
    while u1 > hi and not inside(u1):
        u1 -= 1
    got = _cut(px, g, u0, u1, mid - 2.2 * h, mid + 2.2 * h, whole)
    if got is None:
        return None
    strip, at = got

    # each word's ink
    placed = _boxes(line, at)
    words = [_inked(line[k].text.strip(), placed[k], strip, h, placed[k - 1] if k > 0 else None,
                    placed[k + 1] if k + 1 < len(placed) else None) for k in range(len(line))]
    if words[pick] is None:
        return None
    got = _fit(words, pick, rest, strip)
    if got is None:
        return None
    return Place(strip, at, h, words, pick, got[0], got[1], (0, strip.width))


def _reach(marked, core: range, gap: int, limit: range):
    """From marks known to be the word's (`core`), reach outwards over marks no more than `gap` apart, as far as
    `limit`.  -> (from, to), or None."""
    hits = [k for k in core if marked(k)]
    if not hits:
        return None
    lo, hi = hits[0], hits[-1]
    k = lo - 1
    while k >= limit.start and lo - k <= gap + 1:
        if marked(k):
            lo = k
        k -= 1
    k = hi + 1
    while k < limit.stop and k - hi <= gap + 1:
        if marked(k):
            hi = k
        k += 1
    return lo, hi + 1


def _foot(ink: np.ndarray, top: int, body: int, stop: int, gap: int) -> int:
    """The row just past a word's lowest ink.  `ink`: the columns of its box; `body`: a row through its letters.
    Going down from there: the rows of the letters themselves (all their ink is the word's - a comma beside them
    too), then only what hangs from them - a tail, which may be faint enough to break for a row or two (`gap`) -
    and no lower than a tail goes, a little under half the letters' own height.

    Where typed lines are set close a "y"'s tail ends a pixel or two above the tall letters of the line below, and
    there is no clear row between the two lines for the word's ink to stop at: it ran on into the line below, which
    lost its tops when the word was painted out - and the font came out a fifth too large (a camera scan, Nov
    2026).  Those letters hang from nothing of this word's.  (In ScanPatch.swift since iOS build 29.)"""
    live = ink[body].copy()
    widest = int(live.sum())
    foot, letters, missed, tail = body + 1, body, 0, False
    for r in range(body + 1, min(stop, ink.shape[0])):
        row = ink[r]
        near = live.copy()
        for k in range(1, 2 + missed):                  # the ink above, a column further out for each row missed
            near[k:] |= live[:-k]
            near[:-k] |= live[k:]
        touch = row & near
        if not touch.any():
            missed += 1
            if missed > gap:
                break
            continue
        if not tail and not missed and int(touch.sum()) * 4 >= widest:
            live, letters = row.copy(), r               # still the letters: the whole row is the word's
            widest = max(widest, int(row.sum()))
        else:                                           # a tail: only the ink that joins what is above it
            tail = True
            if r - letters > max(2, round(0.45 * (letters - top + 1))):
                break
            live = np.zeros_like(row)
            k, n = 0, len(row)
            while k < n:
                if not row[k]:
                    k += 1
                    continue
                e = k
                while e < n and row[e]:
                    e += 1
                if touch[k:e].any():
                    live[k:e] = True
                k = e
        missed = 0
        foot = r + 1
    return foot


def _inked(text: str, box: Rect, s: Strip, h: float, before, after):
    """Find a word's ink inside (and just round) the box it was read in, and the paper round it."""
    b = IntRect.of(box).clipped(s.width, s.height)
    if b.w < 2 or b.h < 3 or not text:
        return None
    # paper and ink: most of a word's box is paper
    level = _levels(s.lum[b.y:b.max_y, b.x:b.max_x])
    mid, low, high = level(0.5), level(0.03), level(0.97)
    dark = mid - low >= high - mid
    contrast = mid - low if dark else high - mid
    if contrast < 28:
        return None

    def ink_at(part: float) -> np.ndarray:
        return s.lum < mid - contrast * part if dark else s.lum > mid + contrast * part
    is_ink = ink_at(0.5)

    # rows: from the middle of the box outwards (an i's dot is the word's; the line above isn't)
    row_limit = range(max(0, b.y - int(0.2 * h)), min(s.height, b.max_y + int(0.25 * h)))
    row_has = is_ink[:, b.x:b.max_x].any(axis=1)
    third = int(b.h * 0.3)
    gap = max(1, int(0.12 * h))
    middle = range(b.y + third, max(b.y + third + 1, b.max_y - third))
    rows = _reach(lambda j: row_has[j], middle, gap, row_limit)
    if rows is not None:
        # below, only what hangs from the letters themselves (not the line below, set close: see _foot)
        body = max(j for j in middle if row_has[j])
        rows = (rows[0], min(rows[1], _foot(is_ink[:, b.x:b.max_x], rows[0], body, row_limit.stop, gap)))
    else:
        rows = _reach(lambda j: row_has[j], range(b.y, b.max_y), gap, row_limit)
    if rows is None:
        return None

    # columns: the ink inside the box - less a sliver of the next word's first letter at either end
    col_any = is_ink[rows[0]:rows[1], :].any(axis=0)

    def col_has(i: int) -> bool:
        return 0 <= i < s.width and bool(col_any[i])
    runs = []
    i = b.x
    while i < b.max_x:
        if not col_has(i):
            i += 1
            continue
        e = i + 1
        while e < b.max_x and (col_has(e) or col_has(e + 1)):
            e += 1
        runs.append((i, e))
        i = e
    sliver, apart = int(0.15 * h), max(2, int(0.2 * h))
    # (only where the next word's box begins hard by this one's edge.  A reader that draws its boxes snug - Tesseract,
    # Windows' own - ends a word's box on its last mark, and the comma of "Sam," was left in the picture as a sliver
    # of a next word that is a word's space away, or isn't there.  In ScanPatch.swift since iOS build 29: Vision's
    # boxes are loose and seldom end on the comma, but can.)
    beside = 0.1 * h
    if len(runs) > 1 and runs[-1][1] == b.max_x and runs[-1][1] - runs[-1][0] <= sliver and runs[-1][0] - runs[-2][1] >= apart \
            and after is not None and after.x - box.max_x < beside:
        runs.pop()
    if len(runs) > 1 and runs[0][0] == b.x and runs[0][1] - runs[0][0] <= sliver and runs[1][0] - runs[0][1] >= apart \
            and before is not None and box.x - before.max_x < beside:
        runs.pop(0)
    if not runs:
        return None
    left, right = runs[0][0], runs[-1][1]
    # a letter the box cuts through: to the letter's edge, not into the next word
    left_limit = max(0, b.x - int(0.3 * h), int((before.max_x if before else -1e6) + 0.05 * h))
    right_limit = min(s.width, b.max_x + int(0.3 * h), int((after.x if after else 1e6) - 0.05 * h))
    while left > left_limit and col_has(left - 1):
        left -= 1
    while right < right_limit and col_has(right):
        right += 1
    ink = IntRect(left, rows[0], right - left, rows[1] - rows[0])
    if ink.w < 2 or ink.h < 3:
        return None

    # the paper round the word: everything that isn't ink (or the soft edge of ink), a flat tilted sheet through it
    pad = max(4, int(0.6 * h))
    win = ink.grown(pad, pad).clipped(s.width, s.height)
    soft = ink_at(0.3)
    near = soft[win.y:win.max_y, win.x:win.max_x]
    spread = near.copy()
    for dj in range(-2, 3):
        for di in range(-2, 3):
            if di or dj:
                spread[max(0, dj):win.h + min(0, dj), max(0, di):win.w + min(0, di)] |= \
                    near[max(0, -dj):win.h - max(0, dj), max(0, -di):win.w - max(0, di)]
    jj, ii = np.nonzero(~spread)
    u, v = (win.x + ii + 0.5).astype(F32), (win.y + jj + 0.5).astype(F32)
    c = s.rgb[win.y + jj, win.x + ii].astype(F32)
    centre = (ink.x + ink.w / 2, ink.y + ink.h / 2)
    plane = Plane(np.full(3, mid, F32))
    grain = np.zeros(3, F32)
    if len(u) >= 12:
        plane = _sheet_through(u, v, c, centre)
        # once more without specks and smudges
        res = np.abs((c - plane.at(u, v)) @ LUM)
        spread_ = max(1.5, float(np.sort(res)[len(res) // 2]) * 1.4826)
        clean = res <= 3 * spread_
        if int(clean.sum()) >= 12:
            u, v, c = u[clean], v[clean], c[clean]
            plane = _sheet_through(u, v, c, centre)
        r = np.sort(np.abs(c - plane.at(u, v)), axis=0)
        grain = np.minimum(12, r[len(u) // 2] * 1.4826).astype(F32)
    else:
        # hardly any paper to go by: the box's usual colour
        plain = ~soft[b.y:b.max_y, b.x:b.max_x]
        if plain.any():
            plane = Plane(s.rgb[b.y:b.max_y, b.x:b.max_x][plain].astype(F32).mean(axis=0))

    # the ink's colour: the darkest fifth of it (the middles of the strokes)
    part = is_ink[ink.y:ink.max_y, ink.x:ink.max_x]
    cols = s.rgb[ink.y:ink.max_y, ink.x:ink.max_x][part].astype(F32)
    if len(cols) < 3:
        return None
    ls = cols @ LUM
    order = np.argsort(ls if dark else -ls, kind="stable")
    core = cols[order[:max(3, len(cols) // 5)]]
    return Inked(text, box, ink, plane, core.mean(axis=0), grain)


def _sheet_through(u, v, c, centre) -> Plane:
    """The flat, tilted sheet nearest to these points' colours (least squares, each of red, green and blue)."""
    U, V, C = u.astype(np.float64) - centre[0], v.astype(np.float64) - centre[1], c.astype(np.float64)
    n = float(len(U))
    su, sv, suu, suv, svv = U.sum(), V.sum(), (U * U).sum(), (U * V).sum(), (V * V).sum()
    sc, scu, scv = C.sum(axis=0), (C * U[:, None]).sum(axis=0), (C * V[:, None]).sum(axis=0)
    flat = Plane((sc / n).astype(F32), u0=centre[0], v0=centre[1])
    # [n su sv; su suu suv; sv suv svv] · [a bu bv] = [sc scu scv]
    det = n * (suu * svv - suv * suv) - su * (su * svv - suv * sv) + sv * (su * suv - suu * sv)
    if not abs(det) > 1e-6 * max(1.0, n * suu * svv):
        return flat
    a = (sc * (suu * svv - suv * suv) - scu * (su * svv - suv * sv) + scv * (su * suv - suu * sv)) / det
    bu = (scu * (n * svv - sv * sv) - sc * (su * svv - sv * suv) - scv * (n * suv - su * sv)) / det
    bv = (scv * (n * suu - su * su) + sc * (su * suv - sv * suu) - scu * (n * suv - su * sv)) / det
    # a wild tilt (a few points, all to one side): keep it flat
    span, rise = max(float(u.max() - u.min()), 1) * 0.5, max(float(v.max() - v.min()), 1) * 0.5
    if (np.abs(bu) * span > 40).any() or (np.abs(bv) * rise > 40).any():
        return flat
    return Plane(a.astype(F32), bu.astype(F32), bv.astype(F32), centre[0], centre[1])


# ---------------------------------------------------------------- drawing words

_glyphs: dict = {}
_drawn_cache: dict = {}


def can_draw(text: str, face: Face) -> bool:
    f = font(face)
    return all(ch.isspace() or f.has_glyph(ord(ch)) for ch in text)


def _advance(text: str, face: Face) -> float:
    """How far a line of these letters runs, for a font 1 high."""
    return font(face).text_length(text, fontsize=1)


def _shape(text: str, face: Face):
    """The letters' own shape (not the room round them) at size 100: (x0, y0, x1, y1), x from the line's start, y up
    from the baseline.  None when there is nothing to see."""
    f = font(face)
    x = 0.0
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    for ch in text:
        key = (face.key, ch)
        g = _glyphs.get(key)
        if g is None:
            code = ord(ch)
            bb = f.glyph_bbox(code)
            g = _glyphs[key] = (f.glyph_advance(code), bb.x0, bb.y0, bb.x1, bb.y1)
        adv, gx0, gy0, gx1, gy1 = g
        if gx1 > gx0 and gy1 > gy0 and not ch.isspace():
            x0, x1 = min(x0, x + gx0), max(x1, x + gx1)
            y0, y1 = min(y0, gy0), max(y1, gy1)
        x += adv
    if x1 <= x0:
        return None
    return (x0 * 100, y0 * 100, x1 * 100, y1 * 100)


def _drawn(text: str, face: Face, size: float, xscale: float, origin, window: IntRect, blur: float, weight: float = 0.0,
           slope: float = 0.0) -> np.ndarray:
    """How much of each pixel of `window` the words cover, 0...1, with the line starting at `origin` (strip x and
    baseline row), leaning by `slope` (rows down for each pixel across) and softened by `blur`.  `weight`: fatter
    strokes (by so many hundredths of the size) or, below 0, thinner ones."""
    if window.w <= 0 or window.h <= 0:
        return np.zeros((0, 0), F32)
    key = (text, face.key, round(size, 3), round(weight, 3), round(xscale, 4), round(slope, 5),
           round(origin[0] - window.x, 3), round(origin[1] - window.y, 3), window.w, window.h)
    raw = _drawn_cache.get(key)
    if raw is None:
        doc = fitz.open()
        try:
            pg = doc.new_page(width=window.w, height=window.h)
            at = fitz.Point(origin[0] - window.x, origin[1] - window.y)
            turn = math.atan(slope)
            # wider or narrower, then turned to run down the slope (PyMuPDF's morph turns the PDF's way, y up)
            mat = fitz.Matrix(xscale, 0, 0, 1, 0, 0) * fitz.Matrix(math.cos(turn), -math.sin(turn), math.sin(turn), math.cos(turn), 0, 0)
            kw = dict(fontsize=size, fontname=face.key, morph=(at, mat))
            if abs(weight) > 0.01:
                # a line round each letter over its filling: in the letters' own black it makes them fatter, in the
                # paper's white it eats them thinner
                pg.insert_text(at, text, render_mode=2, fill=(0, 0, 0), color=(0, 0, 0) if weight > 0 else (1, 1, 1),
                               border_width=abs(weight) / 100, **kw)
            else:
                pg.insert_text(at, text, color=(0, 0, 0), **kw)
            k = DRAW_SCALE
            pix = pg.get_pixmap(matrix=fitz.Matrix(k, k), colorspace=fitz.csGRAY, alpha=False)
            a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:window.h * k, :window.w * k]
            raw = (1.0 - a.reshape(window.h, k, window.w, k).mean(axis=(1, 3), dtype=F32) / 255).astype(F32)
        finally:
            doc.close()
        if len(_drawn_cache) > 4000:
            _drawn_cache.clear()
        _drawn_cache[key] = raw
    return soften(raw, blur) if blur > 0.05 else raw


def soften(a: np.ndarray, sigma: float) -> np.ndarray:
    """A Gaussian blur (nothing beyond the edges)."""
    r = max(1, math.ceil(sigma * 3))
    k = np.exp(-np.arange(-r, r + 1, dtype=np.float64) ** 2 / (2 * sigma * sigma))
    k = (k / k.sum()).astype(F32)
    h, w = a.shape
    pad = np.zeros((h, w + 2 * r), F32)
    pad[:, r:r + w] = a
    t = np.zeros((h, w), F32)
    for n in range(2 * r + 1):
        t += pad[:, n:n + w] * k[n]
    pad = np.zeros((h + 2 * r, w), F32)
    pad[r:r + h] = t
    out = np.zeros((h, w), F32)
    for n in range(2 * r + 1):
        out += pad[n:n + h] * k[n]
    return out


def _alike(a: np.ndarray, b: np.ndarray, dx: int = 0, dy: int = 0) -> float:
    """How alike two drawings are, 0...1 (what they share over what either has), with `b` moved by (dx, dy)."""
    if dx or dy:
        h, w = b.shape
        moved = np.zeros_like(b)
        moved[max(0, dy):h + min(0, dy), max(0, dx):w + min(0, dx)] = b[max(0, -dy):h - max(0, dy), max(0, -dx):w - max(0, dx)]
        b = moved
    either = float(np.maximum(a, b).sum())
    return float(np.minimum(a, b).sum()) / either if either > 0 else 0.0


def _old(w: Inked, s: Strip, window: IntRect) -> np.ndarray:
    """The old word's ink, 0...1, in a window round it (ink outside its own box isn't its own)."""
    out = np.zeros((window.h, window.w), F32)
    own = w.ink.grown(1, 1)
    x0, x1, y0, y1 = max(window.x, own.x), min(window.max_x, own.max_x), max(window.y, own.y), min(window.max_y, own.max_y)
    if x0 < x1 and y0 < y1:
        out[y0 - window.y:y1 - window.y, x0 - window.x:x1 - window.x] = w.cover(s, x0, x1, y0, y1)
    return out


def _depth(a: np.ndarray) -> float:
    """How dark the middles of a drawing's strokes get, 0...1 (soft letters never reach full ink): the darkest fifth
    of what is at least half as dark as the darkest - measured as a scan's own ink colour is."""
    top = float(a.max()) if a.size else 0.0
    if not top > 0.05:
        return 1.0
    inked = np.sort(a[a > top * 0.5])[::-1]
    core = inked[:max(3, len(inked) // 5)]
    return max(0.05, float(core.mean()))


def _levelled(a: np.ndarray) -> np.ndarray:
    """A drawing as a scan shows it: its stroke middles count as full ink."""
    return np.minimum(1, a / _depth(a))


def _extent(a: np.ndarray):
    """The box round everything at least half inked, to a fraction of a pixel (the window's own coordinates)."""
    if not a.size:
        return None

    def edges(m):
        on = np.nonzero(m >= 0.5)[0]
        if not len(on):
            return None
        first, last = int(on[0]), int(on[-1])
        before = float(m[first - 1]) if first > 0 else 0.0
        after = float(m[last + 1]) if last + 1 < len(m) else 0.0
        return (first + 0.5 - (float(m[first]) - 0.5) / max(0.001, float(m[first]) - before),
                last + 0.5 + (float(m[last]) - 0.5) / max(0.001, float(m[last]) - after))
    x, y = edges(a.max(axis=0)), edges(a.max(axis=1))
    if x is None or y is None or x[1] - x[0] <= 0.5 or y[1] - y[0] <= 0.5:
        return None
    return Rect(x[0], y[0], x[1] - x[0], y[1] - y[0])


def _texture(a: np.ndarray, box: Rect):
    """Two things about a drawing that don't need it lined up with anything: how much of its box is ink (fat or thin
    strokes), and how sharp its edges are (the steepest tenth of them, in ink per pixel)."""
    v = np.where(a < 0.1, 0, a)
    g = np.sqrt((v[:-1, 1:] - v[:-1, :-1]) ** 2 + (v[1:, :-1] - v[:-1, :-1]) ** 2)
    steps = np.sort(g[g > 0.08])
    edge = float(steps[min(len(steps) - 1, len(steps) * 9 // 10)]) if len(steps) else 0.0
    return float(v.sum()) / max(1.0, box.w * box.h), edge


@dataclass
class _Trial:
    alike: float
    size: float
    xs: float
    origin: tuple
    fill: float             # of the drawing with its stroke middles counted as full ink
    edge: float
    peak: float             # how dark the middles of its strokes get as drawn, 0...1


def _trial(w: Inked, face: Face, was: np.ndarray, window: IntRect, blur: float, weight: float = 0.0, size=None, xs=None,
           slope: float = 0.0, compare: bool = True):
    """Draw the old word in a font so that its ink is as tall and as wide as the scan's, sitting where the scan's
    sits, and see how alike the two are.  `size` and `xs` given: only where it sits is found."""
    b = _shape(w.text, face)
    old = _extent(was)
    if b is None or old is None or b[2] - b[0] <= 1 or b[3] - b[1] <= 1:
        return None
    fixed, fixed_xs = size, xs
    bw, bh = b[2] - b[0], b[3] - b[1]
    size = fixed if fixed is not None else old.h / (bh / 100)
    xs = fixed_xs if fixed_xs is not None else old.w / (bw / 100 * size)
    if not (0.6 < xs < 1.6 and size >= 5):
        return None
    ox, oy = window.x, window.y                    # strip coordinates of the window's own
    origin = (ox + old.x - b[0] / 100 * size * xs, oy + old.max_y + b[1] / 100 * size)
    raw = now = None
    # drawn, measured the way the scan was, and put right (soft letters measure a little big or small)
    rounds = 1 if fixed is not None and fixed_xs is not None else 2
    for k in range(3):
        raw = _drawn(w.text, face, size, xs, origin, window, blur, weight, slope)
        now = _levelled(raw)
        d = _extent(now) if k < rounds else None
        if d is None:
            break
        tall = old.h / d.h if fixed is None else 1.0
        wide = old.w / d.w if fixed_xs is None else 1.0
        origin = (ox + old.x - (ox + d.x - origin[0]) * wide, oy + old.max_y - (oy + d.max_y - origin[1]) * tall)
        size *= tall
        xs *= wide / tall
    if now is None or not now.size or not 0.6 < xs < 1.6:
        return None
    best = (0.0, 0, 0)
    if compare:
        best = (-1.0, 0, 0)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                a = _alike(was, now, dx, dy)
                if a > best[0]:
                    best = (a, dx, dy)
    fill, edge = _texture(now, old)
    return _Trial(best[0], size, xs, (origin[0] + best[1], origin[1] + best[2]), fill, edge, _depth(raw))


def _median(a: list) -> float:
    s = sorted(a)
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def _tells(w: Inked) -> bool:
    """Letters or figures enough to tell fonts apart."""
    return sum(ch.isalnum() for ch in w.text) >= 2


BOLD_OF = {"hebo": "helv", "tibo": "tiro", "cobo": "cour"}      # a bold font and its family's plain one
BOLD_LEAD = 0.05            # bold's score must be this far ahead of plain's to be the font


def _fit(words: list, pick: int, slope: float, s: Strip):
    """The font nearest the line's own: each is tried on the picked word and the words beside it."""
    target = words[pick]
    if target is None:
        return None
    # the picked word and up to four near it
    near = sorted((k for k in range(len(words)) if k != pick and words[k] is not None and _tells(words[k])),
                  key=lambda k: abs(k - pick))
    used = [words[k] for k in near[:4]]
    if _tells(target) or not used:
        used.insert(0, target)
    pad = max(4, target.ink.h // 6)
    windows = [w.ink.grown(pad, pad).clipped(s.width, s.height) for w in used]
    was = [_old(w, s, win) for w, win in zip(used, windows)]
    guess = max(0.6, min(1.6, target.ink.h * 0.04))

    best = None
    scores = []
    tried_as = {}
    for face in FACES:
        trials = [_trial(used[k], face, was[k], windows[k], guess) for k in range(len(used))]
        good = [t for t in trials if t is not None]
        if len(good) * 2 <= len(used):
            continue
        # alike in shape, and no need to stretch the font to make it fit
        score = sum(t.alike - 0.8 * abs(math.log(t.xs)) for t in good) / len(good)
        scores.append((face, score))
        tried_as[face.key] = (score, face, trials)
        if best is None or score > best[0]:
            best = (score, face, trials)
    if best is None:
        return None
    # bold only when it leads its family's plain clearly.  On a small or soft scan bold draws nearly as like plain
    # words as plain does, and the two tie: on a camera scan of a letter (Nov 2026) plain words gave bold a lead of
    # -0.06 to +0.005 and bold headings +0.14 to +0.20 - and two words of a plain address came out bold.
    # (In ScanPatch.swift since iOS build 29: the same words tie as nearly there.)
    plain = BOLD_OF.get(best[1].key)
    if plain in tried_as and best[0] - tried_as[plain][0] < BOLD_LEAD:
        best = tried_as[plain]
    _, face, trials = best

    def middle(of) -> float:
        """The middle one, each word counting for more the better the font matched it (a misread word matches badly)."""
        every = sorted(((of(t), t.alike * t.alike) for t in trials if t is not None), key=lambda p: p[0])
        half = sum(wt for _, wt in every) / 2
        total = 0.0
        for v, wt in every:
            total += wt
            if total >= half:
                return v
        return every[-1][0] if every else 0.0
    size = middle(lambda t: t.size)
    xscale = middle(lambda t: t.xs)
    # the picked word's own width, at the line's size: its letters are then as far apart as they were
    k = next((n for n, w in enumerate(used) if w.ink == target.ink), 0)
    own = trials[k]
    if used[k].ink == target.ink and _tells(target) and own is not None and abs(own.size / size - 1) < 0.15:
        xscale = own.xs * own.size / size
    xscale = min(1.25, max(0.8, xscale))
    fitted = Fit(face, size, xscale, guess, 0.0, 0.0, slope, None, 1.0, 0.0)

    # the line the letters sit on: through each word's own, at the slope the ink runs at
    pts = [t.origin[1] - slope * t.origin[0] for t in trials if t is not None]
    fitted.base_a = _median(pts) if pts else target.ink.max_y - slope * target.ink.x

    # softness and weight: the picked word and the words beside it drawn each way, and the way kept whose ink looks
    # most like the scan's - edges as sharp, as much ink (neither needs the two lined up to the pixel)
    seen = []                                       # (which word, fill, edge, need)
    for m in range(min(3, len(used))):
        box = _extent(was[m])
        if trials[m] is None or box is None:
            continue
        fill, edge = _texture(was[m], box)
        if not (fill > 0 and edge > 0):
            continue
        # the scan's strokes are this dark in the middle: a drawing too soft or thin to get so dark even in the
        # blackest ink is no match (it would come out grey)
        w = used[m]
        sheet = float(lum(w.paper.at(w.ink.x + w.ink.w / 2, w.ink.y + w.ink.h / 2)))
        core = float(lum(w.colour))
        seen.append((m, fill, edge, min(0.97, abs(sheet - core) / max(1.0, sheet if core < sheet else 255 - sheet))))
    if not seen:
        return fitted, scores

    def tried(m, blur, weight, compare=False):
        return _trial(used[m], face, was[m], windows[m], blur, weight, size, xscale, slope, compare)

    def off(blur, weight, of) -> float:
        """How far off a way of drawing is, over all the words (`of`: for one word)."""
        total, n = 0.0, 0
        for sn in seen:
            t = tried(sn[0], blur, weight)
            if t is not None:
                total += of(t, sn)
                n += 1
        return total / n if n else math.inf
    blurs = [0.3, 0.5, 0.7, 0.9, 1.1, 1.4, 1.8, 2.4]
    weights = [-4, -2, -1, 0, 1, 2, 3, 4.5, 6]
    how = [guess, 0.0]
    for rnd in range(3):
        if rnd != 1:                                # as sharp at its edges as the scan - and able to get as dark
            how[0] = min(blurs, key=lambda b: off(b, how[1], lambda t, sn: abs(t.edge / sn[2] - 1) + (0.5 if t.peak < sn[3] else 0)))
        else:                                       # as much ink in all - strokes too thin to get dark enough count for less
            how[1] = min(weights, key=lambda wt: off(how[0], wt, lambda t, sn: abs(t.fill * min(1, t.peak / sn[3]) / sn[1] - 1)))
    fitted.blur, fitted.weight = how[0], float(how[1])
    mine = next((sn for sn in seen if used[sn[0]].ink == target.ink), seen[0])
    t = tried(mine[0], how[0], how[1], True)
    if t is not None:
        fitted.likeness = t.alike
        fitted.depth = max(t.peak, mine[3])
        if used[mine[0]].ink == target.ink:         # the picked word's own line: where its drawing sat best
            fitted.own = t.origin[1]
    return fitted, scores


# ---------------------------------------------------------------- the target

REACH_UP, REACH_DOWN = 1.15, 0.4        # how far a font's letters go above and below the line they sit on, of its size
                                        # (tallest: an accented capital, about 0.95; lowest: a tail, about 0.25)


def target(kind: str, p: Place):
    """The word that was picked, or its whole line, ready to be changed.  None: it can't be (a line that bends, or
    with a word whose ink couldn't be found)."""
    s = p.strip
    chosen = [w for w in ([p.words[p.pick]] if kind == "word" else p.words) if w is not None]
    if not chosen:
        return None
    first_word, last_word = chosen[0], chosen[-1]
    ink = first_word.ink
    for w in chosen:
        ink = ink.union(w.ink)
    if kind == "line":
        # a whole line is retyped straight: only one that is straight in the picture - every word found, all on one
        # level (not a line that bends over a crease)
        if len(chosen) != len(p.words) or len(chosen) <= 1:
            return None
        if ink.h > 1.35 * max(w.ink.h for w in chosen) + 2:
            return None
    text = first_word.text if kind == "word" else " ".join(w.text for w in p.words if w is not None)

    # no lower than the font's tails reach and no higher than its tallest letter with an accent on it.  Where lines
    # are set close (typed, single spaced) a "y"'s tail all but touches the tall letters of the line below; the ink
    # found for "volleyball" ran on into them, and painting it out took the tops off the line below (a camera scan,
    # Nov 2026).  The line the letters sit on is known by now, to a pixel or two.  (In ScanPatch.swift since iOS build 29.)
    sits = [p.fit.baseline(ink.x), p.fit.baseline(ink.max_x)]
    top, bottom = int(math.floor(min(sits) - REACH_UP * p.fit.size)), int(math.ceil(max(sits) + REACH_DOWN * p.fit.size))
    y0, y1 = max(ink.y, top), min(ink.max_y, bottom)
    if y1 - y0 >= 3 and (y0, y1) != (ink.y, ink.max_y):
        ink = IntRect(ink.x, y0, ink.w, y1 - y0)

    # clear paper round the ink: how far to the next mark each way
    far = int(18 * p.height)
    right = left = up = down = float(far)
    end = min(p.inside[1], ink.max_x + far)
    nxt = None
    if end > ink.max_x + 1:
        marks = (last_word.cover(s, ink.max_x + 1, end, ink.y, ink.max_y) > 0.5).sum(axis=0)
        hit = np.nonzero(marks >= 2)[0]
        if len(hit):
            nxt = ink.max_x + 1 + int(hit[0])
    if nxt is not None:
        right = float(nxt - ink.max_x)
    lo = max(p.inside[0], ink.x - int(2 * p.height))
    if ink.x - 2 >= lo:
        marks = (first_word.cover(s, lo, ink.x - 1, ink.y, ink.max_y) > 0.5).sum(axis=0)
        hit = np.nonzero(marks >= 2)[0]
        if len(hit):
            left = float(ink.x - 1 - (lo + int(hit[-1])))
    lo = max(0, ink.y - int(p.height))
    if ink.y - 2 >= lo:
        marks = (first_word.cover(s, ink.x, ink.max_x, lo, ink.y - 1) > 0.5).sum(axis=1)
        hit = np.nonzero(marks >= 2)[0]
        if len(hit):
            up = float(ink.y - 1 - (lo + int(hit[-1])))
    hi = min(s.height, ink.max_y + int(p.height))
    if hi > ink.max_y + 1:
        marks = (first_word.cover(s, ink.x, ink.max_x, ink.max_y + 1, hi) > 0.5).sum(axis=1)
        hit = np.nonzero(marks >= 2)[0]
        if len(hit):
            down = float(ink.max_y + 1 + int(hit[0]) - ink.max_y)
    # new words stop a word's space short of the next mark - or half a line short of the picture's edge
    gap = min(right, max(0.25 * p.fit.size, 0.6 * right))
    room = nxt - gap if nxt is not None else end - 0.5 * p.height
    # the ink is the line's own: the colour of the darker of the words round the target (one short word's few strokes
    # can read pale)
    around = [p.words[k] for k in range(len(p.words)) if abs(k - p.pick) <= 2 and p.words[k] is not None] if kind == "word" else chosen
    around = sorted(around, key=lambda w: -abs(float(lum(w.colour)) - float(lum(w.paper.a))))
    colour = around[len(around) // 3].colour
    grain = sum((w.grain for w in chosen), np.zeros(3, F32)) / len(chosen)
    return Target(kind, text, ink, [(w.ink.x + w.ink.w / 2, w.paper) for w in chosen], colour, grain.astype(F32),
                  (left, right, up, down), room)


# ---------------------------------------------------------------- composing

@dataclass
class Composed:
    """What to put in the picture: paper over the old ink, and the new words."""
    region: IntRect         # the part of the strip that changes
    paper: np.ndarray       # new paper (with its grain), where `cover` says to use it
    cover: np.ndarray
    alpha: np.ndarray       # the new words' ink, 0...1
    ink: np.ndarray
    face: Face
    size: float             # strip pixels
    squeezed: bool          # set narrower to fit before the next word
    text_box: Rect | None   # the new words' box in the strip (None: the old ones are just taken away)
    pieces: list            # [(text, Rect)]: each new word and the room its letters are set in


def _bells(seed: int, n: int) -> np.ndarray:
    """A steady source of grain (the same paint for the same place every time): n bell-shaped numbers round 0."""
    with np.errstate(over="ignore"):
        z = np.uint64(seed & 0xFFFFFFFFFFFFFFFF) + np.arange(1, 2 * n + 1, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15)
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        z = z ^ (z >> np.uint64(31))
    f = (z >> np.uint64(40)).astype(np.float64) / float(1 << 24)
    a, b = np.maximum(1e-7, f[0::2]), f[1::2]
    return (np.sqrt(-2 * np.log(a)) * np.cos(2 * math.pi * b)).astype(F32)


def compose(p: Place, t: Target, text: str, face: Face | None = None, size: float | None = None) -> Composed:
    """Paper over the target's old ink and `text` drawn in its place.  `face` and `size` (strip pixels): the person's
    own choice, else as found.  Raises TooLong or Letters."""
    s = p.strip
    face = face or p.fit.face
    size = size or p.fit.size
    words = " ".join(text.replace("\n", " ").split(" ")).strip()
    xs = p.fit.xscale
    squeezed = False
    text_box = None
    origin = (0.0, 0.0)
    pieces = []
    if words:
        b = _shape(words, face) if can_draw(words, face) else None
        if b is None or b[2] - b[0] <= 0.5:
            raise Letters()
        bw, bh = b[2] - b[0], b[3] - b[1]
        wide = bw / 100 * size * xs
        space = t.room - t.ink.x
        if wide > space:
            if not (space > 0 and space / wide >= 0.8):
                raise TooLong()
            xs *= space / wide
            squeezed = True
        own = p.fit.own if t.kind == "word" else None
        origin = (t.ink.x - b[0] / 100 * size * xs, own if own is not None else p.fit.baseline(t.ink.x))
        w = bw / 100 * size * xs
        drop = abs(p.fit.base_b) * w
        text_box = Rect(t.ink.x, origin[1] - b[3] / 100 * size - drop, w, bh / 100 * size + 2 * drop)
        # each new word's place (for the words written invisibly over a scan)
        f = font(face)
        asc, desc = f.ascender * size, -f.descender * size
        at = 0
        for part in words.split():
            k = words.find(part, at)
            if k < 0:
                continue
            at = k + len(part)
            x0, x1 = _advance(words[:k], face) * size * xs, _advance(words[:at], face) * size * xs
            pieces.append((part, Rect(origin[0] + x0, origin[1] + p.fit.base_b * x0 - asc, x1 - x0, asc + desc)))

    # how far the paint reaches past the old ink, and where it fades out
    h = float(t.ink.h)
    full = min(max(1.5, 0.06 * h), 3) + p.fit.blur
    fade = full + max(1.5, 0.08 * h)

    def edge(clear):
        out = min(fade, max(1, clear - 1))
        return (min(full, max(0.5, out - 1)), out)
    l, r, u, d = (edge(c) for c in t.clear)
    soft = math.ceil(p.fit.blur * 3) + 3
    region = t.ink.grown(math.ceil(fade) + 1, math.ceil(fade) + 1)
    if text_box is not None:
        region = region.union(IntRect.of(text_box).grown(soft, soft))
    region = region.clipped(s.width, s.height)
    if region.empty:
        raise Letters()

    n = region.w * region.h
    noise = soften(_bells(region.x * 73856093 ^ region.y * 19349663 ^ region.w * 83492791, n).reshape(region.h, region.w), 0.6)
    spread = float(np.sqrt((noise * noise).sum() / max(1, n)))
    if spread > 0:
        noise = noise / spread

    alpha = np.zeros((region.h, region.w), F32)
    if words:
        # letters set closer have thinner uprights: some of that is given back in weight, or they look pale
        squeeze = xs / p.fit.xscale
        alpha = _drawn(words, face, size, xs, origin, region, p.fit.blur,
                       p.fit.weight + (5 * (1 - squeeze) / squeeze if squeezed else 0), p.fit.base_b)
    # the ink's own colour: darker than the old strokes' middles by as much as the new strokes' middles fall short of
    # full ink - so the new words come out as dark as the old were (but ink is no blacker than black)
    sheet = t.paper(t.ink.x + t.ink.w / 2, t.ink.y + t.ink.h / 2)
    lp, ls = float(lum(sheet)), float(lum(t.colour))
    need = min(0.97, abs(lp - ls) / max(1.0, lp if ls < lp else 255 - lp))
    real = np.clip(sheet + (t.colour - sheet) / max(_depth(alpha), need), 0, 255).astype(F32)

    def ramp(outside, e):
        return np.where(outside <= e[0], 1.0, np.where(outside >= e[1], 0.0, (e[1] - outside) / (e[1] - e[0]))).astype(F32)
    x = np.arange(region.x, region.max_x, dtype=F32) + 0.5
    y = np.arange(region.y, region.max_y, dtype=F32) + 0.5
    wx = np.where(x < t.ink.x, ramp(t.ink.x - x, l), np.where(x > t.ink.max_x, ramp(x - t.ink.max_x, r), 1.0))
    wy = np.where(y < t.ink.y, ramp(t.ink.y - y, u), np.where(y > t.ink.max_y, ramp(y - t.ink.max_y, d), 1.0))
    cover = (wy[:, None] * wx[None, :]).astype(F32)
    paper = (t.paper(x[None, :], y[:, None]) + t.grain * noise[..., None]).astype(F32)
    ink = (real + t.grain * (noise * 0.6)[..., None]).astype(F32)
    return Composed(region, paper, cover, alpha, ink, face, size, squeezed, text_box, pieces)


def picture(p: Place, c: Composed | None, crop: IntRect):
    """A picture of part of the strip with the change made (or as it is now) - what the person is shown first.
    uint8 (rows, columns, 3), or None."""
    r = crop.clipped(p.strip.width, p.strip.height)
    if r.empty:
        return None
    out = p.strip.rgb[r.y:r.max_y, r.x:r.max_x].astype(F32)
    if c is not None:
        x0, x1, y0, y1 = max(r.x, c.region.x), min(r.max_x, c.region.max_x), max(r.y, c.region.y), min(r.max_y, c.region.max_y)
        if x0 < x1 and y0 < y1:
            a = (slice(y0 - c.region.y, y1 - c.region.y), slice(x0 - c.region.x, x1 - c.region.x))
            b = (slice(y0 - r.y, y1 - r.y), slice(x0 - r.x, x1 - r.x))
            was = out[b]
            base = was + (c.paper[a] - was) * c.cover[a][..., None]
            out[b] = base + (c.ink[a] - base) * c.alpha[a][..., None]
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8)


def view(p: Place, t: Target, c: Composed | None) -> IntRect:
    """The part of the strip worth showing: the change, with the words round it."""
    r = t.ink
    if c is not None:
        r = r.union(c.region)
    return r.grown(int((5 if t.kind == "word" else 1.5) * p.height), int(1.4 * p.height)).clipped(p.strip.width, p.strip.height)


def write(c: Composed, g: Geometry, px: np.ndarray) -> IntRect:
    """Put the change into the picture itself.  Returns the picture's pixels that changed (a box round them)."""
    H, W = px.shape[:2]
    reg = c.region
    corners = [g.picture(reg.x, reg.y), g.picture(reg.max_x, reg.y), g.picture(reg.x, reg.max_y), g.picture(reg.max_x, reg.max_y)]
    x0, x1 = max(0, math.floor(min(p[0] for p in corners))), min(W, math.ceil(max(p[0] for p in corners)))
    y0, y1 = max(0, math.floor(min(p[1] for p in corners))), min(H, math.ceil(max(p[1] for p in corners)))
    if x0 >= x1 or y0 >= y1:
        return IntRect(0, 0, 0, 0)
    w, h = reg.w, reg.h
    # where each pixel's middle is among the strip's pixel middles
    X = (np.arange(x0, x1, dtype=np.float64) + 0.5)[None, :] - g.origin[0]
    Y = (np.arange(y0, y1, dtype=np.float64) + 0.5)[:, None] - g.origin[1]
    det = g.across[0] * g.down[1] - g.across[1] * g.down[0]
    u = (X * g.down[1] - Y * g.down[0]) / det - 0.5 - reg.x
    v = (g.across[0] * Y - g.across[1] * X) / det - 0.5 - reg.y
    fu, fv = np.floor(u), np.floor(v)
    i, j = fu.astype(np.int64), fv.astype(np.int64)
    tu, tv = (u - fu).astype(F32), (v - fv).astype(F32)
    shape = u.shape
    paper, ink = np.zeros(shape + (3,), F32), np.zeros(shape + (3,), F32)
    cover, alpha, total = np.zeros(shape, F32), np.zeros(shape, F32), np.zeros(shape, F32)
    for di, dj, wt in ((0, 0, (1 - tu) * (1 - tv)), (1, 0, tu * (1 - tv)), (0, 1, (1 - tu) * tv), (1, 1, tu * tv)):
        a, b = i + di, j + dj
        ok = (wt > 0.001) & (a >= 0) & (b >= 0) & (a < w) & (b < h)
        wt = np.where(ok, wt, 0).astype(F32)
        a, b = np.clip(a, 0, w - 1), np.clip(b, 0, h - 1)
        paper += c.paper[b, a] * wt[..., None]
        ink += c.ink[b, a] * wt[..., None]
        cover += c.cover[b, a] * wt
        alpha += c.alpha[b, a] * wt
        total += wt
    change = (total > 0.001) & ((cover > 0.001) | (alpha > 0.001))
    if not change.any():
        return IntRect(x0, y0, x1 - x0, y1 - y0)
    safe_total = np.maximum(total, 1e-6)[..., None]
    was = px[y0:y1, x0:x1].astype(F32)
    base = was + (paper / safe_total - was) * cover[..., None]
    new = np.clip(np.floor(base + (ink / safe_total - base) * alpha[..., None] + 0.5), 0, 255).astype(np.uint8)
    part = px[y0:y1, x0:x1]
    part[change] = new[change]
    return IntRect(x0, y0, x1 - x0, y1 - y0)
