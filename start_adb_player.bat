@echo off
REM DJI FPV Goggles V2 - ADB Video Player Launcher
echo Starting DJI FPV Goggles V2 ADB Video Player...
echo.

REM Prüfen ob venv existiert
if not exist ".venv" (
    echo Virtual Environment nicht gefunden. Erstelle...
    python -m venv .venv
    echo Installiere Dependencies...
    .venv\Scripts\pip install -r requirements.txt
    echo.
)

REM Prüfen ob ADB verfügbar ist
echo Pruefe ADB...
adb version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ⚠ ADB nicht gefunden!
    echo Bitte Android Platform Tools installieren:
    echo https://developer.android.com/studio/releases/platform-tools
    echo.
    echo Oder via winget: winget install Google.PlatformTools
    echo.
    pause
    exit /b 1
)

echo ADB OK - Starte Player...
echo.

.venv\Scripts\python dji_goggles_adb_player.py

pause