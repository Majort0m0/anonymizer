#!/usr/bin/env bash
# Build an AppImage for AnonyMeister on Linux.
#
# Run from the repo root: ./scripts/build_linux.sh
# Requires PyInstaller on the active Python (a project .venv set up per
# README.md, with requirements-build.txt installed, or — as in CI — deps
# already installed on whatever Python is on PATH with no venv at all).
#
# IMPORTANT Linux-specific runtime dependency, not bundled by this script:
# pywebview needs GTK + WebKit2GTK (PyGObject bindings + their native
# GObject-introspection typelib files) to open its native window at all.
# These do not bundle reliably into a portable AppImage — they must be
# installed as system packages on the machine that RUNS the AppImage, e.g.
# (Debian/Ubuntu):  sudo apt install python3-gi gir1.2-webkit2-4.1
# (Fedora):         sudo dnf install python3-gobject webkit2gtk4.1
# This is a real limitation of pywebview on Linux, not something specific to
# this app — see https://pywebview.flowrl.com/guide/installation.html.
#
# NOTE: written from the same PyInstaller spec verified on macOS, but not
# run on an actual Linux machine — no Linux environment was available to
# test it. Please report back if a step doesn't match reality; see
# CLAUDE.md's packaging notes for the platform quirks accounted for.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d .venv ]; then
  source .venv/bin/activate
fi

echo "==> Running PyInstaller..."
pyinstaller --clean --noconfirm anonymeister.spec

VERSION="$(python3 -c 'from app.version import APP_VERSION; print(APP_VERSION)')"

DIST_DIR="dist/AnonyMeister"
APPDIR="dist/AppDir"

echo "==> Assembling AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -R "$DIST_DIR/." "$APPDIR/usr/bin/"

# A minimal placeholder icon (this repo ships no dedicated app icon) — swap
# in a real one at AppDir/anonymeister.png before distributing if you have one.
python3 - "$APPDIR/anonymeister.png" <<'PYEOF'
import sys
from PIL import Image, ImageDraw

path = sys.argv[1]
img = Image.new("RGBA", (256, 256), (47, 111, 79, 255))
draw = ImageDraw.Draw(img)
draw.text((128, 128), "A", fill=(255, 255, 255, 255), anchor="mm")
img.save(path)
PYEOF
cp "$APPDIR/anonymeister.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/anonymeister.png"

cat > "$APPDIR/usr/share/applications/anonymeister.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AnonyMeister
Comment=Dokumente & Audio lokal anonymisieren
Exec=AnonyMeister
Icon=anonymeister
Categories=Office;Utility;
X-AppImage-Version=$VERSION
EOF
cp "$APPDIR/usr/share/applications/anonymeister.desktop" "$APPDIR/anonymeister.desktop"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/AnonyMeister" "$@"
EOF
chmod +x "$APPDIR/AppRun"

APPIMAGETOOL="dist/appimagetool.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
  echo "==> Downloading appimagetool..."
  curl -L -o "$APPIMAGETOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$APPIMAGETOOL"
fi

echo "==> Building AppImage..."
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "dist/AnonyMeister-x86_64.AppImage"

echo
echo "Done: dist/AnonyMeister-x86_64.AppImage"
echo "Reminder: users need WebKit2GTK + PyGObject installed system-wide for"
echo "the app window to open at all — see the comment at the top of this"
echo "script for the exact package names per distro."
