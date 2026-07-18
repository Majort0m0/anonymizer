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
    deep_check: bool = True
    language_hint: Optional[str] = None  # "de" | "en" | None -> auto-detect


class PiiEntity(BaseModel):
    entity_type: str
    count: int
    source: str  # "presidio" | "llm_deep_check" | "llm_final_check"


class AnonymizeResult(BaseModel):
    anonymized_text: str
    entities: list[PiiEntity] = Field(default_factory=list)


class DetectedCategory(BaseModel):
    """One row in the pre-finalize category review the user sees before the
    anonymization is actually applied."""

    category: str
    count: int
    source: str  # "presidio" | "llm_deep_check" — never "llm_final_check" (see
    # deep_check.py: that pass runs post-finalize, with no review step)
    samples: list[str] = Field(default_factory=list)
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
    deep_check: bool
    categories: list[DetectedCategory] = Field(default_factory=list)


class FinalizeRequest(BaseModel):
    token: str
    excluded_categories: list[str] = Field(default_factory=list)
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
