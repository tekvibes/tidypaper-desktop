@echo off
setlocal
rem One-time setup for Toolkit: installs the Python libraries, then offers the optional programs.

echo ==== Toolkit setup ====
echo.

set "PY="
py -3 -c "import tkinter" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import tkinter" >nul 2>nul && set "PY=python"
if not defined PY goto :nopython

echo [1/2] Installing Python libraries: pypdf, pillow, openpyxl, reportlab, pillow-heif, pymupdf
%PY% -m pip install --user --upgrade pypdf pillow openpyxl reportlab pillow-heif pymupdf pyhanko cryptography
if errorlevel 1 goto :pipfail
echo.
echo [2/2] Optional programs - each adds a few tools.
echo.

set "HAVE_LO="
where soffice >nul 2>nul && set "HAVE_LO=1"
if exist "%ProgramFiles%\LibreOffice\program\soffice.exe" set "HAVE_LO=1"
if defined HAVE_LO (
    echo   LibreOffice: already installed.
) else (
    choice /c YN /m "  LibreOffice - Office to PDF, PDF to Word - install"
    if not errorlevel 2 winget install -e --id TheDocumentFoundation.LibreOffice --accept-package-agreements --accept-source-agreements
)

set "HAVE_TESS="
where tesseract >nul 2>nul && set "HAVE_TESS=1"
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "HAVE_TESS=1"
if defined HAVE_TESS (
    echo   Tesseract: already installed.
) else (
    choice /c YN /m "  Tesseract - OCR, text from scans and images - install"
    if not errorlevel 2 winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
)

set "HAVE_FF="
where ffmpeg >nul 2>nul && set "HAVE_FF=1"
if defined HAVE_FF (
    echo   ffmpeg: already installed.
) else (
    choice /c YN /m "  ffmpeg - video and audio convert and trim - install"
    if not errorlevel 2 winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
)

echo.
echo Setup finished. If you installed any optional program, open a new window before using it.
echo Now double-click Toolkit.bat. Run install_context_menu.bat for the right-click entry.
echo.
pause
exit /b 0

:nopython
echo Python 3 with tkinter was not found.
choice /c YN /m "Install Python now with winget"
if errorlevel 2 (
    echo Install Python from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then run setup.bat again.
    pause
    exit /b 1
)
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Python installed. Close this window, open a NEW one, and run setup.bat again.
pause
exit /b 0

:pipfail
echo.
echo pip failed - check your internet connection and try again.
pause
exit /b 1
