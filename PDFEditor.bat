@echo off
setlocal
rem PDF Editor launcher. Double-click, or drag a PDF onto this file. Needs: py -m pip install pymupdf

set "HERE=%~dp0"
set "LOG=%TEMP%\PDFEditor_error.log"
set "PY="
py -3 -c "import tkinter" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import tkinter" >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python 3 with tkinter was not found. Run setup.bat first.
    pause
    exit /b 1
)

%PY% "%HERE%pdf_editor.py" %* 2>"%LOG%"
if errorlevel 1 (
    echo.
    echo PDF Editor closed with an error:
    echo ------------------------------------------------------------
    type "%LOG%"
    echo ------------------------------------------------------------
    echo Log saved at: %LOG%
    echo.
    pause
)
