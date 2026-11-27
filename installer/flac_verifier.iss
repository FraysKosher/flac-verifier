; FLAC VERIFIER — Per-User Windows Installer (Inno Setup 6)
; Zero administrator privileges required: installs into {localappdata}\Programs\FLAC Verifier
; Preserves %APPDATA%\FLAC Verifier\library.db across upgrades and silent auto-updates.

#ifndef MyAppVersion
  #define MyAppVersion "3.0.0"
#endif

#define MyAppName "FLAC Verifier"
#define MyAppPublisher "FLAC VERIFIER Contributors"
#define MyAppURL "https://github.com/FraysKosher/flac-verifier"
#define MyAppExeName "FLAC_Verifier.exe"

[Setup]
AppId={{8F9E7A2B-4C5D-4E6F-9A1B-3C2D1E0F4A5B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=..\LICENSE
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=FLAC_Verifier_Setup_{#MyAppVersion}-win64
SetupIconFile=..\Logo\icono_app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\FLAC_Verifier\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "portable.flag"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Interactive install: offer to launch the app on finish
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
; Silent 1-click auto-update: automatically relaunch the updated application
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    DataDir := ExpandConstant('{userappdata}\FLAC Verifier');
    if DirExists(DataDir) then
    begin
      if MsgBox(
        'Do you also want to remove your FLAC Verifier library database, cached album covers, and settings?' + #13#10 + #13#10 +
        'Click No to keep your indexed library (' + DataDir + ') for future installations.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2
      ) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
