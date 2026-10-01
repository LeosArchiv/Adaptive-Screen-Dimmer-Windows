import json
from pathlib import Path

from dimmer.engine import wanted_devices
from dimmer.profiles import Profile
from dimmer.settings import VERSION, Settings, load, save
from dimmer.winapi import Monitor


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    s = load(tmp_path / "nope.json")
    assert s == Settings().normalized()
    assert s.profile == Profile() and s.version == VERSION == 3


def test_corrupt_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert load(p) == Settings().normalized()
    p.write_text("[1, 2]", encoding="utf-8")
    assert load(p) == Settings().normalized()


def test_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    s = Settings(
        profile=Profile(start=40, tint_on=True, tint_kelvin=2800, tint_strength=35, night_start="21:15", glare=3),
        monitors=["mon-a"],
        hotkey=False,
        start_minimized=True,
    )
    save(s, p)
    loaded = load(p)
    assert loaded == s.normalized()
    assert loaded.profile.night_start == "21:15" and loaded.profile.tint_on is True
    assert json.loads(p.read_text(encoding="utf-8"))["version"] == 3


def test_migration_from_version_2_keeps_only_general_options(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "profiles": [{"name": "Tag", "start": 99, "max_opacity": 10}],
                "rules": [{"exe": "a.exe", "profile": "Tag", "title": ""}],
                "schedule": {"enabled": True, "night_start": "22:00"},
                "monitor_profiles": {"mon-a": "Tag"},
                "monitors": ["mon-a", "mon-b"],
                "interval_ms": 80,
                "hotkey": False,
                "start_paused": True,
                "close_to_tray": False,
                "start_minimized": True,
                "version": 2,
            }
        ),
        encoding="utf-8",
    )
    s = load(p)
    assert s.profile == Profile()  # old profiles are dropped on purpose
    assert s.monitors == ["mon-a", "mon-b"] and s.interval_ms == 80
    assert (s.hotkey, s.start_paused, s.close_to_tray, s.start_minimized) == (False, True, False, True)
    assert s.version == 3
    save(s, p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "profiles" not in data and "rules" not in data and "schedule" not in data


def test_migration_from_version_1(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps({"start": 33, "excluded_apps": ["photoshop.exe"], "interval_ms": 100, "monitors": ["mon-a"]}),
        encoding="utf-8",
    )
    s = load(p)
    assert s.profile == Profile()
    assert s.interval_ms == 100 and s.monitors == ["mon-a"]


def test_hostile_values_do_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        '{"version": 3, "profile": {"start": Infinity, "full": NaN, "attack": ["x"], "night_start": "99:99",'
        ' "unknown": 1}, "monitors": "nope", "hotkey": "false", "close_to_tray": 0, "interval_ms": 1e99}',
        encoding="utf-8",
    )
    s = load(p)
    assert s.profile.start == 0 and s.profile.full == 1 and s.profile.attack == "Schnell"
    assert s.profile.night_start == "20:00"
    assert s.monitors == [] and s.hotkey is True and s.close_to_tray is True
    assert s.interval_ms == 500  # clamped to the slowest rate


def test_unreadable_file_is_kept_as_bad(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{broken", encoding="utf-8")
    load(p)
    assert (tmp_path / "settings.bad").read_text(encoding="utf-8") == "{broken"


def test_wrong_profile_type_is_kept_as_bad(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text('{"version": 3, "profile": [1]}', encoding="utf-8")
    assert load(p) == Settings().normalized()
    assert (tmp_path / "settings.bad").exists()


def _mon(dev: str, primary: bool = False) -> Monitor:
    return Monitor(device=dev, left=0, top=0, width=10, height=10, primary=primary)


def test_wanted_devices_defaults_to_primary() -> None:
    mons = [_mon("a"), _mon("b", primary=True)]
    assert wanted_devices([], mons) == ["b"]


def test_wanted_devices_keeps_connected_choice() -> None:
    mons = [_mon("a", True), _mon("b"), _mon("c")]
    assert wanted_devices(["c", "b"], mons) == ["c", "b"]


def test_wanted_devices_falls_back_when_choice_unplugged() -> None:
    mons = [_mon("a", True)]
    assert wanted_devices(["gone"], mons) == ["a"]


def test_language_is_kept_and_validated() -> None:
    from dimmer.settings import Settings, from_dict

    assert Settings().language == "auto"
    assert from_dict({"version": 3, "profile": {}, "language": "en"}).language == "en"
    assert from_dict({"version": 3, "profile": {}, "language": "fr"}).language == "auto"
    assert from_dict({"version": 2, "language": "de"}).language == "de"
