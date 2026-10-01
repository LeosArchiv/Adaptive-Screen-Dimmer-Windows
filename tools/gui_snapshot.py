"""Start the real GUI briefly and save a PNG of the app window only (never the rest of the screen).

Uses a throw-away config directory and quits after a few seconds (kill switch included).

    python tools/gui_snapshot.py out.png [--scroll PX] [--paused] [--open-log]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
import tempfile
import threading
import zlib
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PW_RENDERFULLCONTENT = 2  # needed for WebView2 content


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("rest", ctypes.c_uint32 * 5),
    ]


def grab_window(title: str) -> np.ndarray:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        raise RuntimeError("window not found")
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0)
    buf = (ctypes.c_ubyte * (w * h * 4))()
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, 2::-1].copy()


def write_png(path: Path, rgb: np.ndarray) -> None:
    h, w, _ = rgb.shape
    raw = b"".join(b"\0" + rgb[y].tobytes() for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--paused", action="store_true")
    ap.add_argument("--delay", type=float, default=4.0)
    ap.add_argument("--scroll", type=int, default=0, help="scroll the page down by this many pixels")
    ap.add_argument("--open-log", action="store_true")
    args = ap.parse_args()
    threading.Timer(args.delay + 15, lambda: os._exit(3)).start()  # kill switch
    os.environ["ASD_CONFIG_DIR"] = tempfile.mkdtemp(prefix="asd-snap-")

    from dimmer import app, winapi
    from dimmer import settings as settings_mod
    from dimmer.engine import Engine
    from dimmer.gui import TITLE, DimmerApp

    winapi.enable_dpi_awareness()
    handler = app._setup_logging(False)
    s = settings_mod.load()
    s.start_paused = args.paused
    engine = Engine(s)
    engine.start()
    ui = DimmerApp(engine, s, handler, None)

    def shoot() -> None:
        js = f"window.scrollTo(0, {args.scroll});"
        if args.open_log:
            js += "document.querySelector('details.log').open = true;"
        ui.window.evaluate_js(js)
        threading.Event().wait(0.6)
        rgb = grab_window(TITLE)
        write_png(args.out, rgb)
        print(f"saved {args.out} ({rgb.shape[1]}x{rgb.shape[0]})", flush=True)
        ui.quit()

    threading.Timer(args.delay, shoot).start()
    try:
        ui.run()
    finally:
        engine.stop()
    os._exit(0)


if __name__ == "__main__":
    main()
