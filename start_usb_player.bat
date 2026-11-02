@echo off
setlocal
cd /d "%~dp0"
title DJI FPV Goggles V2 - USB Video Player

echo ==========================================
echo  DJI FPV Goggles V2 - USB Video Player
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Erstelle Virtual Environment ...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo FEHLER: Python wurde nicht gefunden.
        echo Bitte Python 3.10+ installieren: https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

echo [2/3] Pruefe Python-Pakete ...
".venv\Scripts\python.exe" -c "import usb.core, libusb_package, av, cv2, PIL" >nul 2>&1
if errorlevel 1 (
    echo       Installiere fehlende Pakete ...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
)

echo [3/3] Starte Player ...
echo.
echo Hinweis: Beim ersten Mal muss Interface 3 der Goggles auf
echo          WinUSB umgestellt werden (siehe diagnose_usb.bat).
echo.

".venv\Scripts\python.exe" dji_usb_player.py --autostart %*

if errorlevel 1 (
    echo.
    echo Der Player wurde mit einem Fehler beendet.
    echo Bitte diagnose_usb.bat ausfuehren.
    pause
)
endlocal
