"""Live benchmark of the engine on a real monitor with a synthetic test pattern.

Shows a black full-screen test window on one monitor and flashes it white several times,
each flash after a calm phase. Records reaction latency, steadiness while white, time to
brighten again, CPU of the engine thread (calm and overall) and RAM.

Hard kill switch: the process exits after the planned duration + 5 s no matter what, so the
screen can never stay dark. Nothing is saved except numbers.

    python tools/bench_live.py --monitor 1 --flashes 3 --out result.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402

from dimmer import winapi  # noqa: E402
from dimmer.engine import Engine  # noqa: E402
from dimmer.settings import Settings  # noqa: E402

CALM_S = 8.0
WHITE_S = 2.5


def _thread_cpu(proc: psutil.Process, native_id: int | None) -> float:
    for th in proc.threads():
        if th.id == native_id:
            return float(th.user_time + th.system_time)
    return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor", type=int, default=0, help="index in list_monitors() (0 = primary)")
    ap.add_argument("--flashes", type=int, default=3)
    ap.add_argument("--interval", type=int, default=50)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    total = args.flashes * (CALM_S + WHITE_S) + CALM_S
    threading.Timer(total + 5, lambda: os._exit(3)).start()  # kill switch

    winapi.enable_dpi_awareness()
    mon = winapi.list_monitors()[args.monitor]
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{mon.width}x{mon.height}+{mon.left}+{mon.top}")
    root.configure(bg="black")
    root.attributes("-topmost", True)

    engine = Engine(Settings(monitors=[mon.device], interval_ms=args.interval, hotkey=False))
    engine.start()
    engine.wait_ready()
    proc = psutil.Process()
    trace: list[tuple[float, int]] = []
    events: list[tuple[float, str]] = []
    calm_cpu: list[float] = []
    calm_mark: list[float] = []
    t0 = time.perf_counter()
    cpu_start = _thread_cpu(proc, engine.native_id)

    def tick() -> None:
        t = time.perf_counter() - t0
        snap = engine.snapshot()
        trace.append((round(t, 3), snap.monitors[0].opacity if snap.monitors else -1))
        n, pos = divmod(t, CALM_S + WHITE_S)
        want_white = n < args.flashes and pos >= CALM_S
        # calm CPU: last 1.5 s of each black phase (the idle rate should be active by then)
        if CALM_S - 1.5 <= pos < CALM_S and not calm_mark:
            calm_mark[:] = [t, _thread_cpu(proc, engine.native_id)]
        if pos >= CALM_S and calm_mark:
            span = t - calm_mark[0]
            if span > 1.0:
                calm_cpu.append((_thread_cpu(proc, engine.native_id) - calm_mark[1]) / span * 100)
            calm_mark.clear()
        if want_white != (root["bg"] == "white"):
            root.configure(bg="white" if want_white else "black")
            root.update_idletasks()
            events.append((t, "white" if want_white else "black"))
        if t >= total:
            finish(t)
            return
        root.after(5, tick)

    def finish(t: float) -> None:
        eng_cpu = (_thread_cpu(proc, engine.native_id) - cpu_start) / t * 100
        lat: list[float] = []
        full: list[float] = []
        off: list[float] = []
        steady: set[int] = set()
        for et, kind in events:
            after = [(tt, o) for tt, o in trace if tt > et]
            if kind == "white":
                lat += [tt - et for tt, o in after if o > 0][:1]
                full += [tt - et for tt, o in after if o >= 216][:1]
                steady |= {o for tt, o in after if et + 1.0 < tt < et + WHITE_S - 0.05}
            else:
                off += [tt - et for tt, o in after if o == 0][:1]
        result = {
            "interval_ms": args.interval,
            "latency_first_ms": [round(x * 1000) for x in lat],
            "latency_90pct_ms": [round(x * 1000) for x in full],
            "brighten_to_zero_ms": [round(x * 1000) for x in off],
            "steady_values_while_white": sorted(steady),
            "cpu_engine_overall": round(eng_cpu, 1),
            "cpu_engine_calm": round(statistics.mean(calm_cpu), 1) if calm_cpu else None,
            "rss_mb": round(proc.memory_info().rss / 2**20, 1),
        }
        engine.stop()
        root.destroy()
        text = json.dumps(result)
        if args.out:
            args.out.write_text(text)
        print(text)

    root.after(5, tick)
    root.mainloop()
    os._exit(0)


if __name__ == "__main__":
    main()
