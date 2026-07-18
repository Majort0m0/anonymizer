"use strict";

const ACCEPTED_EXTENSIONS = [
  ".txt", ".md", ".docx", ".doc", ".pdf",
  ".xlsx", ".xlsm", ".xls", ".csv", ".json", ".odt", ".ods", ".odp",
  ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".opus", ".aiff", ".aif", ".caf", ".webm",
];

const SOURCE_LABELS = {
  presidio: "Presidio",
  llm_deep_check: "LLM-Tiefencheck",
  llm_final_check: "LLM-Nachkontrolle",
  column_header: "Spalten-Erkennung",
};

const LANGUAGE_LABELS = {
  de: "Deutsch",
  en: "Englisch",
};

// Human-readable German labels for the well-known Presidio categories. Any
// category not listed here (including deep-check's free-form labels like
// "SPITZNAME" or "DECKNAME") falls back to a title-cased version of the raw
// string — see formatCategoryLabel().
const CATEGORY_LABELS = {
  PERSON: "Namen",
  LOCATION: "Orte",
  EMAIL_ADDRESS: "E-Mail-Adressen",
  PHONE_NUMBER: "Telefonnummern",
  POSTAL_CODE: "Postleitzahlen",
  IBAN_CODE: "IBAN",
  DATE_TIME: "Datum/Uhrzeit",
  URL: "URLs",
  CREDIT_CARD: "Kreditkarten",
};

// --- Phase 1: input ---------------------------------------------------------

const inputChooser = document.getElementById("input-chooser");
const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const fileChosen = document.getElementById("file-chosen");
const fileChosenName = document.getElementById("file-chosen-name");
const fileClearBtn = document.getElementById("file-clear-btn");

const clipboardBtn = document.getElementById("clipboard-btn");
const clipboardPreviewWrap = document.getElementById("clipboard-preview-wrap");
const clipboardPreview = document.getElementById("clipboard-preview");
const clipboardClearBtn = document.getElementById("clipboard-clear-btn");

const anonymizeToggle = document.getElementById("anonymize-toggle");
const deepCheckRow = document.getElementById("deep-check-row");
const deepCheckToggle = document.getElementById("deep-check-toggle");
const outputModeRadios = document.querySelectorAll('input[name="output-mode"]');

const segmentedOptions = document.querySelectorAll(".segmented-option");

const analyzeBtn = document.getElementById("analyze-btn");
const analyzeHint = document.getElementById("analyze-hint");
const loadingBox = document.getElementById("loading");
const loadingBarFill = document.getElementById("loading-bar-fill");
const loadingStageLabel = document.getElementById("loading-stage-label");
const loadingEta = document.getElementById("loading-eta");
const errorBox = document.getElementById("error-box");
const errorText = document.getElementById("error-text");

// --- Main content column (empty state / category review / result) ---------

const emptyState = document.getElementById("empty-state");

const reviewCard = document.getElementById("review-card");
const reviewFilename = document.getElementById("review-filename");
const reviewLanguage = document.getElementById("review-language");
const reviewEmpty = document.getElementById("review-empty");
const reviewList = document.getElementById("review-list");
const finalizeBtn = document.getElementById("finalize-btn");
const reviewRestartBtn = document.getElementById("review-restart-btn");
const finalizeLoading = document.getElementById("finalize-loading");
const finalizeBarFill = document.getElementById("finalize-bar-fill");
const finalizeStageLabel = document.getElementById("finalize-stage-label");
const finalizeEta = document.getElementById("finalize-eta");

// --- Phase 3: result ---------------------------------------------------------

const resultCard = document.getElementById("result-card");
const piiAuditBox = document.getElementById("pii-audit");
const piiAuditList = document.getElementById("pii-audit-list");
const resultFilename = document.getElementById("result-filename");
const resultLanguage = document.getElementById("result-language");
const resultTranscriptWrap = document.getElementById("result-transcript-wrap");
const resultTranscript = document.getElementById("result-transcript");
const resultSummaryWrap = document.getElementById("result-summary-wrap");
const resultSummary = document.getElementById("result-summary");
const resultDownloads = document.getElementById("result-downloads");
const resultNewDocumentBtn = document.getElementById("result-new-document-btn");

// Holds the current PipelineResult (from finalize or a previous replace) so
// find/replace requests have the text + metadata they need to send back —
// see performReplace().
let currentResult = null;

// --- Find & replace ------------------------------------------------------

const findReplaceToggle = document.getElementById("find-replace-toggle");
const findReplacePanel = document.getElementById("find-replace-panel");
const findReplaceSearch = document.getElementById("find-replace-search");
const findReplaceReplacement = document.getElementById("find-replace-replacement");
const findReplaceCaseToggle = document.getElementById("find-replace-case-toggle");
const findReplaceOneBtn = document.getElementById("find-replace-one-btn");
const findReplaceAllBtn = document.getElementById("find-replace-all-btn");
const findReplaceStatus = document.getElementById("find-replace-status");

// --- Help modal ---------------------------------------------------------------

const helpBtn = document.getElementById("help-btn");
const helpModal = document.getElementById("help-modal");
const helpModalClose = document.getElementById("help-modal-close");

// --- System status ------------------------------------------------------------

const statusToggle = document.getElementById("status-toggle");
const statusToggleIcon = document.getElementById("status-toggle-icon");
const statusPanel = document.getElementById("status-panel");
const statusLoading = document.getElementById("status-loading");
const dependencyList = document.getElementById("dependency-list");

const MODEL_PICKER_CUSTOM_VALUE = "__custom__";

let selectedFile = null;

// Carries the pending-analysis token and its categories from phase 2 render
// through to the finalize call. Cleared on "Neu starten" and once finalize
// has consumed the token (a token can only be used once server-side anyway).
let currentToken = null;
let currentCategories = [];

function hasClipboardText() {
  return clipboardPreview.value.trim().length > 0;
}

// Once a file or clipboard text is chosen, the dropzone/"oder"/clipboard-button
// picker UI has served its purpose and just eats vertical space that the
// options/analyze cards below need (this used to force scrolling to reach
// "Analysieren" on shorter windows) — collapse it and rely on the compact
// file-chosen/clipboard-preview-wrap rows to show what's selected instead.
// Clearing the selection (or restarting) brings the picker back.
function updateAnalyzeButtonState() {
  const ready = selectedFile !== null || hasClipboardText();
  analyzeBtn.disabled = !ready;
  analyzeHint.classList.toggle("hidden", ready);
  inputChooser.classList.toggle("hidden", ready);
}

function isAcceptedFile(file) {
  const lowerName = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lowerName.endsWith(ext));
}

function clearClipboardText() {
  clipboardPreview.value = "";
  clipboardPreviewWrap.classList.add("hidden");
  updateAnalyzeButtonState();
}

function clearSelectedFile() {
  selectedFile = null;
  fileInput.value = "";
  fileChosen.classList.add("hidden");
  fileChosenName.textContent = "";
  updateAnalyzeButtonState();
}

function setSelectedFile(file) {
  if (!isAcceptedFile(file)) {
    showError(
      `Dateityp nicht unterstützt. Erlaubt sind: ${ACCEPTED_EXTENSIONS.join(", ")}`
    );
    return;
  }
  clearClipboardText();
  selectedFile = file;
  fileChosenName.textContent = file.name;
  fileChosen.classList.remove("hidden");
  hideError();
  updateAnalyzeButtonState();
}

function showError(message) {
  errorText.textContent = message;
  errorBox.classList.remove("hidden");
}

function hideError() {
  errorBox.classList.add("hidden");
  errorText.textContent = "";
}

// --- Phase switching ---------------------------------------------------------
//
// The sidebar (input/options/action/status) is always visible — it's a
// persistent control panel, not a "phase" that gets hidden. Only the main
// content column switches between the empty state, category review, and
// the result.

function showEmptyState() {
  emptyState.classList.remove("hidden");
}

function hideEmptyState() {
  emptyState.classList.add("hidden");
}

// Full reset back to the empty main-content state: discards any pending
// (unfinalized) token client-side (the server-side entry just becomes
// unused until app restart, which is fine) and clears both input methods.
function resetToInputPhase() {
  currentToken = null;
  currentCategories = [];
  currentResult = null;
  reviewList.innerHTML = "";
  reviewCard.classList.add("hidden");
  resultCard.classList.add("hidden");
  findReplaceSearch.value = "";
  findReplaceReplacement.value = "";
  setFindReplaceStatus("");
  findReplacePanel.classList.add("hidden");
  findReplaceToggle.setAttribute("aria-expanded", "false");
  clearSelectedFile();
  clearClipboardText();
  hideError();
  showEmptyState();
}

// --- Dropzone -------------------------------------------------------------

dropzone.addEventListener("click", () => fileInput.click());

dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("dragover");
});

dropzone.addEventListener("dragleave", () => {
  dropzone.classList.remove("dragover");
});

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragover");
  const files = event.dataTransfer?.files;
  if (files && files.length > 0) {
    setSelectedFile(files[0]);
  }
});

fileInput.addEventListener("change", () => {
  if (fileInput.files && fileInput.files.length > 0) {
    setSelectedFile(fileInput.files[0]);
  }
});

fileClearBtn.addEventListener("click", clearSelectedFile);

// --- Clipboard --------------------------------------------------------------

clipboardBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) {
      showError("Die Zwischenablage enthält keinen Text.");
      return;
    }
    clearSelectedFile();
    clipboardPreview.value = text;
    clipboardPreviewWrap.classList.remove("hidden");
    hideError();
    updateAnalyzeButtonState();
  } catch (err) {
    showError(
      "Zugriff auf die Zwischenablage nicht möglich. Bitte Berechtigung erteilen oder den Text manuell einfügen."
    );
  }
});

clipboardPreview.addEventListener("input", updateAnalyzeButtonState);
clipboardClearBtn.addEventListener("click", clearClipboardText);

// --- Options ---------------------------------------------------------------

function getOutputMode() {
  for (const radio of outputModeRadios) {
    if (radio.checked) return radio.value;
  }
  return "both";
}

function updateSegmentedHighlight() {
  for (const option of segmentedOptions) {
    const input = option.querySelector("input");
    option.classList.toggle("selected", input.checked);
  }
}

for (const radio of outputModeRadios) {
  radio.addEventListener("change", updateSegmentedHighlight);
}
updateSegmentedHighlight();

// Tiefencheck is meaningless without anonymization (nothing gets redacted for
// it to double-check) — disabling the anonymize toggle disables and unchecks
// it too, mirroring setPersonToggleEnabled()'s pattern for the person-mode
// toggle below.
function updateDeepCheckAvailability() {
  const anonymizeEnabled = anonymizeToggle.checked;
  deepCheckRow.classList.toggle("disabled", !anonymizeEnabled);
  deepCheckToggle.disabled = !anonymizeEnabled;
  if (!anonymizeEnabled) {
    deepCheckToggle.checked = false;
  }
}

anonymizeToggle.addEventListener("change", updateDeepCheckAvailability);
updateDeepCheckAvailability();

function formatSourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}

function formatLanguageLabel(code) {
  if (!code) return "";
  return LANGUAGE_LABELS[code] || code.toUpperCase();
}

// Falls back to a title-cased version of the raw category string for
// anything not in CATEGORY_LABELS (this naturally covers deep-check's
// free-form labels such as "SPITZNAME" or "DECKNAME").
function formatCategoryLabel(category) {
  if (CATEGORY_LABELS[category]) return CATEGORY_LABELS[category];
  return category
    .split("_")
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

// --- Progress polling ---------------------------------------------------------
//
// analyze-file/analyze-clipboard/finalize all return {job_id} immediately —
// the actual (potentially minutes-long, Ollama-backed) work runs server-side
// in a background thread. pollProgress() polls GET /api/progress/{job_id}
// until the job reports done, updating a progress bar + calibrated ETA (see
// app/progress_calibration.py) as it goes, and returns the job's `result`
// (shaped exactly like the old synchronous response body used to be) once
// finished — so the caller's post-processing is otherwise unchanged.

const PROGRESS_POLL_INTERVAL_MS = 700;

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 1) return "noch < 1 Sek.";
  if (seconds < 60) return `noch ca. ${Math.round(seconds)} Sek.`;
  return `noch ca. ${Math.round(seconds / 60)} Min.`;
}

function resetProgressUI(fillEl, labelEl, etaEl) {
  fillEl.style.width = "0%";
  labelEl.textContent = "Wird vorbereitet…";
  etaEl.textContent = "";
}

// A single dropped fetch (e.g. a momentary loopback hiccup) shouldn't abort
// a multi-minute job outright — the job keeps running server-side either
// way, so retrying a few times is strictly better than surfacing a terminal
// error for a transient blip. A real problem (server actually down) will
// keep failing past this budget and still surface as an error.
const PROGRESS_POLL_MAX_CONSECUTIVE_FAILURES = 5;

async function pollProgress(jobId, { fillEl, labelEl, etaEl }) {
  let consecutiveFailures = 0;
  while (true) {
    let response;
    try {
      response = await fetch(`/api/progress/${jobId}`);
      consecutiveFailures = 0;
    } catch (err) {
      consecutiveFailures += 1;
      if (consecutiveFailures > PROGRESS_POLL_MAX_CONSECUTIVE_FAILURES) {
        throw new Error("Verbindung zum lokalen Server fehlgeschlagen.");
      }
      await new Promise((resolve) => setTimeout(resolve, PROGRESS_POLL_INTERVAL_MS));
      continue;
    }

    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Fortschritt konnte nicht abgerufen werden.");
    }

    const data = await response.json();
    fillEl.style.width = `${data.percent}%`;
    labelEl.textContent = data.stage_label;
    etaEl.textContent = formatEta(data.eta_seconds);

    if (data.done) {
      if (data.error) {
        throw new Error(data.error);
      }
      fillEl.style.width = "100%";
      return data.result;
    }

    await new Promise((resolve) => setTimeout(resolve, PROGRESS_POLL_INTERVAL_MS));
  }
}

// --- Phase 1 -> 2: analyze ---------------------------------------------------

async function analyzeFile(file, outputMode, anonymize, deepCheck) {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("output_mode", outputMode);
  formData.append("anonymize", String(anonymize));
  formData.append("deep_check", String(deepCheck));

  const response = await fetch("/api/analyze-file", {
    method: "POST",
    body: formData,
  });
  return response;
}

async function analyzeClipboard(text, outputMode, anonymize, deepCheck) {
  const response = await fetch("/api/analyze-clipboard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      output_mode: outputMode,
      anonymize,
      deep_check: deepCheck,
    }),
  });
  return response;
}

analyzeBtn.addEventListener("click", async () => {
  if (analyzeBtn.disabled) return;

  hideError();
  hideEmptyState();
  reviewCard.classList.add("hidden");
  resultCard.classList.add("hidden");
  loadingBox.classList.remove("hidden");
  resetProgressUI(loadingBarFill, loadingStageLabel, loadingEta);
  analyzeBtn.disabled = true;

  const outputMode = getOutputMode();
  const anonymize = anonymizeToggle.checked;
  const deepCheck = deepCheckToggle.checked;

  try {
    let response;
    if (selectedFile !== null) {
      response = await analyzeFile(selectedFile, outputMode, anonymize, deepCheck);
    } else {
      response = await analyzeClipboard(clipboardPreview.value, outputMode, anonymize, deepCheck);
    }

    const data = await response.json();

    if (!response.ok) {
      showError(data.error || "Unbekannter Fehler bei der Analyse.");
      return;
    }

    const result = await pollProgress(data.job_id, {
      fillEl: loadingBarFill,
      labelEl: loadingStageLabel,
      etaEl: loadingEta,
    });
    renderCategories(result);
  } catch (err) {
    showError(
      err.message || "Verbindung zum lokalen Server fehlgeschlagen. Bitte erneut versuchen."
    );
  } finally {
    loadingBox.classList.add("hidden");
    updateAnalyzeButtonState();
  }
});

// --- Phase 2: category review -----------------------------------------------

function updateSegmentedSelected(segmented) {
  for (const option of segmented.querySelectorAll(".segmented-option")) {
    const input = option.querySelector("input");
    option.classList.toggle("selected", input.checked);
  }
}

function setPersonToggleEnabled(personToggle, enabled) {
  personToggle.classList.toggle("disabled", !enabled);
  for (const input of personToggle.querySelectorAll("input")) {
    input.disabled = !enabled;
  }
}

function buildSegmentedRadio(name, value, text, checked) {
  const label = document.createElement("label");
  label.className = "segmented-option" + (checked ? " selected" : "");
  const input = document.createElement("input");
  input.type = "radio";
  input.name = name;
  input.value = value;
  input.checked = checked;
  const span = document.createElement("span");
  span.textContent = text;
  label.append(input, span);
  return label;
}

const PERSON_MODE_DESCRIPTIONS = {
  redact: "Ersetzt jede Namensnennung durch den allgemeinen Platzhalter „[PERSON]“.",
  numbered:
    "Nummeriert unterschiedliche Namen durchgehend („[PERSON1]“, „[PERSON2]“, …) — hilfreich, um mehrere Personen im Text auseinanderzuhalten, ohne echte Namen preiszugeben.",
  pseudonymize: "Ersetzt Namen durch erfundene, aber konsistente Fantasienamen.",
};

function buildPersonToggle() {
  const wrap = document.createElement("div");
  wrap.className = "review-person-toggle";

  const segmented = document.createElement("div");
  segmented.className = "segmented segmented--compact";
  segmented.setAttribute("role", "radiogroup");
  segmented.setAttribute("aria-label", "Namen-Behandlung");

  const desc = document.createElement("p");
  desc.className = "review-person-toggle-desc";
  desc.textContent = PERSON_MODE_DESCRIPTIONS.redact;

  const redactOption = buildSegmentedRadio("person-mode", "redact", "Schwärzen", true);
  const numberedOption = buildSegmentedRadio("person-mode", "numbered", "Nummerieren", false);
  const pseudoOption = buildSegmentedRadio("person-mode", "pseudonymize", "Pseudonymisieren", false);
  segmented.append(redactOption, numberedOption, pseudoOption);

  for (const option of [redactOption, numberedOption, pseudoOption]) {
    const input = option.querySelector("input");
    input.addEventListener("change", () => {
      updateSegmentedSelected(segmented);
      desc.textContent = PERSON_MODE_DESCRIPTIONS[input.value];
    });
  }

  wrap.append(segmented, desc);
  return wrap;
}

function buildCategoryRow(category) {
  const li = document.createElement("li");
  li.className = "review-item";
  li.dataset.category = category.category;
  if (category.is_person) {
    li.dataset.isPerson = "true";
  }

  const header = document.createElement("label");
  header.className = "review-item-header";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "review-item-checkbox";
  checkbox.checked = true;

  const labelSpan = document.createElement("span");
  labelSpan.className = "review-item-label";
  labelSpan.textContent = formatCategoryLabel(category.category);

  const countSpan = document.createElement("span");
  countSpan.className = "review-item-count";
  countSpan.textContent = `${category.count}×`;

  const sourceSpan = document.createElement("span");
  sourceSpan.className = "review-item-source";
  sourceSpan.textContent = formatSourceLabel(category.source);

  header.append(checkbox, labelSpan, countSpan, sourceSpan);
  li.appendChild(header);

  if (category.samples && category.samples.length > 0) {
    const samples = document.createElement("p");
    samples.className = "review-item-samples";
    samples.textContent = `Beispiele: ${category.samples.join(", ")}`;
    li.appendChild(samples);
  }

  let personToggle = null;
  if (category.is_person) {
    personToggle = buildPersonToggle();
    li.appendChild(personToggle);
  }

  checkbox.addEventListener("change", () => {
    if (personToggle) {
      setPersonToggleEnabled(personToggle, checkbox.checked);
    }
  });

  return li;
}

function renderCategories(pending) {
  currentToken = pending.token;
  currentCategories = pending.categories || [];

  reviewFilename.textContent = pending.source_filename;
  reviewLanguage.textContent = formatLanguageLabel(pending.detected_language);

  reviewList.innerHTML = "";

  if (pending.anonymize === false) {
    // Nothing was detected because detection never ran (the user turned
    // anonymization off) — a different claim than "we checked and found
    // nothing", so it gets its own message rather than reusing reviewEmpty's
    // default text. Nothing to review or exclude either, so the button just
    // moves on to producing the plain transcript/summary.
    reviewEmpty.textContent =
      "Anonymisierung ist deaktiviert — Transkript und/oder Zusammenfassung werden unverändert aus dem Original erstellt.";
    reviewEmpty.classList.remove("hidden");
    reviewList.classList.add("hidden");
    finalizeBtn.textContent = "Weiter";
  } else if (currentCategories.length === 0) {
    reviewEmpty.textContent = "Es wurden keine personenbezogenen Daten erkannt.";
    reviewEmpty.classList.remove("hidden");
    reviewList.classList.add("hidden");
    finalizeBtn.textContent = "Anonymisierung anwenden";
  } else {
    reviewEmpty.classList.add("hidden");
    reviewList.classList.remove("hidden");
    finalizeBtn.textContent = "Anonymisierung anwenden";
    for (const category of currentCategories) {
      reviewList.appendChild(buildCategoryRow(category));
    }
  }

  hideEmptyState();
  resultCard.classList.add("hidden");
  reviewCard.classList.remove("hidden");
  reviewCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

function getExcludedCategories() {
  const excluded = [];
  for (const item of reviewList.querySelectorAll(".review-item")) {
    const checkbox = item.querySelector(".review-item-checkbox");
    if (!checkbox.checked) {
      excluded.push(item.dataset.category);
    }
  }
  return excluded;
}

function getPersonMode() {
  const personItem = reviewList.querySelector('.review-item[data-is-person="true"]');
  if (!personItem) return "redact";
  const checkbox = personItem.querySelector(".review-item-checkbox");
  if (!checkbox.checked) return "redact"; // excluded entirely -> mode is moot
  const checkedRadio = personItem.querySelector('input[name="person-mode"]:checked');
  return checkedRadio ? checkedRadio.value : "redact";
}

reviewRestartBtn.addEventListener("click", resetToInputPhase);

finalizeBtn.addEventListener("click", async () => {
  if (!currentToken || finalizeBtn.disabled) return;

  hideError();
  finalizeBtn.disabled = true;
  reviewRestartBtn.disabled = true;
  finalizeLoading.classList.remove("hidden");
  resetProgressUI(finalizeBarFill, finalizeStageLabel, finalizeEta);

  const excludedCategories = getExcludedCategories();
  const personMode = getPersonMode();

  try {
    const response = await fetch("/api/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: currentToken,
        excluded_categories: excludedCategories,
        person_mode: personMode,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      showError(data.error || "Unbekannter Fehler bei der Anonymisierung.");
      return;
    }

    const result = await pollProgress(data.job_id, {
      fillEl: finalizeBarFill,
      labelEl: finalizeStageLabel,
      etaEl: finalizeEta,
    });

    // The token is single-use; whether it succeeded or was already
    // consumed server-side, it's no longer valid — drop it client-side too.
    currentToken = null;
    currentCategories = [];
    reviewCard.classList.add("hidden");
    renderResult(result);
  } catch (err) {
    showError(
      err.message || "Verbindung zum lokalen Server fehlgeschlagen. Bitte erneut versuchen."
    );
  } finally {
    finalizeBtn.disabled = false;
    reviewRestartBtn.disabled = false;
    finalizeLoading.classList.add("hidden");
  }
});

// --- Phase 3: result ---------------------------------------------------------

function renderResult(result) {
  currentResult = result;

  piiAuditList.innerHTML = "";
  if (result.pii_audit && result.pii_audit.length > 0) {
    for (const entity of result.pii_audit) {
      const li = document.createElement("li");
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = String(entity.count);
      li.appendChild(count);
      li.append(` × ${entity.entity_type} (${formatSourceLabel(entity.source)})`);
      piiAuditList.appendChild(li);
    }
    piiAuditBox.classList.remove("hidden");
  } else {
    piiAuditBox.classList.add("hidden");
  }

  resultFilename.textContent = result.source_filename;
  resultLanguage.textContent = formatLanguageLabel(result.detected_language);

  if (result.anonymized_transcript) {
    resultTranscript.textContent = result.anonymized_transcript;
    resultTranscriptWrap.classList.remove("hidden");
  } else {
    resultTranscriptWrap.classList.add("hidden");
  }

  if (result.summary) {
    resultSummary.textContent = result.summary;
    resultSummaryWrap.classList.remove("hidden");
  } else {
    resultSummaryWrap.classList.add("hidden");
  }

  resultDownloads.innerHTML = "";
  for (const file of result.downloads || []) {
    const link = document.createElement("a");
    link.className = "primary-btn download-link";
    link.href = `/api/download/${encodeURIComponent(file.filename)}`;
    link.setAttribute("download", file.filename);
    link.textContent = `${file.label} herunterladen`;
    resultDownloads.appendChild(link);
  }

  resultCard.classList.remove("hidden");
  resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

// --- Find & replace ---------------------------------------------------------
//
// Fixes individual words after the fact — e.g. a term an audio transcription
// misheard. "Ersetzen" replaces just the next remaining occurrence (click it
// again for the one after that); "Alle ersetzen" replaces every occurrence
// in one go. Each action round-trips to the server so the downloadable
// markdown files stay in sync with what's shown on screen; any non-markdown
// download (a structured-format copy) is sent along untouched so it isn't
// silently dropped from the list.

findReplaceToggle.addEventListener("click", () => {
  const expanded = findReplaceToggle.getAttribute("aria-expanded") === "true";
  const next = !expanded;
  findReplaceToggle.setAttribute("aria-expanded", String(next));
  findReplacePanel.classList.toggle("hidden", !next);
  if (next) {
    findReplaceSearch.focus();
  }
});

function countOccurrences(text, search, matchCase) {
  if (!text || !search) return 0;
  const haystack = matchCase ? text : text.toLowerCase();
  const needle = matchCase ? search : search.toLowerCase();
  let count = 0;
  let pos = 0;
  while (true) {
    const idx = haystack.indexOf(needle, pos);
    if (idx === -1) break;
    count += 1;
    pos = idx + needle.length;
  }
  return count;
}

function setFindReplaceStatus(message) {
  findReplaceStatus.textContent = message;
  findReplaceStatus.classList.toggle("hidden", !message);
}

async function performReplace(replaceAll) {
  if (!currentResult) return;

  const search = findReplaceSearch.value;
  if (!search.trim()) {
    setFindReplaceStatus("Bitte einen Suchbegriff eingeben.");
    return;
  }

  const matchCase = findReplaceCaseToggle.checked;
  const remainingBefore =
    countOccurrences(currentResult.anonymized_transcript, search, matchCase) +
    countOccurrences(currentResult.summary, search, matchCase);
  if (remainingBefore === 0) {
    setFindReplaceStatus("Kein Treffer gefunden.");
    return;
  }

  findReplaceOneBtn.disabled = true;
  findReplaceAllBtn.disabled = true;
  setFindReplaceStatus("Wird angewendet…");

  try {
    const response = await fetch("/api/replace-text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_filename: currentResult.source_filename,
        detected_language: currentResult.detected_language,
        anonymization_enabled: currentResult.anonymization_enabled,
        deep_check_enabled: currentResult.deep_check_enabled,
        anonymized_transcript: currentResult.anonymized_transcript,
        summary: currentResult.summary,
        pii_audit: currentResult.pii_audit,
        downloads: currentResult.downloads,
        search,
        replacement: findReplaceReplacement.value,
        match_case: matchCase,
        replace_all: replaceAll,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      setFindReplaceStatus(data.error || "Fehler beim Ersetzen.");
      return;
    }

    renderResult(data);

    const remainingAfter =
      countOccurrences(data.anonymized_transcript, search, matchCase) +
      countOccurrences(data.summary, search, matchCase);
    setFindReplaceStatus(
      remainingAfter > 0
        ? `Ersetzt. Noch ${remainingAfter} weitere${remainingAfter === 1 ? "r" : ""} Treffer.`
        : "Ersetzt. Keine weiteren Treffer."
    );
  } catch (err) {
    setFindReplaceStatus("Verbindung zum lokalen Server fehlgeschlagen.");
  } finally {
    findReplaceOneBtn.disabled = false;
    findReplaceAllBtn.disabled = false;
  }
}

findReplaceOneBtn.addEventListener("click", () => performReplace(false));
findReplaceAllBtn.addEventListener("click", () => performReplace(true));

for (const input of [findReplaceSearch, findReplaceReplacement]) {
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      performReplace(true);
    }
  });
}

// --- System status --------------------------------------------------------

function renderDependencies(statuses) {
  dependencyList.innerHTML = "";
  for (const status of statuses) {
    const li = document.createElement("li");
    li.className = "dependency-item";

    const dot = document.createElement("span");
    dot.className = "dependency-dot" + (status.available ? " available" : "");
    li.appendChild(dot);

    const body = document.createElement("div");
    body.className = "dependency-body";

    const name = document.createElement("div");
    name.className = "dependency-name";
    name.textContent = status.name;
    body.appendChild(name);

    if (status.detail) {
      const detail = document.createElement("div");
      detail.className = "dependency-detail";
      detail.textContent = status.detail;
      body.appendChild(detail);
    }

    if (!status.available && status.install_hint) {
      const hint = document.createElement("div");
      hint.className = "dependency-hint";
      hint.textContent = status.install_hint;
      body.appendChild(hint);
    }

    li.appendChild(body);

    if (!status.available) {
      const fixBtn = document.createElement("button");
      fixBtn.type = "button";
      fixBtn.className = "dependency-fix-btn";
      fixBtn.textContent = "Reparieren";
      fixBtn.addEventListener("click", () => fixDependency(status.name, fixBtn));
      li.appendChild(fixBtn);
    }

    dependencyList.appendChild(li);
  }
}

async function loadDependencies() {
  statusLoading.classList.remove("hidden");
  statusLoading.textContent = "Lade Systemstatus…";
  dependencyList.innerHTML = "";
  try {
    const response = await fetch("/api/dependencies");
    if (!response.ok) {
      statusLoading.textContent = "Systemstatus konnte nicht geladen werden.";
      return;
    }
    const statuses = await response.json();
    statusLoading.classList.add("hidden");
    renderDependencies(statuses);
  } catch (err) {
    statusLoading.textContent = "Systemstatus konnte nicht geladen werden.";
  }
}

async function fixDependency(name, button) {
  button.disabled = true;
  const originalText = button.textContent;
  button.innerHTML = "";
  const spinner = document.createElement("span");
  spinner.className = "spinner small";
  button.appendChild(spinner);
  button.append(" Wird installiert…");

  try {
    const response = await fetch("/api/dependencies/fix", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await response.json();
    if (!response.ok) {
      statusLoading.classList.remove("hidden");
      statusLoading.textContent = data.error || "Reparatur fehlgeschlagen.";
    }
  } catch (err) {
    statusLoading.classList.remove("hidden");
    statusLoading.textContent = "Reparatur fehlgeschlagen: Verbindung zum Server nicht möglich.";
  } finally {
    button.disabled = false;
    button.textContent = originalText;
    await loadDependencies();
  }
}

// Shared by both Systemstatus model pickers (Ollama chat model, Whisper
// size): same curated-select-plus-free-text-fallback UI and the same
// GET/POST /api/settings/<endpoint> shape, just different element ids and
// backend endpoint.
function initModelPicker({ idPrefix, endpoint }) {
  const select = document.getElementById(`${idPrefix}-select`);
  const custom = document.getElementById(`${idPrefix}-custom`);
  const applyBtn = document.getElementById(`${idPrefix}-apply`);
  const status = document.getElementById(`${idPrefix}-status`);

  function populate(currentModel, curated) {
    select.innerHTML = "";
    for (const option of curated) {
      const opt = document.createElement("option");
      opt.value = option.name;
      opt.textContent = option.recommended
        ? `${option.name} — ${option.label} (Empfehlung)`
        : `${option.name} — ${option.label}`;
      select.appendChild(opt);
    }
    const customOpt = document.createElement("option");
    customOpt.value = MODEL_PICKER_CUSTOM_VALUE;
    customOpt.textContent = "Andere (Freitext)…";
    select.appendChild(customOpt);

    const isCurated = curated.some((option) => option.name === currentModel);
    if (isCurated) {
      select.value = currentModel;
      custom.classList.add("hidden");
    } else {
      select.value = MODEL_PICKER_CUSTOM_VALUE;
      custom.classList.remove("hidden");
      custom.value = currentModel;
    }
  }

  async function load() {
    try {
      const response = await fetch(`/api/settings/${endpoint}`);
      if (!response.ok) return;
      const data = await response.json();
      populate(data.model, data.curated);
    } catch (err) {
      // Systemstatus panel already surfaces server-unreachable via
      // loadDependencies(); nothing more to show here.
    }
  }

  select.addEventListener("change", () => {
    custom.classList.toggle("hidden", select.value !== MODEL_PICKER_CUSTOM_VALUE);
  });

  applyBtn.addEventListener("click", async () => {
    const model =
      select.value === MODEL_PICKER_CUSTOM_VALUE ? custom.value.trim() : select.value;
    if (!model) {
      status.textContent = "Bitte einen Modellnamen angeben.";
      status.className = "model-picker-status error";
      return;
    }

    applyBtn.disabled = true;
    status.textContent = "Wird übernommen…";
    status.className = "model-picker-status";
    try {
      const response = await fetch(`/api/settings/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      const data = await response.json();
      if (!response.ok) {
        status.textContent = data.error || "Übernehmen fehlgeschlagen.";
        status.className = "model-picker-status error";
        return;
      }
      status.textContent = `Aktives Modell: ${data.model}`;
      status.className = "model-picker-status success";
      await loadDependencies();
    } catch (err) {
      status.textContent = "Übernehmen fehlgeschlagen: Verbindung zum Server nicht möglich.";
      status.className = "model-picker-status error";
    } finally {
      applyBtn.disabled = false;
    }
  });

  return { load };
}

const ollamaModelPicker = initModelPicker({ idPrefix: "ollama-model", endpoint: "ollama-model" });
const whisperModelPicker = initModelPicker({ idPrefix: "whisper-model", endpoint: "whisper-model" });

statusToggle.addEventListener("click", () => {
  const expanded = statusToggle.getAttribute("aria-expanded") === "true";
  const next = !expanded;
  statusToggle.setAttribute("aria-expanded", String(next));
  statusPanel.classList.toggle("hidden", !next);
  if (next) {
    loadDependencies();
    ollamaModelPicker.load();
    whisperModelPicker.load();
  }
});

// --- "Neues Dokument" ---------------------------------------------------------

resultNewDocumentBtn.addEventListener("click", () => {
  resultTranscript.textContent = "";
  resultSummary.textContent = "";
  resultDownloads.innerHTML = "";
  resetToInputPhase();
});

// --- Help modal -----------------------------------------------------------

function openHelpModal() {
  helpModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
}

function closeHelpModal() {
  helpModal.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

helpBtn.addEventListener("click", openHelpModal);
helpModalClose.addEventListener("click", closeHelpModal);

helpModal.addEventListener("click", (event) => {
  if (event.target === helpModal) closeHelpModal();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !helpModal.classList.contains("hidden")) {
    closeHelpModal();
  }
});

updateAnalyzeButtonState();
