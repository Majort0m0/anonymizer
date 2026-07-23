"""Single source of truth for the installed AnonyMeister version.

Every place that displays or embeds a version number reads it from here
instead of hardcoding its own copy. Before this module existed,
`anonymeister.spec` (macOS `CFBundleShortVersionString`) and
`scripts/anonymeister-installer.iss` (`MyAppVersion`) each hardcoded "1.0.0"
independently and neither was ever bumped across eight releases up to
v1.4.1 — both installers and the packaged app kept reporting "1.0.0"
regardless of what was actually tagged and released. Bumping this string is
now the only step a release needs on the version-number side:
- `anonymeister.spec` imports `APP_VERSION` directly (it's plain Python,
  evaluated by PyInstaller) for the macOS bundle's version metadata.
- `scripts/build_windows.ps1` reads it via `python -c` and passes it to
  Inno Setup as a preprocessor define, since a `.iss` script can't import
  Python itself.
- `app/server.py` exposes it via `GET /api/version` for the running app's
  own UI (footer + Systemstatus panel) and `app/update_check.py` compares
  it against the latest GitHub release tag.
- `.github/workflows/build.yml`'s `check-version` job fails a tagged build
  outright if this string doesn't match the pushed `vX.Y.Z` tag, so the
  drift that caused the original bug can't happen silently again.
"""

APP_VERSION = "1.5.0"
