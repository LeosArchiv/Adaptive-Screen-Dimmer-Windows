# Adaptive Screen Dimmer (Windows)

Automatically dims your screen when content gets too bright – a white web page popping up in a
dark IDE, a flash in a game or video – to reduce eye strain and flash blindness. Lightweight,
click-through, multi-monitor, and designed never to flicker.

## Features
- 🛡️ **Flash protection without flicker**: darkens quickly as a soft ramp, brightens again slowly
  after a short hold, and ignores tiny changes – strobing content does not make it pump.
- 🎯 **Accurate measurement**: the overlay is excluded from screen capture, so it never measures
  itself; brightness is the exact mean over every pixel (no aliasing on text while scrolling).
- ⚡ **Reacts to what you do**: a new window, focus change or page title change triggers an
  immediate measurement; while the screen is still, it measures less often to save CPU.
- 🖥️ **Any number of monitors**: pick them by checkbox with live brightness meters; monitor
  choice survives reboots and re-plugging; hot-plug and resolution changes are handled.
- ⏸ **Pause anywhere**: global hotkey **Ctrl+Alt+D**, tray icon menu, or the big button.
- 🚫 **Per-app exceptions**: never dim e.g. your photo editor – add the last used program with one click.
- 🌙 **Runs in the background**: tray icon (grey while paused), close-to-tray, start minimized.
- ⚙️ **Live settings**, saved automatically to `%APPDATA%\AdaptiveScreenDimmer\settings.json`.

## Quick Start
We do not ship prebuilt binaries. Python 3.10+ **with tkinter** is required (in the official
installer: "tcl/tk and IDLE").

### Option A: Build the EXE
```powershell
./build_exe.bat
```
Creates `dist\AdaptiveScreenDimmer.exe` (runs without Python) and smoke-tests it.

### Option B: Run from source
```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.\adaptive_dimmer_START.bat
```
No administrator rights are needed.

## Usage
| Setting | Meaning |
|---|---|
| Beginnt ab Helligkeit | average brightness (0–255) where dimming starts (yellow marker) |
| Volle Stärke ab Helligkeit | brightness where the strongest dimming is reached (red marker) |
| Stärkste Abdunkelung | how dark it gets at most (capped at 94 %, never black) |
| Abdunkeln bei Helligkeit | Sofort / Schnell / Sanft – how fast it darkens |
| Wieder aufhellen | Schnell / Normal / Langsam – how fast it brightens again |

"Bildschirme kennzeichnen" shows the number of each monitor on screen.
Command line: `--paused`, `--exit-after SEC` (quits automatically), `--verbose`.
Log file: `%APPDATA%\AdaptiveScreenDimmer\dimmer.log`.

## Limits
- Exclusive-fullscreen games (old DirectX titles) draw above every window; no overlay can cover
  them. Borderless/windowed fullscreen works.
- Protected screens (UAC prompt, lock screen) cannot be measured; the last state is kept.
- Windows 10 2004 or newer is needed to exclude the overlay from capture; older versions fall
  back to a mathematical compensation.

## Development
```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.\tools\check.ps1          # ruff, format, mypy, unit tests
.\tools\check.ps1 -Live    # + overlay lifecycle tests (real windows, never visibly dimming)
.\tools\check.ps1 -Build   # + EXE build with smoke test
.venv\Scripts\python tools\bench_live.py --monitor 0   # latency/CPU with a synthetic flash
```
Architecture: `dimmer/logic.py` (pure measurement and smoothing), `winapi.py` (monitors,
capture), `overlay.py`, `engine.py` (one thread owns all windows), `gui.py`, `tray.py`, `app.py`.

## License
MIT License — see [LICENSE](LICENSE).
