; Windows installer for Kelvin (Inno Setup 6). Built by .github/workflows/release.yml:
;   ISCC.exe /DAppVersion=0.1.0 packaging\kelvin.iss
; Installs per user (no admin rights) into %LOCALAPPDATA%\Programs\Kelvin.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; /DSuffix=-private for the private build (the Claude Code bridge): same app id and folder, so it replaces the
; installed Kelvin like any update; there is only ever one Kelvin on a computer
#ifndef Suffix
  #define Suffix ""
#endif

[Setup]
AppId={{8C0F6E5A-3B7D-4C1E-9A2F-6D5E4B3A2C10}
AppName=Kelvin
AppVersion={#AppVersion}
AppVerName=Kelvin {#AppVersion}{#Suffix}
AppPublisher=Kelvin project
AppComments=The coolest cat in the lab
DefaultDirName={localappdata}\Programs\Kelvin
DefaultGroupName=Kelvin
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Kelvin-Setup-{#AppVersion}{#Suffix}
SetupIconFile=icons\kelvin.ico
UninstallDisplayIcon={app}\Kelvin.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
; Kelvin holds this mutex while it runs, so installing or uninstalling waits until it is closed
AppMutex=Kelvin-8C0F6E5A

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Kelvin\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Kelvin"; Filename: "{app}\Kelvin.exe"
Name: "{autodesktop}\Kelvin"; Filename: "{app}\Kelvin.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Kelvin.exe"; Description: "Start Kelvin"; Flags: nowait postinstall skipifsilent
; an update Kelvin installed itself (updater.py, /RELAUNCH=1): start it again afterwards
Filename: "{app}\Kelvin.exe"; Flags: nowait runasoriginaluser; Check: Relaunch

[Code]
function Relaunch: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

[UninstallRun]
; Kelvin's own data go (settings, keys, chats, memory, lab config, cards, instrument folders); measurements stay
Filename: "{app}\Kelvin.exe"; Parameters: "--remove-data"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveKelvinData"

[Messages]
ConfirmUninstall=Remove %1?%n%nKelvin's settings, API keys, ChatGPT sign-in, chats, memory, lab config, instrument cards and instrument folders are deleted too.%n%nYour measurement data are kept: the runs, the lab record and the reports.
UninstalledAll=%1 was removed.%n%nYour measurement data are kept in the Kelvin folder in your user folder (virtual-lab), and in any data folder you chose in Settings. "About this folder.txt" there explains what is in it.
