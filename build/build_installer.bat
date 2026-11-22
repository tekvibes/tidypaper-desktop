@echo off
setlocal EnableExtensions
rem Build the app for Windows.  The name comes from brand.json (TidyPaper unless you change it):
rem   build\dist\TidyPaper\TidyPaper.exe        the app (one folder, no Python needed)
rem   build\dist\TidyPaper-portable.zip         that folder zipped - unzip anywhere and run TidyPaper.exe
rem   build\dist\TidyPaperSetup.exe             the installer (only if Inno Setup 6 is installed)
rem   build\dist\How to install TidyPaper.pdf   the guide to send with invitation links
rem
rem Double-click it, or run from a terminal:
rem   build\build_installer.bat [nopause] [name="Another Name"] [version=1.2.3]
rem name= / version= change this one build only; edit brand.json to change it for good.

set "BUILD=%~dp0"
set "ROOT=%~dp0.."
set "DIST=%BUILD%dist"
set "WORK=%BUILD%work"
set "PAUSE_AT_END=1"

:args
if "%~1"=="" goto :argsdone
set "ARG=%~1"
if /i "%ARG%"=="nopause" set "PAUSE_AT_END="
if /i "%ARG:~0,5%"=="name=" set "BRAND_NAME=%ARG:~5%"
if /i "%ARG:~0,8%"=="version=" set "BRAND_VERSION=%ARG:~8%"
rem cmd treats "=" as a separator: version=0.1.1 arrives as two arguments, "version" then "0.1.1"
if /i "%ARG%"=="name" (set "BRAND_NAME=%~2" & shift)
if /i "%ARG%"=="version" (set "BRAND_VERSION=%~2" & shift)
shift
goto :args
:argsdone
if defined BRAND_NAME set "BRAND_NAME=%BRAND_NAME:"=%"
if defined BRAND_VERSION set "BRAND_VERSION=%BRAND_VERSION:"=%"

rem ---- Python
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
    echo [x] Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again.
    goto :fail
)

rem ---- the name, from brand.json (+ name= / version= above)
for /f "delims=" %%N in ('%PY% "%ROOT%\brand.py" name') do set "APPNAME=%%N"
for /f "delims=" %%N in ('%PY% "%ROOT%\brand.py" stem') do set "STEM=%%N"
for /f "delims=" %%N in ('%PY% "%ROOT%\brand.py" version') do set "APPVER=%%N"
for /f "delims=" %%N in ('%PY% "%ROOT%\brand.py" publisher') do set "APPPUB=%%N"
if not defined STEM (
    echo [x] Couldn't read the app's name from brand.json - see the message above.
    goto :fail
)

echo ==== %APPNAME% %APPVER% build ====
echo.

rem ---- PyInstaller + the app's own libraries
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [1/6] Installing PyInstaller...
    %PY% -m pip install --upgrade pyinstaller || goto :fail
) else (
    echo [1/6] PyInstaller is installed.
)
rem (numpy: changing a word in a scan; winrt: Windows' own reader of the words in a scan)
%PY% -c "import webview, pymupdf, pyhanko, darkdetect, numpy, winrt.windows.media.ocr, winrt.windows.graphics.imaging, winrt.windows.storage.streams" >nul 2>nul
if errorlevel 1 (
    echo       Installing the app's libraries from requirements.txt...
    %PY% -m pip install -r "%ROOT%\requirements.txt" || goto :fail
)

rem ---- icon
if not exist "%BUILD%icon.ico" (
    echo [2/6] Making the app icon...
    %PY% "%BUILD%make_icon.py" || goto :fail
) else (
    echo [2/6] App icon found.
)

rem ---- bundle
echo [3/6] Bundling the app with PyInstaller (a minute or two)...
if exist "%DIST%\%STEM%" rmdir /s /q "%DIST%\%STEM%"
%PY% -m PyInstaller "%BUILD%toolkit.spec" --noconfirm --log-level WARN --workpath "%WORK%" --distpath "%DIST%" || goto :fail
if not exist "%DIST%\%STEM%\%STEM%.exe" (
    echo [x] PyInstaller finished but %STEM%.exe is missing.
    goto :fail
)

rem ---- portable zip
echo [4/6] Zipping the portable version...
if exist "%DIST%\%STEM%-portable.zip" del /q "%DIST%\%STEM%-portable.zip"
tar -a -c -f "%DIST%\%STEM%-portable.zip" -C "%DIST%" "%STEM%" >nul 2>nul
if not exist "%DIST%\%STEM%-portable.zip" (
    powershell -NoProfile -Command "Compress-Archive -Path '%DIST%\%STEM%' -DestinationPath '%DIST%\%STEM%-portable.zip' -Force" || goto :fail
)

rem ---- the install guide to send with invitations
echo [5/6] Making "How to install %APPNAME%.pdf"...
%PY% "%ROOT%\docs\make_install_guide.py" "%DIST%" || goto :fail

rem ---- installer (optional: needs Inno Setup 6)
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
    if not defined ISCC if exist %%P set "ISCC=%%~P"
)
if not defined ISCC for /f "delims=" %%I in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%I"

if defined ISCC (
    echo [6/6] Building the installer with Inno Setup...
    "%ISCC%" /Q "/DAppName=%APPNAME%" "/DAppStem=%STEM%" "/DAppVersion=%APPVER%" "/DAppPublisher=%APPPUB%" "%BUILD%installer.iss" || goto :fail
) else (
    echo [6/6] Inno Setup 6 not found - skipping %STEM%Setup.exe.
    echo       To build the installer too:  winget install -e --id JRSoftware.InnoSetup
    echo       then run this file again.
)

echo.
echo ==== Done ====
echo   App folder : %DIST%\%STEM%\%STEM%.exe
echo   Portable   : %DIST%\%STEM%-portable.zip
echo   Guide      : %DIST%\How to install %APPNAME%.pdf
if exist "%DIST%\%STEM%Setup.exe" echo   Installer  : %DIST%\%STEM%Setup.exe
echo.
if defined PAUSE_AT_END pause
exit /b 0

:fail
echo.
echo ==== Build failed - see the messages above ====
if defined PAUSE_AT_END pause
exit /b 1
