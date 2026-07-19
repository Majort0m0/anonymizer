"""Shared data contracts used across the pipeline, backend, and frontend.

Every pipeline module (parsers, transcription, anonymize, deep_check,
summarize, render_markdown) imports from here so the modules can be built
independently against a single, stable contract.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OutputMode(str, Enum):
    TRANSCRIPT = "transcript"
    SUMMARY = "summary"
    BOTH = "both"


class PersonMode(str, Enum):
    REDACT = "redact"  # every PERSON match -> generic "[PERSON]"
    NUMBERED = "numbered"  # distinct names -> "[PERSON1]", "[PERSON2]", ... (first-seen order)
    PSEUDONYMIZE = "pseudonymize"  # distinct names -> consistent fake full names


class SourceKind(str, Enum):
    TEXT = "text"      # .txt, .md, clipboard paste
    DOCX = "docx"      # .docx (legacy .doc is rejected with an actionable error)
    PDF = "pdf"
    EXCEL = "excel"    # .xlsx, .xls
    CSV = "csv"
    JSON = "json"
    ODF = "odf"        # .odt, .ods, .odp
    AUDIO = "audio"    # .mp3, .wav, .m4a, ...


class PipelineOptions(BaseModel):
    output_mode: OutputMode = OutputMode.BOTH
    anonymize: bool = True  # False -> skip PII detection/redaction entirely,
    # producing a plain transcript/summary of the original text (see
    # app.pipeline.pipeline's module docstring). deep_check is meaningless
    # without it and is forced off server-side when this is False.
    deep_check: bool = True
    language_hint: Optional[str] = None  # "de" | "en" | None -> auto-detect


class PiiEntity(BaseModel):
    entity_type: str
    count: int
    source: str  # "presidio" | "llm_deep_check" | "llm_final_check" | "column_header"


class AnonymizeResult(BaseModel):
    anonymized_text: str
    entities: list[PiiEntity] = Field(default_factory=list)


class Occurrence(BaseModel):
    """One specific matched text span within a DetectedCategory, for the
    per-occurrence review checklist — every match gets its own checkbox
    rather than the category being all-or-nothing. `context_before`/
    `context_after` are short surrounding-text snippets (already ellipsis-
    truncated server-side) so a reviewer can judge a flagged span in context
    instead of as a bare, ambiguous word; the frontend renders
    `context_before` + (highlighted) `text` + `context_after`."""

    id: str
    text: str
    context_before: str = ""
    context_after: str = ""


class DetectedCategory(BaseModel):
    """One row in the pre-finalize category review the user sees before the
    anonymization is actually applied."""

    category: str
    count: int
    source: str  # "presidio" | "llm_deep_check" | "column_header" — never
    # "llm_final_check" (see deep_check.py: that pass runs post-finalize,
    # with no review step, unlike column_header which classifies columns
    # from the original bytes during analyze() and so DOES get reviewed)
    occurrences: list[Occurrence] = Field(default_factory=list)
    is_person: bool = False  # true only for Presidio's PERSON category — the
    # frontend uses this to decide whether to offer the person-mode toggle
    # (Schwärzen/Nummerieren/Pseudonymisieren)


class PendingAnalysis(BaseModel):
    """Returned by /api/analyze-*: what was found, not yet redacted. The
    server keeps the actual raw text and detection results server-side,
    keyed by `token`; the client only ever sees the aggregated categories."""

    token: str
    source_filename: str
    detected_language: str
    output_mode: OutputMode
    anonymize: bool
    deep_check: bool
    categories: list[DetectedCategory] = Field(default_factory=list)


class FinalizeRequest(BaseModel):
    token: str
    # Every occurrence id (see Occurrence/DetectedCategory above) the user
    # left unchecked — a whole-category exclusion is just every occurrence
    # id belonging to that category. See app.pipeline.occurrences and
    # app.pipeline.pipeline.finalize() for how this is turned back into an
    # actual redaction exclusion.
    excluded_occurrence_ids: list[str] = Field(default_factory=list)
    person_mode: PersonMode = PersonMode.REDACT


class IngestResult(BaseModel):
    """Output of the ingest stage: raw extracted text plus provenance."""

    source_filename: str
    source_kind: SourceKind
    raw_text: str
    detected_language: Optional[str] = None


class DownloadableFile(BaseModel):
    """One entry in a finalize response's download list. A single finalize
    call can produce several files: the anonymized transcript, a separate
    summary document, and — for tabular source formats (xlsx/csv/json/ods) —
    an anonymized copy in the original format."""

    label: str
    filename: str


class PipelineResult(BaseModel):
    source_filename: str
    detected_language: str
    anonymization_enabled: bool = True
    deep_check_enabled: bool = False
    anonymized_transcript: Optional[str] = None
    summary: Optional[str] = None
    pii_audit: list[PiiEntity] = Field(default_factory=list)
    downloads: list[DownloadableFile] = Field(default_factory=list)


class ReplaceTextRequest(BaseModel):
    """Manual post-finalize find/replace — e.g. fixing a word an audio
    transcription misheard. Operates on the already-finalized transcript/
    summary text the client already holds (from the finalize or a previous
    replace response), not on any server-side token/state; re-renders and
    re-saves the markdown output(s) so downloads stay in sync with on-screen
    corrections. Does not touch a structured-format (xlsx/csv/json/ods)
    output, if one exists — see CLAUDE.md."""

    source_filename: str
    detected_language: str
    anonymization_enabled: bool = True
    deep_check_enabled: bool = False
    anonymized_transcript: Optional[str] = None
    summary: Optional[str] = None
    pii_audit: list[PiiEntity] = Field(default_factory=list)
    # Passed through so a structured-format download (xlsx/csv/json/ods),
    # which this endpoint does not regenerate, isn't silently dropped from
    # the response — only the markdown entries get replaced with fresh ones.
    downloads: list[DownloadableFile] = Field(default_factory=list)
    search: str
    replacement: str = ""
    match_case: bool = False
    replace_all: bool = True


class DependencyStatus(BaseModel):
    name: str
    available: bool
    detail: str = ""
    install_hint: str = ""
