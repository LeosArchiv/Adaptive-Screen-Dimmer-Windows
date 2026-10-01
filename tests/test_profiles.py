import pytest

from dimmer.profiles import (
    GLARE_WEIGHTS,
    Profile,
    format_hhmm,
    kelvin_to_rgb,
    parse_hhmm,
    resolve,
    tint_active_now,
)

H = 60


def test_parse_and_format_time() -> None:
    assert parse_hhmm("07:30", 0) == 7 * H + 30
    assert parse_hhmm("24:00", 99) == 99
    assert parse_hhmm("abc", 5) == 5
    assert format_hhmm(20 * H + 5) == "20:05"
    assert format_hhmm(24 * H + 1) == "00:01"


def test_profile_normalisation() -> None:
    p = Profile(
        start=999,
        full=3,
        max_opacity=255,
        attack="?",
        release=None,  # type: ignore[arg-type]
        glare=9,
        tint_on="yes",  # type: ignore[arg-type]
        tint_kelvin=100,
        tint_strength=99,
        tint_night_only="false",  # type: ignore[arg-type]
        night_start="25:00",
        day_start=" 6:5 ",
    ).normalized()
    assert p.start == 250 and p.full == 251 and p.max_opacity == 240
    assert p.attack == "Schnell" and p.release == "Normal" and p.glare == 3
    assert p.tint_on is False and p.tint_night_only is True
    assert p.tint_kelvin == 1900 and p.tint_strength == 60
    assert p.night_start == "20:00" and p.day_start == "06:05"


def test_defaults_are_already_normal() -> None:
    assert Profile().normalized() == Profile()


@pytest.mark.parametrize(
    ("minute", "on"),
    [
        (19 * H + 59, False),
        (20 * H, True),
        (23 * H, True),
        (0, True),
        (6 * H + 59, True),
        (7 * H, False),
        (12 * H, False),
    ],
)
def test_night_window_across_midnight(minute: int, on: bool) -> None:
    p = Profile(tint_on=True, night_start="20:00", day_start="07:00")
    assert tint_active_now(p, minute) is on
    e = resolve(p, minute)
    assert e.tint_on is on and e.tint_strength == (30 if on else 0)


@pytest.mark.parametrize(("minute", "on"), [(1 * H, False), (2 * H, True), (4 * H + 59, True), (5 * H, False)])
def test_night_window_within_one_day(minute: int, on: bool) -> None:
    p = Profile(tint_on=True, night_start="02:00", day_start="05:00")
    assert tint_active_now(p, minute) is on


def test_equal_times_mean_no_night() -> None:
    p = Profile(tint_on=True, night_start="07:00", day_start="07:00")
    assert not any(tint_active_now(p, m) for m in range(0, 24 * H, 15))
    all_day = Profile(tint_on=True, tint_night_only=False, night_start="07:00", day_start="07:00")
    assert tint_active_now(all_day, 0)


def test_tint_all_day_and_off() -> None:
    assert tint_active_now(Profile(tint_on=True, tint_night_only=False), 12 * H)
    assert not tint_active_now(Profile(tint_on=False, tint_night_only=False), 12 * H)
    assert not tint_active_now(Profile(tint_on=True, tint_night_only=False, tint_strength=0), 12 * H)


def test_resolve_carries_dimming_and_glare() -> None:
    e = resolve(Profile(start=20, full=90, max_opacity=200, attack="Sofort", glare=1), 12 * H)
    assert e.dim_on and (e.start, e.full, e.max_opacity, e.attack) == (20, 90, 200, "Sofort")
    assert e.glare_weight == GLARE_WEIGHTS[1] and not e.glare_local
    local = resolve(Profile(glare=3), 12 * H)
    assert local.glare_local and local.glare_weight == 0.0


def test_kelvin_colours_get_warmer() -> None:
    warm, neutral = kelvin_to_rgb(2000), kelvin_to_rgb(6500)
    assert warm[0] == 255 and warm[2] < 60
    assert neutral[2] > 240
    assert warm[2] < kelvin_to_rgb(3400)[2] < neutral[2]
