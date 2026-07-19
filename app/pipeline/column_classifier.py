"""Detects PII categories from tabular column headers (e.g. "Vorname",
"Email", "PLZ") for structured source formats (xlsx/xls/csv/json/ods).

Presidio's NER performs poorly on short, context-free tokens sitting alone in
a table cell — a fictional first name like "Shaggy" or a made-up place like
"Atlantis" gives spaCy nothing to key off, and this is worse than ordinary
prose (see the ingest step's tabular-NER caveat in CLAUDE.md). But a table's
own column headers already state each column's category unambiguously and
require no guessing at all. This module reads the ORIGINAL structured file's
bytes (the same `source_bytes` PendingState already keeps for the structured
re-export — see pipeline.py) purely to classify columns and collect every
data-row cell value in a classified column as a redaction candidate — same
shape as deep_check's candidates ({"text", "category"}), so they can be
applied via the EXISTING deep_check.apply_candidates() against both the
flattened transcript text and, independently, each isolated cell during the
structured re-export. No new redaction mechanism needed.

Only ever reads the raw structured bytes already held server-side; never
sends anything over the network or to an LLM — this is pure local, keyword-
based header matching, deliberately simple and auditable.

Category strings are deliberately distinct from Presidio's own vocabulary
(PERSON_SPALTE, not PERSON) rather than reusing it — categories double as the
key `excluded_categories` matches against, and if a column-classified finding
shared a category string with a Presidio finding, unchecking either one's
review-UI row would exclude BOTH (there being only one shared string), even
though the user most likely means to exclude just the row they unchecked.
This mirrors why deep_check.py's own findings are always free-form labels
distinct from Presidio's fixed categories.
"""

from __future__ import annotations

import csv
import io
import json
import re

from app.pipeline.occurrences import OccurrenceRef, occurrences_for_text_match
from app.schemas import DetectedCategory

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_header(header: str) -> str:
    return _NORMALIZE_RE.sub("", header.lower())


# Keyword lists are pre-normalized (lowercase, no separators) so they can be
# compared directly against _normalize_header()'s output via substring
# containment. Order matters: categories are checked in this order, and the
# first match wins — narrower/more distinctive keywords (email, iban, phone,
# postal code) are listed before broader ones (person, organization) so e.g.
# a header like "Diensttelefon" matches PHONE_NUMBER, not PERSON.
_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("EMAIL_SPALTE", ["email", "e-mail", "mail"]),
    ("IBAN_SPALTE", ["iban"]),
    ("PLZ_SPALTE", ["plz", "postleitzahl", "zipcode", "zip", "postalcode", "postcode"]),
    (
        "TELEFON_SPALTE",
        ["telefon", "tel", "handy", "mobil", "rufnummer", "phone", "mobile", "fax"],
    ),
    (
        "DATUM_SPALTE",
        ["geburtsdatum", "geburtstag", "birthdate", "dateofbirth", "dob"],
    ),
    (
        "ORT_SPALTE",
        [
            "wohnort",
            "ort",
            "stadt",
            "gemeinde",
            "wohnadresse",
            "adresse",
            "anschrift",
            "strasse",
            "city",
            "address",
            "place",
        ],
    ),
    (
        "PERSON_SPALTE",
        [
            "vorname",
            "nachname",
            "familienname",
            "nachname",
            "vollstaendigername",
            "name",
            "firstname",
            "lastname",
            "givenname",
            "surname",
            "fullname",
            "ansprechpartner",
            "kontaktperson",
        ],
    ),
    (
        "ORGANISATION_SPALTE",
        ["firma", "unternehmen", "arbeitgeber", "company", "employer"],
    ),
]

# Every keyword pre-normalized once at import time, in the same
# (category, [keywords]) shape as _CATEGORY_KEYWORDS.
_NORMALIZED_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    (category, [_normalize_header(kw) for kw in keywords]) for category, keywords in _CATEGORY_KEYWORDS
]


def classify_header(header: str) -> str | None:
    """Return the category a column header implies, or None if it doesn't
    match anything recognized — such a column is simply left to whatever
    other detection (Presidio, deep-check) already covers it, no regression
    from today's behavior."""
    if not header or not header.strip():
        return None
    normalized = _normalize_header(header)
    if not normalized:
        return None
    for category, keywords in _NORMALIZED_CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return None


def _candidates_from_table(rows: list[list[str]]) -> list[dict]:
    """rows[0] is the header row; the header text itself is never a
    candidate (it's not user data, it's the label that classified the
    column) — only rows[1:]'s cell values are collected."""
    if len(rows) < 2:
        return []

    header_row = rows[0]
    column_categories = {i: classify_header(str(h)) for i, h in enumerate(header_row)}
    if not any(column_categories.values()):
        return []

    candidates: list[dict] = []
    for row in rows[1:]:
        for col_index, value in enumerate(row):
            category = column_categories.get(col_index)
            if category is None:
                continue
            text = str(value).strip() if value is not None else ""
            if not text:
                continue
            candidates.append({"text": text, "category": category})
    return candidates


def _extract_xlsx_tables(source_bytes: bytes) -> list[list[list[str]]]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(source_bytes), data_only=True, read_only=True)
    tables = []
    for sheet in workbook.worksheets:
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if rows:
            tables.append(rows)
    return tables


def _extract_xls_tables(source_bytes: bytes) -> list[list[list[str]]]:
    import xlrd

    workbook = xlrd.open_workbook(file_contents=source_bytes)
    tables = []
    for sheet in workbook.sheets():
        rows = [
            ["" if v == "" else str(v) for v in sheet.row_values(row_idx)]
            for row_idx in range(sheet.nrows)
        ]
        rows = [row for row in rows if any(cell for cell in row)]
        if rows:
            tables.append(rows)
    return tables


def _extract_csv_table(source_bytes: bytes) -> list[list[list[str]]]:
    try:
        content = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = source_bytes.decode("latin-1")

    try:
        dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    rows = [row for row in csv.reader(content.splitlines(), dialect=dialect) if any(row)]
    return [rows] if rows else []


def _extract_json_tables(source_bytes: bytes) -> list[list[list[str]]]:
    try:
        content = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = source_bytes.decode("latin-1")

    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return []

    # Only the common "list of flat records" shape is handled — each dict's
    # keys act as that record's headers. Anything else (a single object,
    # deeply nested structures, a list of scalars) has no clear tabular
    # column structure to classify, so it's left to existing detection
    # entirely rather than guessing.
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        return []

    headers: list[str] = []
    seen_headers: set[str] = set()
    for item in value:
        for key in item.keys():
            if key not in seen_headers:
                seen_headers.add(key)
                headers.append(key)

    rows = [headers]
    for item in value:
        row = [item.get(h) for h in headers]
        row = ["" if v is None or isinstance(v, (dict, list)) else str(v) for v in row]
        rows.append(row)
    return [rows]


def _extract_ods_tables(source_bytes: bytes) -> list[list[list[str]]]:
    import odf.opendocument
    import odf.table
    import odf.teletype

    document = odf.opendocument.load(io.BytesIO(source_bytes))
    tables = []
    for sheet in document.getElementsByType(odf.table.Table):
        rows = []
        for tr in sheet.getElementsByType(odf.table.TableRow):
            cells = tr.getElementsByType(odf.table.TableCell)
            row = [odf.teletype.extractText(cell) for cell in cells]
            if any(row):
                rows.append(row)
        if rows:
            tables.append(rows)
    return tables


_TABLE_EXTRACTORS = {
    ".xlsx": _extract_xlsx_tables,
    ".xlsm": _extract_xlsx_tables,
    ".xltx": _extract_xlsx_tables,
    ".xltm": _extract_xlsx_tables,
    ".xls": _extract_xls_tables,
    ".csv": _extract_csv_table,
    ".json": _extract_json_tables,
    ".ods": _extract_ods_tables,
}


def extract_column_candidates(source_bytes: bytes, source_suffix: str) -> list[dict]:
    """Return [{"text": <cell value>, "category": <...>}, ...] for every
    data-row cell in a header-classified column, across every sheet/table in
    the source file, deduplicated (first-seen category wins for a given
    text) and sorted longest-substring-first (matching deep_check.py's
    candidate convention, so a short match doesn't get partially consumed by
    a longer overlapping one first when these are applied). Returns an empty
    list for an unsupported suffix or a file that fails to parse — this
    feature is a pure enhancement layered on top of existing detection, so
    it degrades gracefully rather than raising.
    """
    extractor = _TABLE_EXTRACTORS.get(source_suffix.lower())
    if extractor is None:
        return []

    try:
        tables = extractor(source_bytes)
    except Exception:
        return []

    first_seen_category: dict[str, str] = {}
    for rows in tables:
        for candidate in _candidates_from_table(rows):
            first_seen_category.setdefault(candidate["text"], candidate["category"])

    candidates = [{"text": text, "category": category} for text, category in first_seen_category.items()]
    candidates.sort(key=lambda c: len(c["text"]), reverse=True)
    return candidates


def summarize_column_categories(
    candidates: list[dict], raw_text: str
) -> tuple[list[DetectedCategory], dict[str, OccurrenceRef]]:
    """Aggregate extract_column_candidates() output into full per-occurrence
    review-UI categories, for the pre-finalize review UI — mirrors
    deep_check.summarize_candidate_categories() exactly (including the
    id_prefix="col" so this source's occurrence ids can never collide with
    deep-check's own "dc"-prefixed ones), except occurrences are found by
    scanning `raw_text` (the flattened transcript text) directly, since —
    unlike deep-check's LLM-sourced candidates — these never carry a count of
    their own (extract_column_candidates() only knows the original structured
    bytes, not the flattened text it'll be matched against).

    is_person is always False here — NOT because person_mode never applies
    to PERSON_SPALTE (pipeline.py's finalize() DOES route it through the same
    person_replacer as Presidio-found names, so a name in a header-classified
    column gets the same numbered/pseudonymized label as the identical name
    found elsewhere), but to avoid a SECOND person-mode toggle: the app has
    exactly one global person_mode per finalize() call, and the frontend's
    getPersonMode() reads it from whichever single row has is_person=true
    (see app/web/static/app.js) — a second toggle here would let a user
    select two different values with only one silently taking effect. The
    one tradeoff: a document with ONLY column-classified names and zero
    Presidio PERSON hits has no toggle to select from at all, so those names
    fall back to the flat "[PERSON_SPALTE]" label even in numbered/
    pseudonymize mode — an accepted, narrow edge case rather than a
    confusing dual-toggle UI for the common case."""
    by_category: dict[str, list] = {}
    order: list[str] = []
    all_refs: dict[str, OccurrenceRef] = {}

    for i, candidate in enumerate(candidates):
        category = candidate["category"]
        text = candidate["text"]
        occurrences, refs = occurrences_for_text_match(f"col{i}", category, "column_header", text, raw_text)
        if not occurrences:
            continue
        if category not in by_category:
            by_category[category] = []
            order.append(category)
        by_category[category].extend(occurrences)
        all_refs.update(refs)

    categories = [
        DetectedCategory(
            category=category,
            count=len(by_category[category]),
            source="column_header",
            occurrences=by_category[category],
            is_person=False,
        )
        for category in order
    ]
    return categories, all_refs
