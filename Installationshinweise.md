# Installationshinweise

Kurzübersicht zur Installation der AnonyMeister-App. Eine ausführliche
Bedienungsanleitung gibt es in [`ANLEITUNG.md`](ANLEITUNG.md), technische
Details in [`README.md`](README.md).

## Systemvoraussetzungen (alle Plattformen)

- **Ollama** installiert und gestartet — für den optionalen LLM-Tiefencheck
  und die Zusammenfassung. Download: https://ollama.com/download
  Anschließend ein Modell laden (Standard):
  ```bash
  ollama pull gemma4:e4b
  ```
  Das Modell lässt sich später jederzeit im „Systemstatus"-Bereich der App
  umstellen (kuratierte Auswahl `gemma4:e2b`/`e4b`/`12b`/`26b` oder Freitext
  für jedes andere lokal gepullte Modell).
- **spaCy-Sprachmodelle** (Deutsch + Englisch) — werden beim ersten Start
  über den „Systemstatus"-Bereich der App direkt nachgeladen, kein manueller
  Schritt nötig.
- Kein System-`ffmpeg` nötig — Audiotranskription (`faster-whisper`) bringt
  die benötigten Bibliotheken mit.
- Ohne laufendes Ollama funktioniert die App trotzdem — nur Tiefencheck und
  Zusammenfassung stehen dann nicht zur Verfügung.

---

## macOS

1. `.dmg` aus den [Releases](https://github.com/Majort0m0/anonymizer/releases/latest)
   herunterladen und öffnen, App in den `Applications`-Ordner ziehen.
2. **Beim ersten Start blockiert Gatekeeper die App** („AnonyMeister.app ist
   beschädigt" / „konnte nicht überprüft werden"). Grund: Die App ist nur
   ad-hoc signiert, nicht notariert (ein Apple Developer-Zertifikat kostet
   99 $/Jahr und steht für dieses Projekt nicht zur Verfügung). Die App
   trotzdem öffnen:
   - **Systemeinstellungen → Datenschutz & Sicherheit** öffnen, runterscrollen.
     Dort erscheint „AnonyMeister.app wurde blockiert…" mit Button
     **„Trotzdem öffnen"** — klicken und bestätigen.
   - Falls das nicht erscheint, per Terminal das Quarantäne-Flag entfernen:
     ```bash
     xattr -cr /Applications/AnonyMeister.app
     ```
   - Alternativ: Rechtsklick (bzw. Ctrl-Klick) auf die App → „Öffnen" → im
     Dialog nochmal „Öffnen" bestätigen.
3. App starten, im „Systemstatus"-Bereich fehlende spaCy-Modelle/Ollama-Modell
   nachladen lassen.

## Windows

1. `.exe`-Installer (falls vorhanden, sonst das gepackte `AnonyMeister.exe`
   direkt) aus den [Releases](https://github.com/Majort0m0/anonymizer/releases/latest)
   herunterladen.
2. Installer ausführen bzw. `AnonyMeister.exe` starten. Windows SmartScreen kann
   bei einer unsignierten `.exe` warnen — „Weitere Informationen" →
   „Trotzdem ausführen" wählen.
3. Ollama separat installieren (siehe oben), App starten, im
   „Systemstatus"-Bereich fehlende Modelle nachladen lassen.

## Linux

1. `.AppImage` aus den [Releases](https://github.com/Majort0m0/anonymizer/releases/latest)
   herunterladen, ausführbar machen und starten:
   ```bash
   chmod +x AnonyMeister-x86_64.AppImage
   ./AnonyMeister-x86_64.AppImage
   ```
2. **Voraussetzung, die nicht in der AppImage steckt:** GTK + WebKit2GTK samt
   PyGObject-Bindings müssen systemweit installiert sein, sonst bricht der
   Start mit `ModuleNotFoundError: No module named 'gi'` ab:
   ```bash
   # Debian/Ubuntu
   sudo apt install python3-gi gir1.2-webkit2-4.1

   # Fedora
   sudo dnf install python3-gobject webkit2gtk4.1

   # Arch
   sudo pacman -S python-gobject webkit2gtk-4.1 gtk3
   ```
3. Ollama separat installieren (siehe oben), App starten, im
   „Systemstatus"-Bereich fehlende Modelle nachladen lassen.

> Windows/Linux-Installer sind vom gleichen Grundgerüst wie der verifizierte
> macOS-Build abgeleitet, aber (mangels verfügbarer Test-Umgebung) nicht
> selbst auf einem echten Windows-/Linux-Rechner verifiziert worden.

---

## Docker (Backend, ohne natives Fenster)

Läuft nur als Web-UI im Browser unter `http://localhost:8765` — kein
natives Fenster in einem Container möglich. Schritt für Schritt:

1. **Repo klonen** (oder `docker-compose.yml` + `Dockerfile` herunterladen):
   ```bash
   git clone https://github.com/Majort0m0/anonymizer.git
   cd anonymizer
   ```
2. **Ollama bereitstellen** — entweder auf dem Host installieren und starten
   (https://ollama.com/download, dann `ollama pull gemma4:12b`), oder den
   auskommentierten `ollama`-Service in `docker-compose.yml` aktivieren, um
   Ollama ebenfalls im Container zu betreiben. Standardmäßig zeigt der
   Container über `host.docker.internal` auf einen bereits lokal laufenden
   Ollama.
3. **Container bauen und starten:**
   ```bash
   docker compose up -d
   ```
   Das Image bringt die spaCy-Modelle bereits fest eingebacken mit (Image
   ist entsprechend groß, ~5-6 GB), es ist also kein Nachladeschritt nötig.
4. **App öffnen:** http://localhost:8765 im Browser aufrufen.
5. **Ausgabedateien** landen über ein Volume in `./output` im geklonten
   Repo-Ordner auf dem Host.
6. **Stoppen:**
   ```bash
   docker compose down
   ```

---

## Bekannte Einschränkungen

- PII-Erkennung ist probabilistisch, kein Ersatz für eine manuelle Prüfung
  sensibler Dokumente vor Weitergabe.
- Telefonnummer-/ID-Erkennung ist auf DE/AT/CH/US(/GB) begrenzt.
- Noch keine automatisierte Test-Suite.

Details dazu in [`README.md`](README.md#bekannte-grenzen).
