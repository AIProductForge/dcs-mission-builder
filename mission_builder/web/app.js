"use strict";

const FIELDS = ["aircraft", "terrain", "airport", "mission_type", "complexity", "loadout", "start", "flight_size",
                "date", "hour", "weather", "enemy_skill", "awacs", "tanker"];
const PIN_SVG = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M10.5 1.5 14.5 5.5 12.6 6.1 9.9 8.8 10.2 12 8.9 13.3 6.2 10.6 2.6 14.2 1.8 13.4 5.4 9.8 2.7 7.1 4 5.8 7.2 6.1 9.9 3.4z"/></svg>';

const $ = (id) => document.getElementById(id);
const el = (id) => $("f-" + id);
const state = { options: null, campaigns: {}, locks: new Set(), seed: 0, map: null, layer: null, busy: false };

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
  v.complexity = Number(v.complexity);
  v.seed = state.seed;
  return v;
}

async function refreshAirports(keep) {
  const q = new URLSearchParams({ terrain: el("terrain").value, aircraft: el("aircraft").value,
                                  scenario: (state.campaigns[el("terrain").value] || {}).scenario || "" });
  const list = await api(`/api/airports?${q}`);
  fillSelect(el("airport"), list);
  if (keep && list.includes(keep)) el("airport").value = keep;
  return list.includes(keep);
}

// ---------- campaign ----------
function renderCampaign() {
  const c = state.campaigns[el("terrain").value];
  $("campaign").hidden = !c;
  if (!c) return;
  text("c-name", c.scenario);
  $("c-fields").innerHTML = c.won
    ? "<b>Campaign won.</b> Every enemy airfield is in friendly hands. Reset the map to fight again."
    : `Friendly airfields <b>${c.blue_fields.length}</b> · enemy <b>${c.red_fields.length}</b> · ` +
      `enemy sites left <b>${c.sites_alive}</b> of ${c.sites_total}`;
  const zones = $("c-zones");
  zones.innerHTML = "";
  for (const z of c.zones) {
    const li = document.createElement("li");
    li.className = c.front.includes(z.field) ? "front" : "";
    // Progress toward capture: the slower of the two conditions (all sites, SAM/radar sites).
    const parts = [z.needed ? z.destroyed / z.needed : 1, z.ad_needed ? z.ad_destroyed / z.ad_needed : 1];
    const pct = Math.min(100, 100 * Math.min(...parts));
    li.title = `${z.field}: ${z.destroyed} of ${z.total} enemy sites destroyed; ${z.needed} needed to capture` +
               (z.ad_needed ? `, including ${z.ad_needed} of its ${z.ad_total} SAM and radar sites (${z.ad_destroyed} so far).` : ".");
    li.innerHTML = `<span class="name"></span><span class="count">${z.destroyed}/${z.needed}` +
                   (z.ad_needed ? ` · SAM/radar ${z.ad_destroyed}/${z.ad_needed}` : "") + `</span>` +
                   `<span class="bar"><span style="width:${pct}%"></span></span>`;
    li.querySelector(".name").textContent = z.field;
    zones.appendChild(li);
  }
  const events = $("c-events");
  events.innerHTML = "";
  const recent = [...c.events].reverse().slice(0, 5);
  if (!recent.length) events.innerHTML = "<li>No flights reported yet. Save a mission and fly it.</li>";
  for (const e of recent) {
    const li = document.createElement("li");
    li.innerHTML = "<time></time><span></span>";
    li.querySelector("time").textContent = `${e.when.replace("T", " ").slice(0, 16)} · ${e.mission}, ${e.aircraft}`;
    li.querySelector("span").textContent = e.text;
    events.appendChild(li);
  }
  $("c-pending").textContent = c.pending.length
    ? `${c.pending.length} saved mission${c.pending.length > 1 ? "s" : ""} waiting to be flown. Results are read from dcs.log when the flight ends.`
    : "";
}

function takeCampaigns(campaigns) {
  // Tell the user when a flight's results have just been applied.
  for (const [t, c] of Object.entries(campaigns)) {
    const before = state.campaigns[t];
    if (before && c.events.length && c.events.length !== before.events.length) {
      setStatus(`${t}: ${c.events[c.events.length - 1].text}`, "ok");
    }
  }
  state.campaigns = campaigns;
  renderCampaign();
}

async function checkResults(force) {
  try {
    const res = await api("/api/campaign/check", { force });
    if (!res.changes.length) setStatus(force ? "Nothing logged for the current flight yet." : "No finished flights found in dcs.log.");
    takeCampaigns(res.campaigns);
    if (res.changes.length) await refreshAirports(el("airport").value);
  } catch (e) { setStatus(e.message, "err"); }
}

async function resetCampaign() {
  const t = el("terrain").value;
  if (!confirm(`Reset the ${t} campaign to its starting front? Destroyed sites and captured airfields are lost.`)) return;
  try {
    takeCampaigns((await api("/api/campaign/reset", { theatre: t })).campaigns);
    await refreshAirports(el("airport").value);
    setStatus(`${t} campaign reset.`, "ok");
  } catch (e) { setStatus(e.message, "err"); }
}

// Helicopters fly Free flight, Strike and CAS only.
function refreshMissionTypes(keep) {
  const heli = (state.options.aircraft.find((a) => a.id === el("aircraft").value) || {}).helicopter;
  const list = heli ? state.options.helicopter_missions : state.options.mission_types;
  fillSelect(el("mission_type"), list);
  if (keep && list.includes(keep)) el("mission_type").value = keep;
  return list.includes(keep);
}

function showComplexity() {
  const v = Number(el("complexity").value);
  $("complexity-out").textContent = `${v} · ${state.options.complexity_names[v]}`;
}

// Loadouts for the selected aircraft, the ones suited to the selected mission listed first.
async function refreshLoadouts(keep) {
  const aircraft = el("aircraft").value;
  if (state.loadoutsFor !== aircraft) {
    state.loadouts = await api(`/api/loadouts?aircraft=${encodeURIComponent(aircraft)}`);
    state.loadoutsFor = aircraft;
  }
  const select = el("loadout"), mission = el("mission_type").value;
  select.innerHTML = "";
  if (!state.loadouts.length) {
    const o = document.createElement("option");
    o.value = "";
    o.textContent = "Guns only (none stored)";
    select.appendChild(o);
    return;
  }
  const groups = [[`Suited to ${mission}`, state.loadouts.filter((l) => l.suits.includes(mission))],
                  ["Other", state.loadouts.filter((l) => !l.suits.includes(mission))]];
  for (const [title, list] of groups) {
    if (!list.length) continue;
    const g = document.createElement("optgroup");
    g.label = title;
    for (const l of list) {
      const o = document.createElement("option");
      o.value = l.name;
      o.textContent = loadoutLabel(l.name);
      g.appendChild(o);
    }
    select.appendChild(g);
  }
  if (keep && state.loadouts.some((l) => l.name === keep)) select.value = keep;
}

function loadoutLabel(name) { return name.replace(/^Liberation\s+/i, ""); }

async function setValues(v) {
  el("aircraft").value = v.aircraft;
  el("terrain").value = v.terrain;
  renderCampaign();
  await refreshAirports(v.airport);
  refreshMissionTypes(v.mission_type);
  el("complexity").value = String(v.complexity);
  showComplexity();
  for (const f of ["start", "weather", "enemy_skill"]) el(f).value = v[f];
  await refreshLoadouts(v.loadout);
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
      if (f === "terrain") renderCampaign();
      if (["terrain", "aircraft"].includes(f)) {
        // Home field must be one the player holds: drop the pin if it no longer is.
        if (!(await refreshAirports(el("airport").value))) state.locks.delete("airport");
      }
      if (f === "aircraft" || f === "mission_type") {
        const keep = el("loadout").value;
        await refreshLoadouts(keep);
        // A different aircraft rarely has the same loadout name: let it be picked again.
        if (f === "aircraft" && el("loadout").value !== keep) state.locks.delete("loadout");
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
    setStatus(`Saved ${res.path}` + (res.sortie ? ". Fly it and the campaign updates when the flight ends." : ""), "ok");
    takeCampaigns(await api("/api/campaign"));
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
  text("b-scenario", b.scenario);
  text("b-complexity", b.complexity.toUpperCase());
  text("b-execution", b.execution.join(" "));
  $("b-execution-wrap").hidden = !b.execution.length;
  const events = $("b-triggers");
  events.innerHTML = "";
  for (const t of b.triggers) {
    const li = document.createElement("li");
    li.textContent = t;
    events.appendChild(li);
  }
  $("b-triggers-wrap").hidden = !b.triggers.length;

  const enemy = b.situation.filter((s) => !s.startsWith("Friendly"));
  text("b-enemy", enemy.join(" ") || "None reported.");
  text("b-friendly", b.situation.filter((s) => s.startsWith("Friendly")).join(" "));
  const w = b.weather;
  text("b-weather", `${w.summary}${w.cloud_base_ft ? `, cloud base ${fmtNum(w.cloud_base_ft)} ft` : ""}. ` +
                    `Visibility ${w.visibility}. Surface temperature ${w.temp_c} °C.`);
  text("b-mission", b.mission_statement);
  text("b-loadout", b.loadout === "Guns only" ? "Guns only (no stored loadout for this aircraft)."
                                              : `${loadoutLabel(b.loadout)} configuration, per aircraft:`);
  const stores = $("b-stores");
  stores.innerHTML = "";
  for (const item of b.stores || []) {
    const li = document.createElement("li");
    li.textContent = item;
    stores.appendChild(li);
  }

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

  // friendly AI flights and troops
  for (const f of b.friendly_air || []) {
    if (f.route && f.route.length) {
      const pts = f.route.map(ll);
      L.polyline(pts, { color: C.support, weight: 2.5, dashArray: "2 6" }).addTo(g)
        .bindTooltip(f.name, { className: "map-tip", sticky: true });
      bounds.push(...pts);
    } else if (f.lat !== undefined) {
      L.circleMarker(ll(f), { radius: 7, color: C.support, weight: 2, fillColor: C.support, fillOpacity: 0.35 }).addTo(g)
        .bindTooltip(f.name, { className: "map-tip" });
      label(ll(f), "FRIENDLIES", "support", [-14, -8]).addTo(g);
      bounds.push(ll(f));
    }
  }

  // threats
  for (const t of b.threats) {
    if (t.range_nm) {
      L.circle(ll(t), { radius: t.range_nm * 1852, color: C.threat, weight: 1.5, fillColor: C.threat, fillOpacity: 0.12 }).addTo(g)
        .bindTooltip(`${t.name}: ${t.range_nm} nm ring`, { className: "map-tip", sticky: true });
      label(ll(t), t.name, "threat", [-10, -6]).addTo(g);
      bounds.push(ll(t));
    } else if (t.kind === "EWR" || t.name.includes("alert")) {
      // Point threats without a ring: radars and fighters on alert at their field.
      L.marker(ll(t), { keyboard: false, icon: L.divIcon({ className: "", iconAnchor: [7, 7],
        html: `<svg width="14" height="14" viewBox="0 0 14 14"><rect x="1" y="1" width="12" height="12" fill="none" stroke="${C.threat}" stroke-width="2"/></svg>` }) })
        .addTo(g).bindTooltip(`${t.name}: ${t.note}`, { className: "map-tip" });
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

  // airfields and who holds them
  for (const f of b.front || []) {
    L.marker(ll(f), { keyboard: false, icon: L.divIcon({ className: "", iconSize: [12, 12], iconAnchor: [6, 6],
      html: `<div class="field-marker ${f.side}"></div>` }) }).addTo(g)
      .bindTooltip(`${f.name}: ${f.side === "blue" ? "friendly" : "enemy"}`, { className: "map-tip" });
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
  state.campaigns = o.campaigns || {};
  el("complexity").addEventListener("input", showComplexity);
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
    renderCampaign();
    await refreshAirports();
    refreshMissionTypes();
    el("complexity").value = "3";
    showComplexity();
    await refreshLoadouts();
  }
  renderPins();

  $("btn-generate").addEventListener("click", () => generate(false));
  $("btn-apply").addEventListener("click", () => generate(true));
  $("btn-save").addEventListener("click", save);
  $("btn-check").addEventListener("click", () => checkResults(false));
  $("btn-force").addEventListener("click", () => checkResults(true));
  $("btn-reset").addEventListener("click", resetCampaign);
  $("btn-unlock").addEventListener("click", () => { state.locks.clear(); renderPins(); setStatus("All fields unpinned."); });

  setInterval(() => api("/api/ping", {}).catch(() => setStatus("Mission Builder has stopped. Relaunch it to continue.", "err")), 15000);
  api("/api/ping", {}).catch(() => {});
  // Pick up flights as they finish (the server reads dcs.log on each call).
  setInterval(() => api("/api/campaign").then(takeCampaigns).catch(() => {}), 20000);
  await generate(false);
}

boot().catch((e) => setStatus(e.message, "err"));
