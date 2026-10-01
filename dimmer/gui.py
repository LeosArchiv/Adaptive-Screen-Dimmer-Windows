"""Web user interface (pywebview with the Edge WebView2 runtime).

pywebview owns the main thread. The page in dimmer/web talks to Python only through ``Api``;
its methods run on pywebview worker threads, so every change of the settings goes through a lock.
"""

from __future__ import annotations

import ctypes
import dataclasses
import functools
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import settings as settings_mod
from .engine import Engine, Status, wanted_devices
from .i18n import LANGUAGES, resolve_language, t
from .logic import ATTACK_PRESETS, RELEASE_PRESETS
from .profiles import KELVIN_MAX, KELVIN_MIN, TINT_MAX, Profile, kelvin_to_rgb, tint_active_now
from .settings import Settings
from .tray import TrayIcon

log = logging.getLogger("dimmer")

TITLE = "Adaptive Screen Dimmer"
SAVE_DELAY_S = 0.6
STALL_S = 3.0
TRAY_POLL_S = 0.1
LOG_BATCH = 200
# Measurements per second while something moves; still screens are measured at half the rate.
RATE_PRESETS = {"Sparsam": 100, "Normal": 50, "Schnell": 33}
PROFILE_FIELDS = {f.name for f in dataclasses.fields(Profile)}
# Fields each "Standardwerte" button puts back; profile fields unknown to Profile are skipped.
RESET_SECTIONS: dict[str, tuple[str, ...]] = {
    "dim": ("start", "full", "max_opacity", "attack", "release"),
    "glare": ("glare", "glare_contrast", "glare_strength", "glare_margin", "glare_fade_ms"),
    "tint": ("tint_on", "tint_kelvin", "tint_strength", "tint_night_only", "night_start", "day_start"),
    "options": ("interval_ms", "hotkey", "start_paused", "close_to_tray", "start_minimized"),
}
SECTION_NAMES = {"dim": "dimming", "glare": "bright spots", "tint": "blue light filter", "options": "options"}


def web_dir() -> Path:
    """Folder with index.html; inside the onefile EXE it is unpacked to sys._MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "dimmer" / "web" if base else Path(__file__).with_name("web")


def rate_name(interval_ms: int) -> str:
    return min(RATE_PRESETS, key=lambda name: abs(RATE_PRESETS[name] - interval_ms))


class QueueLogHandler(logging.Handler):
    """Collects log records from any thread; the page fetches them while it is visible."""

    def __init__(self) -> None:
        super().__init__()
        self.records: queue.Queue[str] = queue.Queue(maxsize=1000)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.put_nowait(self.format(record))
        except queue.Full:
            pass

    def drain(self, limit: int = LOG_BATCH) -> list[str]:
        lines: list[str] = []
        while len(lines) < limit:
            try:
                lines.append(self.records.get_nowait())
            except queue.Empty:
                break
        return lines


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


class Api:
    """Bridge for the page (window.pywebview.api). Public methods are callable from JavaScript."""

    def __init__(self, engine: Engine, settings: Settings, log_handler: QueueLogHandler) -> None:
        self._engine = engine
        self._settings = settings
        self._log = log_handler
        self._lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._ui: DimmerApp | None = None

    # ---- settings ------------------------------------------------------------------------------
    def current_settings(self) -> Settings:
        return self._settings

    def _apply(self, new: Settings) -> None:
        """Caller holds the lock."""
        new = new.normalized()
        if new == self._settings:
            return
        self._settings = new
        self._engine.update_settings(new)
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(SAVE_DELAY_S, self.flush)
        self._save_timer.daemon = True
        self._save_timer.start()

    def flush(self) -> None:
        """Writes the settings now (also called on quit so nothing pending gets lost)."""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
                self._save_timer = None
            s = self._settings
        try:
            settings_mod.save(s)
        except OSError as e:
            log.warning("Could not save settings: %s", e)

    # ---- called from JavaScript ------------------------------------------------------------------
    def get_state(self) -> dict[str, Any]:
        """Everything the page needs once at start: settings, choices, limits."""
        with self._lock:
            s = self._settings
        return {
            "profile": dataclasses.asdict(s.profile),
            "defaults": dataclasses.asdict(Profile()),
            "options": _options(s),
            "lang": resolve_language(s.language),
            "choices": {
                "attack": list(ATTACK_PRESETS),
                "release": list(RELEASE_PRESETS),
                "rate": list(RATE_PRESETS),
            },
            "limits": {"kelvin_min": KELVIN_MIN, "kelvin_max": KELVIN_MAX, "tint_max": TINT_MAX},
            "kelvin": {str(k): _hex(kelvin_to_rgb(k)) for k in range(KELVIN_MIN, KELVIN_MAX + 1, 100)},
        }

    def set_profile(self, values: dict[str, Any]) -> dict[str, Any]:
        """Takes changed profile fields, returns the profile as it was stored (clamped)."""
        with self._lock:
            changes = {k: v for k, v in values.items() if k in PROFILE_FIELDS}
            try:
                profile = dataclasses.replace(self._settings.profile, **changes).normalized()
            except (TypeError, ValueError) as e:
                log.warning("Invalid profile values: %s", e)
                profile = self._settings.profile
            self._apply(dataclasses.replace(self._settings, profile=profile))
            return dataclasses.asdict(self._settings.profile)

    def reset_section(self, section: str) -> dict[str, Any]:
        """Puts the fields of one section back to their defaults; returns profile and options."""
        with self._lock:
            s = self._settings
            fields = RESET_SECTIONS.get(section, ())
            if section == "options":
                defaults = Settings()
                new = dataclasses.replace(s, **{f: getattr(defaults, f) for f in fields})
            else:
                base = Profile()
                changes = {f: getattr(base, f) for f in fields if f in PROFILE_FIELDS}
                new = dataclasses.replace(s, profile=dataclasses.replace(s.profile, **changes))
            self._apply(new)
            log.info("Reset %s to defaults", SECTION_NAMES.get(section, section))
            return {"profile": dataclasses.asdict(self._settings.profile), "options": _options(self._settings)}

    def set_option(self, name: str, value: Any) -> str:
        """Changes one option; returns the language to show (it may just have changed)."""
        with self._lock:
            s = self._settings
            if name == "rate" and value in RATE_PRESETS:
                new = dataclasses.replace(s, interval_ms=RATE_PRESETS[value])
            elif name == "language" and value in ("auto", *LANGUAGES):
                new = dataclasses.replace(s, language=value)
            elif name in ("hotkey", "start_paused", "close_to_tray", "start_minimized"):
                new = dataclasses.replace(s, **{name: bool(value)})  # type: ignore[arg-type]
            else:
                return self.language()
            self._apply(new)
        return self.language()

    def language(self) -> str:
        """The language shown right now ('de' or 'en')."""
        return resolve_language(self._settings.language)

    def toggle_pause(self) -> bool:
        self._engine.toggle_paused()
        return self._engine.snapshot().paused_reason == "user"

    def set_monitor_enabled(self, device: str, enabled: bool) -> None:
        with self._lock:
            monitors = self._engine.monitors()
            present = {m.device for m in monitors}
            chosen = set(wanted_devices(self._settings.monitors, monitors))
            if enabled:
                chosen.add(device)
            else:
                chosen.discard(device)
            if not chosen:
                return  # at least one monitor stays active
            ordered = [m.device for m in monitors if m.device in chosen]
            # Monitors that are unplugged right now (laptop on the road) keep their choice.
            ordered += [d for d in self._settings.monitors if d not in present]
            self._apply(dataclasses.replace(self._settings, monitors=ordered))

    def identify(self) -> None:
        if self._ui:
            self._ui.identify()

    def get_status(self) -> dict[str, Any]:
        """Live values for the page, polled several times per second while it is visible."""
        st = self._engine.snapshot()
        monitors = self._engine.monitors()
        with self._lock:
            s = self._settings
        wanted = set(wanted_devices(s.monitors, monitors))
        live = {m.device: m for m in st.monitors}
        rows = []
        for i, m in enumerate(monitors):
            ms = live.get(m.device)
            rows.append(
                {
                    "device": m.device,
                    "number": i + 1,
                    "size": f"{m.width} × {m.height}",
                    "primary": m.primary,
                    "enabled": m.device in wanted,
                    "active": ms is not None,
                    "brightness": round(ms.brightness, 1) if ms else 0,
                    "start": ms.start if ms else None,
                    "full": ms.full if ms else None,
                    "dim": round(ms.opacity / 255 * 100) if ms else 0,
                    "tint": round(ms.tint) if ms else 0,
                    "capture": ms.capture if ms else "",
                }
            )
        lt = time.localtime()
        return {
            "state": _state(st),
            "paused": st.paused_reason == "user",
            "hotkey_ok": st.hotkey_ok,
            "monitors": rows,
            "tint_now": tint_active_now(s.profile, lt.tm_hour * 60 + lt.tm_min),
            "log": self._log.drain(),
        }


def _options(s: Settings) -> dict[str, Any]:
    return {
        "rate": rate_name(s.interval_ms),
        "hotkey": s.hotkey,
        "start_paused": s.start_paused,
        "close_to_tray": s.close_to_tray,
        "start_minimized": s.start_minimized,
        "language": s.language,
    }


def _state(st: Status) -> dict[str, str]:
    """Kind and reason; the page turns the reason into text in its language."""
    stalled = st.running and st.heartbeat and time.monotonic() - st.heartbeat > STALL_S
    if st.error:
        return {"kind": "error", "reason": "error", "detail": str(st.error)}
    if stalled:
        return {"kind": "error", "reason": "stalled"}
    if not st.running:
        return {"kind": "error", "reason": "stopped"}
    if st.paused_reason == "user":
        return {"kind": "paused", "reason": "paused"}
    return {"kind": "ok", "reason": "ok"}


def _place(title: str, x: int, y: int) -> None:
    """pywebview scales window positions by the DPI factor; monitor rects are physical pixels."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


IDENTIFY_HTML = """<!doctype html><html><body style="margin:0;height:100vh;display:grid;place-items:center;
background:#f0b45a;color:#1b1407;font:600 96px 'Segoe UI Variable Display','Segoe UI',sans-serif;
user-select:none;cursor:default">{n}</body></html>"""


class DimmerApp:
    """Window, tray and engine glue. Create it, then call run() on the main thread."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        log_handler: QueueLogHandler,
        tray: TrayIcon | None,
        start_hidden: bool = False,
    ) -> None:
        import webview

        self._webview = webview
        self.engine = engine
        self.tray = tray
        self.api = Api(engine, settings, log_handler)
        self.api._ui = self
        self._visible = not start_hidden
        self._quitting = False
        self._stop = threading.Event()
        window = webview.create_window(
            TITLE,
            url=str(web_dir() / "index.html"),
            js_api=self.api,
            width=580,
            height=820,
            min_size=(440, 480),
            hidden=start_hidden,
            background_color="#1c1c1c",
        )
        if window is None:
            raise RuntimeError("window could not be created")
        self.window = window
        self.window.events.closing += self._on_closing
        self.window.events.minimized += lambda: self._set_visible(False)
        self.window.events.restored += lambda: self._set_visible(True)

    # ---- window ------------------------------------------------------------------------------------
    def run(self) -> None:
        threading.Thread(target=self._tray_loop, name="ui-tray", daemon=True).start()
        self._webview.start(private_mode=True, debug=False)
        self._stop.set()

    def _set_visible(self, visible: bool) -> None:
        self._visible = visible
        try:
            self.window.evaluate_js(f"window.dimmerVisible && window.dimmerVisible({str(visible).lower()})")
        except Exception:  # page not loaded yet
            pass

    def _on_closing(self) -> bool:
        """Close button: hide to the tray when wanted, otherwise quit. False keeps the window."""
        if self._quitting:
            return True
        if self.tray and self.tray.is_alive() and self.api.current_settings().close_to_tray:
            self.hide_window()
            return False
        self._quitting = True
        self.api.flush()
        return True

    def show_window(self) -> None:
        self.window.show()
        self.window.restore()
        self._set_visible(True)

    def hide_window(self) -> None:
        self.window.hide()
        self._set_visible(False)

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.api.flush()
        self.window.destroy()

    def identify(self) -> None:
        """Shows the number of each monitor in its top left corner for two seconds."""
        windows = []
        for i, m in enumerate(self.engine.monitors()):
            w = self._webview.create_window(
                f"{TITLE} {i + 1}",
                html=IDENTIFY_HTML.format(n=i + 1),
                x=m.left + 48,
                y=m.top + 48,
                width=180,
                height=180,
                frameless=True,
                on_top=True,
                focus=False,
                resizable=False,
                min_size=(120, 120),
                easy_drag=False,
                background_color="#f0b45a",
            )
            if w is not None:
                windows.append(w)
                w.events.shown += functools.partial(_place, f"{TITLE} {i + 1}", m.left + 48, m.top + 48)

        def close() -> None:
            for w in windows:
                try:
                    w.destroy()
                except Exception:
                    pass

        threading.Timer(2.0, close).start()

    # ---- tray -----------------------------------------------------------------------------------------
    def _tray_loop(self) -> None:
        last_tip = None
        while not self._stop.wait(TRAY_POLL_S):
            if not self.tray:
                continue
            self.tray.set_language(self.api.language())
            try:
                while True:
                    action = self.tray.actions.get_nowait()
                    if action == "toggle":
                        self.engine.toggle_paused()
                    elif action == "show":
                        if self._visible:
                            self.hide_window()
                        else:
                            self.show_window()
                    elif action == "quit":
                        self.quit()
                        return
            except queue.Empty:
                pass
            st = self.engine.snapshot()
            paused = st.paused_reason == "user"
            if paused:
                tip = f"{TITLE}: {t(self.api.language(), 'tip_paused')}"
            else:
                parts = [f"{i + 1}: {round(m.opacity / 255 * 100)} %" for i, m in enumerate(st.monitors)]
                tip = f"{TITLE}: " + ", ".join(parts)
            if (paused, tip) != last_tip:
                last_tip = (paused, tip)
                self.tray.set_state(paused, tip)
