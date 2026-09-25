"""Pure measurement and dimming logic, free of Win32 and GUI code.

Brightness is the plain mean over the B, G and R channels (0..255), the same metric the
original single-file version used, so existing threshold values keep their meaning.
Opacity is the alpha of a black overlay (0 = invisible, 255 = black).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

MAX_ALPHA = 255


def brightness(img: np.ndarray) -> float:
    """Mean brightness of a BGRA/BGR image in 0..255 (alpha channel ignored).

    Exact over every pixel: sampling a subset aliases on text and grids and would make the
    value (and the overlay) pump while scrolling. For contiguous BGRA frames it sums all bytes
    as integers and subtracts the alpha channel, about twice as fast as a float mean.
    """
    if img.size == 0:
        return 0.0
    if img.ndim == 3 and img.shape[2] == 4 and img.flags.c_contiguous:
        total = int(img.sum(dtype=np.uint64)) - int(img[..., 3].sum(dtype=np.uint64))
        return total / (img.shape[0] * img.shape[1] * 3)
    return float(np.mean(img[..., :3], dtype=np.float64))


def compensate(observed: float, alpha: float) -> float:
    """Undo the darkening of a black overlay with the given alpha that is part of a capture.

    Only used when the overlay cannot be excluded from screen capture. A black overlay with
    alpha a turns a pixel value v into v * (1 - a/255).
    """
    visible = 1.0 - max(0.0, min(float(MAX_ALPHA), alpha)) / MAX_ALPHA
    if visible <= 0.02:
        return 255.0
    return min(255.0, observed / visible)


def target_opacity(level: float, start: float, full: float, max_opacity: float) -> float:
    """Map a brightness level to the desired overlay alpha (linear ramp from start to full)."""
    if full <= start:
        full = start + 1.0
    if level > full:
        return float(max_opacity)
    if level > start:
        return (level - start) / (full - start) * max_opacity
    return 0.0


# Time constants in seconds: (attack = getting darker, release = getting brighter again).
ATTACK_PRESETS: dict[str, float] = {"Sofort": 0.03, "Schnell": 0.08, "Sanft": 0.25}
RELEASE_PRESETS: dict[str, float] = {"Schnell": 0.25, "Normal": 0.6, "Langsam": 1.5}


@dataclass
class Smoother:
    """Flicker-free opacity follower.

    * Darkening follows quickly (flash protection) as a short exponential ramp, never as a hard
      jump, so a white page fades in over roughly 3 * attack seconds.
    * Brightening only starts after the target has stayed lower for ``hold`` seconds and then
      follows slowly. This stops strobing content (videos, games) from pumping the overlay.
    * Once settled, changes smaller than ``deadband`` alpha units are ignored, so measurement
      noise never produces visible micro-flicker.
    """

    attack: float = ATTACK_PRESETS["Schnell"]
    release: float = RELEASE_PRESETS["Normal"]
    hold: float = 0.4
    deadband: float = 3.0
    value: float = 0.0
    settled: bool = True
    _lower_since: float = 0.0

    def reset(self, value: float = 0.0) -> None:
        self.value = value
        self.settled = True
        self._lower_since = 0.0

    def step(self, target: float, dt: float, skip_hold: bool = False) -> float:
        """Advance by ``dt`` seconds toward ``target`` and return the new value.

        ``skip_hold`` starts brightening at once (used when dimming is switched off on purpose).
        """
        dt = max(0.0, min(dt, 1.0))
        diff = target - self.value
        # The deadband never keeps a faint overlay alive: reaching 0 hides the window entirely.
        if self.settled and abs(diff) < self.deadband and not (round(target) == 0 and round(self.value) > 0):
            self._lower_since = 0.0
            return self.value

        if diff > 0:
            self._lower_since = 0.0
            tau = max(self.attack, 1e-3)
        else:
            self._lower_since += dt
            if self._lower_since < self.hold and not skip_hold:
                return self.value
            tau = max(self.release, 1e-3)
        # Exponential ease-out, plus a minimum speed so the tail ends in about 4 * tau
        # instead of creeping along for seconds.
        step = abs(diff) * (1.0 - math.exp(-dt / tau))
        step = max(step, MAX_ALPHA / (4.0 * tau) * dt)
        self.value += math.copysign(min(step, abs(diff)), diff)

        if abs(target - self.value) < 0.5:
            self.value = target
            self.settled = True
        else:
            self.settled = False
        return self.value
