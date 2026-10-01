import numpy as np
import pytest

from dimmer.logic import (
    ATTACK_PRESETS,
    RELEASE_PRESETS,
    LocalParams,
    Smoother,
    blend_mask,
    brightness,
    compensate,
    frame_stats,
    glare_level,
    local_mask,
    local_target,
    target_opacity,
    tile_means,
    tile_sums,
)

# The mapping of the original single-file version, kept verbatim as reference.
OLD_START, OLD_MAX, OLD_OPACITY = 25, 100, 240


def old_brightness(img: np.ndarray) -> float:
    return float(np.mean(np.mean(img[:, :, :3], axis=2)))


def old_target(b: float) -> float:
    if b > OLD_MAX:
        return OLD_OPACITY
    if b > OLD_START:
        return (b - OLD_START) / (OLD_MAX - OLD_START) * OLD_OPACITY
    return 0


def synthetic_images() -> list[np.ndarray]:
    rng = np.random.default_rng(1)
    imgs = [
        np.zeros((90, 160, 4), np.uint8),
        np.full((90, 160, 4), 255, np.uint8),
        rng.integers(0, 256, (90, 160, 4), dtype=np.uint8),
    ]
    ide = np.full((90, 160, 4), 30, np.uint8)  # dark IDE with a white page on the right
    ide[:, 100:] = 250
    imgs.append(ide)
    stripes = np.zeros((90, 160, 4), np.uint8)
    stripes[::2] = 255
    imgs.append(stripes)
    only_blue = np.zeros((90, 160, 4), np.uint8)
    only_blue[..., 0] = 255
    only_blue[..., 3] = 17  # alpha must be ignored
    imgs.append(only_blue)
    return imgs


@pytest.mark.parametrize("img", synthetic_images())
def test_brightness_matches_original_metric(img: np.ndarray) -> None:
    assert brightness(img) == pytest.approx(old_brightness(img), abs=1e-9)


def test_brightness_empty() -> None:
    assert brightness(np.zeros((0, 0, 4), np.uint8)) == 0.0


@pytest.mark.parametrize("b", [0, 10, 25, 25.5, 40, 62.5, 99.9, 100, 100.1, 200, 255])
def test_target_matches_original_mapping(b: float) -> None:
    assert target_opacity(b, OLD_START, OLD_MAX, OLD_OPACITY) == pytest.approx(old_target(b))


def test_target_handles_inverted_thresholds() -> None:
    assert target_opacity(50, 60, 40, 200) == 0.0
    assert target_opacity(70, 60, 40, 200) == 200.0


@pytest.mark.parametrize("true_level", [0, 30, 128, 255])
@pytest.mark.parametrize("alpha", [0, 60, 150, 240])
def test_compensate_inverts_black_overlay(true_level: float, alpha: int) -> None:
    observed = true_level * (1 - alpha / 255)
    assert compensate(observed, alpha) == pytest.approx(true_level, abs=1e-6)


def test_compensate_saturates_when_overlay_nearly_opaque() -> None:
    assert compensate(0.0, 255) == 255.0


def run(sm: Smoother, targets: list[float], dt: float = 0.05) -> list[float]:
    return [sm.step(t, dt) for t in targets]


def test_attack_is_a_ramp_not_a_jump() -> None:
    for tau in ATTACK_PRESETS.values():
        sm = Smoother(attack=tau)
        values = run(sm, [240] * 40)
        assert values[0] < 240 or tau <= 0.03  # "Sofort" may nearly reach it in one tick
        assert all(b >= a for a, b in zip(values, values[1:])), "no overshoot/oscillation"
        assert values[-1] == 240


def test_fast_attack_reaches_90_percent_quickly() -> None:
    sm = Smoother(attack=ATTACK_PRESETS["Schnell"])
    values = run(sm, [240] * 10)
    first = next(i for i, v in enumerate(values) if v >= 216)
    assert (first + 1) * 0.05 <= 0.25


def test_release_waits_then_fades_monotonically() -> None:
    sm = Smoother(hold=0.4, release=RELEASE_PRESETS["Normal"])
    sm.reset(240)
    values = run(sm, [0] * 80)
    assert values[6] == 240, "hold phase keeps the value"
    assert all(b <= a for a, b in zip(values, values[1:]))
    assert values[-1] == 0
    done = next(i for i, v in enumerate(values) if v == 0) * 0.05
    assert done < 0.4 + 4 * RELEASE_PRESETS["Normal"] + 0.1


def test_strobe_does_not_pump() -> None:
    """Content flashing 5x per second must not make the overlay go up and down."""
    sm = Smoother()
    pattern = ([240] * 2 + [0] * 2) * 30  # 100 ms bright / 100 ms dark
    values = run(sm, pattern)
    settled = values[10:]
    assert min(settled) > 200
    assert max(settled) - min(settled) < 25


def test_deadband_ignores_noise_when_settled() -> None:
    sm = Smoother()
    sm.reset(120)
    values = run(sm, [120, 121.5, 118.8, 122.0, 119.1] * 10)
    assert set(values) == {120}


def test_deadband_does_not_block_real_changes() -> None:
    sm = Smoother()
    sm.reset(120)
    values = run(sm, [130] * 20)
    assert values[-1] == 130


def test_no_flicker_on_constant_bright_content() -> None:
    sm = Smoother()
    values = run(sm, [240] * 200)
    tail = values[20:]
    assert set(tail) == {240}


def test_deadband_never_keeps_faint_overlay() -> None:
    sm = Smoother()
    sm.reset(2.0)
    values = run(sm, [0] * 40)
    assert values[-1] == 0


def test_skip_hold_fades_out_at_once_and_smoothly() -> None:
    sm = Smoother(release=RELEASE_PRESETS["Normal"])
    sm.reset(240)
    values = [sm.step(0, 0.05, skip_hold=True) for _ in range(60)]
    assert values[0] < 240
    assert all(b <= a for a, b in zip(values, values[1:]))
    assert max(a - b for a, b in zip([240.0] + values, values)) < 40  # no visible stair steps
    assert values[-1] == 0


def test_large_dt_is_clamped() -> None:
    sm = Smoother()
    assert sm.step(240, 100.0) == 240  # clamped to 1 s, still finite and at target


def test_deadband_releases_targets_that_round_to_zero() -> None:
    sm = Smoother()
    sm.reset(2.6)
    values = run(sm, [0.5] * 40)
    assert round(values[-1]) == 0


@pytest.mark.parametrize("true_level", [0, 40, 128, 255])
def test_compensate_with_tint_below_dimming(true_level: float) -> None:
    dim, tint, tint_level = 180, 70, 190.0
    after_tint = true_level * (1 - tint / 255) + tint_level * tint / 255
    observed = after_tint * (1 - dim / 255)
    assert compensate(observed, dim, tint, tint_level) == pytest.approx(true_level, abs=1e-6)


def test_frame_stats_mean_matches_brightness() -> None:
    img = np.random.default_rng(5).integers(0, 256, (1000, 1500, 4), dtype=np.uint8)
    st = frame_stats(tile_sums(img), 1500, 1000)
    assert st.mean == pytest.approx(brightness(img), abs=1e-9)
    assert st.spot >= st.mean


def dark_scene(spot_value: int = 255, background: int = 12) -> np.ndarray:
    img = np.full((1080, 1920, 4), background, np.uint8)  # dark film scene
    img[100:260, 1600:1760, :3] = spot_value  # small flashlight spot (~1.2 % of the screen)
    return img


def test_spot_finds_flashlight_in_dark_scene() -> None:
    st = frame_stats(tile_sums(dark_scene()), 1920, 1080)
    assert st.mean < 20  # the mean hides it ...
    assert st.spot > 250  # ... the spot value does not, wherever it sits on the tile grid
    assert st.background == pytest.approx(12, abs=0.5)
    assert glare_level(st, 0.6) > 100  # enough to trigger dimming with profile "Filme"
    assert glare_level(st, 0.0) == st.mean  # glare protection off: classic behaviour


def test_same_spot_on_mid_grey_hardly_counts() -> None:
    st = frame_stats(tile_sums(dark_scene(background=110)), 1920, 1080)
    assert glare_level(st, 0.6) < st.mean + 10  # contrast only ~2:1, no discomfort glare


def test_dim_spot_is_not_glare() -> None:
    st = frame_stats(tile_sums(dark_scene(spot_value=110)), 1920, 1080)
    assert glare_level(st, 0.9) == pytest.approx(st.mean)


def test_small_bright_text_is_not_a_spot() -> None:
    img = np.full((1080, 1920, 4), 30, np.uint8)  # dark IDE
    img[200:1000:20, 100:900, :3] = 220  # 1 px bright text lines every 20 px
    st = frame_stats(tile_sums(img), 1920, 1080)
    assert st.spot < 60
    assert glare_level(st, 0.9) < st.mean + 5


def test_black_share_detects_blanked_video() -> None:
    img = np.zeros((1080, 1920, 4), np.uint8)
    img[:40, :, :3] = 60  # a browser toolbar is still visible
    st = frame_stats(tile_sums(img), 1920, 1080)
    assert st.black_share > 0.9
    dark = frame_stats(tile_sums(np.full((1080, 1920, 4), 3, np.uint8)), 1920, 1080)
    assert dark.black_share == 0.0  # a very dark but real picture is not "blanked"


def test_edge_slivers_do_not_count_as_blocks() -> None:
    img = np.zeros((1080, 1924, 4), np.uint8)
    img[:, 1920:, :3] = 255  # 4 px bright column at the right edge
    st = frame_stats(tile_sums(img), 1924, 1080)
    assert st.spot < 10


def test_hold_phase_is_not_settled() -> None:
    """Callers skip settled smoothers; during the hold before brightening it must keep stepping."""
    sm = Smoother(hold=0.4)
    sm.reset(240)
    sm.step(0, 0.05)
    assert sm.value == 240 and not sm.settled
    values = [sm.step(0, 0.05) for _ in range(80)]
    assert values[-1] == 0 and sm.settled


def test_local_mask_darkens_only_the_spot() -> None:
    img = dark_scene()
    st = frame_stats(tile_sums(img), 1920, 1080)
    means = tile_means(tile_sums(img), 1920, 1080)
    mask = local_mask(means, st.background, None, 0.05)
    spot_tile = mask[180 // 16, 1680 // 16]
    assert 0.5 < spot_tile <= LocalParams().max_alpha  # 255 -> about the cap (96) -> alpha ~0.62
    assert mask[600 // 16, 600 // 16] == 0.0  # the dark rest of the picture stays untouched
    assert mask[50 // 16, 50 // 16] == 0.0


def test_local_mask_ignores_normal_bright_content() -> None:
    img = np.full((1080, 1920, 4), 200, np.uint8)  # a bright web page: global dimming's job
    st = frame_stats(tile_sums(img), 1920, 1080)
    means = tile_means(tile_sums(img), 1920, 1080)
    assert not local_mask(means, st.background, None, 0.05).any()


def test_local_mask_fades_out_smoothly() -> None:
    img = dark_scene()
    st = frame_stats(tile_sums(img), 1920, 1080)
    mask = local_mask(tile_means(tile_sums(img), 1920, 1080), st.background, None, 0.05)
    dark = np.full((68, 120), 12.0)
    steps = [mask]
    for _ in range(40):
        steps.append(local_mask(dark, 12.0, steps[-1], 0.05))
    peaks = [float(m.max()) for m in steps]
    assert all(b <= a for a, b in zip(peaks, peaks[1:]))  # monotonic fade, no pumping
    assert max(a - b for a, b in zip(peaks, peaks[1:])) < 0.2  # no hard jump
    assert peaks[-1] == 0.0


def test_mask_target_is_kept_while_the_picture_does_not_change() -> None:
    """A paused film: no new frames, the bright spot must stay covered."""
    img = dark_scene()
    st = frame_stats(tile_sums(img), 1920, 1080)
    target = local_target(tile_means(tile_sums(img), 1920, 1080), st.background)
    mask = blend_mask(target, None, 0.05)
    for _ in range(100):
        mask = blend_mask(target, mask, 0.05)
    assert mask[180 // 16, 1680 // 16] > 0.5


def test_local_settings_change_the_mask() -> None:
    img = dark_scene()
    st = frame_stats(tile_sums(img), 1920, 1080)
    means = tile_means(tile_sums(img), 1920, 1080)
    normal = local_target(means, st.background)
    weak = local_target(means, st.background, LocalParams(max_alpha=0.3))
    tight = local_target(means, st.background, LocalParams(margin=0))
    wide = local_target(means, st.background, LocalParams(margin=2))
    assert weak.max() <= 0.3 + 1e-6 < normal.max()
    assert (tight > 0).sum() < (normal > 0).sum() < (wide > 0).sum()
    # less sensitive: a spot only six times brighter than the background is no longer touched
    assert local_target(means, st.background, LocalParams(contrast=12)).max() <= normal.max()


def test_local_fade_time() -> None:
    mask = np.full((4, 4), 0.6, np.float32)
    zero = np.zeros((4, 4), np.float32)
    quick = blend_mask(zero, mask, 0.3, release_s=0.1)
    slow = blend_mask(zero, mask, 0.3, release_s=1.5)
    assert quick.max() < slow.max()
