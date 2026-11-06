; The developer builds a per-user installer; end users do not need Python.
; Pass /DAppSource=... /DAppOutput=... when compiling this file.
#ifndef AppSource
  #define AppSource "..\..\dist\windows\PLaMo for CP"
#endif
#ifndef AppOutput
  #define AppOutput "..\..\dist\windows"
#endif

[Setup]
AppId={{ED00503C-59D5-4F52-AF2D-4F87CB1D8255}
AppName=PLaMo for CP
AppVersion=0.2.0
AppPublisher=PLaMo for CP contributors
DefaultDirName={localappdata}\Programs\PLaMoForCP
DefaultGroupName=PLaMo for CP
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
OutputDir={#AppOutput}
OutputBaseFilename=PLaMo-for-CP-Windows-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icons\app-icon.ico
UninstallDisplayIcon={app}\PLaMo for CP.exe

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PLaMo for CP"; Filename: "{app}\PLaMo for CP.exe"; IconFilename: "{app}\PLaMo for CP.exe"; AppUserModelID: "PLaMoForCP.Desktop"
Name: "{autodesktop}\PLaMo for CP"; Filename: "{app}\PLaMo for CP.exe"; IconFilename: "{app}\PLaMo for CP.exe"; AppUserModelID: "PLaMoForCP.Desktop"; Tasks: desktopicon

[Run]
Filename: "{app}\PLaMo for CP.exe"; Description: "PLaMo for CPを開く"; Flags: nowait postinstall skipifsilent
