"use strict";

// Texts of the page. Keys in index.html: data-t (text), data-t-aria (aria-label), data-t-title (title).
const STRINGS = {
  de: {
    starting: "Startet",
    on: "An",
    paused: "Pausiert",
    shortcut_title: "Strg+Alt+D",
    displays: "Bildschirme",
    show_numbers: "Nummern zeigen",
    reset: "Standardwerte",
    dim: "Abdunkeln",
    start: "Beginn",
    start_hint: "ab hier wird gedimmt",
    full: "Volle Stärke ab",
    max: "Stärkste Abdunkelung",
    attack: "Dunkler werden",
    release: "Heller werden",
    glare: "Helle Flecken",
    mode: "Modus",
    mode_hint: "Lokal dunkelt nur die grelle Stelle ab",
    sensitivity: "Empfindlichkeit",
    sensitivity_hint: "kleiner reagiert früher",
    strength: "Stärke",
    margin: "Rand",
    fade: "Ausblenden",
    tint: "Blaulichtfilter",
    kelvin: "Farbtemperatur",
    night_only: "Nur nachts",
    from: "von",
    to: "bis",
    options: "Optionen",
    rate: "Messrate",
    hotkey: "Tastenkürzel Strg+Alt+D",
    start_paused: "Beim Start pausiert",
    close_to_tray: "Schließen blendet nur aus",
    start_minimized: "Minimiert starten",
    log: "Protokoll",
    brightness: "Helligkeit",
    dimmed: "Abdunkelung",
    filter: "Filter",
    main_display: "Hauptbildschirm",
    display_n: "Bildschirm {n}",
    measured_by: "Messung per {c}",
    not_dimmed: "nicht gedimmt",
    brighter: "{n}× heller",
    state_ok: "Aktiv",
    state_paused: "Pausiert",
    state_stalled: "Reagiert nicht",
    state_stopped: "Gestoppt",
    state_error: "Fehler: {e}",
    tint_now: "Blaulichtfilter an",
    hotkey_taken: "Kürzel ist schon belegt",
    night_now: "gerade aktiv",
    night_from: "aktiv ab {t}",
    decimal: ",",
    choices: {
      attack: { Sofort: "Sofort", Schnell: "Schnell", Sanft: "Sanft" },
      release: { Schnell: "Schnell", Normal: "Normal", Langsam: "Langsam" },
      glare: ["Aus", "Normal", "Stark", "Lokal"],
      margin: ["Eng", "Normal", "Weit"],
      rate: { Sparsam: "Sparsam", Normal: "Normal", Schnell: "Schnell" },
    },
  },
  en: {
    starting: "Starting",
    on: "On",
    paused: "Paused",
    shortcut_title: "Ctrl+Alt+D",
    displays: "Displays",
    show_numbers: "Show numbers",
    reset: "Reset",
    dim: "Dim",
    start: "Starts at",
    start_hint: "dimming begins here",
    full: "Full strength at",
    max: "Maximum dimming",
    attack: "Getting darker",
    release: "Getting brighter",
    glare: "Bright spots",
    mode: "Mode",
    mode_hint: "Local darkens only the bright spot",
    sensitivity: "Sensitivity",
    sensitivity_hint: "lower reacts sooner",
    strength: "Strength",
    margin: "Margin",
    fade: "Fade out",
    tint: "Blue light filter",
    kelvin: "Color temperature",
    night_only: "Night only",
    from: "from",
    to: "to",
    options: "Options",
    rate: "Sample rate",
    hotkey: "Shortcut Ctrl+Alt+D",
    start_paused: "Start paused",
    close_to_tray: "Close to tray",
    start_minimized: "Start minimized",
    log: "Log",
    brightness: "Brightness",
    dimmed: "Dimmed",
    filter: "Filter",
    main_display: "Main display",
    display_n: "Display {n}",
    measured_by: "measured via {c}",
    not_dimmed: "not dimmed",
    brighter: "{n}× brighter",
    state_ok: "Active",
    state_paused: "Paused",
    state_stalled: "Not responding",
    state_stopped: "Stopped",
    state_error: "Error: {e}",
    tint_now: "blue light filter on",
    hotkey_taken: "Already used by another app",
    night_now: "active now",
    night_from: "starts at {t}",
    decimal: ".",
    choices: {
      attack: { Sofort: "Instant", Schnell: "Fast", Sanft: "Gentle" },
      release: { Schnell: "Fast", Normal: "Normal", Langsam: "Slow" },
      glare: ["Off", "Normal", "Strong", "Local"],
      margin: ["Tight", "Normal", "Wide"],
      rate: { Sparsam: "Low", Normal: "Normal", Schnell: "High" },
    },
  },
};

// Language names are always shown in their own language.
const LANGUAGE_CHOICES = { auto: "Auto", de: "Deutsch", en: "English" };

let LANG = "de";

function tr(key, vars) {
  let text = (STRINGS[LANG] && STRINGS[LANG][key]) ?? STRINGS.en[key] ?? key;
  for (const [k, v] of Object.entries(vars || {})) text = text.replace(`{${k}}`, v);
  return text;
}

function choiceLabel(group, value) {
  const table = STRINGS[LANG].choices[group];
  return (table && table[value]) ?? String(value);
}

function translatePage() {
  document.documentElement.lang = LANG;
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = tr(el.dataset.t);
  for (const el of document.querySelectorAll("[data-t-aria]")) el.setAttribute("aria-label", tr(el.dataset.tAria));
  for (const el of document.querySelectorAll("[data-t-title]")) el.title = tr(el.dataset.tTitle);
}
