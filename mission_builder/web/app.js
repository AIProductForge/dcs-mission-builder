"use strict";

const FIELDS = ["aircraft", "terrain", "airport", "mission_type", "start", "flight_size",
                "date", "hour", "weather", "enemy_skill", "awacs", "tanker"];
const PIN_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M10.5 1.5 14.5 5.5 12.6 6.1 9.9 8.8 10.2 12 8.9 13.3 6.2 10.6 2.6 14.2 1.8 13.4 5.4 9.8 2.7 7.1 4 5.8 7.2 6.1 9.9 3.4z"/></svg>';

const $ = (id) => document.getElementById(id);
const el = (id) => $("f-" + id);
const state = { options: null, locks: new Set(), seed: 0, map: null, layer: null, busy: false };

// ---------- API ----------
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function setStatus(text, kind = "") {
  const s = $("status");
  s.textContent = text;
  s.className = "status " + kind;
}

// ---------- form ----------
function fillSelect(select, items, labelOf = (x) => x, valueOf = (x) => x) {
  select.innerHTML = "";
  for (const item of items) {
    const o = document.createElement("option");
    o.value = valueOf(item);
    o.textContent = labelOf(item);
    select.appendChild(o);
  }
}

function aircraftLabel(a) {
  return a.id === a.module ? a.id : `${a.id}  (${a.module})`;
}

function getValues() {
  const v = {};
  for (const f of FIELDS) {
    const input = el(f);
    v[f] = input.type === "checkbox" ? input.checked : input.value;
  }
  v.flight_size = Number(v.flight_size);
  v.hour = Number(v.hour);
  v.seed = state.seed;
  return v;
}

async function refreshAirports(keep) {
  const terrain = el("terrain").value, aircraft = el("aircraft").value;
  const list = await api(`/api/airports?terrain=${encodeURIComponent(terrain)}&aircraft=${encodeURIComponent(aircraft)}`);
  fillSelect(el("airport"), list);
  if (keep && list.includes(keep)) el("airport").value = keep;
}

async function setValues(v) {
  el("aircraft").value = v.aircraft;
  el("terrain").value = v.terrain;
  await refreshAirports(v.airport);
  for (const f of ["mission_type", "start", "weather", "enemy_skill"]) el(f).value = v[f];
  el("flight_size").value = String(v.flight_size);
  el("hour").value = String(v.hour);
  el("date").value = v.date;
  el("awacs").checked = !!v.awacs;
  el("tanker").checked = !!v.tanker;
  state.seed = v.seed || 0;
}

function renderPins() {
  for (const node of document.querySelectorAll(".field")) {
    const pinned = state.locks.has(node.dataset.field);
    node.classList.toggle("pinned", pinned);
    const btn = node.querySelector(".pin");
    btn.setAttribute("aria-pressed", String(pinned));
    btn.title = pinned ? "Pinned: kept when generating. Click to unpin." : "Not pinned: rerolled when generating. Click to pin.";
  }
}

function setupPins() {
  for (const node of document.querySelectorAll(".field")) {
    const f = node.dataset.field;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pin";
    btn.innerHTML = PIN_SVG;
    btn.setAttribute("aria-label", `Pin ${f.replace("_", " ")}`);
    btn.addEventListener("click", () => {
      state.locks.has(f) ? state.locks.delete(f) : state.locks.add(f);
      if (f === "airport" && state.locks.has("airport")) state.locks.add("terrain");
      renderPins();
    });
    node.appendChild(btn);
    el(f).addEventListener("change", async () => {
      state.locks.add(f);
      if (f === "airport") state.locks.add("terrain");
      if (f === "terrain" || f === "aircraft") {
        const keep = el("airport").value;
        await refreshAirports(keep);
        if (f === "terrain") state.locks.delete("airport");
      }
      renderPins();
      setStatus("Edited. Generate to reroll the rest, or Apply edits to keep this exact mission.");
    });
  }
}

// ---------- generate / save ----------
async function generate(lockEverything = false) {
  if (state.busy) return;
  state.busy = true;
  $("doc").classList.add("busy");
  setStatus(lockEverything ? "Applying edits…" : "Generating…");
  const locks = lockEverything ? [...FIELDS, "seed"] : [...state.locks];
  try {
    const res = await api("/api/generate", { values: getValues(), locks });
    await setValues(res.values);
    renderBriefing(res.briefing, res.values);
    $("btn-save").disabled = false;
    $("b-file").textContent = "NOT YET SAVED";
    setStatus(`Ready: OPERATION ${res.briefing.codename}. Save .miz when you're happy with it.`, "ok");
  } catch (e) {
    setStatus(e.message, "err");
  } finally {
    state.busy = false;
    $("doc").classList.remove("busy");
  }
}

async function save() {
  try {
    const res = await api("/api/save", {});
    $("b-file").textContent = res.path.toUpperCase();
    setStatus(`Saved ${res.path}`, "ok");
  } catch (e) {
    setStatus(e.message, "err");
  }
}

// ---------- briefing ----------
function ddm(lat, lon) {
  const part = (v, pos, neg, degW) => {
    const h = v >= 0 ? pos : neg;
    const a = Math.abs(v), d = Math.floor(a), m = (a - d) * 60;
    return `${h}${String(d).padStart(degW, "0")}°${m.toFixed(1).padStart(4, "0")}'`;
  };
  return `${part(lat, "N", "S", 2)} ${part(lon, "E", "W", 3)}`;
}

function text(id, value) { $(id).textContent = value ?? "—"; }

function rows(tbodySel, list, cells, emptyText) {
  const tbody = document.querySelector(tbodySel + " tbody");
  tbody.innerHTML = "";
  const cols = document.querySelectorAll(tbodySel + " thead th").length || 2;
  if (!list.length) {
    const tr = tbody.insertRow();
    tr.className = "empty";
    const td = tr.insertCell();
    td.colSpan = cols;
    td.textContent = emptyText;
    return;
  }
  for (const item of list) {
    const tr = tbody.insertRow();
    for (const [value, cls, html] of cells(item)) {
      const td = tr.insertCell();
      if (cls) td.className = cls;
      if (html) td.innerHTML = value; else td.textContent = value ?? "—";
    }
  }
}

function fmtNum(n) { return n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"); }

function renderBriefing(b, v) {
  text("b-codename", b.codename);
  text("b-msn", String(v.seed % 10000).padStart(4, "0"));
  text("b-dtg", b.dtg);
  text("b-theatre", b.theatre.toUpperCase());
  text("b-flight", `${b.callsign} (${b.flight_size}-SHIP)`);
  text("b-aircraft", b.aircraft);
  text("b-task", b.mission_type.toUpperCase());

  const enemy = b.situation.filter((s) => !s.startsWith("Friendly"));
  text("b-enemy", enemy.join(" ") || "None reported.");
  text("b-friendly", b.situation.filter((s) => s.startsWith("Friendly")).join(" "));
  const w = b.weather;
  text("b-weather", `${w.summary}${w.cloud_base_ft ? `, cloud base ${fmtNum(w.cloud_base_ft)} ft` : ""}. ` +
                    `Visibility ${w.visibility}. Surface temperature ${w.temp_c} °C.`);
  text("b-mission", b.mission_statement);
  const loadout = b.loadout.replace(/^Liberation\s+/i, "");
  text("b-loadout", b.loadout === "Guns only" ? "Guns only (no stored loadout for this aircraft)." : `${loadout} configuration.`);

  rows("#b-route", b.waypoints, (p) => [
    [p.n, "num"], [p.name], [ddm(p.lat, p.lon)],
    [p.brg === undefined ? "—" : String(p.brg).padStart(3, "0"), "num"],
    [p.dist_nm === undefined ? "—" : p.dist_nm.toFixed(1), "num"],
    [p.alt_ft ? fmtNum(p.alt_ft) : "GND", "num"], [p.speed_kt ? p.speed_kt : "—", "num"],
  ], "No route.");

  rows("#b-threats", b.threats, (t) => [
    [t.name], [`<span class="tag ${t.kind}">${t.kind}</span>`, "", true], [ddm(t.lat, t.lon)],
    [t.range_nm ?? "—", "num"], [t.note],
  ], "No threats expected.");

  rows("#b-support", b.support, (s) => [
    [s.role], [s.callsign], [s.type], [s.freq + " MHz"], [s.tacan || "—"], [fmtNum(s.alt_ft), "num"],
  ], "No support assets.");

  const h = b.home;
  const signal = [
    ["Flight callsign", `${b.callsign}`],
    [`${h.name} tower`, [h.uhf && `${h.uhf} MHz UHF`, h.vhf && `${h.vhf} MHz VHF`].filter(Boolean).join(" / ") || "—"],
    [`${h.name} runways`, h.runways.length ? h.runways.join(", ") : "Helipad"],
    ["Home field TACAN", h.tacan || "None"],
    ...b.support.map((s) => [`${s.callsign} (${s.role})`, `${s.freq} MHz${s.tacan ? `, TACAN ${s.tacan}` : ""}`]),
    ["Bullseye", `${ddm(b.bullseye.lat, b.bullseye.lon)} (over ${h.name})`],
    ["Objective", `${ddm(b.objective.lat, b.objective.lon)}, ${String(b.objective.brg).padStart(3, "0")}°T / ${b.objective.dist_nm} nm from home`],
  ];
  const tbody = document.querySelector("#b-signal tbody");
  tbody.innerHTML = "";
  for (const [k, val] of signal) {
    const tr = tbody.insertRow();
    const th = document.createElement("th");
    th.textContent = k;
    tr.appendChild(th);
    tr.insertCell().textContent = val;
  }

  const notes = $("b-notes");
  notes.innerHTML = "";
  for (const n of b.notes) {
    const li = document.createElement("li");
    li.textContent = n;
    notes.appendChild(li);
  }
  $("notes-section").hidden = !b.notes.length;

  renderMap(b);
}

// ---------- map ----------
function initMap() {
  if (!window.L) {
    $("map").innerHTML = '<div class="map-offline">Map library could not load (offline?). The mission itself is unaffected.</div>';
    return;
  }
  state.map = L.map("map", { zoomControl: true, attributionControl: true, scrollWheelZoom: false });
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 15, subdomains: "abc",
    attribution: 'Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ' +
                 'SRTM | style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
  }).addTo(state.map);
  state.layer = L.layerGroup().addTo(state.map);
  state.map.on("click", () => state.map.scrollWheelZoom.enable());
  state.map.on("mouseout", () => state.map.scrollWheelZoom.disable());
  let lastWidth = 0;
  new ResizeObserver(([entry]) => {
    const w = Math.round(entry.contentRect.width);
    if (w !== lastWidth) { lastWidth = w; fitMap(); }
  }).observe($("map"));
  if (document.fonts) document.fonts.ready.then(fitMap);
}

function label(latlng, html, cls, anchor = [-14, 10]) {
  return L.marker(latlng, {
    interactive: false, keyboard: false,
    icon: L.divIcon({ className: "", html: `<div class="map-text ${cls}">${html}</div>`, iconAnchor: anchor }),
  });
}

function renderMap(b) {
  if (!state.map) return;
  const css = getComputedStyle(document.documentElement);
  const C = { threat: css.getPropertyValue("--threat").trim(), support: css.getPropertyValue("--support").trim(),
              route: css.getPropertyValue("--route").trim() };
  const g = state.layer;
  g.clearLayers();
  const bounds = [];
  const ll = (p) => [p.lat, p.lon];

  // bullseye
  for (const r of [10, 20, 30]) {
    L.circle(ll(b.bullseye), { radius: r * 1852, color: C.support, weight: 0.8, opacity: 0.35, fill: false, interactive: false }).addTo(g);
  }

  // support tracks
  for (const s of b.support) {
    const pts = s.track.map(ll);
    L.polyline(pts, { color: C.support, weight: 3, dashArray: "8 6" }).addTo(g)
      .bindTooltip(`${s.callsign} — ${s.role}, ${s.freq} MHz`, { className: "map-tip", sticky: true });
    label(pts[0], `${s.callsign} ${s.freq}`, "support").addTo(g);
    bounds.push(...pts);
  }

  // threats
  for (const t of b.threats) {
    if (t.range_nm) {
      L.circle(ll(t), { radius: t.range_nm * 1852, color: C.threat, weight: 1.5, fillColor: C.threat, fillOpacity: 0.12 }).addTo(g)
        .bindTooltip(`${t.name}: ${t.range_nm} nm ring`, { className: "map-tip", sticky: true });
      label(ll(t), t.name, "threat", [-10, -6]).addTo(g);
      bounds.push(ll(t));
    }
  }
  for (const a of b.enemy_air) {
    const pts = a.route.map(ll);
    L.polyline(pts, { color: C.threat, weight: 2.5, dashArray: "4 6" }).addTo(g)
      .bindTooltip(`${a.name} patrol`, { className: "map-tip", sticky: true });
    L.marker(pts[0], { interactive: false, icon: L.divIcon({ className: "", iconAnchor: [8, 8],
      html: `<svg width="16" height="16" viewBox="0 0 16 16"><path d="M8 1 15 14H1z" fill="${C.threat}"/></svg>` }) }).addTo(g);
    label(pts[0], a.name.toUpperCase(), "threat", [-12, -8]).addTo(g);
    bounds.push(...pts);
  }
  for (const t of b.targets) {
    L.circleMarker(ll(t), { radius: 7, color: C.threat, weight: 2, fill: false }).addTo(g)
      .bindTooltip(`${t.name}: ${t.units.join(", ")}`, { className: "map-tip" });
    bounds.push(ll(t));
  }

  // route
  const route = b.waypoints.map(ll);
  L.polyline(route, { color: C.route, weight: 2.5 }).addTo(g);
  b.waypoints.forEach((p, i) => {
    const isHome = i === 0 && p.name === b.home.name;
    const isObj = p.name === "OBJECTIVE";
    if (i === b.waypoints.length - 1 && p.name === "RECOVERY" && b.waypoints[0].name === b.home.name) return;
    const cls = isHome ? "home" : isObj ? "obj" : "";
    L.marker(ll(p), { keyboard: false, icon: L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11],
      html: `<div class="wp-label ${cls}">${isHome ? "H" : p.n}</div>` }) }).addTo(g)
      .bindTooltip(`${p.n}. ${p.name} — ${ddm(p.lat, p.lon)}`, { className: "map-tip" });
    label(ll(p), p.name, "", [-16, -14]).addTo(g);
  });
  bounds.push(...route);
  if (b.waypoints[0].name !== b.home.name) {
    L.marker(ll(b.home), { icon: L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11],
      html: '<div class="wp-label home">H</div>' }) }).addTo(g).bindTooltip(b.home.name, { className: "map-tip" });
    bounds.push(ll(b.home));
  }

  state.bounds = L.latLngBounds(bounds);
  fitMap();
}

// Fit after layout settles (web fonts and the busy fade can change the container size).
function fitMap() {
  if (!state.map || !state.bounds) return;
  requestAnimationFrame(() => {
    state.map.invalidateSize();
    state.map.fitBounds(state.bounds, { padding: [30, 30], maxZoom: 11, animate: false });
  });
}

// ---------- boot ----------
async function boot() {
  initMap();
  const o = await api("/api/options");
  state.options = o;
  fillSelect(el("aircraft"), o.aircraft, aircraftLabel, (a) => a.id);
  fillSelect(el("terrain"), o.terrains);
  fillSelect(el("mission_type"), o.mission_types);
  fillSelect(el("start"), o.starts);
  fillSelect(el("weather"), o.weather);
  fillSelect(el("enemy_skill"), o.skills);
  fillSelect(el("hour"), [...Array(24).keys()], (h) => `${String(h).padStart(2, "0")}00`, String);
  setupPins();

  const last = o.last && o.last.values ? o.last : null;
  if (last) {
    await setValues(last.values);
    state.locks = new Set((last.locks || []).filter((f) => FIELDS.includes(f)));
  } else {
    await refreshAirports();
  }
  renderPins();

  $("btn-generate").addEventListener("click", () => generate(false));
  $("btn-apply").addEventListener("click", () => generate(true));
  $("btn-save").addEventListener("click", save);
  $("btn-unlock").addEventListener("click", () => { state.locks.clear(); renderPins(); setStatus("All fields unpinned."); });

  setInterval(() => api("/api/ping", {}).catch(() => setStatus("Mission Builder has stopped. Relaunch it to continue.", "err")), 15000);
  api("/api/ping", {}).catch(() => {});
  await generate(false);
}

boot().catch((e) => setStatus(e.message, "err"));
