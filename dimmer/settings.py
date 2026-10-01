"""Persistent user settings stored as JSON in %APPDATA%\\AdaptiveScreenDimmer."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .profiles import Profile, clamp

APP_NAME = "AdaptiveScreenDimmer"
VERSION = 3
GENERAL_KEYS = ("monitors", "interval_ms", "hotkey", "start_paused", "close_to_tray", "start_minimized", "language")
LANGUAGES = ("auto", "de", "en")


def config_dir() -> Path:
    override = os.environ.get("ASD_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


@dataclass
class Settings:
    profile: Profile = field(default_factory=Profile)
    monitors: list[str] = field(default_factory=list)  # stable monitor ids; empty = primary only
    interval_ms: int = 50  # measurement interval
    hotkey: bool = True
    start_paused: bool = False
    close_to_tray: bool = True  # window close button hides to the notification area
    start_minimized: bool = False
    language: str = "auto"  # "auto" follows the Windows display language
    version: int = VERSION

    def normalized(self) -> Settings:
        s = dataclasses.replace(self)
        s.profile = s.profile.normalized() if isinstance(s.profile, Profile) else Profile()
        monitors = s.monitors if isinstance(s.monitors, list) else []
        s.monitors = [m for m in monitors if isinstance(m, str)]
        s.interval_ms = clamp(s.interval_ms, 16, 500)
        defaults = Settings()
        for name in ("hotkey", "start_paused", "close_to_tray", "start_minimized"):
            if not isinstance(getattr(s, name), bool):  # "false" as a string must not mean True
                setattr(s, name, getattr(defaults, name))
        if s.language not in LANGUAGES:
            s.language = defaults.language
        s.version = VERSION
        return s


# ---- (de)serialisation ---------------------------------------------------------------------
def _build_profile(raw: Any) -> Profile | None:
    """Profile from a dict, ignoring unknown keys; None when raw is unusable."""
    if not isinstance(raw, dict):
        return None
    names = {f.name for f in dataclasses.fields(Profile)}
    try:
        return Profile(**{k: v for k, v in raw.items() if k in names})
    except TypeError:
        return None


def from_dict(raw: dict[str, Any]) -> Settings:
    """Version 1 and 2 files keep only the general options; their profiles, rules and
    schedule are dropped on purpose (the profile starts from fresh defaults)."""
    s = Settings()
    if raw.get("version") == VERSION and "profile" in raw:
        if not isinstance(raw["profile"], dict):
            raise TypeError("profile must be an object")
        s.profile = _build_profile(raw["profile"]) or Profile()
    for key in GENERAL_KEYS:
        if key in raw:
            setattr(s, key, raw[key])
    return s.normalized()


def load(path: Path | None = None) -> Settings:
    path = path or config_dir() / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings().normalized()
    except (OSError, ValueError):
        _keep_broken(path)
        return Settings().normalized()
    if not isinstance(raw, dict):
        _keep_broken(path)
        return Settings().normalized()
    try:
        return from_dict(raw)
    except (TypeError, ValueError, AttributeError):
        _keep_broken(path)
        return Settings().normalized()


def _keep_broken(path: Path) -> None:
    """Keep an unreadable settings file as .bad before defaults overwrite it."""
    try:
        os.replace(path, path.with_suffix(".bad"))
    except OSError:
        pass


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or config_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings.normalized()), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
