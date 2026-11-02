@echo off
setlocal
cd /d "%~dp0"
title DJI FPV Goggles V2 - USB-Diagnose

echo ==========================================
echo  DJI FPV Goggles V2 - USB-Diagnose
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Erstelle Virtual Environment ...
    python -m venv .venv
)

".venv\Scripts\python.exe" -c "import usb.core, libusb_package" >nul 2>&1
if errorlevel 1 (
    echo Installiere fehlende Pakete ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyusb libusb-package av
)

echo.
echo Tipp: Fuer einen echten Video-Test:
echo    diagnose_usb.bat --test --seconds 5
echo.

".venv\Scripts\python.exe" diagnose_usb.py %*

echo.
pause
endlocal
