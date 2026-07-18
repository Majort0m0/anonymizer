"""Summarization of text via the local Ollama model.

Normally given the final, fully-processed anonymized text (see
app.pipeline.pipeline's module docstring: this is a correctness requirement,
not a privacy precaution — a summary of an "anonymized" document has to
summarize the actually-anonymized text, or it would leak exactly what the
user asked to have redacted). The one deliberate exception is
`PipelineOptions.anonymize=False` (pipeline.py's
`_finalize_without_anonymization()`), where the user explicitly chose to
skip redaction entirely — there, this is called with the raw original text
instead, via `anonymized=False` below, which selects a prompt that doesn't
falsely claim placeholders are present.
"""

from __future__ import annotations

from app.llm.ollama_client import generate

_SYSTEM_ANONYMIZED_DE = (
    "Du bist ein Assistent, der bereits anonymisierte Texte zusammenfasst. "
    "Der Eingabetext wurde bereits anonymisiert: eckige Platzhalter wie [PERSON], "
    "[EMAIL_ADDRESS] oder [ORGANIZATION] sind absichtliche Schwärzungen und stehen für "
    "entfernte personenbezogene Daten. Übernimm solche Platzhalter unverändert in deine "
    "Zusammenfassung, wenn du auf die betreffende Stelle Bezug nimmst. Erfinde niemals "
    "Namen oder Details für diese Platzhalter und entferne sie nicht. "
    "Erstelle eine prägnante, gut strukturierte Zusammenfassung: ein kurzer Absatz mit "
    "den wichtigsten Punkten und, falls es der Inhalt hergibt, zusätzlich einige "
    "Stichpunkte mit Kernfakten. Antworte ausschließlich mit der Zusammenfassung, ohne "
    "Einleitung oder Meta-Kommentar."
)

_SYSTEM_ANONYMIZED_EN = (
    "You are an assistant that summarizes already-anonymized text. "
    "The input text has already been anonymized: bracketed placeholders such as "
    "[PERSON], [EMAIL_ADDRESS], or [ORGANIZATION] are intentional redactions standing "
    "in for removed personal data. Preserve these placeholders verbatim whenever you "
    "refer to the corresponding detail. Never invent names or details for these "
    "placeholders and never remove them. "
    "Produce a concise, well-structured summary: a short paragraph covering the key "
    "points and, if the content warrants it, a few bullet points of key facts. Respond "
    "with only the summary, no preamble or meta-commentary."
)

_SYSTEM_RAW_DE = (
    "Du bist ein Assistent, der Texte zusammenfasst. "
    "Erstelle eine prägnante, gut strukturierte Zusammenfassung des folgenden Textes: "
    "ein kurzer Absatz mit den wichtigsten Punkten und, falls es der Inhalt hergibt, "
    "zusätzlich einige Stichpunkte mit Kernfakten. Antworte ausschließlich mit der "
    "Zusammenfassung, ohne Einleitung oder Meta-Kommentar."
)

_SYSTEM_RAW_EN = (
    "You are an assistant that summarizes text. "
    "Produce a concise, well-structured summary of the following text: a short "
    "paragraph covering the key points and, if the content warrants it, a few bullet "
    "points of key facts. Respond with only the summary, no preamble or meta-commentary."
)


def summarize_text(text: str, language: str, anonymized: bool = True) -> str:
    is_de = language.lower().startswith("de")
    if anonymized:
        system = _SYSTEM_ANONYMIZED_DE if is_de else _SYSTEM_ANONYMIZED_EN
    else:
        system = _SYSTEM_RAW_DE if is_de else _SYSTEM_RAW_EN
    return generate(prompt=text, system=system).strip()
