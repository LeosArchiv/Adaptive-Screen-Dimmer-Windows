import json
from pathlib import Path

from dimmer.engine import wanted_devices
from dimmer.settings import Settings, load, save
from dimmer.winapi import Monitor


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.json") == Settings()


def test_corrupt_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    assert load(p) == Settings()
    p.write_text("[1, 2]", encoding="utf-8")
    assert load(p) == Settings()


def test_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    s = Settings(
        start=40,
        full=120,
        max_opacity=200,
        attack="Sanft",
        release="Langsam",
        monitors=["mon-a"],
        excluded_apps=["Game.EXE "],
        hotkey=False,
        start_paused=True,
    )
    save(s, p)
    loaded = load(p)
    assert loaded.start == 40 and loaded.full == 120 and loaded.max_opacity == 200
    assert loaded.attack == "Sanft" and loaded.release == "Langsam"
    assert loaded.monitors == ["mon-a"]
    assert loaded.excluded_apps == ["game.exe"]
    assert loaded.hotkey is False and loaded.start_paused is True


def test_values_are_clamped_and_unknown_keys_ignored(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "start": 300,
                "full": -5,
                "max_opacity": 255,
                "interval_ms": 1,
                "attack": "Blitz",
                "monitors": "not-a-list",
                "excluded_apps": None,
                "future_option": 1,
            }
        ),
        encoding="utf-8",
    )
    s = load(p)
    assert s.start == 250
    assert s.full == 251
    assert s.max_opacity == 240  # never fully black
    assert s.interval_ms == 16
    assert s.attack == Settings().attack
    assert s.monitors == [] and s.excluded_apps == []


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


def test_hostile_values_do_not_crash(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(
        '{"start": Infinity, "full": NaN, "attack": ["x"], "hotkey": "false", "close_to_tray": 0}',
        encoding="utf-8",
    )
    s = load(p)
    assert s.start == 0 and s.full == 1
    assert s.attack == Settings().attack
    assert s.hotkey is True  # string "false" is not trusted, default kept
    assert s.close_to_tray is True
