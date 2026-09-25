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
