"""Checks GitHub Releases for a newer AnonyMeister version than the one
currently installed — backs the Systemstatus panel's version display, its
"Auf Updates prüfen" button, and the Systemstatus button's traffic-light
badge (see app/server.py's `/api/update-check`).

Deliberately not in app/pipeline/: this has nothing to do with document
processing. It's app-level infrastructure in the same category as
app/settings.py (persisted UI choices) or app/pipeline/setup_check.py (local
dependency checks) — just for the app's own version instead of a runtime
dependency. The only network traffic this ever produces is a single GET to
GitHub's public releases API with no document content, filenames, or
anything else user-supplied in it — the frontend's Datenschutz section
documents this as the one deliberate exception to "no external API calls"
(that claim was always scoped to document analysis / AI evaluation, never to
version metadata, but the exception is now spelled out explicitly rather
than left implicit).

The frontend calls `check_for_update()` once automatically on load (an
explicit product decision — see CLAUDE.md/conversation history — so the
Systemstatus button's badge is accurate without the user needing to open the
panel first) plus on demand via the "Auf Updates prüfen" button. `_cache`
exists so repeated automatic checks within one running process (e.g. the
desktop app's window reloading, or several browser tabs against the same
Docker-hosted backend) don't hit GitHub's API every single time; the button
always passes `force=True` to bypass it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from app.schemas import UpdateCheckResult
from app.version import APP_VERSION

_RELEASES_API_URL = "https://api.github.com/repos/Majort0m0/anonymeister/releases/latest"
_HTTP_TIMEOUT = 5
_CACHE_TTL_SECONDS = 6 * 60 * 60

_cache: UpdateCheckResult | None = None
_cache_time: float = 0.0


def _parse_version(value: str) -> tuple[int, ...]:
    """"v1.4.1" / "1.4.1" -> (1, 4, 1). Trailing non-numeric content (e.g. a
    "-beta1" suffix) is dropped rather than raising, so a release tag that
    doesn't strictly follow MAJOR.MINOR.PATCH still compares best-effort
    instead of crashing the check."""
    value = value.lstrip("vV")
    parts: list[int] = []
    for segment in value.split("."):
        digits = ""
        for char in segment:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _fetch_latest_release() -> UpdateCheckResult:
    request = urllib.request.Request(
        _RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub's API rejects unauthenticated requests with no
            # User-Agent header (403), regardless of rate-limit status.
            "User-Agent": "AnonyMeister-UpdateCheck",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return UpdateCheckResult(
            current_version=APP_VERSION,
            error="Update-Prüfung fehlgeschlagen (keine Verbindung zu GitHub).",
        )

    tag = payload.get("tag_name") or ""
    latest = tag.lstrip("vV") or None
    return UpdateCheckResult(
        current_version=APP_VERSION,
        latest_version=latest,
        update_available=bool(latest) and _parse_version(tag) > _parse_version(APP_VERSION),
        release_url=payload.get("html_url"),
    )


def check_for_update(force: bool = False) -> UpdateCheckResult:
    global _cache, _cache_time
    if not force and _cache is not None and (time.monotonic() - _cache_time) < _CACHE_TTL_SECONDS:
        return _cache
    result = _fetch_latest_release()
    if result.error is None:
        _cache = result
        _cache_time = time.monotonic()
    return result
