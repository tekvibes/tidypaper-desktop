@echo off
setlocal
rem Adds "Open in Toolkit" to the right-click menu for every file and folder (current user only).
rem Toolkit opens with the file loaded and the most relevant tool pre-selected.
rem Also adds direct "Convert to PDF" for Office files and "Merge PDFs" for .pdf files.
rem Re-run this if you move the Toolkit folder.

set "BAT=%~dp0Toolkit.bat"
if not exist "%BAT%" (echo Toolkit.bat not found next to this installer. & pause & exit /b 1)
set "ICON=shell32.dll,-16773"

reg add "HKCU\Software\Classes\*\shell\Toolkit" /ve /d "Open in Toolkit" /f >nul
reg add "HKCU\Software\Classes\*\shell\Toolkit" /v Icon /d "%ICON%" /f >nul
reg add "HKCU\Software\Classes\*\shell\Toolkit\command" /ve /d "\"%BAT%\" \"%%1\"" /f >nul

reg add "HKCU\Software\Classes\Directory\shell\Toolkit" /ve /d "Open in Toolkit" /f >nul
reg add "HKCU\Software\Classes\Directory\shell\Toolkit" /v Icon /d "%ICON%" /f >nul
reg add "HKCU\Software\Classes\Directory\shell\Toolkit\command" /ve /d "\"%BAT%\" \"%%1\"" /f >nul

reg add "HKCU\Software\Classes\Directory\Background\shell\Toolkit" /ve /d "Open Toolkit here" /f >nul
reg add "HKCU\Software\Classes\Directory\Background\shell\Toolkit" /v Icon /d "%ICON%" /f >nul
reg add "HKCU\Software\Classes\Directory\Background\shell\Toolkit\command" /ve /d "\"%BAT%\" \"%%V\"" /f >nul

rem "Edit PDF" directly on .pdf files
set "PDFBAT=%~dp0PDFEditor.bat"
reg add "HKCU\Software\Classes\SystemFileAssociations\.pdf\shell\ToolkitPDF" /ve /d "Edit PDF" /f >nul
reg add "HKCU\Software\Classes\SystemFileAssociations\.pdf\shell\ToolkitPDF" /v Icon /d "shell32.dll,-16826" /f >nul
reg add "HKCU\Software\Classes\SystemFileAssociations\.pdf\shell\ToolkitPDF\command" /ve /d "\"%PDFBAT%\" \"%%1\"" /f >nul

rem "Send to" shortcut so multi-select hands ALL files to one window
set "SENDTO=%APPDATA%\Microsoft\Windows\SendTo"
if exist "%SENDTO%" (
    powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SENDTO%\Toolkit.lnk'); $s.TargetPath='%BAT%'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%ICON%'; $s.Save()" >nul 2>nul
)

echo.
echo Done.  Right-click any file or folder -> "Open in Toolkit".  Right-click a PDF -> "Edit PDF".
echo For several files at once: select them -> right-click -> Send to -> Toolkit.
echo On Windows 11 these are under "Show more options" (or press Shift+F10).
echo.
pause
