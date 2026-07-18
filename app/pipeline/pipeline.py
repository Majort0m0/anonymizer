"""Pipeline orchestrator: ingest/transcription -> analyze -> (user reviews
categories) -> finalize (redact -> deep_check -> summarize -> render).

Two-step flow: `analyze_file`/`analyze_clipboard` (via `analyze`) detect PII
and return categories for the user to review, without redacting anything yet.
`finalize` takes the user's category exclusions (and person-mode choice —
redact/numbered/pseudonymize) and produces a `FinalizeOutput`. The PendingState in between is kept server-side
(see app.server's token cache) — it is not a pydantic model because it holds
Presidio's internal RecognizerResult objects and, for tabular source formats,
the original file bytes, none of which cross the HTTP boundary.

Deep-check's first LLM pass (`deep_check.find_candidates()`, called from
`analyze()`) is handed the RAW, unredacted text. It used to run against a
Presidio-redacted "preliminary" text instead, purely as an extra privacy
precaution — but Ollama here is always the local instance (never reachable
over the network either way), and that precaution had two real, observed
costs and no compensating benefit: Presidio's own NER mistakes could corrupt
the very phrases the LLM needed to read intact (observed: a mis-tagged
LOCATION span ate part of an unrelated sentence that contained a nickname),
and pre-redacting collapsed every distinct person in the document to the
same generic "[PERSON]" placeholder, removing the LLM's ability to tell
WHICH person a nickname/role/context clue belonged to whenever a document
mentions more than one. See `app/pipeline/deep_check.py`'s module docstring
for how its prompts were adjusted to match (the model must now be told to
ignore the obvious direct identifiers it can plainly see, since Presidio
already handles those, and only report indirect/contextual ones).

`find_missed_pii()`/`find_missed_locations()` (run later, in `finalize()`)
and `summarize_text()` are NOT part of the above change and still only ever
see the true FINAL, fully-processed text (post category-exclusion, post
person-mode). That is not a privacy precaution to begin with, but a
correctness requirement independent of it: those two deep-check passes exist
specifically to audit the actual final output for anything left un-redacted,
and a summary of an "anonymized" document has to summarize the
actually-anonymized text — summarizing the original would defeat the
category exclusions/pseudonymization the user just chose.

`finalize()` also runs a second, later deep-check pass (`deep_check.
find_missed_pii()`) against the true final text, once category exclusions
and person-mode are already applied — a safety net for plain PII the
deterministic pass and the first LLM sweep both missed outright (stray
location names, names left in signature lines, business/reference numbers),
gated behind the same `deep_check` option. See `app/pipeline/deep_check.py`'s
module docstring for why this one needs `excluded_categories` itself rather
than filtering its output afterwards.

Progress reporting: `analyze()`/`analyze_file()`/`finalize()` all accept an
optional `on_progress(stage, current, total)` callback (fired once per stage
start/end, and once per chunk for the two deep-check stages — see
deep_check.py) and an optional `on_plan(stages)` callback. `on_plan` fires
exactly once, as the very first thing each function does, with the full
ordered `[(stage_name, expected_unit_count), ...]` list for the work it is
about to do — raw_text (and therefore chunk counts) is already fully known
at that point in both functions, so the caller (app.server, driving the
progress-bar UI) gets an accurate plan up front rather than having to
discover stages as they happen. `analyze_file()` reports its own "ingest"
stage via on_progress before calling analyze() (whose on_plan intentionally
covers only the stages from analyze() onward — ingest precedes it and isn't
part of that plan).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import DEFAULT_LANGUAGE, SPACY_MODELS
from app.pipeline import anonymize, column_classifier, deep_check
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
    anonymize_requested: bool
    deep_check_requested: bool
    raw_text: str
    presidio_results: list
    deep_check_candidates: list = field(default_factory=list)
    column_candidates: list = field(default_factory=list)
    source_bytes: bytes | None = None  # only set for STRUCTURED_REWRITE_EXTENSIONS
    source_suffix: str | None = None  # original lowercase extension, e.g. ".xlsx"


@dataclass
class FinalizeOutput:
    source_filename: str
    redacted_source_filename: str
    detected_language: str
    anonymization_enabled: bool
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
    on_progress: Callable[[str, int, int], None] | None = None,
    on_plan: Callable[[list[tuple[str, int]]], None] | None = None,
) -> tuple[PendingState, list[DetectedCategory]]:
    language = _resolve_language(options.language_hint, detected_language)

    if not options.anonymize:
        # The user explicitly wants a plain transcript/summary of the
        # original text with no PII detection/redaction at all (see module
        # docstring) — skip straight past Presidio, deep-check, and column
        # classification entirely; there is nothing to review, and
        # finalize() takes the same early-exit path. deep_check is forced
        # off here too (server.py also enforces this) since it's meaningless
        # without anonymization.
        state = PendingState(
            source_filename=source_filename,
            detected_language=detected_language,
            language=language,
            output_mode=options.output_mode,
            anonymize_requested=False,
            deep_check_requested=False,
            raw_text=raw_text,
            presidio_results=[],
            source_bytes=source_bytes,
            source_suffix=source_suffix,
        )
        return state, []

    if on_plan:
        plan: list[tuple[str, int]] = [("presidio_analyze", 1)]
        if options.deep_check:
            plan.append(("deep_check_find", deep_check.estimate_chunk_count(raw_text)))
        on_plan(plan)

    if on_progress:
        on_progress("presidio_analyze", 0, 1)
    presidio_results = anonymize.analyze(raw_text, language)
    categories = anonymize.summarize_categories(raw_text, presidio_results)
    if on_progress:
        on_progress("presidio_analyze", 1, 1)

    # Header-driven column classification (see column_classifier.py) — only
    # possible for structured source formats, which are the only ones
    # source_bytes is ever set for. Deliberately independent of the
    # deep_check toggle: this is free, deterministic, local keyword matching
    # with no LLM involved, not an extra opt-in check.
    column_candidates: list = []
    if source_bytes is not None and source_suffix is not None:
        column_candidates = column_classifier.extract_column_candidates(source_bytes, source_suffix)
        categories += column_classifier.summarize_column_categories(column_candidates, raw_text)

    deep_check_candidates: list = []
    if options.deep_check:
        # Raw text, deliberately — see this module's docstring for why
        # find_candidates() no longer needs a pre-redacted "preliminary"
        # text built for it first.
        deep_check_candidates = deep_check.find_candidates(
            raw_text, language, on_progress=on_progress
        )
        categories += deep_check.summarize_candidate_categories(deep_check_candidates)

    state = PendingState(
        source_filename=source_filename,
        detected_language=detected_language,
        language=language,
        output_mode=options.output_mode,
        anonymize_requested=True,
        deep_check_requested=options.deep_check,
        raw_text=raw_text,
        presidio_results=presidio_results,
        deep_check_candidates=deep_check_candidates,
        column_candidates=column_candidates,
        source_bytes=source_bytes,
        source_suffix=source_suffix,
    )
    return state, categories


def analyze_file(
    path: Path,
    options: PipelineOptions,
    on_progress: Callable[[str, int, int], None] | None = None,
    on_plan: Callable[[list[tuple[str, int]]], None] | None = None,
) -> tuple[PendingState, list[DetectedCategory]]:
    suffix = path.suffix.lower()

    # Reported up front (before analyze()'s own on_plan fires, which only
    # covers the stages from presidio_analyze onward) so the caller always
    # has a non-empty plan to compute progress against — otherwise ingest
    # (which, for audio input, is the entire faster-whisper transcription
    # and often the single slowest stage of the whole job) would show a
    # frozen 0%/unknown-ETA for its whole duration, reproducing the old
    # indefinite spinner for exactly the case this feature most needs to
    # cover. Superseded the moment analyze()'s own on_plan call fires.
    if on_plan:
        on_plan([("ingest", 1)])
    if on_progress:
        on_progress("ingest", 0, 1)
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
    if on_progress:
        on_progress("ingest", 1, 1)

    return analyze(
        raw_text,
        source_filename,
        detected_language,
        options,
        source_bytes=source_bytes,
        source_suffix=suffix if source_bytes is not None else None,
        on_progress=on_progress,
        on_plan=on_plan,
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


def _finalize_without_anonymization(
    state: PendingState,
    on_progress: Callable[[str, int, int], None] | None,
    on_plan: Callable[[list[tuple[str, int]]], None] | None,
) -> FinalizeOutput:
    """finalize()'s entire redaction machinery, skipped: the user explicitly
    asked for a plain transcript/summary of the original text (see module
    docstring). No structured re-export either — with nothing redacted, it
    would just be an unmodified copy of the original file, which isn't worth
    producing. The original filename is kept as-is too, for the same reason
    (anonymize_filename() exists specifically to protect the ANONYMIZED
    output; there's nothing to protect it from here since the user chose not
    to anonymize this document at all)."""
    if on_plan:
        plan: list[tuple[str, int]] = []
        if state.output_mode in (OutputMode.SUMMARY, OutputMode.BOTH):
            plan.append(("summarize", 1))
        plan.append(("render", 1))
        on_plan(plan)

    text = state.raw_text
    pii_audit: list[PiiEntity] = []

    summary = None
    if state.output_mode in (OutputMode.SUMMARY, OutputMode.BOTH):
        if on_progress:
            on_progress("summarize", 0, 1)
        summary = summarize_text(text, state.language, anonymized=False)
        if on_progress:
            on_progress("summarize", 1, 1)

    anonymized_transcript = None
    if state.output_mode in (OutputMode.TRANSCRIPT, OutputMode.BOTH):
        anonymized_transcript = text

    if on_progress:
        on_progress("render", 0, 1)
    transcript_markdown = None
    if anonymized_transcript is not None:
        transcript_markdown = render_transcript(
            source_filename=state.source_filename,
            detected_language=state.detected_language,
            anonymization_enabled=False,
            deep_check_enabled=False,
            anonymized_transcript=anonymized_transcript,
            pii_audit=pii_audit,
        )

    summary_markdown = None
    if summary is not None:
        summary_markdown = render_summary(
            source_filename=state.source_filename,
            detected_language=state.detected_language,
            anonymization_enabled=False,
            deep_check_enabled=False,
            summary=summary,
            pii_audit=pii_audit,
        )
    if on_progress:
        on_progress("render", 1, 1)

    return FinalizeOutput(
        source_filename=state.source_filename,
        redacted_source_filename=state.source_filename,
        detected_language=state.detected_language,
        anonymization_enabled=False,
        deep_check_enabled=False,
        anonymized_transcript=anonymized_transcript,
        summary=summary,
        pii_audit=pii_audit,
        transcript_markdown=transcript_markdown,
        summary_markdown=summary_markdown,
        structured_bytes=None,
        structured_suffix=None,
    )


def finalize(
    state: PendingState,
    excluded_categories: set[str],
    person_mode: PersonMode,
    on_progress: Callable[[str, int, int], None] | None = None,
    on_plan: Callable[[list[tuple[str, int]]], None] | None = None,
) -> FinalizeOutput:
    if not state.anonymize_requested:
        return _finalize_without_anonymization(state, on_progress, on_plan)

    if on_plan:
        plan: list[tuple[str, int]] = [("redact", 1)]
        if state.deep_check_requested:
            plan.append(("deep_check_apply", 1))
            plan.append(("deep_check_missed", deep_check.estimate_chunk_count(state.raw_text)))
            plan.append(("deep_check_locations", deep_check.estimate_chunk_count(state.raw_text)))
        if state.output_mode in (OutputMode.SUMMARY, OutputMode.BOTH):
            plan.append(("summarize", 1))
        plan.append(("render", 1))
        if state.source_bytes is not None and state.source_suffix is not None:
            plan.append(("structured_rewrite", 1))
        on_plan(plan)

    if on_progress:
        on_progress("redact", 0, 1)

    # Built once and reused for the transcript AND every structured-format
    # cell below, so the same real name maps to the same label (number or
    # fake name) across every output produced from this one finalize() call.
    person_replacer = None
    if person_mode == PersonMode.PSEUDONYMIZE:
        person_replacer = make_person_pseudonymizer(state.language)
    elif person_mode == PersonMode.NUMBERED:
        person_replacer = make_person_numberer()

    # Column-classified values (see column_classifier.py) are matched by
    # their FULL original text, so this must run BEFORE Presidio's own
    # redaction below, against the pristine raw_text — applying it AFTER
    # would let Presidio's per-span NER catch just PART of a multi-token
    # classified value first (e.g. a full street address's city/postal-code
    # tail), permanently defeating the exact-whole-value match for whatever
    # wasn't independently caught and leaving it exposed in the output (a
    # real gap, found and fixed before release). state.presidio_results'
    # offsets are only valid against the ORIGINAL raw_text, so once column
    # candidates change the text, Presidio must be re-run fresh against the
    # result — only paid for documents that actually have column candidates
    # (i.e. structured sources with classifiable headers); every other
    # document reuses the precomputed results exactly as before.
    working_text = state.raw_text
    column_pii_audit: list[PiiEntity] = []
    presidio_results_for_redaction = state.presidio_results
    if state.column_candidates:
        column_result = deep_check.apply_candidates(
            working_text,
            state.column_candidates,
            excluded_categories,
            source="column_header",
            person_replacer=person_replacer,
            person_category="PERSON_SPALTE",
        )
        working_text = column_result.anonymized_text
        column_pii_audit = column_result.entities
        presidio_results_for_redaction = anonymize.analyze(working_text, state.language)

    anon_result = anonymize.apply_anonymization(
        working_text,
        presidio_results_for_redaction,
        excluded_types=excluded_categories,
        person_mode=person_mode,
        person_replacer=person_replacer,
    )
    text = anon_result.anonymized_text
    pii_audit = list(anon_result.entities) + column_pii_audit

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
    if on_progress:
        on_progress("redact", 1, 1)

    if state.deep_check_requested:
        if on_progress:
            on_progress("deep_check_apply", 0, 1)
        dc_result = deep_check.apply_candidates(text, state.deep_check_candidates, excluded_categories)
        text = dc_result.anonymized_text
        pii_audit += dc_result.entities
        if on_progress:
            on_progress("deep_check_apply", 1, 1)

        # A second, later LLM sweep against the now-fully-redacted final
        # text, looking for plain PII the pass above still missed outright
        # (stray location names, names left in signature lines, business/
        # file reference numbers) rather than indirect/contextual clues.
        # Unlike find_candidates() above (run once during analyze(), before
        # the user's category choices exist), this runs here against the
        # true final text so it can be told which categories to leave alone.
        missed_candidates = deep_check.find_missed_pii(
            text, state.language, excluded_categories, on_progress=on_progress
        )
        if missed_candidates:
            missed_result = deep_check.apply_candidates(
                text, missed_candidates, excluded_categories, source="llm_final_check"
            )
            text = missed_result.anonymized_text
            pii_audit += missed_result.entities

        # A third, narrowly-scoped sweep focused on nothing but place/city
        # names — see deep_check.find_missed_locations()'s docstring for why
        # this is worth a dedicated call rather than folding into the
        # general sweep above.
        missed_locations = deep_check.find_missed_locations(
            text, state.language, excluded_categories, on_progress=on_progress
        )
        if missed_locations:
            location_result = deep_check.apply_candidates(
                text, missed_locations, excluded_categories, source="llm_final_check"
            )
            text = location_result.anonymized_text
            pii_audit += location_result.entities

    summary = None
    if state.output_mode in (OutputMode.SUMMARY, OutputMode.BOTH):
        if on_progress:
            on_progress("summarize", 0, 1)
        summary = summarize_text(text, state.language)
        if on_progress:
            on_progress("summarize", 1, 1)

    anonymized_transcript = None
    if state.output_mode in (OutputMode.TRANSCRIPT, OutputMode.BOTH):
        anonymized_transcript = text

    if on_progress:
        on_progress("render", 0, 1)
    transcript_markdown = None
    if anonymized_transcript is not None:
        transcript_markdown = render_transcript(
            source_filename=redacted_source_filename,
            detected_language=state.detected_language,
            anonymization_enabled=True,
            deep_check_enabled=state.deep_check_requested,
            anonymized_transcript=anonymized_transcript,
            pii_audit=pii_audit,
        )

    summary_markdown = None
    if summary is not None:
        summary_markdown = render_summary(
            source_filename=redacted_source_filename,
            detected_language=state.detected_language,
            anonymization_enabled=True,
            deep_check_enabled=state.deep_check_requested,
            summary=summary,
            pii_audit=pii_audit,
        )
    if on_progress:
        on_progress("render", 1, 1)

    structured_bytes: bytes | None = None
    structured_suffix: str | None = None
    if state.source_bytes is not None and state.source_suffix is not None:
        if on_progress:
            on_progress("structured_rewrite", 0, 1)

        def cell_transform(cell_text: str) -> str:
            # Column candidates matched BEFORE Presidio's per-cell analyze,
            # against the pristine cell text — same reasoning as the main
            # text above: a multi-token classified value needs to be matched
            # as one whole unit before Presidio's own NER has a chance to
            # catch only part of it. No extra cost here (unlike the main
            # text path): analyze() already runs fresh per cell regardless
            # of ordering, so this just changes what it's run against.
            working_cell_text = cell_text
            if state.column_candidates:
                working_cell_text = deep_check.apply_candidates(
                    working_cell_text,
                    state.column_candidates,
                    excluded_categories,
                    source="column_header",
                    person_replacer=person_replacer,
                    person_category="PERSON_SPALTE",
                ).anonymized_text
            cell_results = anonymize.analyze(working_cell_text, state.language)
            cell_anon = anonymize.apply_anonymization(
                working_cell_text,
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
        if on_progress:
            on_progress("structured_rewrite", 1, 1)

    return FinalizeOutput(
        source_filename=state.source_filename,
        redacted_source_filename=redacted_source_filename,
        detected_language=state.detected_language,
        anonymization_enabled=True,
        deep_check_enabled=state.deep_check_requested,
        anonymized_transcript=anonymized_transcript,
        summary=summary,
        pii_audit=pii_audit,
        transcript_markdown=transcript_markdown,
        summary_markdown=summary_markdown,
        structured_bytes=structured_bytes,
        structured_suffix=structured_suffix,
    )
