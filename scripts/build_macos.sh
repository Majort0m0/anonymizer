#!/usr/bin/env bash
# Build AnonyMeister.app + a distributable .dmg for macOS.
#
# Run from the repo root: ./scripts/build_macos.sh
# Requires PyInstaller on the active Python (a project .venv set up per
# README.md, with requirements-build.txt installed, or — as in CI — deps
# already installed on whatever Python is on PATH with no venv at all).
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d .venv ]; then
  source .venv/bin/activate
fi

echo "==> Running PyInstaller..."
pyinstaller --clean --noconfirm anonymeister.spec

APP="dist/AnonyMeister.app"

# Files fetched by pip (spaCy models, wheels, ...) — and, on some machines,
# every file a build process creates at all — can carry macOS's
# com.apple.provenance extended attribute, which `codesign` rejects with
# "resource fork, Finder information, or similar detritus not allowed".
# PyInstaller's own signing attempt already fails silently for the same
# reason (a warning, not a build error). `xattr -cr` removes the ordinary
# case; on some machines com.apple.provenance gets silently re-applied by
# the OS no matter how the files are recreated, and no amount of
# stripping/copying fixes it from a build script — so this step is
# best-effort, not fatal: the .app still runs fine locally either way (this
# only affects Gatekeeper's opinion of a fresh download on another machine).
echo "==> Stripping extended attributes and ad-hoc signing (best-effort)..."
xattr -cr "$APP" || true
if codesign --force --deep -s - "$APP" 2>&1; then
  codesign -dv "$APP"
else
  echo "WARNING: ad-hoc signing failed (likely com.apple.provenance — see" >&2
  echo "comment above). The .app still runs locally; re-run this script from" >&2
  echo "a normal Terminal (outside any sandboxed/agent shell) to sign it" >&2
  echo "properly before distributing." >&2
fi

echo "==> Building .dmg..."
DMG_NAME="AnonyMeister-macOS.dmg"
rm -f "dist/$DMG_NAME"
hdiutil create -volname "AnonyMeister" -srcfolder "$APP" -ov -format UDZO "dist/$DMG_NAME"

echo
echo "Done: dist/AnonyMeister.app and dist/$DMG_NAME"
echo "Note: this is an ad-hoc signature (not notarized by Apple) — first"
echo "launch on another Mac will need a right-click > Open, or"
echo "'xattr -cr /Applications/AnonyMeister.app' if Gatekeeper reports it as damaged."
