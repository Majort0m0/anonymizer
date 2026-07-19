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

// --- Step bar / wizard navigation ------------------------------------------
//
// Four steps (Eingabe / Optionen / Kategorien prüfen / Ergebnis), one visible
// at a time inside #stage. currentStep is whichever the user is looking at;
// maxReachedStep is how far the actual pipeline has progressed (analyze/
// finalize) — the step bar only lets you jump back to an already-reached
// step, never ahead of it, since a later step's content doesn't exist yet.

const stepPanels = document.querySelectorAll(".step-panel");
const stepNodes = document.querySelectorAll(".step-node");
const stepConnectors = document.querySelectorAll(".step-connector");
const loadingOverlay = document.getElementById("loading-overlay");

let currentStep = 1;
let maxReachedStep = 1;

function renderStepBar() {
  stepNodes.forEach((node) => {
    const n = Number(node.dataset.step);
    const btn = node.querySelector(".step-btn");
    node.classList.toggle("current", n === currentStep);
    node.classList.toggle("done", n < currentStep || (n <= maxReachedStep && n !== currentStep));
    btn.disabled = n > maxReachedStep;
  });
  stepConnectors.forEach((conn, i) => {
    conn.classList.toggle("done", (i + 1) < maxReachedStep || (i + 1) < currentStep);
  });
}

function showPanel(step) {
  stepPanels.forEach((panel) => {
    if (panel === loadingOverlay) return;
    panel.classList.toggle("hidden", Number(panel.dataset.panel) !== step);
  });
  loadingOverlay.classList.add("hidden");
}

function goToStep(n) {
  if (n > maxReachedStep) return;
  currentStep = n;
  showPanel(n);
  renderStepBar();
}

function showLoadingOverlay() {
  stepPanels.forEach((panel) => panel.classList.add("hidden"));
  loadingOverlay.classList.remove("hidden");
}

document.querySelectorAll("[data-goto]").forEach((el) => {
  el.addEventListener("click", () => goToStep(Number(el.dataset.goto)));
});

// --- Global error banner ----------------------------------------------------

const errorBox = document.getElementById("error-box");
const errorText = document.getElementById("error-text");

function showError(message) {
  errorText.textContent = message;
  errorBox.classList.remove("hidden");
}

function hideError() {
  errorBox.classList.add("hidden");
  errorText.textContent = "";
}

// --- Step 1: Eingabe --------------------------------------------------------

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

const step1NextBtn = document.getElementById("step1-next-btn");

let selectedFile = null;

function hasClipboardText() {
  return clipboardPreview.value.trim().length > 0;
}

// Once a file or clipboard text is chosen, the dropzone/"oder"/clipboard-button
// picker UI has served its purpose — collapse it and rely on the compact
// file-chosen/clipboard-preview-wrap rows to show what's selected instead.
// Clearing the selection brings the picker back.
function updateStep1Readiness() {
  const ready = selectedFile !== null || hasClipboardText();
  step1NextBtn.disabled = !ready;
  inputChooser.classList.toggle("hidden", ready);
}

function isAcceptedFile(file) {
  const lowerName = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => lowerName.endsWith(ext));
}

function clearClipboardText() {
  clipboardPreview.value = "";
  clipboardPreviewWrap.classList.add("hidden");
  updateStep1Readiness();
}

function clearSelectedFile() {
  selectedFile = null;
  fileInput.value = "";
  fileChosen.classList.add("hidden");
  fileChosenName.textContent = "";
  updateStep1Readiness();
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
  updateStep1Readiness();
}

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
    updateStep1Readiness();
  } catch (err) {
    showError(
      "Zugriff auf die Zwischenablage nicht möglich. Bitte Berechtigung erteilen oder den Text manuell einfügen."
    );
  }
});

clipboardPreview.addEventListener("input", updateStep1Readiness);
clipboardClearBtn.addEventListener("click", clearClipboardText);

step1NextBtn.addEventListener("click", () => {
  if (step1NextBtn.disabled) return;
  maxReachedStep = Math.max(maxReachedStep, 2);
  goToStep(2);
});

// --- Step 2: Optionen --------------------------------------------------------

const anonymizeToggle = document.getElementById("anonymize-toggle");
const deepCheckRow = document.getElementById("deep-check-row");
const deepCheckToggle = document.getElementById("deep-check-toggle");
const outputModeRadios = document.querySelectorAll('input[name="output-mode"]');
const segmentedOptions = document.querySelectorAll(".segmented-option");
const analyzeBtn = document.getElementById("analyze-btn");

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
// finished. Both the analyze->step3 and finalize->step4 transitions reuse
// the SAME loading-overlay elements — only one of them is ever in flight at
// a time, since the wizard's steps are mutually exclusive.

const loadingBarFill = document.getElementById("loading-bar-fill");
const loadingStageLabel = document.getElementById("loading-stage-label");
const loadingEta = document.getElementById("loading-eta");

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

// --- Step 2 -> 3: analyze ---------------------------------------------------

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
  showLoadingOverlay();
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
      goToStep(2);
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
    goToStep(2);
    showError(
      err.message || "Verbindung zum lokalen Server fehlgeschlagen. Bitte erneut versuchen."
    );
  } finally {
    analyzeBtn.disabled = false;
  }
});

// --- Step 3: Kategorien prüfen (full occurrence checklist) ------------------

const reviewFilename = document.getElementById("review-filename");
const reviewLanguage = document.getElementById("review-language");
const reviewEmpty = document.getElementById("review-empty");
const reviewList = document.getElementById("review-list");
const finalizeBtn = document.getElementById("finalize-btn");

// Carries the pending-analysis token and its categories from step 3's render
// through to the finalize call. Cleared once finalize has consumed the token
// (a token can only be used once server-side anyway) or on "Neues Dokument".
let currentToken = null;
let currentCategories = [];

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

// The person-mode toggle sits ABOVE the occurrence list (built below it in
// the DOM here, but see styles.css: it's placed before .occurrence-list) so
// it never scrolls out of view on a category with many occurrences.
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

function buildOccurrenceSnippet(occurrence) {
  const span = document.createElement("span");
  span.className = "occurrence-snippet";
  span.append(occurrence.context_before);
  const mark = document.createElement("mark");
  mark.textContent = occurrence.text;
  span.appendChild(mark);
  span.append(occurrence.context_after);
  return span;
}

// Every matched text span gets its own checkbox (dataset.occurrenceId, read
// by getExcludedOccurrenceIds() below) — the category's own checkbox is a
// "select all" control: checked when every occurrence is included,
// indeterminate when some are, and toggling it flips every occurrence at
// once.
function buildCategoryRow(category) {
  const li = document.createElement("li");
  li.className = "review-item";
  li.dataset.category = category.category;
  if (category.is_person) {
    li.dataset.isPerson = "true";
  }

  const header = document.createElement("label");
  header.className = "review-item-header";

  const masterCheckbox = document.createElement("input");
  masterCheckbox.type = "checkbox";
  masterCheckbox.className = "review-item-checkbox";
  masterCheckbox.checked = true;

  const labelSpan = document.createElement("span");
  labelSpan.className = "review-item-label";
  labelSpan.textContent = formatCategoryLabel(category.category);

  const countSpan = document.createElement("span");
  countSpan.className = "review-item-count";
  countSpan.textContent = `${category.count}×`;

  const sourceSpan = document.createElement("span");
  sourceSpan.className = "review-item-source";
  sourceSpan.textContent = formatSourceLabel(category.source);

  header.append(masterCheckbox, labelSpan, countSpan, sourceSpan);
  li.appendChild(header);

  let personToggle = null;
  if (category.is_person) {
    personToggle = buildPersonToggle();
    li.appendChild(personToggle);
  }

  const occurrenceList = document.createElement("ul");
  occurrenceList.className = "occurrence-list";
  const occurrenceCheckboxes = [];

  function updateMasterCheckbox() {
    const total = occurrenceCheckboxes.length;
    const checkedCount = occurrenceCheckboxes.filter((cb) => cb.checked).length;
    masterCheckbox.checked = checkedCount === total;
    masterCheckbox.indeterminate = checkedCount > 0 && checkedCount < total;
    if (personToggle) setPersonToggleEnabled(personToggle, checkedCount > 0);
  }

  for (const occurrence of category.occurrences) {
    const occurrenceItem = document.createElement("li");
    occurrenceItem.className = "occurrence-item";
    const occurrenceLabel = document.createElement("label");
    const occurrenceCheckbox = document.createElement("input");
    occurrenceCheckbox.type = "checkbox";
    occurrenceCheckbox.checked = true;
    occurrenceCheckbox.dataset.occurrenceId = occurrence.id;
    occurrenceCheckbox.addEventListener("change", updateMasterCheckbox);
    occurrenceCheckboxes.push(occurrenceCheckbox);
    occurrenceLabel.appendChild(occurrenceCheckbox);
    occurrenceLabel.appendChild(buildOccurrenceSnippet(occurrence));
    occurrenceItem.appendChild(occurrenceLabel);
    occurrenceList.appendChild(occurrenceItem);
  }
  li.appendChild(occurrenceList);

  masterCheckbox.addEventListener("change", () => {
    const newValue = masterCheckbox.checked;
    for (const checkbox of occurrenceCheckboxes) checkbox.checked = newValue;
    masterCheckbox.indeterminate = false;
    if (personToggle) setPersonToggleEnabled(personToggle, newValue);
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
    // Categories arrive already sorted PERSON-first by the backend (see
    // app.pipeline.pipeline.analyze()) — rendered in that order as-is.
    for (const category of currentCategories) {
      reviewList.appendChild(buildCategoryRow(category));
    }
  }

  maxReachedStep = Math.max(maxReachedStep, 3);
  currentStep = 3;
  showPanel(3);
  renderStepBar();
}

function getExcludedOccurrenceIds() {
  const excluded = [];
  for (const checkbox of reviewList.querySelectorAll(".occurrence-item input[type=checkbox]")) {
    if (!checkbox.checked) excluded.push(checkbox.dataset.occurrenceId);
  }
  return excluded;
}

function getPersonMode() {
  const personItem = reviewList.querySelector('.review-item[data-is-person="true"]');
  if (!personItem) return "redact";
  const anyIncluded = Array.from(
    personItem.querySelectorAll(".occurrence-item input[type=checkbox]")
  ).some((checkbox) => checkbox.checked);
  if (!anyIncluded) return "redact"; // every occurrence excluded -> mode is moot
  const checkedRadio = personItem.querySelector('input[name="person-mode"]:checked');
  return checkedRadio ? checkedRadio.value : "redact";
}

finalizeBtn.addEventListener("click", async () => {
  if (!currentToken || finalizeBtn.disabled) return;

  hideError();
  finalizeBtn.disabled = true;
  showLoadingOverlay();
  resetProgressUI(loadingBarFill, loadingStageLabel, loadingEta);

  const excludedOccurrenceIds = getExcludedOccurrenceIds();
  const personMode = getPersonMode();

  try {
    const response = await fetch("/api/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: currentToken,
        excluded_occurrence_ids: excludedOccurrenceIds,
        person_mode: personMode,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      goToStep(3);
      showError(data.error || "Unbekannter Fehler bei der Anonymisierung.");
      return;
    }

    const result = await pollProgress(data.job_id, {
      fillEl: loadingBarFill,
      labelEl: loadingStageLabel,
      etaEl: loadingEta,
    });

    // The token is single-use; whether it succeeded or was already
    // consumed server-side, it's no longer valid — drop it client-side too.
    currentToken = null;
    currentCategories = [];
    renderResult(result);
  } catch (err) {
    goToStep(3);
    showError(
      err.message || "Verbindung zum lokalen Server fehlgeschlagen. Bitte erneut versuchen."
    );
  } finally {
    finalizeBtn.disabled = false;
  }
});

// --- Step 4: Ergebnis --------------------------------------------------------

const resultFilename = document.getElementById("result-filename");
const resultLanguage = document.getElementById("result-language");
const resultTabsBar = document.getElementById("result-tabs");
const resultTabButtons = document.querySelectorAll(".ribbon-tab");
const resultTranscriptWrap = document.getElementById("result-transcript-wrap");
const resultTranscript = document.getElementById("result-transcript");
const resultSummaryWrap = document.getElementById("result-summary-wrap");
const resultSummary = document.getElementById("result-summary");
const resultDownloads = document.getElementById("result-downloads");
const resultNewDocumentBtn = document.getElementById("result-new-document-btn");

const piiAuditBox = document.getElementById("pii-audit");
const piiAuditToggle = document.getElementById("pii-audit-toggle");
const piiAuditList = document.getElementById("pii-audit-list");

// Holds the current PipelineResult (from finalize or a previous replace) so
// find/replace requests have the text + metadata they need to send back —
// see performReplace().
let currentResult = null;

function collapsePiiAudit() {
  piiAuditToggle.setAttribute("aria-expanded", "false");
  piiAuditList.classList.add("hidden");
}

piiAuditToggle.addEventListener("click", () => {
  const expanded = piiAuditToggle.getAttribute("aria-expanded") === "true";
  piiAuditToggle.setAttribute("aria-expanded", String(!expanded));
  piiAuditList.classList.toggle("hidden", expanded);
});

// Switches between the transcript/summary panels via the ribbon — only
// relevant when both exist (output_mode "both"); see renderResult(), which
// hides the ribbon entirely and just shows the single panel otherwise.
function activateResultTab(targetId) {
  resultTabButtons.forEach((tab) => {
    tab.setAttribute("aria-selected", String(tab.dataset.target === targetId));
  });
  resultTranscriptWrap.classList.toggle("hidden", targetId !== "result-transcript-wrap");
  resultSummaryWrap.classList.toggle("hidden", targetId !== "result-summary-wrap");
}

resultTabButtons.forEach((tab) => {
  tab.addEventListener("click", () => activateResultTab(tab.dataset.target));
});

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
    collapsePiiAudit();
  } else {
    piiAuditBox.classList.add("hidden");
  }

  resultFilename.textContent = result.source_filename;
  resultLanguage.textContent = formatLanguageLabel(result.detected_language);

  resultTranscript.textContent = result.anonymized_transcript || "";
  resultSummary.textContent = result.summary || "";

  const hasTranscript = Boolean(result.anonymized_transcript);
  const hasSummary = Boolean(result.summary);

  if (hasTranscript && hasSummary) {
    resultTabsBar.classList.remove("hidden");
    activateResultTab("result-transcript-wrap");
  } else {
    resultTabsBar.classList.add("hidden");
    resultTranscriptWrap.classList.toggle("hidden", !hasTranscript);
    resultSummaryWrap.classList.toggle("hidden", !hasSummary);
  }

  resultDownloads.innerHTML = "";
  for (const file of result.downloads || []) {
    const link = document.createElement("a");
    link.className = "primary-btn download-link";
    link.href = `/api/download/${encodeURIComponent(file.filename)}`;
    link.setAttribute("download", file.filename);
    // Full label (e.g. "Transkript (Markdown)") goes in the tooltip; the
    // visible text is just the short name so the button stays single-line.
    link.textContent = file.label.split(" (")[0];
    link.title = `${file.label} herunterladen`;
    resultDownloads.appendChild(link);
  }

  findReplacePanel.classList.add("hidden");
  findReplaceToggle.setAttribute("aria-expanded", "false");
  setFindReplaceStatus("");

  maxReachedStep = Math.max(maxReachedStep, 4);
  currentStep = 4;
  showPanel(4);
  renderStepBar();
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

const findReplaceToggle = document.getElementById("find-replace-toggle");
const findReplacePanel = document.getElementById("find-replace-panel");
const findReplaceSearch = document.getElementById("find-replace-search");
const findReplaceReplacement = document.getElementById("find-replace-replacement");
const findReplaceCaseToggle = document.getElementById("find-replace-case-toggle");
const findReplaceOneBtn = document.getElementById("find-replace-one-btn");
const findReplaceAllBtn = document.getElementById("find-replace-all-btn");
const findReplaceStatus = document.getElementById("find-replace-status");

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

// --- "Neues Dokument" --------------------------------------------------------

function resetToInputPhase() {
  currentToken = null;
  currentCategories = [];
  currentResult = null;
  reviewList.innerHTML = "";
  resultTranscript.textContent = "";
  resultSummary.textContent = "";
  resultDownloads.innerHTML = "";
  findReplaceSearch.value = "";
  findReplaceReplacement.value = "";
  setFindReplaceStatus("");
  findReplacePanel.classList.add("hidden");
  findReplaceToggle.setAttribute("aria-expanded", "false");
  clearSelectedFile();
  clearClipboardText();
  hideError();
  maxReachedStep = 1;
  goToStep(1);
}

resultNewDocumentBtn.addEventListener("click", resetToInputPhase);

// --- System status ------------------------------------------------------------

const statusOpenBtn = document.getElementById("status-open-btn");
const statusModal = document.getElementById("status-modal");
const statusModalClose = document.getElementById("status-modal-close");
const statusLoading = document.getElementById("status-loading");
const dependencyList = document.getElementById("dependency-list");

const MODEL_PICKER_CUSTOM_VALUE = "__custom__";

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

// System status lives in a modal (like the help modal), opened from the
// header — there's no persistent sidebar anymore for it to live in.
function openStatusModal() {
  statusModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  loadDependencies();
  ollamaModelPicker.load();
  whisperModelPicker.load();
}

function closeStatusModal() {
  statusModal.classList.add("hidden");
  document.body.classList.remove("modal-open");
}

statusOpenBtn.addEventListener("click", openStatusModal);
statusModalClose.addEventListener("click", closeStatusModal);

statusModal.addEventListener("click", (event) => {
  if (event.target === statusModal) closeStatusModal();
});

// --- Help modal -----------------------------------------------------------

const helpBtn = document.getElementById("help-btn");
const helpModal = document.getElementById("help-modal");
const helpModalClose = document.getElementById("help-modal-close");

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
  if (event.key !== "Escape") return;
  if (!helpModal.classList.contains("hidden")) closeHelpModal();
  if (!statusModal.classList.contains("hidden")) closeStatusModal();
});

// --- init ---------------------------------------------------------------

updateStep1Readiness();
renderStepBar();
showPanel(1);
