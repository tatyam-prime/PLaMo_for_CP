; The developer builds a per-user installer; end users do not need Python.
; Pass /DAppSource=... /DAppOutput=... when compiling this file.
#ifndef AppSource
  #define AppSource "..\..\dist\windows\PLaMo for 競プロ"
#endif
#ifndef AppOutput
  #define AppOutput "..\..\dist\windows"
#endif

[Setup]
AppId={{ED00503C-59D5-4F52-AF2D-4F87CB1D8255}
AppName=PLaMo for 競プロ
AppVersion=0.2.0
AppPublisher=PLaMo for 競プロ contributors
DefaultDirName={localappdata}\Programs\PLaMo for 競プロ
DefaultGroupName=PLaMo for 競プロ
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
OutputDir={#AppOutput}
OutputBaseFilename=PLaMo-for-競プロ-Windows-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icons\app-icon.ico
UninstallDisplayIcon={app}\PLaMo for 競プロ.exe

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PLaMo for 競プロ"; Filename: "{app}\PLaMo for 競プロ.exe"; IconFilename: "{app}\PLaMo for 競プロ.exe"; AppUserModelID: "PLaMoForCP.Desktop"
Name: "{autodesktop}\PLaMo for 競プロ"; Filename: "{app}\PLaMo for 競プロ.exe"; IconFilename: "{app}\PLaMo for 競プロ.exe"; AppUserModelID: "PLaMoForCP.Desktop"; Tasks: desktopicon

[Registry]
; Fixed open-only command: deliberately do not interpolate the untrusted URI (%1).
Root: HKCU; Subkey: "Software\Classes\plamo-for-cp"; ValueType: string; ValueData: "URL:PLaMo for 競プロ"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\plamo-for-cp"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\plamo-for-cp\DefaultIcon"; ValueType: string; ValueData: """{app}\PLaMo for 競プロ.exe"",0"
Root: HKCU; Subkey: "Software\Classes\plamo-for-cp\shell\open\command"; ValueType: string; ValueData: """{app}\PLaMo for 競プロ.exe"" --open-url ""plamo-for-cp://open"""

[Run]
Filename: "{app}\PLaMo for 競プロ.exe"; Description: "PLaMo for 競プロを開く"; Flags: nowait postinstall skipifsilent
