"""Application entry point: single instance, logging, engine + GUI wiring."""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import logging
import logging.handlers
import os
import sys
import threading
from typing import TYPE_CHECKING

from . import settings as settings_mod
from .winapi import enable_dpi_awareness

if TYPE_CHECKING:
    from .gui import QueueLogHandler

log = logging.getLogger("dimmer")

MUTEX_NAME = "Local\\AdaptiveScreenDimmer.SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _single_instance() -> object | None:
    """Returns a mutex handle, or None when another instance already runs."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:  # could not create the guard at all: run without it rather than refuse
        log.warning("Einzelinstanz-Sperre nicht verfügbar (%d)", ctypes.get_last_error())
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _message_box(text: str) -> None:
    MB_ICONINFORMATION = 0x40
    ctypes.windll.user32.MessageBoxW(None, text, "Adaptive Screen Dimmer", MB_ICONINFORMATION)


def _setup_logging(verbose: bool) -> QueueLogHandler:
    from .gui import QueueLogHandler

    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    queue_handler = QueueLogHandler()
    log.addHandler(queue_handler)
    try:
        path = settings_mod.config_dir() / "dimmer.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=256_000, backupCount=1, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
        log.addHandler(file_handler)
    except OSError:
        pass
    if sys.stderr is not None:  # pythonw / windowed EXE have no console
        log.addHandler(logging.StreamHandler())
    return queue_handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Adaptive Screen Dimmer")
    ap.add_argument("--exit-after", type=float, metavar="SEC", help="quit automatically (testing kill switch)")
    ap.add_argument("--paused", action="store_true", help="start paused")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    enable_dpi_awareness()  # before any window exists
    mutex = _single_instance()
    if mutex is None:
        _message_box("Das Programm läuft bereits. Du findest es unten rechts im Infobereich.")
        return 1

    queue_handler = _setup_logging(args.verbose)
    from .engine import Engine
    from .gui import DimmerApp
    from .tray import TrayIcon

    settings = settings_mod.load()
    # --paused applies to this run only and must not end up in the saved settings
    engine_settings = dataclasses.replace(settings, start_paused=True) if args.paused else settings
    log.info("Start")

    engine = Engine(engine_settings)
    engine.start()
    tray = TrayIcon()
    tray.start()
    tray.wait_ready()
    hidden = settings.start_minimized and tray.is_alive()
    ui = DimmerApp(engine, settings, queue_handler, tray if tray.is_alive() else None, start_hidden=hidden)
    if args.exit_after:
        timer = threading.Timer(args.exit_after, ui.quit)
        timer.daemon = True
        timer.start()
        # Hard fallback in case the window never came up and could not be closed normally.
        hard = threading.Timer(args.exit_after + 15, lambda: os._exit(2))
        hard.daemon = True
        hard.start()
    try:
        ui.run()
    finally:
        engine.stop()  # overlays are destroyed on the engine thread
        tray.close()
    log.info("Beendet")
    return 0


def run() -> None:
    try:
        sys.exit(main())
    except Exception:
        log.exception("Unerwarteter Fehler")
        raise
