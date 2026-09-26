import pytest

from dimmer.profiles import (
    AUTO,
    INHERIT,
    OFF,
    OFF_PROFILE,
    OWN,
    Profile,
    Schedule,
    blend,
    default_profiles,
    effective_of,
    format_hhmm,
    kelvin_to_rgb,
    parse_hhmm,
    resolve_monitor,
    schedule_phase,
)

H = 60  # minutes per hour


def profiles() -> dict[str, Profile]:
    return {p.name: p for p in default_profiles()}


def test_parse_and_format_time() -> None:
    assert parse_hhmm("07:30", 0) == 7 * H + 30
    assert parse_hhmm("24:00", 99) == 99
    assert parse_hhmm("abc", 5) == 5
    assert format_hhmm(20 * H + 5) == "20:05"
    assert format_hhmm(24 * H + 1) == "00:01"


@pytest.mark.parametrize(
    ("minute", "expected"),
    [
        (12 * H, ("Tag", "Tag", 1.0)),
        (3 * H, ("Nacht", "Nacht", 1.0)),
        (20 * H + 15, ("Tag", "Nacht", 0.5)),  # halfway through the evening fade
        (7 * H + 30, ("Tag", "Tag", 1.0)),  # morning fade (30 min) is over
        (7 * H + 6, ("Nacht", "Tag", 0.2)),
    ],
)
def test_schedule_phase(minute: int, expected: tuple[str, str, float]) -> None:
    a, b, t = schedule_phase(Schedule(), minute)
    assert (a, b) == expected[:2]
    assert t == pytest.approx(expected[2])


def test_schedule_night_across_midnight_and_no_fade() -> None:
    s = Schedule(day_start="06:00", night_start="23:30", fade_minutes=0)
    assert schedule_phase(s, 0)[:2] == ("Nacht", "Nacht")
    assert schedule_phase(s, 23 * H + 29)[:2] == ("Tag", "Tag")
    assert schedule_phase(s, 23 * H + 30)[:2] == ("Nacht", "Nacht")


def test_blend_is_continuous_and_fades_tint_in() -> None:
    p = profiles()
    day, night = effective_of(p["Tag"]), effective_of(p["Nacht"])
    assert not day.tint_on and night.tint_on
    mid = blend(day, night, 0.5)
    assert mid.tint_kelvin == night.tint_kelvin  # colour stays, only strength fades
    assert mid.tint_strength == pytest.approx(night.tint_strength / 2)
    assert mid.start == pytest.approx((day.start + night.start) / 2)
    assert blend(day, night, 0) == day and blend(day, night, 1) == night


def test_app_profile_inherits_tint_from_night() -> None:
    p = profiles()
    rules = {"ddnet.exe": "Zocken"}
    e = resolve_monitor(p, rules, Schedule(), AUTO, "ddnet.exe", 23 * H)
    assert e.attack == "Sofort"  # from Zocken
    assert e.tint_on and e.tint_strength == p["Nacht"].tint_strength  # from Nacht
    assert e.label == "Zocken + Nacht"
    assert "ddnet.exe" in e.reason


def test_app_profile_by_day_has_no_tint() -> None:
    e = resolve_monitor(profiles(), {"ddnet.exe": "Zocken"}, Schedule(), AUTO, "ddnet.exe", 12 * H)
    assert not e.tint_on and e.dim_on
    assert e.label == "Zocken + Tag"


def test_off_profile_switches_everything_off() -> None:
    e = resolve_monitor(profiles(), {"photoshop.exe": OFF_PROFILE}, Schedule(), AUTO, "photoshop.exe", 23 * H)
    assert not e.dim_on and not e.tint_on and e.max_opacity == 0
    assert e.label == OFF_PROFILE


def test_unknown_app_uses_base() -> None:
    e = resolve_monitor(profiles(), {"ddnet.exe": "Zocken"}, Schedule(), AUTO, "notepad.exe", 12 * H)
    assert e.label == "Tag"


def test_fixed_base_profile_ignores_schedule() -> None:
    e = resolve_monitor(profiles(), {}, Schedule(), "Arbeit", None, 23 * H)
    assert e.label == "Arbeit"
    assert not e.tint_on  # Arbeit inherits tint, but a base has nothing to inherit from


def test_schedule_disabled_uses_day_profile() -> None:
    e = resolve_monitor(profiles(), {}, Schedule(enabled=False), AUTO, None, 23 * H)
    assert e.label == "Tag"


def test_profile_normalisation() -> None:
    p = Profile(
        "  X ", dim_mode="weird", start=999, full=3, max_opacity=255, tint_kelvin=100, tint_strength=99, attack="?"
    ).normalized()
    assert p.name == "X" and p.dim_mode == OWN
    assert p.start == 250 and p.full == 251 and p.max_opacity == 240
    assert p.tint_kelvin == 1900 and p.tint_strength == 60 and p.attack == "Schnell"
    off = Profile(OFF_PROFILE, dim_mode=OWN, tint_mode=OWN).normalized()
    assert off.dim_mode == OFF and off.tint_mode == OFF


def test_kelvin_colours_get_warmer() -> None:
    warm, neutral = kelvin_to_rgb(2000), kelvin_to_rgb(6500)
    assert warm[0] == 255 and warm[2] < 60
    assert neutral[2] > 240
    assert warm[2] < kelvin_to_rgb(3400)[2] < neutral[2]


def test_inherit_mode_constant_used_in_defaults() -> None:
    assert profiles()["Zocken"].tint_mode == INHERIT
