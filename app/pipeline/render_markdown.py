from datetime import datetime

from app.schemas import PiiEntity

_SOURCE_LABELS = {
    "presidio": "Presidio",
    "llm_deep_check": "LLM-Tiefencheck",
    "llm_final_check": "LLM-Nachkontrolle",
}


def render_transcript(
    source_filename: str,
    detected_language: str,
    deep_check_enabled: bool,
    anonymized_transcript: str,
    pii_audit: list[PiiEntity],
) -> str:
    """Build the standalone anonymized-transcript document."""
    return _render_document(
        source_filename=source_filename,
        document_type="Transkript",
        detected_language=detected_language,
        deep_check_enabled=deep_check_enabled,
        body_heading="Transkript",
        body_text=anonymized_transcript,
        pii_audit=pii_audit,
    )


def render_summary(
    source_filename: str,
    detected_language: str,
    deep_check_enabled: bool,
    summary: str,
    pii_audit: list[PiiEntity],
) -> str:
    """Build the standalone summary document — always a separate file from the
    transcript, since the two serve different audiences/purposes."""
    return _render_document(
        source_filename=source_filename,
        document_type="Zusammenfassung",
        detected_language=detected_language,
        deep_check_enabled=deep_check_enabled,
        body_heading="Zusammenfassung",
        body_text=summary,
        pii_audit=pii_audit,
    )


def _render_document(
    source_filename: str,
    document_type: str,
    detected_language: str,
    deep_check_enabled: bool,
    body_heading: str,
    body_text: str,
    pii_audit: list[PiiEntity],
) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = [
        f"# {source_filename}",
        _render_metadata(timestamp, document_type, detected_language, deep_check_enabled),
        f"## {body_heading}\n\n{body_text}",
        _render_pii_audit(pii_audit),
    ]
    return "\n\n".join(sections) + "\n"


def _render_metadata(timestamp: str, document_type: str, detected_language: str, deep_check_enabled: bool) -> str:
    deep_check_label = "ja" if deep_check_enabled else "nein"
    lines = [
        f"- **Dokumenttyp:** {document_type}",
        f"- **Verarbeitet am:** {timestamp}",
        f"- **Erkannte Sprache:** {detected_language}",
        f"- **LLM-Tiefencheck:** {deep_check_label}",
    ]
    return "\n".join(lines)


def _render_pii_audit(pii_audit: list[PiiEntity]) -> str:
    header = "## Anonymisierungs-Protokoll"

    if not pii_audit:
        return f"{header}\n\nEs wurden keine personenbezogenen Daten erkannt."

    rows = sorted(pii_audit, key=lambda entity: entity.count, reverse=True)
    table_lines = [
        "| Kategorie | Anzahl | Quelle |",
        "| --- | --- | --- |",
    ]
    for entity in rows:
        source_label = _SOURCE_LABELS.get(entity.source, entity.source)
        table_lines.append(f"| {entity.entity_type} | {entity.count} | {source_label} |")

    return header + "\n\n" + "\n".join(table_lines)
