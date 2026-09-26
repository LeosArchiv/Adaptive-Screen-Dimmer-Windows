import numpy as np
import pytest

from dimmer.logic import (
    ATTACK_PRESETS,
    RELEASE_PRESETS,
    Smoother,
    brightness,
    compensate,
    target_opacity,
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
