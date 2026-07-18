"""Learned average duration per pipeline stage, used to turn raw stage-
completion events into a calibrated ETA for the progress UI.

Calibrated per STAGE, not per document or per run, because stage cost scales
differently: LLM stages (deep_check_find/deep_check_missed) are chunked (see
app/pipeline/deep_check.py — typically just 1 chunk for an ordinary
document, more only for genuinely long ones), so their duration is tracked
per chunk and the estimate for a new run is simply per-chunk average * chunk
count; cheap, non-chunked stages (ingest, presidio_analyze, render, ...) are
tracked as one duration per stage-run. An exponential moving average lets
the estimate improve with repeated use (as the user asked for) without
needing to store a full history or being thrown off by one unusually
slow/fast outlier run.

The three Ollama-backed stages (deep_check_find, deep_check_missed,
summarize) are actually stored under a per-model key (e.g.
"deep_check_find::gemma4:e4b") — see app/server.py's _calibration_key() —
since a fast and a slow model's real durations are wildly different and
averaging them together made estimates swing badly whenever the user
switched models. This module itself just stores whatever key it's given;
the per-model qualification decision lives in app/server.py, not here.
"""

from __future__ import annotations

import json
import threading

from app.config import APP_DATA_DIR

_CALIBRATION_PATH = APP_DATA_DIR / "progress_calibration.json"
_lock = threading.Lock()

# Seconds; rough guesses so the very first run of an as-yet-uncalibrated
# (stage, model) combination still shows a plausible (if imprecise) ETA
# instead of "unknown" — corrected towards real measurements from the first
# run onwards. The three LLM defaults assume one big chunk covering a whole
# ordinary document (the common case since the chunk-size increase in
# deep_check.py) rather than the old small ~350-word chunks, so they're
# deliberately much higher than a per-chunk estimate would have been before.
_DEFAULT_DURATIONS: dict[str, float] = {
    "ingest": 2.0,
    "presidio_analyze": 3.0,
    "deep_check_find": 90.0,  # per chunk (usually the whole document)
    "redact": 0.5,
    "deep_check_apply": 0.2,
    "deep_check_missed": 90.0,  # per chunk (usually the whole document)
    "deep_check_locations": 60.0,  # per chunk (usually the whole document) - narrower prompt, typically faster
    "summarize": 60.0,
    "render": 0.2,
    "structured_rewrite": 3.0,
}

# How much weight a single new measurement gets against the running average.
# High enough to adapt within a handful of runs, low enough that one freak
# slow/fast run (e.g. the OS swapping, or a cold model load) doesn't wildly
# swing the next estimate.
_EMA_ALPHA = 0.3


# In-memory mirror of the persisted file, loaded once and then updated in
# place — a busy job's progress poll can hit get_stage_durations() every
# ~700ms for minutes on end (see app/server.py's PROGRESS_POLL_INTERVAL_MS),
# and re-opening/re-parsing the JSON file that often on every single poll
# (for data that only changes a handful of times over a job's life) is pure
# waste. It also removes the previous unguarded-read-during-write race:
# get_stage_durations() no longer touches the file at all once loaded, only
# record_stage_duration() (under _lock) ever writes it again.
_cache: dict[str, float] | None = None


def _load_from_disk() -> dict:
    try:
        data = json.loads(_CALIBRATION_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def get_stage_durations() -> dict[str, float]:
    """Current best-known average seconds per stage (per chunk, for the
    chunked LLM stages) — defaults layered under with any real, persisted
    measurements taking precedence. Returns a copy; callers may freely read
    it without affecting the cache."""
    global _cache
    with _lock:
        if _cache is None:
            stored = _load_from_disk()
            durations = dict(_DEFAULT_DURATIONS)
            for stage, value in stored.items():
                if isinstance(value, (int, float)) and value > 0:
                    durations[stage] = float(value)
            _cache = durations
        return dict(_cache)


def record_stage_duration(stage: str, seconds: float) -> None:
    """Fold one real, measured duration into the running average for `stage`."""
    global _cache
    if seconds <= 0:
        return
    with _lock:
        if _cache is None:
            _cache = dict(_DEFAULT_DURATIONS)
            stored = _load_from_disk()
            for name, value in stored.items():
                if isinstance(value, (int, float)) and value > 0:
                    _cache[name] = float(value)

        previous = _cache.get(stage, _DEFAULT_DURATIONS.get(stage, seconds))
        _cache[stage] = _EMA_ALPHA * seconds + (1 - _EMA_ALPHA) * previous

        _CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CALIBRATION_PATH.write_text(json.dumps(_cache, indent=2), encoding="utf-8")
