; Windows installer for Degreaser (Inno Setup 7). Built by .github/workflows/release.yml:
;   iscc /DAppVersion=0.1.0 packaging\windows\degreaser.iss
; Installs per user (no administrator prompt), so WinSparkle updates install silently.

#ifndef AppVersion
  #error Pass /DAppVersion=x.y.z
#endif

[Setup]
; Never change AppId: it's how updates and uninstall find an existing installation.
AppId={{40853deb-33fb-470e-becf-03e7ca3b5455}
AppName=Degreaser
AppVersion={#AppVersion}
AppVerName=Degreaser {#AppVersion}
AppPublisher=Griffin Hurt
AppPublisherURL=https://github.com/griffhurt/degreaser
AppSupportURL=https://github.com/griffhurt/degreaser/issues
AppUpdatesURL=https://github.com/griffhurt/degreaser/releases
DefaultDirName={autopf}\Degreaser
DefaultGroupName=Degreaser
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Degreaser-{#AppVersion}-Windows-Setup
SetupIconFile=..\icons\degreaser.ico
UninstallDisplayIcon={app}\Degreaser.exe
UninstallDisplayName=Degreaser
LicenseFile=..\..\LICENSE
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An update replaces the whole app folder, so files dropped from a release don't linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\Degreaser\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Degreaser"; Filename: "{app}\Degreaser.exe"
Name: "{autodesktop}\Degreaser"; Filename: "{app}\Degreaser.exe"; Tasks: desktopicon

[Run]
; Interactive install: offer to start the app. Silent install (an update WinSparkle started):
; start it again automatically, since WinSparkle closed it first.
Filename: "{app}\Degreaser.exe"; Description: "{cm:LaunchProgram,Degreaser}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\Degreaser.exe"; Flags: nowait; Check: WizardSilent
