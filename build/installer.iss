; Inno Setup 6 script.  build_installer.bat runs it after PyInstaller and passes the name from brand.json:
;   ISCC.exe /DAppName="TidyPaper" /DAppStem="TidyPaper" /DAppVersion="0.1.0" build\installer.iss
;     ->  build\dist\TidyPaperSetup.exe          (without /D... the defaults below are used)
;
; Per-user install (no admin prompt), Start Menu + optional Desktop shortcut, right-click entries,
; and optional free add-ons installed with winget after the main install.

#ifndef AppName
  #define AppName "TidyPaper"
#endif
#ifndef AppStem
  #define AppStem AppName
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef AppPublisher
  #define AppPublisher AppName
#endif
#define AppExe AppStem + ".exe"
#define SrcDir "dist\" + AppStem

[Setup]
; The AppId never changes, whatever the name: a renamed build upgrades the copy that's already installed.
AppId={{6F1C2B7A-3E44-4D0C-9A57-7C1B0E5D2F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppStem}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousGroup=no
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename={#AppStem}Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a {#AppName} shortcut on the desktop"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Edit with {#AppName}"" and ""Open in {#AppName}"" to the right-click menu"; GroupDescription: "Shortcuts:"
Name: "addon_libreoffice"; Description: "LibreOffice - open and convert Word, Excel and PowerPoint files when Microsoft Office isn't installed (about 350 MB)"; GroupDescription: "Free add-ons (downloaded now, can be added later from the app):"; Flags: unchecked
Name: "addon_tesseract"; Description: "Tesseract OCR - pull text out of scans and photos (about 80 MB)"; GroupDescription: "Free add-ons (downloaded now, can be added later from the app):"; Flags: unchecked
Name: "addon_ffmpeg"; Description: "FFmpeg - convert and trim video and audio (about 100 MB)"; GroupDescription: "Free add-ons (downloaded now, can be added later from the app):"; Flags: unchecked

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

#if AppStem != "Toolkit"
[InstallDelete]
; The app used to be called Toolkit: remove its old program file and shortcuts.  Saved signatures, details and
; settings in ~\.toolkit are kept - the new name uses the same folder.
Type: files; Name: "{app}\Toolkit.exe"
Type: files; Name: "{userprograms}\Toolkit\Toolkit.lnk"
Type: files; Name: "{userprograms}\Toolkit\Uninstall Toolkit.lnk"
Type: dirifempty; Name: "{userprograms}\Toolkit"
Type: files; Name: "{userdesktop}\Toolkit.lnk"
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Right-click a PDF -> "Edit with <app>".  The key names (Toolkit.Edit / Toolkit.Open) stay fixed whatever the app
; is called, so a renamed build replaces the old entries instead of adding a second set.
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Toolkit.Edit"; ValueType: string; ValueName: ""; ValueData: "Edit with {#AppName}"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Toolkit.Edit"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\Toolkit.Edit\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu
; Right-click any file -> "Open in <app>" (documents open in the editor; for other files the home screen suggests a task)
Root: HKCU; Subkey: "Software\Classes\*\shell\Toolkit.Open"; ValueType: string; ValueName: ""; ValueData: "Open in {#AppName}"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\Toolkit.Open"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\{#AppExe}"",0"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\*\shell\Toolkit.Open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
; a self-installing update (updater.py runs Setup /SILENT ... /UPDATE=1 after the app quits) opens the app again
; when it was "Restart now" (/RELAUNCH=1); closing the app just installs
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WantRelaunch

[Code]
var
  AddonNote: String;

// Setup was started by the app's own update (see updater.py): no add-ons; /RELAUNCH=1 opens the app at the end
function IsAutoUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

function WantRelaunch(): Boolean;
begin
  Result := IsAutoUpdate() and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

function WingetPath(): String;
begin
  Result := ExpandConstant('{localappdata}\Microsoft\WindowsApps\winget.exe');
  if not FileExists(Result) then
    Result := '';
end;

procedure InstallAddon(const Title, Id: String);
var
  Winget: String;
  Code: Integer;
begin
  Winget := WingetPath();
  if Winget = '' then
  begin
    AddonNote := AddonNote + #13#10 + '- ' + Title + ': not installed (Windows Package Manager is missing). Add it later from the app.';
    Exit;
  end;
  WizardForm.StatusLabel.Caption := 'Installing ' + Title + ' (this can take a few minutes)...';
  if Exec(Winget, 'install -e --id ' + Id + ' --accept-package-agreements --accept-source-agreements --silent',
          '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0) then
    AddonNote := AddonNote + #13#10 + '- ' + Title + ': installed.'
  else
    AddonNote := AddonNote + #13#10 + '- ' + Title + ': could not be installed now (code ' + IntToStr(Code) + '). Add it later from the app.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not IsAutoUpdate() then
  begin
    AddonNote := '';
    if WizardIsTaskSelected('addon_libreoffice') then InstallAddon('LibreOffice', 'TheDocumentFoundation.LibreOffice');
    if WizardIsTaskSelected('addon_tesseract') then InstallAddon('Tesseract OCR', 'UB-Mannheim.TesseractOCR');
    if WizardIsTaskSelected('addon_ffmpeg') then InstallAddon('FFmpeg', 'Gyan.FFmpeg');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (AddonNote <> '') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + 'Add-ons:' + AddonNote;
end;
