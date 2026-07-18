"""FastAPI backend: serves the pywebview frontend and exposes the pipeline as
a local-only HTTP API. Pipeline exceptions are allowed to propagate up to the
route handlers here, which translate them into JSON error responses instead
of raw tracebacks or a crashed process.

Two-step flow: /api/analyze-* detects PII and returns categories for the user
to review, without redacting anything; /api/finalize takes the user's
category exclusions (and person-mode choice) and produces the final result.
The PendingState between the two calls holds Presidio's internal result
objects (not JSON-serializable in any useful way) and raw source text, so it
is kept server-side in `_pending`, keyed by an opaque per-analysis token —
never sent to or reconstructed from the client.

Background jobs + progress polling: analyze-file/analyze-clipboard/finalize
can run for minutes (Presidio, spaCy, and — with deep-check on — one or more
Ollama calls). Rather than blocking the HTTP request for all of that (leaving
the frontend with nothing to show but an indefinite spinner), each of these
three routes starts the actual pipeline call on a background thread and
returns a `job_id` immediately; the frontend polls GET /api/progress/{job_id}
(see `_Job`/`_jobs` below) for a stage label, percentage, and calibrated ETA
until `done` is true, at which point the same response's `result` field holds
exactly what the old synchronous response used to return in its body
(PendingAnalysis for analyze-*, PipelineResult for finalize) — so downstream
handling of that payload is unchanged, only how it's retrieved differs.
Errors from the pipeline call surface as the job's `error` field, not an
HTTP error status, since they're only known once the poll after they occur.

The pipeline calls (analyze/finalize/dependency-fix) are synchronous, CPU- and
IO-heavy code (Presidio, spaCy, Ollama HTTP calls, subprocess installs) — the
background-thread job runner above is this app's own mechanism for that, not
FastAPI's; route handlers that don't spawn a job (dependencies, settings,
replace-text) stay plain `def`s, not `async def`, so FastAPI's automatic
worker-thread dispatch for sync routes still protects the single asyncio
event loop from a slow one blocking any other request.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langdetect import LangDetectException, detect
from pydantic import BaseModel

from app.config import (
    CURATED_OLLAMA_MODELS,
    CURATED_WHISPER_MODELS,
    DEFAULT_LANGUAGE,
    OUTPUT_DIR,
    SPACY_MODELS,
)
from app.pipeline.pipeline import PendingState, analyze, analyze_file, finalize
from app.pipeline.render_markdown import render_summary, render_transcript
from app.pipeline.setup_check import attempt_auto_install, check_dependencies
from app.progress_calibration import get_stage_durations, record_stage_duration
from app.schemas import (
    DetectedCategory,
    DownloadableFile,
    FinalizeRequest,
    OutputMode,
    PendingAnalysis,
    PersonMode,
    PipelineOptions,
    PipelineResult,
    ReplaceTextRequest,
)
from app.settings import (
    get_ollama_model,
    get_whisper_model_size,
    set_ollama_model,
    set_whisper_model_size,
)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

app = FastAPI(title="Anonymizer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")

_pending: dict[str, PendingState] = {}
_pending_lock = threading.Lock()


class ClipboardAnalyzeRequest(BaseModel):
    text: str
    output_mode: OutputMode = OutputMode.BOTH
    deep_check: bool = True


class DependencyFixRequest(BaseModel):
    name: str


class OllamaModelRequest(BaseModel):
    model: str


class WhisperModelRequest(BaseModel):
    model: str


def _detect_clipboard_language(text: str) -> str:
    try:
        code = detect(text)
    except LangDetectException:
        return DEFAULT_LANGUAGE
    return code if code in SPACY_MODELS else DEFAULT_LANGUAGE


def _sanitize_stem(filename: str) -> str:
    stem = Path(filename).stem or "document"
    sanitized = _SAFE_STEM_RE.sub("_", stem).strip("_.")
    return sanitized or "document"


def _unique_filename(source_filename: str, suffix_label: str, extension: str) -> str:
    """{stem}{suffix_label}{extension}, e.g. "report-anonymisiert.md" — with a
    " (2)", " (3)", ... tag appended if that name is already taken in
    OUTPUT_DIR, so re-processing the same source file never silently
    overwrites a previous result."""
    base_name = f"{_sanitize_stem(source_filename)}{suffix_label}"
    candidate = f"{base_name}{extension}"
    if not (OUTPUT_DIR / candidate).exists():
        return candidate

    n = 2
    while True:
        candidate = f"{base_name} ({n}){extension}"
        if not (OUTPUT_DIR / candidate).exists():
            return candidate
        n += 1


def _save_text(source_filename: str, suffix_label: str, extension: str, content: str) -> str:
    filename = _unique_filename(source_filename, suffix_label, extension)
    (OUTPUT_DIR / filename).write_text(content, encoding="utf-8")
    return filename


def _save_bytes(source_filename: str, suffix_label: str, extension: str, content: bytes) -> str:
    filename = _unique_filename(source_filename, suffix_label, extension)
    (OUTPUT_DIR / filename).write_bytes(content)
    return filename


def _store_pending(state: PendingState) -> str:
    token = uuid.uuid4().hex
    with _pending_lock:
        _pending[token] = state
    return token


# ---------------------------------------------------------------------------
# Background jobs + progress polling
#
# A `_Job` is created synchronously (fast) by the route, then handed to a
# background thread that does the actual (slow) pipeline call, mutating the
# SAME `_Job` object as it goes via the on_progress/on_plan callbacks pipeline
# functions accept (see app.pipeline.pipeline's module docstring). The route
# returns `job_id` immediately; GET /api/progress/{job_id} reports whatever
# the job's current state is.
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: bool = False
    error: str | None = None
    stage: str | None = None
    stage_current: int = 0
    stage_total: int = 1
    plan: list[tuple[str, int]] = field(default_factory=list)
    percent: float = 0.0
    eta_seconds: float | None = None
    overtime: bool = False
    result: dict | None = None
    _last_event_time: float | None = None
    created_at: float = field(default_factory=time.monotonic)
    # Snapshotted once when the job's callbacks are built (see
    # _make_callbacks) rather than re-read on every poll — which Ollama
    # model is active affects how long the LLM stages actually take, so
    # calibration for those stages is keyed per-model (see _calibration_key);
    # caching it here avoids re-reading settings.json on every single
    # /api/progress poll for the job's whole lifetime.
    model: str = ""


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.Lock()

# A job is normally removed the moment a client polls it through to `done`
# (see get_progress()). If the client stops polling first — tab closed, page
# reloaded, a dropped connection — the background thread still runs to
# completion and leaves its full result sitting in _jobs forever, since
# nothing else ever prunes it. _sweep_stale_jobs() is called opportunistically
# whenever a new job is created (cheap: this app creates at most a few jobs
# per minute) rather than running a dedicated timer thread.
_STALE_JOB_TTL_SECONDS = 30 * 60


def _sweep_stale_jobs() -> None:
    now = time.monotonic()
    with _jobs_lock:
        # Only ever sweep FINISHED jobs — one still in flight past the TTL is
        # just an unusually slow real job, not an abandoned one, and its
        # background thread holds the only reference needed to finish it;
        # evicting it here would make a later legitimate poll 404 even though
        # the job is still genuinely running.
        stale_ids = [
            job_id
            for job_id, job in _jobs.items()
            if job.done and now - job.created_at > _STALE_JOB_TTL_SECONDS
        ]
        for job_id in stale_ids:
            del _jobs[job_id]

# Human-readable stage labels for the progress UI. Chunked stages (deep-check
# passes) get a "(Teil X/Y)" suffix appended when there's more than one chunk
# — see _stage_label().
_STAGE_LABELS = {
    "ingest": "Datei wird eingelesen",
    "presidio_analyze": "Automatische Erkennung läuft",
    "deep_check_find": "LLM-Tiefencheck läuft",
    "redact": "Anonymisierung wird angewendet",
    "deep_check_apply": "Tiefencheck-Ergebnisse werden angewendet",
    "deep_check_missed": "LLM-Nachkontrolle läuft",
    "deep_check_locations": "LLM-Ortsnamen-Kontrolle läuft",
    "summarize": "Zusammenfassung wird erstellt",
    "render": "Dokument wird erstellt",
    "structured_rewrite": "Tabellen-Kopie wird erstellt",
}

# Fallback per-unit duration (seconds) for any stage name not yet in the
# calibration store at all (should only happen if a new stage is added to
# the pipeline without a matching default in progress_calibration.py).
_FALLBACK_STAGE_SECONDS = 5.0

# How far past the calibrated estimate a stage's current unit has to run
# before the UI switches from a (by then likely-wrong) numeric ETA to an
# honest "taking longer than expected" state.
_OVERTIME_FACTOR = 1.5

# Stages whose duration depends on which Ollama model is active — mixing
# measurements from e.g. gemma4:e4b (fast) and gemma4:12b (much slower) into
# one shared average made the ETA swing wildly and appear to "reset" whenever
# a chunk finished much faster/slower than a stale, other-model-trained
# estimate expected (observed directly: switching models between test runs).
_MODEL_DEPENDENT_STAGES = {"deep_check_find", "deep_check_missed", "deep_check_locations", "summarize"}


def _calibration_key(stage: str, model: str) -> str:
    if stage in _MODEL_DEPENDENT_STAGES and model:
        return f"{stage}::{model}"
    return stage


def _stage_duration(durations: dict[str, float], stage: str, model: str) -> float:
    key = _calibration_key(stage, model)
    if key in durations:
        return durations[key]
    # First time this model is seen for this stage — fall back to the
    # stage's own (model-agnostic) default rather than the generic
    # catch-all, since it's still a much better guess than 5s for an LLM call.
    return durations.get(stage, _FALLBACK_STAGE_SECONDS)


def _stage_label(stage: str | None, current: int, total: int, overtime: bool = False) -> str:
    if stage is None:
        return "Wird vorbereitet…"
    label = _STAGE_LABELS.get(stage, stage)
    if total > 1:
        label = f"{label} (Teil {max(current, 1)}/{total})"
    if overtime:
        # The calibrated estimate for this stage has already been exceeded —
        # showing a stale "<1 Sek." for minutes on end (which the estimate
        # alone would otherwise do) reads as broken/frozen. Say so honestly
        # instead of pretending we still know how much longer this will take.
        label += " — dauert länger als erwartet…"
    return label


def _recompute_progress(job: _Job) -> None:
    """Recomputes job.percent/eta_seconds from job.plan + job.stage/current,
    using the LATEST calibration data — so a long job's own early stages
    refine the ETA shown for its later ones, not just future jobs.

    Called both when a stage/chunk actually completes (from the on_progress/
    on_plan callbacks) AND on every /api/progress poll (see get_progress()) —
    the latter is what makes the bar/ETA keep moving smoothly WHILE a single
    long, non-chunked LLM call (e.g. summarize) is still in flight, by
    extrapolating from wall-clock time elapsed since that stage started
    rather than only updating at the (rare, coarse) checkpoints the pipeline
    itself reports. Without this, the display would freeze at whatever it
    showed at the start of any stage lasting longer than its estimate until
    that stage's single "done" event finally arrives.

    job.plan's per-stage unit counts are only an UPFRONT ESTIMATE — for the
    two chunked deep-check stages it's computed from a text (raw_text /
    state.raw_text) that isn't the same text actually chunked once Presidio
    (and, in finalize(), the first deep-check pass) has redacted it down to
    something shorter, so the true chunk count is usually lower. job.stage_total
    is refreshed with the REAL count on every on_progress call for whichever
    stage is currently running, so it's used here (instead of the plan's
    static estimate) for that one stage, in both the numerator and the
    denominator — without this, a stage whose real count undercuts the
    estimate would visibly stall near the end (waiting on estimated-but-
    nonexistent remaining chunks) and then jump once the next stage begins.
    Stages not yet reached still have to rely on the plan's original guess,
    since nothing better is known about them yet.
    """
    if not job.plan:
        job.percent = 0.0
        job.eta_seconds = None
        job.overtime = False
        return

    durations = get_stage_durations()
    now = time.monotonic()
    total_seconds = 0.0
    done_seconds = 0.0
    reached_current = False
    current_overtime = False
    for name, planned_units in job.plan:
        per_unit = _stage_duration(durations, name, job.model)
        units = job.stage_total if name == job.stage else planned_units
        stage_total_seconds = per_unit * units
        total_seconds += stage_total_seconds

        if reached_current:
            continue
        if name == job.stage:
            elapsed_in_current_unit = now - (job._last_event_time or now)
            done_seconds += min(job.stage_current * per_unit + elapsed_in_current_unit, stage_total_seconds)
            # The calibrated estimate can be badly off for a while after a
            # workload shape change (e.g. chunk sizes changing what "one
            # unit" means) — once real elapsed time for the unit in flight
            # has clearly blown past what was estimated for it, stop
            # reporting a numeric ETA (which would just sit wrong) and say
            # so instead (see _stage_label()).
            current_overtime = elapsed_in_current_unit > per_unit * _OVERTIME_FACTOR
            reached_current = True
        else:
            done_seconds += stage_total_seconds

    job.overtime = current_overtime

    if total_seconds <= 0:
        job.percent = 0.0
        job.eta_seconds = None
        return

    job.percent = min(99.0, (done_seconds / total_seconds) * 100)
    job.eta_seconds = None if current_overtime else max(0.0, total_seconds - done_seconds)


def _make_callbacks(job: _Job):
    job.model = get_ollama_model()

    def on_plan(plan: list[tuple[str, int]]) -> None:
        with job.lock:
            job.plan = plan
            job.stage = plan[0][0] if plan else None
            job.stage_current = 0
            job.stage_total = plan[0][1] if plan else 1
            job._last_event_time = time.monotonic()
            _recompute_progress(job)

    def on_progress(stage: str, current: int, total: int) -> None:
        now = time.monotonic()
        with job.lock:
            if job.stage != stage:
                # New stage starting — nothing to measure yet (the elapsed
                # time since the previous stage's last event belongs to
                # whatever ran in between, e.g. non-instrumented glue code).
                job.stage = stage
                job.stage_current = current
                job.stage_total = total
            else:
                elapsed = now - (job._last_event_time or now)
                units_done = current - job.stage_current
                if units_done > 0 and elapsed > 0:
                    record_stage_duration(_calibration_key(stage, job.model), elapsed / units_done)
                job.stage_current = current
                job.stage_total = total
            job._last_event_time = now
            _recompute_progress(job)

    return on_progress, on_plan


def _finish_job(job: _Job, result: dict) -> None:
    with job.lock:
        job.result = result
        job.done = True
        job.percent = 100.0
        job.eta_seconds = 0.0


def _fail_job(job: _Job, exc: Exception) -> None:
    with job.lock:
        job.error = str(exc)
        job.done = True


def _run_job(job: _Job, build_result: Callable[[], dict], cleanup: Callable[[], None] | None = None) -> None:
    """Shared shape for every background job: build_result() does the actual
    (slow) pipeline work and returns the dict to finish the job with; any
    exception fails the job instead of propagating, since this runs on a bare
    background thread with no other handler to catch it. `cleanup`, if given,
    always runs afterward regardless of success/failure — e.g. removing
    analyze-file's temp upload directory."""
    try:
        result = build_result()
        _finish_job(job, result)
    except Exception as exc:
        _fail_job(job, exc)
    finally:
        if cleanup:
            cleanup()


def _build_pending_analysis_result(state: PendingState, categories: list[DetectedCategory]) -> dict:
    token = _store_pending(state)
    pending = PendingAnalysis(
        token=token,
        source_filename=state.source_filename,
        detected_language=state.detected_language,
        output_mode=state.output_mode,
        deep_check=state.deep_check_requested,
        categories=categories,
    )
    return pending.model_dump()


def _create_job() -> tuple[str, _Job]:
    _sweep_stale_jobs()
    job_id = uuid.uuid4().hex
    job = _Job()
    with _jobs_lock:
        _jobs[job_id] = job
    return job_id, job


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _run_analyze_file_job(job: _Job, tmp_path: Path, options: PipelineOptions, tmp_dir: Path) -> None:
    on_progress, on_plan = _make_callbacks(job)

    def build_result() -> dict:
        state, categories = analyze_file(tmp_path, options, on_progress=on_progress, on_plan=on_plan)
        return _build_pending_analysis_result(state, categories)

    _run_job(job, build_result, cleanup=lambda: shutil.rmtree(tmp_dir, ignore_errors=True))


@app.post("/api/analyze-file")
async def analyze_file_route(
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.BOTH),
    deep_check: bool = Form(True),
) -> JSONResponse:
    try:
        upload_name = Path(file.filename or "").name or "upload"
        tmp_dir = Path(tempfile.mkdtemp(prefix="anonymizer_"))
        tmp_path = tmp_dir / upload_name
        tmp_path.write_bytes(await file.read())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

    options = PipelineOptions(output_mode=output_mode, deep_check=deep_check)
    job_id, job = _create_job()
    threading.Thread(
        target=_run_analyze_file_job, args=(job, tmp_path, options, tmp_dir), daemon=True
    ).start()
    return JSONResponse({"job_id": job_id})


def _run_analyze_clipboard_job(job: _Job, text: str, options: PipelineOptions) -> None:
    on_progress, on_plan = _make_callbacks(job)

    def build_result() -> dict:
        detected_language = _detect_clipboard_language(text)
        state, categories = analyze(
            raw_text=text,
            source_filename="clipboard",
            detected_language=detected_language,
            options=options,
            on_progress=on_progress,
            on_plan=on_plan,
        )
        return _build_pending_analysis_result(state, categories)

    _run_job(job, build_result)


@app.post("/api/analyze-clipboard")
def analyze_clipboard_route(payload: ClipboardAnalyzeRequest) -> JSONResponse:
    options = PipelineOptions(output_mode=payload.output_mode, deep_check=payload.deep_check)
    job_id, job = _create_job()
    threading.Thread(
        target=_run_analyze_clipboard_job, args=(job, payload.text, options), daemon=True
    ).start()
    return JSONResponse({"job_id": job_id})


def _run_finalize_job(job: _Job, state: PendingState, excluded_categories: set[str], person_mode: PersonMode) -> None:
    on_progress, on_plan = _make_callbacks(job)

    def build_result() -> dict:
        output = finalize(
            state,
            excluded_categories=excluded_categories,
            person_mode=person_mode,
            on_progress=on_progress,
            on_plan=on_plan,
        )

        downloads: list[DownloadableFile] = []
        if output.transcript_markdown is not None:
            filename = _save_text(
                output.redacted_source_filename, "-anonymisiert", ".md", output.transcript_markdown
            )
            downloads.append(DownloadableFile(label="Transkript (Markdown)", filename=filename))
        if output.summary_markdown is not None:
            filename = _save_text(
                output.redacted_source_filename, "-zusammenfassung", ".md", output.summary_markdown
            )
            downloads.append(DownloadableFile(label="Zusammenfassung (Markdown)", filename=filename))
        if output.structured_bytes is not None and output.structured_suffix is not None:
            filename = _save_bytes(
                output.redacted_source_filename, "-anonymisiert", output.structured_suffix, output.structured_bytes
            )
            label = f"Tabelle ({output.structured_suffix.lstrip('.').upper()})"
            downloads.append(DownloadableFile(label=label, filename=filename))

        result = PipelineResult(
            source_filename=output.source_filename,
            detected_language=output.detected_language,
            deep_check_enabled=output.deep_check_enabled,
            anonymized_transcript=output.anonymized_transcript,
            summary=output.summary,
            pii_audit=output.pii_audit,
            downloads=downloads,
        )
        return result.model_dump()

    _run_job(job, build_result)


@app.post("/api/finalize")
def finalize_route(payload: FinalizeRequest) -> JSONResponse:
    with _pending_lock:
        state = _pending.pop(payload.token, None)

    if state is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Analyse abgelaufen oder nicht gefunden. Bitte die Datei erneut analysieren."},
        )

    job_id, job = _create_job()
    threading.Thread(
        target=_run_finalize_job,
        args=(job, state, set(payload.excluded_categories), payload.person_mode),
        daemon=True,
    ).start()
    return JSONResponse({"job_id": job_id})


@app.get("/api/progress/{job_id}")
def get_progress(job_id: str) -> JSONResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Unbekannter oder abgelaufener Job."})

    with job.lock:
        # Snapshot `done` once and reuse it for both the response body and
        # the pop decision below — re-reading job.done a second time after
        # releasing this lock could observe the background thread having
        # finished in between, popping the job out from under a response
        # that still says done:false (the client would then get a phantom
        # 404 on its next poll for a job that actually succeeded).
        done = job.done
        if not done:
            # Live-extrapolate from wall-clock time, not just the last
            # reported checkpoint — see _recompute_progress()'s docstring.
            _recompute_progress(job)
        response = {
            "done": done,
            "error": job.error,
            "stage_label": _stage_label(job.stage, job.stage_current, job.stage_total, job.overtime),
            "percent": round(job.percent, 1),
            "eta_seconds": round(job.eta_seconds, 1) if job.eta_seconds is not None else None,
            "result": job.result,
        }

    if done:
        with _jobs_lock:
            _jobs.pop(job_id, None)

    return JSONResponse(response)


_MARKDOWN_DOWNLOAD_LABELS = {"Transkript (Markdown)", "Zusammenfassung (Markdown)"}


@app.post("/api/replace-text")
def replace_text_route(payload: ReplaceTextRequest) -> JSONResponse:
    """Manual post-finalize find/replace (e.g. fixing a word an audio
    transcription misheard). Takes the transcript/summary text the client
    already holds — no server-side token/state involved — applies a plain
    literal-text substitution, and re-renders + re-saves the markdown so the
    downloads stay in sync with what's on screen. Any non-markdown download
    (a structured-format xlsx/csv/json/ods copy) is passed through unchanged;
    this endpoint never touches it."""
    if not payload.search:
        return JSONResponse(status_code=400, content={"error": "Bitte einen Suchbegriff angeben."})

    try:
        flags = 0 if payload.match_case else re.IGNORECASE
        pattern = re.compile(re.escape(payload.search), flags)
        count = 0 if payload.replace_all else 1

        def apply_replace(text: str | None) -> str | None:
            if text is None:
                return None
            return pattern.sub(lambda m: payload.replacement, text, count=count)

        new_transcript = apply_replace(payload.anonymized_transcript)
        new_summary = apply_replace(payload.summary)

        downloads = [d for d in payload.downloads if d.label not in _MARKDOWN_DOWNLOAD_LABELS]

        if new_transcript is not None:
            markdown = render_transcript(
                source_filename=payload.source_filename,
                detected_language=payload.detected_language,
                deep_check_enabled=payload.deep_check_enabled,
                anonymized_transcript=new_transcript,
                pii_audit=payload.pii_audit,
            )
            filename = _save_text(payload.source_filename, "-anonymisiert", ".md", markdown)
            downloads.append(DownloadableFile(label="Transkript (Markdown)", filename=filename))

        if new_summary is not None:
            markdown = render_summary(
                source_filename=payload.source_filename,
                detected_language=payload.detected_language,
                deep_check_enabled=payload.deep_check_enabled,
                summary=new_summary,
                pii_audit=payload.pii_audit,
            )
            filename = _save_text(payload.source_filename, "-zusammenfassung", ".md", markdown)
            downloads.append(DownloadableFile(label="Zusammenfassung (Markdown)", filename=filename))

        result = PipelineResult(
            source_filename=payload.source_filename,
            detected_language=payload.detected_language,
            deep_check_enabled=payload.deep_check_enabled,
            anonymized_transcript=new_transcript,
            summary=new_summary,
            pii_audit=payload.pii_audit,
            downloads=downloads,
        )
        return JSONResponse(result.model_dump())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/dependencies")
def get_dependencies() -> list[dict]:
    return [status.model_dump() for status in check_dependencies()]


@app.post("/api/dependencies/fix")
def fix_dependency(payload: DependencyFixRequest) -> JSONResponse:
    try:
        status = attempt_auto_install(payload.name)
        return JSONResponse(status.model_dump())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/api/settings/ollama-model")
def get_ollama_model_setting() -> dict:
    return {
        "model": get_ollama_model(),
        "curated": CURATED_OLLAMA_MODELS,
    }


@app.post("/api/settings/ollama-model")
def set_ollama_model_setting(payload: OllamaModelRequest) -> JSONResponse:
    try:
        model = set_ollama_model(payload.model)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse({"model": model})


@app.get("/api/settings/whisper-model")
def get_whisper_model_setting() -> dict:
    return {
        "model": get_whisper_model_size(),
        "curated": CURATED_WHISPER_MODELS,
    }


@app.post("/api/settings/whisper-model")
def set_whisper_model_setting(payload: WhisperModelRequest) -> JSONResponse:
    try:
        size = set_whisper_model_size(payload.model)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse({"model": size})


@app.get("/api/download/{filename}")
async def download(filename: str) -> FileResponse:
    resolved_dir = OUTPUT_DIR.resolve()
    candidate = (resolved_dir / filename).resolve()
    if candidate.parent != resolved_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(candidate, filename=filename)
