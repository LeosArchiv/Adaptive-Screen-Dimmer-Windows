"use strict";

const $ = (id) => document.getElementById(id);
const POLL_MS = 125;
const SEND_DELAY_MS = 150;
const LOG_LINES = 300;

let api = null;
let state = null;
let profile = null;
let visible = true;
let pollTimer = null;
let pending = {};
let sendTimer = null;
const monitorEls = new Map();

const GLARE_LOCAL = 3;
const segLabels = new Map();  // seg id -> function value -> label, for relabelling on a language switch
let lastPaused = false;

const pct = (v) => Math.round((v / 255) * 100);

// Fields the backend does not know yet still get sensible values on screen.
const withDefaults = (p) => ({ glare_contrast: 6, glare_strength: 75, glare_margin: 1, glare_fade_ms: 300, ...state.defaults, ...p });

/* ---- sending ------------------------------------------------------------------------------------ */
function queueProfile(changes) {
  Object.assign(profile, changes);
  Object.assign(pending, changes);
  clearTimeout(sendTimer);
  sendTimer = setTimeout(flushProfile, SEND_DELAY_MS);
}

async function flushProfile() {
  const changes = pending;
  pending = {};
  if (!Object.keys(changes).length) return;
  const stored = await api.set_profile(changes);
  if (!Object.keys(pending).length) {  // no newer edits in flight: show what was really stored
    profile = withDefaults(stored);
    renderProfile();
  }
}

/* ---- controls ----------------------------------------------------------------------------------- */
function paintRange(input) {
  const min = Number(input.min), max = Number(input.max);
  input.style.setProperty("--p", `${((input.value - min) / (max - min)) * 100}%`);
}

function bindRange(id, toProfile) {
  const input = $(id);
  input.addEventListener("input", () => {
    paintRange(input);
    queueProfile(toProfile(Number(input.value)));
    renderProfile(id);
  });
}

function buildSeg(id, values, label, onPick) {
  const el = $(id);
  segLabels.set(id, label);
  el.replaceChildren(...values.map((value) => {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.value = String(value);
    b.textContent = label(value);
    b.addEventListener("click", () => onPick(value));
    return b;
  }));
}

function relabelSegs() {
  for (const [id, label] of segLabels) {
    for (const b of $(id).children) {
      const raw = b.dataset.value;
      b.textContent = label(/^\d+$/.test(raw) ? Number(raw) : raw);
    }
  }
}

function setSeg(id, value) {
  for (const b of $(id).children) b.setAttribute("aria-pressed", String(b.dataset.value === String(value)));
}

function setRange(id, value) {
  const input = $(id);
  if (document.activeElement !== input || !input.matches(":active")) input.value = value;
  paintRange(input);
}

function kelvinColor(k) {
  const key = String(Math.round(k / 100) * 100);
  return state.kelvin[key] || "#ffffff";
}

function renderProfile(skip) {
  const p = profile;
  if (skip !== "start") setRange("start", p.start);
  if (skip !== "full") setRange("full", p.full);
  if (skip !== "max") setRange("max", Math.round((p.max_opacity / 255) * 100));
  $("start-out").textContent = `${pct(p.start)} %`;
  $("full-out").textContent = `${pct(p.full)} %`;
  $("max-out").textContent = `${Math.round((p.max_opacity / 255) * 100)} %`;
  setSeg("attack", p.attack);
  setSeg("release", p.release);
  setSeg("glare", p.glare);
  $("glare-local").hidden = p.glare !== GLARE_LOCAL;
  if (skip !== "glare_contrast") setRange("glare_contrast", p.glare_contrast);
  if (skip !== "glare_strength") setRange("glare_strength", p.glare_strength);
  if (skip !== "glare_fade_ms") setRange("glare_fade_ms", p.glare_fade_ms);
  $("glare_contrast-out").textContent = tr("brighter", { n: p.glare_contrast });
  $("glare_strength-out").textContent = `${p.glare_strength} %`;
  $("glare_fade_ms-out").textContent = `${(p.glare_fade_ms / 1000).toFixed(p.glare_fade_ms % 100 ? 2 : 1).replace(".", tr("decimal"))} s`;
  setSeg("glare_margin", p.glare_margin);

  $("tint_on").checked = p.tint_on;
  $("tint-group").classList.toggle("off", !p.tint_on);
  if (skip !== "kelvin") setRange("kelvin", p.tint_kelvin);
  if (skip !== "strength") setRange("strength", p.tint_strength);
  $("kelvin-out").textContent = `${p.tint_kelvin} K`;
  $("swatch").style.background = kelvinColor(p.tint_kelvin);
  $("strength-out").textContent = `${p.tint_strength} %`;
  $("tint_night_only").checked = p.tint_night_only;
  for (const id of ["night_start", "day_start"]) {
    const el = $(id);
    if (document.activeElement !== el) el.value = p[id];
    el.disabled = !p.tint_night_only;
  }
  renderMeters();
}

function renderOptions(o) {
  setSeg("rate", o.rate);
  for (const id of ["hotkey", "start_paused", "close_to_tray", "start_minimized"]) $(id).checked = o[id];
  setSeg("language", o.language);
}

function applyLanguage(lang) {
  LANG = lang;
  translatePage();
  relabelSegs();
  renderProfile();
  renderRunning(lastPaused);
  renderMonitors(lastMonitors);
}

function setup() {
  const c = state.choices;
  const lim = state.limits;
  $("kelvin").min = lim.kelvin_min;
  $("kelvin").max = lim.kelvin_max;
  $("strength").max = lim.tint_max;

  bindRange("start", (v) => {
    const changes = { start: v };
    if (profile.full <= v) changes.full = Math.min(255, v + 1);
    return changes;
  });
  bindRange("full", (v) => {
    const changes = { full: v };
    if (profile.start >= v) changes.start = Math.max(0, v - 1);
    return changes;
  });
  bindRange("max", (v) => ({ max_opacity: Math.round((v / 100) * 255) }));
  bindRange("kelvin", (v) => ({ tint_kelvin: v }));
  bindRange("strength", (v) => ({ tint_strength: v }));
  bindRange("glare_contrast", (v) => ({ glare_contrast: v }));
  bindRange("glare_strength", (v) => ({ glare_strength: v }));
  bindRange("glare_fade_ms", (v) => ({ glare_fade_ms: v }));
  const pick = (field) => (v) => { queueProfile({ [field]: v }); renderProfile(); };
  buildSeg("glare_margin", [0, 1, 2], (v) => choiceLabel("margin", v), pick("glare_margin"));
  buildSeg("attack", c.attack, (v) => choiceLabel("attack", v), pick("attack"));
  buildSeg("release", c.release, (v) => choiceLabel("release", v), pick("release"));
  buildSeg("glare", [0, 1, 2, 3], (v) => choiceLabel("glare", v), pick("glare"));
  buildSeg("rate", c.rate, (v) => choiceLabel("rate", v), (v) => { setSeg("rate", v); api.set_option("rate", v); });
  buildSeg("language", Object.keys(LANGUAGE_CHOICES), (v) => LANGUAGE_CHOICES[v], async (v) => {
    setSeg("language", v);
    applyLanguage(await api.set_option("language", v));
  });

  $("tint_on").addEventListener("change", (e) => { queueProfile({ tint_on: e.target.checked }); renderProfile(); });
  $("tint_night_only").addEventListener("change", (e) => {
    queueProfile({ tint_night_only: e.target.checked });
    renderProfile();
  });
  for (const id of ["night_start", "day_start"]) {
    $(id).addEventListener("change", (e) => { if (e.target.value) queueProfile({ [id]: e.target.value }); });
  }
  for (const id of ["hotkey", "start_paused", "close_to_tray", "start_minimized"]) {
    $(id).addEventListener("change", (e) => api.set_option(id, e.target.checked));
  }

  $("running").addEventListener("change", async () => {
    const paused = await api.toggle_pause();
    renderRunning(paused);
  });
  $("identify").addEventListener("click", () => api.identify());
  for (const btn of document.querySelectorAll("[data-reset]")) {
    btn.addEventListener("click", async () => {
      await flushProfile();  // edits still waiting must not overwrite the reset afterwards
      const res = await api.reset_section(btn.dataset.reset);
      profile = withDefaults(res.profile);
      renderProfile();
      renderOptions(res.options);
    });
  }

  renderProfile();
  renderOptions(state.options);
}

/* ---- live status -------------------------------------------------------------------------------- */
function renderRunning(paused) {
  lastPaused = paused;
  $("running").checked = !paused;
  $("running-label").textContent = tr(paused ? "paused" : "on");
}

function monitorEl(m) {
  let el = monitorEls.get(m.device);
  if (el) return el;
  el = $("monitor-tpl").content.firstElementChild.cloneNode(true);
  const box = el.querySelector("input");
  box.addEventListener("change", () => api.set_monitor_enabled(m.device, box.checked));
  for (const t of el.querySelectorAll("[data-t]")) t.textContent = tr(t.dataset.t);
  monitorEls.set(m.device, el);
  return el;
}

let lastMonitors = [];

function renderMeters() {
  for (const m of lastMonitors) {
    const el = monitorEls.get(m.device);
    if (!el) continue;
    // The sliders win over the engine's values so the markers follow the hand without lag.
    const start = profile.start, full = profile.full;
    const ramp = el.querySelector(".ramp");
    ramp.style.left = `${(start / 255) * 100}%`;
    ramp.style.setProperty("--ramp-end", `${((full - start) / (255 - start || 1)) * 100}%`);
    el.querySelector(".needle").style.left = `${(m.brightness / 255) * 100}%`;
  }
}

function renderMonitors(list) {
  const box = $("monitors");
  const order = list.map(monitorEl);
  if (order.length !== box.children.length || order.some((el, i) => box.children[i] !== el)) {
    box.replaceChildren(...order);
  }
  list.forEach((m, i) => {
    const el = order[i];
    el.classList.toggle("off", !m.active);
    el.querySelector(".mon-num").textContent = m.number;
    el.querySelector(".mon-title").textContent = m.primary ? tr("main_display") : tr("display_n", { n: m.number });
    const meta = [m.size];
    if (m.active && m.capture) meta.push(tr("measured_by", { c: m.capture }));
    if (!m.active) meta.push(tr("not_dimmed"));
    el.querySelector(".mon-meta").textContent = meta.join(", ");
    const input = el.querySelector("input");
    if (document.activeElement !== input) input.checked = m.enabled;
    el.querySelector(".v-bright").textContent = m.active ? `${pct(m.brightness)} %` : "";
    el.querySelector(".v-dim").textContent = m.active ? `${m.dim} %` : "";
    el.querySelector(".v-tint").textContent = m.active ? `${m.tint} %` : "";
  });
  lastMonitors = list;
  renderMeters();
}

function appendLog(lines) {
  if (!lines.length) return;
  const pre = $("log");
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
  const all = (pre.textContent ? pre.textContent.split("\n") : []).concat(lines);
  pre.textContent = all.slice(-LOG_LINES).join("\n");
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

async function poll() {
  pollTimer = null;
  if (!visible || document.hidden) return;
  try {
    const s = await api.get_status();
    const st = $("status");
    st.dataset.kind = s.state.kind;
    let text = tr(`state_${s.state.reason}`, { e: s.state.detail || "" });
    if (s.state.kind === "ok" && profile.tint_on && s.tint_now) text += `, ${tr("tint_now")}`;
    $("status-text").textContent = text;
    renderRunning(s.paused);
    $("hotkey-hint").textContent = $("hotkey").checked && !s.hotkey_ok ? tr("hotkey_taken") : "";
    $("night-hint").textContent = profile.tint_night_only
      ? (s.tint_now ? tr("night_now") : tr("night_from", { t: profile.night_start }))
      : "";
    renderMonitors(s.monitors);
    appendLog(s.log);
  } catch (e) {
    console.error(e);
  }
  schedulePoll();
}

function schedulePoll() {
  if (!pollTimer && visible && !document.hidden) pollTimer = setTimeout(poll, POLL_MS);
}

window.dimmerVisible = (v) => {
  visible = v;
  schedulePoll();
};
document.addEventListener("visibilitychange", schedulePoll);
document.addEventListener("contextmenu", (e) => e.preventDefault());

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  state = await api.get_state();
  profile = withDefaults(state.profile);
  LANG = state.lang;
  translatePage();
  setup();
  $("app").hidden = false;
  poll();
});
