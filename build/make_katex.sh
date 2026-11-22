#!/bin/bash
# Puts KaTeX (maths in Ask answers; MIT licence, https://katex.org) into ui/katex/ from a pinned npm release,
# checked against the checksum below.  The result is committed, so this only needs running to move to a newer KaTeX:
#   change VERSION and SHA, run  build/make_katex.sh  then check an answer with maths in the Ask panel.
# Only the .woff2 fonts are kept (every browser engine the app runs in reads them).
set -euo pipefail
VERSION="0.19.1"                  # released 23 Oct 2026
SHA=24da5a599eb2fcede2e270b99a6ff48bca816beecc95feeb84e8cccbd1bb34fd

HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL "https://registry.npmjs.org/katex/-/katex-$VERSION.tgz" -o "$WORK/katex.tgz"
echo "$SHA  $WORK/katex.tgz" | shasum -a 256 -c - >/dev/null || { echo "Checksum wrong for KaTeX $VERSION" >&2; exit 1; }
tar xzf "$WORK/katex.tgz" -C "$WORK"
OUT="$HERE/ui/katex"
rm -rf "$OUT" && mkdir -p "$OUT/fonts"
cp "$WORK/package/dist/katex.min.js" "$WORK/package/dist/katex.min.css" "$WORK/package/LICENSE" "$OUT/"
cp "$WORK/package/dist/fonts/"*.woff2 "$OUT/fonts/"
echo "$VERSION" > "$OUT/VERSION"
echo "Made $OUT (KaTeX $VERSION)"
