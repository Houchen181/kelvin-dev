; Windows installer for Kelvin (Inno Setup 6). Built by .github/workflows/release.yml:
;   ISCC.exe /DAppVersion=0.1.0 packaging\kelvin.iss
; Installs per user (no admin rights) into %LOCALAPPDATA%\Programs\Kelvin.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C0F6E5A-3B7D-4C1E-9A2F-6D5E4B3A2C10}
AppName=Kelvin
AppVersion={#AppVersion}
AppVerName=Kelvin {#AppVersion}
AppPublisher=Kelvin project
AppComments=The coolest cat in the lab
DefaultDirName={localappdata}\Programs\Kelvin
DefaultGroupName=Kelvin
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Kelvin-Setup-{#AppVersion}
SetupIconFile=icons\kelvin.ico
UninstallDisplayIcon={app}\Kelvin.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Kelvin\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Kelvin"; Filename: "{app}\Kelvin.exe"
Name: "{autodesktop}\Kelvin"; Filename: "{app}\Kelvin.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Kelvin.exe"; Description: "Start Kelvin"; Flags: nowait postinstall skipifsilent
