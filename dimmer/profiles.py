"""Profiles, app rules and the day/night schedule. Pure logic, no Win32 or GUI.

Which values apply to a monitor is decided per monitor:

1. its *base profile*: a fixed profile, or "automatic" = day/night profile by time of day
   (with a smooth transition),
2. overlaid by an *app profile* when an app with a rule is in front on that monitor.

Each profile group (dimming, blue-light filter) is either set by the profile itself
("own"), switched off ("off"), or taken over from the base profile ("inherit"). That is how
mixes such as "Zocken + Nacht" come about without extra profiles.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from .logic import ATTACK_PRESETS, RELEASE_PRESETS

OWN, INHERIT, OFF = "own", "inherit", "off"
MODES = (OWN, INHERIT, OFF)
OFF_PROFILE = "Aus"  # built-in: never dims, never tints (replaces the old per-app exceptions)
AUTO = "auto"  # base profile of a monitor chosen by the day/night schedule

KELVIN_MIN, KELVIN_MAX = 1900, 6500
TINT_MAX = 60  # percent; stronger tints wash the picture out too much
MAX_OPACITY = 240  # never fully black
# How strongly the brightest 128 px block counts compared to the mean (0 = only the mean).
GLARE_WEIGHTS = (0.0, 0.5, 0.7)
GLARE_LABELS = ("aus", "normal", "stark")


@dataclass
class Profile:
    name: str
    dim_mode: str = OWN
    start: int = 25  # dimming begins above this brightness (0..255)
    full: int = 100  # strongest dimming reached at this brightness
    max_opacity: int = 240  # strongest dimming as overlay alpha
    attack: str = "Schnell"
    release: str = "Normal"
    tint_mode: str = OFF
    tint_kelvin: int = 3400
    tint_strength: int = 25  # percent
    glare: int = 0  # protection against small bright spots in dark pictures: 0 off, 1 normal, 2 strong

    @property
    def builtin(self) -> bool:
        return self.name == OFF_PROFILE

    def normalized(self) -> Profile:
        p = dataclasses.replace(self)
        p.name = str(p.name).strip()[:40] or "Profil"
        p.dim_mode = p.dim_mode if p.dim_mode in MODES else OWN
        p.tint_mode = p.tint_mode if p.tint_mode in MODES else OFF
        p.start = clamp(p.start, 0, 250)
        p.full = clamp(p.full, p.start + 1, 255)
        p.max_opacity = clamp(p.max_opacity, 0, MAX_OPACITY)
        if not isinstance(p.attack, str) or p.attack not in ATTACK_PRESETS:
            p.attack = "Schnell"
        if not isinstance(p.release, str) or p.release not in RELEASE_PRESETS:
            p.release = "Normal"
        p.tint_kelvin = clamp(p.tint_kelvin, KELVIN_MIN, KELVIN_MAX)
        p.tint_strength = clamp(p.tint_strength, 0, TINT_MAX)
        p.glare = clamp(p.glare, 0, len(GLARE_WEIGHTS) - 1)
        if p.builtin:
            p.dim_mode = OFF
            p.tint_mode = OFF
        return p


@dataclass
class Rule:
    exe: str  # lower-case exe name, e.g. "ddnet.exe"
    profile: str


@dataclass
class Schedule:
    enabled: bool = True
    day_profile: str = "Tag"
    night_profile: str = "Nacht"
    day_start: str = "07:00"
    night_start: str = "20:00"
    fade_minutes: int = 30


def default_profiles() -> list[Profile]:
    return [
        Profile("Tag"),
        Profile(
            "Nacht", start=15, full=70, release="Langsam", tint_mode=OWN, tint_kelvin=3400, tint_strength=30, glare=1
        ),
        Profile("Arbeit", start=40, full=140, max_opacity=200, attack="Sanft", tint_mode=INHERIT),
        Profile("Zocken", start=30, full=110, max_opacity=230, attack="Sofort", release="Schnell", tint_mode=INHERIT),
        Profile(
            "Filme", start=60, full=170, max_opacity=160, attack="Sanft", release="Langsam", tint_mode=INHERIT, glare=1
        ),
        Profile(OFF_PROFILE, dim_mode=OFF, tint_mode=OFF),
    ]


def default_rules() -> list[Rule]:
    return [Rule("ddnet.exe", "Zocken"), Rule("vlc.exe", "Filme")]


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


# ---- resolution ------------------------------------------------------------------------
@dataclass(frozen=True)
class Effective:
    """The values that actually apply to one monitor right now."""

    dim_on: bool = False
    start: float = 25
    full: float = 100
    max_opacity: float = 0
    attack: str = "Schnell"
    release: str = "Normal"
    tint_on: bool = False
    tint_kelvin: float = 3400
    tint_strength: float = 0  # percent
    glare_weight: float = 0.0
    label: str = ""
    reason: str = ""


def effective_of(p: Profile, reason: str = "") -> Effective:
    """A profile on its own ('inherit' has nothing to inherit from and means off)."""
    dim = p.dim_mode == OWN
    tint = p.tint_mode == OWN and p.tint_strength > 0
    return Effective(
        dim_on=dim,
        start=p.start,
        full=p.full,
        max_opacity=p.max_opacity if dim else 0,
        glare_weight=GLARE_WEIGHTS[p.glare] if dim else 0.0,
        attack=p.attack,
        release=p.release,
        tint_on=tint,
        tint_kelvin=p.tint_kelvin,
        tint_strength=p.tint_strength if tint else 0,
        label=p.name,
        reason=reason,
    )


def overlay_app(base: Effective, app: Profile, exe: str) -> Effective:
    """Apply an app profile on top of the monitor's base values."""
    e = base
    if app.dim_mode == OWN:
        e = dataclasses.replace(
            e,
            dim_on=True,
            start=app.start,
            full=app.full,
            max_opacity=app.max_opacity,
            glare_weight=GLARE_WEIGHTS[app.glare],
            attack=app.attack,
            release=app.release,
        )
    elif app.dim_mode == OFF:
        e = dataclasses.replace(e, dim_on=False, max_opacity=0)
    if app.tint_mode == OWN:
        on = app.tint_strength > 0
        e = dataclasses.replace(
            e, tint_on=on, tint_kelvin=app.tint_kelvin, tint_strength=app.tint_strength if on else 0
        )
    elif app.tint_mode == OFF:
        e = dataclasses.replace(e, tint_on=False, tint_strength=0)
    inherits = INHERIT in (app.dim_mode, app.tint_mode) and base.label and app.name != OFF_PROFILE
    label = f"{app.name} + {base.label}" if inherits else app.name
    return dataclasses.replace(e, label=label, reason=f"Programm {exe}")


def blend(a: Effective, b: Effective, t: float) -> Effective:
    """Linear mix for smooth day/night transitions (t=0 -> a, t=1 -> b)."""
    t = max(0.0, min(1.0, t))
    if t <= 0:
        return a
    if t >= 1:
        return b

    def mix(x: float, y: float) -> float:
        return x + (y - x) * t

    # A group that is off on one side fades from/to zero strength with the other side's colour.
    a_kelvin = a.tint_kelvin if a.tint_on else b.tint_kelvin
    b_kelvin = b.tint_kelvin if b.tint_on else a.tint_kelvin
    return Effective(
        dim_on=a.dim_on or b.dim_on,
        start=mix(a.start if a.dim_on else b.start, b.start if b.dim_on else a.start),
        full=mix(a.full if a.dim_on else b.full, b.full if b.dim_on else a.full),
        max_opacity=mix(a.max_opacity, b.max_opacity),
        glare_weight=mix(
            a.glare_weight if a.dim_on else b.glare_weight, b.glare_weight if b.dim_on else a.glare_weight
        ),
        attack=a.attack if t < 0.5 else b.attack,
        release=a.release if t < 0.5 else b.release,
        tint_on=a.tint_on or b.tint_on,
        tint_kelvin=mix(a_kelvin, b_kelvin),
        tint_strength=mix(a.tint_strength, b.tint_strength),
        label=f"{a.label} → {b.label}",
        reason=a.reason,
    )


def schedule_phase(schedule: Schedule, minute_of_day: float) -> tuple[str, str, float]:
    """(from_profile, to_profile, t) for the given time; t is the progress of a running fade."""
    day = parse_hhmm(schedule.day_start, 7 * 60)
    night = parse_hhmm(schedule.night_start, 20 * 60)
    if day == night:  # no night at all: never switch (and never jump)
        return schedule.day_profile, schedule.day_profile, 1.0
    # A fade never outlasts the shorter phase, otherwise the next switch would cut it off.
    shorter = min((night - day) % (24 * 60), (day - night) % (24 * 60))
    fade = max(0, min(180, int(schedule.fade_minutes), shorter))
    now = minute_of_day % (24 * 60)

    def since(start: int) -> float:
        return (now - start) % (24 * 60)

    # Whichever switch happened most recently decides the phase.
    if since(night) <= since(day):
        elapsed, prev, cur = since(night), schedule.day_profile, schedule.night_profile
    else:
        elapsed, prev, cur = since(day), schedule.night_profile, schedule.day_profile
    if fade and elapsed < fade:
        return prev, cur, elapsed / fade
    return cur, cur, 1.0


def resolve_monitor(
    profiles: dict[str, Profile],
    rules: dict[str, str],
    schedule: Schedule,
    base_choice: str,
    exe: str | None,
    minute_of_day: float,
) -> Effective:
    """Values for one monitor given its base profile choice and the app in front on it."""
    if base_choice != AUTO and base_choice in profiles:
        base = effective_of(profiles[base_choice], "fest eingestellt")
    elif schedule.enabled:
        a_name, b_name, t = schedule_phase(schedule, minute_of_day)
        a = profiles.get(a_name) or next(iter(profiles.values()))
        b = profiles.get(b_name) or a
        base = blend(effective_of(a, "Zeitplan"), effective_of(b, "Zeitplan"), t)
    else:
        first = profiles.get(schedule.day_profile) or next(iter(profiles.values()))
        base = effective_of(first, "Standard")
    app_profile = rules.get(exe) if exe else None
    if exe and app_profile and app_profile in profiles:
        return overlay_app(base, profiles[app_profile], exe)
    return base


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
