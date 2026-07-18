"""Pipeline orchestrator: ingest/transcription -> analyze -> (user reviews
categories) -> finalize (redact -> deep_check -> summarize -> render).

Two-step flow: `analyze_file`/`analyze_clipboard` (via `analyze`) detect PII
and return categories for the user to review, without redacting anything yet.
`finalize` takes the user's category exclusions (and person-mode choice —
redact/numbered/pseudonymize) and produces a `FinalizeOutput`. The PendingState in between is kept server-side
(see app.server's token cache) — it is not a pydantic model because it holds
Presidio's internal RecognizerResult objects and, for tabular source formats,
the original file bytes, none of which cross the HTTP boundary.

Privacy-critical ordering, unchanged by the two-step split: deep-check's LLM
call during analysis always runs against a FULLY redacted (no exclusions)
preliminary text — the user's later category choices affect only what
appears in the final output, never what is sent to the LLM. summarize_text()
is likewise only ever given the final, fully-processed anonymized text.

`finalize()` also runs a second, later deep-check pass (`deep_check.
find_missed_pii()`) against the true final text, once category exclusions
and person-mode are already applied — a safety net for plain PII the
deterministic pass and the first LLM sweep both missed outright (stray
location names, names left in signature lines, business/reference numbers),
gated behind the same `deep_check` option. See `app/pipeline/deep_check.py`'s
module docstring for why this one needs `excluded_categories` itself rather
than filtering its output afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import DEFAULT_LANGUAGE, SPACY_MODELS
from app.pipeline import anonymize, deep_check
from app.pipeline.ingest import (
    AUDIO_EXTENSIONS,
    CSV_EXTENSIONS,
    EXCEL_EXTENSIONS,
    JSON_EXTENSIONS,
    STRUCTURED_REWRITE_EXTENSIONS,
    ingest_file,
)
from app.pipeline.pseudonymize import make_person_numberer, make_person_pseudonymizer
from app.pipeline.render_markdown import render_summary, render_transcript
from app.pipeline.rewrite_csv import rewrite_csv
from app.pipeline.rewrite_excel import output_suffix_for as excel_output_suffix_for
from app.pipeline.rewrite_excel import rewrite_excel
from app.pipeline.rewrite_json import rewrite_json
from app.pipeline.rewrite_ods import rewrite_ods
from app.pipeline.summarize import summarize_text
from app.pipeline.transcription import transcribe_audio
from app.schemas import DetectedCategory, OutputMode, PersonMode, PiiEntity, PipelineOptions


@dataclass
class PendingState:
    source_filename: str
    detected_language: str
    language: str  # resolved language actually used for analysis/LLM calls
    output_mode: OutputMode
    deep_check_requested: bool
    raw_text: str
    presidio_results: list
    deep_check_candidates: list = field(default_factory=list)
    source_bytes: bytes | None = None  # only set for STRUCTURED_REWRITE_EXTENSIONS
    source_suffix: str | None = None  # original lowercase extension, e.g. ".xlsx"


@dataclass
class FinalizeOutput:
    source_filename: str
    redacted_source_filename: str
    detected_language: str
    deep_check_enabled: bool
    anonymized_transcript: str | None
    summary: str | None
    pii_audit: list[PiiEntity]
    transcript_markdown: str | None
    summary_markdown: str | None
    structured_bytes: bytes | None
    structured_suffix: str | None


def _resolve_language(language_hint: str | None, detected_language: str) -> str:
    if language_hint:
        return language_hint
    if detected_language in SPACY_MODELS:
        return detected_language
    return DEFAULT_LANGUAGE


def analyze(
    raw_text: str,
    source_filename: str,
    detected_language: str,
    options: PipelineOptions,
    source_bytes: bytes | None = None,
    source_suffix: str | None = None,
) -> tuple[PendingState, list[DetectedCategory]]:
    language = _resolve_language(options.language_hint, detected_language)

    presidio_results = anonymize.analyze(raw_text, language)
    categories = anonymize.summarize_categories(raw_text, presidio_results)

    deep_check_candidates: list = []
    if options.deep_check:
        # Deep-check must only ever see fully-redacted text, regardless of
        # what the user later chooses to exclude from the final output.
        preliminary = anonymize.apply_anonymization(raw_text, presidio_results)
        deep_check_candidates = deep_check.find_candidates(preliminary.anonymized_text, language)
        categories += deep_check.summarize_candidate_categories(deep_check_candidates)

    state = PendingState(
        source_filename=source_filename,
        detected_language=detected_language,
        language=language,
        output_mode=options.output_mode,
        deep_check_requested=options.deep_check,
        raw_text=raw_text,
        presidio_results=presidio_results,
        deep_check_candidates=deep_check_candidates,
        source_bytes=source_bytes,
        source_suffix=source_suffix,
    )
    return state, categories


def analyze_file(path: Path, options: PipelineOptions) -> tuple[PendingState, list[DetectedCategory]]:
    suffix = path.suffix.lower()

    if suffix in AUDIO_EXTENSIONS:
        raw_text, detected_language = transcribe_audio(path)
        source_filename = path.name
        source_bytes = None
    else:
        ingest_result = ingest_file(path)
        raw_text = ingest_result.raw_text
        detected_language = ingest_result.detected_language or DEFAULT_LANGUAGE
        source_filename = ingest_result.source_filename
        # Kept only for tabular formats: finalize() re-parses these bytes to
        # produce an anonymized copy in the original format, alongside the
        # markdown transcript.
        source_bytes = path.read_bytes() if suffix in STRUCTURED_REWRITE_EXTENSIONS else None

    return analyze(
        raw_text,
        source_filename,
        detected_language,
        options,
        source_bytes=source_bytes,
        source_suffix=suffix if source_bytes is not None else None,
    )


def _rewrite_structured(
    source_bytes: bytes, source_suffix: str, transform: Callable[[str], str]
) -> tuple[bytes, str]:
    if source_suffix in EXCEL_EXTENSIONS:
        return rewrite_excel(source_bytes, source_suffix, transform), excel_output_suffix_for(source_suffix)
    if source_suffix in CSV_EXTENSIONS:
        return rewrite_csv(source_bytes, transform), source_suffix
    if source_suffix in JSON_EXTENSIONS:
        return rewrite_json(source_bytes, transform), source_suffix
    if source_suffix == ".ods":
        return rewrite_ods(source_bytes, transform), source_suffix
    raise ValueError(f"No structured rewriter available for suffix {source_suffix!r}.")


def finalize(
    state: PendingState,
    excluded_categories: set[str],
    person_mode: PersonMode,
) -> FinalizeOutput:
    # Built once and reused for the transcript AND every structured-format
    # cell below, so the same real name maps to the same label (number or
    # fake name) across every output produced from this one finalize() call.
    person_replacer = None
    if person_mode == PersonMode.PSEUDONYMIZE:
        person_replacer = make_person_pseudonymizer(state.language)
    elif person_mode == PersonMode.NUMBERED:
        person_replacer = make_person_numberer()

    anon_result = anonymize.apply_anonymization(
        state.raw_text,
        state.presidio_results,
        excluded_types=excluded_categories,
        person_mode=person_mode,
        person_replacer=person_replacer,
    )
    text = anon_result.anonymized_text
    pii_audit = list(anon_result.entities)

    # The original upload filename can itself carry PII (e.g. a person's
    # name) and would otherwise flow untouched into the saved output
    # filename and the document title below — redact it the same way as the
    # body, reusing person_replacer so a name matches its body-text label.
    # "clipboard" (app.server / ingest.py's sentinel for pasted-text input,
    # not a real filename) is skipped: it isn't user data, and spaCy's NER
    # false-positives on it as a PERSON (observed: score 0.85), which would
    # otherwise rename every clipboard-sourced output to "[PERSON]...".
    if state.source_filename == "clipboard":
        redacted_source_filename = state.source_filename
    else:
        redacted_source_filename = anonymize.anonymize_filename(
            state.source_filename,
            state.language,
            excluded_types=excluded_categories,
            person_mode=person_mode,
            person_replacer=person_replacer,
        )

    if state.deep_check_requested:
        dc_result = deep_check.apply_candidates(text, state.deep_check_candidates, excluded_categories)
        text = dc_result.anonymized_text
        pii_audit += dc_result.entities

        # A second, later LLM sweep against the now-fully-redacted final
        # text, looking for plain PII the pass above still missed outright
        # (stray location names, names left in signature lines, business/
        # file reference numbers) rather than indirect/contextual clues.
        # Unlike find_candidates() above (run once during analyze(), before
        # the user's category choices exist), this runs here against the
        # true final text so it can be told which categories to leave alone.
        missed_candidates = deep_check.find_missed_pii(text, state.language, excluded_categories)
        if missed_candidates:
            missed_result = deep_check.apply_candidates(
                text, missed_candidates, excluded_categories, source="llm_final_check"
            )
            text = missed_result.anonymized_text
            pii_audit += missed_result.entities

    summary = None
    if state.output_mode in (OutputMode.SUMMARY, OutputMode.BOTH):
        summary = summarize_text(text, state.language)

    anonymized_transcript = None
    if state.output_mode in (OutputMode.TRANSCRIPT, OutputMode.BOTH):
        anonymized_transcript = text

    transcript_markdown = None
    if anonymized_transcript is not None:
        transcript_markdown = render_transcript(
            source_filename=redacted_source_filename,
            detected_language=state.detected_language,
            deep_check_enabled=state.deep_check_requested,
            anonymized_transcript=anonymized_transcript,
            pii_audit=pii_audit,
        )

    summary_markdown = None
    if summary is not None:
        summary_markdown = render_summary(
            source_filename=redacted_source_filename,
            detected_language=state.detected_language,
            deep_check_enabled=state.deep_check_requested,
            summary=summary,
            pii_audit=pii_audit,
        )

    structured_bytes: bytes | None = None
    structured_suffix: str | None = None
    if state.source_bytes is not None and state.source_suffix is not None:

        def cell_transform(cell_text: str) -> str:
            cell_results = anonymize.analyze(cell_text, state.language)
            cell_anon = anonymize.apply_anonymization(
                cell_text,
                cell_results,
                excluded_types=excluded_categories,
                person_mode=person_mode,
                person_replacer=person_replacer,
            )
            cell_text_out = cell_anon.anonymized_text
            if state.deep_check_requested:
                cell_text_out = deep_check.apply_candidates(
                    cell_text_out, state.deep_check_candidates, excluded_categories
                ).anonymized_text
            return cell_text_out

        structured_bytes, structured_suffix = _rewrite_structured(
            state.source_bytes, state.source_suffix, cell_transform
        )

    return FinalizeOutput(
        source_filename=state.source_filename,
        redacted_source_filename=redacted_source_filename,
        detected_language=state.detected_language,
        deep_check_enabled=state.deep_check_requested,
        anonymized_transcript=anonymized_transcript,
        summary=summary,
        pii_audit=pii_audit,
        transcript_markdown=transcript_markdown,
        summary_markdown=summary_markdown,
        structured_bytes=structured_bytes,
        structured_suffix=structured_suffix,
    )
