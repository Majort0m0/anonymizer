# AnonyMeister — backend-only container image.
#
# The native desktop app wraps this same FastAPI backend in a pywebview
# window; a container has no native GUI, so this image runs just the backend
# (app/server.py) and the user opens the UI in a regular browser instead.
# Ollama is NOT bundled here — point OLLAMA_HOST at wherever it actually runs
# (see docker-compose.yml for the common cases: host machine, or a sibling
# "ollama" container).

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# pywebview is only used by the native desktop entrypoint (app/main.py), never
# imported by app/server.py — skip it here so the image doesn't need Linux's
# GTK/Qt GUI system libraries just to run a headless backend.
RUN grep -v '^pywebview' requirements.txt > requirements-server.txt \
    && pip install --no-cache-dir -r requirements-server.txt

# Baked in at build time so a fresh container works immediately, without
# relying on the "Systemstatus" panel's runtime self-heal download.
RUN python -m spacy download de_core_news_lg \
    && python -m spacy download en_core_web_lg

COPY app/ ./app/

RUN mkdir -p /app/output
VOLUME ["/app/output"]

EXPOSE 8765

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8765"]
