"""GPU reduction must be bit-identical to the CPU path (synthetic images only)."""

import numpy as np
import pytest

from dimmer.logic import brightness, frame_stats, tile_sums

gpu_mod = pytest.importorskip("dimmer.gpu")


@pytest.fixture(scope="module")
def gpu():
    try:
        g = gpu_mod.GpuBrightness()
    except OSError as e:  # no D3D11 hardware device (e.g. some VMs)
        pytest.skip(f"no GPU: {e}")
    yield g
    g.close()


@pytest.mark.parametrize("shape", [(1080, 1920, 4), (1081, 1917, 4), (16, 16, 4), (3, 5, 4), (2160, 3840, 4)])
def test_gpu_tiles_equal_cpu_tiles(gpu, shape) -> None:
    img = np.random.default_rng(3).integers(0, 256, shape, dtype=np.uint8)
    gpu_tiles = gpu.upload_for_test(img)
    cpu_tiles = tile_sums(img)
    assert np.array_equal(gpu_tiles.astype(np.uint64), cpu_tiles)
    h, w = shape[:2]
    assert frame_stats(gpu_tiles, w, h).mean == pytest.approx(brightness(img), abs=1e-9)
