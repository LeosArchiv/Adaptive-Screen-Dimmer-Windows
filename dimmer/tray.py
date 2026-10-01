"""Notification-area (tray) icon on its own thread with its own hidden window.

It never touches the window: menu choices are put into ``TrayIcon.actions`` and the GUI drains that
queue on its own thread. ``set_state`` may be called from any thread.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import wintypes

import numpy as np
import win32api
import win32con
import win32gui

from .i18n import t

log = logging.getLogger("dimmer")

WM_TRAY = win32con.WM_USER + 20
WM_UPDATE = win32con.WM_USER + 21
CMD_TOGGLE, CMD_SHOW, CMD_QUIT = 1, 2, 3
CLASS_NAME = "AdaptiveScreenDimmerTray"

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes = [wintypes.HICON]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]


def _moon_pixels(size: int, color: tuple[int, int, int]) -> np.ndarray:
    """BGRA crescent moon, anti-aliased, transparent background."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64) + 0.5
    c = size / 2
    r = size * 0.44
    disc = np.clip(r - np.hypot(xx - c, yy - c) + 0.5, 0, 1)
    cut = np.clip(np.hypot(xx - c - size * 0.26, yy - c + size * 0.2) - r * 0.82 + 0.5, 0, 1)
    alpha = (disc * cut * 255).astype(np.uint8)
    img = np.zeros((size, size, 4), np.uint8)
    img[..., 0], img[..., 1], img[..., 2] = color[2], color[1], color[0]
    img[..., 3] = alpha
    return img


def make_icon(color: tuple[int, int, int], size: int = 32) -> int:
    pixels = np.ascontiguousarray(_moon_pixels(size, color))
    color_bmp = gdi32.CreateBitmap(size, size, 1, 32, pixels.ctypes.data)
    mask_bits = np.zeros((size, (size + 15) // 16 * 2), np.uint8)  # all 0: alpha channel decides
    mask_bmp = gdi32.CreateBitmap(size, size, 1, 1, mask_bits.ctypes.data)
    info = ICONINFO(True, 0, 0, mask_bmp, color_bmp)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(color_bmp)
    gdi32.DeleteObject(mask_bmp)
    return int(icon or 0)


ACTIVE_COLOR = (240, 180, 60)
PAUSED_COLOR = (140, 140, 150)


class TrayIcon(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="dimmer-tray", daemon=True)
        self.actions: queue.Queue[str] = queue.Queue()
        self._hwnd = 0
        self._paused = False
        self._tip = "Adaptive Screen Dimmer"
        self._ready = threading.Event()
        self._icons: dict[bool, int] = {}
        self._lang = "en"

    # thread-safe API
    def set_state(self, paused: bool, tip: str) -> None:
        if paused == self._paused and tip[:127] == self._tip:
            return
        self._paused, self._tip = paused, tip[:127]
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, WM_UPDATE, 0, 0)

    def set_language(self, lang: str) -> None:
        """Menu texts; the menu is built on every right click, so this applies at once."""
        self._lang = lang

    def close(self) -> None:
        """Remove the icon and end the thread. Safe to call more than once."""
        hwnd, self._hwnd = self._hwnd, 0
        if hwnd:
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
        if self.is_alive() and threading.current_thread() is not self:
            self.join(2)

    def wait_ready(self, timeout: float = 3.0) -> bool:
        return self._ready.wait(timeout)

    # tray thread
    def run(self) -> None:
        try:
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self._wnd_proc
            wc.hInstance = win32api.GetModuleHandle(None)
            wc.lpszClassName = CLASS_NAME
            try:
                win32gui.RegisterClass(wc)
            except win32gui.error as e:
                if e.winerror != 1410:
                    raise
            self._hwnd = win32gui.CreateWindow(CLASS_NAME, "", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
            self._icons = {False: make_icon(ACTIVE_COLOR), True: make_icon(PAUSED_COLOR)}
            self._notify(win32gui.NIM_ADD)
        except Exception:
            log.exception("Could not create the tray icon")
            self._ready.set()
            return
        self._ready.set()
        win32gui.PumpMessages()
        for icon in self._icons.values():
            if icon:
                user32.DestroyIcon(icon)

    def _notify(self, op: int, hwnd: int | None = None) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        data = (hwnd or self._hwnd, 0, flags, WM_TRAY, self._icons[self._paused], self._tip)
        try:
            win32gui.Shell_NotifyIcon(op, data)
        except win32gui.error as e:  # explorer not ready / restarting
            log.debug("Shell_NotifyIcon failed: %s", e)

    def _wnd_proc(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
        try:
            if msg == WM_TRAY:
                if lp == win32con.WM_LBUTTONUP:
                    self.actions.put("show")
                elif lp in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == WM_UPDATE:
                self._notify(win32gui.NIM_MODIFY)
                return 0
            if msg == win32con.WM_COMMAND:
                cmd = win32api.LOWORD(wp)
                self.actions.put({CMD_TOGGLE: "toggle", CMD_SHOW: "show", CMD_QUIT: "quit"}.get(cmd, ""))
                return 0
            if msg == win32con.WM_CLOSE:
                self._notify(win32gui.NIM_DELETE, hwnd)
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            if msg == _TASKBAR_CREATED:  # explorer restarted: add the icon again
                self._notify(win32gui.NIM_ADD)
                return 0
        except Exception:
            log.exception("Tray error")
            return 0
        return int(win32gui.DefWindowProc(hwnd, msg, wp, lp))

    def _menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        lang = self._lang
        toggle = t(lang, "tray_resume" if self._paused else "tray_pause")
        win32gui.AppendMenu(menu, win32con.MF_STRING, CMD_TOGGLE, toggle)
        win32gui.AppendMenu(menu, win32con.MF_STRING, CMD_SHOW, t(lang, "tray_open"))
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, CMD_QUIT, t(lang, "tray_quit"))
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self._hwnd)  # required so the menu closes when clicking elsewhere
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON, x, y, 0, self._hwnd, None)
        win32gui.PostMessage(self._hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)


_TASKBAR_CREATED = win32gui.RegisterWindowMessage("TaskbarCreated")
