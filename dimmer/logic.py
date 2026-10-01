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


TILE = 16  # pixels per tile edge (matches the GPU compute shader's thread groups)
SPOT_BLOCK = 8  # tiles per spot block edge: 128 x 128 px, about 0.8 % of a 1080p screen


def tile_sums(img: np.ndarray) -> np.ndarray:
    """Exact B+G+R sum of every 16x16 tile of a BGRA/BGR image (edge tiles may be smaller)."""
    h, w = img.shape[:2]
    rows = np.add.reduceat(img[:, :, :3], np.arange(0, h, TILE), axis=0, dtype=np.uint32)
    tiles = np.add.reduceat(rows, np.arange(0, w, TILE), axis=1, dtype=np.uint64)
    return tiles.sum(axis=2, dtype=np.uint64)


@dataclass(frozen=True)
class FrameStats:
    """What one captured frame tells the dimmer (brightness values 0..255)."""

    mean: float  # exact mean over all pixels (the classic metric)
    spot: float  # brightest 128 x 128 px block anywhere on the screen
    background: float  # median tile brightness: what the eyes are adapted to
    black_share: float  # share of tiles that are exactly black (protected video shows as black)

    @classmethod
    def flat(cls, mean: float) -> FrameStats:
        return cls(mean, mean, mean, 0.0)


def frame_stats(tiles: np.ndarray, width: int, height: int) -> FrameStats:
    """Statistics from exact tile sums.

    The spot value finds a small very bright area in an otherwise dark picture (a flashlight
    in a dark film scene) that the mean hides.
    """
    tiles = tiles.astype(np.uint64, copy=False)
    mean = float(tiles.sum(dtype=np.uint64)) / (width * height * 3)
    gy, gx = tiles.shape
    rows = np.minimum(TILE, height - np.arange(gy) * TILE)
    cols = np.minimum(TILE, width - np.arange(gx) * TILE)
    counts = np.outer(rows, cols).astype(np.uint64)
    tile_means = tiles / (counts * 3)
    background = float(np.median(tile_means))
    black_share = float(np.count_nonzero(tiles == 0)) / tiles.size
    b = min(SPOT_BLOCK, gy, gx)
    # Sliding block (step: one tile) via integral images, so a spot is found wherever it sits,
    # not only when it happens to align with a fixed grid.
    block_sums = _window_sums(tiles, b)
    block_counts = _window_sums(counts, b)
    # ignore slivers at the right/bottom edge: a few pixels must not count as a "block"
    full = block_counts >= (TILE * b) ** 2 // 2
    spot = float((block_sums[full] / (block_counts[full] * 3)).max()) if full.any() else mean
    return FrameStats(mean, max(spot, mean), background, black_share)


def _window_sums(a: np.ndarray, b: int) -> np.ndarray:
    """Sums of all b x b windows of a 2-D array (integral image)."""
    ii = np.zeros((a.shape[0] + 1, a.shape[1] + 1), dtype=np.uint64)
    ii[1:, 1:] = a.cumsum(axis=0, dtype=np.uint64).cumsum(axis=1, dtype=np.uint64)
    return ii[b:, b:] - ii[:-b, b:] - ii[b:, :-b] + ii[:-b, :-b]


GLARE_FLOOR = 6.0  # darkest adaptation level assumed (display black + some room light)
GLARE_MIN_SPOT = 128.0  # spots darker than this never count as glare


def glare_level(stats: FrameStats, weight: float) -> float:
    """Brightness the dimming reacts to: the mean, raised when a small bright spot glares.

    Discomfort glare grows with the spot's brightness relative to what the eyes are adapted
    to (glare indices such as UGR use the ratio of source to background luminance). So the
    same white spot counts fully in a dark film scene and hardly on a mid-grey page:
    contrast 4:1 or less -> no glare, 32:1 or more -> full effect.
    """
    if weight <= 0 or stats.spot <= stats.mean:
        return stats.mean
    contrast = stats.spot / max(stats.background, GLARE_FLOOR)
    strength = min(1.0, max(0.0, (math.log2(max(contrast, 1e-9)) - 2.0) / 3.0))
    strength *= min(1.0, max(0.0, (stats.spot - GLARE_MIN_SPOT) / 64.0))
    return stats.mean + weight * strength * (stats.spot - stats.mean)


LOCAL_MIN_CAP = 96.0  # a spot is never darkened below this brightness (keeps highlights intact)


@dataclass(frozen=True)
class LocalParams:
    """User settings of local dimming."""

    contrast: float = 6.0  # a glaring area is limited to this many times the background
    max_alpha: float = 0.75  # never black out an area completely
    margin: int = 1  # widen the mask by this many tiles (soft rim of the spot, motion)
    release_s: float = 0.3  # the mask fades out over roughly this time when the glare is gone


def tile_means(tiles: np.ndarray, width: int, height: int) -> np.ndarray:
    """Mean brightness (0..255) of every tile, edge tiles weighted by their real size."""
    gy, gx = tiles.shape
    rows = np.minimum(TILE, height - np.arange(gy) * TILE)
    cols = np.minimum(TILE, width - np.arange(gx) * TILE)
    return tiles / (np.outer(rows, cols) * 3.0)


def local_mask(
    means: np.ndarray, background: float, previous: np.ndarray | None, dt: float, params: LocalParams | None = None
) -> np.ndarray:
    """Per-tile overlay alpha (0..1) that darkens only glaring areas.

    A tile brighter than ``cap`` (a multiple of the background the eyes are adapted to) is
    darkened to about the cap: alpha = 1 - cap / brightness, because a black overlay scales the
    displayed value by (1 - alpha). The mask is widened by one tile (covers the spot's soft rim
    and a frame of motion) and blurred, so it has no hard block edges. It appears at once with
    the glare but fades out over release_s: no pumping, no hard dark "ghost" jumping away.
    """
    params = params or LocalParams()
    return blend_mask(local_target(means, background, params), previous, dt, params.release_s)


def local_target(means: np.ndarray, background: float, params: LocalParams | None = None) -> np.ndarray:
    """The mask the current picture asks for (before temporal smoothing)."""
    params = params or LocalParams()
    cap = max(LOCAL_MIN_CAP, max(background, GLARE_FLOOR) * params.contrast)
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha = np.where(means > cap, 1.0 - cap / np.maximum(means, 1e-6), 0.0)
    alpha = np.minimum(alpha, params.max_alpha)
    for _ in range(params.margin):
        alpha = _max3(alpha)  # widen by one tile
    return _box3(_box3(alpha)).astype(np.float32)  # soften


def blend_mask(
    target: np.ndarray, previous: np.ndarray | None, dt: float, release_s: float = LocalParams.release_s
) -> np.ndarray:
    """Appear at once, fade out over release_s."""
    alpha = target
    if previous is not None and previous.shape == target.shape:
        decay = math.exp(-max(0.0, dt) / max(release_s, 0.01))
        alpha = np.maximum(target, previous * decay)
    alpha = alpha.astype(np.float32)
    alpha[alpha < 0.01] = 0.0
    return alpha


def _max3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    out = a.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            np.maximum(out, p[dy : dy + a.shape[0], dx : dx + a.shape[1]], out=out)
    return out


def _box3(a: np.ndarray) -> np.ndarray:
    p = np.pad(a, 1, mode="edge")
    acc = np.zeros_like(a, dtype=np.float64)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            acc += p[dy : dy + a.shape[0], dx : dx + a.shape[1]]
    return acc / 9.0


def compensate(observed: float, alpha: float, tint_alpha: float = 0.0, tint_level: float = 0.0) -> float:
    """Undo overlays that are part of a capture (only when they cannot be excluded from it).

    A black overlay with alpha a turns a pixel value v into v * (1 - a/255). A tint overlay
    below it (alpha t, mean channel value T) first turns v into v * (1 - t/255) + T * t/255.
    """
    visible = 1.0 - max(0.0, min(float(MAX_ALPHA), alpha)) / MAX_ALPHA
    if visible <= 0.02:
        return 255.0
    level = observed / visible
    t = max(0.0, min(float(MAX_ALPHA), tint_alpha)) / MAX_ALPHA
    if t > 0:
        if t >= 0.98:
            return 0.0
        level = (level - tint_level * t) / (1.0 - t)
    return max(0.0, min(255.0, level))


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
        if abs(diff) < 0.5:  # on target: settled, and any pending hold is cancelled
            self.value = target
            self.settled = True
            self._lower_since = 0.0
            return self.value
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
                self.settled = False  # waiting to brighten is still "in motion" for the caller
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
