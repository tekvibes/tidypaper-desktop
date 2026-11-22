@echo off
setlocal
rem Creates "Toolkit" in your Start Menu (current user). Launches without a console window.
rem Also creates a Desktop shortcut if you answer Y.

set "HERE=%~dp0"
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Toolkit.lnk"
set "ICON=%SystemRoot%\System32\shell32.dll,-16773"

rem Prefer the windowless Python launcher so no console appears.
set "TARGET="
for /f "delims=" %%P in ('where pyw 2^>nul') do if not defined TARGET set "TARGET=%%P"
if not defined TARGET for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined TARGET set "TARGET=%%P"

if defined TARGET (
    set "ARGS=-3 ""%HERE%toolkit.py"""
    echo %TARGET% | find /i "pythonw" >nul && set "ARGS=""%HERE%toolkit.py"""
) else (
    set "TARGET=%HERE%Toolkit.bat"
    set "ARGS="
)

powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
  "$s.TargetPath='%TARGET%'; $s.Arguments='%ARGS%'; $s.WorkingDirectory='%HERE%';" ^
  "$s.IconLocation='%ICON%'; $s.Description='Everyday file utilities'; $s.WindowStyle=7; $s.Save()"

if exist "%LNK%" (
    echo Added "Toolkit" to the Start Menu. Press Start and type Toolkit.
    echo To pin it: Start - type Toolkit - right-click it - "Pin to Start" or "Pin to taskbar".
) else (
    echo Could not create the shortcut. & pause & exit /b 1
)

echo.
choice /c YN /m "Also put a shortcut on the Desktop"
if not errorlevel 2 copy /y "%LNK%" "%USERPROFILE%\Desktop\Toolkit.lnk" >nul && echo Desktop shortcut created.
echo.
pause
