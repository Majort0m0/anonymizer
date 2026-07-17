"""FastAPI backend: serves the pywebview frontend and exposes the pipeline as
a local-only HTTP API. Pipeline exceptions are allowed to propagate up to the
route handlers here, which translate them into JSON error responses instead
of raw tracebacks or a crashed process.

Two-step flow: /api/analyze-* detects PII and returns categories for the user
to review, without redacting anything; /api/finalize takes the user's
category exclusions (and pseudonymize choice) and produces the final result.
The PendingState between the two calls holds Presidio's internal result
objects (not JSON-serializable in any useful way) and raw source text, so it
is kept server-side in `_pending`, keyed by an opaque per-analysis token —
never sent to or reconstructed from the client.

The pipeline calls (analyze/finalize/dependency-fix) are synchronous, CPU- and
IO-heavy code (Presidio, spaCy, Ollama HTTP calls, subprocess installs). Route
handlers that only do that work are plain `def`s, not `async def` — FastAPI
runs sync path operations in a worker thread automatically, so one slow
request doesn't block the single asyncio event loop from serving any other
request (as `async def` calling blocking code directly would).
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langdetect import LangDetectException, detect
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.config import DEFAULT_LANGUAGE, OUTPUT_DIR, SPACY_MODELS
from app.pipeline.pipeline import PendingState, analyze, analyze_file, finalize
from app.pipeline.render_markdown import render_summary, render_transcript
from app.pipeline.setup_check import attempt_auto_install, check_dependencies
from app.schemas import (
    DownloadableFile,
    FinalizeRequest,
    OutputMode,
    PendingAnalysis,
    PipelineOptions,
    PipelineResult,
    ReplaceTextRequest,
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze-file")
async def analyze_file_route(
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.BOTH),
    deep_check: bool = Form(True),
) -> JSONResponse:
    tmp_dir: Path | None = None
    try:
        upload_name = Path(file.filename or "").name or "upload"
        tmp_dir = Path(tempfile.mkdtemp(prefix="anonymizer_"))
        tmp_path = tmp_dir / upload_name
        tmp_path.write_bytes(await file.read())

        options = PipelineOptions(output_mode=output_mode, deep_check=deep_check)
        state, categories = await run_in_threadpool(analyze_file, tmp_path, options)
        token = _store_pending(state)

        pending = PendingAnalysis(
            token=token,
            source_filename=state.source_filename,
            detected_language=state.detected_language,
            output_mode=state.output_mode,
            deep_check=state.deep_check_requested,
            categories=categories,
        )
        return JSONResponse(pending.model_dump())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/api/analyze-clipboard")
def analyze_clipboard_route(payload: ClipboardAnalyzeRequest) -> JSONResponse:
    try:
        options = PipelineOptions(output_mode=payload.output_mode, deep_check=payload.deep_check)
        detected_language = _detect_clipboard_language(payload.text)
        state, categories = analyze(
            raw_text=payload.text,
            source_filename="clipboard",
            detected_language=detected_language,
            options=options,
        )
        token = _store_pending(state)

        pending = PendingAnalysis(
            token=token,
            source_filename=state.source_filename,
            detected_language=state.detected_language,
            output_mode=state.output_mode,
            deep_check=state.deep_check_requested,
            categories=categories,
        )
        return JSONResponse(pending.model_dump())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/api/finalize")
def finalize_route(payload: FinalizeRequest) -> JSONResponse:
    with _pending_lock:
        state = _pending.pop(payload.token, None)

    if state is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Analyse abgelaufen oder nicht gefunden. Bitte die Datei erneut analysieren."},
        )

    try:
        output = finalize(
            state,
            excluded_categories=set(payload.excluded_categories),
            pseudonymize_person=payload.pseudonymize_person,
        )

        downloads: list[DownloadableFile] = []
        if output.transcript_markdown is not None:
            filename = _save_text(output.source_filename, "-anonymisiert", ".md", output.transcript_markdown)
            downloads.append(DownloadableFile(label="Transkript (Markdown)", filename=filename))
        if output.summary_markdown is not None:
            filename = _save_text(output.source_filename, "-zusammenfassung", ".md", output.summary_markdown)
            downloads.append(DownloadableFile(label="Zusammenfassung (Markdown)", filename=filename))
        if output.structured_bytes is not None and output.structured_suffix is not None:
            filename = _save_bytes(
                output.source_filename, "-anonymisiert", output.structured_suffix, output.structured_bytes
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
        return JSONResponse(result.model_dump())
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


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


@app.get("/api/download/{filename}")
async def download(filename: str) -> FileResponse:
    resolved_dir = OUTPUT_DIR.resolve()
    candidate = (resolved_dir / filename).resolve()
    if candidate.parent != resolved_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(candidate, filename=filename)
