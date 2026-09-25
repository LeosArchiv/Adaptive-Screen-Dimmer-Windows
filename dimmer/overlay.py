"""Click-through, top-most black overlay window for one monitor.

All methods must be called from the thread that created the overlay (Win32 window affinity).
"""

from __future__ import annotations

from collections.abc import Callable

import win32api
import win32con
import win32gui

from .winapi import Monitor, exclude_from_capture

CLASS_NAME = "AdaptiveScreenDimmerOverlay"
_registered = False
_message_hook: Callable[[int, int, int], None] | None = None

EX_STYLE = (
    win32con.WS_EX_LAYERED
    | win32con.WS_EX_TRANSPARENT  # clicks pass through
    | win32con.WS_EX_TOPMOST
    | win32con.WS_EX_NOACTIVATE
    | win32con.WS_EX_TOOLWINDOW  # no taskbar / Alt-Tab entry
)


def set_message_hook(hook: Callable[[int, int, int], None] | None) -> None:
    """Receive broadcast messages (display change etc.) that arrive at overlay windows."""
    global _message_hook
    _message_hook = hook


WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
WM_SETTINGCHANGE = 0x001A
WATCHED_MESSAGES = (WM_DISPLAYCHANGE, WM_DPICHANGED, WM_SETTINGCHANGE)


def _wnd_proc(hwnd: int, msg: int, wp: int, lp: int) -> int:
    try:
        return _handle(hwnd, msg, wp, lp)
    except Exception:  # an exception escaping a window procedure would spam and misbehave
        return 0


def _handle(hwnd: int, msg: int, wp: int, lp: int) -> int:
    if msg in WATCHED_MESSAGES:
        if _message_hook:
            _message_hook(msg, wp, lp)
        return int(win32gui.DefWindowProc(hwnd, msg, wp, lp))
    if msg == win32con.WM_ERASEBKGND:
        return 1
    if msg == win32con.WM_PAINT:
        hdc, ps = win32gui.BeginPaint(hwnd)
        win32gui.FillRect(hdc, win32gui.GetClientRect(hwnd), win32gui.GetStockObject(win32con.BLACK_BRUSH))
        win32gui.EndPaint(hwnd, ps)
        return 0
    if msg == win32con.WM_CLOSE:
        return 0  # only the owner destroys overlays
    return int(win32gui.DefWindowProc(hwnd, msg, wp, lp))


def _register() -> None:
    global _registered
    if _registered:
        return
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = CLASS_NAME
    wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error as e:
        if e.winerror != 1410:  # ERROR_CLASS_ALREADY_EXISTS
            raise
    _registered = True


class Overlay:
    def __init__(self, monitor: Monitor) -> None:
        _register()
        self.monitor = monitor
        self.alpha = 0
        self.visible = False
        m = monitor
        self.hwnd = win32gui.CreateWindowEx(
            EX_STYLE,
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
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 0, win32con.LWA_ALPHA)
        # Excluded windows are invisible to screen capture, so the overlay never measures itself.
        self.excluded = exclude_from_capture(self.hwnd)

    def set_alpha(self, alpha: int) -> None:
        alpha = max(0, min(255, int(alpha)))
        if alpha == self.alpha and self.visible == (alpha > 0):
            return
        if alpha != self.alpha:
            win32gui.SetLayeredWindowAttributes(self.hwnd, 0, alpha, win32con.LWA_ALPHA)
            self.alpha = alpha
        if alpha > 0 and not self.visible:
            self._show()
        elif alpha == 0 and self.visible:
            # A hidden overlay costs nothing and does not block fullscreen direct flip.
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
            self.visible = False

    def _show(self) -> None:
        m = self.monitor
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOPMOST,
            m.left,
            m.top,
            m.width,
            m.height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        self.visible = True

    def keep_on_top(self) -> None:
        if self.visible:
            win32gui.SetWindowPos(
                self.hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                0,
                0,
                win32con.SWP_NOACTIVATE | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )

    def destroy(self) -> None:
        if self.hwnd:
            try:
                win32gui.DestroyWindow(self.hwnd)
            except win32gui.error:
                pass
            self.hwnd = 0
            self.visible = False
