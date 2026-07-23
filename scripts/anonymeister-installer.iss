; Inno Setup script for the AnonyMeister Windows installer.
; Compile via scripts\build_windows.ps1, which passes the real app version
; through as /DMyAppVersion=X.Y.Z (read from app/version.py — the single
; source of truth also used by anonymeister.spec's macOS bundle metadata).
; Compiling this file directly (ISCC.exe scripts\anonymeister-installer.iss,
; skipping build_windows.ps1) falls back to "0.0.0-dev" below, so a
; version-less manual invocation is conspicuous rather than silently
; minting another stale "1.0.0" like this installer used to.
;
; NOTE: written to match scripts\build_windows.ps1's PyInstaller output
; (dist\AnonyMeister\AnonyMeister.exe) but not verified on an actual Windows
; machine — no Windows environment was available to test the compile step.

#define MyAppName "AnonyMeister"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "Lernsachen.blog"
#define MyAppURL "https://lernsachen.blog"
#define MyAppExeName "AnonyMeister.exe"

[Setup]
AppId={{B1E9F6B4-9C6E-4B5A-9C7A-ANONYMEISTERAPP}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=AnonyMeister-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\AnonyMeister\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
