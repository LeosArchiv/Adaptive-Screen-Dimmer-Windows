"""Monitor capture through Windows.Graphics.Capture (WGC), plain ctypes WinRT/COM.

Why WGC: frames arrive only when the screen content changes (a still screen costs nothing),
capture runs on the GPU and works across adapters (hybrid laptops, external monitors), and
windows excluded from capture (our overlays) are left out. Frames are reduced to a brightness
value on the GPU (see gpu.py), so no image data is copied to the CPU.

Must be used from one thread (the engine thread); that thread must initialise WinRT (MTA).
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int32, c_uint, c_void_p, wintypes

import numpy as np

from .gpu import (
    GUID,
    HRESULT,
    GpuBrightness,
    GpuError,
    IID_ID3D11Texture2D,
    IID_IDXGIDevice,
    check,
    query,
    release,
    vcall,
)

IID_IGraphicsCaptureItemInterop = GUID.parse("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
IID_IGraphicsCaptureItem = GUID.parse("79C3F95B-31F7-4EC2-A464-632EF5D30760")
IID_IFramePoolStatics2 = GUID.parse("589B103F-6BBC-5DF5-A991-02E28B3B66D5")
IID_ISessionStatics = GUID.parse("2224A540-5974-49AA-B232-0882536F4CB5")
IID_ISession2 = GUID.parse("2C39AE40-7D2E-5044-804E-8B6799D4CF9E")
IID_ISession3 = GUID.parse("F2CDD966-22AE-5EA1-9596-3A289344C3BE")
IID_ISession5 = GUID.parse("67C0EA62-1F85-5061-925A-239BE0AC09CB")  # MinUpdateInterval (Win11 24H2+)
IID_IClosable = GUID.parse("30D5A829-7FA4-4026-83BB-D75BAE4EA99E")
IID_IDxgiInterfaceAccess = GUID.parse("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
IID_IDirect3DDevice = GUID.parse("A37624AB-8D5F-4650-9D3E-9EAE3D9BC670")

PIXEL_FORMAT_B8G8R8A8 = 87
BUFFERS = 2


class SizeInt32(ctypes.Structure):
    _fields_ = [("Width", c_int32), ("Height", c_int32)]


_combase = ctypes.WinDLL("combase")
_combase.RoInitialize.argtypes = [c_uint]
_combase.RoInitialize.restype = HRESULT
_combase.RoUninitialize.argtypes = []
_combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, c_uint, POINTER(c_void_p)]
_combase.WindowsCreateString.restype = HRESULT
_combase.WindowsDeleteString.argtypes = [c_void_p]
_combase.RoGetActivationFactory.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]
_combase.RoGetActivationFactory.restype = HRESULT
_d3d11 = ctypes.WinDLL("d3d11")
_d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [c_void_p, POINTER(c_void_p)]
_d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype = HRESULT


def init_thread() -> bool:
    """Initialise WinRT (multithreaded apartment) for the calling thread."""
    hr = _combase.RoInitialize(1)
    return hr >= 0 or hr == -2147417850  # RPC_E_CHANGED_MODE: already initialised differently


def _factory(class_name: str, iid: GUID) -> c_void_p:
    hs = c_void_p()
    check(_combase.WindowsCreateString(class_name, len(class_name), byref(hs)), "WindowsCreateString")
    try:
        out = c_void_p()
        check(_combase.RoGetActivationFactory(hs, byref(iid), byref(out)), f"factory {class_name}")
        return out
    finally:
        _combase.WindowsDeleteString(hs)


def is_supported() -> bool:
    try:
        statics = _factory("Windows.Graphics.Capture.GraphicsCaptureSession", IID_ISessionStatics)
    except OSError:
        return False
    try:
        ok = ctypes.c_bool()
        return vcall(statics, 6, HRESULT, [POINTER(ctypes.c_bool)], byref(ok)) >= 0 and bool(ok.value)
    finally:
        release(statics)


def _close(obj: c_void_p) -> None:
    if not obj or not obj.value:
        return
    try:
        closable = query(obj, IID_IClosable)
        vcall(closable, 6, HRESULT, [])
        release(closable)
    except OSError:
        pass


class WinrtDevice:
    """The IDirect3DDevice wrapper WGC needs, made from the GPU helper's D3D11 device."""

    def __init__(self, gpu: GpuBrightness) -> None:
        dxgi = query(gpu.device, IID_IDXGIDevice)
        try:
            inspectable = c_void_p()
            check(
                _d3d11.CreateDirect3D11DeviceFromDXGIDevice(dxgi, byref(inspectable)),
                "CreateDirect3D11DeviceFromDXGIDevice",
            )
        finally:
            release(dxgi)
        try:
            self.ptr = query(inspectable, IID_IDirect3DDevice)
        finally:
            release(inspectable)

    def close(self) -> None:
        release(self.ptr)
        self.ptr = c_void_p()


class MonitorCapture:
    """Captures one monitor; ``poll`` returns the brightness of the newest frame, or None."""

    def __init__(self, gpu: GpuBrightness, device: WinrtDevice, hmonitor: int) -> None:
        self.gpu = gpu
        self.device = device
        self.item = self.pool = self.session = c_void_p()
        self._held = c_void_p()
        interop = _factory("Windows.Graphics.Capture.GraphicsCaptureItem", IID_IGraphicsCaptureItemInterop)
        try:
            item = c_void_p()
            check(
                vcall(
                    interop,
                    4,
                    HRESULT,
                    [c_void_p, POINTER(GUID), POINTER(c_void_p)],
                    hmonitor,
                    byref(IID_IGraphicsCaptureItem),
                    byref(item),
                ),
                "CreateForMonitor",
            )
            self.item = item
        finally:
            release(interop)
        try:
            self.size = SizeInt32()
            check(vcall(self.item, 7, HRESULT, [POINTER(SizeInt32)], byref(self.size)), "GraphicsCaptureItem.Size")
            statics = _factory("Windows.Graphics.Capture.Direct3D11CaptureFramePool", IID_IFramePoolStatics2)
            try:
                pool = c_void_p()
                check(
                    vcall(
                        statics,
                        6,
                        HRESULT,
                        [c_void_p, c_int32, c_int32, SizeInt32, POINTER(c_void_p)],
                        device.ptr,
                        PIXEL_FORMAT_B8G8R8A8,
                        BUFFERS,
                        self.size,
                        byref(pool),
                    ),
                    "CreateFreeThreaded",
                )
                self.pool = pool
            finally:
                release(statics)
            session = c_void_p()
            check(
                vcall(self.pool, 10, HRESULT, [c_void_p, POINTER(c_void_p)], self.item, byref(session)),
                "CreateCaptureSession",
            )
            self.session = session
            self.border_hidden = self._set_flag(IID_ISession3, False)  # no yellow capture border
            self._set_flag(IID_ISession2, False)  # no mouse cursor in the frames
            self.throttled = False
            check(vcall(self.session, 6, HRESULT, []), "StartCapture")
        except Exception:
            self.close()
            raise

    def set_min_interval(self, seconds: float) -> bool:
        """Let the compositor itself deliver frames no faster than this (Windows 11 24H2+).

        Saves the GPU copies of frames we would skip anyway. Returns False where unsupported;
        the caller's own rate limit then does the job.
        """
        try:
            iface = query(self.session, IID_ISession5)
        except OSError:
            return False
        try:
            ticks = ctypes.c_int64(max(0, int(seconds * 10_000_000)))  # TimeSpan: 100 ns units
            self.throttled = vcall(iface, 7, HRESULT, [ctypes.c_int64], ticks) >= 0
            return self.throttled
        finally:
            release(iface)

    def _set_flag(self, iid: GUID, value: bool) -> bool:
        try:
            iface = query(self.session, iid)
        except OSError:
            return False  # older Windows: property not available
        try:
            return vcall(iface, 7, HRESULT, [ctypes.c_bool], value) >= 0
        finally:
            release(iface)

    def poll(self, process: bool = True) -> tuple[np.ndarray, int, int] | None:
        """(tile sums, width, height) of the newest frame, or None.

        Frames are drained on every call and only the newest one is kept, so no buffer ever
        fills up and the final state of a change is never dropped. It is measured (the GPU
        work) only when ``process`` is true; otherwise it waits for the next measuring slot.
        """
        while True:
            frame = c_void_p()
            check(vcall(self.pool, 7, HRESULT, [POINTER(c_void_p)], byref(frame)), "TryGetNextFrame")
            if not frame.value:
                break
            self._drop_held()
            self._held = frame
        if not process or not self._held.value:
            return None
        try:
            return self._measure(self._held)
        finally:
            self._drop_held()

    @property
    def pending(self) -> bool:
        """A newer frame is waiting to be measured."""
        return bool(self._held.value)

    def _drop_held(self) -> None:
        if self._held.value:
            _close(self._held)
            release(self._held)
            self._held = c_void_p()

    def _measure(self, frame: c_void_p) -> tuple[np.ndarray, int, int]:
        content = SizeInt32()
        check(vcall(frame, 8, HRESULT, [POINTER(SizeInt32)], byref(content)), "ContentSize")
        if (content.Width, content.Height) != (self.size.Width, self.size.Height) and content.Width > 0:
            self.size = content  # resolution changed: new buffers for the next frames
            check(
                vcall(
                    self.pool,
                    6,
                    HRESULT,
                    [c_void_p, c_int32, c_int32, SizeInt32],
                    self.device.ptr,
                    PIXEL_FORMAT_B8G8R8A8,
                    BUFFERS,
                    self.size,
                ),
                "Recreate",
            )
        surface = c_void_p()
        check(vcall(frame, 6, HRESULT, [POINTER(c_void_p)], byref(surface)), "Surface")
        try:
            access = query(surface, IID_IDxgiInterfaceAccess)
            try:
                texture = c_void_p()
                check(
                    vcall(
                        access,
                        3,
                        HRESULT,
                        [POINTER(GUID), POINTER(c_void_p)],
                        byref(IID_ID3D11Texture2D),
                        byref(texture),
                    ),
                    "GetInterface",
                )
            finally:
                release(access)
            try:
                w = max(1, min(content.Width, self.size.Width))
                h = max(1, min(content.Height, self.size.Height))
                return self.gpu.tiles_of_texture(texture, w, h, full_copy=False), w, h
            finally:
                release(texture)
        finally:
            release(surface)

    def close(self) -> None:
        self._drop_held()
        for name in ("session", "pool"):
            obj = getattr(self, name)
            _close(obj)
            release(obj)
            setattr(self, name, c_void_p())
        release(self.item)
        self.item = c_void_p()


__all__ = ["GpuError", "MonitorCapture", "WinrtDevice", "init_thread", "is_supported"]
