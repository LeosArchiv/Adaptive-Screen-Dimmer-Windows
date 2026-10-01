"""Thin Win32 helpers: DPI awareness, monitor enumeration, cheap screen sampling, foreground app."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field

import numpy as np
import win32api
import win32con

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11

user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC] + [ctypes.c_int] * 4 + [wintypes.HDC] + [ctypes.c_int] * 2 + [wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def enable_dpi_awareness() -> None:
    """Per-monitor v2 so monitor rects, window positions and captures share physical pixels."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


@dataclass(frozen=True)
class Monitor:
    device: str  # stable monitor interface id, survives reboots and re-plugging
    left: int
    top: int
    width: int
    height: int
    primary: bool
    gdi_name: str = ""  # e.g. "\\\\.\\DISPLAY2"; Windows renumbers these, so only for logs
    hmonitor: int = field(default=0, compare=False)  # handle for capture APIs

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


_id_cache: dict[tuple[str, tuple[int, int, int, int]], str] = {}


def _stable_id(gdi_name: str, rect: tuple[int, int, int, int]) -> str:
    """Device interface path of the first monitor on this output, e.g. \\\\?\\DISPLAY#HWP2949#...

    Cached per output name and rectangle: the periodic check then costs no driver query.
    """
    key = (gdi_name, rect)
    cached = _id_cache.get(key)
    if cached:
        return cached
    stable = _query_stable_id(gdi_name)
    if stable != gdi_name:  # never cache the fallback: the real id may be readable a moment later
        _id_cache[key] = stable
    return stable


def clear_monitor_id_cache() -> None:
    """Called on display changes: another monitor may now sit on the same output."""
    _id_cache.clear()


def _query_stable_id(gdi_name: str) -> str:
    try:
        dev = win32api.EnumDisplayDevices(gdi_name, 0, 1)  # EDD_GET_DEVICE_INTERFACE_NAME
        if dev.DeviceID:
            return str(dev.DeviceID)
    except win32api.error:
        pass
    return gdi_name


def list_monitors() -> list[Monitor]:
    """All monitors, primary first, then left to right."""
    result = []
    for hmon, _hdc, _rect in win32api.EnumDisplayMonitors(None, None):
        info = win32api.GetMonitorInfo(hmon)
        left, top, right, bottom = info["Monitor"]
        gdi_name = str(info["Device"])
        result.append(
            Monitor(
                device=_stable_id(gdi_name, (left, top, right, bottom)),
                gdi_name=gdi_name,
                hmonitor=int(hmon),
                left=left,
                top=top,
                width=right - left,
                height=bottom - top,
                primary=bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY),
            )
        )
    result.sort(key=lambda m: (not m.primary, m.left, m.top))
    return result


def exclude_from_capture(hwnd: int) -> bool:
    """Hide a window from screenshots (Windows 10 2004+). Returns False when unsupported."""
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


class Sampler:
    """Copies a screen region 1:1 into a reusable BGRA buffer.

    GDI's own downscaling is no cheaper (HALFTONE) or aliases badly on text and grids
    (COLORONCOLOR), which would make the brightness, and with it the overlay, pump while
    scrolling. So the full region is copied and averaged exactly. Single-thread use only.
    """

    def __init__(self, source_dc: int | None = None) -> None:
        self._size: tuple[int, int] = (0, 0)
        self._bitmap = None
        self._old = None
        self._pixels: np.ndarray | None = None
        self._own_dc = source_dc is None
        self.src_dc = user32.GetDC(None) if source_dc is None else source_dc
        self.mem_dc = gdi32.CreateCompatibleDC(self.src_dc) if self.src_dc else None
        if not self.src_dc or not self.mem_dc:
            self.close()
            raise OSError("could not create a device context for screen capture")

    def _ensure(self, w: int, h: int) -> np.ndarray:
        if self._size != (w, h) or self._pixels is None:
            self._free_bitmap()
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bits = ctypes.c_void_p()
            self._bitmap = gdi32.CreateDIBSection(
                self.mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not self._bitmap or not bits.value:
                raise OSError("CreateDIBSection failed")
            self._old = gdi32.SelectObject(self.mem_dc, self._bitmap)
            buf = (ctypes.c_uint8 * (w * h * 4)).from_address(bits.value)
            self._pixels = np.ctypeslib.as_array(buf).reshape(h, w, 4)
            self._size = (w, h)
        return self._pixels

    def grab(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        """Returns a view of the internal buffer, valid until the next grab."""
        w, h = max(1, width), max(1, height)
        pixels = self._ensure(w, h)
        if not gdi32.BitBlt(self.mem_dc, 0, 0, w, h, self.src_dc, left, top, SRCCOPY):
            raise OSError(f"BitBlt failed ({ctypes.get_last_error()})")
        gdi32.GdiFlush()
        return pixels

    def _free_bitmap(self) -> None:
        if self._bitmap:
            gdi32.SelectObject(self.mem_dc, self._old)
            gdi32.DeleteObject(self._bitmap)
        self._bitmap = None
        self._pixels = None
        self._size = (0, 0)

    def close(self) -> None:
        self._free_bitmap()
        if self.mem_dc:
            gdi32.DeleteDC(self.mem_dc)
            self.mem_dc = None
        if self._own_dc and self.src_dc:
            user32.ReleaseDC(None, self.src_dc)
            self.src_dc = None
