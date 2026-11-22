@echo off
reg delete "HKCU\Software\Classes\*\shell\Toolkit" /f >nul 2>nul
reg delete "HKCU\Software\Classes\Directory\shell\Toolkit" /f >nul 2>nul
reg delete "HKCU\Software\Classes\Directory\Background\shell\Toolkit" /f >nul 2>nul
reg delete "HKCU\Software\Classes\SystemFileAssociations\.pdf\shell\ToolkitPDF" /f >nul 2>nul
del "%APPDATA%\Microsoft\Windows\SendTo\Toolkit.lnk" >nul 2>nul
echo Removed Toolkit from the right-click and Send to menus.
pause
