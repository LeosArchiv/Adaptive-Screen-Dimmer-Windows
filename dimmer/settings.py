"""Persistent user settings stored as JSON in %APPDATA%\\AdaptiveScreenDimmer."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .profiles import (
    AUTO,
    OFF_PROFILE,
    Profile,
    Rule,
    Schedule,
    clamp,
    default_profiles,
    default_rules,
    format_hhmm,
    parse_hhmm,
)

APP_NAME = "AdaptiveScreenDimmer"
VERSION = 2


def config_dir() -> Path:
    override = os.environ.get("ASD_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


@dataclass
class Settings:
    profiles: list[Profile] = field(default_factory=default_profiles)
    rules: list[Rule] = field(default_factory=default_rules)
    schedule: Schedule = field(default_factory=Schedule)
    monitors: list[str] = field(default_factory=list)  # stable monitor ids; empty = primary only
    monitor_profiles: dict[str, str] = field(default_factory=dict)  # monitor id -> profile or "auto"
    interval_ms: int = 50  # measurement interval
    hotkey: bool = True
    start_paused: bool = False
    close_to_tray: bool = True  # window close button hides to the notification area
    start_minimized: bool = False
    version: int = VERSION

    # ---- helpers ---------------------------------------------------------------------
    def profile_map(self) -> dict[str, Profile]:
        return {p.name: p for p in self.profiles}

    def rule_map(self) -> dict[str, str]:
        return {r.exe: r.profile for r in self.rules}

    def base_choice(self, device: str) -> str:
        return self.monitor_profiles.get(device, AUTO)

    def normalized(self) -> Settings:
        s = dataclasses.replace(self)
        # profiles: valid, unique names, built-in "Aus" always present
        profiles: list[Profile] = []
        seen: set[str] = set()
        for p in s.profiles if isinstance(s.profiles, list) else []:
            if not isinstance(p, Profile):
                continue
            p = p.normalized()
            if p.name in seen:
                continue
            seen.add(p.name)
            profiles.append(p)
        if not any(not p.builtin for p in profiles):
            profiles = [p for p in default_profiles() if not p.builtin] + [p for p in profiles if p.builtin]
            seen = {p.name for p in profiles}
        if OFF_PROFILE not in seen:
            profiles.append(Profile(OFF_PROFILE).normalized())
        s.profiles = profiles
        names = {p.name for p in profiles}
        first = next(p.name for p in profiles if not p.builtin)

        rules: dict[str, str] = {}
        for r in s.rules if isinstance(s.rules, list) else []:
            if isinstance(r, Rule):
                exe = str(r.exe).strip().lower()
                if exe and r.profile in names:
                    rules[exe] = r.profile
        s.rules = [Rule(exe, prof) for exe, prof in sorted(rules.items())]

        sch = s.schedule if isinstance(s.schedule, Schedule) else Schedule()
        sch = dataclasses.replace(
            sch,
            enabled=sch.enabled if isinstance(sch.enabled, bool) else True,
            day_profile=sch.day_profile if sch.day_profile in names else first,
            night_profile=sch.night_profile if sch.night_profile in names else first,
            day_start=format_hhmm(parse_hhmm(sch.day_start, 7 * 60)),
            night_start=format_hhmm(parse_hhmm(sch.night_start, 20 * 60)),
            fade_minutes=clamp(sch.fade_minutes, 0, 180),
        )
        s.schedule = sch

        monitors = s.monitors if isinstance(s.monitors, list) else []
        s.monitors = [m for m in monitors if isinstance(m, str)]
        mp = s.monitor_profiles if isinstance(s.monitor_profiles, dict) else {}
        s.monitor_profiles = {str(k): v for k, v in mp.items() if isinstance(v, str) and (v in names) and v != AUTO}
        s.interval_ms = clamp(s.interval_ms, 16, 500)
        defaults = Settings()
        for name in ("hotkey", "start_paused", "close_to_tray", "start_minimized"):
            if not isinstance(getattr(s, name), bool):  # "false" as a string must not mean True
                setattr(s, name, getattr(defaults, name))
        s.version = VERSION
        return s


# ---- (de)serialisation ---------------------------------------------------------------------
def _build(cls: type, raw: Any) -> Any:
    """Dataclass from a dict, ignoring unknown keys; None when raw is unusable."""
    if not isinstance(raw, dict):
        return None
    names = {f.name for f in dataclasses.fields(cls)}
    try:
        return cls(**{k: v for k, v in raw.items() if k in names})
    except TypeError:
        return None


def from_dict(raw: dict[str, Any]) -> Settings:
    if "profiles" not in raw and ("start" in raw or "excluded_apps" in raw):
        raw = _migrate_v1(raw)
    s = Settings()
    if isinstance(raw.get("profiles"), list):
        s.profiles = [p for p in (_build(Profile, x) for x in raw["profiles"]) if p]
    if isinstance(raw.get("rules"), list):
        s.rules = [r for r in (_build(Rule, x) for x in raw["rules"]) if r]
    s.schedule = _build(Schedule, raw.get("schedule")) or Schedule()
    for key in (
        "monitors",
        "monitor_profiles",
        "interval_ms",
        "hotkey",
        "start_paused",
        "close_to_tray",
        "start_minimized",
    ):
        if key in raw:
            setattr(s, key, raw[key])
    return s.normalized()


def _migrate_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Version 1 had one flat set of values plus a list of excluded apps."""
    profiles = [asdict(p) for p in default_profiles()]
    for key in ("start", "full", "max_opacity", "attack", "release"):
        if key in raw:
            profiles[0][key] = raw[key]  # "Tag" keeps the values the user had tuned
    rules = [asdict(r) for r in default_rules()]
    for exe in raw.get("excluded_apps") or []:
        if isinstance(exe, str):
            rules.append({"exe": exe, "profile": OFF_PROFILE})
    out = {k: v for k, v in raw.items() if k not in ("start", "full", "max_opacity", "attack", "release")}
    out.update(profiles=profiles, rules=rules)
    return out


def load(path: Path | None = None) -> Settings:
    path = path or config_dir() / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings().normalized()
    if not isinstance(raw, dict):
        return Settings().normalized()
    try:
        return from_dict(raw)
    except (TypeError, ValueError, AttributeError):
        return Settings().normalized()


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or config_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings.normalized()), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
