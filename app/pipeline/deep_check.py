"""Second-pass PII detection over text the deterministic Presidio pass already redacted.

Privacy invariant: every function in this module must only ever be called
with text that has already gone through the Presidio anonymization pass
(placeholders like [PERSON] or [EMAIL_ADDRESS] already in place). The raw
original document must never reach this module or be sent to the local
model.

Two independent LLM passes live here:

- find_candidates() / summarize_candidate_categories() / apply_candidates()
  - the original contextual sweep, run once during analyze() against a
  preliminary, fully-redacted (no exclusions) text. Scoped to indirect
  identifiers Presidio structurally cannot resolve: nicknames, role-based
  references, project codenames. Split into three steps so the caller can
  show the user what was found (as categories with counts) before anything
  is actually redacted:
  1. find_candidates()               - runs the LLM pass, returns raw
                                        candidate substrings + normalized
                                        categories.
  2. summarize_candidate_categories() - aggregates candidates into the
                                        DetectedCategory rows the review UI
                                        shows.
  3. apply_candidates()              - actually performs the redaction,
                                        honoring any categories the user
                                        chose to exclude.

- find_missed_pii() - a second, later sweep run during finalize() against
  the ACTUAL final text (post category-exclusion, post person-mode), looking
  for plain PII the deterministic pass and the sweep above still missed
  outright: stray location names, names left in signature lines/closings,
  business or file reference numbers. There is no review step for this one
  (the text it runs against only exists once finalize() has already applied
  the user's choices) — its findings are applied directly via the same
  apply_candidates(), which is why it takes excluded_categories itself: it
  must not re-flag something the user deliberately chose to keep visible.

- find_missed_locations() - a third sweep, run alongside find_missed_pii(),
  narrowly scoped to ONLY place/city/location names. A single prompt asking
  about several different category types at once has weaker recall for any
  one of them than a pass with only one thing to look for — added after
  real-world testing showed a city name (mentioned several times) still
  slipping through the general find_missed_pii() sweep on a full-length
  document. Same apply mechanism and excluded_categories handling as
  find_missed_pii().

All three passes chunk very long input before sending it to the LLM (see
_split_into_chunks()). This is NOT about context-window truncation — tested
directly against this app's default model/config, a ~1750-word document was
recalled perfectly in one call. The original motivation was recall degrading
on real (non-synthetic) documents once a lot of already-redacted placeholder
noise piles up in one call — a real 55-PERSON/30-LOCATION medical report
caught known misses when tested as a short, low-noise excerpt but missed the
same terms in one long call. However, real-world testing at the
~2000-2500-word range this app's documents typically fall in showed chunking
at a small chunk size added 6-9x the LLM calls (and proportional wall-clock
time — an analyze+finalize pass going from a couple of minutes to 20-60)
without measurably fixing the specific misses that motivated it in the first
place (a generic institutional name and an adjacent postal code both still
slipped through with chunking on). So the threshold/target sizes below are
now deliberately large: an ordinary document stays a single call (matching
pre-chunking speed), and only a genuinely long document (multi-page reports)
gets split at all, into a small number of large pieces rather than many small
ones — chunking is kept as a safety net for the extreme case, not the default
path. Overlap ensures a candidate phrase straddling a chunk boundary still
appears whole in at least one chunk; each pass's results are merged by exact
text match and occurrence counts re-derived against the full text afterwards,
so chunk boundaries never affect correctness, only how thoroughly the model
is asked to look in one call.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from app.llm.ollama_client import generate
from app.schemas import AnonymizeResult, DetectedCategory, PiiEntity

# Extraction/classification wants low-variance, systematic output rather than
# creative sampling (the Modelfile default is temperature=1) — repeated runs
# against the same text at temperature=1 showed some real terms found every
# time and others (a genuine blind spot, not noise) found never; a low,
# near-deterministic temperature at least removes run-to-run noise as a
# variable, leaving chunking (below) to address density-driven misses.
_EXTRACTION_TEMPERATURE = 0.2

# Deliberately large — see the module docstring: a smaller chunk size was
# tried and, on real documents in this app's typical ~2000-2500-word range,
# cost 6-9x the LLM calls without measurably improving recall. These
# thresholds mean a typical document stays a single call; only a genuinely
# long one (multi-page reports) gets split, into a handful of large chunks.
_CHUNK_TARGET_WORDS = 1800
_CHUNK_OVERLAP_WORDS = 200
_CHUNK_THRESHOLD_WORDS = 2500  # below this, a single call is not worth splitting


def _split_into_chunks(
    text: str,
    target_words: int = _CHUNK_TARGET_WORDS,
    overlap_words: int = _CHUNK_OVERLAP_WORDS,
    threshold_words: int = _CHUNK_THRESHOLD_WORDS,
) -> list[str]:
    """Split `text` into overlapping chunks along line boundaries (never mid-
    line), so tabular "|"-joined rows stay intact rather than being merged
    into one whitespace-collapsed blob. Chunk boundaries don't need to be
    precise: callers only use the returned candidate substrings to redact
    against the ORIGINAL full text afterwards, never against chunk offsets.

    `threshold_words` (not `target_words`) gates whether splitting happens at
    all — a text under threshold_words isn't worth the extra Ollama round-trip
    even though it's already above target_words, the size chunks are cut to
    once splitting actually is warranted.
    """
    lines = text.split("\n")
    if len(text.split()) <= threshold_words:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for line in lines:
        line_words = len(line.split())
        if current and current_words + line_words > target_words:
            chunks.append("\n".join(current))
            overlap_lines: list[str] = []
            overlap_words_count = 0
            for prev_line in reversed(current):
                prev_words = len(prev_line.split())
                if overlap_words_count + prev_words > overlap_words:
                    break
                overlap_lines.insert(0, prev_line)
                overlap_words_count += prev_words
            current = overlap_lines
            current_words = overlap_words_count
        current.append(line)
        current_words += line_words

    if current:
        chunks.append("\n".join(current))
    return chunks


def estimate_chunk_count(text: str) -> int:
    """How many chunks find_candidates()/find_missed_pii() will actually call
    the LLM for on this text — used by the pipeline to plan progress/ETA
    before the LLM call(s) happen."""
    return len(_split_into_chunks(text))


def _merge_chunk_candidates(chunk_results: list[list[dict]], full_text: str) -> list[dict]:
    """Union candidates found across chunks (first-seen category wins for a
    given text), then recompute occurrence counts against the FULL text —
    chunk-local counts would undercount (or, with overlap, double-count)
    occurrences relative to the whole document."""
    first_seen_category: dict[str, str] = {}
    for candidates in chunk_results:
        for candidate in candidates:
            first_seen_category.setdefault(candidate["text"], candidate["category"])

    merged: list[dict] = []
    for text, category in first_seen_category.items():
        occurrences = full_text.count(text)
        if occurrences == 0:
            continue
        merged.append({"text": text, "category": category, "count": occurrences})

    merged.sort(key=lambda c: len(c["text"]), reverse=True)
    return merged

_SYSTEM_DE = (
    "Du bist ein Datenschutz-Experte. Der folgende Text wurde bereits automatisch "
    "anonymisiert: direkte Namen, Adressen, E-Mails usw. sind bereits durch Platzhalter "
    "wie [PERSON] oder [EMAIL_ADDRESS] ersetzt. Deine Aufgabe ist es, verbleibende "
    "Hinweise zu finden, die eine Person trotzdem identifizierbar machen könnten: "
    "Spitznamen, Rollenbezeichnungen (z. B. \"der Teamleiter\", \"die Assistentin von X\"), "
    "indirekte Verweise, Projekt- oder Decknamen, oder andere kontextabhängige Hinweise. "
    "Antworte AUSSCHLIESSLICH mit einem JSON-Array von Objekten der Form "
    '{"text": "<exakte Textstelle>", "category": "<kurze Kategorie>"}. '
    "Wenn nichts gefunden wird, antworte mit einem leeren Array []. "
    "Gib keinerlei zusätzlichen Text, keine Erklärungen und keine Code-Blöcke aus."
)

_SYSTEM_EN = (
    "You are a privacy expert. The following text has already been automatically "
    "anonymized: direct names, addresses, emails etc. have already been replaced with "
    "placeholders such as [PERSON] or [EMAIL_ADDRESS]. Your job is to find any remaining "
    "clues that could still identify a person: nicknames, role-based identifiers "
    "(e.g. \"the team lead\", \"X's assistant\"), indirect references, project or code "
    "names, or other context-dependent hints. "
    'Respond ONLY with a JSON array of objects of the form {"text": "<exact substring>", '
    '"category": "<short category label>"}. '
    "If nothing is found, respond with an empty array []. "
    "Do not output any additional text, explanations, or code blocks."
)

_MISSED_SYSTEM_DE = (
    "Du bist ein Datenschutz-Experte. Der folgende Text wurde bereits automatisch "
    "anonymisiert: erkannte personenbezogene Daten sind bereits durch Platzhalter wie "
    "[PERSON], [LOCATION] oder [EMAIL_ADDRESS] ersetzt. Prüfe den Text noch einmal "
    "vollständig auf übersehene, eindeutig identifizierende Angaben, die NICHT durch "
    "einen Platzhalter ersetzt wurden — insbesondere: Personennamen (auch in "
    "Unterschriftszeilen, Grußformeln oder Signaturen am Ende), Orts- oder "
    "Städtenamen im Fließtext, Namen konkreter Einrichtungen (z. B. Kindergarten, "
    "Schule, Klinik, Verband oder Firma — auch wenn der Name Teil eines längeren, "
    "unauffällig klingenden Ausdrucks ist), sowie Geschäfts-, Akten- oder "
    "Referenznummern. Achte besonders auf eine 4-5-stellige Postleitzahl unmittelbar "
    "vor einem Ortsnamen (z. B. \"83022 Musterstadt\") — melde in diesem Fall "
    "Postleitzahl UND Ortsname zusammen als eine einzige Textstelle, auch wenn der "
    "Ortsname allein schon an anderer Stelle erkannt wurde. Melde NUR Stellen, die "
    "eindeutig identifizierend sind — keine "
    "bereits durch Platzhalter ersetzten Stellen, keine Vermutungen.{exclusion_note} "
    "Antworte AUSSCHLIESSLICH mit einem JSON-Array von Objekten der Form "
    '{{"text": "<exakte Textstelle>", "category": "<kurze Kategorie>"}}. '
    "Wenn nichts gefunden wird, antworte mit einem leeren Array []. Gib keinerlei "
    "zusätzlichen Text, keine Erklärungen und keine Code-Blöcke aus."
)

_MISSED_SYSTEM_EN = (
    "You are a privacy expert. The following text has already been automatically "
    "anonymized: detected PII has already been replaced with placeholders such as "
    "[PERSON] or [EMAIL_ADDRESS]. Check the text once more, in full, for any remaining "
    "clearly identifying details that were NOT replaced with a placeholder — "
    "especially: person names (including in signature lines, sign-offs, or closings), "
    "place/city names in the body text, names of specific institutions (e.g. a "
    "kindergarten, school, clinic, association, or company — even as part of a "
    "longer, unremarkable-sounding phrase), and business, file, or reference numbers. "
    "Pay particular attention to a 4-5 digit postal code immediately before a place "
    "name (e.g. \"83022 Musterstadt\") — in that case report the postal code AND the "
    "place name together as a single span, even if the place name alone was already "
    "caught elsewhere. Only report spans that are clearly identifying — not already-replaced "
    "placeholders, and no guesses.{exclusion_note} "
    'Respond ONLY with a JSON array of objects of the form {{"text": "<exact substring>", '
    '"category": "<short category label>"}}. '
    "If nothing is found, respond with an empty array []. "
    "Do not output any additional text, explanations, or code blocks."
)

# A narrowly-scoped THIRD pass, focused on nothing but place/city names —
# added after real-world testing showed a city name (mentioned multiple
# times) still slipping through find_missed_pii() on a full-length document.
# A single prompt asking about many different category types at once (names,
# institutions, reference numbers, AND locations) has weaker recall for any
# one of them than a pass with only one thing to look for — this trades one
# more Ollama call for stronger location-specific recall specifically.
_MISSED_LOCATION_SYSTEM_DE = (
    "Du bist ein Datenschutz-Experte. Der folgende Text wurde bereits automatisch "
    "anonymisiert: erkannte personenbezogene Daten sind bereits durch Platzhalter wie "
    "[PERSON] oder [LOCATION] ersetzt. Deine EINZIGE Aufgabe in diesem Durchgang: "
    "finde JEDEN noch nicht geschwärzten Orts-, Stadt- oder Gemeindenamen im "
    "GESAMTEN Text — auch in Adresszeilen, Fußzeilen, Briefköpfen und beiläufigen "
    "Erwähnungen, auch wenn derselbe oder ein anderer Ort an anderer Stelle bereits "
    "ersetzt wurde. Gehe den Text systematisch von Anfang bis Ende durch und liste "
    "JEDES einzelne Vorkommen einzeln auf, auch wenn ein Ort mehrfach vorkommt. "
    "WICHTIG: Ein Platzhalter in eckigen Klammern wie [LOCATION], [PERSON] oder "
    "[ORT] ist selbst KEIN Ortsname, sondern eine bereits vorgenommene Schwärzung — "
    "melde niemals eine Textstelle, die nur aus einem solchen Platzhalter "
    "besteht.{exclusion_note} "
    "Antworte AUSSCHLIESSLICH mit einem JSON-Array von Objekten der Form "
    '{{"text": "<exakte Textstelle>", "category": "<kurze Kategorie>"}}. '
    "Wenn nichts gefunden wird, antworte mit einem leeren Array []. Gib keinerlei "
    "zusätzlichen Text, keine Erklärungen und keine Code-Blöcke aus."
)

_MISSED_LOCATION_SYSTEM_EN = (
    "You are a privacy expert. The following text has already been automatically "
    "anonymized: detected PII has already been replaced with placeholders such as "
    "[PERSON] or [LOCATION]. Your ONLY job in this pass: find EVERY place, city, or "
    "town name in the ENTIRE text that has not been replaced with a placeholder — "
    "including in address lines, footers, letterheads, and passing mentions, even if "
    "the same or a different place was already replaced elsewhere. Go through the "
    "text systematically from start to finish and list EVERY single occurrence "
    "separately, even if one place is mentioned more than once. "
    "IMPORTANT: a placeholder in square brackets like [LOCATION] or [PERSON] is NOT "
    "itself a place name — it is an already-applied redaction. Never report a span "
    "that consists only of such a placeholder.{exclusion_note} "
    'Respond ONLY with a JSON array of objects of the form {{"text": "<exact substring>", '
    '"category": "<short category label>"}}. '
    "If nothing is found, respond with an empty array []. "
    "Do not output any additional text, explanations, or code blocks."
)

_EXCLUSION_NOTE_DE = (
    " Der Nutzer hat sich bewusst entschieden, folgende Datenkategorien in diesem "
    "Dokument sichtbar zu lassen — melde dazu passende Stellen NICHT: {categories}."
)

_EXCLUSION_NOTE_EN = (
    " The user has deliberately chosen to leave the following data categories visible "
    "in this document — do NOT report spans matching them: {categories}."
)

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")

# Matches a candidate "text" that is ITSELF nothing but an already-applied
# placeholder (e.g. "[LOCATION]", "[PERSON1]") — observed happening despite
# explicit prompt instructions not to (a narrowly-focused prompt asking
# specifically about locations proved more prone to this than the general
# sweep, likely because the literal word "LOCATION" inside the brackets
# reads as relevant to what it was asked to find). Prompt wording alone
# isn't reliable enough here, so candidates matching this are dropped
# deterministically, regardless of which pass found them — silently
# "re-labeling" an existing placeholder isn't a privacy leak (still
# redacted either way) but is confusing and wrong.
_PLACEHOLDER_ONLY_RE = re.compile(r"^\[[A-ZÄÖÜ][A-ZÄÖÜ0-9_]*\]$")


def _extract_json_array(response: str) -> list | None:
    fenced = _CODE_FENCE_RE.search(response)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        bare = _JSON_ARRAY_RE.search(response)
        candidate = bare.group(0) if bare else None
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _normalize_category(category: str) -> str:
    normalized = _NON_ALNUM_RE.sub("_", category.strip()).strip("_").upper()
    return normalized or "PII"


def find_candidates(
    anonymized_text: str,
    language: str,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Run the LLM deep-check pass and return validated candidate substrings.

    `anonymized_text` must already be the output of the Presidio pass (see
    module docstring) — this function never sees, and must never be given,
    the raw original document.

    Returns a list of {"text": <original candidate substring>, "category":
    <normalized UPPER_SNAKE_CASE category>, "count": <occurrences found in
    anonymized_text>} dicts, sorted by substring length descending (so a
    short match doesn't get consumed by redacting a longer overlapping one
    first when these are later applied). On any parse failure, or when no
    candidate actually occurs in the text, this degrades gracefully by
    dropping/omitting rather than raising.

    `on_progress(stage, current, total)`, if given, is called once per chunk
    processed (see module docstring on chunking) with stage="deep_check_find".
    """
    system = _SYSTEM_DE if language.lower().startswith("de") else _SYSTEM_EN
    return _run_chunked_pass(anonymized_text, system, "deep_check_find", on_progress)


def find_missed_pii(
    anonymized_text: str,
    language: str,
    excluded_categories: set[str] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Second LLM sweep, run during finalize() against the ACTUAL final text
    (post category-exclusion, post person-mode) — unlike find_candidates(),
    which only ever sees a preliminary, fully-redacted text and looks for
    indirect/contextual clues, this one looks for plain PII spans that were
    simply missed outright: stray location names, names left in signature
    lines or closings, business/file/reference numbers.

    `excluded_categories` (the same set finalize() used to redact the body)
    is woven into the prompt itself, not just used to filter results
    afterwards — since this pass sees the true final text, a category the
    user chose to leave visible is genuinely present in plain text here, and
    without this note the model would otherwise "catch" and re-redact
    exactly what the user asked to keep.

    Same return shape as find_candidates() — pass straight to
    apply_candidates() (with a distinct `source` label) to apply.
    `on_progress(stage, current, total)`, if given, is called once per chunk
    processed with stage="deep_check_missed".
    """
    exclusion_note = ""
    if excluded_categories:
        note_template = _EXCLUSION_NOTE_DE if language.lower().startswith("de") else _EXCLUSION_NOTE_EN
        exclusion_note = note_template.format(categories=", ".join(sorted(excluded_categories)))

    system_template = _MISSED_SYSTEM_DE if language.lower().startswith("de") else _MISSED_SYSTEM_EN
    system = system_template.format(exclusion_note=exclusion_note)
    return _run_chunked_pass(anonymized_text, system, "deep_check_missed", on_progress)


def find_missed_locations(
    anonymized_text: str,
    language: str,
    excluded_categories: set[str] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> list[dict]:
    """Third LLM sweep, run during finalize() alongside find_missed_pii() —
    see _MISSED_LOCATION_SYSTEM_DE/_EN's comment for why this is a separate,
    narrowly-scoped call rather than folded into find_missed_pii(): a prompt
    asking about several different category types at once has weaker recall
    for any single one of them than a pass with only one thing to look for,
    and real-world testing showed a city name (repeated several times)
    slipping through the general sweep on a full-length document.

    Same return shape/usage as find_missed_pii() — apply via
    apply_candidates(). `on_progress(stage, current, total)`, if given, is
    called once per chunk processed with stage="deep_check_locations".
    """
    exclusion_note = ""
    if excluded_categories:
        note_template = _EXCLUSION_NOTE_DE if language.lower().startswith("de") else _EXCLUSION_NOTE_EN
        exclusion_note = note_template.format(categories=", ".join(sorted(excluded_categories)))

    system_template = (
        _MISSED_LOCATION_SYSTEM_DE if language.lower().startswith("de") else _MISSED_LOCATION_SYSTEM_EN
    )
    system = system_template.format(exclusion_note=exclusion_note)
    return _run_chunked_pass(anonymized_text, system, "deep_check_locations", on_progress)


def _run_chunked_pass(
    anonymized_text: str,
    system: str,
    stage: str,
    on_progress: Callable[[str, int, int], None] | None,
) -> list[dict]:
    chunks = _split_into_chunks(anonymized_text)
    if on_progress:
        # Explicit start marker (mirrors every other stage's (0, total) call)
        # so the caller has a clean timestamp to measure chunk 1's duration
        # from too, not just chunks 2..N.
        on_progress(stage, 0, len(chunks))
    chunk_results: list[list[dict]] = []
    for i, chunk in enumerate(chunks):
        chunk_results.append(_run_candidate_pass(chunk, system))
        # Fired immediately after each chunk's LLM call returns (not batched
        # after the loop) — the caller times the gap between consecutive
        # on_progress calls to calibrate a real per-chunk duration estimate
        # (see server.py); batching these would collapse that measurement.
        if on_progress:
            on_progress(stage, i + 1, len(chunks))
    if len(chunks) == 1:
        return chunk_results[0]
    return _merge_chunk_candidates(chunk_results, anonymized_text)


def _run_candidate_pass(anonymized_text: str, system: str) -> list[dict]:
    response = generate(prompt=anonymized_text, system=system, temperature=_EXTRACTION_TEMPERATURE)

    items = _extract_json_array(response)
    if items is None:
        return []

    raw_candidates: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_text = item.get("text")
        raw_category = item.get("category")
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        if not isinstance(raw_category, str) or not raw_category.strip():
            continue
        if _PLACEHOLDER_ONLY_RE.match(raw_text.strip()):
            continue
        raw_candidates.append((raw_text, raw_category))

    candidates: list[dict] = []
    for raw_text, raw_category in raw_candidates:
        occurrences = anonymized_text.count(raw_text)
        if occurrences == 0:
            continue
        candidates.append(
            {
                "text": raw_text,
                "category": _normalize_category(raw_category),
                "count": occurrences,
            }
        )

    # Longer substrings first so a short match (e.g. a surname) doesn't
    # consume part of a longer one (e.g. "role + surname") before it is
    # checked, once these candidates are later applied.
    candidates.sort(key=lambda c: len(c["text"]), reverse=True)
    return candidates


def summarize_candidate_categories(candidates: list[dict]) -> list[DetectedCategory]:
    """Aggregate find_candidates() output into review-UI-ready categories.

    Every row here is a deep-check finding: source="llm_deep_check" and
    is_person=False always (deep-check never touches Presidio's PERSON
    category — its findings are free-form labels like "SPITZNAME" or
    "ROLLENBEZEICHNUNG").
    """
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    order: list[str] = []

    for candidate in candidates:
        category = candidate["category"]
        text = candidate["text"]
        count = candidate["count"]

        if category not in counts:
            counts[category] = 0
            samples[category] = []
            order.append(category)
        counts[category] += count
        if text not in samples[category] and len(samples[category]) < 3:
            samples[category].append(text)

    return [
        DetectedCategory(
            category=category,
            count=counts[category],
            source="llm_deep_check",
            samples=samples[category],
            is_person=False,
        )
        for category in order
    ]


def apply_candidates(
    text: str,
    candidates: list[dict],
    excluded_categories: set | None = None,
    source: str = "llm_deep_check",
) -> AnonymizeResult:
    """Actually redact find_candidates()/find_missed_pii() output against `text`.

    `text` may not be byte-identical to whatever the candidates were found
    against (the user may have excluded some Presidio categories between the
    "analyze" and "finalize" steps, changing surrounding text), so occurrence
    counts are re-derived from `text` here rather than trusting the stored
    "count". A candidate whose category is in `excluded_categories` is left
    untouched; a candidate that no longer occurs in `text` at all is skipped
    silently, matching this module's existing graceful-degradation behavior.

    `candidates` is expected in longest-substring-first order (both
    find_candidates() and find_missed_pii() sort this way), which is
    preserved (not re-sorted) here. `source` is stamped onto the returned
    PiiEntity rows so the audit table can distinguish which LLM pass found
    what — the default matches find_candidates()'s original single-pass
    behavior; find_missed_pii() results should pass a different label.
    """
    excluded = excluded_categories or set()

    result_text = text
    counts: dict[str, int] = {}
    for candidate in candidates:
        category = candidate["category"]
        if category in excluded:
            continue

        raw_text = candidate["text"]
        occurrences = result_text.count(raw_text)
        if occurrences == 0:
            continue

        result_text = result_text.replace(raw_text, f"[{category}]")
        counts[category] = counts.get(category, 0) + occurrences

    entities = [
        PiiEntity(entity_type=category, count=count, source=source)
        for category, count in counts.items()
    ]
    return AnonymizeResult(anonymized_text=result_text, entities=entities)
