"""The one profile and how it turns into the values that apply right now. Pure logic, no Win32 or GUI.

There is a single profile ("Standard"). Dimming always follows it; the blue-light filter can be
limited to a night window given by two clock times (no transition: the engine fades the tint
strength gently over about a second when it switches).
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from .logic import ATTACK_PRESETS, RELEASE_PRESETS, LocalParams

KELVIN_MIN, KELVIN_MAX = 1900, 6500
TINT_MAX = 60  # percent; stronger tints wash the picture out too much
MAX_OPACITY = 240  # never fully black
# How strongly the brightest 128 px block counts compared to the mean (0 = only the mean).
GLARE_WEIGHTS = (0.0, 0.6, 0.9, 0.0)  # "lokal" darkens only the spot, not the whole screen
GLARE_LABELS = ("aus", "normal", "stark", "lokal")
GLARE_LOCAL = 3
PROFILE_LABEL = "Standard"
DEFAULT_NIGHT = 20 * 60
DEFAULT_DAY = 7 * 60


@dataclass
class Profile:
    start: int = 30  # dimming begins above this brightness (0..250)
    full: int = 160  # strongest dimming reached at this brightness
    max_opacity: int = 150  # strongest dimming as overlay alpha (0..240)
    attack: str = "Schnell"
    release: str = "Normal"
    glare: int = 0  # 0 aus, 1 normal, 2 stark, 3 lokal
    glare_contrast: int = 6  # lokal: a spot counts when this many times brighter than the surroundings
    glare_strength: int = 75  # lokal: strongest darkening of a spot in percent
    glare_margin: int = 1  # lokal: extra border around a spot in 16 px tiles
    glare_fade_ms: int = 300  # lokal: how long the darkening takes to fade out
    tint_on: bool = False
    tint_kelvin: int = 3400
    tint_strength: int = 30  # percent
    tint_night_only: bool = True
    night_start: str = "20:00"
    day_start: str = "07:00"

    def normalized(self) -> Profile:
        p = dataclasses.replace(self)
        defaults = Profile()
        p.start = clamp(p.start, 0, 250)
        p.full = clamp(p.full, p.start + 1, 255)
        p.max_opacity = clamp(p.max_opacity, 0, MAX_OPACITY)
        if not isinstance(p.attack, str) or p.attack not in ATTACK_PRESETS:
            p.attack = defaults.attack
        if not isinstance(p.release, str) or p.release not in RELEASE_PRESETS:
            p.release = defaults.release
        p.glare = clamp(p.glare, 0, len(GLARE_WEIGHTS) - 1)
        p.glare_contrast = clamp(p.glare_contrast, 3, 12)
        p.glare_strength = clamp(p.glare_strength, 20, 90)
        p.glare_margin = clamp(p.glare_margin, 0, 2)
        p.glare_fade_ms = clamp(p.glare_fade_ms, 100, 1500)
        for name in ("tint_on", "tint_night_only"):
            if not isinstance(getattr(p, name), bool):  # "false" as a string must not mean True
                setattr(p, name, getattr(defaults, name))
        p.tint_kelvin = clamp(p.tint_kelvin, KELVIN_MIN, KELVIN_MAX)
        p.tint_strength = clamp(p.tint_strength, 0, TINT_MAX)
        p.night_start = format_hhmm(parse_hhmm(p.night_start, DEFAULT_NIGHT))
        p.day_start = format_hhmm(parse_hhmm(p.day_start, DEFAULT_DAY))
        return p


def clamp(value: object, lo: int, hi: int) -> int:
    try:
        v = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        v = lo
    return max(lo, min(hi, v))


def parse_hhmm(text: str, fallback: int) -> int:
    """'20:30' -> minutes after midnight; fallback on anything invalid."""
    try:
        hh, mm = str(text).strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return fallback
    if not (0 <= h < 24 and 0 <= m < 60):
        return fallback
    return h * 60 + m


def format_hhmm(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def is_night(p: Profile, minute_of_day: float) -> bool:
    """Inside [night_start, day_start), also across midnight. Equal times mean: no night."""
    night = parse_hhmm(p.night_start, DEFAULT_NIGHT)
    day = parse_hhmm(p.day_start, DEFAULT_DAY)
    if night == day:
        return False
    return (minute_of_day - night) % (24 * 60) < (day - night) % (24 * 60)


def tint_active_now(p: Profile, minute_of_day: float) -> bool:
    """Whether the blue-light filter should be on at this time of day."""
    if not p.tint_on or p.tint_strength <= 0:
        return False
    return not p.tint_night_only or is_night(p, minute_of_day)


# ---- resolution ------------------------------------------------------------------------
@dataclass(frozen=True)
class Effective:
    """The values that actually apply right now."""

    dim_on: bool = False
    start: float = 30
    full: float = 160
    max_opacity: float = 0
    attack: str = "Schnell"
    release: str = "Normal"
    tint_on: bool = False
    tint_kelvin: float = 3400
    tint_strength: float = 0  # percent
    glare_weight: float = 0.0
    glare_local: bool = False  # darken only glaring areas (local dimming layer)
    local: LocalParams = LocalParams()
    label: str = ""
    reason: str = ""


def resolve(p: Profile, minute_of_day: float) -> Effective:
    """Values of the profile at the given time (minutes after midnight)."""
    tint = tint_active_now(p, minute_of_day)
    return Effective(
        dim_on=True,
        start=p.start,
        full=p.full,
        max_opacity=p.max_opacity,
        attack=p.attack,
        release=p.release,
        tint_on=tint,
        tint_kelvin=p.tint_kelvin,
        tint_strength=p.tint_strength if tint else 0,
        glare_weight=GLARE_WEIGHTS[p.glare],
        glare_local=p.glare == GLARE_LOCAL,
        local=LocalParams(
            contrast=float(p.glare_contrast),
            max_alpha=p.glare_strength / 100,
            margin=p.glare_margin,
            release_s=p.glare_fade_ms / 1000,
        ),
        label=PROFILE_LABEL,
        reason="Nacht" if p.tint_on and p.tint_night_only and tint else "",
    )


# ---- colour ------------------------------------------------------------------------------
def kelvin_to_rgb(kelvin: float) -> tuple[int, int, int]:
    """Approximate colour of a black body (Tanner Helland's fit), used as the tint colour."""
    t = max(1000.0, min(40000.0, kelvin)) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    return tuple(int(max(0, min(255, round(c)))) for c in (r, g, b))  # type: ignore[return-value]
