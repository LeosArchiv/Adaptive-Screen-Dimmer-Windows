"""Tk user interface. Runs on the main thread and talks to the engine only via its API."""

from __future__ import annotations

import functools
import logging
import queue
import tkinter as tk
from tkinter import ttk

from . import settings as settings_mod
from .engine import Engine, Status, wanted_devices
from .logic import ATTACK_PRESETS, RELEASE_PRESETS
from .settings import Settings
from .winapi import Monitor

log = logging.getLogger("dimmer")

BG = "#1f2126"
PANEL = "#2a2d34"
FG = "#e8e8ea"
MUTED = "#9aa0aa"
ACCENT = "#e0a030"
OK = "#5cc27a"
WARN = "#e0a030"
ERR = "#e05d5d"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_TITLE = ("Segoe UI Semibold", 13)
LOG_LINES = 300
POLL_MS = 100
SAVE_DELAY_MS = 600


class QueueLogHandler(logging.Handler):
    """Collects log records from any thread; the GUI drains the queue on its own thread."""

    def __init__(self) -> None:
        super().__init__()
        self.records: queue.Queue[str] = queue.Queue(maxsize=1000)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.put_nowait(self.format(record))
        except queue.Full:
            pass


class Meter(tk.Canvas):
    """Brightness bar with markers where dimming starts and reaches full strength."""

    W, H = 150, 12

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.create_rectangle(0, 0, self.W, self.H, fill="#15171b", outline="")
        self.bar = self.create_rectangle(0, 0, 0, self.H, fill="#c9c9c9", outline="")
        self.start = self.create_line(0, 0, 0, self.H, fill=ACCENT, width=2)
        self.full = self.create_line(0, 0, 0, self.H, fill=ERR, width=2)

    def show(self, level: float, start: int, full: int) -> None:
        x = lambda v: max(0.0, min(1.0, v / 255.0)) * self.W  # noqa: E731
        self.coords(self.bar, 0, 0, x(level), self.H)
        self.coords(self.start, x(start), 0, x(start), self.H)
        self.coords(self.full, x(full), 0, x(full), self.H)


class DimmerApp:
    def __init__(self, root: tk.Tk, engine: Engine, settings: Settings, log_handler: QueueLogHandler) -> None:
        self.root = root
        self.engine = engine
        self.settings = settings.normalized()
        self.log_handler = log_handler
        self._save_job: str | None = None
        self._monitor_rows: dict[str, tuple[tk.BooleanVar, Meter, tk.Label]] = {}
        self._monitor_key: list[Monitor] = []
        self._log_visible = False

        root.title("Adaptive Screen Dimmer")
        root.configure(bg=BG)
        root.minsize(470, 300)
        self._style()
        self._build()
        self._load_into_widgets()
        root.after(POLL_MS, self._poll)

    # ---- layout ----------------------------------------------------------------------
    def _style(self) -> None:
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, font=FONT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TLabelframe", background=BG, bordercolor="#3a3e47", relief="solid")
        st.configure("Card.TLabelframe.Label", background=BG, foreground=MUTED, font=FONT_SMALL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Muted.TLabel", foreground=MUTED, font=FONT_SMALL)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("Horizontal.TScale", background=BG, troughcolor="#15171b")
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure("TButton", background=PANEL, foreground=FG, bordercolor="#3a3e47", padding=(10, 4))
        st.map("TButton", background=[("active", "#353943")])
        st.configure("Big.TButton", font=("Segoe UI Semibold", 11), padding=(12, 8))

    def _card(self, parent: tk.Misc, title: str) -> ttk.Labelframe:
        card = ttk.Labelframe(parent, text=f" {title} ", style="Card.TLabelframe", padding=(10, 6))
        card.pack(fill=tk.X, padx=12, pady=(0, 10))
        return card

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 12, 12, 8))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Adaptive Screen Dimmer", font=FONT_TITLE).pack(side=tk.LEFT)
        self.state_label = tk.Label(top, text="…", bg=BG, fg=MUTED, font=FONT_SMALL)
        self.state_label.pack(side=tk.RIGHT)

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        bar.pack(fill=tk.X)
        self.pause_btn = ttk.Button(bar, style="Big.TButton", command=self.engine.toggle_paused)
        self.pause_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.hotkey_hint = ttk.Label(bar, text="Strg+Alt+D", style="Muted.TLabel")
        self.hotkey_hint.pack(side=tk.LEFT, padx=(10, 0))

        mon = self._card(self.root, "Bildschirme")
        self.monitor_frame = ttk.Frame(mon)
        self.monitor_frame.pack(fill=tk.X)
        links = ttk.Frame(mon)
        links.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(links, text="Bildschirme kennzeichnen", command=self._identify).pack(side=tk.LEFT)

        dim = self._card(self.root, "Abdunkelung")
        dim.columnconfigure(1, weight=1)
        self.start_var = tk.IntVar()
        self.full_var = tk.IntVar()
        self.max_var = tk.IntVar()
        self._slider(dim, 0, "Beginnt ab Helligkeit", self.start_var, 0, 250, lambda v: f"{v}")
        self._slider(dim, 1, "Volle Stärke ab Helligkeit", self.full_var, 1, 255, lambda v: f"{v}")
        self._slider(dim, 2, "Stärkste Abdunkelung", self.max_var, 0, 94, lambda v: f"{v} %")
        self.attack_var = tk.StringVar()
        self.release_var = tk.StringVar()
        self._combo(dim, 3, "Abdunkeln bei Helligkeit", self.attack_var, list(ATTACK_PRESETS))
        self._combo(dim, 4, "Wieder aufhellen", self.release_var, list(RELEASE_PRESETS))
        row = ttk.Frame(dim)
        row.grid(row=5, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(
            row, text="Balken: aktuelle Helligkeit · gelb: Beginn · rot: volle Stärke", style="Muted.TLabel"
        ).pack(side=tk.LEFT)
        ttk.Button(row, text="Standardwerte", command=self._reset_defaults).pack(side=tk.RIGHT)

        exc = self._card(self.root, "Ausnahmen – hier nie abdunkeln")
        self.exc_list = tk.Listbox(
            exc,
            height=3,
            bg=PANEL,
            fg=FG,
            selectbackground="#3d5a80",
            highlightthickness=0,
            borderwidth=0,
            font=FONT_SMALL,
            activestyle="none",
        )
        self.exc_list.pack(fill=tk.X)
        row = ttk.Frame(exc)
        row.pack(fill=tk.X, pady=(6, 0))
        self.add_app_btn = ttk.Button(row, text="Zuletzt genutztes Programm hinzufügen", command=self._add_last_app)
        self.add_app_btn.pack(side=tk.LEFT)
        ttk.Button(row, text="Entfernen", command=self._remove_app).pack(side=tk.RIGHT)

        opts = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        opts.pack(fill=tk.X)
        self.hotkey_var = tk.BooleanVar()
        self.start_paused_var = tk.BooleanVar()
        ttk.Checkbutton(
            opts, text="Tastenkürzel Strg+Alt+D (Pause/Weiter)", variable=self.hotkey_var, command=self._changed
        ).pack(anchor="w")
        ttk.Checkbutton(
            opts, text="Beim Programmstart pausiert", variable=self.start_paused_var, command=self._changed
        ).pack(anchor="w")

        foot = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        foot.pack(fill=tk.X)
        self.log_btn = ttk.Button(foot, text="Protokoll anzeigen", command=self._toggle_log)
        self.log_btn.pack(side=tk.LEFT)
        self.log_frame = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        self.log_text = tk.Text(
            self.log_frame,
            height=8,
            bg="#15171b",
            fg=MUTED,
            font=("Consolas", 9),
            borderwidth=0,
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _slider(self, parent: ttk.Labelframe, row: int, text: str, var: tk.IntVar, lo: int, hi: int, fmt) -> None:
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2)
        value = ttk.Label(parent, width=6, anchor="e")
        value.grid(row=row, column=2, sticky="e")

        def moved(raw: str) -> None:
            v = int(round(float(raw)))
            if v != var.get():
                var.set(v)
                self._changed()

        def var_written(*_: object) -> None:
            value.config(text=fmt(var.get()))
            if abs(float(scale.get()) - var.get()) >= 0.5:
                scale.set(var.get())

        scale = ttk.Scale(parent, from_=lo, to=hi, orient=tk.HORIZONTAL, command=moved)
        scale.grid(row=row, column=1, sticky="we", padx=8)
        var.trace_add("write", var_written)

    def _combo(self, parent: ttk.Labelframe, row: int, text: str, var: tk.StringVar, values: list[str]) -> None:
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2)
        box = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=10)
        box.grid(row=row, column=1, sticky="w", padx=8)
        box.bind("<<ComboboxSelected>>", lambda _e: self._changed())

    # ---- settings <-> widgets --------------------------------------------------------
    def _load_into_widgets(self) -> None:
        s = self.settings
        self._loading = True
        self.start_var.set(s.start)
        self.full_var.set(s.full)
        self.max_var.set(round(s.max_opacity / 255 * 100))
        self.attack_var.set(s.attack)
        self.release_var.set(s.release)
        self.hotkey_var.set(s.hotkey)
        self.start_paused_var.set(s.start_paused)
        self.exc_list.delete(0, tk.END)
        for app in s.excluded_apps:
            self.exc_list.insert(tk.END, app)
        self._loading = False

    def _changed(self) -> None:
        if getattr(self, "_loading", False):
            return
        s = self.settings
        start = self.start_var.get()
        full = self.full_var.get()
        if full <= start:  # keep the pair consistent while dragging either slider
            full = min(255, start + 1)
            self.full_var.set(full)
        new = Settings(
            start=start,
            full=full,
            max_opacity=round(self.max_var.get() / 100 * 255),
            interval_ms=s.interval_ms,
            attack=self.attack_var.get(),
            release=self.release_var.get(),
            monitors=list(s.monitors),
            excluded_apps=list(self.exc_list.get(0, tk.END)),
            hotkey=self.hotkey_var.get(),
            start_paused=self.start_paused_var.get(),
        ).normalized()
        self._apply(new)

    def _apply(self, new: Settings) -> None:
        if new == self.settings:
            return
        self.settings = new
        self.engine.update_settings(new)
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self._save)

    def _save(self) -> None:
        self._save_job = None
        try:
            settings_mod.save(self.settings)
        except OSError as e:
            log.warning("Einstellungen konnten nicht gespeichert werden: %s", e)

    def _reset_defaults(self) -> None:
        """Reset the dimming values; monitors, exceptions and options stay as they are."""
        k = self.settings
        self._apply(
            Settings(monitors=k.monitors, excluded_apps=k.excluded_apps, hotkey=k.hotkey, start_paused=k.start_paused)
        )
        self._load_into_widgets()

    # ---- monitors --------------------------------------------------------------------
    def _rebuild_monitors(self, monitors: list[Monitor], active: set[str]) -> None:
        for child in self.monitor_frame.winfo_children():
            child.destroy()
        self._monitor_rows.clear()
        for i, m in enumerate(monitors):
            row = ttk.Frame(self.monitor_frame)
            row.pack(fill=tk.X, pady=1)
            var = tk.BooleanVar(value=m.device in active)
            text = f"Bildschirm {i + 1}  {m.width}×{m.height}" + ("  (Haupt)" if m.primary else "")
            toggle = functools.partial(self._toggle_monitor, m.device)
            ttk.Checkbutton(row, text=text, variable=var, command=toggle).pack(side=tk.LEFT)
            pct = tk.Label(row, text="", bg=BG, fg=MUTED, font=FONT_SMALL, width=5, anchor="e")
            pct.pack(side=tk.RIGHT)
            meter = Meter(row)
            meter.pack(side=tk.RIGHT, padx=(8, 4))
            self._monitor_rows[m.device] = (var, meter, pct)

    def _toggle_monitor(self, device: str) -> None:
        chosen = [d for d, (var, _m, _p) in self._monitor_rows.items() if var.get()]
        if not chosen:
            self._monitor_rows[device][0].set(True)  # at least one monitor stays active
            return
        self._apply(Settings(**{**vars(self.settings), "monitors": chosen}).normalized())

    def _identify(self) -> None:
        for i, m in enumerate(self._monitor_key):
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=ACCENT)
            size = 160
            win.geometry(f"{size}x{size}+{m.left + 40}+{m.top + 40}")
            tk.Label(win, text=str(i + 1), bg=ACCENT, fg="#111", font=("Segoe UI Semibold", 72)).pack(expand=True)
            win.after(1800, win.destroy)

    # ---- exclusions -------------------------------------------------------------------
    def _add_last_app(self) -> None:
        app = self._last_app
        if app and app not in self.exc_list.get(0, tk.END):
            self.exc_list.insert(tk.END, app)
            self._changed()

    def _remove_app(self) -> None:
        for index in reversed(self.exc_list.curselection()):
            self.exc_list.delete(index)
        self._changed()

    # ---- log --------------------------------------------------------------------------
    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_frame.pack(fill=tk.BOTH, expand=True)
            self.log_btn.config(text="Protokoll ausblenden")
        else:
            self.log_frame.pack_forget()
            self.log_btn.config(text="Protokoll anzeigen")

    def _drain_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.log_handler.records.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "\n".join(lines) + "\n")
        extra = int(self.log_text.index("end-1c").split(".")[0]) - LOG_LINES
        if extra > 0:
            self.log_text.delete("1.0", f"{extra + 1}.0")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ---- polling ------------------------------------------------------------------------
    _last_app: str | None = None

    def _poll(self) -> None:
        try:
            self._refresh(self.engine.snapshot())
            self._drain_log()
        finally:
            self.root.after(POLL_MS, self._poll)

    def _refresh(self, st: Status) -> None:
        monitors = self.engine.monitors()
        # Checkboxes follow the settings (what the user chose), not the engine's asynchronous state.
        wanted = set(wanted_devices(self.settings.monitors, monitors))
        if monitors != self._monitor_key:
            self._monitor_key = monitors
            self._rebuild_monitors(monitors, wanted)
        for device, (var, _meter, _pct) in self._monitor_rows.items():
            if var.get() != (device in wanted):
                var.set(device in wanted)
        rows = {m.device: m for m in st.monitors}
        for device, (_var, meter, pct) in self._monitor_rows.items():
            m = rows.get(device)
            meter.show(m.brightness if m else 0.0, self.settings.start, self.settings.full)
            pct.config(text=f"{round(m.opacity / 255 * 100)} %" if m else "–")

        if st.error:
            text, color = f"Fehler: {st.error}", ERR
        elif not st.running:
            text, color = "Gestoppt", ERR
        elif st.paused_reason == "user":
            text, color = "Pausiert", WARN
        elif st.paused_reason.startswith("app:"):
            text, color = f"Ausnahme: {st.paused_reason[4:]}", WARN
        else:
            text, color = "Aktiv", OK
        self.state_label.config(text=f"● {text}", fg=color)
        self.pause_btn.config(text="▶  Fortsetzen" if st.paused_reason == "user" else "⏸  Pausieren")
        self.hotkey_hint.config(text="Strg+Alt+D" if st.hotkey_ok else "")

        self._last_app = st.last_foreign_app
        self.add_app_btn.config(
            text=f"„{st.last_foreign_app}“ hinzufügen"
            if st.last_foreign_app
            else "Zuletzt genutztes Programm hinzufügen",
            state=tk.NORMAL if st.last_foreign_app else tk.DISABLED,
        )
