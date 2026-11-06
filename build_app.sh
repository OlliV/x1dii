#!/bin/sh
# Build "X1D II Tether.app" into dist/.
#
#   ./build_app.sh            build the bundle
#   ./build_app.sh --open     build it and launch it
#
# The bundle carries the Python code (lib/ + capture/) and pyusb, and points pyusb at a
# bundled libusb so it does not depend on Homebrew's search paths. ffplay is *not*
# vendored - it drags in all of ffmpeg - so the app finds it on PATH or tells you it is
# missing.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
NAME="X1D II Tether"
BUNDLE_ID="local.x1d2.tether"
VERSION="0.1.0"
DIST="$ROOT/dist"
APP="$DIST/$NAME.app"
RES="$APP/Contents/Resources"

say() { printf '  %s\n' "$*"; }

echo "Building $NAME.app"

# ---- 1. an interpreter with Tk
# Prefer Tk 9: Tk 8.6.12 (python.org 3.11) drops mouse clicks while the app is not the
# active one, so buttons need several clicks. Homebrew's python-tk@3.x brings Tk 9.
# Only tkinter.TkVersion is checked - creating a Tk window here can abort the process.
# The repo's .venv is deliberately not a candidate: the launcher bakes in this path, and a
# bundle must not depend on where the checkout lives.
CANDIDATES="/opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
            /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
            $(ls -r /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null | tr '\n' ' ') \
            /usr/local/bin/python3 $(command -v python3 2>/dev/null || true)"
PYTHON=""
for min_tk in 9.0 0; do
    for cand in $CANDIDATES; do
        [ -x "$cand" ] || continue
        if "$cand" -c "import tkinter,sys; sys.exit(tkinter.TkVersion < $min_tk)" >/dev/null 2>&1; then
            PYTHON="$cand"; break 2
        fi
    done
done
[ -n "$PYTHON" ] || { echo "error: no python3 with tkinter found" >&2; exit 1; }
TK_VERSION=$("$PYTHON" -c 'import tkinter; print(tkinter.TkVersion)')
say "python:  $PYTHON (Tk $TK_VERSION)"
case "$TK_VERSION" in
    8.*) say "warning: Tk $TK_VERSION - before 8.6.13 buttons drop clicks while inactive;" \
             "brew install python-tk@3.13 for Tk 9" ;;
esac

# ---- 2. ffplay (external dependency)
FFPLAY="$(command -v ffplay 2>/dev/null || true)"
if [ -n "$FFPLAY" ]; then
    FFPLAY_DIR=$(dirname "$FFPLAY")
    say "ffplay:  $FFPLAY"
else
    FFPLAY_DIR=""
    say "ffplay:  NOT FOUND - the app will report this at startup"
fi

# ---- 3. lay out the bundle
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/app" "$RES/vendor"

cp -R "$ROOT/lib" "$ROOT/capture" "$RES/app/"
find "$RES/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---- 4. vendor pyusb
USB_PARENT=$("$PYTHON" -c 'import usb,os;print(os.path.dirname(os.path.dirname(usb.__file__)))' 2>/dev/null || true)
if [ -z "$USB_PARENT" ] && [ -x "$ROOT/.venv/bin/python" ]; then
    USB_PARENT=$("$ROOT/.venv/bin/python" -c 'import usb,os;print(os.path.dirname(os.path.dirname(usb.__file__)))' 2>/dev/null || true)
fi
if [ -z "$USB_PARENT" ] && [ -d /tmp/pylibs ]; then
    USB_PARENT=$(PYTHONPATH=/tmp/pylibs "$PYTHON" -c 'import usb,os;print(os.path.dirname(os.path.dirname(usb.__file__)))' 2>/dev/null || true)
fi
if [ -n "$USB_PARENT" ] && [ -d "$USB_PARENT/usb" ]; then
    cp -R "$USB_PARENT/usb" "$RES/vendor/usb"
    say "pyusb:   vendored from $USB_PARENT"
else
    echo "error: pyusb not found. Install it with: $PYTHON -m pip install pyusb" >&2
    exit 1
fi

# ---- 5. vendor libusb (so the app does not depend on Homebrew's loader paths)
LIBUSB=""
for cand in /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib \
            /usr/lib/libusb-1.0.dylib; do
    [ -f "$cand" ] && { LIBUSB="$cand"; break; }
done
if [ -n "$LIBUSB" ]; then
    cp "$LIBUSB" "$RES/vendor/libusb-1.0.dylib"
    chmod u+w "$RES/vendor/libusb-1.0.dylib"
    say "libusb:  vendored from $LIBUSB"
else
    say "libusb:  not found - will fall back to the system loader"
fi

# ---- 6. launcher (a real Mach-O binary, not a shell script)
# LaunchServices picks the architecture from the executable. A shell script has none, so
# the bundle gets launched as x86_64 under Rosetta and the arm64 libusb will not load.
if command -v xcrun >/dev/null 2>&1; then
    xcrun clang -O2 -arch arm64 -arch x86_64 -DPYTHON="\"$PYTHON\"" \
        -o "$APP/Contents/MacOS/$NAME" "$ROOT/capture/launcher.c" 2>/dev/null \
        && say "launcher: compiled (arm64 + x86_64)" \
        || { echo "error: launcher.c failed to compile" >&2; exit 1; }
else
    echo "error: no xcrun/clang; cannot build the launcher" >&2
    exit 1
fi

# ---- 7. Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$NAME</string>
    <key>CFBundleDisplayName</key><string>$NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleExecutable</key><string>$NAME</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSArchitecturePriority</key>
    <array><string>arm64</string><string>x86_64</string></array>
    <key>LSApplicationCategoryType</key><string>public.app-category.photography</string>
</dict>
</plist>
PLIST

# ---- 8. optional icon
if [ -f "$ROOT/assets/icon.png" ]; then
    ICONSET=$(mktemp -d)/icon.iconset
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$ROOT/assets/icon.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1
        dbl=$((sz * 2))
        sips -z $dbl $dbl "$ROOT/assets/icon.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" -o "$RES/icon.icns" 2>/dev/null && \
        /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string icon" "$APP/Contents/Info.plist" 2>/dev/null
    say "icon:    from assets/icon.png"
fi

# ---- 9. ad-hoc signature, so Gatekeeper does not complain about an unsigned bundle
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 && say "signed:  ad-hoc" || say "signed:  FAILED (app still runs locally)"

# ---- 10. refresh LaunchServices' cached idea of this bundle
# It caches the executable's architecture; a rebuilt bundle can otherwise keep launching
# translated (x86_64) even though the launcher is now native.
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 && say "registered with LaunchServices"

echo "Done: $APP"
[ "${1:-}" = "--open" ] && open "$APP"
exit 0
