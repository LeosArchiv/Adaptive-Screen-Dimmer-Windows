"""Local dimming: darken only glaring areas, via a DirectComposition overlay drawn on the GPU.

A click-through top-most window without a redirection bitmap hosts a composition swap chain
with premultiplied alpha. Each update uploads a small mask (one value per 16x16 tile, a few KB)
and a pixel shader stretches it with bilinear filtering to the full monitor, so the darkening
has soft edges. The CPU never touches full-resolution pixels.

Engine thread only (window affinity and the D3D immediate context).
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_char_p, c_float, c_size_t, c_uint, c_void_p, wintypes
from typing import Any

import numpy as np
import win32api
import win32con
import win32gui

from .gpu import (
    D3D11_TEXTURE2D_DESC,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    DXGI_SAMPLE_DESC,
    GUID,
    HRESULT,
    GpuBrightness,
    GpuError,
    IID_IDXGIDevice,
    check,
    query,
    release,
    vcall,
)
from .winapi import Monitor, exclude_from_capture

IID_IDXGIFactory2 = GUID.parse("50c83a1c-e072-4c48-87b0-3630fa36a6d0")
IID_ID3D11Texture2D = GUID.parse("6f15aaf2-d208-4e89-9ab4-489535d34f9c")
IID_IDCompositionDevice = GUID.parse("C37EA93A-E7AA-450D-B16F-9746CB0407F3")

DXGI_FORMAT_R8_UNORM = 61
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL = 3
DXGI_ALPHA_MODE_PREMULTIPLIED = 1
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_USAGE_DEFAULT = 0
D3D11_FILTER_MIN_MAG_MIP_LINEAR = 0x15
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4
D3D11_COMPARISON_NEVER = 1
DXGI_PRESENT_DO_NOT_WAIT = 0x8
DXGI_ERROR_WAS_STILL_DRAWING = -2005270518
DCOMP_CHECK_DEVICE_STATE = 26

# vtable indices
DEV_CREATE_TEXTURE2D, DEV_CREATE_SRV, DEV_CREATE_RTV = 5, 7, 9
DEV_CREATE_VS, DEV_CREATE_PS, DEV_CREATE_SAMPLER = 12, 15, 23
CTX_PS_SET_SRV, CTX_PS_SET_SHADER, CTX_PS_SET_SAMPLERS, CTX_VS_SET_SHADER = 8, 9, 10, 11
CTX_DRAW, CTX_IA_SET_LAYOUT, CTX_IA_SET_TOPOLOGY = 13, 17, 24
CTX_OM_SET_RT, CTX_RS_SET_VIEWPORTS, CTX_UPDATE_SUBRESOURCE = 33, 44, 48
CTX_CLEAR_RTV = 50
FACTORY2_CREATE_SWAPCHAIN_FOR_COMPOSITION = 24
SWAPCHAIN_PRESENT, SWAPCHAIN_GET_BUFFER = 8, 9
DCOMP_COMMIT, DCOMP_CREATE_TARGET_FOR_HWND, DCOMP_CREATE_VISUAL = 3, 6, 7
TARGET_SET_ROOT = 3
VISUAL_SET_CONTENT = 15  # after SetOffsetX x2, SetOffsetY x2, SetTransform x2, parent, effect, interp, border, clip x2

SHADER_COMMON = """
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
"""
VS = (
    SHADER_COMMON
    + """
VSOut main(uint id : SV_VertexID)
{
    VSOut o;
    float2 t = float2((id << 1) & 2, id & 2);
    o.pos = float4(t * float2(2, -2) + float2(-1, 1), 0, 1);
    o.uv = t;
    return o;
}
"""
)
# The mask texture covers whole 16 px tiles (e.g. 68 x 16 = 1088 px for a 1080 px screen), so
# screen UVs are scaled by screen / (tiles * 16) to keep the mask exactly over the spot.
PS_TEMPLATE = (
    SHADER_COMMON
    + """
Texture2D<float> mask : register(t0);
SamplerState lin : register(s0);
float4 main(VSOut i) : SV_Target
{
    float a = mask.Sample(lin, i.uv * float2(%(sx).8f, %(sy).8f));
    return float4(0, 0, 0, a);  // premultiplied black
}
"""
)


class DXGI_SWAP_CHAIN_DESC1(ctypes.Structure):
    _fields_ = [
        ("Width", c_uint),
        ("Height", c_uint),
        ("Format", c_uint),
        ("Stereo", wintypes.BOOL),
        ("SampleDesc", DXGI_SAMPLE_DESC),
        ("BufferUsage", c_uint),
        ("BufferCount", c_uint),
        ("Scaling", c_uint),
        ("SwapEffect", c_uint),
        ("AlphaMode", c_uint),
        ("Flags", c_uint),
    ]


class D3D11_SAMPLER_DESC(ctypes.Structure):
    _fields_ = [
        ("Filter", c_uint),
        ("AddressU", c_uint),
        ("AddressV", c_uint),
        ("AddressW", c_uint),
        ("MipLODBias", c_float),
        ("MaxAnisotropy", c_uint),
        ("ComparisonFunc", c_uint),
        ("BorderColor", c_float * 4),
        ("MinLOD", c_float),
        ("MaxLOD", c_float),
    ]


class D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [(n, c_float) for n in ("TopLeftX", "TopLeftY", "Width", "Height", "MinDepth", "MaxDepth")]


def _compile(src: bytes, target: bytes) -> bytes:
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
        src, len(src), b"mask", None, None, b"main", target, 1 << 15, 0, byref(code), byref(errors)
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


_dcomp = ctypes.WinDLL("dcomp")
_dcomp.DCompositionCreateDevice.restype = HRESULT
_dcomp.DCompositionCreateDevice.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]

CLASS_NAME = "AdaptiveScreenDimmerLocal"
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
_registered = False


def _wnd_proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    try:
        if msg == win32con.WM_CLOSE:
            return 0  # only the owner destroys this window
        return int(win32gui.DefWindowProc(hwnd, msg, wp, lp))
    except Exception:
        return 0


def _register() -> None:
    global _registered
    if _registered:
        return
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = CLASS_NAME
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error as e:
        if e.winerror != 1410:
            raise
    _registered = True


class LocalDimmer:
    """One monitor's local-dimming layer. ``show_mask`` takes alpha values (0..1) per tile."""

    def __init__(self, gpu: GpuBrightness, monitor: Monitor, grid: tuple[int, int], exclude: bool = True) -> None:
        self.gpu = gpu
        self.monitor = monitor
        self.grid_h, self.grid_w = grid
        self.visible = False
        self.below: Any = None  # a window with .hwnd to stay directly under (the dimming overlay)
        self._objs: list[c_void_p] = []
        _register()
        m = monitor
        ex = (
            WS_EX_NOREDIRECTIONBITMAP
            | win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_TOPMOST
            | win32con.WS_EX_NOACTIVATE
            | win32con.WS_EX_TOOLWINDOW
        )
        self.hwnd = win32gui.CreateWindowEx(
            ex,
            CLASS_NAME,
            "",
            win32con.WS_POPUP,
            m.left,
            m.top,
            m.width,
            m.height,
            0,
            0,
            win32api.GetModuleHandle(None),
            None,
        )
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 255, win32con.LWA_ALPHA)
        self.excluded = exclude_from_capture(self.hwnd) if exclude else False
        try:
            if exclude and not self.excluded:
                # The capture would see the darkened spot, the mask would drop, the spot return:
                # a pumping loop. Better no local layer at all.
                raise GpuError("local layer cannot be excluded from screen capture")
            self._build()
        except Exception:
            self.close()
            raise

    def _keep(self, obj: c_void_p) -> c_void_p:
        self._objs.append(obj)
        return obj

    def _build(self) -> None:
        dev, ctx = self.gpu.device, self.gpu.context
        m = self.monitor
        # swap chain for composition, full monitor size
        dxgi_dev = query(dev, IID_IDXGIDevice)
        adapter, factory = c_void_p(), c_void_p()
        try:
            check(vcall(dxgi_dev, 7, HRESULT, [POINTER(c_void_p)], byref(adapter)), "GetAdapter")
            check(
                vcall(
                    adapter, 6, HRESULT, [POINTER(GUID), POINTER(c_void_p)], byref(IID_IDXGIFactory2), byref(factory)
                ),
                "GetParent(IDXGIFactory2)",
            )
            desc = DXGI_SWAP_CHAIN_DESC1(
                m.width,
                m.height,
                DXGI_FORMAT_B8G8R8A8_UNORM,
                False,
                DXGI_SAMPLE_DESC(1, 0),
                DXGI_USAGE_RENDER_TARGET_OUTPUT,
                2,
                0,
                DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL,
                DXGI_ALPHA_MODE_PREMULTIPLIED,
                0,
            )
            self.swapchain = self._keep(c_void_p())
            check(
                vcall(
                    factory,
                    FACTORY2_CREATE_SWAPCHAIN_FOR_COMPOSITION,
                    HRESULT,
                    [c_void_p, POINTER(DXGI_SWAP_CHAIN_DESC1), c_void_p, POINTER(c_void_p)],
                    dev,
                    byref(desc),
                    None,
                    byref(self.swapchain),
                ),
                "CreateSwapChainForComposition",
            )
            # composition tree: target(hwnd) -> visual -> swap chain
            self.dcomp = self._keep(c_void_p())
            check(
                _dcomp.DCompositionCreateDevice(dxgi_dev, byref(IID_IDCompositionDevice), byref(self.dcomp)),
                "DCompositionCreateDevice",
            )
        finally:
            release(factory)
            release(adapter)
            release(dxgi_dev)
        self.target = self._keep(c_void_p())
        check(
            vcall(
                self.dcomp,
                DCOMP_CREATE_TARGET_FOR_HWND,
                HRESULT,
                [c_void_p, wintypes.BOOL, POINTER(c_void_p)],
                self.hwnd,
                True,
                byref(self.target),
            ),
            "CreateTargetForHwnd",
        )
        self.visual = self._keep(c_void_p())
        check(vcall(self.dcomp, DCOMP_CREATE_VISUAL, HRESULT, [POINTER(c_void_p)], byref(self.visual)), "CreateVisual")
        check(vcall(self.visual, VISUAL_SET_CONTENT, HRESULT, [c_void_p], self.swapchain), "SetContent")
        check(vcall(self.target, TARGET_SET_ROOT, HRESULT, [c_void_p], self.visual), "SetRoot")
        check(vcall(self.dcomp, DCOMP_COMMIT, HRESULT, []), "Commit")
        # shaders, sampler, mask texture
        scale = {"sx": m.width / (self.grid_w * 16), "sy": m.height / (self.grid_h * 16)}
        vs_code = _compile(VS.encode(), b"vs_5_0")
        ps_code = _compile((PS_TEMPLATE % scale).encode(), b"ps_5_0")
        self.vs = self._keep(c_void_p())
        check(
            vcall(
                dev,
                DEV_CREATE_VS,
                HRESULT,
                [c_char_p, c_size_t, c_void_p, POINTER(c_void_p)],
                vs_code,
                len(vs_code),
                None,
                byref(self.vs),
            ),
            "CreateVertexShader",
        )
        self.ps = self._keep(c_void_p())
        check(
            vcall(
                dev,
                DEV_CREATE_PS,
                HRESULT,
                [c_char_p, c_size_t, c_void_p, POINTER(c_void_p)],
                ps_code,
                len(ps_code),
                None,
                byref(self.ps),
            ),
            "CreatePixelShader",
        )
        sd = D3D11_SAMPLER_DESC(
            D3D11_FILTER_MIN_MAG_MIP_LINEAR,
            D3D11_TEXTURE_ADDRESS_CLAMP,
            D3D11_TEXTURE_ADDRESS_CLAMP,
            D3D11_TEXTURE_ADDRESS_CLAMP,
            0.0,
            1,
            D3D11_COMPARISON_NEVER,
            (c_float * 4)(0, 0, 0, 0),
            0.0,
            3.4e38,
        )
        self.sampler = self._keep(c_void_p())
        check(
            vcall(
                dev,
                DEV_CREATE_SAMPLER,
                HRESULT,
                [POINTER(D3D11_SAMPLER_DESC), POINTER(c_void_p)],
                byref(sd),
                byref(self.sampler),
            ),
            "CreateSamplerState",
        )
        td = D3D11_TEXTURE2D_DESC(
            self.grid_w,
            self.grid_h,
            1,
            1,
            DXGI_FORMAT_R8_UNORM,
            DXGI_SAMPLE_DESC(1, 0),
            D3D11_USAGE_DEFAULT,
            D3D11_BIND_SHADER_RESOURCE,
            0,
            0,
        )
        self.mask_tex = self._keep(c_void_p())
        check(
            vcall(
                dev,
                DEV_CREATE_TEXTURE2D,
                HRESULT,
                [POINTER(D3D11_TEXTURE2D_DESC), c_void_p, POINTER(c_void_p)],
                byref(td),
                None,
                byref(self.mask_tex),
            ),
            "CreateTexture2D(mask)",
        )
        self.mask_srv = self._keep(c_void_p())
        check(
            vcall(
                dev,
                DEV_CREATE_SRV,
                HRESULT,
                [c_void_p, c_void_p, POINTER(c_void_p)],
                self.mask_tex,
                None,
                byref(self.mask_srv),
            ),
            "CreateShaderResourceView(mask)",
        )
        back = c_void_p()
        check(
            vcall(
                self.swapchain,
                SWAPCHAIN_GET_BUFFER,
                HRESULT,
                [c_uint, POINTER(GUID), POINTER(c_void_p)],
                0,
                byref(IID_ID3D11Texture2D),
                byref(back),
            ),
            "GetBuffer",
        )
        try:
            # Flip model in D3D11: buffer 0 always refers to the current back buffer, so one view
            # serves every frame.
            self.rtv = self._keep(c_void_p())
            check(
                vcall(
                    dev, DEV_CREATE_RTV, HRESULT, [c_void_p, c_void_p, POINTER(c_void_p)], back, None, byref(self.rtv)
                ),
                "CreateRenderTargetView",
            )
        finally:
            release(back)
        _ = ctx

    def alive(self) -> bool:
        """False when DirectComposition lost its device (e.g. DWM restarted)."""
        valid = wintypes.BOOL()
        hr = vcall(self.dcomp, DCOMP_CHECK_DEVICE_STATE, HRESULT, [POINTER(wintypes.BOOL)], byref(valid))
        return hr >= 0 and bool(valid.value)

    def show_mask(self, alpha: np.ndarray) -> bool:
        """Draw the mask (grid-sized array of 0..1) and show the layer; all zero hides it.

        Returns False when the frame could not be presented yet (try again next round)."""
        if not alpha.any():
            self.hide()
            return True
        data = np.ascontiguousarray(np.clip(alpha * 255.0 + 0.5, 0, 255).astype(np.uint8))
        ctx = self.gpu.context
        vcall(
            ctx,
            CTX_UPDATE_SUBRESOURCE,
            None,
            [c_void_p, c_uint, c_void_p, c_void_p, c_uint, c_uint],
            self.mask_tex,
            0,
            None,
            data.ctypes.data,
            data.shape[1],
            0,
        )
        if not self._draw(clear=False):
            return False  # compositor still busy with the previous frame
        m = self.monitor
        if not self.visible:
            win32gui.SetWindowPos(
                self.hwnd,
                self._insert_after(),
                m.left,
                m.top,
                m.width,
                m.height,
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
            )
            self.visible = True
        return True

    def _draw(self, clear: bool) -> bool:
        """Render the mask (or a fully transparent frame) and present it without blocking."""
        ctx = self.gpu.context
        m = self.monitor
        vp = D3D11_VIEWPORT(0, 0, float(m.width), float(m.height), 0.0, 1.0)
        rtvs = (c_void_p * 1)(self.rtv.value)
        vcall(ctx, CTX_OM_SET_RT, None, [c_uint, c_void_p, c_void_p], 1, rtvs, None)
        if clear:
            zero = (c_float * 4)(0, 0, 0, 0)
            vcall(ctx, CTX_CLEAR_RTV, None, [c_void_p, c_void_p], self.rtv, zero)
        else:
            vcall(ctx, CTX_RS_SET_VIEWPORTS, None, [c_uint, POINTER(D3D11_VIEWPORT)], 1, byref(vp))
            vcall(ctx, CTX_IA_SET_LAYOUT, None, [c_void_p], None)
            vcall(ctx, CTX_IA_SET_TOPOLOGY, None, [c_uint], D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
            vcall(ctx, CTX_VS_SET_SHADER, None, [c_void_p, c_void_p, c_uint], self.vs, None, 0)
            vcall(ctx, CTX_PS_SET_SHADER, None, [c_void_p, c_void_p, c_uint], self.ps, None, 0)
            srvs = (c_void_p * 1)(self.mask_srv.value)
            vcall(ctx, CTX_PS_SET_SRV, None, [c_uint, c_uint, c_void_p], 0, 1, srvs)
            samplers = (c_void_p * 1)(self.sampler.value)
            vcall(ctx, CTX_PS_SET_SAMPLERS, None, [c_uint, c_uint, c_void_p], 0, 1, samplers)
            vcall(ctx, CTX_DRAW, None, [c_uint, c_uint], 3, 0)
            null = (c_void_p * 1)(None)
            vcall(ctx, CTX_PS_SET_SRV, None, [c_uint, c_uint, c_void_p], 0, 1, null)
        vcall(ctx, CTX_OM_SET_RT, None, [c_uint, c_void_p, c_void_p], 0, None, None)
        hr = vcall(self.swapchain, SWAPCHAIN_PRESENT, HRESULT, [c_uint, c_uint], 1, DXGI_PRESENT_DO_NOT_WAIT)
        if hr == DXGI_ERROR_WAS_STILL_DRAWING:
            return False
        check(hr, "Present")
        return True

    def hide(self) -> None:
        """Clear, then hide: when shown again, an old mask can never flash up for a frame."""
        if self.visible:
            try:
                self._draw(clear=True)
            except OSError:
                pass
        self.visible = False
        win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)

    def _insert_after(self) -> int:
        if self.below is not None and self.below.hwnd:
            return int(self.below.hwnd)
        return win32con.HWND_TOPMOST

    def keep_on_top(self, even_hidden: bool = False) -> None:
        if self.visible or even_hidden:
            win32gui.SetWindowPos(
                self.hwnd,
                self._insert_after(),
                0,
                0,
                0,
                0,
                win32con.SWP_NOACTIVATE | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )

    def close(self) -> None:
        for obj in reversed(self._objs):
            release(obj)
        self._objs.clear()
        if self.hwnd:
            try:
                win32gui.DestroyWindow(self.hwnd)
            except win32gui.error:
                pass
            self.hwnd = 0
