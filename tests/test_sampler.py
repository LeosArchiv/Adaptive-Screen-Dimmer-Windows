"""Sampler correctness against synthetic bitmaps in a memory DC (the real screen is never read)."""

import ctypes

import numpy as np
import pytest

from dimmer.logic import brightness
from dimmer.winapi import BITMAPINFOHEADER, Sampler, gdi32, user32

W, H = 1920, 1080


class MemorySource:
    """A GDI memory DC holding a synthetic image, used as the Sampler's source."""

    def __init__(self, img: np.ndarray) -> None:
        screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = W, -H, 1, 32
        bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bmp)
        buf = (ctypes.c_uint8 * (W * H * 4)).from_address(bits.value)
        np.ctypeslib.as_array(buf).reshape(H, W, 4)[:] = img

    def close(self) -> None:
        gdi32.SelectObject(self.dc, self.old)
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.dc)


def patterns() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(7)
    ide = np.full((H, W, 4), 30, np.uint8)
    ide[200:900, 1100:1800] = 245  # white dialog on a dark IDE
    text = np.full((H, W, 4), 250, np.uint8)
    text[::12, :] = 20  # dark text lines on a white page
    text[:, ::7] = 40
    gradient = np.repeat(np.linspace(0, 255, W, dtype=np.uint8)[None, :, None], H, 0).repeat(4, 2)
    noise = rng.integers(0, 256, (H, W, 4), dtype=np.uint8)
    result = {"ide": ide, "text": text, "gradient": gradient, "noise": noise}
    for period in range(2, 17):  # regular line/column patterns are the aliasing worst case
        lines = np.full((H, W, 4), 250, np.uint8)
        lines[::period, :] = 10
        lines[:, ::period] = 10
        result[f"grid{period}"] = lines
    return result


@pytest.mark.parametrize("name", list(patterns()))
def test_sampled_brightness_close_to_full_mean(name: str) -> None:
    img = patterns()[name]
    src = MemorySource(img)
    sampler = Sampler(source_dc=src.dc)
    try:
        sampled = brightness(sampler.grab(0, 0, W, H))
    finally:
        sampler.close()
        src.close()
    full = brightness(img)
    # Exact: regular patterns (text, grids) must not alias, or scrolling would make it pump.
    assert sampled == pytest.approx(full, abs=1e-9), (sampled, full)


def test_sampler_reuses_buffer_and_handles_resize() -> None:
    img = np.full((H, W, 4), 100, np.uint8)
    src = MemorySource(img)
    sampler = Sampler(source_dc=src.dc)
    try:
        a = sampler.grab(0, 0, W, H)
        b = sampler.grab(0, 0, W, H)
        assert a is b  # buffer is reused, no allocation per frame
        c = sampler.grab(0, 0, 640, 480)
        assert c.shape == (480, 640, 4)
        assert brightness(c) == pytest.approx(100, abs=0.01)
    finally:
        sampler.close()
        src.close()


def test_sampler_reports_invalid_source_cleanly() -> None:
    with pytest.raises(OSError):
        Sampler(source_dc=0)
