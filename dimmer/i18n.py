"""Language choice and the few user-visible strings shown outside the web page (tray, message box).

The page has its own table in dimmer/web/i18n.js. Log messages stay English.
"""

from __future__ import annotations

import ctypes

LANGUAGES = ("de", "en")
LANG_GERMAN = 0x07  # primary language id of every German locale

STRINGS: dict[str, dict[str, str]] = {
    "de": {
        "tray_pause": "Pausieren",
        "tray_resume": "Fortsetzen",
        "tray_open": "Fenster öffnen",
        "tray_quit": "Beenden",
        "tip_paused": "pausiert",
        "already_running": "Das Programm läuft bereits. Du findest es unten rechts im Infobereich.",
    },
    "en": {
        "tray_pause": "Pause",
        "tray_resume": "Resume",
        "tray_open": "Open window",
        "tray_quit": "Quit",
        "tip_paused": "paused",
        "already_running": "The app is already running. You can find it in the notification area.",
    },
}


def windows_language() -> str:
    """'de' when the Windows display language is German, otherwise 'en'."""
    try:
        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except (AttributeError, OSError):
        return "en"
    return "de" if lang_id & 0x3FF == LANG_GERMAN else "en"


def resolve_language(setting: str) -> str:
    """Turns the setting ('auto', 'de', 'en') into the language to show."""
    return setting if setting in LANGUAGES else windows_language()


def t(lang: str, key: str) -> str:
    table = STRINGS.get(lang, STRINGS["en"])
    return table.get(key, STRINGS["en"].get(key, key))
