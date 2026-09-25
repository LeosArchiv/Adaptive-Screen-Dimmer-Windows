@echo off
setlocal
cd /d "%~dp0"

REM Create venv if missing
if not exist .venv (
  py -3 -m venv .venv || goto :fail
)

REM The GUI needs tkinter. Some Python installs ship without it (installer option "tcl/tk and IDLE").
.venv\Scripts\python -c "import tkinter" 2>nul
if errorlevel 1 (
  echo.
  echo FEHLER: Das Python in .venv hat kein tkinter.
  echo Loesung: Python-Installer starten, "Modify" waehlen, "tcl/tk and IDLE" ankreuzen,
  echo dann den Ordner .venv loeschen und dieses Skript erneut starten.
  exit /b 1
)

.venv\Scripts\python -m pip install --upgrade pip || goto :fail
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller || goto :fail

REM Build onefile GUI exe (mss is no longer needed; unused heavy modules are excluded)
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --noconsole --name AdaptiveScreenDimmer ^
  --exclude-module mss --exclude-module PIL --exclude-module matplotlib ^
  adaptive_dimmer.py || goto :fail

if not exist dist\AdaptiveScreenDimmer.exe goto :fail

REM Smoke test: start paused with a separate config folder and quit after 4 seconds.
set "ASD_CONFIG_DIR=%TEMP%\asd-build-check"
start "" /wait dist\AdaptiveScreenDimmer.exe --paused --exit-after 4
if errorlevel 1 (
  echo Smoke test failed, see %ASD_CONFIG_DIR%\dimmer.log
  exit /b 1
)
echo Build successful: dist\AdaptiveScreenDimmer.exe
endlocal
exit /b 0

:fail
echo Build failed.
exit /b 1
