import json
from pathlib import Path

from dimmer.engine import wanted_devices
from dimmer.profiles import OFF_PROFILE, Profile, Rule, Schedule
from dimmer.settings import Settings, load, save
from dimmer.winapi import Monitor


def names(s: Settings) -> list[str]:
    return [p.name for p in s.profiles]


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    s = load(tmp_path / "nope.json")
    assert names(s) == ["Tag", "Nacht", "Arbeit", "Zocken", "Filme", OFF_PROFILE]
    assert {r.exe: r.profile for r in s.rules} == {"ddnet.exe": "Zocken", "vlc.exe": "Filme"}


def test_corrupt_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert load(p) == Settings().normalized()
    p.write_text("[1, 2]", encoding="utf-8")
    assert load(p) == Settings().normalized()


def test_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    s = Settings(
        profiles=[Profile("Mein Abend", start=40, tint_mode="own", tint_kelvin=2800, tint_strength=35)],
        rules=[Rule("Game.EXE ", "Mein Abend")],
        schedule=Schedule(day_profile="Mein Abend", night_profile="Mein Abend", night_start="21:15"),
        monitors=["mon-a"],
        monitor_profiles={"mon-a": "Mein Abend", "mon-b": "gibt es nicht"},
        hotkey=False,
    )
    save(s, p)
    loaded = load(p)
    assert names(loaded) == ["Mein Abend", OFF_PROFILE]
    prof = loaded.profile_map()["Mein Abend"]
    assert prof.start == 40 and prof.tint_kelvin == 2800 and prof.tint_strength == 35
    assert loaded.rule_map() == {"game.exe": "Mein Abend"}
    assert loaded.schedule.night_start == "21:15"
    assert loaded.monitor_profiles == {"mon-a": "Mein Abend"}  # unknown profile dropped -> auto
    assert loaded.hotkey is False


def test_migration_from_version_1(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "start": 33,
                "full": 120,
                "max_opacity": 200,
                "attack": "Sanft",
                "release": "Langsam",
                "interval_ms": 100,
                "monitors": ["mon-a"],
                "excluded_apps": ["photoshop.exe"],
                "hotkey": False,
                "start_paused": False,
            }
        ),
        encoding="utf-8",
    )
    s = load(p)
    day = s.profile_map()["Tag"]
    assert (day.start, day.full, day.max_opacity, day.attack, day.release) == (33, 120, 200, "Sanft", "Langsam")
    assert s.rule_map()["photoshop.exe"] == OFF_PROFILE
    assert s.interval_ms == 100 and s.monitors == ["mon-a"] and s.hotkey is False


def test_rules_to_deleted_profiles_are_dropped() -> None:
    s = Settings(rules=[Rule("a.exe", "Weg"), Rule("b.exe", "Tag")]).normalized()
    assert s.rule_map() == {"b.exe": "Tag"}


def test_off_profile_always_present_and_names_unique() -> None:
    s = Settings(profiles=[Profile("A"), Profile("A", start=99)]).normalized()
    assert names(s) == ["A", OFF_PROFILE]
    assert s.profiles[0].start == 25  # first one wins
    assert s.schedule.day_profile == "A"  # schedule repaired to an existing profile


def test_only_off_profile_restores_defaults() -> None:
    s = Settings(profiles=[Profile(OFF_PROFILE)]).normalized()
    assert "Tag" in names(s)


def test_hostile_values_do_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        '{"profiles": [{"name": "X", "start": Infinity, "full": NaN, "attack": ["x"]}, 5, null],'
        ' "rules": "nope", "schedule": {"night_start": "99:99", "fade_minutes": -3},'
        ' "hotkey": "false", "close_to_tray": 0, "interval_ms": 1e99}',
        encoding="utf-8",
    )
    s = load(p)
    x = s.profile_map()["X"]
    assert x.start == 0 and x.full == 1 and x.attack == "Schnell"
    assert s.schedule.night_start == "20:00" and s.schedule.fade_minutes == 0
    assert s.hotkey is True and s.close_to_tray is True
    assert s.interval_ms == 500  # clamped to the slowest rate


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


def test_list_values_for_names_do_not_reset_everything(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        '{"profiles": [{"name": "Mein", "start": 40}], "rules": [{"exe": "a.exe", "profile": ["x"]}],'
        ' "schedule": {"day_profile": {"x": 1}, "night_profile": ["y"]}}',
        encoding="utf-8",
    )
    s = load(p)
    assert s.profile_map()["Mein"].start == 40  # user's profile survived
    assert s.rules == [] and s.schedule.day_profile == "Mein"


def test_unreadable_file_is_kept_as_bad(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text("{broken", encoding="utf-8")
    load(p)
    assert (tmp_path / "settings.bad").read_text(encoding="utf-8") == "{broken"
