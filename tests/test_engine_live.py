"""Engine lifecycle on the real desktop. Opt-in (ASD_LIVE_TESTS=1): creates real overlay windows,
but with max_opacity=0 so nothing is ever dimmed, and every test has a hard timeout."""

import os
import time

import pytest

from dimmer.engine import Engine
from dimmer.settings import Settings
from dimmer.winapi import enable_dpi_awareness

pytestmark = pytest.mark.skipif(os.environ.get("ASD_LIVE_TESTS") != "1", reason="set ASD_LIVE_TESTS=1")


@pytest.fixture
def engine():
    enable_dpi_awareness()
    e = Engine(Settings(max_opacity=0, hotkey=False))
    e.start()
    assert e.wait_ready(5)
    yield e
    e.stop()


def test_start_measure_pause_stop(engine: Engine) -> None:
    time.sleep(0.4)
    st = engine.snapshot()
    assert st.running and not st.error
    assert len(st.monitors) == 1
    assert st.monitors[0].opacity == 0
    assert st.monitors[0].excluded_from_capture

    engine.set_paused(True)
    time.sleep(0.2)
    assert engine.snapshot().paused_reason == "user"
    engine.toggle_paused()
    time.sleep(0.2)
    assert engine.snapshot().paused_reason == ""

    engine.stop()
    assert not engine.is_alive()
    assert engine.snapshot().monitors == []


def test_switch_monitors_at_runtime(engine: Engine) -> None:
    monitors = engine.monitors()
    all_ids = [m.device for m in monitors]
    engine.update_settings(Settings(max_opacity=0, hotkey=False, monitors=all_ids))
    time.sleep(0.4)
    assert {m.device for m in engine.snapshot().monitors} == set(all_ids)
    engine.update_settings(Settings(max_opacity=0, hotkey=False, monitors=all_ids[-1:]))
    time.sleep(0.4)
    assert [m.device for m in engine.snapshot().monitors] == all_ids[-1:]


def test_transient_error_does_not_kill_engine(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import dimmer.engine as engine_mod

    real = engine_mod.list_monitors
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("monitor vanished during enumeration")
        return real()

    monkeypatch.setattr(engine_mod, "list_monitors", flaky)
    engine._monitors_dirty = True  # force a refresh on the next round
    engine._wake()
    time.sleep(1.0)
    st = engine.snapshot()
    assert calls["n"] >= 2
    assert engine.is_alive() and st.running
    assert st.error is None  # cleared after the next good round
    assert len(st.monitors) == 1


def test_persistent_errors_keep_engine_controllable(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import dimmer.engine as engine_mod

    def broken():
        raise OSError("display driver hiccup")

    monkeypatch.setattr(engine_mod, "list_monitors", broken)
    engine._monitors_dirty = True
    engine._wake()
    time.sleep(1.5)
    st = engine.snapshot()
    assert engine.is_alive() and st.running
    assert st.error and "display driver hiccup" in st.error
    assert all(m.opacity == 0 for m in st.monitors)  # fail safe: never frozen dark
    engine.set_paused(True)
    time.sleep(2.5)  # longest backoff is 2 s
    assert engine.snapshot().paused_reason == "user"
