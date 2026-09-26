"""Tk user interface. Runs on the main thread and talks to the engine only via its API."""

from __future__ import annotations

import dataclasses
import functools
import logging
import queue
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk

from . import settings as settings_mod
from .engine import Engine, Status, wanted_devices
from .logic import ATTACK_PRESETS, RELEASE_PRESETS
from .profiles import (
    AUTO,
    INHERIT,
    KELVIN_MAX,
    KELVIN_MIN,
    OFF,
    OWN,
    TINT_MAX,
    Profile,
    Rule,
    kelvin_to_rgb,
    parse_hhmm,
    schedule_phase,
)
from .settings import Settings
from .tray import TrayIcon
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
STALL_S = 3.0
# Measurements per second while something moves; still screens are measured at half the rate.
RATE_PRESETS = {"Sparsam (10/s)": 100, "Normal (20/s)": 50, "Schnell (30/s)": 33}
AUTO_LABEL = "Automatisch (Tag/Nacht)"
MODE_LABELS = {OWN: "eigene Werte", INHERIT: "vom Grundprofil übernehmen", OFF: "aus"}


def rate_name(interval_ms: int) -> str:
    return min(RATE_PRESETS, key=lambda name: abs(RATE_PRESETS[name] - interval_ms))


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

    def show(self, level: float, start: float | None, full: float | None) -> None:
        def x(v: float) -> float:
            return max(0.0, min(1.0, v / 255.0)) * self.W

        self.coords(self.bar, 0, 0, x(level), self.H)
        for item, value in ((self.start, start), (self.full, full)):
            if value is None:
                self.coords(item, -5, 0, -5, self.H)
            else:
                self.coords(item, x(value), 0, x(value), self.H)


class _MonitorRow:
    def __init__(self, var: tk.BooleanVar, base: ttk.Combobox, meter: Meter, info: ttk.Label) -> None:
        self.var, self.base, self.meter, self.info = var, base, meter, info


class DimmerApp:
    def __init__(
        self,
        root: tk.Tk,
        engine: Engine,
        settings: Settings,
        log_handler: QueueLogHandler,
        tray: TrayIcon | None = None,
    ) -> None:
        self.root = root
        self.tray = tray
        self.engine = engine
        self.settings = settings.normalized()
        self.log_handler = log_handler
        self._save_job: str | None = None
        self._monitor_rows: dict[str, _MonitorRow] = {}
        self._monitor_key: list[Monitor] = []
        self._loading = False
        self._editing = self.settings.profiles[0].name
        self._recent_apps: list[str] = []

        root.title("Adaptive Screen Dimmer")
        root.configure(bg=BG)
        root.minsize(520, 560)
        self._style()
        self._build()
        self._load_all()
        root.after(POLL_MS, self._poll)

    # ---- style / helpers ---------------------------------------------------------------
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
        st.configure("TRadiobutton", background=BG, foreground=FG)
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("Horizontal.TScale", background=BG, troughcolor="#15171b")
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure("TButton", background=PANEL, foreground=FG, bordercolor="#3a3e47", padding=(10, 4))
        st.map("TButton", background=[("active", "#353943")])
        st.configure("Big.TButton", font=("Segoe UI Semibold", 11), padding=(12, 8))
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(12, 5))
        st.map("TNotebook.Tab", background=[("selected", BG)], foreground=[("selected", FG)])
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, insertcolor=FG)
        st.configure("TSpinbox", fieldbackground=PANEL, foreground=FG, arrowcolor=FG)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, borderwidth=0)
        st.configure("Treeview.Heading", background=BG, foreground=MUTED)

    def _card(self, parent: tk.Misc, title: str) -> ttk.Labelframe:
        card = ttk.Labelframe(parent, text=f" {title} ", style="Card.TLabelframe", padding=(10, 6))
        card.pack(fill=tk.X, padx=12, pady=(0, 10))
        return card

    def _slider(
        self,
        parent: tk.Misc,
        row: int,
        text: str,
        var: tk.IntVar,
        lo: int,
        hi: int,
        fmt: Callable[[int], str],
        on_change: Callable[[], None],
    ) -> ttk.Scale:
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2)
        value = ttk.Label(parent, width=7, anchor="e")
        value.grid(row=row, column=2, sticky="e")

        def moved(raw: str) -> None:
            v = int(round(float(raw)))
            if v != var.get():
                var.set(v)
                on_change()

        def var_written(*_: object) -> None:
            value.config(text=fmt(var.get()))
            if abs(float(scale.get()) - var.get()) >= 0.5:
                scale.set(var.get())

        scale = ttk.Scale(parent, from_=lo, to=hi, orient=tk.HORIZONTAL, command=moved)
        scale.grid(row=row, column=1, sticky="we", padx=8)
        var.trace_add("write", var_written)
        return scale

    def _combo(self, parent: tk.Misc, var: tk.StringVar, values: list[str], width: int, cmd: Callable[[], None]):
        box = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width)
        box.bind("<<ComboboxSelected>>", lambda _e: cmd())
        return box

    def _profile_names(self, with_off: bool = True) -> list[str]:
        return [p.name for p in self.settings.profiles if with_off or not p.builtin]

    # ---- layout ----------------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 12, 12, 8))
        top.pack(fill=tk.X)
        ttk.Label(top, text="Adaptive Screen Dimmer", font=FONT_TITLE).pack(side=tk.LEFT)
        self.state_label = tk.Label(top, text="…", bg=BG, fg=MUTED, font=FONT_SMALL)
        self.state_label.pack(side=tk.RIGHT)

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        bar.pack(fill=tk.X)
        self.pause_btn = ttk.Button(bar, style="Big.TButton", command=self.engine.toggle_paused)
        self.pause_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.hotkey_hint = ttk.Label(bar, text="Strg+Alt+D", style="Muted.TLabel")
        self.hotkey_hint.pack(side=tk.LEFT, padx=(10, 0))

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.notebook = nb
        for title, builder in (
            ("Übersicht", self._build_overview),
            ("Profile", self._build_profiles),
            ("Programme", self._build_rules),
            ("Zeitplan", self._build_schedule),
            ("Optionen", self._build_options),
        ):
            page = ttk.Frame(nb, padding=(0, 10, 0, 0))
            nb.add(page, text=title)
            builder(page)

    def _build_overview(self, page: ttk.Frame) -> None:
        card = self._card(page, "Bildschirme")
        self.monitor_frame = ttk.Frame(card)
        self.monitor_frame.pack(fill=tk.X)
        row = ttk.Frame(card)
        row.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row, text="Bildschirme kennzeichnen", command=self._identify).pack(side=tk.LEFT)
        ttk.Label(
            page,
            text=(
                "Grundprofil: gilt auf dem Bildschirm, solange dort kein Programm mit Regel vorne liegt.\n"
                "„Automatisch“ wechselt nach dem Zeitplan zwischen Tag und Nacht.\n"
                "Balken: aktuelle Helligkeit · gelb: Beginn · rot: volle Stärke"
            ),
            style="Muted.TLabel",
            justify=tk.LEFT,
        ).pack(anchor="w", padx=14)

    def _build_profiles(self, page: ttk.Frame) -> None:
        body = ttk.Frame(page, padding=(12, 0, 12, 0))
        body.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        self.profile_list = tk.Listbox(
            left,
            width=16,
            height=10,
            bg=PANEL,
            fg=FG,
            selectbackground="#3d5a80",
            highlightthickness=0,
            borderwidth=0,
            activestyle="none",
            exportselection=False,
        )
        self.profile_list.pack(fill=tk.Y, expand=True)
        self.profile_list.bind("<<ListboxSelect>>", lambda _e: self._select_profile())
        for text, cmd in (
            ("Neu", self._new_profile),
            ("Kopieren", self._copy_profile),
            ("Umbenennen", self._rename_profile),
            ("Löschen", self._delete_profile),
        ):
            ttk.Button(left, text=text, command=cmd).pack(fill=tk.X, pady=(4, 0))

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.p_title = ttk.Label(right, font=("Segoe UI Semibold", 11))
        self.p_title.pack(anchor="w", pady=(0, 6))

        dim = ttk.Labelframe(right, text=" Abdunkelung ", style="Card.TLabelframe", padding=(10, 6))
        dim.pack(fill=tk.X, pady=(0, 8))
        dim.columnconfigure(1, weight=1)
        self.p_dim_mode = tk.StringVar()
        modes = ttk.Frame(dim)
        modes.grid(row=0, column=0, columnspan=3, sticky="w")
        self._mode_buttons: list[ttk.Radiobutton] = []
        for mode in (OWN, INHERIT, OFF):
            b = ttk.Radiobutton(
                modes, text=MODE_LABELS[mode], value=mode, variable=self.p_dim_mode, command=self._profile_changed
            )
            b.pack(side=tk.LEFT, padx=(0, 8))
            self._mode_buttons.append(b)
        self.p_start, self.p_full, self.p_max = tk.IntVar(), tk.IntVar(), tk.IntVar()
        self._dim_widgets = [
            self._slider(dim, 1, "Beginnt ab Helligkeit", self.p_start, 0, 250, str, self._profile_changed),
            self._slider(dim, 2, "Volle Stärke ab", self.p_full, 1, 255, str, self._profile_changed),
            self._slider(dim, 3, "Stärkste Abdunkelung", self.p_max, 0, 94, lambda v: f"{v} %", self._profile_changed),
        ]
        self.p_attack, self.p_release = tk.StringVar(), tk.StringVar()
        ttk.Label(dim, text="Abdunkeln").grid(row=4, column=0, sticky="w", pady=2)
        a = self._combo(dim, self.p_attack, list(ATTACK_PRESETS), 10, self._profile_changed)
        a.grid(row=4, column=1, sticky="w", padx=8)
        ttk.Label(dim, text="Wieder aufhellen").grid(row=5, column=0, sticky="w", pady=2)
        r = self._combo(dim, self.p_release, list(RELEASE_PRESETS), 10, self._profile_changed)
        r.grid(row=5, column=1, sticky="w", padx=8)
        self._dim_widgets += [a, r]

        tint = ttk.Labelframe(right, text=" Blaulichtfilter ", style="Card.TLabelframe", padding=(10, 6))
        tint.pack(fill=tk.X, pady=(0, 8))
        tint.columnconfigure(1, weight=1)
        self.p_tint_mode = tk.StringVar()
        modes = ttk.Frame(tint)
        modes.grid(row=0, column=0, columnspan=3, sticky="w")
        for mode in (OWN, INHERIT, OFF):
            b = ttk.Radiobutton(
                modes, text=MODE_LABELS[mode], value=mode, variable=self.p_tint_mode, command=self._profile_changed
            )
            b.pack(side=tk.LEFT, padx=(0, 8))
            self._mode_buttons.append(b)
        self.p_kelvin, self.p_strength = tk.IntVar(), tk.IntVar()
        self._tint_widgets = [
            self._slider(
                tint,
                1,
                "Farbtemperatur",
                self.p_kelvin,
                KELVIN_MIN,
                KELVIN_MAX,
                lambda v: f"{round(v / 50) * 50} K",
                self._profile_changed,
            ),
            self._slider(tint, 2, "Stärke", self.p_strength, 0, TINT_MAX, lambda v: f"{v} %", self._profile_changed),
        ]
        self.swatch = tk.Canvas(tint, width=46, height=14, highlightthickness=0, bg=BG)
        self.swatch.grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(tint, text="niedrige Kelvin = wärmer, weniger Blau", style="Muted.TLabel").grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            right,
            text="„Übernehmen“: Werte kommen vom Grundprofil des Bildschirms (z. B. Nacht).\n"
            "So entstehen Mischungen wie „Zocken + Nacht“.",
            style="Muted.TLabel",
            justify=tk.LEFT,
        ).pack(anchor="w")

    def _build_rules(self, page: ttk.Frame) -> None:
        body = ttk.Frame(page, padding=(12, 0, 12, 0))
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="Liegt ein Programm auf einem Bildschirm vorne, gilt dort sein Profil –\n"
            "nur auf diesem Bildschirm. „Aus“ = dort nie abdunkeln.",
            style="Muted.TLabel",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 6))
        self.rule_tree = ttk.Treeview(body, columns=("exe", "profile"), show="headings", height=8)
        self.rule_tree.heading("exe", text="Programm")
        self.rule_tree.heading("profile", text="Profil")
        self.rule_tree.column("exe", width=220)
        self.rule_tree.column("profile", width=160)
        self.rule_tree.pack(fill=tk.BOTH, expand=True)
        self.rule_tree.bind("<<TreeviewSelect>>", lambda _e: self._rule_selected())
        form = ttk.Frame(body)
        form.pack(fill=tk.X, pady=(8, 0))
        self.r_exe, self.r_profile = tk.StringVar(), tk.StringVar()
        ttk.Label(form, text="Programm").grid(row=0, column=0, sticky="w")
        self.r_exe_box = ttk.Combobox(form, textvariable=self.r_exe, width=24)
        self.r_exe_box.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, text="Profil").grid(row=0, column=2, sticky="w")
        self.r_profile_box = ttk.Combobox(form, textvariable=self.r_profile, state="readonly", width=14)
        self.r_profile_box.grid(row=0, column=3, sticky="w", padx=6)
        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="Hinzufügen / ändern", command=self._save_rule).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Entfernen", command=self._remove_rule).pack(side=tk.LEFT, padx=6)
        ttk.Label(
            body,
            text="Die Liste „Programm“ zeigt, was zuletzt auf deinen Bildschirmen vorne lag.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 0))

    def _build_schedule(self, page: ttk.Frame) -> None:
        card = self._card(page, "Tag / Nacht")
        self.s_enabled = tk.BooleanVar()
        ttk.Checkbutton(
            card,
            text="Automatisch wechseln (für Bildschirme mit Grundprofil „Automatisch“)",
            variable=self.s_enabled,
            command=self._schedule_changed,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.s_day, self.s_night = tk.StringVar(), tk.StringVar()
        self.s_day_at, self.s_night_at = tk.StringVar(), tk.StringVar()
        ttk.Label(card, text="Tag-Profil").grid(row=1, column=0, sticky="w", pady=2)
        self.s_day_box = self._combo(card, self.s_day, [], 12, self._schedule_changed)
        self.s_day_box.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(card, text="ab").grid(row=1, column=2, sticky="w")
        ttk.Entry(card, textvariable=self.s_day_at, width=6).grid(row=1, column=3, sticky="w", padx=6)
        ttk.Label(card, text="Nacht-Profil").grid(row=2, column=0, sticky="w", pady=2)
        self.s_night_box = self._combo(card, self.s_night, [], 12, self._schedule_changed)
        self.s_night_box.grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(card, text="ab").grid(row=2, column=2, sticky="w")
        ttk.Entry(card, textvariable=self.s_night_at, width=6).grid(row=2, column=3, sticky="w", padx=6)
        for var in (self.s_day_at, self.s_night_at):
            var.trace_add("write", lambda *_: self._schedule_changed())
        fade = ttk.Frame(card)
        fade.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))
        fade.columnconfigure(1, weight=1)
        self.s_fade = tk.IntVar()
        self._slider(fade, 0, "Übergang", self.s_fade, 0, 120, lambda v: f"{v} min", self._schedule_changed)
        self.s_now = ttk.Label(page, style="Muted.TLabel")
        self.s_now.pack(anchor="w", padx=14)

    def _build_options(self, page: ttk.Frame) -> None:
        card = self._card(page, "Allgemein")
        card.columnconfigure(1, weight=1)
        self.rate_var = tk.StringVar()
        ttk.Label(card, text="Messrate").grid(row=0, column=0, sticky="w")
        self._combo(card, self.rate_var, list(RATE_PRESETS), 15, self._options_changed).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self.hotkey_var = tk.BooleanVar()
        self.start_paused_var = tk.BooleanVar()
        self.close_to_tray_var = tk.BooleanVar()
        self.start_minimized_var = tk.BooleanVar()
        for i, (text, var) in enumerate(
            (
                ("Tastenkürzel Strg+Alt+D (Pause/Weiter)", self.hotkey_var),
                ("Beim Programmstart pausiert", self.start_paused_var),
                ("Schließen-Knopf blendet nur aus (läuft im Infobereich weiter)", self.close_to_tray_var),
                ("Minimiert im Infobereich starten", self.start_minimized_var),
            ),
            start=1,
        ):
            ttk.Checkbutton(card, text=text, variable=var, command=self._options_changed).grid(
                row=i, column=0, columnspan=2, sticky="w"
            )
        log_card = self._card(page, "Protokoll")
        self.log_text = tk.Text(
            log_card,
            height=9,
            bg="#15171b",
            fg=MUTED,
            font=("Consolas", 9),
            borderwidth=0,
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ---- settings <-> widgets ------------------------------------------------------------
    def _load_all(self) -> None:
        self._loading = True
        try:
            s = self.settings
            names = self._profile_names()
            self.profile_list.delete(0, tk.END)
            for n in names:
                self.profile_list.insert(tk.END, n)
            if self._editing not in names:
                self._editing = names[0]
            idx = names.index(self._editing)
            self.profile_list.selection_clear(0, tk.END)
            self.profile_list.selection_set(idx)
            self._load_profile()
            self.rule_tree.delete(*self.rule_tree.get_children())
            for r in s.rules:
                self.rule_tree.insert("", tk.END, iid=r.exe, values=(r.exe, r.profile))
            self.r_profile_box.config(values=names)
            if self.r_profile.get() not in names:
                self.r_profile.set(names[0])
            self.s_enabled.set(s.schedule.enabled)
            base_names = self._profile_names(with_off=False)
            self.s_day_box.config(values=base_names)
            self.s_night_box.config(values=base_names)
            self.s_day.set(s.schedule.day_profile)
            self.s_night.set(s.schedule.night_profile)
            self.s_day_at.set(s.schedule.day_start)
            self.s_night_at.set(s.schedule.night_start)
            self.s_fade.set(s.schedule.fade_minutes)
            self.rate_var.set(rate_name(s.interval_ms))
            self.hotkey_var.set(s.hotkey)
            self.start_paused_var.set(s.start_paused)
            self.close_to_tray_var.set(s.close_to_tray)
            self.start_minimized_var.set(s.start_minimized)
            self._monitor_key = []  # rebuild monitor rows (profile names may have changed)
        finally:
            self._loading = False

    def _load_profile(self) -> None:
        p = self.settings.profile_map()[self._editing]
        was = self._loading
        self._loading = True
        try:
            self.p_title.config(text=p.name + ("  (fest: nie abdunkeln, kein Filter)" if p.builtin else ""))
            self.p_dim_mode.set(p.dim_mode)
            self.p_start.set(p.start)
            self.p_full.set(p.full)
            self.p_max.set(round(p.max_opacity / 255 * 100))
            self.p_attack.set(p.attack)
            self.p_release.set(p.release)
            self.p_tint_mode.set(p.tint_mode)
            self.p_kelvin.set(p.tint_kelvin)
            self.p_strength.set(p.tint_strength)
            state = tk.DISABLED if p.builtin else tk.NORMAL
            for b in self._mode_buttons:
                b.config(state=state)
            self._update_profile_widgets(p)
        finally:
            self._loading = was

    def _update_profile_widgets(self, p: Profile) -> None:
        dim_state = "!disabled" if p.dim_mode == OWN and not p.builtin else "disabled"
        for w in self._dim_widgets:
            w.state([dim_state])
            if isinstance(w, ttk.Combobox) and dim_state == "!disabled":
                w.state(["readonly"])
        tint_state = "!disabled" if p.tint_mode == OWN and not p.builtin else "disabled"
        for w in self._tint_widgets:
            w.state([tint_state])
        r, g, b = kelvin_to_rgb(p.tint_kelvin)
        self.swatch.delete("all")
        self.swatch.create_rectangle(0, 0, 46, 14, fill=f"#{r:02x}{g:02x}{b:02x}", outline="")

    def _profile_changed(self) -> None:
        if self._loading:
            return
        old = self.settings.profile_map()[self._editing]
        if old.builtin:
            return
        start, full = self.p_start.get(), self.p_full.get()
        if full <= start:  # push the other slider instead of letting the dragged one jump back
            if start != old.start:
                full = min(255, start + 1)
                self.p_full.set(full)
            else:
                start = max(0, full - 1)
                self.p_start.set(start)
        pct = self.p_max.get()
        max_opacity = old.max_opacity if round(old.max_opacity / 255 * 100) == pct else round(pct / 100 * 255)
        new = dataclasses.replace(
            old,
            dim_mode=self.p_dim_mode.get(),
            start=start,
            full=full,
            max_opacity=max_opacity,
            attack=self.p_attack.get(),
            release=self.p_release.get(),
            tint_mode=self.p_tint_mode.get(),
            tint_kelvin=self.p_kelvin.get(),
            tint_strength=self.p_strength.get(),
        ).normalized()
        self._update_profile_widgets(new)
        profiles = [new if p.name == old.name else p for p in self.settings.profiles]
        self._apply(dataclasses.replace(self.settings, profiles=profiles))

    def _apply(self, new: Settings, reload: bool = False) -> None:
        new = new.normalized()
        if new == self.settings:
            return
        self.settings = new
        self.engine.update_settings(new)
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(SAVE_DELAY_MS, self._save)
        if reload:
            self._load_all()

    def _save(self) -> None:
        self._save_job = None
        try:
            settings_mod.save(self.settings)
        except OSError as e:
            log.warning("Einstellungen konnten nicht gespeichert werden: %s", e)

    # ---- profiles --------------------------------------------------------------------------
    def _select_profile(self) -> None:
        sel = self.profile_list.curselection()
        if sel:
            self._editing = self.profile_list.get(sel[0])
            self._load_profile()

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Name des Profils:", initialvalue=initial, parent=self.root)
        if name is None:
            return None
        name = name.strip()[:40]
        if not name or name == AUTO:
            return None
        if name in self.settings.profile_map() and name != initial:
            messagebox.showinfo("Profile", f"„{name}“ gibt es schon.", parent=self.root)
            return None
        return name

    def _insert_profile(self, profile: Profile) -> None:
        profiles = [p for p in self.settings.profiles if not p.builtin] + [profile]
        profiles += [p for p in self.settings.profiles if p.builtin]
        self._editing = profile.name
        self._apply(dataclasses.replace(self.settings, profiles=profiles), reload=True)

    def _new_profile(self) -> None:
        name = self._ask_name("Neues Profil")
        if name:
            self._insert_profile(Profile(name, tint_mode=INHERIT))

    def _copy_profile(self) -> None:
        src = self.settings.profile_map()[self._editing]
        name = self._ask_name("Profil kopieren", f"{src.name} 2")
        if name:
            copy = dataclasses.replace(src, name=name)
            if src.builtin:
                copy = dataclasses.replace(copy, dim_mode=OWN)
            self._insert_profile(copy)

    def _rename_profile(self) -> None:
        old = self._editing
        if self.settings.profile_map()[old].builtin:
            return
        new = self._ask_name("Profil umbenennen", old)
        if not new or new == old:
            return
        s = self.settings

        def ren(n: str) -> str:
            return new if n == old else n

        self._editing = new
        self._apply(
            dataclasses.replace(
                s,
                profiles=[dataclasses.replace(p, name=ren(p.name)) for p in s.profiles],
                rules=[Rule(r.exe, ren(r.profile)) for r in s.rules],
                schedule=dataclasses.replace(
                    s.schedule, day_profile=ren(s.schedule.day_profile), night_profile=ren(s.schedule.night_profile)
                ),
                monitor_profiles={d: ren(n) for d, n in s.monitor_profiles.items()},
            ),
            reload=True,
        )

    def _delete_profile(self) -> None:
        name = self._editing
        s = self.settings
        if s.profile_map()[name].builtin:
            return
        if len(self._profile_names(with_off=False)) <= 1:
            messagebox.showinfo("Profile", "Das letzte Profil kann nicht gelöscht werden.", parent=self.root)
            return
        used = [r.exe for r in s.rules if r.profile == name]
        text = f"Profil „{name}“ löschen?"
        if used:
            text += "\nDiese Regeln werden mit entfernt: " + ", ".join(used)
        if not messagebox.askyesno("Profile", text, parent=self.root):
            return
        self._apply(
            dataclasses.replace(
                s,
                profiles=[p for p in s.profiles if p.name != name],
                monitor_profiles={d: n for d, n in s.monitor_profiles.items() if n != name},
            ),
            reload=True,
        )

    # ---- rules -------------------------------------------------------------------------------
    def _rule_selected(self) -> None:
        sel = self.rule_tree.selection()
        if sel:
            rule = self.settings.rule_map()
            self.r_exe.set(sel[0])
            self.r_profile.set(rule.get(sel[0], ""))

    def _save_rule(self) -> None:
        exe = self.r_exe.get().strip().lower()
        prof = self.r_profile.get()
        if not exe or prof not in self.settings.profile_map():
            return
        if not exe.endswith(".exe"):
            exe += ".exe"
        rules = [r for r in self.settings.rules if r.exe != exe] + [Rule(exe, prof)]
        self._apply(dataclasses.replace(self.settings, rules=rules), reload=True)

    def _remove_rule(self) -> None:
        sel = set(self.rule_tree.selection())
        if sel:
            rules = [r for r in self.settings.rules if r.exe not in sel]
            self._apply(dataclasses.replace(self.settings, rules=rules), reload=True)

    # ---- schedule / options ----------------------------------------------------------------
    def _schedule_changed(self) -> None:
        if self._loading:
            return
        sch = self.settings.schedule
        day_at, night_at = self.s_day_at.get().strip(), self.s_night_at.get().strip()
        valid = parse_hhmm(day_at, -1) >= 0 and parse_hhmm(night_at, -1) >= 0
        new = dataclasses.replace(
            sch,
            enabled=self.s_enabled.get(),
            day_profile=self.s_day.get(),
            night_profile=self.s_night.get(),
            day_start=day_at if valid else sch.day_start,
            night_start=night_at if valid else sch.night_start,
            fade_minutes=self.s_fade.get(),
        )
        self._apply(dataclasses.replace(self.settings, schedule=new))

    def _options_changed(self) -> None:
        if self._loading:
            return
        self._apply(
            dataclasses.replace(
                self.settings,
                interval_ms=RATE_PRESETS.get(self.rate_var.get(), self.settings.interval_ms),
                hotkey=self.hotkey_var.get(),
                start_paused=self.start_paused_var.get(),
                close_to_tray=self.close_to_tray_var.get(),
                start_minimized=self.start_minimized_var.get(),
            )
        )

    # ---- monitors --------------------------------------------------------------------------
    def _rebuild_monitors(self, monitors: list[Monitor], active: set[str]) -> None:
        for child in self.monitor_frame.winfo_children():
            child.destroy()
        self._monitor_rows.clear()
        choices = [AUTO_LABEL] + self._profile_names()
        for i, m in enumerate(monitors):
            box = ttk.Frame(self.monitor_frame)
            box.pack(fill=tk.X, pady=(2, 6))
            head = ttk.Frame(box)
            head.pack(fill=tk.X)
            var = tk.BooleanVar(value=m.device in active)
            text = f"Bildschirm {i + 1}  {m.width}×{m.height}" + ("  (Haupt)" if m.primary else "")
            ttk.Checkbutton(
                head, text=text, variable=var, command=functools.partial(self._toggle_monitor, m.device)
            ).pack(side=tk.LEFT)
            base_var = tk.StringVar()
            choice = self.settings.base_choice(m.device)
            base_var.set(AUTO_LABEL if choice == AUTO else choice)
            base = ttk.Combobox(head, textvariable=base_var, values=choices, state="readonly", width=22)
            base.bind("<<ComboboxSelected>>", functools.partial(self._base_changed, m.device, base_var))
            base.pack(side=tk.RIGHT)
            line = ttk.Frame(box)
            line.pack(fill=tk.X, pady=(2, 0))
            meter = Meter(line)
            meter.pack(side=tk.LEFT, padx=(22, 8))
            info = ttk.Label(line, text="", style="Muted.TLabel")
            info.pack(side=tk.LEFT)
            self._monitor_rows[m.device] = _MonitorRow(var, base, meter, info)

    def _base_changed(self, device: str, var: tk.StringVar, _event: object = None) -> None:
        value = var.get()
        mp = dict(self.settings.monitor_profiles)
        if value == AUTO_LABEL:
            mp.pop(device, None)
        else:
            mp[device] = value
        self._apply(dataclasses.replace(self.settings, monitor_profiles=mp))

    def _toggle_monitor(self, device: str) -> None:
        chosen = [d for d, row in self._monitor_rows.items() if row.var.get()]
        if not chosen:
            self._monitor_rows[device].var.set(True)  # at least one monitor stays active
            return
        # Monitors that are unplugged right now (laptop on the road) keep their choice.
        chosen += [d for d in self.settings.monitors if d not in self._monitor_rows]
        self._apply(dataclasses.replace(self.settings, monitors=chosen))

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

    # ---- window / tray -----------------------------------------------------------------------
    def close_window(self) -> None:
        """Window close button: hide to the tray when possible, otherwise quit."""
        if self.tray and self.tray.is_alive() and self.settings.close_to_tray:
            self.root.withdraw()
        else:
            self.quit()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        if self._save_job:  # write pending changes before leaving
            self.root.after_cancel(self._save_job)
            self._save()
        self.engine.stop()
        if self.tray:
            self.tray.close()
        self.root.destroy()

    def _set_all_bases(self, choice: str) -> None:
        mp = {} if choice == AUTO else {m.device: choice for m in self._monitor_key}
        self._apply(dataclasses.replace(self.settings, monitor_profiles=mp))
        self._monitor_key = []  # refresh the comboboxes

    def _handle_tray(self, st: Status) -> None:
        if not self.tray:
            return
        while True:
            try:
                action = self.tray.actions.get_nowait()
            except queue.Empty:
                break
            if action == "toggle":
                self.engine.toggle_paused()
            elif action == "show":
                if self.root.state() == "withdrawn":
                    self.show_window()
                else:
                    self.root.withdraw()
            elif action == "quit":
                self.quit()
                return
            elif action.startswith("base:"):
                self._set_all_bases(action[5:])
        if st.paused_reason == "user":
            tip = "Adaptive Screen Dimmer – pausiert"
        else:
            parts = [f"{i + 1}: {m.profile} {round(m.opacity / 255 * 100)} %" for i, m in enumerate(st.monitors)]
            tip = "Adaptive Screen Dimmer – " + " · ".join(parts)
        bases = set(self.settings.monitor_profiles.get(m.device, AUTO) for m in self._monitor_key) or {AUTO}
        current = bases.pop() if len(bases) == 1 else ""
        self.tray.set_state(bool(st.paused_reason), tip, self._profile_names(), current)

    # ---- log ---------------------------------------------------------------------------------
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

    # ---- polling -------------------------------------------------------------------------------
    def _poll(self) -> None:
        try:
            st = self.engine.snapshot()
            self._refresh(st)
            self._drain_log()
            self._handle_tray(st)
        finally:
            self.root.after(POLL_MS, self._poll)

    def _refresh(self, st: Status) -> None:
        monitors = self.engine.monitors()
        wanted = set(wanted_devices(self.settings.monitors, monitors))
        if monitors != self._monitor_key:
            self._monitor_key = monitors
            self._rebuild_monitors(monitors, wanted)
        rows = {m.device: m for m in st.monitors}
        for device, row in self._monitor_rows.items():
            if row.var.get() != (device in wanted):
                row.var.set(device in wanted)
            m = rows.get(device)
            if m is None:
                row.meter.show(0.0, None, None)
                row.info.config(text="nicht aktiv")
                continue
            eff = self._effective_thresholds(m.profile)
            row.meter.show(m.brightness, *eff)
            parts = [m.profile or "–"]
            if m.app:
                parts.append(m.app)
            parts.append(f"{round(m.opacity / 255 * 100)} %")
            if m.tint >= 0.5:
                parts.append(f"Filter {round(m.tint)} %")
            row.info.config(text="  ·  ".join(parts))

        stalled = st.running and st.heartbeat and time.monotonic() - st.heartbeat > STALL_S
        if st.error:
            text, color = f"Fehler: {st.error}", ERR
        elif stalled:
            text, color = "Reagiert nicht", ERR
        elif not st.running:
            text, color = "Gestoppt", ERR
        elif st.paused_reason == "user":
            text, color = "Pausiert", WARN
        else:
            text, color = "Aktiv", OK
        self.state_label.config(text=f"● {text}", fg=color)
        self.pause_btn.config(text="▶  Fortsetzen" if st.paused_reason == "user" else "⏸  Pausieren")
        self.hotkey_hint.config(text="Strg+Alt+D" if st.hotkey_ok else "")

        if st.recent_apps != self._recent_apps:
            self._recent_apps = list(st.recent_apps)
            self.r_exe_box.config(values=self._recent_apps)
        self._refresh_schedule_hint()

    def _effective_thresholds(self, label: str) -> tuple[float | None, float | None]:
        """Meter markers for the profile shown on a monitor (first name of a mix)."""
        name = label.split(" + ")[0].split(" → ")[0]
        p = self.settings.profile_map().get(name)
        if p is None or p.dim_mode != OWN:
            return (None, None)
        return (p.start, p.full)

    def _refresh_schedule_hint(self) -> None:
        sch = self.settings.schedule
        if not sch.enabled:
            self.s_now.config(text="Zeitplan aus: „Automatisch“ nutzt das Tag-Profil.")
            return
        lt = time.localtime()
        a, b, t = schedule_phase(sch, lt.tm_hour * 60 + lt.tm_min + lt.tm_sec / 60)
        if a == b:
            nxt = sch.night_start if a == sch.day_profile else sch.day_start
            self.s_now.config(text=f"Jetzt: {a}  ·  nächster Wechsel um {nxt}")
        else:
            self.s_now.config(text=f"Jetzt: Übergang {a} → {b} ({round(t * 100)} %)")
