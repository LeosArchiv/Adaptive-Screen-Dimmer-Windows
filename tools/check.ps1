# Quality gate: lint, format check, types, tests. Add -Live for the overlay lifecycle tests
# (real windows, never visibly dimming) and -Build for the EXE build incl. smoke test.
param([switch]$Live, [switch]$Build)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"

function Step($name, [scriptblock]$cmd) {
    Write-Host "== $name" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$name failed" }
}

Step "ruff check" { & $py -m ruff check dimmer tests tools adaptive_dimmer.py }
Step "ruff format" { & $py -m ruff format --check dimmer tests tools adaptive_dimmer.py }
Step "mypy" { & $py -m mypy }
if ($Live) { $env:ASD_LIVE_TESTS = "1" }
Step "pytest" { & $py -m pytest -q }
if ($Build) { Step "build" { cmd /c "$PWD\build_exe.bat" } }
Write-Host "All checks passed." -ForegroundColor Green
