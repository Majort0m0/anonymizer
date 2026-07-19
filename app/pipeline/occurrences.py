"""Shared occurrence-building helpers for the category-review UI.

Every detected span, regardless of which of the three independent sources
found it (Presidio, deep-check's LLM candidates, column_classifier's
header-classified cell values), needs to become one row the user can
individually check/uncheck in the "Kategorien prüfen" step — not just a
category with a couple of example strings. This module is what all three
sources build that row list through, so the review UI and the exclusion
mechanism stay consistent across them despite the sources having very
different underlying shapes (Presidio results carry exact character offsets;
deep-check/column candidates are just a text value with no position at all).

Two identity schemes, chosen per source:

- Presidio (`occurrences_for_offsets`): exact (start, end) offsets are
  already known, so the occurrence id encodes them directly
  ("p:<start>:<end>"). This is precise but only valid against the SAME
  results list it was computed from — see app.pipeline.pipeline.finalize()'s
  handling of the column_candidates re-analyze case for why that matters.

- Deep-check / column-header (`occurrences_for_text_match`): a candidate is
  just a text value with no position of its own, so every occurrence of it
  is found here by scanning the given text, and identified by its
  left-to-right ORDINAL among all matches of that exact text ("<prefix>:
  <ordinal>"). An ordinal (unlike an absolute offset) survives finalize()-time
  text mutation from other redaction steps applied in between, because
  deep_check.apply_candidates() re-scans whatever text it is currently given
  fresh and recomputes the same left-to-right ordinal against it — it never
  trusts a stored offset from analyze()-time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas import Occurrence

# How much surrounding text (characters) to show on each side of a match in
# the review UI, so a reviewer can judge whether a flagged span is actually
# PII in context — not just a bare word with nothing around it.
_CONTEXT_RADIUS = 40


@dataclass
class OccurrenceRef:
    """Server-side-only record of what one occurrence id actually refers to
    — never sent to the frontend (see schemas.Occurrence, the pydantic
    counterpart that is). Kept on PendingState so finalize() can translate
    the ids the frontend sends back into an actual exclusion."""

    category: str
    source: str  # "presidio" | "llm_deep_check" | "column_header"
    text: str
    # Presidio only — see this module's docstring.
    start: int | None = None
    end: int | None = None
    # Deep-check / column-header only — see this module's docstring.
    ordinal: int | None = None


def _context(text: str, start: int, end: int) -> tuple[str, str]:
    before = text[max(0, start - _CONTEXT_RADIUS) : start]
    after = text[end : end + _CONTEXT_RADIUS]
    if start - _CONTEXT_RADIUS > 0:
        before = "…" + before
    if end + _CONTEXT_RADIUS < len(text):
        after = after + "…"
    return before, after


def occurrences_for_offsets(
    category: str, source: str, text: str, spans: list[tuple[int, int]]
) -> tuple[list[Occurrence], dict[str, OccurrenceRef]]:
    """Presidio-style: every span's exact (start, end) is already known."""
    occurrences: list[Occurrence] = []
    refs: dict[str, OccurrenceRef] = {}
    for start, end in spans:
        occ_id = f"p:{start}:{end}"
        before, after = _context(text, start, end)
        occurrences.append(
            Occurrence(id=occ_id, text=text[start:end], context_before=before, context_after=after)
        )
        refs[occ_id] = OccurrenceRef(category=category, source=source, text=text[start:end], start=start, end=end)
    return occurrences, refs


def occurrences_for_text_match(
    id_prefix: str, category: str, source: str, match_text: str, haystack: str
) -> tuple[list[Occurrence], dict[str, OccurrenceRef]]:
    """deep-check / column-header style: `match_text` carries no position of
    its own — every occurrence of it in `haystack` is found here via a fresh
    scan, identified by its left-to-right ordinal. `id_prefix` must be unique
    per candidate (callers use the candidate's index in its own list) so two
    different candidates' ordinals never collide."""
    occurrences: list[Occurrence] = []
    refs: dict[str, OccurrenceRef] = {}
    for ordinal, match in enumerate(re.finditer(re.escape(match_text), haystack)):
        occ_id = f"{id_prefix}:{ordinal}"
        before, after = _context(haystack, match.start(), match.end())
        occurrences.append(Occurrence(id=occ_id, text=match_text, context_before=before, context_after=after))
        refs[occ_id] = OccurrenceRef(category=category, source=source, text=match_text, ordinal=ordinal)
    return occurrences, refs


def fully_excluded_categories(
    occurrence_refs: dict[str, OccurrenceRef], excluded_occurrence_ids: set[str]
) -> set[str]:
    """Categories where EVERY occurrence found at analyze()-time is in the
    frontend's exclusion set — i.e. the user unchecked every single instance,
    equivalent to (and used as a drop-in replacement for) today's whole-
    category exclusion wherever occurrence-level identity doesn't survive to
    finalize() time: the per-cell structured re-export, the column_candidates
    Presidio re-analyze fallback, and the exclusion_note handed to
    find_missed_pii()/find_missed_locations() (which run with no review step
    of their own, so only ever know categories, never individual ids)."""
    by_category: dict[str, list[str]] = {}
    for occ_id, ref in occurrence_refs.items():
        by_category.setdefault(ref.category, []).append(occ_id)
    return {
        category
        for category, ids in by_category.items()
        if all(occ_id in excluded_occurrence_ids for occ_id in ids)
    }
