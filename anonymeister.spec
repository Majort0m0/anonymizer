# PyInstaller spec for the AnonyMeister desktop app (macOS/Windows/Linux).
#
# spaCy models and Ollama are deliberately NOT bundled — they're large
# (500MB+ each) and already have a working "install if missing" story via
# the app's own Systemstatus panel. Bundling them would make every installer
# hundreds of MB larger for something the app can already self-heal.

import sys

from PyInstaller.utils.hooks import collect_all

# SPECPATH is a name PyInstaller injects into this file's exec namespace
# (the directory containing this .spec, i.e. the repo root) — inserted
# explicitly rather than relying on sys.path already containing the CWD,
# since that isn't guaranteed for however `pyinstaller` itself was invoked.
sys.path.insert(0, SPECPATH)
from app.version import APP_VERSION

block_cipher = None

datas = [("app/web/static", "app/web/static")]
binaries = []
hiddenimports = []

# Packages with resource files (YAML/JSON configs, native extension modules)
# that PyInstaller's static import analysis can't fully see on its own.
# de_core_news_lg/en_core_web_lg are separate installed packages (spaCy
# language models), not part of the "spacy" package itself — spacy.load()
# imports them dynamically by name, which static analysis can't follow, so
# without collecting them explicitly the frozen app fails at runtime with
# "Can't find model 'de_core_news_lg'" even though `spacy` itself is bundled.
for pkg in [
    "presidio_analyzer",
    "presidio_anonymizer",
    "spacy",
    "de_core_news_lg",
    "en_core_web_lg",
    "langdetect",
    "faster_whisper",
    "tokenizers",
    "ctranslate2",
]:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["app/main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AnonyMeister",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AnonyMeister",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="AnonyMeister.app",
        icon=None,
        bundle_identifier="blog.lernsachen.anonymeister",
        info_plist={
            "CFBundleName": "AnonyMeister",
            "CFBundleDisplayName": "AnonyMeister",
            "CFBundleShortVersionString": APP_VERSION,
            "NSHighResolutionCapable": True,
        },
    )
