#ifndef AppVersion
  #define AppVersion "0.2.13"
#endif
#ifndef AppFiles
  #define AppFiles "..\dist-local\VideoManager"
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
CloseApplications=no
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

[Code]
function CreateFileW(FileName: String; DesiredAccess, ShareMode: Cardinal;
  SecurityAttributes: Integer; CreationDisposition, FlagsAndAttributes: Cardinal;
  TemplateFile: Integer): Integer;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseHandle(Handle: Integer): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function IsFileInUse(FileName: String): Boolean;
var
  Handle: Integer;
begin
  Result := False;
  if not FileExists(FileName) then
    Exit;
  Handle := CreateFileW(FileName, $40000000, 0, 0, 3, $80, 0);
  Result := Handle = -1;
  if Handle <> -1 then
    CloseHandle(Handle);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsFileInUse(ExpandConstant('{app}\VideoManager.exe')) or
     IsFileInUse(ExpandConstant('{app}\_internal\bin\ffmpeg.exe')) or
     IsFileInUse(ExpandConstant('{app}\_internal\bin\ffprobe.exe')) then
    Result := 'VideoManager 或背景壓縮程序仍在使用目前版本。請等工作完成，從 VM 正常關閉程式後，再重新執行安裝。安裝器不會代替你結束壓縮工作。';
end;
