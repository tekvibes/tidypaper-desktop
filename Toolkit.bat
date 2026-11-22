@echo off
setlocal
rem Toolkit launcher. Double-click to open, or drag files/folders onto this .bat.
rem First time: run setup.bat.

set "HERE=%~dp0"
set "LOG=%TEMP%\Toolkit_error.log"
set "PY="

py -3 -c "import tkinter" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import tkinter" >nul 2>nul && set "PY=python"

if not defined PY (
    echo Could not find Python 3 with tkinter. Run setup.bat first, or install Python from
    echo https://www.python.org/downloads/  - tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

%PY% "%HERE%toolkit.py" %* 2>"%LOG%"
if errorlevel 1 (
    echo.
    echo Toolkit closed with an error:
    echo ------------------------------------------------------------
    type "%LOG%"
    echo ------------------------------------------------------------
    echo Log saved at: %LOG%
    echo.
    pause
)
