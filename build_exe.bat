@echo off
setlocal
cd /d "%~dp0"

REM Create venv if missing
if not exist .venv (
  py -3 -m venv .venv || goto :fail
)

.venv\Scripts\python -m pip install --upgrade pip || goto :fail
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller || goto :fail

REM Build the onefile exe from the spec (it bundles the web UI in dimmer\web and pywebview).
.venv\Scripts\python -m PyInstaller --noconfirm --clean AdaptiveScreenDimmer.spec || goto :fail

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
