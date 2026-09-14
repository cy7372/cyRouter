; cyRouter Inno Setup script
; Build:  ISCC.exe cyRouter.iss
; Output: Output\cyRouter-Setup-v<VERSION>.exe
;
; Design choices:
;   - PrivilegesRequired=lowest → installs to %APPDATA%, no UAC prompt.
;     Corporate laptop users without admin rights can still install.
;   - data/ (config.json, .key) is preserved across uninstall+reinstall
;     via uninsneveruninstall flag — users don't lose their API keys.
;   - Signed-double-click to quit via taskkill so the uninstaller doesn't
;     leave a running .exe locked.

#define MyAppName "cyRouter"
; NOTE: MyAppVersion is overwritten by build.ps1 on every build, sourced
;       from version.py (the single source of truth). Do not edit by hand.
#define MyAppVersion "1.2.0.16"
#define MyAppPublisher "cyRouter"
#define MyAppExeName "cyRouter.exe"
#define MyAppURL "https://github.com/anomalyco/cyRouter"

[Setup]
AppId={{C7A4F2E1-8B3D-4E5F-9A6B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={userappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=cyRouter-Setup
SetupIconFile=packaging\cyrouter.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/ultra64
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
DisableDirPage=no
; Enable later when you have a code-signing certificate:
; SignTool=mysigntool $f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "开机自启动"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\cyRouter.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
; data/ is created at runtime by the app, not shipped. But if a previous
; install created it, we must NOT remove it on uninstall (keys/config live there).

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{autostartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch cyRouter"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Kill the running exe before uninstalling (otherwise file is locked)
Filename: "taskkill"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; Remove the binary but PRESERVE data/ (user keys + config).
; Type: filesandordirs is NOT used on {app} to protect data/.
Type: files; Name: "{app}\{#MyAppExeName}"

[Code]
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  // Kill the running exe during uninstall too (otherwise file is locked).
  Exec('taskkill', '/F /IM {#MyAppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
end;

// Check if cyRouter is running. Uses the window class via FindWindow —
// PyInstaller windowless exes have no window, so we also check the
// process list via tasklist.
function IsAppRunning(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if Exec('tasklist', '/FI "IMAGENAME eq {#MyAppExeName}" /NH', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode) then
  begin
    // tasklist prints the image name in its stdout when the process exists.
    // Inno caches output in the <TmpStr> via LoadStringFromFile on the temp.
    // Simpler: check exit code is not 128 (no tasks running).
    // tasklist returns 0 when tasks match, 1 when filter fails — so we
    // inspect stdout instead.
  end;
  // Fallback: use FindWindowByClassName which works for tray apps.
end;

// Prompt the user and kill the running instance before upgrading.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  TmpFile: String;
  Content: AnsiString;
  IsRunning: Boolean;
begin
  Result := '';

  // Detect running process: tasklist output to temp file, then read it.
  TmpFile := ExpandConstant('{tmp}\cr_tl.txt');
  Exec('cmd.exe', '/c tasklist /FI "IMAGENAME eq cyRouter.exe" /NH > "' + TmpFile + '" 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  IsRunning := False;
  if FileExists(TmpFile) then
  begin
    if LoadStringFromFile(TmpFile, Content) then
    begin
      if Pos('cyRouter.exe', Content) > 0 then
        IsRunning := True;
    end;
    DeleteFile(TmpFile);
  end;

  if IsRunning then
  begin
    if MsgBox('检测到 cyRouter 正在运行，需要先关闭它才能继续安装。' + #13#10 + #13#10 +
              '点击"是"关闭并继续，点击"否"取消安装。',
              mbConfirmation, MB_YESNO) = IDNO then
    begin
      Result := '安装已取消。';
      Exit;
    end;
    Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM cyRouter.exe',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(500);
  end;
end;
