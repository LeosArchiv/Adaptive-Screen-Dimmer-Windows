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
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np

from . import overlay as overlay_mod
from . import wgc
from .gamma import GammaTint, channel_factors
from .gpu import GpuBrightness
from .localdim import LocalDimmer
from .logic import (
    ATTACK_PRESETS,
    RELEASE_PRESETS,
    FrameStats,
    Smoother,
    blend_mask,
    brightness,
    compensate,
    frame_stats,
    glare_level,
    local_target,
    target_opacity,
    tile_means,
    tile_sums,
)
from .overlay import Overlay
from .profiles import GLARE_WEIGHTS, Effective, kelvin_to_rgb, resolve
from .settings import Settings
from .winapi import Monitor, Sampler, clear_monitor_id_cache, list_monitors, user32

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
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x2
TIMER_ALL_ACCESS = 0x1F0003
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateWaitableTimerExW.restype = wintypes.HANDLE
kernel32.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
kernel32.SetWaitableTimer.restype = wintypes.BOOL
kernel32.SetWaitableTimer.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(ctypes.c_longlong),
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

TINT_RATE = 25.0  # percent per second: profile changes blend the tint in ~1 s
KELVIN_RATE = 3000.0  # kelvin per second
MONITOR_CHECK_S = 3.0
TOPMOST_REFRESH_S = 1.0
PROFILE_CHECK_S = 0.25  # profile and night window are re-evaluated this often
FINGERPRINT_STEP = 8
LOCAL_REDRAW_S = 0.033
LOCAL_FREE_AFTER_S = 30.0
BURST_DELTA = 8.0  # brightness change (0..255) between two measurements that starts a burst
WGC_WATCHDOG_S = 15.0  # GPU capture without any frame this long: restart it (it may be dead)
WM_POWERBROADCAST = 0x0218
WGC_RETRY_S = 30.0  # a monitor that fell back to GDI tries GPU capture again after this
STATUS_REFRESH_S = 0.25
WGC_MIN_INTERVAL_S = 0.016  # GPU capture: polling is nearly free, so poll up to ~60x per second
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
    tint: float = 0.0  # current blue-light filter strength in percent
    start: float | None = None  # thresholds in effect (None: dimming off), for the meter
    full: float | None = None
    spot: float = 0.0  # brightest 128 px block
    capture: str = ""  # "GPU" (Windows.Graphics.Capture) or "GDI" (fallback)


@dataclass
class Status:
    running: bool = False
    paused: bool = False
    paused_reason: str = ""  # "" or "user"
    monitors: list[MonitorStatus] = field(default_factory=list)
    error: str | None = None
    hotkey_ok: bool = False
    heartbeat: float = 0.0  # time.monotonic() of the last engine loop round


class _GpuContext:
    """One D3D11 device + its WinRT wrapper, shared by all monitor captures (engine thread)."""

    def __init__(self) -> None:
        self.gpu = GpuBrightness()
        try:
            self.device = wgc.WinrtDevice(self.gpu)
        except Exception:
            self.gpu.close()
            raise

    def close(self) -> None:
        self.device.close()
        self.gpu.close()


class _Slot:
    """Per-monitor state owned by the engine thread."""

    def __init__(
        self, monitor: Monitor, index: int, gpu: _GpuContext | None = None, min_interval: float = 0.05
    ) -> None:
        self.monitor = monitor
        self.min_interval = min(min_interval, 0.033)  # compositor frame cap (bursts/local need ~30/s)
        self.index = index
        self.gpu = gpu
        self.capture: wgc.MonitorCapture | None = None
        self.wgc_retry_at = 0.0
        self.wgc_failures = 0
        self.processed_at = 0.0
        self.stats = FrameStats.flat(0.0)
        self.tiles: np.ndarray | None = None  # last GPU tile sums (for local dimming)
        self.frame_size = (0, 0)
        self.local: LocalDimmer | None = None
        self.local_failed = False
        self.local_retry_at = 0.0
        self.local_hidden_at: float | None = None  # hidden since (layer is freed after a while)
        self.mask: np.ndarray | None = None  # what the local layer shows
        self.mask_target: np.ndarray | None = None  # what the current picture asks for
        self._mask_moving = False
        self._mask_shown_at = 0.0
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
        self.computed_for: Effective | None = None  # profile values the current target was computed for
        self._fingerprint: np.ndarray | None = None
        self._fingerprint_spot = False
        self.effective = Effective()
        self.tint_strength = 0.0  # current, fades toward effective.tint_strength
        self.tint_kelvin = 3400.0
        self.gamma = GammaTint(monitor.gdi_name)  # preferred: warm white without a coloured veil
        self.start_gpu_capture()

    # ---- measuring -------------------------------------------------------------------------
    @property
    def backend(self) -> str:
        return "GPU" if self.capture else "GDI"

    def start_gpu_capture(self, quiet: bool = False) -> None:
        if self.gpu is None or self.capture is not None:
            return
        if not self.dim.excluded:
            return  # overlay would be in the frames: the GDI path with compensation handles that
        try:
            self.capture = wgc.MonitorCapture(self.gpu.gpu, self.gpu.device, self.monitor.hmonitor)
            self.capture.set_min_interval(self.min_interval)
            self.forget_frame()
            self.computed_for = None
            self.wgc_failures = 0
            (log.debug if quiet else log.info)("%s: GPU capture (Windows.Graphics.Capture)", self.monitor.gdi_name)
        except Exception as e:
            self.capture = None
            self.wgc_failures += 1
            self.wgc_retry_at = time.monotonic() + WGC_RETRY_S
            # warn once; later retries of the same problem go to the debug log only
            (log.debug if quiet or self.wgc_failures > 1 else log.warning)(
                "%s: GPU capture not available (%s), using GDI", self.monitor.gdi_name, e
            )

    def stop_gpu_capture(self) -> None:
        if self.capture is not None:
            try:
                self.capture.close()
            except Exception:
                log.debug("capture close failed", exc_info=True)
            self.capture = None

    def sample(self, want_tiles: bool, process_interval: float) -> FrameStats | None:
        """Statistics of the current picture, or None when it did not change.

        GPU capture: polled at display rate, but measured at most every ``process_interval``
        (continuous change such as video would otherwise cost GPU work 60x per second). The
        first frame after a still phase is measured at once, so flashes are not delayed.
        """
        if self.capture is not None:
            now = time.perf_counter()
            due = now - self.processed_at >= process_interval and not self.capture.pending_gpu
            try:
                submitted = self.capture.submissions
                result = self.capture.poll(process=due)
                if self.capture.submissions != submitted:
                    self.processed_at = now  # the measuring slot starts with the submission
            except Exception as e:
                log.warning("%s: GPU capture failed (%s), switching to GDI", self.monitor.gdi_name, e)
                self.stop_gpu_capture()
                self.wgc_retry_at = time.monotonic() + WGC_RETRY_S
            else:
                if result is None:
                    return None  # nothing new, or a frame waits for its slot / the GPU
                tiles, w, h = result
                self.tiles, self.frame_size = tiles, (w, h)
                return frame_stats(tiles, w, h)
        m = self.monitor
        return self._sample_gdi(self.sampler.grab(m.left, m.top, m.width, m.height), want_tiles)

    def _sample_gdi(self, pixels: np.ndarray, want_tiles: bool) -> FrameStats | None:
        """GDI fallback: exact values, skipped when the frame is unchanged.

        A fixed coarse grid (every 8th pixel) is compared with the previous frame first; the
        exact sums (the most expensive step) only run when something changed. A change missing
        every grid point is smaller than 8x8 pixels and moves the mean by less than 0.01.
        """
        fingerprint = pixels[::FINGERPRINT_STEP, ::FINGERPRINT_STEP, :3]
        if (
            self._fingerprint is not None
            and self._fingerprint_spot == want_tiles
            and np.array_equal(fingerprint, self._fingerprint)
        ):
            return None
        self._fingerprint = fingerprint.copy()
        self._fingerprint_spot = want_tiles
        if want_tiles:  # tile statistics cost more on the CPU: only when a profile needs them
            h, w = pixels.shape[:2]
            return frame_stats(tile_sums(pixels), w, h)
        return FrameStats.flat(brightness(pixels))

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
        if monitor.hmonitor != self.monitor.hmonitor and monitor == self.monitor:
            self.monitor = monitor  # same place, new handle (e.g. after sleep): capture anew
            self.stop_gpu_capture()
            self.start_gpu_capture(quiet=True)
            return
        if monitor != self.monitor:
            sampler = Sampler()  # swap only once the new one exists
            self.gamma.reset()
            self.gamma = GammaTint(monitor.gdi_name)
            self.warm.move(monitor)
            self.dim.move(monitor)
            self.sampler.close()
            self.sampler = sampler
            self.monitor = monitor
            self.forget_frame()
            self.stop_local()  # the mask grid depends on the monitor size
            self.stop_gpu_capture()  # the monitor handle may have changed: capture it anew
            self.start_gpu_capture()

    def renew_sampler(self) -> None:
        sampler = Sampler()
        self.sampler.close()
        self.sampler = sampler
        self.forget_frame()

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
        rgb = kelvin_to_rgb(self.tint_kelvin)
        if not self.gamma.failed:
            try:
                if self.gamma.apply(channel_factors(rgb, self.tint_strength)):
                    self.warm.set_alpha(0)
                    return
            except OSError:
                self.gamma.failed = True
        self.warm.set_color(rgb)  # fallback: coloured overlay
        self.warm.set_alpha(round(self.tint_strength / 100 * 255))

    def keep_on_top(self) -> None:
        # Dimming first (even while hidden, so its z position is always the reference), then the
        # local layer and the tint directly below it: nothing ever covers the dimming.
        self.dim.keep_on_top(even_hidden=True)
        if self.local is not None:
            self.local.keep_on_top(even_hidden=True)
        self.warm.keep_on_top()
        self.gamma.reassert()

    # ---- local dimming ------------------------------------------------------------------------
    @property
    def local_active(self) -> bool:
        """The local mask is still moving toward its target (fading in or out)."""
        return self.mask is not None and self.mask_target is not None and self._mask_moving

    def update_local(self, dt: float, fresh: bool) -> None:
        """New frame: new mask target. Otherwise the picture is unchanged, so the target stays
        (a paused film keeps its bright spot covered) and only a running fade continues."""
        if self.tiles is None or self.gpu is None:
            return
        if self.local_failed:
            if time.monotonic() < self.local_retry_at:
                return
            self.local_failed = False  # transient problems (session switch, DWM restart) pass
        if fresh or self.mask_target is None:
            # self.tiles is always the newest picture: after pause/clear/restart the mask is
            # restored at once, not only when the picture changes again.
            w, h = self.frame_size
            self.mask_target = local_target(tile_means(self.tiles, w, h), self.stats.background, self.effective.local)
        now = time.perf_counter()
        if not fresh and now - self._mask_shown_at < LOCAL_REDRAW_S:
            return  # fades are redrawn at ~30 Hz, fast enough to look continuous
        mask = blend_mask(
            self.mask_target,
            self.mask,
            now - self._mask_shown_at if self.mask is not None else dt,
            self.effective.local.release_s,
        )
        if self.mask is not None and mask.shape == self.mask.shape and np.allclose(mask, self.mask, atol=0.004):
            # Nothing visible changes this round; keep going until the target is reached (the
            # next step gets a longer dt, so a slow fade still finishes and the layer hides).
            self._mask_moving = not np.allclose(self.mask, self.mask_target, atol=0.004)
            return
        self._mask_moving = not np.allclose(mask, self.mask_target, atol=0.004)
        self.mask = mask
        self._mask_shown_at = now
        try:
            if self.local is None:
                self.local = LocalDimmer(self.gpu.gpu, self.monitor, mask.shape)
                self.local.below = self.dim
                self.warm.below = self.local  # order: dimming > local layer > tint
            if self.local.grid_h != mask.shape[0] or self.local.grid_w != mask.shape[1]:
                self.stop_local()
                return
            if not self.local.show_mask(mask):
                self._mask_moving = True  # not presented yet: try again next round
        except Exception as e:
            log.warning("%s: local dimming not available (%s)", self.monitor.gdi_name, e)
            self.stop_local()
            self.local_failed = True
            self.local_retry_at = time.monotonic() + WGC_RETRY_S

    def stop_local(self) -> None:
        self.mask = self.mask_target = None
        self._mask_moving = False
        if self.local is not None:
            try:
                self.local.close()
            except Exception:
                log.debug("local layer close failed", exc_info=True)
            self.local = None
            self.warm.below = self.dim

    def check_local(self) -> None:
        """Periodic: free a layer hidden for long; rebuild one whose composition device died."""
        layer = self.local
        if layer is None:
            return
        if layer.visible:
            self.local_hidden_at = None
            if not layer.alive():
                log.info("%s: composition device lost, rebuilding the local layer", self.monitor.gdi_name)
                self.stop_local()
        elif self.local_hidden_at is None:
            self.local_hidden_at = time.monotonic()
        elif time.monotonic() - self.local_hidden_at > LOCAL_FREE_AFTER_S:
            self.stop_local()
            self.local_hidden_at = None

    def hide_local(self) -> None:
        self.mask = self.mask_target = None
        self._mask_moving = False
        if self.local is not None:
            self.local.hide()

    def clear(self) -> None:
        """Fail-safe: hide both layers; each one independently of the other."""
        self.smoother.reset(0.0)
        self.computed_for = None
        self.tint_strength = 0.0
        self.mask = self.mask_target = None
        self._mask_moving = False
        try:
            self.gamma.reset()
        except Exception:
            log.debug("could not reset gamma", exc_info=True)
        for layer in (self.overlay, self.tint, self.local):
            if layer is None:
                continue
            try:
                layer.hide()
            except Exception:
                log.debug("could not hide overlay", exc_info=True)

    def close(self) -> None:
        try:
            self.gamma.reset()
        except Exception:
            log.debug("could not reset gamma", exc_info=True)
        self.stop_local()
        self.stop_gpu_capture()
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
    text = f"Monitor {index + 1} · {m.width}×{m.height}"
    return text + " (primary)" if m.primary else text


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
        self._wake_now = False
        self._monitors_dirty = True  # first loop round enumerates the monitors
        self._force_refresh = False
        self._hook: list[int] = []
        self._hook_proc = WinEventProc(self._on_win_event)
        self._gpu: _GpuContext | None = None
        self._timer = 0  # high-resolution waitable timer, created on the engine thread
        self._resume_pending = False
        self._published_key: tuple = ()
        self._published_at = 0.0
        self.use_gpu = True  # tests can force the GDI path

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
            self._timer = kernel32.CreateWaitableTimerExW(
                None, None, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS
            ) or kernel32.CreateWaitableTimerExW(None, None, 0, TIMER_ALL_ACCESS)  # older Windows
            self._start_gpu()
            self._register_hotkey()
            self._install_hook()
            try:
                self._refresh_monitors()
                self._monitors_dirty = False
            except Exception:  # the loop retries; a vanishing monitor must not end the engine
                log.exception("Could not read the monitors at startup")
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
                base = self._base_interval()
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
                    if self._resume_pending:  # after sleep every capture session is suspect
                        self._resume_pending = False
                        for slot in self._slots.values():
                            slot.stop_gpu_capture()
                            slot.start_gpu_capture(quiet=True)
                    self._retry_gpu_captures()
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
                        slot.check_local()
                    next_topmost = now + TOPMOST_REFRESH_S
                if failures:
                    log.info("Measuring loop recovered")
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
                    log.exception("Error in the measuring loop")
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

    def _start_gpu(self) -> None:
        if not self.use_gpu:
            return
        try:
            if not wgc.init_thread() or not wgc.is_supported():
                raise OSError("Windows.Graphics.Capture not supported")
            self._gpu = _GpuContext()
        except Exception as e:
            self._gpu = None
            log.warning("GPU capture not available (%s), using GDI", e)

    def _retry_gpu_captures(self) -> None:
        """Rebuild a lost GPU device, restart dead or stale captures, retry fallen-back monitors."""
        if self._gpu is not None and self._gpu.gpu.is_lost():
            log.warning("Graphics device was reset, rebuilding GPU capture")
            self._restart_gpu()
            return
        now = time.monotonic()
        for slot in self._slots.values():
            cap = slot.capture
            if cap is not None and now - cap.last_frame_at > WGC_WATCHDOG_S and not self._paused:
                # No frame for a long time: a still screen, or a dead capture (monitor handle
                # changed, resume from sleep). A new session always delivers a current frame.
                slot.stop_gpu_capture()
                slot.start_gpu_capture(quiet=True)
            elif self._gpu and cap is None and now >= slot.wgc_retry_at:
                slot.start_gpu_capture(quiet=slot.wgc_failures > 0)

    def _restart_gpu(self) -> None:
        for slot in self._slots.values():
            slot.stop_local()
            slot.stop_gpu_capture()
            slot.gpu = None
        if self._gpu is not None:
            try:
                self._gpu.close()
            except Exception:
                log.debug("GPU close failed", exc_info=True)
            self._gpu = None
        self._start_gpu()
        for slot in self._slots.values():
            slot.gpu = self._gpu
            slot.local_failed = False
            slot.start_gpu_capture()

    def _all_gpu(self) -> bool:
        return bool(self._slots) and all(slot.capture is not None for slot in self._slots.values())

    def _base_interval(self) -> float:
        """GPU capture polls are nearly free (no new frame = no work), so they run at about the
        display rate: a flash is seen within one or two frames instead of one GDI interval."""
        base = self._settings.interval_ms / 1000.0
        if self._all_gpu():
            return max(WGC_MIN_INTERVAL_S, base / 3)
        return base

    def _any_moving(self) -> bool:
        return any(not slot.smoother.settled or slot.tint_moving(self._paused) for slot in self._slots.values())

    def _current_interval(self, calm_for: float) -> float:
        """Full rate while anything moves; a slower rate once every monitor has been still."""
        base = self._base_interval()
        if self._any_moving():
            return base  # a running fade always gets full rate, or it would show as steps
        if self._paused or not any(slot.effective.dim_on for slot in self._slots.values()):
            return max(base, IDLE_INTERVAL_MAX_S)  # nothing to measure
        if calm_for >= IDLE_AFTER_S and not self._all_gpu():  # GDI only: a still screen is measured less often
            return min(max(base, base * IDLE_FACTOR), max(base, IDLE_INTERVAL_MAX_S))
        return base

    def _tick(self, dt: float) -> bool:
        """One measurement/update round. Returns True when something changed noticeably."""
        active = False
        touched = False
        for slot in self._slots.values():
            e = slot.effective
            smoother = slot.smoother
            if (self._paused or not e.dim_on) and slot.capture is not None:
                try:
                    slot.capture.poll(process=False)  # keep the pool drained: no stale frames later
                except Exception:
                    log.debug("drain failed", exc_info=True)
            if self._paused:
                slot.target = 0.0
                smoother.reset(0.0)  # user asked for it: off at once
                slot.tint_strength = 0.0
                slot.computed_for = None  # on resume the target is recomputed, frame or not
            elif not e.dim_on:
                slot.target = 0.0
                smoother.step(0.0, dt, skip_hold=True)  # fade out gently, no capture needed
            else:
                try:
                    want_tiles = e.glare_weight > 0 or e.glare_local
                    interval = self._settings.interval_ms / 1000.0
                    if slot.local_active:
                        interval = 0.033  # a visible local mask follows every frame (moving light)
                    stats = slot.sample(want_tiles, interval)
                    if slot.capture_failures:
                        slot.capture_failures = 0
                        slot.computed_for = None  # capture is back: recompute even if unchanged
                except OSError as err:  # e.g. secure desktop (UAC, lock screen): keep last state
                    self._capture_failed(slot, err)
                    if slot.capture_failures >= CAPTURE_RENEW_AFTER:
                        smoother.step(0.0, dt)
                        slot.dim.set_alpha(round(smoother.value))
                    slot.step_tint(dt, off=False)
                    continue
                if (
                    stats is None
                    and e is slot.computed_for
                    and smoother.settled
                    and slot.dim.excluded
                    and not slot.tint_moving(False)
                    and not slot.local_active
                ):
                    continue  # fast path: no new frame, nothing moving, same profile -> no work
                slot.computed_for = e
                if stats is not None:
                    if (
                        abs(stats.mean - slot.stats.mean) > BURST_DELTA
                        or abs(stats.spot - slot.stats.spot) > BURST_DELTA
                    ):
                        # The picture is in the middle of a big change (a page loading, a flash):
                        # measure the next frame at once instead of waiting for the measuring slot.
                        slot.processed_at = 0.0
                    slot.stats = stats
                weight = e.glare_weight
                if e.glare_local and (slot.capture is None or slot.local_failed):
                    weight = GLARE_WEIGHTS[2]  # no GPU tiles for a local mask: protect globally instead
                level = glare_level(slot.stats, weight)
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
                if e.glare_local and slot.capture is not None:
                    slot.update_local(dt, fresh=stats is not None)
                elif slot.local is not None:
                    slot.hide_local()
            if (self._paused or not e.dim_on) and slot.local is not None:
                slot.hide_local()
            touched = True
            slot.dim.set_alpha(round(smoother.value))
            slot.step_tint(dt, off=self._paused)
            if not smoother.settled or slot.tint_moving(self._paused):
                active = True
        if touched or time.monotonic() - self._published_at >= STATUS_REFRESH_S:
            self._publish_if_changed()
        return active

    def _capture_failed(self, slot: _Slot, error: OSError) -> None:
        slot.capture_failures += 1
        slot.computed_for = None
        log.debug("capture failed on %s: %s", slot.monitor.gdi_name, error)
        if slot.capture_failures % CAPTURE_RENEW_AFTER == 0:
            # Persistent failure (not just a short UAC prompt): get a fresh screen DC and, so a
            # stale dark overlay cannot linger unnoticed, fade it out until capture works again.
            log.warning("Monitor %s cannot be measured", slot.monitor.gdi_name)
            slot.target = 0.0
            slot.renew_sampler()

    def _wait(self, seconds: float) -> None:
        """Sleep until the next deadline or a message, whichever comes first.

        A plain timeout is rounded up to the 15.6 ms system tick (a 16 ms wait becomes 31 ms).
        A high-resolution waitable timer is precise without changing the system-wide timer
        resolution (no timeBeginPeriod, no extra power use elsewhere).
        """
        if self._wake_now or self._stop_event.is_set() or seconds <= 0:
            return
        timer = self._timer
        if timer:
            due = ctypes.c_longlong(-max(1, int(seconds * 10_000_000)))  # relative, 100 ns units
            if kernel32.SetWaitableTimer(timer, ctypes.byref(due), 0, None, None, False):
                handles = (wintypes.HANDLE * 1)(timer)
                user32.MsgWaitForMultipleObjects(1, handles, False, math.ceil(seconds * 1000) + 20, QS_ALLINPUT)
                return
        user32.MsgWaitForMultipleObjects(0, None, False, max(1, math.ceil(seconds * 1000)), QS_ALLINPUT)

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
        if s.interval_ms != old.interval_ms:
            for slot in self._slots.values():
                slot.min_interval = min(s.interval_ms / 1000.0, 0.033)
                if slot.capture is not None:
                    slot.capture.set_min_interval(slot.min_interval)
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
            log.info("Paused" if paused else "Resumed")
            for slot in self._slots.values():
                slot.computed_for = None  # the target must be recomputed, even without a new frame
        self._wake_now = True
        self._publish()

    def _resolve_profiles(self) -> None:
        """Apply the profile to every monitor; the night window may switch the tint on or off."""
        lt = time.localtime()
        minute = lt.tm_hour * 60 + lt.tm_min + lt.tm_sec / 60
        effective = resolve(self._settings.profile, minute)
        for slot in self._slots.values():
            if effective.tint_on != slot.effective.tint_on:
                log.info("%s: blue light filter %s", slot.monitor.gdi_name, "on" if effective.tint_on else "off")
            if effective != slot.effective:
                old = slot.effective
                slot.effective = effective
                slot.smoother.attack = ATTACK_PRESETS[effective.attack]
                slot.smoother.release = RELEASE_PRESETS[effective.release]
                if old.local != effective.local and slot.mask is not None:
                    slot.mask_target = None  # recompute with the new spot settings, even on a still picture
                    slot._mask_moving = True
                if (old.dim_on, old.tint_on) != (effective.dim_on, effective.tint_on):
                    self._wake_now = True

    def _refresh_monitors(self, force: bool = False) -> None:
        monitors = list_monitors()
        if not force and monitors == self._monitors:
            return
        if monitors != self._monitors:
            log.info("Monitors found: %s", ", ".join(f"{m.gdi_name} {m.width}x{m.height}" for m in monitors))
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
            slot = _Slot(monitor, index, self._gpu, self._settings.interval_ms / 1000.0)
            self._slots[device] = slot
            self._resolve_profiles()
            if not slot.dim.excluded:
                log.warning("Overlay cannot be excluded from capture, compensating")
        with self._lock:
            self._monitors = monitors
        self._publish()

    def _publish_if_changed(self) -> None:
        """At display rate most rounds change nothing; the GUI needs at most ~10 updates/s."""
        key = tuple(
            (s.dim.alpha, round(s.tint_strength), round(s.stats.mean), round(s.stats.spot))
            for s in self._slots.values()
        )
        now = time.monotonic()
        if (
            key != self._published_key and now - self._published_at >= 0.05
        ) or now - self._published_at >= STATUS_REFRESH_S:
            self._published_key, self._published_at = key, now
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
                tint=slot.tint_strength,
                start=slot.effective.start if slot.effective.dim_on else None,
                full=slot.effective.full if slot.effective.dim_on else None,
                spot=slot.stats.spot,
                capture=slot.backend,
            )
            for d, slot in sorted(self._slots.items(), key=lambda kv: index.get(kv[0], 99))
        ]
        with self._lock:
            self._status.paused = self._paused
            self._status.paused_reason = "user" if self._paused else ""
            self._status.monitors = rows
            self._status.heartbeat = time.monotonic()

    # ---- Win32 plumbing ----------------------------------------------------------------
    def _on_window_message(self, msg: int, _wp: int, _lp: int) -> None:
        self._monitors_dirty = True
        if msg == WM_POWERBROADCAST:
            self._resume_pending = True
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
                log.warning("Hotkey Ctrl+Alt+D is already used by another program")
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
        if self._gpu is not None:
            try:
                self._gpu.close()
            except Exception:
                log.debug("GPU cleanup failed", exc_info=True)
            self._gpu = None
        for h in self._hook:
            if h:
                user32.UnhookWinEvent(h)
        self._unregister_hotkey()
        overlay_mod.set_message_hook(None)
        if self._timer:
            kernel32.CloseHandle(self._timer)
            self._timer = 0
        with self._lock:
            self._status.running = False
            self._status.monitors = []
