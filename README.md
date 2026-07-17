# Anonymizer

Lokale Desktop-App: liest Dokumente oder Audioaufnahmen ein, anonymisiert
personenbezogene Daten (PII) und erzeugt daraus ein anonymisiertes Transkript
und/oder eine Zusammenfassung, jeweils als eigenständige Markdown-Datei. Für
tabellarische Quellformate (Excel, CSV, JSON, ODS) wird zusätzlich eine
anonymisierte Kopie im Originalformat erzeugt. Läuft vollständig lokal —
Transkription und Sprachmodell-Auswertung laufen über lokal installierte
Modelle (kein Cloud-API-Aufruf).

## Architektur

- **Backend**: Python (FastAPI), lokal auf `127.0.0.1:8765`
- **UI**: natives Fenster via `pywebview`, das die lokale Web-UI lädt (kein
  Electron, kein Node, kein Build-Schritt)
- **PII-Erkennung**: [Presidio](https://microsoft.github.io/presidio/)
  (Regex + spaCy-NER, Deutsch + Englisch) als deterministische Basis, optional
  gefolgt von einem LLM-"Tiefencheck" (lokales Ollama-Modell) für kontextuelle
  Hinweise wie Spitznamen, Rollenbezeichnungen oder Decknamen, die Presidio
  strukturell nicht erkennen kann. Postleitzahlen werden zusätzlich erkannt,
  wenn sie direkt vor einem als Ort erkannten Namen stehen (z. B. "12345
  Berlin") — eine reine Ziffern-Regel wäre im Deutschen zu ungenau, da jedes
  Substantiv großgeschrieben wird.
- **Workflow**: zweistufig — zuerst "Analysieren" (zeigt erkannte Kategorien
  mit Beispielen an, ohne bereits etwas zu schwärzen), dann wählt man aus,
  welche Kategorien tatsächlich anonymisiert werden sollen, bevor
  "Anonymisierung anwenden" den finalen Text erzeugt. Für Namen kann
  wahlweise geschwärzt ("[PERSON]") oder pseudonymisiert werden (Ersetzung
  durch einen erfundenen, aber im Dokument konsistenten Fantasienamen).
- **Transkription**: `faster-whisper` für Audio-Input
- **Zusammenfassung**: wird immer aus dem final anonymisierten Text erzeugt,
  nie aus dem Original — unabhängig davon, welche Kategorien der Nutzer von
  der Schwärzung ausgenommen hat. Sie wird als eigenständige Markdown-Datei
  ausgegeben, getrennt vom Transkript.
- **Ausgabedateien**: `{Originalname}-anonymisiert.md` (Transkript),
  `{Originalname}-zusammenfassung.md` (Zusammenfassung, falls gewählt), und
  bei tabellarischen Quellformaten (`.xlsx`/`.xls`, `.csv`, `.json`, `.ods`)
  zusätzlich `{Originalname}-anonymisiert.<Format>` — eine echte, weiter
  nutzbare Tabelle/Datei mit denselben Zeilen/Spalten/Schlüsseln wie das
  Original, nur mit anonymisierten Werten. Existiert ein Dateiname bereits,
  wird automatisch " (2)", " (3)" usw. angehängt, statt die vorherige Datei
  zu überschreiben. Legacy `.xls` wird dabei als `.xlsx` ausgegeben (siehe
  unten).
- **Suchen & Ersetzen**: nach der Anonymisierung lassen sich einzelne Begriffe
  im Transkript/in der Zusammenfassung nachträglich korrigieren — etwa ein
  von der Audio-Transkription falsch verstandenes Wort — wahlweise einzeln
  oder für alle Vorkommen auf einmal, mit optionaler Berücksichtigung von
  Groß-/Kleinschreibung. Die betroffenen Markdown-Downloads werden dabei neu
  erzeugt; eine tabellarische Ausgabedatei (xlsx/csv/json/ods) bleibt davon
  unberührt.

Die Modul-Verträge stehen in `app/schemas.py` (Datentypen) und `app/config.py`
(Modelle, Pfade, Regionen).

Eine ausführliche Bedienungsanleitung mit Erklärung aller Optionen und einem
Datenschutz-Abschnitt gibt es auch direkt in der App über den Button
„Anleitung & Datenschutz", sowie als eigenständiges Dokument in
[`ANLEITUNG.md`](ANLEITUNG.md).

## Setup

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -r requirements.txt

python -m spacy download de_core_news_lg
python -m spacy download en_core_web_lg
```

Voraussetzungen auf dem System:
- [Ollama](https://ollama.com/download) installiert und gestartet, Modell
  gepullt: `ollama pull gemma4:12b` (Modellname in `app/config.py` anpassbar)
- `ffmpeg` auf dem PATH (macOS: `brew install ffmpeg`)

Der "Systemstatus"-Bereich in der App zeigt an, was fehlt, und kann spaCy-
Modelle sowie das Ollama-Modell direkt aus der UI nachladen. `ffmpeg` und eine
fehlende Ollama-Installation müssen manuell installiert werden (die App
installiert keine Systempakete ohne Zutun).

## Starten

```bash
source .venv/bin/activate
python -m app.main
```

Öffnet ein natives Fenster mit der App. Alternativ nur das Backend (z. B. zum
Testen im Browser):

```bash
uvicorn app.server:app --host 127.0.0.1 --port 8765
```

## Installierbare App (macOS / Windows / Linux)

Baut ein natives Paket (kein `python`/`.venv` nötig zum Ausführen) über
PyInstaller. Vorher `uv pip install -r requirements-build.txt` (im aktivierten
venv) einmalig ausführen.

| Plattform | Befehl | Ergebnis |
| --- | --- | --- |
| macOS | `./scripts/build_macos.sh` | `dist/Anonymizer.app`, `dist/Anonymizer-macOS.dmg` |
| Windows | `.\scripts\build_windows.ps1` | `dist/Anonymizer/Anonymizer.exe`, mit installiertem [Inno Setup](https://jrsoftware.org/isinfo.php) zusätzlich `dist/Anonymizer-Setup.exe` |
| Linux | `./scripts/build_linux.sh` | `dist/Anonymizer-x86_64.AppImage` (braucht `python3-gi` + `gir1.2-webkit2-4.1` bzw. distro-Äquivalent auf dem Zielsystem — pywebview kann diese GTK/WebKit-Systemabhängigkeit nicht selbst mitbringen) |

Die macOS-Variante ist hier gebaut und getestet worden (inkl. eines
Ad-hoc-Signaturschritts). Windows/Linux sind vom selben Grundgerüst
abgeleitet, aber mangels verfügbarer Windows-/Linux-Umgebung nicht selbst
verifiziert — `.github/workflows/build.yml` baut alle drei Plattformen
automatisch auf ihren jeweiligen nativen GitHub-Actions-Runnern, sobald dieses
Repo auf GitHub liegt (Tag `v*` oder manuell auslösbar); das ist der
verlässlichste Weg zu einem echten, geprüften Windows-/Linux-Build.

spaCy-Modelle und Ollama werden bewusst **nicht** mit ins Paket gebündelt
(zusammen 500MB+ und bereits über den „Systemstatus"-Bereich selbst
nachladbar) — ein frisch installiertes Paket zeigt beim ersten Start also
noch fehlende Abhängigkeiten an, genau wie ein Quellcode-Checkout.

## Docker

```bash
docker compose up -d
```

Startet nur das Backend (kein natives Fenster in einem Container möglich) —
die Web-UI ist danach unter `http://localhost:8765` im Browser erreichbar.
`docker-compose.yml` ist so konfiguriert, dass der Container einen bereits
auf dem Host laufenden Ollama erreicht (`host.docker.internal`); alternativ
lässt sich dort ein `ollama`-Service auskommentieren, um Ollama ebenfalls im
Container zu betreiben. Ergebnisdateien landen über ein Volume in `./output`
auf dem Host. spaCy-Modelle werden beim Image-Build fest eingebacken, das
Image ist entsprechend groß (~5-6GB).

## Unterstützte Eingaben

- Text/Dokumente: `.txt`, `.md`, `.docx`, `.pdf`
- Tabellen/Daten: `.xlsx`/`.xlsm`/`.xls`, `.csv`, `.json`
- OpenDocument: `.odt`, `.ods`, `.odp`
- Audio: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`, `.aac`, `.wma`, `.opus`, `.aiff`/`.aif`, `.caf`, `.webm`
- Text aus der Zwischenablage

Legacy `.doc` (altes Word-Binärformat) wird erkannt, aber mit einer klaren
Fehlermeldung abgelehnt — vorher als `.docx` speichern. Tabellenformate
(`.xlsx`/`.xls`, `.ods`) werden pro Blatt als `## Blattname` + `|`-getrennte
Zeilen ins Transkript übernommen; `.json` wird eingerückt neu serialisiert.
Für diese vier Formate (`.xlsx`/`.xls`, `.csv`, `.json`, `.ods`) entsteht
zusätzlich eine anonymisierte Kopie im Originalformat (siehe oben). `.docx`,
`.pdf`, `.odt`/`.odp` bekommen nur das Markdown-Transkript, kein Original-
formatiges Duplikat.

## Bekannte Grenzen

- **PII-Erkennung ist nicht perfekt.** Presidios spaCy-NER erkennt Namen in
  unnatürlichem, tabellarischem Text (z. B. `Name | Erika Musterfrau`)
  zuverlässiger in Fließtext als in listenartigen Formaten. Der LLM-Tiefencheck
  ist gezielt auf *kontextuelle* Hinweise (Spitznamen, Rollenbezeichnungen,
  Decknamen) ausgelegt, nicht als generischer zweiter Namens-Scan — er fängt
  nicht automatisch jede von Presidio verpasste Entität ab. Das
  Anonymisierungs-Protokoll am Ende jeder Ausgabe listet auf, was erkannt
  wurde, damit das Ergebnis nachvollziehbar bleibt; bei sensiblen Dokumenten
  lohnt sich ein manueller Blick auf das Transkript vor der Weitergabe.
- **Telefonnummer-/ID-Erkennung** ist auf die in `app/config.py` unter
  `SUPPORTED_PHONE_REGIONS` / `RELEVANT_ID_COUNTRIES` gelisteten Länder
  begrenzt (aktuell DE/AT/CH/US/GB bzw. DE/AT/CH/US).
- **`faster-whisper`** lädt das gewählte Modell (`WHISPER_MODEL_SIZE` in
  `app/config.py`, Standard `small`) beim ersten Gebrauch automatisch aus dem
  Internet und cached es danach lokal.
- **Die anonymisierte Tabellen-Kopie (`.xlsx`/`.csv`/`.json`/`.ods`) kann
  strenger anonymisiert sein als das Markdown-Transkript desselben
  Dokuments.** Für die Tabellen-Kopie wird jede Zelle einzeln neu auf PII
  geprüft, statt die bereits im Vorschau-Schritt gefundenen Treffer wieder-
  zuverwenden — isolierter Zelltext ist für die Namenserkennung oft
  zuverlässiger als derselbe Text im tabellarisch zusammengefügten Fließtext
  (siehe Punkt oben), sodass die Tabellen-Kopie gelegentlich zusätzliche
  Treffer schwärzt, die im Transkript stehen geblieben sind. Das ist
  beabsichtigt (mehr Vorsicht bei wiederverwendbaren Rohdaten), kann sich
  aber wie eine Inkonsistenz zwischen den beiden Ausgabedateien anfühlen.
- **Legacy `.xls`** kann nicht im Originalformat zurückgeschrieben werden
  (kein verlässlicher moderner Writer dafür) — die anonymisierte Kopie wird
  stattdessen als `.xlsx` gespeichert.
