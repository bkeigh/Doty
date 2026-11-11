; Inno Setup script for DotySetup-<version>.exe.
; Don't compile this directly; use:  .venv\Scripts\python packaging\build.py
; build.py passes /DAppVersion=... and /DIconFile=... and makes the wizard art in packaging\build.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise upgrades of the same app.
AppId={{8F178F8D-DE35-40EF-8148-64F79A299B6D}
AppName=Doty
AppVersion={#AppVersion}
AppVerName=Doty {#AppVersion}
AppPublisher=Doty
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Doty: live Dota 2 coach (setup)
; Per-user install: no admin prompt, and Doty can update its own files.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Doty
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
OutputDir=..\dist
OutputBaseFilename=DotySetup-{#AppVersion}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Doty.exe
UninstallDisplayName=Doty
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardImageFile=build\wizard.bmp,build\wizard@2x.bmp
WizardSmallImageFile=build\wizard-small.bmp,build\wizard-small@2x.bmp
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Doty holds this mutex while open, so setup asks you to close it instead of failing.
AppMutex=DotyCoachRunning
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Doty\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The GPL asks that the license travels with the program (it also covers the bundled eSpeak NG).
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[InstallDelete]
; An upgrade replaces the bundled libraries wholesale (no stale DLLs from an older version).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
; Same AppUserModelID as the running window, so "Pin to taskbar" pins this shortcut.
Name: "{autoprograms}\Doty"; Filename: "{app}\Doty.exe"; WorkingDir: "{app}"; Comment: "Doty: live Dota 2 coach"; AppUserModelID: "Doty.DotaCoach"
Name: "{autodesktop}\Doty"; Filename: "{app}\Doty.exe"; WorkingDir: "{app}"; Comment: "Doty: live Dota 2 coach"; AppUserModelID: "Doty.DotaCoach"; Tasks: desktopicon

[Run]
Filename: "{app}\Doty.exe"; Description: "{cm:LaunchProgram,Doty}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Take Doty's Game State Integration file back out of Dota's folder.
Filename: "{app}\Doty.exe"; Parameters: "--uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "DotyGsiCleanup"
