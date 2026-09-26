"""Engine helpers that need no windows."""

from dimmer.engine import APP_CONFIRM, Engine


class FakeSlot:
    def __init__(self) -> None:
        self.app = None
        self.app_known = False
        self.pending_app = None
        self.pending_hits = 0


def test_first_sighting_is_taken_at_once() -> None:
    slot = FakeSlot()
    assert Engine._confirmed_app(slot, "ddnet.exe") == "ddnet.exe"  # type: ignore[arg-type]


def test_short_lived_window_does_not_switch() -> None:
    slot = FakeSlot()
    Engine._confirmed_app(slot, "ddnet.exe")  # type: ignore[arg-type]
    seen = ["startmenu.exe", "ddnet.exe", "ddnet.exe"]  # Start menu open for one check only
    results = [Engine._confirmed_app(slot, s) for s in seen]  # type: ignore[arg-type]
    assert results == ["ddnet.exe"] * 3


def test_real_switch_needs_consecutive_sightings() -> None:
    slot = FakeSlot()
    Engine._confirmed_app(slot, "ddnet.exe")  # type: ignore[arg-type]
    results = [Engine._confirmed_app(slot, "vlc.exe") for _ in range(APP_CONFIRM + 1)]  # type: ignore[arg-type]
    assert results[0] == "ddnet.exe"
    assert results[-1] == "vlc.exe"
