"""PII detection and redaction — the privacy boundary of the app.

This module's output (never the raw ingested text) is what may legally be
handed on to the LLM stages (deep_check, summarize). The AnalyzerEngine and
its NLP models are expensive to build, so they are constructed once, lazily,
and reused for every call.

Detection (`analyze`) and redaction (`apply_anonymization`) are deliberately
split: the app shows the user what categories were found before actually
redacting anything, so they can opt specific categories out of redaction.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import PhoneRecognizer
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

from app.config import (
    DEFAULT_LANGUAGE,
    RELEVANT_ID_COUNTRIES,
    SPACY_MODELS,
    SUPPORTED_PHONE_REGIONS,
)
from app.pipeline.occurrences import OccurrenceRef, occurrences_for_offsets
from app.pipeline.pseudonymize import make_person_numberer, make_person_pseudonymizer
from app.schemas import AnonymizeResult, DetectedCategory, PersonMode, PiiEntity

_analyzer: AnalyzerEngine | None = None
_anonymizer = AnonymizerEngine()
_build_lock = threading.Lock()

_POSTAL_CODE_SCORE = 0.85

# Presidio ships no generic postal-code recognizer, and a plain "digits
# followed by a capitalized word" pattern is far too noisy in German: every
# noun is capitalized, so things like "54321 Stück" or "12345 Einheiten"
# false-positive constantly. Instead, a 4-5 digit run is only treated as a
# postal code if it sits immediately before a span spaCy's NER already
# recognized as a LOCATION (the actual "<PLZ> <Ort>" address convention) —
# requiring an NER-confirmed place name is a much stronger signal than
# capitalization alone.
_DIGITS_BEFORE_LOCATION_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)\s+$")


def _find_postal_codes(text: str, results: list) -> list:
    postal_codes = []
    for result in results:
        if result.entity_type != "LOCATION":
            continue
        match = _DIGITS_BEFORE_LOCATION_RE.search(text[: result.start])
        if match:
            postal_codes.append(
                RecognizerResult(
                    entity_type="POSTAL_CODE",
                    start=match.start(1),
                    end=match.end(1),
                    score=_POSTAL_CODE_SCORE,
                )
            )
    return postal_codes


def _is_inside_bracketed_placeholder(text: str, start: int, end: int) -> bool:
    """True if `text[start:end]` is immediately wrapped in "[" and "]" —
    i.e. it's (all of, or a piece of) an already-applied redaction
    placeholder like "[PERSON4]" or "[LOCATION]", not real content.

    analyze() can be called on text that's already partially redacted
    (app.pipeline.pipeline.finalize() re-runs Presidio fresh after
    column_classifier's whole-value substitutions change the text — see its
    docstring) — spaCy's NER can then mistake the placeholder's own inner
    text for a real entity (observed directly: given `"[PERSON4]"`, it
    tagged the substring "PERSON4" — without the brackets — as a PERSON,
    score 0.85). Redacting that "again" would wrap an existing placeholder
    in a second one (e.g. "[[PERSON5]]"), corrupting the output for no
    privacy benefit — the content is already redacted.
    """
    return start > 0 and end < len(text) and text[start - 1] == "[" and text[end] == "]"


def _drop_bracketed_placeholder_matches(text: str, results: list) -> list:
    return [r for r in results if not _is_inside_bracketed_placeholder(text, r.start, r.end)]


def _missing_model_error(exc: OSError) -> RuntimeError:
    message = str(exc)
    for model_name in SPACY_MODELS.values():
        if model_name in message:
            return RuntimeError(
                f"Missing spaCy model '{model_name}'. Install it with: "
                f"python -m spacy download {model_name}"
            )
    hints = " ; ".join(f"python -m spacy download {m}" for m in SPACY_MODELS.values())
    return RuntimeError(
        "A required spaCy model is missing. Install the configured models with: "
        f"{hints}"
    )


def _build_analyzer() -> AnalyzerEngine:
    supported_languages = list(SPACY_MODELS.keys())

    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": lang, "model_name": model_name}
            for lang, model_name in SPACY_MODELS.items()
        ],
    }
    nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()

    try:
        nlp_engine.load()
    except OSError as exc:
        raise _missing_model_error(exc) from exc

    registry = RecognizerRegistry(supported_languages=supported_languages)
    registry.load_predefined_recognizers(
        languages=supported_languages,
        nlp_engine=nlp_engine,
        countries=RELEVANT_ID_COUNTRIES,
    )

    registry.remove_recognizer("PhoneRecognizer")
    for lang in supported_languages:
        registry.add_recognizer(
            PhoneRecognizer(
                supported_language=lang,
                supported_regions=SUPPORTED_PHONE_REGIONS,
            )
        )

    return AnalyzerEngine(
        nlp_engine=nlp_engine,
        registry=registry,
        supported_languages=supported_languages,
    )


def _get_analyzer() -> AnalyzerEngine:
    global _analyzer
    if _analyzer is None:
        with _build_lock:
            if _analyzer is None:
                _analyzer = _build_analyzer()
    return _analyzer


def _resolve_overlaps(results: list) -> list:
    """Greedily keep the highest-scoring, non-overlapping spans.

    Presidio's own cross-type conflict handling drops one side of an overlap
    based on iteration order (effectively: whichever span ends later wins),
    not confidence — so a stray low-score NER false positive can silently
    swallow a high-score, checksum-validated match (e.g. an IBAN). This
    resolves overlaps ourselves, by score, before anonymizing.
    """
    ordered = sorted(results, key=lambda r: (-r.score, r.start))
    selected: list = []
    for candidate in ordered:
        if not any(candidate.start < s.end and s.start < candidate.end for s in selected):
            selected.append(candidate)
    return sorted(selected, key=lambda r: r.start)


def resolve_language(language: str) -> str:
    return language if language in SPACY_MODELS else DEFAULT_LANGUAGE


def analyze(text: str, language: str) -> list:
    """Detect PII without redacting anything yet.

    Returns Presidio RecognizerResult objects (overlap-resolved by score).
    These are plain in-memory objects, not pydantic models — callers keep
    them server-side (see app.server's token cache) rather than serializing
    them to the frontend; only `summarize_categories()`'s output crosses the
    HTTP boundary.

    Safe to call on text that's already partially redacted (some callers do
    — see app.pipeline.pipeline.finalize()) — matches wholly inside an
    existing "[...]" placeholder are dropped (see
    _is_inside_bracketed_placeholder()) rather than risking a
    double-redaction like "[[PERSON5]]".
    """
    lang = resolve_language(language)
    analyzer = _get_analyzer()
    raw_results = analyzer.analyze(text=text, language=lang)
    if not raw_results:
        return []

    resolved = _resolve_overlaps(raw_results)
    resolved = _drop_bracketed_placeholder_matches(text, resolved)
    if not resolved:
        return []
    postal_codes = _find_postal_codes(text, resolved)
    return _resolve_overlaps(resolved + postal_codes) if postal_codes else resolved


def summarize_categories(
    text: str, results: list, source: str = "presidio"
) -> tuple[list[DetectedCategory], dict[str, OccurrenceRef]]:
    """Aggregate detection results into full per-occurrence category rows for
    the pre-finalize review UI, plus the server-internal OccurrenceRef map
    (kept on PendingState) that lets finalize() translate the frontend's
    chosen occurrence ids back into an actual exclusion."""
    by_type: dict[str, list] = {}
    for result in results:
        by_type.setdefault(result.entity_type, []).append(result)

    categories: list[DetectedCategory] = []
    all_refs: dict[str, OccurrenceRef] = {}
    for entity_type, type_results in by_type.items():
        spans = [(r.start, r.end) for r in sorted(type_results, key=lambda r: r.start)]
        occurrences, refs = occurrences_for_offsets(entity_type, source, text, spans)
        all_refs.update(refs)
        categories.append(
            DetectedCategory(
                category=entity_type,
                count=len(occurrences),
                source=source,
                occurrences=occurrences,
                is_person=(source == "presidio" and entity_type == "PERSON"),
            )
        )
    return categories, all_refs


def occurrence_ids_for_categories(results: list, excluded_categories: set[str]) -> set[str]:
    """Translate a whole-category exclusion set into the occurrence-id shape
    apply_anonymization() expects, against WHATEVER results list is actually
    about to be redacted. Used wherever occurrence-level identity from the
    review step doesn't (or can't) survive to this point — the per-cell
    structured re-export, and pipeline.finalize()'s column_candidates
    re-analyze fallback — since both re-run Presidio fresh against a text
    whose offsets bear no relation to the ones the review UI showed."""
    return {f"p:{r.start}:{r.end}" for r in results if r.entity_type in excluded_categories}


def apply_anonymization(
    text: str,
    results: list,
    excluded_occurrence_ids: set[str] | None = None,
    person_mode: PersonMode = PersonMode.REDACT,
    person_replacer: Callable[[str], str] | None = None,
) -> AnonymizeResult:
    """Redact `results`, skipping any whose occurrence id ("p:<start>:<end>",
    see app.pipeline.occurrences) is in `excluded_occurrence_ids` (left as
    original text) — excluding an entire category is just excluding every one
    of its occurrence ids, which is exactly what the frontend sends when a
    user unchecks a category's master checkbox. `person_mode` controls what
    PERSON matches become: PersonMode.REDACT -> the generic "[PERSON]" (like
    every other category), PersonMode.NUMBERED -> consistent numbered
    placeholders ("[PERSON1]", "[PERSON2]", ...) so a reader can still tell
    distinct people apart without seeing any name, PersonMode.PSEUDONYMIZE ->
    consistent fake full names.

    Pass an existing `person_replacer` (from `make_person_numberer()` or
    `make_person_pseudonymizer()`) when this is called multiple times for the
    same document/export — e.g. once for the markdown transcript and once per
    cell for a structured-format re-export — so the same real name maps to the
    same label everywhere. If omitted (and person_mode isn't REDACT), a fresh
    one is created (consistent only within this single call).
    """
    excluded_occurrence_ids = excluded_occurrence_ids or set()
    filtered = [r for r in results if f"p:{r.start}:{r.end}" not in excluded_occurrence_ids]

    if not filtered:
        return AnonymizeResult(anonymized_text=text, entities=[])

    counts: dict[str, int] = {}
    for result in filtered:
        counts[result.entity_type] = counts.get(result.entity_type, 0) + 1

    if person_mode != PersonMode.REDACT and "PERSON" in counts:
        if person_replacer is None:
            person_replacer = (
                make_person_pseudonymizer()
                if person_mode == PersonMode.PSEUDONYMIZE
                else make_person_numberer()
            )
        # Presidio's AnonymizerEngine does not guarantee it invokes custom
        # operator callbacks in left-to-right text order (observed: it
        # processes right-to-left, to keep not-yet-replaced spans' offsets
        # valid while replacing). Left to its own order, "[PERSON1]" could
        # land on whichever name happens to sit last in the text instead of
        # the first-mentioned one — undermining exactly what numbered mode
        # is for. Both make_person_numberer() and make_person_pseudonymizer()
        # assign on first call, so pre-seeding the closure here, sorted by
        # true start position, fixes the assignment order regardless of
        # whatever order Presidio calls it in below; already-assigned names
        # (from an earlier call reusing the same person_replacer) are a
        # no-op here since the closures never reassign an existing key.
        for result in sorted(filtered, key=lambda r: r.start):
            if result.entity_type == "PERSON":
                person_replacer(text[result.start : result.end])

    operators: dict[str, OperatorConfig] = {}
    for entity_type in counts:
        if entity_type == "PERSON" and person_mode != PersonMode.REDACT and person_replacer is not None:
            operators[entity_type] = OperatorConfig("custom", {"lambda": person_replacer})
        else:
            operators[entity_type] = OperatorConfig("replace", {"new_value": f"[{entity_type}]"})

    anonymized = _anonymizer.anonymize(text=text, analyzer_results=filtered, operators=operators)

    # Count from anonymized.items (the replacements actually applied after Presidio's
    # own overlap resolution), not the raw analyzer results, which can contain
    # overlapping candidate detections that never make it into the output text.
    applied_counts: dict[str, int] = {}
    for item in anonymized.items:
        applied_counts[item.entity_type] = applied_counts.get(item.entity_type, 0) + 1

    entities = [
        PiiEntity(entity_type=entity_type, count=count, source="presidio")
        for entity_type, count in applied_counts.items()
    ]

    return AnonymizeResult(anonymized_text=anonymized.text, entities=entities)


_FILENAME_SEPARATOR_RE = re.compile(r"[_-]")


def anonymize_filename(
    filename: str,
    language: str,
    excluded_categories: set[str] | None = None,
    person_mode: PersonMode = PersonMode.REDACT,
    person_replacer: Callable[[str], str] | None = None,
) -> str:
    """Redact PII out of an original upload filename before it is used to
    build a saved output filename or embedded as a document title.

    spaCy's NER lumps an underscore-joined stem like "Max_Mustermann_Vertrag"
    into a single MISC span instead of recognizing "Max Mustermann" as a
    PERSON, but handles hyphens and spaces fine (verified interactively) — so
    separators are normalized to spaces before detection. The substitution is
    1-for-1 (never changes length), which keeps analyze()'s character offsets
    valid for apply_anonymization() to redact the same span it detected.

    Takes a whole-category exclusion set (not occurrence ids) — this is a
    completely separate analyze() call over just the filename stem, so the
    body text's occurrence ids have no meaning here; occurrence_ids_for_
    categories() re-derives the equivalent exclusion against these fresh
    results instead.
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    spaced_stem = _FILENAME_SEPARATOR_RE.sub(" ", stem)
    results = analyze(spaced_stem, language)
    excluded_ids = occurrence_ids_for_categories(results, excluded_categories or set())
    anonymized = apply_anonymization(
        spaced_stem,
        results,
        excluded_occurrence_ids=excluded_ids,
        person_mode=person_mode,
        person_replacer=person_replacer,
    )
    return anonymized.anonymized_text + suffix
