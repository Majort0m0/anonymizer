# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Environment setup (Python 3.11 via uv; see README.md for full setup incl. spaCy models)
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python -m spacy download de_core_news_lg
python -m spacy download en_core_web_lg

# Run the desktop app (pywebview window + FastAPI backend in a background thread)
python -m app.main

# Run just the backend (useful for testing via curl/browser without the native window)
uvicorn app.server:app --host 127.0.0.1 --port 8765

# Package a native installer (macOS verified; Windows/Linux written but
# unverified locally — see "Packaging" below)
uv pip install -r requirements-build.txt
./scripts/build_macos.sh        # -> dist/Anonymizer.app, dist/Anonymizer-macOS.dmg
.\scripts\build_windows.ps1     # -> dist/Anonymizer/Anonymizer.exe (+ installer if Inno Setup present)
./scripts/build_linux.sh        # -> dist/Anonymizer-x86_64.AppImage

# Docker (backend only — no native window in a container)
docker compose up -d            # -> http://localhost:8765
```

There is no test suite yet. Ad-hoc verification during development was done by
calling pipeline functions directly (`python -c "from app.pipeline...`) and by
curling the running FastAPI server's endpoints — see `README.md` for example
payloads. If you add tests, wire the runner in here.

No linter/formatter is configured yet.

## Architecture

Local-only desktop app: ingest a document or audio file → anonymize PII → optionally
summarize → render a formatted markdown file. "Local-only" is a hard privacy
requirement, not a preference — no stage may send raw (non-anonymized) content to a
network API; the only network-capable dependency is Ollama, and it is only ever
called with already-anonymized text (see the privacy-invariant docstrings in
`app/pipeline/deep_check.py`, `app/pipeline/summarize.py`, and `app/pipeline/pipeline.py`).

**Process shape**: `app/main.py` starts the FastAPI app (`app/server.py`) on a
background thread and opens it in a native `pywebview` window — no Electron/Node,
no frontend build step. The frontend (`app/web/static/`) is plain HTML/CSS/JS with
no external CDN dependencies (the app must work fully offline).

**Shared contracts**: `app/schemas.py` (pydantic models) and `app/config.py`
(model names, spaCy models, phone/ID country allowlists, paths) are the source of
truth every pipeline module is built against. Read these before touching any
pipeline module — they define the shape every stage passes to the next.

**Two-step flow, orchestrated by `app/pipeline/pipeline.py`**: the app shows the
user what PII it found *before* redacting anything, so they can exclude specific
categories (e.g. keep locations visible) or choose pseudonymization for names,
instead of an all-or-nothing single-shot pipeline. `analyze()` /
`analyze_file()` do detection only and return a `PendingState` (kept server-side,
see below) plus a `list[DetectedCategory]` for the review UI; `finalize()` takes
the user's `excluded_categories` + `pseudonymize_person` choice and returns a
`FinalizeOutput` (a plain dataclass, not the HTTP-facing pydantic model —
it carries the transcript/summary markdown text and, for tabular sources, raw
output file bytes, none of which belong in a JSON response body).
`app/server.py`'s `finalize_route` turns a `FinalizeOutput` into the actual
saved files plus the `PipelineResult` (schemas.py) JSON response, whose
`downloads: list[DownloadableFile]` can hold 1–3 entries depending on
`output_mode` and source format (see step 6). There is no backward-compatible
single-shot function — `/api/analyze-*` and `/api/finalize` are the only
callers of `analyze*`/`finalize`.

1. **Ingest** (`app/pipeline/ingest.py` + `app/pipeline/parsers/`) — dispatches by
   file extension to a parser (txt/md, docx, pdf, excel, csv, json, odf) or, for
   audio extensions, routes to `app/pipeline/transcription.py` (`faster-whisper`)
   instead. Legacy `.doc` (pre-XML Word format) is deliberately rejected with an
   actionable error; `python-docx` cannot read it. Spreadsheet-shaped formats
   (`excel_parser.py`, and `odf_parser.py`'s `.ods` branch) render each sheet as
   `## <sheet name>` followed by `" | "`-joined rows rather than a flat text dump,
   so tabular structure survives into the transcript. `odf_parser.py` walks
   block-level elements itself instead of calling `odf.teletype.extractText()` on
   the whole document tree — the latter flattens all paragraphs into one run with
   no separators and also picks up unrelated `<office:meta>` text.
2. **Analyze** (`app/pipeline/anonymize.py`'s `analyze()`) — Presidio
   (`AnalyzerEngine`) configured for German + English via spaCy, detection only,
   no redaction. Several non-obvious fixes baked into this module, worth
   understanding before changing it:
   - `RecognizerRegistry` must be constructed with `supported_languages=` set
     explicitly — Presidio's `AnalyzerEngine` validates that the registry's
     `supported_languages` attribute (not just what `load_predefined_recognizers`
     loaded) matches, and the registry's constructor defaults it to `["en"]`.
   - Presidio's default cross-type overlap resolution picks a winner by
     iteration/span order, not confidence score — a low-score false positive can
     silently swallow a high-score correct match (observed: a stray NER `PERSON`
     span ate a checksum-validated `IBAN_CODE`). `_resolve_overlaps()` in this
     module re-resolves overlaps by score before handing results to Presidio's
     anonymizer, and `RELEVANT_ID_COUNTRIES` in `app/config.py` excludes
     country-specific structured-ID recognizers (e.g. UK NHS numbers) that are
     irrelevant to this app's DE/EN scope and otherwise collide with generic
     phone-number matches.
   - Presidio ships no generic postal-code recognizer, and a plain "digits
     followed by a capitalized word" regex is far too noisy in German (every
     noun is capitalized — "54321 Stück" would false-positive constantly).
     `_find_postal_codes()` instead only treats a 4-5 digit run as `POSTAL_CODE`
     when it sits immediately before a span already recognized as `LOCATION`
     (the real "`<PLZ> <Ort>`" address convention), run as a post-processing
     pass over `analyze()`'s results rather than a registered recognizer.
   - `summarize_categories()` derives per-category example `samples` text
     directly from the input string via each result's start/end — this is the
     data the frontend's category-review checklist renders (see
     `app/schemas.py`'s `DetectedCategory`).
3. **Apply** (`anonymize.py`'s `apply_anonymization()`, called from
   `pipeline.finalize()`) — redacts, skipping any `entity_type` in
   `excluded_types` (left as original text). `pseudonymize_person=True` routes
   `PERSON` through a Presidio `"custom"` operator backed by
   `app/pipeline/pseudonymize.py`'s `make_person_pseudonymizer()` — a closure
   that maps each distinct matched name to a consistent fake full name. `finalize()`
   builds exactly ONE `person_pseudonymizer` per call and threads it through
   every `apply_anonymization()` call it makes — the main-text redaction AND
   (for tabular sources) every per-cell redaction below — so the same real name
   maps to the same fake name across the transcript, the summary, and the
   structured-format re-export. `apply_anonymization()` accepts an optional
   `person_pseudonymizer` param for exactly this reuse; omitting it builds a
   fresh (so only self-consistent) one. The PII audit count is derived from
   `anonymized.items` (what was *actually* replaced after conflict resolution),
   not the raw analyzer results — raw results can include overlapping
   candidates that never make it into the output text.
4. **Deep-check** (`app/pipeline/deep_check.py`, optional, toggled by
   `PipelineOptions.deep_check`) — split into `find_candidates()` (the LLM call
   + JSON parsing, run once during `analyze()` against a fully-redacted
   preliminary text — see privacy invariant below), `summarize_candidate_categories()`
   (review-UI rows for deep-check's free-form categories like `SPITZNAME`), and
   `apply_candidates()` (the actual substring replacement, run during
   `finalize()`, re-deriving occurrence counts from whatever text it's given
   since the user's category exclusions can change what's around a candidate
   substring between `analyze()` and `finalize()`). This pass is scoped
   specifically to *contextual* identifiers Presidio's regex/NER approach
   structurally cannot catch (nicknames, role-based references, project code
   names) — it is not a general-purpose redundant NER pass, and won't
   necessarily catch a plain name Presidio simply missed (e.g. in table-like,
   non-prose text).
5. **Summarize** (`app/pipeline/summarize.py`, only if `output_mode` includes a
   summary) — always given the *final* anonymized text (post-deep-check, if
   enabled, and post category-exclusion/pseudonymization), instructed to
   preserve `[PLACEHOLDER]` redactions verbatim.
6. **Render** (`app/pipeline/render_markdown.py`'s `render_transcript()` /
   `render_summary()`) — two independent functions, not one combined document:
   the summary is always a separate markdown file from the transcript, each
   with its own metadata block and PII audit table (`_render_document()` is
   the shared implementation both call into). `finalize()` calls whichever of
   the two apply for the requested `output_mode`.
7. **Structured re-export** (`pipeline.finalize()`, tabular sources only —
   `app/pipeline/ingest.py`'s `STRUCTURED_REWRITE_EXTENSIONS` =
   xlsx/xlsm/xltx/xltm/xls/csv/json/ods) — `analyze_file()` stashes the
   original upload's raw bytes + suffix on `PendingState` (`source_bytes`,
   `source_suffix`) for these extensions only (audio/prose/clipboard leave
   them `None`). `finalize()` builds a per-cell `transform(text) -> text`
   closure — re-running `anonymize.analyze()` + `apply_anonymization()` (with
   the SAME `excluded_categories`/`person_pseudonymizer` as the main text) and
   `deep_check.apply_candidates()` fresh on each individual cell's isolated
   text — then dispatches to `app/pipeline/rewrite_excel.py` /
   `rewrite_csv.py` / `rewrite_json.py` / `rewrite_ods.py`'s `rewrite_*()`,
   which parse the ORIGINAL bytes with the same library the read-side parser
   uses, replace only leaf string values via the callback, and re-serialize —
   preserving sheet/row/column structure, JSON key names and nesting, and
   (for xlsx) untouched formulas/formatting. `rewrite_excel.py`'s
   `output_suffix_for(suffix)` reports the actual output extension to use,
   since legacy `.xls` is read with xlrd but re-emitted as `.xlsx` (no
   maintained modern writer exists for the legacy binary format).
   **Non-obvious consequence**: re-running detection per isolated cell instead
   of reusing the flattened-text results from `analyze()` means the structured
   copy can redact MORE than the markdown transcript of the same document —
   isolated cell text is often easier for spaCy's NER to classify correctly
   than the same text embedded in the flattened `"Name | Email | ..."` table
   dump (see the ingest step's tabular-NER caveat above). This is intentional
   (favor over-redaction in the reusable-data output) but means the two output
   files' redaction is not always byte-for-byte aligned on identical input.

**Privacy invariant across the analyze/finalize split**: `analyze()` runs
deep-check's LLM call against a preliminary text redacted with *all* categories
(no exclusions) — the user's later category choices affect only what appears in
the final output, never what was sent to the local LLM. `PendingState` (raw
text, Presidio results, deep-check candidates, and — for tabular sources —
the original file's raw bytes) is held server-side in `app/server.py`'s
`_pending` dict, keyed by a one-time-use token; it is never serialized to the
frontend.

**Output filenames**: `app/server.py`'s `_unique_filename()` builds
`{sanitized source stem}{suffix_label}{extension}` (e.g.
`report-anonymisiert.md`, `report-zusammenfassung.md`,
`report-anonymisiert.xlsx`) and appends `" (2)"`, `" (3)"`, ... if that exact
name already exists in `OUTPUT_DIR` — re-processing the same source file never
silently overwrites a previous run's output.

**Post-finalize find & replace** (`app/server.py`'s `/api/replace-text`,
`app/web/static/app.js`'s `performReplace()`) — a manual correction pass for
text the pipeline got right *structurally* but wrong *lexically* (most
commonly: a word `faster-whisper` misheard in an audio transcription).
Deliberately NOT part of the analyze/finalize token flow — it operates on
whatever transcript/summary text the client currently has in memory
(`currentResult`, round-tripped as `ReplaceTextRequest`'s
`anonymized_transcript`/`summary` fields) via a plain `re.escape()`d literal
substitution, not a re-run of detection. Single "Ersetzen" applies `count=1`
independently to the transcript AND the summary — one substitution per
*document*, not one substitution total, so a term appearing in both gets
corrected in both from a single click; "Alle ersetzen" is `count=0`
(unlimited). Re-renders and re-saves the markdown output(s) via the same
`render_transcript()`/`render_summary()` `finalize()` uses, so downloads stay
in sync with on-screen corrections — any non-markdown download (a
structured-format xlsx/csv/json/ods copy) is passed through unchanged in the
response, since this endpoint never re-parses that original file's bytes.

**Route handlers that call blocking pipeline code (Presidio, spaCy, Ollama HTTP,
subprocess installs) are plain `def`, not `async def`** — FastAPI runs sync path
operations in a worker thread automatically. An `async def` route that calls
this code directly blocks the single asyncio event loop for the *entire*
server, not just that request (verified live: a concurrent `/api/dependencies`
request timed out while a slow `/api/finalize` was in flight, before this fix).
`analyze_file_route` is the one exception (needs `await file.read()`) — it
wraps the blocking `analyze_file()` call in `starlette.concurrency.run_in_threadpool`
instead.

**Setup/dependency checking** (`app/pipeline/setup_check.py`) backs the frontend's
"Systemstatus" panel: checks ffmpeg, Ollama (installed vs. running vs. model
pulled), and spaCy models, with safe auto-fix for Ollama model pulls and spaCy
downloads. It deliberately does *not* auto-install ffmpeg or Ollama itself
(installing system packages without explicit user action is out of scope).

**pywebview downloads are off by default on every backend** (Cocoa, EdgeChromium,
GTK, Qt — not macOS-specific). `app/main.py` sets
`webview.settings["ALLOW_DOWNLOADS"] = True` before creating the window; without
it, the frontend's `<a download>` result link is a silent no-op with no error.

**pywebview opens `target="_blank"` links in the system browser by default**
(`webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']` defaults to `True`) — the
footer's license link and the help modal's Ollama-download link rely on this;
don't add `target="_blank"` links assuming they'd otherwise hijack the app
window, but also don't add special-case handling for them, it already works.

**Two-column desktop layout: a persistent sidebar + a main content column.**
`app/web/static/index.html`'s `<main class="app-layout">` splits into
`.sidebar` (input, options, action/analyze button, system status — always
visible, never hidden/shown as a "phase") and `.main-content` (the empty
state, category review, and result — only one of which is ever unhidden at a
time). This replaced an earlier single-column, phase-toggling layout where
`showInputPhase()`/`hideInputPhase()` hid the input cards while reviewing/
viewing results; that design is gone entirely now; don't reintroduce it. Two
consequences worth knowing before touching this area:
- Because the sidebar never hides/shows, picking a new file or pasting new
  clipboard text while a review/result is displayed and clicking
  "Analysieren" again just works — no need to explicitly reset first
  (`resetToInputPhase()`/the "Neues Dokument" button are conveniences, not
  the only way back to a blank state).
- `renderResult()` ends with `resultCard.scrollIntoView(...)`. A now-fixed
  bug: the finalize handler used to also unhide the (then-hidden) input
  cards right after, which — since they sat *above* the result card in a
  single-column DOM — reflowed the page and silently undid the just-completed
  scroll, making the result appear to "jump to the top". With the sidebar
  now a separate grid column that's never toggled, this specific failure
  mode can't recur, but the general lesson stands: don't show/hide content
  positioned above an element right after scrolling to it.
- `.result-previews` shows the transcript and summary side by side (flexbox,
  wraps to stacked below ~320px each) and `.review-list` is a responsive grid
  (`auto-fill, minmax(300px, 1fr)`) — both exist specifically to make use of
  the wider main-content column; don't reflatten them back to a single
  stacked list without a reason, that's the whole point of this layout.
- The grid collapses to a single stacked column below `880px` (see the
  `@media` query on `.app-layout`) for narrow windows.

**The page itself never scrolls — `body` is locked to `height: 100vh` with
`overflow: hidden`.** Only `.sidebar` and `.main-content` have
`overflow-y: auto` and scroll independently of each other. This was a direct
fix for a reported bug: the sidebar's own content (dropzone, options,
analyze button, system status) is taller than short windows, and with a
page-level scroll, scrolling down to reach the analyze button also scrolled
the header out of view. Now the header/footer stay fixed and only the
sidebar's own area scrolls if it doesn't fit — and everything in the sidebar
(card padding, dropzone size, option descriptions, hint text) was
deliberately kept compact specifically so the analyze button fits without
even that internal scroll on ordinary laptop-window heights (~800px+); don't
casually add back verbose copy or generous padding in the sidebar without
checking it still fits around that height. This is desktop/laptop-landscape
only by explicit product decision — no attempt is made to support portrait
or phone-sized viewports.

`anonymizer.spec` is the single PyInstaller spec for all three OSes — its
`Analysis`/`EXE`/`COLLECT` blocks are platform-generic (PyInstaller resolves
platform differences internally when *run on* that OS; it cannot
cross-compile, so each platform's build must actually execute there — see
`.github/workflows/build.yml`). Only the final `BUNDLE(...)` step
(macOS `.app` metadata) is guarded by `if sys.platform == "darwin"`.

**spaCy language models must be explicitly collected — they will NOT be found
otherwise.** `de_core_news_lg`/`en_core_web_lg` are separate installed
packages, not part of the `spacy` package itself; `spacy.load("de_core_news_lg")`
resolves them via a dynamic import PyInstaller's static analysis can't see.
Without `collect_all("de_core_news_lg")` / `collect_all("en_core_web_lg")` in
the spec (in addition to `collect_all("spacy")`), the frozen app fails at
runtime with `[E050] Can't find model 'de_core_news_lg'` — this was hit and
fixed during development, not a hypothetical.

**`OUTPUT_DIR` must not default to a path inside the frozen app.** A
PyInstaller build's `__file__` resolves to somewhere inside the bundle/install
dir; writing generated files there breaks code signing and typically isn't
writable once properly installed (e.g. `/Applications`, `Program Files`).
`app/config.py`'s `_default_output_dir()` checks `sys.frozen` (set by
PyInstaller's bootloader) and points at a platform-appropriate user data dir
instead (`~/Library/Application Support/Anonymizer` on macOS, `%APPDATA%`
on Windows, `$XDG_DATA_HOME`/`~/.local/share` on Linux) — verified by building,
running the frozen macOS app, and confirming output actually lands there
instead of inside `Anonymizer.app/Contents/Resources/output/`.

**macOS ad-hoc codesigning can fail with "resource fork, Finder information,
or similar detritus not allowed"** even after `xattr -cr` on the bundle, and
even on a byte-for-byte fresh copy (`tar --no-xattrs` round-trip) — on at
least one build machine, `com.apple.provenance` got silently reapplied
regardless of how the files were recreated, which looks like the OS tagging
output by the *creating process's* own provenance rather than something
carried on the file content. `scripts/build_macos.sh` treats this signing
step as best-effort (warns, doesn't fail the build) since the `.app` still
runs fine locally either way — only Gatekeeper's opinion of a fresh download
elsewhere is affected. If you hit this, try re-running from a plain Terminal
outside any sandboxed/wrapped shell before assuming it's a spec problem.

**Ollama's dependency check uses its HTTP API (`/api/tags`, `/api/pull`), not
the `ollama` CLI.** `app/pipeline/setup_check.py` originally shelled out to
`ollama list` / `ollama pull` — this breaks entirely for Docker (or any setup
where `OLLAMA_HOST` points at a different machine/container than the one
running this app), since there is correctly no local `ollama` binary to shell
out to in that case, even though the remote Ollama is perfectly reachable and
functional. `_ollama_tags()`/`_ollama_pull_via_http()` talk to Ollama over
HTTP instead, working identically whether Ollama is local (desktop app) or
remote (Docker) — `shutil.which("ollama")` is now only used to produce a
nicer diagnostic message (not-installed vs. installed-but-not-running) for
the local case, never to gate availability.

**Docker**: `Dockerfile` bakes spaCy models in at build time (so a fresh
container works immediately) and deliberately excludes `pywebview` from the
installed requirements (it's only imported by `app/main.py`, never
`app/server.py`, and needs Linux GUI system libraries the container has no
use for). The container runs `uvicorn app.server:app --host 0.0.0.0` directly
— never `app/main.py` — since there's no native GUI in a container.
`docker-compose.yml` points `OLLAMA_HOST` at `http://host.docker.internal:11434`
with an `extra_hosts: host-gateway` entry so this resolves on native Linux
Docker too, not just Docker Desktop (Mac/Windows) where it works by default.
Verified end-to-end: built, ran via `docker compose up`, confirmed
`/api/dependencies` reaches the host's real Ollama instance, and ran a full
analyze→finalize round trip with output landing in the volume-mounted
`./output` on the host.

**Linux packaging has a real, unavoidable runtime dependency this repo cannot
bundle away**: pywebview needs GTK + WebKit2GTK (PyGObject bindings and their
GObject-introspection typelib files) installed system-wide on whatever
machine *runs* the AppImage — these don't bundle reliably into a portable
AppImage. `scripts/build_linux.sh` documents the exact package names
(`python3-gi` + `gir1.2-webkit2-4.1` on Debian/Ubuntu, etc.) rather than
pretending this is a non-issue.

**Windows/Linux builds are unverified.** `scripts/build_windows.ps1` +
`scripts/anonymizer-installer.iss` (Inno Setup) and `scripts/build_linux.sh`
were written from the same working macOS spec and PyInstaller's documented
cross-platform behavior, but there was no Windows or Linux machine available
to actually run them. `.github/workflows/build.yml`'s matrix (macOS/Windows/
Linux native runners + a Docker smoke test) is what would actually prove
them — pushing this repo to GitHub and letting that workflow run is the
recommended next step before trusting those two scripts, rather than treating
them as equivalent in confidence to the macOS build or the Docker setup.

## Known limitations (see README.md for the user-facing version)

PII detection is inherently probabilistic — the audit table in every output is
there so a human can verify what was found, not because recall is guaranteed.
Phone/structured-ID recognition is limited to the countries listed in
`SUPPORTED_PHONE_REGIONS` / `RELEVANT_ID_COUNTRIES` in `app/config.py`.
