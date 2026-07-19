import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _app_data_dir() -> Path:
    # A PyInstaller-frozen app's __file__ resolves to a path inside the
    # (signed, read-only-in-spirit) app bundle/install dir — writing there
    # breaks code signing and isn't guaranteed writable once installed
    # (e.g. /Applications). Running from source (this repo, unfrozen) keeps
    # the original BASE_DIR-relative behavior.
    if not getattr(sys, "frozen", False):
        return BASE_DIR

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AnonyMeister"
    elif sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "AnonyMeister"
    else:
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "anonymeister"


# Base directory for anything this app persists locally (generated output,
# the settings.json in app/settings.py) — same directory for both, just
# different files, so there is only one platform-path decision to make.
APP_DATA_DIR = _app_data_dir()

# Overridable so a mounted volume (Docker) or a different install location can
# relocate where generated files land, without touching any other code.
OUTPUT_DIR = Path(os.environ.get("ANONYMIZER_OUTPUT_DIR", str(APP_DATA_DIR / "output")))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Ollama — OLLAMA_HOST must be overridable: inside a Docker container,
# "localhost" refers to the container itself, not the host machine (or a
# sibling "ollama" container), so the default only works for the native
# desktop app running directly on the host.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# This is the hardcoded/env-var fallback only. The Systemstatus UI lets a
# user pick a different model at runtime (persisted via app/settings.py); an
# explicit OLLAMA_MODEL env var (e.g. set by Docker) always wins over that
# UI choice — see app.settings.get_ollama_model(). gemma4:e4b (also the
# "recommended" entry in CURATED_OLLAMA_MODELS below) rather than the
# larger 12b: direct side-by-side testing on real documents showed e4b
# several times faster with equal or better deep-check recall — the larger
# model wasn't buying back anything the size cost, at least for this task.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")

# Ollama's /api/chat defaults to a small context window (historically 2048
# tokens) unless a request explicitly asks for more — regardless of how much
# context the model itself supports. Left unset, a long document could be
# silently truncated before the model ever sees all of it, with no error.
# 8192 tokens comfortably covers several times the length of a typical
# document processed by this app while still bounding the extra KV-cache
# memory every deep-check/summarize call reserves.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

# Curated choices offered in the Systemstatus UI's model picker, covering the
# realistic RAM/VRAM range end users' machines will have (sizes/RAM figures
# per ollama.com/library/gemma4 tags, 4-bit quantization). Not exhaustive —
# the UI also accepts a free-text model name for anything else pulled
# locally.
CURATED_OLLAMA_MODELS = [
    {
        "name": "gemma4:e2b",
        "label": "Sehr sparsam (~3 GB) — für Rechner mit wenig RAM/VRAM, keine dedizierte GPU nötig",
    },
    {
        "name": "gemma4:e4b",
        "label": "Empfohlen (~4,5 GB) — guter Kompromiss aus Qualität und Ressourcenbedarf für die meisten Rechner",
        "recommended": True,
    },
    {
        "name": "gemma4:12b",
        "label": "Beste Qualität (~6,7 GB) — braucht mehr RAM/VRAM (mind. 16 GB RAM empfohlen)",
    },
    {
        "name": "gemma4:26b",
        "label": "Höchste Qualität (~14,4 GB) — für leistungsstarke Maschinen mit viel RAM/dedizierter GPU",
    },
]

# faster-whisper — hardcoded/env-var fallback only, same pattern as
# OLLAMA_MODEL above: the Systemstatus UI lets a user pick a different size
# at runtime (persisted via app/settings.py), an explicit WHISPER_MODEL_SIZE
# env var always wins over that UI choice — see
# app.settings.get_whisper_model_size().
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "small")  # tiny|base|small|medium|large-v3 - small fits 16GB comfortably
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")  # good speed/RAM tradeoff on Apple Silicon CPU

# Curated choices offered in the Systemstatus UI's Whisper size picker
# (RAM figures for CPU inference at int8 quantization, widely-cited
# community benchmarks for faster-whisper/CTranslate2). Not exhaustive — the
# UI also accepts a free-text size (e.g. "large-v3-turbo") for anything else
# faster-whisper supports.
CURATED_WHISPER_MODELS = [
    {
        "name": "tiny",
        "label": "Sehr sparsam (~1 GB RAM) — deutlich schneller, aber spürbar ungenauer; für kurze/einfache Aufnahmen",
    },
    {
        "name": "small",
        "label": "Empfohlen (~1,5–2 GB RAM) — guter Kompromiss aus Qualität und Ressourcenbedarf für die meisten Rechner",
        "recommended": True,
    },
    {
        "name": "medium",
        "label": "Bessere Qualität (~5 GB RAM) — für Rechner mit mehr Arbeitsspeicher",
    },
    {
        "name": "large-v3",
        "label": "Beste Qualität (~10 GB RAM) — braucht deutlich mehr Ressourcen",
    },
]

# Presidio / spaCy
SPACY_MODELS = {
    "de": "de_core_news_lg",
    "en": "en_core_web_lg",
}
DEFAULT_LANGUAGE = "de"
SUPPORTED_PHONE_REGIONS = ["DE", "AT", "CH", "US", "GB"]

# Presidio ships country-specific structured-ID recognizers (US SSN, UK NHS, ...)
# tagged with a lowercase ISO-3166-1-ish country_code. Locales outside this list
# are excluded so an unrelated country's ID pattern (e.g. a UK NHS checksum)
# can't coincidentally out-score and swallow a real match (e.g. a phone number)
# from a country we don't otherwise support.
RELEVANT_ID_COUNTRIES = ["de", "at", "ch", "us"]

# 127.0.0.1 is correct for the native desktop app (pywebview talks to its own
# local backend only); a Docker container needs 0.0.0.0 to accept connections
# from outside the container — set directly via the container's CMD/uvicorn
# invocation rather than this default, so the desktop app's behavior is
# unaffected. SERVER_HOST/PORT are still overridable here for any other
# deployment that needs it.
SERVER_HOST = os.environ.get("ANONYMIZER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("ANONYMIZER_PORT", "8765"))
