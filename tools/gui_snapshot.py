"""Start the real GUI briefly and save a PNG of the app window only (never the rest of the screen).

Uses a throw-away config directory and quits after a few seconds (kill switch included).

    python tools/gui_snapshot.py out.png [--log] [--paused]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--log", action="store_true", help="expand the log panel")
    ap.add_argument("--paused", action="store_true")
    ap.add_argument("--delay", type=float, default=3.0)
    args = ap.parse_args()
    threading.Timer(args.delay + 8, lambda: os._exit(3)).start()  # kill switch
    os.environ["ASD_CONFIG_DIR"] = tempfile.mkdtemp(prefix="asd-snap-")

    from dimmer import app, winapi
    from dimmer import settings as settings_mod
    from dimmer.engine import Engine
    from dimmer.gui import DimmerApp

    winapi.enable_dpi_awareness()
    handler = app._setup_logging(False)
    s = settings_mod.load()
    s.start_paused = args.paused
    s.excluded_apps = ["photoshop.exe"]
    engine = Engine(s)
    engine.start()
    root = tk.Tk()
    root.attributes("-topmost", True)
    ui = DimmerApp(root, engine, s, handler)  # type: ignore[arg-type]
    if args.log:
        ui._toggle_log()

    def shoot() -> None:
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        sampler = winapi.Sampler()
        px = sampler.grab(x, y, w, h).copy()
        sampler.close()
        img = tk.PhotoImage(width=w, height=h)
        rows = ["{" + " ".join(f"#{r:02x}{g:02x}{b:02x}" for b, g, r, _a in line) + "}" for line in px.tolist()]
        img.put(" ".join(rows))
        img.write(str(args.out), format="png")
        print(f"saved {args.out} ({w}x{h})")
        engine.stop()
        root.destroy()

    root.after(int(args.delay * 1000), shoot)
    root.mainloop()
    os._exit(0)


if __name__ == "__main__":
    main()
