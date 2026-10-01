# Adaptive Screen Dimmer (Windows)

Automatically dims your screen when content gets too bright (a white web page popping up in a
dark IDE, a flash in a game or video) to reduce eye strain and flash blindness. Lightweight,
click-through, multi-monitor, and designed never to flicker.

## Features
- 🛡️ **Flash protection without flicker**: darkens quickly as a soft ramp, brightens again slowly
  after a short hold, and ignores tiny changes, so strobing content does not make it pump.
- ⚡ **GPU capture, near-zero CPU**: Windows.Graphics.Capture delivers frames only when the screen
  changes; a D3D11 compute shader reduces each frame to exact per-tile sums, so no image data is
  copied to the CPU. A flash is seen within ~2 display frames. Automatic GDI fallback.
- 🎯 **Accurate measurement**: the overlays are excluded from capture, so they never measure
  themselves; brightness is the exact mean over every pixel (no aliasing on text while scrolling).
- 🔦 **Glare protection** ("Helle Flecken"): a small very bright area in a dark picture (a
  flashlight in a dark film scene) is detected by its contrast to the background. *normal/stark*
  dim the whole screen, *lokal* darkens **only the glaring area** with a soft GPU-drawn mask.
- 🖥️ **Any number of monitors**: pick them by checkbox with live brightness meters; monitor
  choice survives reboots and re-plugging; hot-plug and resolution changes are handled.
- ⏸ **Pause anywhere**: global hotkey **Ctrl+Alt+D**, tray icon menu, or the big button.
- 🎛️ **One profile**: a single set of values for all monitors, tuned live while you watch the
  meters.
- 🌅 **Blue-light filter** on every monitor via the display's gamma ramp: colour temperature and
  strength, optionally only at night between two times.
- 🌙 **Runs in the background**: tray icon (grey while paused), close-to-tray, start minimized.
- ⚙️ **Live settings**, saved automatically to `%APPDATA%\AdaptiveScreenDimmer\settings.json`.

## Quick Start
We do not ship prebuilt binaries. Python 3.10+ is required. The window uses the Edge WebView2
runtime that is part of Windows 11 (on Windows 10 it may need the free WebView2 runtime).

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
One window, top to bottom:

- **Header**: the big switch pauses and resumes (same as Ctrl+Alt+D), the line below shows the state.
- **Bildschirme**: per monitor a live meter. The bar is the current picture brightness (white
  marker), the amber line below shows where dimming starts and where it reaches full strength.
  Next to it the current dimming and filter in percent, and a switch to dim that monitor or not.
  "Nummern zeigen" flashes the number of each monitor on screen.
- **Abdunkeln**: Beginn, Volle Stärke ab, Stärkste Abdunkelung (capped at 94 %, never black),
  and how fast it gets darker and brighter again.
- **Helle Flecken**: Aus, Normal, Stark (whole screen) or Lokal (only the glaring area). With
  Lokal: Empfindlichkeit (how many times brighter than the surroundings a spot has to be),
  Stärke, Rand (Eng, Normal, Weit) and Ausblenden (how slowly the darkening disappears).
- **Blaulichtfilter**: on or off, colour temperature (lower is warmer), strength up to 60 %, and
  "Nur nachts" with two times.
- **Optionen**: Messrate (Sparsam 10/s, Normal 20/s, Schnell 30/s while the screen changes, half
  when it is still), hotkey, start paused, close to tray, start minimized. Below that the log.
- Every section has a "Standardwerte" button that resets only that section.

Every change applies at once and is saved automatically.
Command line: `--paused`, `--exit-after SEC` (quits automatically), `--verbose`.
Log file: `%APPDATA%\AdaptiveScreenDimmer\dimmer.log`.

## Limits
- Exclusive-fullscreen games (old DirectX titles) draw above every window; no overlay can cover
  them. Borderless/windowed fullscreen works.
- Protected screens (UAC prompt, lock screen) cannot be measured; the last state is kept.
- DRM-protected video is blanked in screen captures: brightness and glare there cannot be
  measured. Local files (VLC, mpv, …) are not affected.
- Local dimming lags the picture by about two display frames; fast-moving lights can briefly
  show an uncovered edge. It needs GPU capture (Windows 10 2004+ with a D3D11 GPU).
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
Architecture: `dimmer/logic.py` (pure measurement and smoothing), `profiles.py` (pure profile
and night time logic), `gpu.py` (D3D11 tile reduction), `wgc.py` (Windows.Graphics.Capture),
`localdim.py` (DirectComposition mask layer), `winapi.py` (monitors, GDI capture, apps per monitor), `overlay.py`, `engine.py` (one thread owns all windows), `gui.py` with the page in `dimmer/web`
(pywebview), `tray.py`, `app.py`. `tools\gui_snapshot.py out.png` saves a picture of the window.

## License
MIT License, see [LICENSE](LICENSE).
