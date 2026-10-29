; PICKEMUP — Inno Setup 6 installer definition
; Build with scripts\build-installer.bat (stages a runtime-free copy, then compiles this file).

#define MyAppName "PICKEMUP"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "the PICKEMUP contributors"
#define MyAppURL "https://github.com/<you>/pickemup"

[Setup]
AppId={{7B4F0E5A-2C1D-4E9F-9A6B-PICKEMUP0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PICKEMUP
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=PICKEMUP-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\PICKEMUP_PORTABLE.bat
CloseApplications=force

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut (double-click PICKEMUP to run)"; GroupDescription: "Additional icons:"

[Files]
; Source is the staging folder produced by build-installer.bat (no .venv,
; no node_modules, no .next, no data, no .env — first run builds those in-place)
Source: "stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PICKEMUP"; Filename: "{app}\PICKEMUP_PORTABLE.bat"; IconFilename: "{sys}\shell32.dll"; IconIndex: 176; WorkingDir: "{app}"; Comment: "Start PICKEMUP (API + console + browser)"
Name: "{group}\Uninstall PICKEMUP"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PICKEMUP"; Filename: "{app}\PICKEMUP_PORTABLE.bat"; IconFilename: "{sys}\shell32.dll"; IconIndex: 176; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\PICKEMUP_PORTABLE.bat"; Description: "Launch PICKEMUP now"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; developer artifacts created at runtime inside the app folder
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\apps\web\node_modules"
Type: filesandordirs; Name: "{app}\apps\web\.next"
Type: filesandordirs; Name: "{app}\.npmcache"
Type: filesandordirs; Name: "{app}\.pipcache"
Type: filesandordirs; Name: "{app}\.uvcache"

[Code]
// The operator's collected corpus and credentials are EVIDENCE -
// uninstalling the program must never delete them. Ask, then preserve.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Answer: Integer;
  Silent: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    // WizardSilent is unavailable in uninstall mode; inspect the command tail
    Silent := (Pos('/SILENT', GetCmdTail) > 0) or (Pos('/VERYSILENT', GetCmdTail) > 0);
    if Silent then
      Log('silent uninstall: preserving data\ and .env by default')
    else
    begin
      Answer := MsgBox(
        'Keep collected data (data\pickemup.db) and your .env settings?' + #13#10 +
        'Choosing No deletes them too.', mbConfirmation, MB_YESNO or MB_DEFBUTTON1);
      if Answer = IDNO then
      begin
        DelTree(ExpandConstant('{app}\data'), True, True, True);
        DeleteFile(ExpandConstant('{app}\.env'));
      end
      else
        Log('uninstall: preserving data\ and .env by operator choice');
    end;
  end;
end;
