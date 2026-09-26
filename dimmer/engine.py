"""Engine thread: owns all overlay windows, measures brightness and drives the overlays.

Every Win32 window, the hotkey and the WinEvent hook live on this one thread, so window
affinity rules are never violated. Other threads talk to it only through ``Engine.post``
(a command queue) and read results through ``Engine.snapshot`` (a copy under a lock).
"""

from __future__ import annotations

import ctypes
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np

from . import overlay as overlay_mod
from .logic import ATTACK_PRESETS, RELEASE_PRESETS, Smoother, brightness, compensate, target_opacity
from .overlay import Overlay
from .profiles import Effective, kelvin_to_rgb, resolve_monitor
from .settings import Settings
from .winapi import Monitor, Sampler, apps_per_monitor, clear_monitor_id_cache, list_monitors, user32

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

TINT_RATE = 25.0  # percent per second: profile changes blend the tint in ~1 s
KELVIN_RATE = 3000.0  # kelvin per second
MONITOR_CHECK_S = 3.0
TOPMOST_REFRESH_S = 1.0
PROFILE_CHECK_S = 0.25  # which app is in front on which monitor
FINGERPRINT_STEP = 8
APP_CONFIRM = 2  # consecutive sightings before a monitor switches to another app's profile
IDLE_AFTER_S = 2.0  # screen unchanged this long -> slower measuring
IDLE_FACTOR = 2.0  # at most 100 ms extra latency for a flash after a still phase
IDLE_INTERVAL_MAX_S = 0.25  # while paused
CALM_THRESHOLD = 1.5  # brightness change (0..255) that counts as "something moved"
EVENT_DELAY_S = 0.03
MIN_SLEEP_S = 0.005
FAILSAFE_AFTER = 3  # consecutive loop failures before all overlays are cleared
CAPTURE_RENEW_AFTER = 40  # consecutive capture failures (~2 s) before the DC is renewed


@dataclass
class MonitorStatus:
    device: str
    label: str
    brightness: float = 0.0
    target: float = 0.0
    opacity: int = 0
    excluded_from_capture: bool = True
    profile: str = ""  # e.g. "Zocken + Nacht"
    reason: str = ""  # e.g. "Programm ddnet.exe", "Zeitplan"
    app: str | None = None  # app in front on this monitor
    tint: float = 0.0  # current blue-light filter strength in percent
    start: float | None = None  # thresholds in effect (None: dimming off), for the meter
    full: float | None = None


@dataclass
class Status:
    running: bool = False
    paused: bool = False
    paused_reason: str = ""  # "" or "user"
    recent_apps: list[str] = field(default_factory=list)  # newest first, for rule suggestions
    monitors: list[MonitorStatus] = field(default_factory=list)
    error: str | None = None
    hotkey_ok: bool = False
    heartbeat: float = 0.0  # time.monotonic() of the last engine loop round


class _Slot:
    """Per-monitor state owned by the engine thread."""

    def __init__(self, monitor: Monitor, index: int) -> None:
        self.monitor = monitor
        self.index = index
        # One capture buffer per monitor: a shared one would be reallocated every round when
        # monitors differ in size. A fresh screen DC also follows display mode changes.
        self.sampler = Sampler()
        self.overlay: Overlay | None = None
        self.tint: Overlay | None = None
        try:
            self.tint = Overlay(monitor, kelvin_to_rgb(3400))
            self.overlay = Overlay(monitor)
            self.tint.below = self.overlay  # the tint always stays directly under the dimming
        except Exception:
            self.close()
            raise
        self.smoother = Smoother()
        self.level = 0.0
        self.target = 0.0
        self.capture_failures = 0
        self._fingerprint: np.ndarray | None = None
        self._fingerprint_level = 0.0
        self.effective = Effective()
        self.app: str | None = None
        self.app_known = False
        self.pending_app: str | None = None
        self.pending_hits = 0
        self.tint_strength = 0.0  # current, fades toward effective.tint_strength
        self.tint_kelvin = 3400.0

    def measure(self, pixels: np.ndarray) -> float:
        """Exact mean brightness, skipped when the frame is unchanged.

        A fixed coarse grid (every 8th pixel) is compared with the previous frame first; the
        exact sum over all pixels (the most expensive step) only runs when something changed.
        A change missing every grid point is smaller than 8x8 pixels and moves the mean by
        less than 0.01, far below anything visible.
        """
        fingerprint = pixels[::FINGERPRINT_STEP, ::FINGERPRINT_STEP, :3]
        if self._fingerprint is not None and np.array_equal(fingerprint, self._fingerprint):
            return self._fingerprint_level
        self._fingerprint = fingerprint.copy()
        self._fingerprint_level = brightness(pixels)
        return self._fingerprint_level

    def forget_frame(self) -> None:
        self._fingerprint = None

    @property
    def dim(self) -> Overlay:
        assert self.overlay is not None
        return self.overlay

    @property
    def warm(self) -> Overlay:
        assert self.tint is not None
        return self.tint

    def update_geometry(self, monitor: Monitor, index: int) -> None:
        self.index = index
        if monitor != self.monitor:
            sampler = Sampler()  # swap only once the new one exists
            self.warm.move(monitor)
            self.dim.move(monitor)
            self.sampler.close()
            self.sampler = sampler
            self.monitor = monitor
            self.forget_frame()

    def renew_sampler(self) -> None:
        sampler = Sampler()
        self.sampler.close()
        self.sampler = sampler

    def tint_moving(self, paused: bool = False) -> bool:
        e = self.effective
        goal = 0.0 if paused else e.tint_strength
        return abs(self.tint_strength - goal) > 0.05 or (
            not paused and e.tint_on and abs(self.tint_kelvin - e.tint_kelvin) > 1
        )

    def step_tint(self, dt: float, off: bool) -> None:
        """Move the blue-light filter linearly toward its target; slow enough to never flicker."""
        goal = 0.0 if off else self.effective.tint_strength
        self.tint_strength = _approach(self.tint_strength, goal, TINT_RATE * dt)
        if self.effective.tint_on:
            self.tint_kelvin = _approach(self.tint_kelvin, self.effective.tint_kelvin, KELVIN_RATE * dt)
        self.warm.set_color(kelvin_to_rgb(self.tint_kelvin))
        self.warm.set_alpha(round(self.tint_strength / 100 * 255))

    def keep_on_top(self) -> None:
        # Dimming first (even while hidden, so its z position is always the reference), then the
        # tint directly below it: the tint never covers the dimming, not even for a frame.
        self.dim.keep_on_top(even_hidden=True)
        self.warm.keep_on_top()

    def clear(self) -> None:
        """Fail-safe: hide both layers; each one independently of the other."""
        self.smoother.reset(0.0)
        self.tint_strength = 0.0
        for layer in (self.overlay, self.tint):
            if layer is None:
                continue
            try:
                layer.hide()
            except Exception:
                log.debug("could not hide overlay", exc_info=True)

    def close(self) -> None:
        if self.overlay:
            self.overlay.destroy()
        if self.tint:
            self.tint.destroy()
        self.sampler.close()


def _approach(value: float, goal: float, step: float) -> float:
    if abs(goal - value) <= step:
        return goal
    return value + step if goal > value else value - step


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
        self._recent_apps: deque[str] = deque(maxlen=12)
        self._wake_now = False
        self._monitors_dirty = True  # first loop round enumerates the monitors
        self._force_refresh = False
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
                recent_apps=list(s.recent_apps),
                monitors=[MonitorStatus(**vars(m)) for m in s.monitors],
                error=s.error,
                hotkey_ok=s.hotkey_ok,
                heartbeat=s.heartbeat,
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
        try:
            overlay_mod.set_message_hook(self._on_window_message)
            self._register_hotkey()
            self._install_hook()
            try:
                self._refresh_monitors()
                self._monitors_dirty = False
            except Exception:  # the loop retries; a vanishing monitor must not end the engine
                log.exception("Bildschirme konnten beim Start nicht gelesen werden")
            with self._lock:
                self._status.running = True
            self._ready.set()
            self._loop()
        except Exception as e:  # report instead of dying silently
            log.exception("Engine stopped because of an error")
            with self._lock:
                self._status.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()
            self._teardown()

    def _loop(self) -> None:
        last = calm_since = time.perf_counter()
        next_tick = next_monitor_check = next_topmost = next_fg = 0.0
        failures = 0
        last_error = ""
        while not self._stop_event.is_set():
            try:
                self._pump()
                self._run_commands()
                if self._stop_event.is_set():
                    break
                now = time.perf_counter()
                base = self._settings.interval_ms / 1000.0
                if self._wake_now:
                    # New window / focus / title change: measure soon, after the window had a
                    # moment to paint, never above the base rate. Whether idle mode ends is
                    # decided by the measurement itself (a chatty title ticker must not keep
                    # the engine at full rate).
                    self._wake_now = False
                    next_tick = min(next_tick, max(last + base, now + EVENT_DELAY_S))
                if self._monitors_dirty or self._force_refresh or now >= next_monitor_check:
                    self._monitors_dirty = False
                    next_monitor_check = now + MONITOR_CHECK_S
                    self._refresh_monitors(force=self._force_refresh)
                    self._force_refresh = False
                if now >= next_fg:
                    self._resolve_profiles()
                    next_fg = now + PROFILE_CHECK_S

                if now >= next_tick:
                    # Cap dt at two active intervals: after a slow idle phase the ramp must still
                    # be gradual instead of jumping in a single step.
                    if self._tick(min(now - last, 2 * base)):
                        calm_since = now
                    last = now
                    # Measured from the end of the tick, so a slow capture can never turn the
                    # loop into a busy spin.
                    next_tick = max(now + self._current_interval(now - calm_since), time.perf_counter() + MIN_SLEEP_S)

                if now >= next_topmost:
                    for slot in self._slots.values():
                        slot.keep_on_top()
                    next_topmost = now + TOPMOST_REFRESH_S
                if failures:
                    log.info("Messschleife läuft wieder")
                    failures, last_error = 0, ""
                    with self._lock:
                        self._status.error = None
                self._wait(min(next_tick, next_fg) - time.perf_counter())
            except Exception as e:
                # A transient Win32 error (e.g. a monitor vanishing mid-enumeration) must not end
                # the dimmer for the rest of the session: log, back off, rebuild, carry on.
                failures += 1
                error = f"{type(e).__name__}: {e}"
                if error != last_error:  # full traceback once per distinct error, not every 2 s
                    log.exception("Fehler in der Messschleife")
                    last_error = error
                with self._lock:
                    self._status.error = error
                self._monitors_dirty = True
                # Fail safe: while things keep failing, never leave a frozen dark overlay.
                if failures >= FAILSAFE_AFTER or self._paused:
                    self._clear_overlays()
                self._publish_safe()
                self._sleep_pumping(min(2.0, 0.2 * failures))

    def _clear_overlays(self) -> None:
        for slot in self._slots.values():
            try:
                slot.clear()
            except Exception:
                log.debug("could not clear overlay", exc_info=True)

    def _publish_safe(self) -> None:
        try:
            self._publish()
        except Exception:
            log.debug("publish failed", exc_info=True)

    def _sleep_pumping(self, seconds: float) -> None:
        """Back off without blocking window messages (broadcasts to our overlays keep flowing)."""
        end = time.perf_counter() + seconds
        while not self._stop_event.is_set():
            left = end - time.perf_counter()
            if left <= 0:
                return
            user32.MsgWaitForMultipleObjects(0, None, False, max(1, math.ceil(left * 1000)), QS_ALLINPUT)
            try:
                self._pump()
            except Exception:
                log.debug("pump failed during backoff", exc_info=True)

    def _any_moving(self) -> bool:
        return any(not slot.smoother.settled or slot.tint_moving(self._paused) for slot in self._slots.values())

    def _current_interval(self, calm_for: float) -> float:
        """Full rate while anything moves; a slower rate once every monitor has been still."""
        base = self._settings.interval_ms / 1000.0
        if self._any_moving():
            return base  # a running fade always gets full rate, or it would show as steps
        if self._paused or not any(slot.effective.dim_on for slot in self._slots.values()):
            return max(base, IDLE_INTERVAL_MAX_S)  # nothing to measure
        if calm_for >= IDLE_AFTER_S:
            return min(max(base, base * IDLE_FACTOR), max(base, IDLE_INTERVAL_MAX_S))
        return base

    def _tick(self, dt: float) -> bool:
        """One measurement/update round. Returns True when something changed noticeably."""
        active = False
        for slot in self._slots.values():
            e = slot.effective
            smoother = slot.smoother
            if self._paused:
                slot.target = 0.0
                smoother.reset(0.0)  # user asked for it: off at once
                slot.tint_strength = 0.0
            elif not e.dim_on:
                slot.target = 0.0
                smoother.step(0.0, dt, skip_hold=True)  # fade out gently, no capture needed
            else:
                try:
                    m = slot.monitor
                    level = slot.measure(slot.sampler.grab(m.left, m.top, m.width, m.height))
                    slot.capture_failures = 0
                except OSError as err:  # e.g. secure desktop (UAC, lock screen): keep last state
                    self._capture_failed(slot, err)
                    if slot.capture_failures >= CAPTURE_RENEW_AFTER:
                        smoother.step(0.0, dt)
                        slot.dim.set_alpha(round(smoother.value))
                    slot.step_tint(dt, off=False)
                    continue
                if not slot.dim.excluded:
                    # The alpha set one tick (>= 16 ms, several frames) ago is what the capture shows.
                    tint = slot.warm
                    tint_alpha = 0 if tint.excluded or not tint.visible else tint.alpha
                    level = compensate(level, slot.dim.alpha, tint_alpha, sum(tint.color) / 3)
                if abs(level - slot.level) > CALM_THRESHOLD:
                    active = True
                slot.level = level
                slot.target = target_opacity(level, e.start, e.full, e.max_opacity)
                smoother.step(slot.target, dt)
            slot.dim.set_alpha(round(smoother.value))
            slot.step_tint(dt, off=self._paused)
            if not smoother.settled or slot.tint_moving(self._paused):
                active = True
        self._publish()
        return active

    def _capture_failed(self, slot: _Slot, error: OSError) -> None:
        slot.capture_failures += 1
        log.debug("capture failed on %s: %s", slot.monitor.gdi_name, error)
        if slot.capture_failures % CAPTURE_RENEW_AFTER == 0:
            # Persistent failure (not just a short UAC prompt): get a fresh screen DC and, so a
            # stale dark overlay cannot linger unnoticed, fade it out until capture works again.
            log.warning("Bildschirm %s kann nicht gemessen werden", slot.monitor.gdi_name)
            slot.target = 0.0
            slot.renew_sampler()

    def _wait(self, seconds: float) -> None:
        if self._wake_now or self._stop_event.is_set():
            return
        ms = max(0, math.ceil(seconds * 1000))
        if ms:
            user32.MsgWaitForMultipleObjects(0, None, False, ms, QS_ALLINPUT)

    def _pump(self) -> None:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._set_paused(not self._paused)
                if self.on_hotkey:
                    try:
                        self.on_hotkey()
                    except Exception:
                        log.exception("Hotkey callback failed")
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
        if s.hotkey != old.hotkey:
            self._unregister_hotkey()
            self._register_hotkey()
        if s.monitors != old.monitors:
            self._force_refresh = True  # done by the loop, which retries if it fails
        self._resolve_profiles()  # profile edits apply at once
        self._wake_now = True

    def _set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            log.info("Pausiert" if paused else "Fortgesetzt")
        self._wake_now = True
        self._publish()

    def _resolve_profiles(self) -> None:
        """Decide per monitor which profile applies: base (fixed or day/night) + app in front."""
        s = self._settings
        profiles, rules = s.profile_map(), s.rule_map()
        apps = apps_per_monitor([slot.monitor for slot in self._slots.values()]) if self._slots else {}
        lt = time.localtime()
        minute = lt.tm_hour * 60 + lt.tm_min + lt.tm_sec / 60
        for device, slot in self._slots.items():
            app = self._confirmed_app(slot, apps.get(device))
            if app and (not self._recent_apps or self._recent_apps[0] != app):
                if app in self._recent_apps:
                    self._recent_apps.remove(app)
                self._recent_apps.appendleft(app)
            effective = resolve_monitor(profiles, rules, s.schedule, s.base_choice(device), app, minute)
            if effective.label != slot.effective.label:
                log.info("%s: Profil %s (%s)", slot.monitor.gdi_name, effective.label, effective.reason)
            if effective != slot.effective:
                old = slot.effective
                slot.effective = effective
                slot.smoother.attack = ATTACK_PRESETS[effective.attack]
                slot.smoother.release = RELEASE_PRESETS[effective.release]
                # Wake only for real switches, not for every step of a slow day/night fade.
                if (old.label, old.dim_on, old.tint_on) != (effective.label, effective.dim_on, effective.tint_on):
                    self._wake_now = True

    @staticmethod
    def _confirmed_app(slot: _Slot, seen: str | None) -> str | None:
        """Accept a new app in front only when seen twice in a row (~0.25 s): Start menu,
        Alt+Tab and similar short-lived windows must not make the profile flip back and forth."""
        if not slot.app_known:
            slot.app, slot.app_known = seen, True
        elif seen == slot.app:
            slot.pending_hits = 0
        elif seen == slot.pending_app:
            slot.pending_hits += 1
            if slot.pending_hits >= APP_CONFIRM:
                slot.app, slot.pending_hits = seen, 0
        else:
            slot.pending_app, slot.pending_hits = seen, 1
        return slot.app

    def _refresh_monitors(self, force: bool = False) -> None:
        monitors = list_monitors()
        if not force and monitors == self._monitors:
            return
        if monitors != self._monitors:
            log.info("Bildschirme erkannt: %s", ", ".join(f"{m.gdi_name} {m.width}x{m.height}" for m in monitors))
        wanted = set(wanted_devices(self._settings.monitors, monitors))
        by_device = {m.device: (i, m) for i, m in enumerate(monitors)}
        for device in list(self._slots):
            if device not in wanted or device not in by_device:
                self._slots.pop(device).close()
        for device in wanted:
            index, monitor = by_device[device]
            slot = self._slots.get(device)
            if slot:
                # Keep window and smoother: a resolution/arrangement change must not flash.
                slot.update_geometry(monitor, index)
                continue
            slot = _Slot(monitor, index)
            self._slots[device] = slot
            self._resolve_profiles()
            if not slot.dim.excluded:
                log.warning("Overlay kann nicht aus der Messung ausgenommen werden \u2013 Kompensation aktiv")
        with self._lock:
            self._monitors = monitors
        self._publish()

    def _publish(self) -> None:
        index = {m.device: i for i, m in enumerate(self._monitors)}
        rows = [
            MonitorStatus(
                device=d,
                label=monitor_label(slot.monitor, index.get(d, slot.index)),
                brightness=slot.level,
                target=slot.target,
                opacity=slot.dim.alpha,
                excluded_from_capture=slot.dim.excluded,
                profile=slot.effective.label,
                reason=slot.effective.reason,
                app=slot.app,
                tint=slot.tint_strength,
                start=slot.effective.start if slot.effective.dim_on else None,
                full=slot.effective.full if slot.effective.dim_on else None,
            )
            for d, slot in sorted(self._slots.items(), key=lambda kv: index.get(kv[0], 99))
        ]
        with self._lock:
            self._status.paused = self._paused
            self._status.paused_reason = "user" if self._paused else ""
            self._status.monitors = rows
            self._status.recent_apps = list(self._recent_apps)
            self._status.heartbeat = time.monotonic()

    # ---- Win32 plumbing ----------------------------------------------------------------
    def _on_window_message(self, msg: int, _wp: int, _lp: int) -> None:
        self._monitors_dirty = True
        clear_monitor_id_cache()

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

    def _teardown(self) -> None:
        for slot in self._slots.values():
            try:
                slot.close()  # never leave a dark screen behind
            except Exception:
                log.exception("Overlay cleanup failed")
        self._slots.clear()
        for h in self._hook:
            if h:
                user32.UnhookWinEvent(h)
        self._unregister_hotkey()
        overlay_mod.set_message_hook(None)
        with self._lock:
            self._status.running = False
            self._status.monitors = []
