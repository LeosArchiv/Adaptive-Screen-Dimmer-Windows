"""Engine thread: owns all overlay windows, measures brightness and drives the overlays.

Every Win32 window, the hotkey and the WinEvent hook live on this one thread, so window
affinity rules are never violated. Other threads talk to it only through ``Engine.post``
(a command queue) and read results through ``Engine.snapshot`` (a copy under a lock).
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field

from . import overlay as overlay_mod
from .logic import ATTACK_PRESETS, RELEASE_PRESETS, Smoother, brightness, compensate, target_opacity
from .overlay import Overlay
from .settings import Settings
from .winapi import Monitor, Sampler, foreground_exe, list_monitors, user32

log = logging.getLogger("dimmer")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0xD1
HOTKEY_VK = ord("D")

# WinEvents that usually mean "new content appeared": measure right away instead of waiting.
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0

WinEventProc = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    WinEventProc,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.MsgWaitForMultipleObjects.argtypes = [
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
QS_ALLINPUT = 0x04FF

MONITOR_CHECK_S = 3.0
TOPMOST_REFRESH_S = 1.0
FOREGROUND_CHECK_S = 0.25
IDLE_AFTER_S = 2.0  # screen unchanged this long -> slower measuring
IDLE_FACTOR = 3.0
IDLE_INTERVAL_MAX_S = 0.25
CALM_THRESHOLD = 1.5  # brightness change (0..255) that counts as "something moved"
EVENT_DELAY_S = 0.03
EVENT_MIN_GAP_S = 0.025


@dataclass
class MonitorStatus:
    device: str
    label: str
    brightness: float = 0.0
    target: float = 0.0
    opacity: int = 0
    excluded_from_capture: bool = True


@dataclass
class Status:
    running: bool = False
    paused: bool = False
    paused_reason: str = ""  # "", "user", "app:<exe>"
    last_foreign_app: str | None = None
    monitors: list[MonitorStatus] = field(default_factory=list)
    error: str | None = None
    hotkey_ok: bool = False


class _Slot:
    """Per-monitor state owned by the engine thread."""

    def __init__(self, monitor: Monitor, index: int) -> None:
        self.monitor = monitor
        self.index = index
        self.overlay = Overlay(monitor)
        self.smoother = Smoother()
        self.level = 0.0
        self.target = 0.0


def wanted_devices(chosen: list[str], monitors: list[Monitor]) -> list[str]:
    """Chosen monitors that are connected; falls back to the primary monitor."""
    present = [m.device for m in monitors]
    wanted = [d for d in chosen if d in present]
    if not wanted:
        wanted = [m.device for m in monitors if m.primary][:1] or present[:1]
    return wanted


def monitor_label(m: Monitor, index: int) -> str:
    text = f"Bildschirm {index + 1} · {m.width}×{m.height}"
    return text + " (Hauptbildschirm)" if m.primary else text


class Engine(threading.Thread):
    def __init__(self, settings: Settings, on_hotkey: Callable[[], None] | None = None) -> None:
        super().__init__(name="dimmer-engine", daemon=True)
        self._settings = settings.normalized()
        self._commands: queue.Queue[Callable[[], None]] = queue.Queue()
        self._lock = threading.Lock()
        self._status = Status()
        self._stop_event = threading.Event()
        self._thread_id = 0
        self._ready = threading.Event()
        self.on_hotkey = on_hotkey
        self._slots: dict[str, _Slot] = {}
        self._monitors: list[Monitor] = []
        self._paused = settings.start_paused
        self._app_paused: str | None = None
        self._wake_now = False
        self._monitors_dirty = False
        self._hook: list[int] = []
        self._hook_proc = WinEventProc(self._on_win_event)

    # ---- public, thread-safe ---------------------------------------------------------
    def post(self, command: Callable[[], None]) -> None:
        self._commands.put(command)
        self._wake()

    def update_settings(self, settings: Settings) -> None:
        s = settings.normalized()
        self.post(lambda: self._apply_settings(s))

    def set_paused(self, paused: bool) -> None:
        self.post(lambda: self._set_paused(paused))

    def toggle_paused(self) -> None:
        self.post(lambda: self._set_paused(not self._paused))

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self._wake()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def snapshot(self) -> Status:
        with self._lock:
            s = self._status
            return Status(
                running=s.running,
                paused=s.paused,
                paused_reason=s.paused_reason,
                last_foreign_app=s.last_foreign_app,
                monitors=[MonitorStatus(**vars(m)) for m in s.monitors],
                error=s.error,
                hotkey_ok=s.hotkey_ok,
            )

    def monitors(self) -> list[Monitor]:
        with self._lock:
            return list(self._monitors)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def _wake(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0400, 0, 0)  # WM_USER, just wakes the wait

    # ---- engine thread ---------------------------------------------------------------
    def run(self) -> None:
        self._thread_id = threading.get_native_id()
        sampler: Sampler | None = None
        try:
            overlay_mod.set_message_hook(self._on_window_message)
            sampler = Sampler()
            self._register_hotkey()
            self._install_hook()
            self._refresh_monitors()
            with self._lock:
                self._status.running = True
            self._ready.set()
            self._loop(sampler)
        except Exception as e:  # report instead of dying silently
            log.exception("Engine stopped because of an error")
            with self._lock:
                self._status.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()
            self._teardown(sampler)

    def _loop(self, sampler: Sampler) -> None:
        last = calm_since = time.perf_counter()
        next_tick = next_monitor_check = next_topmost = next_fg = 0.0
        while not self._stop_event.is_set():
            self._pump()
            self._run_commands()
            if self._stop_event.is_set():
                break
            now = time.perf_counter()
            if self._wake_now:
                # New window / focus change: measure soon, but let it paint first and never
                # measure more often than every EVENT_MIN_GAP_S.
                self._wake_now = False
                next_tick = min(next_tick, max(last + EVENT_MIN_GAP_S, now + EVENT_DELAY_S))
            if self._monitors_dirty or now >= next_monitor_check:
                self._monitors_dirty = False
                self._refresh_monitors()
                next_monitor_check = now + MONITOR_CHECK_S
            if now >= next_fg:
                self._check_foreground()
                next_fg = now + FOREGROUND_CHECK_S

            if now >= next_tick:
                # Cap dt at one active interval: after a slow idle phase the ramp must still
                # be gradual instead of jumping in a single step.
                base = self._settings.interval_ms / 1000.0
                if self._tick(sampler, min(now - last, base)):
                    calm_since = now
                last = now
                next_tick = now + self._current_interval(now - calm_since)

            if now >= next_topmost:
                for slot in self._slots.values():
                    slot.overlay.keep_on_top()
                next_topmost = now + TOPMOST_REFRESH_S

            wake_at = min(next_tick, next_fg)
            self._wait(wake_at - time.perf_counter())

    def _current_interval(self, calm_for: float) -> float:
        """Full rate while anything moves; a slower rate once every monitor has been still."""
        base = self._settings.interval_ms / 1000.0
        if self._paused or self._app_paused is not None:
            return max(base, IDLE_INTERVAL_MAX_S)
        if calm_for >= IDLE_AFTER_S:
            return min(max(base, base * IDLE_FACTOR), max(base, IDLE_INTERVAL_MAX_S))
        return base

    def _tick(self, sampler: Sampler, dt: float) -> bool:
        """One measurement/update round. Returns True when something changed noticeably."""
        s = self._settings
        active = False
        for slot in self._slots.values():
            if self._paused:
                slot.target = 0.0
                slot.smoother.reset(0.0)  # user asked for it: off at once
            elif self._app_paused is not None:
                slot.target = 0.0
                slot.smoother.step(0.0, dt)  # fade out gently, no capture needed
            else:
                try:
                    m = slot.monitor
                    level = brightness(sampler.grab(m.left, m.top, m.width, m.height))
                except OSError as e:  # e.g. secure desktop (UAC, lock screen): keep last state
                    log.debug("capture failed on %s: %s", slot.monitor.device, e)
                    continue
                if not slot.overlay.excluded:
                    level = compensate(level, slot.overlay.alpha)
                if abs(level - slot.level) > CALM_THRESHOLD:
                    active = True
                slot.level = level
                slot.target = target_opacity(level, s.start, s.full, s.max_opacity)
                slot.smoother.step(slot.target, dt)
            slot.overlay.set_alpha(round(slot.smoother.value))
            if not slot.smoother.settled:
                active = True
        self._publish()
        return active

    def _wait(self, seconds: float) -> None:
        if self._wake_now or self._stop_event.is_set():
            return
        ms = max(0, int(seconds * 1000))
        if ms:
            user32.MsgWaitForMultipleObjects(0, None, False, ms, QS_ALLINPUT)

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._set_paused(not self._paused)
                if self.on_hotkey:
                    self.on_hotkey()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _run_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                command()
            except Exception:
                log.exception("Command failed")

    # ---- state changes (engine thread only) -----------------------------------------
    def _apply_settings(self, s: Settings) -> None:
        old = self._settings
        self._settings = s
        for slot in self._slots.values():
            self._configure_smoother(slot.smoother)
        if s.hotkey != old.hotkey:
            self._unregister_hotkey()
            self._register_hotkey()
        if s.monitors != old.monitors:
            self._refresh_monitors(force=True)
        if s.excluded_apps != old.excluded_apps:
            self._check_foreground()
        self._wake_now = True

    def _configure_smoother(self, smoother: Smoother) -> None:
        smoother.attack = ATTACK_PRESETS[self._settings.attack]
        smoother.release = RELEASE_PRESETS[self._settings.release]

    def _set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            log.info("Pausiert" if paused else "Fortgesetzt")
        self._wake_now = True
        self._publish()

    def _check_foreground(self) -> None:
        exe = foreground_exe()
        if exe:
            with self._lock:
                self._status.last_foreign_app = exe
        excluded = exe if exe and exe in self._settings.excluded_apps else None
        # Our own window in front keeps the previous decision (exe is None then).
        if exe is None:
            return
        if excluded != self._app_paused:
            self._app_paused = excluded
            if excluded:
                log.info("Ausnahme aktiv: %s", excluded)
            self._wake_now = True

    def _refresh_monitors(self, force: bool = False) -> None:
        monitors = list_monitors()
        if not force and monitors == self._monitors:
            return
        if monitors != self._monitors:
            log.info("Bildschirme erkannt: %s", ", ".join(f"{m.gdi_name} {m.width}x{m.height}" for m in monitors))
        wanted = set(wanted_devices(self._settings.monitors, monitors))
        by_device = {m.device: (i, m) for i, m in enumerate(monitors)}
        for device in list(self._slots):
            slot = self._slots[device]
            current = by_device.get(device)
            if device not in wanted or current is None or current[1] != slot.monitor:
                slot.overlay.destroy()
                del self._slots[device]
        for device in wanted:
            if device not in self._slots:
                index, monitor = by_device[device]
                slot = _Slot(monitor, index)
                self._configure_smoother(slot.smoother)
                self._slots[device] = slot
                if not slot.overlay.excluded:
                    log.warning("Overlay kann nicht aus der Messung ausgenommen werden – Kompensation aktiv")
        with self._lock:
            self._monitors = monitors
        self._publish()

    def _publish(self) -> None:
        index = {m.device: i for i, m in enumerate(self._monitors)}
        reason = "user" if self._paused else (f"app:{self._app_paused}" if self._app_paused else "")
        rows = [
            MonitorStatus(
                device=d,
                label=monitor_label(slot.monitor, index.get(d, slot.index)),
                brightness=slot.level,
                target=slot.target,
                opacity=slot.overlay.alpha,
                excluded_from_capture=slot.overlay.excluded,
            )
            for d, slot in sorted(self._slots.items(), key=lambda kv: index.get(kv[0], 99))
        ]
        with self._lock:
            self._status.paused = bool(reason)
            self._status.paused_reason = reason
            self._status.monitors = rows

    # ---- Win32 plumbing ----------------------------------------------------------------
    def _on_window_message(self, msg: int, _wp: int, _lp: int) -> None:
        self._monitors_dirty = True

    def _on_win_event(self, _hook, event, hwnd, id_object, _child, _thread, _time) -> None:
        if id_object == OBJID_WINDOW and hwnd:
            self._wake_now = True

    def _install_hook(self) -> None:
        # FOREGROUND..MINIMIZEEND, SHOW and NAMECHANGE (window title, e.g. page navigation).
        # LOCATIONCHANGE is far too chatty (every cursor move) and deliberately left out.
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        self._hook = [
            user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MINIMIZEEND, None, self._hook_proc, 0, 0, flags
            ),
            user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, self._hook_proc, 0, 0, flags),
            user32.SetWinEventHook(
                EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, None, self._hook_proc, 0, 0, flags
            ),
        ]

    def _register_hotkey(self) -> None:
        ok = False
        if self._settings.hotkey:
            ok = bool(user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, HOTKEY_VK))
            if not ok:
                log.warning("Tastenkürzel Strg+Alt+D ist bereits von einem anderen Programm belegt")
        with self._lock:
            self._status.hotkey_ok = ok

    def _unregister_hotkey(self) -> None:
        user32.UnregisterHotKey(None, HOTKEY_ID)

    def _teardown(self, sampler: Sampler | None) -> None:
        for slot in self._slots.values():
            slot.overlay.destroy()  # never leave a dark screen behind
        self._slots.clear()
        for h in self._hook:
            if h:
                user32.UnhookWinEvent(h)
        self._unregister_hotkey()
        overlay_mod.set_message_hook(None)
        if sampler:
            sampler.close()
        with self._lock:
            self._status.running = False
            self._status.monitors = []
