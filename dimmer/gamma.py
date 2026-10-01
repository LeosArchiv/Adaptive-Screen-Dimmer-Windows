"""Blue-light filter through the monitor's gamma ramp.

A coloured overlay adds orange light on top of the picture: blacks turn brownish and contrast
drops. Scaling the colour channels in the gamma ramp instead keeps black black and only makes
white warmer (the way Night Light and f.lux work), and it works on every monitor whose driver
accepts a ramp, external ones included. The ramp is not saved anywhere: it is reset when the
app ends, and Windows resets it itself on sign-out, reboot and display changes.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger(__name__)

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.CreateDCW.restype = wintypes.HDC
gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

Ramp = ctypes.c_ushort * (3 * 256)

_active: set[GammaTint] = set()


def channel_factors(rgb: tuple[int, int, int], strength: float) -> tuple[float, float, float]:
    """Per-channel gain: strength 0 = unchanged, 100 = the full colour of the black body."""
    s = max(0.0, min(1.0, strength / 100.0))
    r, g, b = (1.0 - s * (1.0 - c / 255.0) for c in rgb)
    return r, g, b


def build_ramp(factors: tuple[float, float, float]) -> ctypes.Array[ctypes.c_ushort]:
    ramp = Ramp()
    for ch, f in enumerate(factors):
        for i in range(256):
            ramp[ch * 256 + i] = min(65535, round(i * 257 * f))
    return ramp


class GammaTint:
    """Gamma ramp of one monitor (by its GDI device name, e.g. \\\\.\\DISPLAY1)."""

    def __init__(self, device: str) -> None:
        self.device = device
        self.failed = False
        self._key: tuple[float, float, float] | None = None
        self._changed = False

    def apply(self, factors: tuple[float, float, float]) -> bool:
        """Set the ramp; False when the driver refuses it (the caller falls back)."""
        key = tuple(round(f, 3) for f in factors)
        if key == self._key or (key == (1.0, 1.0, 1.0) and not self._changed):
            return True
        if not self._set(build_ramp(factors)):
            if key != (1.0, 1.0, 1.0):
                log.info("%s: Farbfilter über Gamma nicht möglich, Overlay wird genutzt", self.device)
                self.failed = True
            return False
        self._key = key  # type: ignore[assignment]
        self._changed = key != (1.0, 1.0, 1.0)
        if self._changed:
            _active.add(self)
        else:
            _active.discard(self)
        return True

    def reassert(self) -> None:
        """Periodic: Windows drops the ramp on display changes, sleep or full-screen games."""
        if self._changed and self._key is not None:
            key = self._key
            self._key = None
            self.apply(key)

    def reset(self) -> None:
        if self._changed:  # never touch a ramp we did not change (e.g. Night Light's)
            self._set(build_ramp((1.0, 1.0, 1.0)))
        self._key = None
        self._changed = False
        _active.discard(self)

    def _set(self, ramp: ctypes.Array[ctypes.c_ushort]) -> bool:
        hdc = gdi32.CreateDCW("DISPLAY", self.device, None, None)
        if not hdc:
            return False
        try:
            return bool(gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))
        finally:
            gdi32.DeleteDC(hdc)


@atexit.register
def _reset_all() -> None:
    for tint in list(_active):
        try:
            tint.reset()
        except Exception:
            pass
