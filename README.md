# Adaptive Screen Dimmer (Windows)

Automatically dims your screen when content gets too bright – a white web page popping up in a
dark IDE, a flash in a game or video – to reduce eye strain and flash blindness. Lightweight,
click-through, multi-monitor, and designed never to flicker.

## Features
- 🛡️ **Flash protection without flicker**: darkens quickly as a soft ramp, brightens again slowly
  after a short hold, and ignores tiny changes – strobing content does not make it pump.
- ⚡ **GPU capture, near-zero CPU**: Windows.Graphics.Capture delivers frames only when the screen
  changes; a D3D11 compute shader reduces each frame to exact per-tile sums, so no image data is
  copied to the CPU. A flash is seen within ~2 display frames. Automatic GDI fallback.
- 🎯 **Accurate measurement**: the overlays are excluded from capture, so they never measure
  themselves; brightness is the exact mean over every pixel (no aliasing on text while scrolling).
- 🔦 **Glare protection** ("Helle Flecken"): a small very bright area in a dark picture (a
  flashlight in a dark film scene) is detected by its contrast to the background. *normal/stark*
  dim the whole screen, *lokal* darkens **only the glaring area** with a soft GPU-drawn mask.
- 🔒 **Protected video**: DRM video (Netflix & co. in a browser) is black in every screen capture;
  when a monitor stays exactly black, a per-profile fixed dimming value can apply.
- 🖥️ **Any number of monitors**: pick them by checkbox with live brightness meters; monitor
  choice survives reboots and re-plugging; hot-plug and resolution changes are handled.
- ⏸ **Pause anywhere**: global hotkey **Ctrl+Alt+D**, tray icon menu, or the big button.
- 🎛️ **Profiles per monitor**: Tag, Nacht, Arbeit, Zocken, Filme (editable, add your own). Each
  monitor has a base profile – fixed, or automatic day/night by a schedule with a smooth transition.
- 🎮 **App profiles**: when e.g. `DDNet.exe` is in front on a monitor, its profile applies on *that*
  monitor only; the other monitors keep theirs. Profile "Aus" never dims (for a photo editor etc.).
- 🧩 **Mixes**: a profile can take the dimming or the blue-light filter over from the base profile,
  so "Zocken" at night automatically becomes "Zocken + Nacht".
- 🌅 **Blue-light filter** on every monitor (also where Windows Night Light does not work): a warm
  overlay with colour temperature and strength, fading in and out gently.
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
Tabs: **Übersicht** (monitors, base profile, what is active), **Profile** (editor),
**Programme** (app → profile rules; the list suggests apps recently in front),
**Zeitplan** (day/night times and transition), **Optionen**.

Profile settings (each group: *eigene Werte* / *vom Grundprofil übernehmen* / *aus*):

| Setting | Meaning |
|---|---|
| Beginnt ab Helligkeit | average brightness (0–255) where dimming starts (yellow marker) |
| Volle Stärke ab Helligkeit | brightness where the strongest dimming is reached (red marker) |
| Stärkste Abdunkelung | how dark it gets at most (capped at 94 %, never black) |
| Abdunkeln bei Helligkeit | Sofort / Schnell / Sanft – how fast it darkens |
| Wieder aufhellen | Schnell / Normal / Langsam – how fast it brightens again |
| Helle Flecken | aus / normal / stark (whole screen) / lokal (only the glaring area) |
| Geschütztes Video | fixed dimming while the picture cannot be measured (DRM video), off by default |
| Farbtemperatur / Stärke | blue-light filter: lower kelvin = warmer; strength up to 60 % |
| Messrate (Optionen) | Sparsam 10/s · Normal 20/s · Schnell 30/s – measurements while the screen changes (half the rate when still); higher = faster flash protection, more CPU |

"Bildschirme kennzeichnen" shows the number of each monitor on screen.
Command line: `--paused`, `--exit-after SEC` (quits automatically), `--verbose`.
Log file: `%APPDATA%\AdaptiveScreenDimmer\dimmer.log`.

## Limits
- Exclusive-fullscreen games (old DirectX titles) draw above every window; no overlay can cover
  them. Borderless/windowed fullscreen works.
- Protected screens (UAC prompt, lock screen) cannot be measured; the last state is kept.
- DRM-protected video is blanked in screen captures: brightness and glare there cannot be
  measured (see "Geschütztes Video"). Local files (VLC, mpv, …) are not affected.
- Local dimming lags the picture by about two display frames; fast-moving lights can briefly
  show an uncovered edge. It needs GPU capture (Windows 10 2004+ with a D3D11 GPU).
- The blue-light filter is an overlay, not a change of the display's colour pipeline: warm, but
  blacks get slightly lifted at high strength. It works on every monitor and never stays behind.
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
Architecture: `dimmer/logic.py` (pure measurement and smoothing), `profiles.py` (pure profile,
rule and schedule resolution), `gpu.py` (D3D11 tile reduction), `wgc.py` (Windows.Graphics.Capture),
`localdim.py` (DirectComposition mask layer), `winapi.py` (monitors, GDI capture, apps per monitor), `overlay.py`, `engine.py` (one thread owns all windows), `gui.py`, `tray.py`, `app.py`.

## License
MIT License — see [LICENSE](LICENSE).
