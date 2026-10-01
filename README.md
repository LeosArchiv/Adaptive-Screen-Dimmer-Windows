# Adaptive Screen Dimmer (Windows)

**English** | [Deutsch](README.de.md)

Dims your screen automatically as soon as it gets too bright: a white web page in a dark room, a
flash in a game, a flashlight in a dark film scene. Easy on the eyes, no glare. Runs quietly in
the background, on any number of monitors, and never flickers.

## Features

- **Dimming without flicker.** When the picture gets bright, it darkens quickly and smoothly. It
  only gets brighter again after a short pause, and slowly. Small changes are ignored, so nothing
  pumps.
- **Bright spots.** A small, very bright area in a dark picture is detected by its contrast to the
  surroundings. With "Local" only that area is darkened and the rest of the picture stays as it is.
- **Blue light filter.** Makes white warmer, on external monitors too. Black stays black, because
  the filter changes the monitor's colour curve instead of laying a coloured layer over the
  picture. Optionally only at night.
- **Light on resources.** Brightness is measured on the graphics card, and only when the picture
  changes. At idle the app uses about 1 % CPU.
- **One profile.** Every slider applies at once, so you tune everything while looking at the
  picture.
- **Pause any time** with Ctrl+Alt+D, from the tray icon or with the switch in the window.
- **English and German.** Follows the Windows display language, switchable in the options.

## Getting started

The easy way: download `AdaptiveScreenDimmer.exe` from
[Releases](https://github.com/LeosArchiv/Adaptive-Screen-Dimmer-Windows/releases/latest) and run
it. No installation, no Python, no administrator rights. The window uses WebView2, which is part
of Windows 11 (on Windows 10 you can get the WebView2 Runtime from Microsoft for free). The EXE is
not signed, so Windows SmartScreen may warn on the first start: click "More info", then
"Run anyway".

Build it yourself (Python 3.10 or newer):
```powershell
./build_exe.bat
```
This creates `dist\AdaptiveScreenDimmer.exe`. It runs without Python and is started briefly as a
test during the build.

Run from source without an EXE:
```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.\adaptive_dimmer_START.bat
```

## Usage

- **Top:** the big switch pauses and resumes, the line below shows the state.
- **Displays:** a live meter for each monitor. The white marker is the current picture
  brightness, the amber line shows where dimming starts and where it reaches full strength.
  Next to it the current dimming and filter in percent, and a switch to include the monitor or
  not. "Show numbers" briefly shows the number on each monitor.
- **Dim:** starts at, full strength at, maximum dimming (at most 94 %, never fully black), and
  how fast it gets darker and brighter again.
- **Bright spots:** Off, Normal, Strong (whole screen) or Local (only the glaring area). With
  Local you also get:
  - Sensitivity: how much brighter than the surroundings an area has to be.
  - Strength: how much a spot is darkened at most.
  - Margin: Tight, Normal or Wide.
  - Fade out: how long the darkening takes to disappear.
- **Blue light filter:** on or off, colour temperature (fewer kelvin is warmer), strength up to
  60 %, and "Night only" with two times.
- **Options:** sample rate, shortcut, start paused, close to tray, start minimized, and the
  language (Auto follows Windows, Deutsch or English). The log is below.

Every section has a "Reset" button that resets only that section. All changes are saved at once
to `%APPDATA%\AdaptiveScreenDimmer\settings.json`. The log is next to it in `dimmer.log`.

Command line: `--paused` (start paused), `--exit-after SEC` (quit after that many seconds),
`--verbose` (detailed log).

## Limits

- Games in exclusive fullscreen (older DirectX titles) are drawn above every window, nothing can
  dim them there. Borderless or windowed mode works.
- Secure screens (User Account Control, lock screen) cannot be measured. The last state is kept.
- Copy-protected video (such as Netflix in a browser) is black in screen captures and therefore
  cannot be measured. YouTube and local files are not affected.
- Bright spots are measured in tiles of 16 × 16 pixels. A spot of about 20 to 30 pixels is
  detected reliably, very small dots and thin lines only partly.
- Local dimming trails the picture by about two frames. Very fast lights can briefly show an
  edge, "Margin: Wide" helps.

## Development

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.\tools\check.ps1          # ruff, formatting, mypy, tests
.\tools\check.ps1 -Live    # plus tests with real, invisible overlays
.\tools\check.ps1 -Build   # plus building the EXE and starting it briefly
.venv\Scripts\python tools\bench_live.py --monitor 0   # latency and CPU with a synthetic flash
```
`bench_live.py` draws its test picture with tkinter and needs a Python that includes tkinter.
`tools\gui_snapshot.py out.png` saves a picture of the window.

Code layout in `dimmer/`:

| File | Purpose |
| --- | --- |
| `logic.py` | measurement, smoothing, bright spots (no Windows dependencies) |
| `profiles.py` | the profile and the night window |
| `settings.py` | loading and saving settings |
| `engine.py` | one thread runs the measuring and all overlay windows |
| `gpu.py`, `wgc.py` | capture with Windows.Graphics.Capture, evaluation on the graphics card |
| `localdim.py` | mask for local dimming (DirectComposition) |
| `gamma.py` | blue light filter through the monitor's colour curve |
| `overlay.py`, `winapi.py` | overlay windows, monitors, GDI capture as fallback |
| `gui.py`, `web/` | the window, built with pywebview |
| `i18n.py`, `web/i18n.js` | texts in English and German |
| `tray.py`, `app.py` | tray icon, startup |

## License

MIT, see [LICENSE](LICENSE).
