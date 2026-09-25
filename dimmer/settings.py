"""Persistent user settings stored as JSON in %APPDATA%\\AdaptiveScreenDimmer."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .logic import ATTACK_PRESETS, RELEASE_PRESETS

APP_NAME = "AdaptiveScreenDimmer"


def config_dir() -> Path:
    override = os.environ.get("ASD_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


@dataclass
class Settings:
    start: int = 25  # dimming begins above this brightness (0..255)
    full: int = 100  # maximum dimming reached at this brightness
    max_opacity: int = 240  # strongest dimming as overlay alpha (0..240)
    interval_ms: int = 50  # measurement interval
    attack: str = "Schnell"
    release: str = "Normal"
    monitors: list[str] = field(default_factory=list)  # stable monitor ids; empty = primary only
    excluded_apps: list[str] = field(default_factory=list)  # lower-case exe names
    hotkey: bool = True
    start_paused: bool = False
    close_to_tray: bool = True  # window close button hides to the notification area
    start_minimized: bool = False

    def normalized(self) -> Settings:
        s = Settings(**asdict(self))
        s.start = _clamp(s.start, 0, 250)
        s.full = _clamp(s.full, s.start + 1, 255)
        s.max_opacity = _clamp(s.max_opacity, 0, 240)
        s.interval_ms = _clamp(s.interval_ms, 16, 500)
        if s.attack not in ATTACK_PRESETS:
            s.attack = Settings.attack
        if s.release not in RELEASE_PRESETS:
            s.release = Settings.release
        monitors = s.monitors if isinstance(s.monitors, list) else []
        s.monitors = [m for m in monitors if isinstance(m, str)]
        apps = s.excluded_apps if isinstance(s.excluded_apps, list) else []
        s.excluded_apps = sorted({str(a).strip().lower() for a in apps if str(a).strip()})
        s.hotkey = bool(s.hotkey)
        s.start_paused = bool(s.start_paused)
        s.close_to_tray = bool(s.close_to_tray)
        s.start_minimized = bool(s.start_minimized)
        return s


def _clamp(value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def load(path: Path | None = None) -> Settings:
    path = path or config_dir() / "settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    known = {f.name for f in fields(Settings)}
    defaults = asdict(Settings())
    merged = {k: raw.get(k, defaults[k]) for k in known}
    try:
        return Settings(**merged).normalized()
    except (TypeError, ValueError):
        return Settings()


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or config_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings.normalized()), indent=2), encoding="utf-8")
    os.replace(tmp, path)
