#!/bin/bash
# Build the Mac app: <Name>.app -> signed -> <Name>-<version>.dmg -> notarised (when set up).  Run on a Mac:
#
#   build/build_mac.sh                      name and version from brand.json
#   build/build_mac.sh version=0.2.0        a one-off version (edit brand.json to change it for good)
#   build/build_mac.sh name="Other Name"    a one-off name
#   build/build_mac.sh nonotary             sign but don't send to Apple (quicker, for trying it on this Mac)
#
# Needs: Homebrew's python@3.12, tesseract, ffmpeg (brew install python@3.12 tesseract ffmpeg).
# Signing: a "Developer ID Application" certificate in the login keychain (Xcode -> Settings -> Accounts ->
# Manage Certificates -> + ).  Without one the app is signed ad hoc: fine on this Mac, other Macs need
# System Settings -> Privacy & Security -> Open Anyway.
# Notarising, once:  xcrun notarytool store-credentials tidypaper-notary --apple-id <you@example.com> \
#                      --team-id <TEAMID> --password <app-specific password from appleid.apple.com>
# Output in build/dist-mac:  <Name>.app,  <Name>-<version>.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DIST="$BUILD/dist-mac"
WORK="$BUILD/work-mac"
NOTARY_PROFILE="${NOTARY_PROFILE:-tidypaper-notary}"
NOTARISE=1

for arg in "$@"; do
  case "$arg" in
    name=*) export BRAND_NAME="${arg#name=}" ;;
    version=*) export BRAND_VERSION="${arg#version=}" ;;
    nonotary) NOTARISE=0 ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m[x] %s\033[0m\n' "$*"; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "This builds the Mac app - run it on a Mac."
[ "$(uname -m)" = "arm64" ] || fail "Build on an Apple Silicon Mac (the app is built for Apple chips)."

# ---- Python: the project's .venv, made with Homebrew's python@3.12 (3.14 is too new for some packages)
PY="$ROOT/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  say "[1/7] Making the Python environment (.venv)..."
  BASE="$(command -v python3.12 || echo /opt/homebrew/bin/python3.12)"
  [ -x "$BASE" ] || fail "Needs Python 3.12:  brew install python@3.12"
  "$BASE" -m venv "$ROOT/.venv"
else
  say "[1/7] Python environment found."
fi
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt"
for prog in tesseract ffmpeg; do
  command -v "$prog" >/dev/null || [ -x "/opt/homebrew/bin/$prog" ] || fail "$prog isn't installed:  brew install $prog"
done

NAME="$("$PY" "$ROOT/brand.py" name)"
STEM="$("$PY" "$ROOT/brand.py" stem)"
VERSION="$("$PY" "$ROOT/brand.py" version)"
APP="$DIST/$STEM.app"
DMG="$DIST/$STEM-$VERSION.dmg"
echo "    $NAME $VERSION"

# ---- icon: build/icon.png -> build/icon.icns
if [ ! -f "$BUILD/icon.icns" ] || [ "$BUILD/icon.png" -nt "$BUILD/icon.icns" ]; then
  say "[2/7] Making the app icon..."
  [ -f "$BUILD/icon.png" ] || "$PY" "$BUILD/make_icon.py"
  SET="$WORK/icon.iconset"; rm -rf "$SET"; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$BUILD/icon.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$BUILD/icon.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$BUILD/icon.icns"
else
  say "[2/7] App icon found."
fi

# ---- bundle
say "[3/7] Bundling the app with PyInstaller (a few minutes)..."
rm -rf "$APP" "$DIST/$STEM"
"$PY" -m PyInstaller "$BUILD/tidypaper_mac.spec" --noconfirm --log-level WARN --workpath "$WORK" --distpath "$DIST"
[ -d "$APP" ] || fail "PyInstaller finished but $STEM.app is missing."
rm -rf "$DIST/$STEM"                       # the plain folder PyInstaller leaves next to the .app

# the bundled programs must run with nothing from Homebrew (a family Mac won't have it)
for prog in tesseract ffmpeg; do
  BIN="$APP/Contents/Frameworks/bin/$prog"
  [ -x "$BIN" ] || fail "$prog is missing from the bundle."
  if otool -L "$BIN" | grep -q "/opt/homebrew\|/usr/local/"; then
    otool -L "$BIN" | grep "/opt/homebrew\|/usr/local/" | head -5
    fail "$prog still points at Homebrew's libraries."
  fi
done

# ---- sign: every library and program inside, then the app (hardened runtime: needed for notarising)
IDENTITY="${SIGN_IDENTITY:-$(security find-identity -v -p codesigning | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)}"
ENT="$BUILD/mac_entitlements.plist"
if [ -n "$IDENTITY" ]; then
  say "[4/7] Signing as $IDENTITY..."
  SIGN=(codesign --force --timestamp --options runtime --sign "$IDENTITY")
else
  say "[4/7] No Developer ID certificate - signing ad hoc (works on this Mac; others need Open Anyway)."
  SIGN=(codesign --force --sign -)
  NOTARISE=0
fi
# inside out: Mach-O files first (libraries, Python extensions, the bundled programs), then the app itself
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    case "$f" in
      */Contents/MacOS/*|*/Frameworks/bin/*) "${SIGN[@]}" --entitlements "$ENT" "$f" 2>&1 | grep -v "replacing existing signature" || true ;;
      *) "${SIGN[@]}" "$f" 2>&1 | grep -v "replacing existing signature" || true ;;
    esac
  fi
done < <(find "$APP/Contents" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0)
"${SIGN[@]}" --entitlements "$ENT" "$APP" 2>&1 | grep -v "replacing existing signature" || true
codesign --verify --deep --strict "$APP" || fail "The signature didn't verify."

# ---- try it: the bundled programs run, and the app starts (quits by itself after a moment)
say "[5/7] Checking the bundle..."
env -i HOME="$HOME" PATH=/usr/bin:/bin "$APP/Contents/Frameworks/bin/tesseract" --list-langs 2>&1 | grep -q "^eng$" \
  || TESSDATA_PREFIX="$APP/Contents/Resources/tessdata" env -i HOME="$HOME" PATH=/usr/bin:/bin \
       TESSDATA_PREFIX="$APP/Contents/Resources/tessdata" "$APP/Contents/Frameworks/bin/tesseract" --list-langs 2>&1 | grep -q "^eng$" \
  || fail "The bundled Tesseract doesn't run."
env -i HOME="$HOME" PATH=/usr/bin:/bin "$APP/Contents/Frameworks/bin/ffmpeg" -hide_banner -version >/dev/null 2>&1 \
  || fail "The bundled FFmpeg doesn't run."
echo "    Tesseract and FFmpeg run without Homebrew."
du -sh "$APP" | awk '{print "    App size: " $1}'

# ---- the install guide to send with the link (Mac edition)
"$PY" "$ROOT/docs/make_install_guide.py" --mac "$DIST" | sed 's/^/    /'

# ---- disk image: the app plus an Applications shortcut to drag it onto
say "[6/7] Making $STEM-$VERSION.dmg..."
STAGE="$WORK/dmg"; rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$DMG" >/dev/null
rm -rf "$STAGE"
[ -n "$IDENTITY" ] && codesign --force --timestamp --sign "$IDENTITY" "$DMG"
du -h "$DMG" | awk '{print "    " $1 "  " $2}'

# ---- notarise: Apple checks it, then the ticket is stapled on so it opens without asking, even offline
if [ "$NOTARISE" = 1 ]; then
  if xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    say "[7/7] Sending to Apple to notarise (usually a few minutes)..."
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait | tee "$WORK/notary.txt"
    grep -q "status: Accepted" "$WORK/notary.txt" || fail "Apple didn't accept it - see:  xcrun notarytool log <id> --keychain-profile $NOTARY_PROFILE"
    xcrun stapler staple "$DMG"
    spctl -a -t open --context context:primary-signature -v "$DMG" 2>&1 | sed 's/^/    /'
  else
    say "[7/7] Not notarised: no saved Apple ID for notarytool (see the top of this script)."
  fi
else
  say "[7/7] Not notarised."
fi

say "Done:  $DMG"
echo "    Try it:  open \"$APP\""
