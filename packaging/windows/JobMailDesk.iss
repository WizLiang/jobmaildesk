; JobMailDesk Windows installer (Inno Setup 6.3+).
; Compiled by Install-JobMailDesk.ps1:
;   ISCC.exe /DAppVersion=0.7.0rc1 /DSourceDir=<dist\JobMailDesk> [/DBootstrapper=<MicrosoftEdgeWebview2Setup.exe>] JobMailDesk.iss
; Per-user install (no administrator rights). The data directory
; %LOCALAPPDATA%\JobMailDesk holds the Markdown facts and is never touched by
; install or uninstall; the system credential store is not touched either.

#ifndef AppVersion
  #error Pass the package version with /DAppVersion=...
#endif
#ifndef SourceDir
  #error Pass the PyInstaller onedir output with /DSourceDir=...
#endif

#define MyAppName "JobMailDesk"
#define MyAppExeName "JobMailDesk.exe"
#define MyAppMutex "Local\JobMailDesk.Desktop.Singleton.v1"
#define MyAppUserModelID "JobMailDesk"
#define WebView2ClientKey "Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{7D5B7C2E-3D8A-4B39-9E1A-6C4A2F0B9D11}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher=JobMailDesk
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableDirPage=auto
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputBaseFilename=JobMailDesk-Setup-v{#AppVersion}-win-x64
SetupIconFile=JobMailDesk.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
AppMutex={#MyAppMutex}
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "Start JobMailDesk when you sign in (tray, reminders, mail scan)"; GroupDescription: "Startup:"

[InstallDelete]
; PyInstaller onedir upgrades must not leave stale modules behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
#ifdef Bootstrapper
Source: "{#Bootstrapper}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall; Check: not WebView2Installed
#endif

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "show"; WorkingDir: "{app}"; IconFilename: "{app}\JobMailDesk.ico"; AppUserModelID: "{#MyAppUserModelID}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "show"; WorkingDir: "{app}"; IconFilename: "{app}\JobMailDesk.ico"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "show"; WorkingDir: "{app}"; IconFilename: "{app}\JobMailDesk.ico"; AppUserModelID: "{#MyAppUserModelID}"; Tasks: autostart

[Registry]
; Toasts sent by the PowerShell fallback are attributed to this AppUserModelID.
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#MyAppUserModelID}"; ValueType: string; ValueName: "DisplayName"; ValueData: "{#MyAppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\{#MyAppUserModelID}"; ValueType: string; ValueName: "IconUri"; ValueData: "{app}\JobMailDesk.ico"

[Run]
#ifdef Bootstrapper
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2 Runtime..."; Check: not WebView2Installed; Flags: waituntilterminated
#endif
Filename: "{app}\{#MyAppExeName}"; Parameters: "show"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function VersionPresent(const Root: Integer; const SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, SubKey, 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  Result := VersionPresent(HKLM, 'SOFTWARE\WOW6432Node\{#WebView2ClientKey}')
    or VersionPresent(HKLM, 'SOFTWARE\{#WebView2ClientKey}')
    or VersionPresent(HKCU, 'Software\{#WebView2ClientKey}');
end;
