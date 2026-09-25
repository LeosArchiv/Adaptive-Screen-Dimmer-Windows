@echo off
cd /d "%~dp0"
REM Prefer packaged exe if available
if exist "dist\AdaptiveScreenDimmer.exe" (
	start "" "dist\AdaptiveScreenDimmer.exe"
) else (
	start "" .venv\Scripts\pythonw.exe adaptive_dimmer.py
)
exit
