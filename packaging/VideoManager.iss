#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef AppFiles
  #define AppFiles "..\dist-release\VideoManager"
#endif

[Setup]
AppId={{8B63AB53-62CC-4DA3-AE04-2B6739F04993}
AppName=VideoManager
AppVersion={#AppVersion}
AppPublisher=herhtung
AppPublisherURL=https://github.com/herhtung/VideoManager
AppSupportURL=https://github.com/herhtung/VideoManager/issues
AppUpdatesURL=https://github.com/herhtung/VideoManager/releases
DefaultDirName={localappdata}\Programs\VideoManager
DefaultGroupName=VideoManager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayIcon={app}\VideoManager.exe
SetupIconFile=..\assets\videomanager.ico
OutputDir=..\build\installer
OutputBaseFilename=VideoManager-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
LicenseFile=..\LICENSE.md

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#AppFiles}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\VideoManager"; Filename: "{app}\VideoManager.exe"
Name: "{autodesktop}\VideoManager"; Filename: "{app}\VideoManager.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VideoManager.exe"; Description: "Launch VideoManager"; Flags: nowait postinstall skipifsilent
