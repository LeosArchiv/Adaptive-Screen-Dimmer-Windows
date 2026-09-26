"""Exact mean brightness on the GPU (Direct3D 11 compute shader), plain ctypes COM.

A compute shader sums R+G+B of every pixel as integers per 16x16 tile; only the tile sums
(about 8 000 values for 1080p, 32 KB) travel back to the CPU instead of the whole frame.
The result is bit-identical to the CPU mean (integers, no rounding chain).
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_char_p, c_size_t, c_uint, c_void_p, wintypes

import numpy as np

HRESULT = ctypes.c_long


class GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> GUID:
        import uuid

        u = uuid.UUID(text)
        g = cls()
        g.d1, g.d2, g.d3 = u.fields[0], u.fields[1], u.fields[2]
        g.d4[:] = list(u.bytes[8:])
        return g


IID_ID3D11Texture2D = GUID.parse("6f15aaf2-d208-4e89-9ab4-489535d34f9c")
IID_IDXGIDevice = GUID.parse("54ec77fa-1377-44e6-8c32-88fd5f44c84c")


class GpuError(OSError):
    pass


def check(hr: int, what: str) -> None:
    if hr < 0:
        raise GpuError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")


def vcall(ptr: int | c_void_p, index: int, restype, argtypes, *args):
    """Call method ``index`` of the COM vtable behind ``ptr``."""
    p = ptr.value if isinstance(ptr, c_void_p) else ptr
    if not p:
        raise GpuError("null COM pointer")
    vtbl = ctypes.cast(p, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])
    return fn(p, *args)


def release(ptr: int | c_void_p | None) -> None:
    p = ptr.value if isinstance(ptr, c_void_p) else ptr
    if p:
        vcall(p, 2, ctypes.c_ulong, [])


def query(ptr: int | c_void_p, iid: GUID) -> c_void_p:
    out = c_void_p()
    check(vcall(ptr, 0, HRESULT, [POINTER(GUID), POINTER(c_void_p)], byref(iid), byref(out)), "QueryInterface")
    return out


# ---- D3D11 structures ------------------------------------------------------------------------
class D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [
        ("ByteWidth", c_uint),
        ("Usage", c_uint),
        ("BindFlags", c_uint),
        ("CPUAccessFlags", c_uint),
        ("MiscFlags", c_uint),
        ("StructureByteStride", c_uint),
    ]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", c_uint),
        ("Height", c_uint),
        ("MipLevels", c_uint),
        ("ArraySize", c_uint),
        ("Format", c_uint),
        ("SampleDesc", DXGI_SAMPLE_DESC),
        ("Usage", c_uint),
        ("BindFlags", c_uint),
        ("CPUAccessFlags", c_uint),
        ("MiscFlags", c_uint),
    ]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [(n, c_uint) for n in ("left", "top", "front", "right", "bottom", "back")]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", c_void_p), ("RowPitch", c_uint), ("DepthPitch", c_uint)]


class D3D11_BUFFER_UAV(ctypes.Structure):
    _fields_ = [("FirstElement", c_uint), ("NumElements", c_uint), ("Flags", c_uint)]


class D3D11_UNORDERED_ACCESS_VIEW_DESC(ctypes.Structure):
    _fields_ = [("Format", c_uint), ("ViewDimension", c_uint), ("Buffer", D3D11_BUFFER_UAV), ("_pad", c_uint * 2)]


class D3D11_TEX2D_SRV(ctypes.Structure):
    _fields_ = [("MostDetailedMip", c_uint), ("MipLevels", c_uint)]


class D3D11_SHADER_RESOURCE_VIEW_DESC(ctypes.Structure):
    _fields_ = [("Format", c_uint), ("ViewDimension", c_uint), ("Texture2D", D3D11_TEX2D_SRV), ("_pad", c_uint * 2)]


DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_UNKNOWN = 0
D3D11_USAGE_DEFAULT, D3D11_USAGE_STAGING = 0, 3
D3D11_BIND_SHADER_RESOURCE, D3D11_BIND_UNORDERED_ACCESS = 0x8, 0x80
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_RESOURCE_MISC_BUFFER_STRUCTURED = 0x40
D3D11_UAV_DIMENSION_BUFFER = 1
D3D11_SRV_DIMENSION_TEXTURE2D = 4
D3D11_MAP_READ = 1
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7

# vtable indices (d3d11.h order)
DEV_CREATE_BUFFER, DEV_CREATE_TEXTURE2D, DEV_CREATE_SRV, DEV_CREATE_UAV, DEV_CREATE_CS = 3, 5, 7, 8, 18
CTX_MAP, CTX_UNMAP, CTX_DISPATCH, CTX_COPY_SUBRESOURCE_REGION, CTX_COPY_RESOURCE = 14, 15, 41, 46, 47
CTX_FLUSH = 111  # ID3D11DeviceContext::Flush
DEV_GET_REMOVED_REASON = 39
FACTORY6_ENUM_BY_PREFERENCE = 29
ADAPTER_ENUM_OUTPUTS = 7
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D11_MAP_FLAG_DO_NOT_WAIT = 0x100000
DXGI_ERROR_WAS_STILL_DRAWING = -2005270518  # 0x887A000A
CTX_CS_SET_SRV, CTX_CS_SET_UAV, CTX_CS_SET_SHADER = 67, 68, 69
TEX_GET_DESC = 10  # ID3D11Texture2D::GetDesc (after IUnknown 3, DeviceChild 4, Resource 3)

SHADER = b"""
Texture2D<float4> src : register(t0);
RWStructuredBuffer<uint> sums : register(u0);
groupshared uint part[256];
[numthreads(16, 16, 1)]
void main(uint3 gid : SV_GroupID, uint gi : SV_GroupIndex, uint3 id : SV_DispatchThreadID)
{
    uint w, h;
    src.GetDimensions(w, h);
    uint v = 0;
    if (id.x < w && id.y < h) {
        float4 c = src.Load(int3(id.xy, 0));
        uint3 b = (uint3)round(saturate(c.rgb) * 255.0);
        v = b.r + b.g + b.b;
    }
    part[gi] = v;
    GroupMemoryBarrierWithGroupSync();
    [unroll] for (uint s = 128; s > 0; s >>= 1) {
        if (gi < s) part[gi] += part[gi + s];
        GroupMemoryBarrierWithGroupSync();
    }
    if (gi == 0) sums[gid.y * ((w + 15) / 16) + gid.x] = part[0];
}
"""

_d3d11 = ctypes.WinDLL("d3d11")
_d3d11.D3D11CreateDevice.restype = HRESULT
_d3d11.D3D11CreateDevice.argtypes = [
    c_void_p,
    c_uint,
    c_void_p,
    c_uint,
    c_void_p,
    c_uint,
    c_uint,
    POINTER(c_void_p),
    POINTER(c_uint),
    POINTER(c_void_p),
]


def _compile_shader() -> bytes:
    compiler = ctypes.WinDLL("d3dcompiler_47")
    compiler.D3DCompile.restype = HRESULT
    compiler.D3DCompile.argtypes = [
        c_char_p,
        c_size_t,
        c_char_p,
        c_void_p,
        c_void_p,
        c_char_p,
        c_char_p,
        c_uint,
        c_uint,
        POINTER(c_void_p),
        POINTER(c_void_p),
    ]
    code, errors = c_void_p(), c_void_p()
    hr = compiler.D3DCompile(
        SHADER, len(SHADER), b"sum", None, None, b"main", b"cs_5_0", 1 << 15, 0, byref(code), byref(errors)
    )
    if hr < 0:
        msg = ""
        if errors.value:
            msg = ctypes.string_at(vcall(errors, 3, c_void_p, []), vcall(errors, 4, c_size_t, [])).decode(
                errors="replace"
            )
            release(errors)
        raise GpuError(f"shader compile failed: {msg}")
    data = ctypes.string_at(vcall(code, 3, c_void_p, []), vcall(code, 4, c_size_t, []))
    release(code)
    return data


class GpuBrightness:
    """Owns one D3D11 device and the reduction shader; ``Reducer`` objects do the work."""

    def __init__(self) -> None:
        self.device, self.context, self.shader = c_void_p(), c_void_p(), c_void_p()
        self._test_reducer: Reducer | None = None
        try:
            self._init(low_power=True)
        except Exception:
            # e.g. an old integrated GPU without compute shader 5.0: use the default adapter
            self._release_device()
            try:
                self._init(low_power=False)
            except Exception:
                self.close()
                raise

    def _init(self, low_power: bool) -> None:
        self._create_device(low_power)
        code = _compile_shader()
        check(
            vcall(
                self.device,
                DEV_CREATE_CS,
                HRESULT,
                [c_char_p, c_size_t, c_void_p, POINTER(c_void_p)],
                code,
                len(code),
                None,
                byref(self.shader),
            ),
            "CreateComputeShader",
        )
        self._protect_multithreaded()

    def _release_device(self) -> None:
        for name in ("shader", "context", "device"):
            release(getattr(self, name))
            setattr(self, name, c_void_p())

    def _create_device(self, low_power: bool) -> None:
        """Prefer the low-power GPU: on hybrid laptops the dedicated GPU must not be kept awake."""
        adapter = _low_power_adapter() if low_power else c_void_p()
        level = c_uint()
        try:
            check(
                _d3d11.D3D11CreateDevice(
                    adapter,
                    D3D_DRIVER_TYPE_UNKNOWN if adapter.value else D3D_DRIVER_TYPE_HARDWARE,
                    None,
                    D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                    None,
                    0,
                    D3D11_SDK_VERSION,
                    byref(self.device),
                    byref(level),
                    byref(self.context),
                ),
                "D3D11CreateDevice",
            )
        finally:
            release(adapter)

    def _protect_multithreaded(self) -> None:
        """The free-threaded capture pool touches the device from system threads."""
        try:
            mt = query(self.context, IID_ID3D11Multithread)
        except OSError:
            return
        try:
            vcall(mt, 5, wintypes.BOOL, [wintypes.BOOL], True)
        finally:
            release(mt)

    def is_lost(self) -> bool:
        """Device removed (driver reset/update, GPU switched off): everything must be rebuilt."""
        if not self.device.value:
            return True
        return bool(vcall(self.device, DEV_GET_REMOVED_REASON, HRESULT, []) < 0)

    def reducer(self) -> Reducer:
        return Reducer(self)

    def tiles_of_texture(self, texture: c_void_p | int, w: int, h: int) -> np.ndarray:
        """Blocking helper (tests): tile sums of a whole BGRA8 texture."""
        if self._test_reducer is None:
            self._test_reducer = Reducer(self)
        self._test_reducer.submit(texture, w, h)
        result = self._test_reducer.collect(wait=True)
        assert result is not None
        return result

    def upload_for_test(self, img: np.ndarray) -> np.ndarray:
        """Test helper: upload a BGRA image and return its tile sums (validates the shader path)."""
        h, w = img.shape[:2]
        data = np.ascontiguousarray(img, dtype=np.uint8)

        class D3D11_SUBRESOURCE_DATA(ctypes.Structure):
            _fields_ = [("pSysMem", c_void_p), ("SysMemPitch", c_uint), ("SysMemSlicePitch", c_uint)]

        init = D3D11_SUBRESOURCE_DATA(data.ctypes.data, w * 4, 0)
        td = D3D11_TEXTURE2D_DESC(
            w,
            h,
            1,
            1,
            DXGI_FORMAT_B8G8R8A8_UNORM,
            DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT,
            D3D11_BIND_SHADER_RESOURCE,
            0,
            0,
        )
        tex = c_void_p()
        check(
            vcall(
                self.device,
                DEV_CREATE_TEXTURE2D,
                HRESULT,
                [POINTER(D3D11_TEXTURE2D_DESC), POINTER(D3D11_SUBRESOURCE_DATA), POINTER(c_void_p)],
                byref(td),
                byref(init),
                byref(tex),
            ),
            "CreateTexture2D (test)",
        )
        try:
            return self.tiles_of_texture(tex, w, h)
        finally:
            release(tex)

    def close(self) -> None:
        if self._test_reducer is not None:
            self._test_reducer.close()
            self._test_reducer = None
        for name in ("shader", "context", "device"):
            release(getattr(self, name))
            setattr(self, name, c_void_p())


class Reducer:
    """GPU buffers for one capture, so monitors of different sizes never share (and reallocate)
    them. ``submit`` queues copy + reduction; ``collect`` picks the result up without stalling
    the engine thread behind a busy GPU (e.g. a game), a round later if it is not ready yet."""

    def __init__(self, gpu: GpuBrightness) -> None:
        self.gpu = gpu
        self._size = (0, 0)
        self._tex = self._srv = self._buf = self._uav = self._staging = c_void_p()
        self._groups = self._gx = self._gy = 0
        self.pending = False

    def _create(self, method: int, argtypes: list, *args: object) -> c_void_p:
        out = c_void_p()
        check(vcall(self.gpu.device, method, HRESULT, [*argtypes, POINTER(c_void_p)], *args, byref(out)), "create")
        return out

    def _ensure(self, w: int, h: int) -> None:
        if self._size == (w, h):
            return
        self._free()
        td = D3D11_TEXTURE2D_DESC(
            w,
            h,
            1,
            1,
            DXGI_FORMAT_B8G8R8A8_UNORM,
            DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT,
            D3D11_BIND_SHADER_RESOURCE,
            0,
            0,
        )
        self._tex = self._create(DEV_CREATE_TEXTURE2D, [POINTER(D3D11_TEXTURE2D_DESC), c_void_p], byref(td), None)
        sd = D3D11_SHADER_RESOURCE_VIEW_DESC(
            DXGI_FORMAT_B8G8R8A8_UNORM, D3D11_SRV_DIMENSION_TEXTURE2D, D3D11_TEX2D_SRV(0, 1)
        )
        self._srv = self._create(
            DEV_CREATE_SRV, [c_void_p, POINTER(D3D11_SHADER_RESOURCE_VIEW_DESC)], self._tex, byref(sd)
        )
        gx, gy = (w + 15) // 16, (h + 15) // 16
        self._groups = gx * gy
        bd = D3D11_BUFFER_DESC(
            self._groups * 4,
            D3D11_USAGE_DEFAULT,
            D3D11_BIND_UNORDERED_ACCESS,
            0,
            D3D11_RESOURCE_MISC_BUFFER_STRUCTURED,
            4,
        )
        self._buf = self._create(DEV_CREATE_BUFFER, [POINTER(D3D11_BUFFER_DESC), c_void_p], byref(bd), None)
        ud = D3D11_UNORDERED_ACCESS_VIEW_DESC(
            DXGI_FORMAT_UNKNOWN, D3D11_UAV_DIMENSION_BUFFER, D3D11_BUFFER_UAV(0, self._groups, 0)
        )
        self._uav = self._create(
            DEV_CREATE_UAV, [c_void_p, POINTER(D3D11_UNORDERED_ACCESS_VIEW_DESC)], self._buf, byref(ud)
        )
        sb = D3D11_BUFFER_DESC(self._groups * 4, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, 0, 0)
        self._staging = self._create(DEV_CREATE_BUFFER, [POINTER(D3D11_BUFFER_DESC), c_void_p], byref(sb), None)
        self._size = (w, h)
        self._gx, self._gy = gx, gy

    def submit(self, texture: c_void_p | int, w: int, h: int) -> None:
        """Queue: copy the top-left w x h of a BGRA8 texture, reduce it, copy the sums out."""
        self._ensure(w, h)
        ctx = self.gpu.context
        box = D3D11_BOX(0, 0, 0, w, h, 1)
        vcall(
            ctx,
            CTX_COPY_SUBRESOURCE_REGION,
            None,
            [c_void_p, c_uint, c_uint, c_uint, c_uint, c_void_p, c_uint, POINTER(D3D11_BOX)],
            self._tex,
            0,
            0,
            0,
            0,
            texture,
            0,
            byref(box),
        )
        srvs = (c_void_p * 1)(self._srv.value)
        uavs = (c_void_p * 1)(self._uav.value)
        vcall(ctx, CTX_CS_SET_SHADER, None, [c_void_p, c_void_p, c_uint], self.gpu.shader, None, 0)
        vcall(ctx, CTX_CS_SET_SRV, None, [c_uint, c_uint, c_void_p], 0, 1, srvs)
        vcall(ctx, CTX_CS_SET_UAV, None, [c_uint, c_uint, c_void_p, c_void_p], 0, 1, uavs, None)
        vcall(ctx, CTX_DISPATCH, None, [c_uint, c_uint, c_uint], self._gx, self._gy, 1)
        null = (c_void_p * 1)(None)
        vcall(ctx, CTX_CS_SET_SRV, None, [c_uint, c_uint, c_void_p], 0, 1, null)
        vcall(ctx, CTX_CS_SET_UAV, None, [c_uint, c_uint, c_void_p, c_void_p], 0, 1, null, None)
        vcall(ctx, CTX_COPY_RESOURCE, None, [c_void_p, c_void_p], self._staging, self._buf)
        vcall(ctx, CTX_FLUSH, None, [])
        self.pending = True

    def collect(self, wait: bool = False) -> np.ndarray | None:
        """Tile sums of the last submission; None while the GPU is still working on it."""
        if not self.pending:
            return None
        ctx = self.gpu.context
        mapped = D3D11_MAPPED_SUBRESOURCE()
        flags = 0 if wait else D3D11_MAP_FLAG_DO_NOT_WAIT
        hr = vcall(
            ctx,
            CTX_MAP,
            HRESULT,
            [c_void_p, c_uint, c_uint, c_uint, POINTER(D3D11_MAPPED_SUBRESOURCE)],
            self._staging,
            0,
            D3D11_MAP_READ,
            flags,
            byref(mapped),
        )
        if hr == DXGI_ERROR_WAS_STILL_DRAWING:
            return None
        check(hr, "Map")
        try:
            buf = (ctypes.c_uint32 * self._groups).from_address(mapped.pData)
            tiles = np.ctypeslib.as_array(buf).reshape(self._gy, self._gx).copy()
        finally:
            vcall(ctx, CTX_UNMAP, None, [c_void_p, c_uint], self._staging, 0)
        self.pending = False
        return tiles

    def _free(self) -> None:
        for name in ("_staging", "_uav", "_buf", "_srv", "_tex"):
            release(getattr(self, name))
            setattr(self, name, c_void_p())
        self._size = (0, 0)
        self.pending = False

    def close(self) -> None:
        self._free()


IID_ID3D11Multithread = GUID.parse("9B7E4E00-342C-4106-A19F-4F2704F689F0")
IID_IDXGIFactory6 = GUID.parse("c1b6694f-ff09-44a9-b03c-77900a0a1d17")
IID_IDXGIAdapter1 = GUID.parse("29038f61-3839-4626-91fd-086879011a05")
DXGI_GPU_PREFERENCE_MINIMUM_POWER = 1
_dxgi = ctypes.WinDLL("dxgi")
_dxgi.CreateDXGIFactory2.restype = HRESULT
_dxgi.CreateDXGIFactory2.argtypes = [c_uint, POINTER(GUID), POINTER(c_void_p)]


def _low_power_adapter() -> c_void_p:
    """The minimum-power adapter (IDXGIFactory6, Windows 10 1803+), or null for the default."""
    factory = c_void_p()
    try:
        if _dxgi.CreateDXGIFactory2(0, byref(IID_IDXGIFactory6), byref(factory)) < 0:
            return c_void_p()
        adapter = c_void_p()
        hr = vcall(
            factory,
            FACTORY6_ENUM_BY_PREFERENCE,
            HRESULT,
            [c_uint, c_uint, POINTER(GUID), POINTER(c_void_p)],
            0,
            DXGI_GPU_PREFERENCE_MINIMUM_POWER,
            byref(IID_IDXGIAdapter1),
            byref(adapter),
        )
        if hr < 0:
            return c_void_p()
        # Only if a display hangs on it: otherwise every captured frame would have to cross
        # adapters (e.g. a desktop with an enabled but unused integrated GPU).
        output = c_void_p()
        if vcall(adapter, ADAPTER_ENUM_OUTPUTS, HRESULT, [c_uint, POINTER(c_void_p)], 0, byref(output)) < 0:
            release(adapter)
            return c_void_p()
        release(output)
        return adapter
    except OSError:
        return c_void_p()
    finally:
        release(factory)
