// Campaign map: the air war moving on a map, the ATO, and the clock. Server state comes
// from /api/war every couple of seconds; between polls flights are moved locally along
// their routes from the clock, so they glide instead of jumping.
"use strict";

const $ = (id) => document.getElementById(id);
const S = { theatre: null, snap: null, at: 0, selected: null, map: null, layers: {}, markers: new Map(), busy: false };
const COLORS = { blue: "#1f4e8c", red: "#b3261e", dead: "#8a8a80" };

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function setStatus(text, kind = "") {
  $("status").textContent = text;
  $("status").className = "status " + kind;
}

// ---------- clock ----------
function running(s = S.snap) { return s && !s.paused && s.mode === "running"; }

function clockNow() {
  if (!S.snap) return 0;
  return S.snap.clock + (running() ? (performance.now() - S.at) / 1000 * S.snap.rate : 0);
}

function label(t) {
  const start = new Date(S.snap.start);
  const when = new Date(start.getTime() + t * 1000);
  const day = Math.floor((Date.UTC(when.getFullYear(), when.getMonth(), when.getDate()) -
                          Date.UTC(start.getFullYear(), start.getMonth(), start.getDate())) / 86400000) + 1;
  return `Day ${day} ${String(when.getHours()).padStart(2, "0")}:${String(when.getMinutes()).padStart(2, "0")}`;
}

function hhmm(t) { return label(t).split(" ").pop(); }

// ---------- geometry ----------
function posAt(f, t) {
  const r = f.route;
  if (t <= r[0].t) return { ll: r[0].ll, hdg: bearing(r[0].ll, r[1].ll), leg: 0 };
  for (let i = 1; i < r.length; i++) {
    if (t <= r[i].t) {
      const k = (t - r[i - 1].t) / Math.max(1e-6, r[i].t - r[i - 1].t);
      const a = r[i - 1].ll, b = r[i].ll;
      return { ll: [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k], hdg: bearing(a, b), leg: i };
    }
  }
  return { ll: r[r.length - 1].ll, hdg: 0, leg: r.length - 1 };
}

function bearing(a, b) {
  const toR = Math.PI / 180, y = Math.sin((b[1] - a[1]) * toR) * Math.cos(b[0] * toR);
  const x = Math.cos(a[0] * toR) * Math.sin(b[0] * toR) - Math.sin(a[0] * toR) * Math.cos(b[0] * toR) * Math.cos((b[1] - a[1]) * toR);
  return (Math.atan2(y, x) / toR + 360) % 360;
}

function inAir(f, t) {
  if (f.status === "cancelled" || f.status === "planned" && t < f.takeoff) return false;
  const end = f.end ?? f.land;
  return t >= f.takeoff && t < end;
}

// ---------- map ----------
function initMap() {
  if (typeof L === "undefined") { $("map").textContent = "Map unavailable offline."; return; }
  S.map = L.map("map", { zoomControl: true, attributionControl: true });
  L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 13, attribution: 'Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
  }).addTo(S.map);
  for (const k of ["sites", "fields", "route", "flights"]) S.layers[k] = L.layerGroup().addTo(S.map);
}

function planeIcon(f, hdg, selected) {
  const c = COLORS[f.side];
  const tag = `${f.callsign}${f.player ? " ★" : ""}`;
  return L.divIcon({ className: "", iconSize: [16, 16], iconAnchor: [8, 8], html:
    `<div class="flight-icon${selected ? " sel" : ""}"><svg width="16" height="16" viewBox="-8 -8 16 16" style="transform:rotate(${hdg}deg)">` +
    `<path d="M0 -7 L5 6 L0 3 L-5 6 Z" fill="${c}" stroke="#fff" stroke-width="1"/></svg>` +
    `<span class="flight-tag">${tag}</span></div>` });
}

function drawStatic() {
  if (!S.map) return;
  const s = S.snap;
  S.layers.sites.clearLayers();
  S.layers.fields.clearLayers();
  for (const site of s.sites) {
    L.circleMarker(site.ll, { radius: 3, weight: site.alive ? 0 : 1.2, color: site.alive ? COLORS[site.side] : COLORS.dead,
                              fillColor: COLORS[site.side], fillOpacity: site.alive ? 0.8 : 0 })
      .bindTooltip(`${site.side === "blue" ? "Friendly" : "Enemy"} ${site.role} · ${site.zone}${site.alive ? "" : " (destroyed)"}`,
                   { className: "map-tip" })
      .addTo(S.layers.sites);
  }
  for (const f of s.fields) {
    L.marker(f.ll, { keyboard: false, icon: L.divIcon({ className: "", iconSize: [12, 12], iconAnchor: [6, 6],
      html: `<div class="field-marker ${f.side}"></div>` }) })
      .bindTooltip(`${f.name}: ${f.side === "blue" ? "friendly" : "enemy"}`, { className: "map-tip", permanent: false })
      .addTo(S.layers.fields);
  }
  if (!S.fitted) {
    S.map.fitBounds(L.latLngBounds(s.fields.map((f) => f.ll)), { padding: [40, 40] });
    S.fitted = true;
  }
}

function drawFlights() {
  if (!S.map || !S.snap) return;
  const t = clockNow();
  const seen = new Set();
  for (const f of S.snap.flights) {
    if (!inAir(f, t)) continue;
    seen.add(f.id);
    const p = posAt(f, t);
    const sel = S.selected === f.id;
    let m = S.markers.get(f.id);
    if (!m) {
      m = L.marker(p.ll, { keyboard: false, zIndexOffset: 1000 }).addTo(S.layers.flights);
      m.on("click", () => select(f.id));
      S.markers.set(f.id, m);
    }
    m.setLatLng(p.ll);
    const key = `${Math.round(p.hdg / 5)}|${sel}|${f.player}`;
    if (m._key !== key) { m.setIcon(planeIcon(f, p.hdg, sel)); m._key = key; }
  }
  for (const [id, m] of S.markers) {
    if (!seen.has(id)) { S.layers.flights.removeLayer(m); S.markers.delete(id); }
  }
  $("clock").textContent = label(t);
}

function drawRoute() {
  S.layers.route.clearLayers();
  const f = S.snap && S.snap.flights.find((x) => x.id === S.selected);
  if (!f || !S.map) return;
  const c = COLORS[f.side];
  L.polyline(f.route.map((r) => r.ll), { color: c, weight: 2, dashArray: "6 5", opacity: 0.9 }).addTo(S.layers.route);
  const tgt = f.route.find((r) => r.name === "TARGET" || r.name === "STATION");
  if (tgt) L.circleMarker(tgt.ll, { radius: 8, color: c, weight: 2, fill: false }).addTo(S.layers.route)
    .bindTooltip(f.target_name || "CAP station", { className: "map-tip" });
}

// ---------- panel ----------
function flightTask(f) {
  return `${f.mission}${f.target_name ? " · " + f.target_name : f.mission === "CAP" ? " · front" : ""}`;
}

function flightStatus(f, t) {
  if (f.player) return ["player", "You"];
  if (f.flown) return ["", "Flown"];
  if (f.status === "planned" && t < f.takeoff) return ["", "Planned"];
  if (f.status === "lost") return ["lost", "Lost"];
  if (f.status === "cancelled") return ["lost", "Scrubbed"];
  if (inAir(f, t)) return ["airborne", "Airborne"];
  return ["", f.status === "landed" ? "Landed" : f.status];
}

function renderAto() {
  const t = clockNow(), tb = $("ato").querySelector("tbody");
  const showDone = $("show-done").checked;
  const list = S.snap.flights
    .filter((f) => $(`show-${f.side}`).checked)
    .filter((f) => showDone || ["planned", "airborne"].includes(f.status))
    .sort((a, b) => a.takeoff - b.takeoff);
  tb.innerHTML = "";
  for (const f of list) {
    const [cls, st] = flightStatus(f, t);
    const tr = document.createElement("tr");
    tr.className = (S.selected === f.id ? "sel " : "") + (["planned", "airborne"].includes(f.status) ? "" : "done");
    tr.innerHTML = `<td>${hhmm(f.takeoff)}</td><td><span class="side ${f.side}"></span><span class="n"></span></td>` +
                   `<td class="tk"></td><td><span class="st ${cls}">${st}</span></td>`;
    tr.querySelector(".n").textContent = `${f.callsign} · ${f.count}× ${f.type}`;
    tr.querySelector(".tk").textContent = flightTask(f);
    tr.addEventListener("click", () => select(f.id, true));
    tb.appendChild(tr);
  }
  if (!list.length) tb.innerHTML = `<tr><td colspan="4">Nothing planned yet. Run the clock: both sides plan every hour.</td></tr>`;
}

function renderSelected() {
  const f = S.snap.flights.find((x) => x.id === S.selected);
  $("selected").hidden = !f;
  if (!f) return;
  const t = clockNow();
  $("sel-title").textContent = `${f.callsign} · ${f.count}× ${f.type}`;
  const lines = [`${f.side === "blue" ? "Friendly" : "Enemy"} ${flightTask(f)}`, `From ${f.base}. Takeoff ${label(f.takeoff)}` +
                 (f.tot ? `, on target ${hhmm(f.tot)}` : f.station ? `, on station ${hhmm(f.station)}` : "") +
                 `, back ${hhmm(f.land)}.`];
  if (f.result) lines.push(`Result: ${f.result}.`);
  if (f.losses) lines.push(`Losses: ${f.losses}.`);
  $("sel-text").textContent = lines.join(" ");
  const flyable = f.side === "blue" && f.status === "planned" && t < f.takeoff && S.snap.mode === "running";
  $("btn-fly").hidden = !flyable;
}

function renderZones() {
  const ul = $("zones");
  ul.innerHTML = "";
  for (const side of ["red", "blue"]) {
    const front = S.snap.front[side === "red" ? "blue" : "red"];
    for (const z of S.snap.zones[side]) {
      if (!front.includes(z.field)) continue;
      const parts = [z.needed ? z.destroyed / z.needed : 1, z.ad_needed ? z.ad_destroyed / z.ad_needed : 1];
      const li = document.createElement("li");
      li.className = "front " + (side === "blue" ? "blueside" : "");
      li.title = `${z.field} (${side === "blue" ? "friendly" : "enemy"}): ${z.destroyed} of ${z.total} sites destroyed; ` +
                 `falls at ${z.needed}, including ${z.ad_needed} SAM/radar (${z.ad_destroyed} so far).`;
      li.innerHTML = `<span class="name"></span><span class="count">${z.destroyed}/${z.needed} · SAM/radar ${z.ad_destroyed}/${z.ad_needed}</span>` +
                     `<span class="bar"><span style="width:${Math.min(100, 100 * Math.min(...parts))}%"></span></span>`;
      li.querySelector(".name").textContent = `${side === "blue" ? "Ours: " : ""}${z.field}`;
      ul.appendChild(li);
    }
  }
}

function renderSquadrons() {
  const tb = $("sqn").querySelector("tbody");
  tb.innerHTML = "";
  for (const sq of S.snap.squadrons) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="side ${sq.side}"></span><span class="n"></span></td><td class="b"></td><td>${sq.available}/${sq.total}</td>`;
    tr.querySelector(".n").textContent = sq.type;
    tr.querySelector(".b").textContent = sq.total ? sq.base : "disbanded";
    tb.appendChild(tr);
  }
}

function renderLog() {
  const ul = $("log");
  ul.innerHTML = "";
  for (const e of [...S.snap.events].reverse().slice(0, 25)) {
    const li = document.createElement("li");
    li.textContent = e.text;
    if (e.side) li.className = e.side;
    ul.appendChild(li);
  }
}

function renderClock() {
  const s = S.snap;
  $("btn-run").textContent = s.paused ? "Run" : "Pause";
  $("btn-run").classList.toggle("on", !s.paused);
  $("btn-run").disabled = s.mode !== "running";
  for (const b of document.querySelectorAll(".rate")) b.classList.toggle("on", Number(b.dataset.rate) === s.rate);
  $("clock-state").textContent = s.mode === "in_dcs" ? "in DCS" : s.paused ? "paused" : `running ${s.rate}×`;
  $("clock-state").classList.toggle("live", running());
  $("scenario").textContent = `${s.scenario} · losses: friendly ${s.stats.blue_lost}, enemy ${s.stats.red_lost}` +
                              (s.won ? " · CAMPAIGN WON" : s.lost ? " · CAMPAIGN LOST" : "");
  const pf = s.flights.find((f) => f.id === s.player_flight);
  $("dcs-banner").hidden = s.mode !== "in_dcs";
  if (pf) $("dcs-text").textContent = `${pf.callsign} (${pf.count}× ${pf.type}, ${flightTask(pf)}) is saved as a mission. ` +
    "Fly it in DCS: the war stays paused and resumes from takeoff once the results are read from dcs.log.";
}

function render(full) {
  renderClock();
  renderAto();
  renderSelected();
  renderZones();
  renderSquadrons();
  renderLog();
  if (full) drawStatic();
  drawRoute();
  drawFlights();
}

function select(id, pan) {
  S.selected = S.selected === id ? null : id;
  render(false);
  const f = S.snap.flights.find((x) => x.id === S.selected);
  if (f && pan && S.map) S.map.panTo(inAir(f, clockNow()) ? posAt(f, clockNow()).ll : f.route[0].ll);
}

// ---------- server ----------
async function refresh(full = false) {
  if (!S.theatre) return;
  try {
    const before = S.snap;
    const snap = await api(`/api/war?theatre=${encodeURIComponent(S.theatre)}`);
    take(snap, full || !before || before.sites.filter((x) => x.alive).length !== snap.sites.filter((x) => x.alive).length ||
         JSON.stringify(before.fields) !== JSON.stringify(snap.fields));
  } catch (e) { setStatus(e.message, "err"); }
}

function take(snap, full) {
  S.snap = snap;
  S.at = performance.now();
  render(full);
}

async function clockCmd(body) {
  try { take(await api("/api/war/clock", { theatre: S.theatre, ...body }), false); }
  catch (e) { setStatus(e.message, "err"); }
}

async function fly() {
  const f = S.snap.flights.find((x) => x.id === S.selected);
  if (!f || S.busy) return;
  S.busy = true;
  setStatus(`Building ${f.callsign}'s mission…`);
  try {
    const res = await api("/api/war/fly", { theatre: S.theatre, flight: f.id });
    take(res.war, true);
    setStatus(`Saved ${res.path}. It's in DCS under Mission → My Missions → Generated.`, "ok");
  } catch (e) { setStatus(e.message, "err"); }
  S.busy = false;
}

async function boot() {
  initMap();
  const o = await api("/api/options");
  const theatres = Object.keys(o.campaigns);
  const sel = $("theatre");
  for (const t of theatres) { const op = document.createElement("option"); op.value = op.textContent = t; sel.appendChild(op); }
  let want = new URLSearchParams(location.search).get("theatre");
  try { want = want || localStorage.getItem("war-theatre"); } catch (e) { /* storage blocked */ }
  S.theatre = theatres.includes(want) ? want : theatres[0];
  sel.value = S.theatre;
  sel.addEventListener("change", async () => {
    S.theatre = sel.value; S.fitted = false; S.selected = null;
    try { localStorage.setItem("war-theatre", S.theatre); } catch (e) { /* storage blocked */ }
    for (const m of S.markers.values()) S.layers.flights.removeLayer(m);
    S.markers.clear();
    await refresh(true);
  });
  $("btn-run").addEventListener("click", () => clockCmd({ paused: !S.snap.paused }));
  for (const b of document.querySelectorAll(".rate")) b.addEventListener("click", () => clockCmd({ rate: Number(b.dataset.rate) }));
  for (const id of ["show-blue", "show-red", "show-done"]) $(id).addEventListener("change", renderAto);
  $("btn-fly").addEventListener("click", fly);
  $("btn-release").addEventListener("click", async () => {
    try { take(await api("/api/war/release", { theatre: S.theatre }), false); setStatus("Flight handed back to the AI."); }
    catch (e) { setStatus(e.message, "err"); }
  });
  $("btn-apply").addEventListener("click", async () => {
    try {
      const res = await api("/api/campaign/check", { force: true });
      setStatus(res.changes.length ? res.changes.join(" ") : "Nothing logged for this flight yet.", res.changes.length ? "ok" : "");
      await refresh(true);
    } catch (e) { setStatus(e.message, "err"); }
  });
  $("btn-reset").addEventListener("click", async () => {
    if (!confirm(`Reset the ${S.theatre} campaign? The war starts again from its first day.`)) return;
    try { await api("/api/campaign/reset", { theatre: S.theatre }); S.selected = null; await refresh(true); setStatus("Campaign reset.", "ok"); }
    catch (e) { setStatus(e.message, "err"); }
  });
  await refresh(true);
  setInterval(() => refresh(false), 2000);
  setInterval(() => api("/api/ping", {}).catch(() => setStatus("Mission Builder has stopped. Relaunch it to continue.", "err")), 15000);
  api("/api/ping", {}).catch(() => {});
  setInterval(() => { if (running()) { drawFlights(); } }, 100);
  setInterval(() => { if (running()) { renderAto(); renderSelected(); } }, 1000);
}

boot().catch((e) => setStatus(e.message, "err"));
