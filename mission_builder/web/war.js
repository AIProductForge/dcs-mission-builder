// Campaign map: the air war moving on a map, the ATO, and the clock. Server state comes
// from /api/war every couple of seconds; between polls flights are moved locally along
// their routes from the clock, so they glide instead of jumping.
"use strict";

const $ = (id) => document.getElementById(id);
const S = { theatre: null, snap: null, at: 0, selected: null, map: null, layers: {}, markers: new Map(), busy: false };
const COLORS = { blue: "#1f4e8c", red: "#b3261e", dead: "#8a8a80" };
// Coverage ring radii: the ranges the campaign's odds model uses (war._threat), in metres.
const RINGS = { sam: 40000, shorad: 8000, ewr: 80000 };
const RING_STYLE = { sam: { weight: 1.2, fillOpacity: 0.05 }, shorad: { weight: 1, dashArray: "4 4", fillOpacity: 0.07 },
                     ewr: { weight: 1, dashArray: "2 6", fillOpacity: 0 } };
const ROLE = { sam: "SAM site", ewr: "Early-warning radar", shorad: "Short-range air defence", armor: "Armour",
               missile: "Missile battery", structure: "Installation", farp: "Helicopter base" };

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
  S.bases = {
    topo: L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      maxZoom: 13, attribution: 'Map data © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | style © <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)' }),
    satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 17, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }),
    plain: L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png", {
      maxZoom: 17, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>' }),
  };
  S.hillshade = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 15, opacity: 0.45, attribution: "Hillshade © Esri" });
  S.map.attributionControl.addAttribution("Elevation: Copernicus DEM GLO-90 via Open-Meteo");
  // Leaflet measures its container once; re-measure when the layout changes it (fonts, panels, resize).
  new ResizeObserver(() => S.map.invalidateSize()).observe($("map"));
  S.ringRenderer = L.canvas({ padding: 0.5 });  // hundreds of circles: canvas keeps panning smooth
  for (const k of ["rings", "sites", "fields", "route", "flights", "plan"]) S.layers[k] = L.layerGroup().addTo(S.map);
  // With the frag form open on a CAP, a click on the map sets the station.
  S.map.on("click", (e) => {
    if (!$("plan").open || $("p-mission").value !== "CAP") return;
    S.capPoint = [e.latlng.lat, e.latlng.lng];
    $("p-station").value = "point";
    drawCapPoint();
    planNote();
  });
}

function planeIcon(f, hdg, selected) {
  const c = COLORS[f.side];
  const tag = `${f.callsign}${f.player ? " ★" : ""}`;
  return L.divIcon({ className: "", iconSize: [16, 16], iconAnchor: [8, 8], html:
    `<div class="flight-icon${selected ? " sel" : ""}"><svg width="16" height="16" viewBox="-8 -8 16 16" style="transform:rotate(${hdg}deg)">` +
    `<path d="M0 -7 L5 6 L0 3 L-5 6 Z" fill="${c}" stroke="#fff" stroke-width="1"/></svg>` +
    `<span class="flight-tag">${tag}</span></div>` });
}

// ---------- base maps and terrain ----------
function setupBaseMaps() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("war-map") || "{}"); } catch (e) { /* storage blocked */ }
  const pick = () => {
    const base = document.querySelector('input[name="base"]:checked').value;
    for (const [k, layer] of Object.entries(S.bases)) {
      if (k === base) { if (!S.map.hasLayer(layer)) layer.addTo(S.map); layer.bringToBack(); }
      else if (S.map.hasLayer(layer)) S.map.removeLayer(layer);
    }
    if ($("hillshade").checked) { if (!S.map.hasLayer(S.hillshade)) S.hillshade.addTo(S.map); }
    else if (S.map.hasLayer(S.hillshade)) S.map.removeLayer(S.hillshade);
    try { localStorage.setItem("war-map", JSON.stringify({ base, hillshade: $("hillshade").checked })); } catch (e) { /* blocked */ }
  };
  if (saved.base) { const r = document.querySelector(`input[name="base"][value="${saved.base}"]`); if (r) r.checked = true; }
  if (saved.hillshade) $("hillshade").checked = true;
  for (const el of document.querySelectorAll('input[name="base"], #hillshade')) el.addEventListener("change", pick);
  pick();
}

// Ground height along a flight's route (Copernicus 90 m DEM via Open-Meteo, up to 100 points per
// request) against its altitude. Altitudes at or below 500 m are above ground level, as in the mission.
const AGL_BELOW = 500;
S.profiles = {};

function routeSamples(f, n = 100) {
  // Fly the route once: a CAP's orbit legs repeat the same two points, so stop after the first lap.
  const pts = [];
  const seen = new Set();
  for (const r of f.route) {
    const key = r.ll.join(",") + r.name;
    if (r.name === "CAP" || r.name === "STATION") { if (seen.has(r.name)) continue; seen.add(r.name); }
    pts.push(r);
    if (key && pts.length > 1 && r.name === "LAND") break;
  }
  const legs = [];
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const d = S.map.distance(pts[i - 1].ll, pts[i].ll);
    legs.push({ a: pts[i - 1], b: pts[i], d, start: total });
    total += d;
  }
  const out = [];
  for (let k = 0; k < n; k++) {
    const dist = total * k / (n - 1);
    const leg = legs.find((l) => dist <= l.start + l.d) || legs[legs.length - 1];
    const u = leg.d ? (dist - leg.start) / leg.d : 0;
    out.push({ dist, lat: leg.a.ll[0] + (leg.b.ll[0] - leg.a.ll[0]) * u, lon: leg.a.ll[1] + (leg.b.ll[1] - leg.a.ll[1]) * u,
               alt: leg.b.name === "LAND" ? null : leg.b.alt ?? leg.a.alt, wp: u < 0.01 ? leg.a.name : null });
  }
  out[out.length - 1].wp = pts[pts.length - 1].name;
  return out;
}

async function showProfile(f) {
  const box = $("sel-profile");
  if (!f || !f.route || f.route.length < 2 || !S.map) { box.hidden = true; return; }
  box.hidden = false;
  if (!S.profiles[f.id]) {
    $("profile-note").textContent = "loading terrain…";
    $("profile-note").className = "";
    const samples = routeSamples(f);
    const q = `latitude=${samples.map((p) => p.lat.toFixed(4)).join(",")}&longitude=${samples.map((p) => p.lon.toFixed(4)).join(",")}`;
    try {
      const r = await fetch(`https://api.open-meteo.com/v1/elevation?${q}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const elev = (await r.json()).elevation;
      samples.forEach((p, i) => { p.ground = Math.max(0, elev[i] ?? 0); });
      S.profiles[f.id] = samples;
    } catch (e) {
      $("profile-note").textContent = "terrain data unavailable (offline?)";
      return;
    }
  }
  if (S.selected === f.id) drawProfile(f, S.profiles[f.id]);
}

function drawProfile(f, samples) {
  const cv = $("profile-canvas"), g = cv.getContext("2d");
  const W = cv.width, H = cv.height, padL = 34, padB = 16, padT = 6;
  // The flight's height above sea level at each sample: low legs follow the ground, and it climbs
  // out over the first and descends over the last RAMP metres (left out of the clearance check).
  const RAMP = 25000, end = samples[samples.length - 1].dist;
  let cruise = samples.find((p) => p.alt !== null)?.alt ?? 0;
  for (const p of samples) {
    if (p.alt !== null) cruise = p.alt;
    const msl = cruise <= AGL_BELOW ? p.ground + cruise : cruise;
    const k = Math.min(1, p.dist / RAMP, (end - p.dist) / RAMP);
    p.ramp = k < 1;
    p.msl = p.ground + (msl - p.ground) * Math.max(0, k);
  }
  const top = Math.max(...samples.map((p) => Math.max(p.msl, p.ground))) * 1.1 + 50;
  const X = (d) => padL + (W - padL - 4) * d / samples[samples.length - 1].dist;
  const Y = (m) => H - padB - (H - padB - padT) * m / top;
  g.clearRect(0, 0, W, H);
  g.font = "10px IBM Plex Mono, monospace";
  g.fillStyle = "#9aa08d";
  g.strokeStyle = "#3a4034";
  for (const m of [0, top / 2, top]) {
    g.beginPath(); g.moveTo(padL, Y(m)); g.lineTo(W - 4, Y(m)); g.stroke();
    g.fillText(`${Math.round(m / 100) * 100}`, 2, Y(m) + 3);
  }
  // ground
  g.beginPath(); g.moveTo(X(0), Y(0));
  for (const p of samples) g.lineTo(X(p.dist), Y(p.ground));
  g.lineTo(X(samples[samples.length - 1].dist), Y(0)); g.closePath();
  g.fillStyle = "#6b5a3e"; g.fill();
  // flight path, red where it's close to the ground
  let lowest = Infinity, lowestAt = null;
  for (let i = 1; i < samples.length; i++) {
    const a = samples[i - 1], b = samples[i];
    const clear = Math.min(a.msl - a.ground, b.msl - b.ground);
    const counted = !a.ramp && !b.ramp;
    if (counted && clear < lowest) { lowest = clear; lowestAt = b; }
    g.strokeStyle = counted && clear < 100 ? "#f08a80" : "#5b8fd6";
    g.lineWidth = 2;
    g.beginPath(); g.moveTo(X(a.dist), Y(a.msl)); g.lineTo(X(b.dist), Y(b.msl)); g.stroke();
  }
  g.lineWidth = 1;
  g.fillStyle = "#d9dccf";
  for (const p of samples) {
    if (!p.wp) continue;
    g.fillRect(X(p.dist) - 0.5, H - padB, 1, 3);
    const label = p.wp === "TARGET" ? "TGT" : p.wp.slice(0, 4);
    g.fillText(label, Math.min(W - 26, Math.max(padL, X(p.dist) - 10)), H - 3);
  }
  const maxGround = Math.max(...samples.map((p) => p.ground));
  const km = Math.round(samples[samples.length - 1].dist / 1000);
  const note = $("profile-note");
  note.className = lowest < 100 ? "warn" : "";
  note.textContent = `${km} km · highest ground ${Math.round(maxGround)} m` +
    (Number.isFinite(lowest) ? ` · lowest clearance ${Math.max(0, Math.round(lowest))} m` : "");
}

function ringChoice() {
  const on = {};
  for (const box of document.querySelectorAll("[data-ring]")) on[box.dataset.ring] = box.checked;
  return on;
}

function drawRings() {
  if (!S.map || !S.snap) return;
  S.layers.rings.clearLayers();
  const on = ringChoice();
  for (const site of S.snap.sites) {
    if (!site.alive || !RINGS[site.role] || !on[site.side] || !on[site.role]) continue;
    L.circle(site.ll, { radius: RINGS[site.role], color: COLORS[site.side], fillColor: COLORS[site.side],
                        interactive: false, renderer: S.ringRenderer, ...RING_STYLE[site.role] })
      .addTo(S.layers.rings);
  }
}

function setupRings() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem("war-rings") || "null"); } catch (e) { /* storage blocked */ }
  for (const box of document.querySelectorAll("[data-ring]")) {
    if (saved && box.dataset.ring in saved) box.checked = saved[box.dataset.ring];
    box.addEventListener("change", () => {
      try { localStorage.setItem("war-rings", JSON.stringify(ringChoice())); } catch (e) { /* storage blocked */ }
      drawRings();
    });
  }
}

function drawStatic() {
  if (!S.map) return;
  const s = S.snap;
  drawRings();
  S.layers.sites.clearLayers();
  S.layers.fields.clearLayers();
  for (const site of s.sites) {
    L.circleMarker(site.ll, { radius: 3, weight: site.alive ? 0 : 1.2, color: site.alive ? COLORS[site.side] : COLORS.dead,
                              fillColor: COLORS[site.side], fillOpacity: site.alive ? 0.8 : 0 })
      .bindTooltip(`${site.side === "blue" ? "Friendly" : "Enemy"} ${ROLE[site.role] || site.role} · ${site.zone}` +
                   `${site.alive ? "" : " (destroyed)"}`, { className: "map-tip" })
      .on("click", () => pickTarget(site))
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
    if (f.scheduled || f.night) tr.querySelector(".tk").insertAdjacentHTML("beforeend",
      `<span class="tag">${[f.scheduled ? "YOURS" : "", f.night ? "NIGHT" : ""].filter(Boolean).join(" · ")}</span>`);
    tr.addEventListener("click", () => select(f.id, true));
    tb.appendChild(tr);
  }
  if (!list.length) tb.innerHTML = `<tr><td colspan="4">Nothing planned yet. Run the clock: both sides plan every hour.</td></tr>`;
}

function renderSelected() {
  const f = S.snap.flights.find((x) => x.id === S.selected);
  $("selected").hidden = !f;
  if (!f) { S.profileFor = null; return; }
  const t = clockNow();
  $("sel-title").textContent = `${f.callsign} · ${f.count}× ${f.type}`;
  const lines = [`${f.side === "blue" ? "Friendly" : "Enemy"} ${flightTask(f)}`, `From ${f.base}. Takeoff ${label(f.takeoff)}` +
                 (f.tot ? `, on target ${hhmm(f.tot)}` : f.station ? `, on station ${hhmm(f.station)}` : "") +
                 `, back ${hhmm(f.land)}.`];
  if (f.result) lines.push(`Result: ${f.result}.`);
  if (f.losses) lines.push(`Losses: ${f.losses}.`);
  $("sel-text").textContent = lines.join(" ");
  $("sel-stores").innerHTML = "";
  if (f.loadout) {
    $("sel-stores").innerHTML = "<b></b> ";
    $("sel-stores").querySelector("b").textContent = `Loadout ${f.loadout.replace(/^Liberation\s+/i, "")}:`;
    $("sel-stores").append(f.stores && f.stores.length ? `${f.stores.join(", ")} (each aircraft).` : "guns only.");
  }
  const open = f.side === "blue" && f.status === "planned" && t < f.takeoff && S.snap.mode === "running";
  // A friendly flight in the air can be joined now, where it is (not when it's about to land).
  const joinable = f.side === "blue" && f.status === "airborne" && !f.player && f.count > 0 && inAir(f, t) &&
                   t < f.land - 300 && S.snap.mode === "running";
  if (S.profileFor !== f.id) { S.profileFor = f.id; showProfile(f); }
  $("btn-fly").hidden = !(open || joinable) || S.snap.ai_only.includes(f.type);
  $("btn-fly").textContent = joinable ? "Jump in now (in the air)" : "Fly this flight (at takeoff)";
  $("btn-scrub").hidden = !open;
  if (open && S.snap.ai_only.includes(f.type)) $("sel-text").textContent += " AI only in DCS: fly an escort or SEAD flight alongside it.";
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
    li.className = [e.side || "", e.debrief ? "debrief" : ""].join(" ").trim();
    ul.appendChild(li);
  }
}

function renderClock() {
  const s = S.snap;
  $("btn-run").textContent = s.paused ? "Run" : "Pause";
  $("btn-run").classList.toggle("on", !s.paused);
  $("btn-run").disabled = s.mode !== "running";
  $("btn-jump").disabled = s.mode !== "running" || !!S.offline;
  for (const b of document.querySelectorAll(".rate")) b.classList.toggle("on", Number(b.dataset.rate) === s.rate);
  $("clock-state").textContent = S.offline ? "server stopped" : s.mode === "in_dcs" ? "in DCS" : s.paused ? "paused" : `running ${s.rate}×`;
  $("clock-state").classList.toggle("live", running());
  $("scenario").textContent = `${s.scenario} · losses: friendly ${s.stats.blue_lost}, enemy ${s.stats.red_lost}` +
                              (s.won ? " · CAMPAIGN WON" : s.lost ? " · CAMPAIGN LOST" : "");
  const pf = s.flights.find((f) => f.id === s.player_flight);
  $("dcs-banner").hidden = s.mode !== "in_dcs";
  if (pf) $("dcs-text").textContent = `${pf.callsign} (${pf.count}× ${pf.type}, ${flightTask(pf)}) is saved as a mission. ` +
    "Fly it in DCS: the war stays paused and resumes from takeoff once the results are read from dcs.log.";
}

// ---------- air commander ----------
const PROFILE = { standard: "", low: "low level", standoff: "stand-off", sof: "special forces" };

async function askCommander() {
  const order = $("c-order").value.trim();
  if (!order || S.asking) return;
  S.asking = true;
  $("btn-ask").disabled = true;
  const box = $("c-result");
  box.innerHTML = '<p class="campaign-line">The commander is planning. This usually takes 20 to 60 seconds…</p>';
  try {
    const plan = await api("/api/commander/plan", { theatre: S.theatre, order, site: S.pickedSite ? S.pickedSite.id : null });
    S.plan = plan;
    renderPlanResult(plan);
  } catch (e) {
    box.innerHTML = "";
    setStatus(e.message, "err");
  }
  S.asking = false;
  $("btn-ask").disabled = false;
}

function pct(x) { return x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`; }

function renderPlanResult(plan) {
  const box = $("c-result");
  box.innerHTML = "";
  const head = document.createElement("p");
  head.className = "c-head";
  head.innerHTML = "<b>Understood:</b> <span></span>";
  head.querySelector("span").textContent = plan.understanding + (plan.clarification ? ` ${plan.clarification}` : "");
  box.appendChild(head);
  plan.options.forEach((opt, i) => {
    const card = document.createElement("div");
    card.className = "coa";
    card.innerHTML = `<h3></h3><p class="odds"></p><p class="concept"></p><p class="risk"></p><ol></ol>`;
    card.querySelector("h3").textContent = `${i + 1}. ${opt.name}`;
    const odds = [];
    if (opt.objective !== null) odds.push(`Objective achieved ${pct(opt.objective)}`);
    if (opt.expected_losses !== null) odds.push(`expected losses ${opt.expected_losses} aircraft`);
    card.querySelector(".odds").textContent = odds.join(" · ") || "No odds for this option";
    card.querySelector(".concept").textContent = opt.concept;
    card.querySelector(".risk").textContent = `Risk: ${opt.risk}`;
    const ol = card.querySelector("ol");
    for (const f of opt.frags) {
      const li = document.createElement("li");
      if (f.error) {
        li.className = "bad";
        li.textContent = `${f.count}× ${f.type} ${f.mission}: can't be fragged (${f.error}).`;
      } else {
        const prof = PROFILE[f.profile] ? `, ${PROFILE[f.profile]}` : "";
        li.innerHTML = "<span class='what'></span><span class='meta'></span>";
        li.querySelector(".what").textContent = `${f.count}× ${f.type} ${f.mission}${prof}: ${f.target || f.purpose}` +
          (f.flyable ? " ✈ flyable" : "");
        li.querySelector(".meta").textContent = `T/O ${f.takeoff}, on target ${f.on_target.split(" ").pop()}` +
          (f.night ? " (night)" : "") + (f.hit !== undefined ? ` · hit ${pct(f.hit)}, losses ${f.losses}` : "") +
          (f.stores && f.stores.length ? ` · ${f.stores.slice(0, 3).join(", ")}` : "") + ` · ${f.purpose}`;
      }
      ol.appendChild(li);
    }
    const btn = document.createElement("button");
    btn.className = "btn primary";
    btn.type = "button";
    btn.textContent = opt.valid ? `Execute option ${i + 1} (${opt.valid} frag${opt.valid > 1 ? "s" : ""})` : "Nothing to frag";
    btn.disabled = !opt.valid;
    btn.addEventListener("click", () => executeOption(i));
    card.appendChild(btn);
    box.appendChild(card);
  });
  if (plan.usage) {
    const u = document.createElement("p");
    u.className = "campaign-line";
    const us = plan.usage;
    u.textContent = `${us.model}: ${us.input_tokens} tokens in, ${us.output_tokens} out` +
      (us.reasoning_tokens ? ` (${us.reasoning_tokens} reasoning)` : "") +
      (typeof us.cost === "number" ? ` · $${us.cost.toFixed(4)}` : "") + ".";
    box.appendChild(u);
  }
}

async function executeOption(i) {
  if (!S.plan) return;  // already executed (double click)
  try {
    const res = await api("/api/commander/execute", { theatre: S.theatre, plan: S.plan.id, option: i });
    take(res.war, false);
    const msg = `Fragged ${res.fragged.length} flight${res.fragged.length === 1 ? "" : "s"} from "${S.plan.options[i].name}".` +
                (res.problems.length ? ` Not fragged: ${res.problems.join("; ")}` : "");
    setStatus(msg, res.problems.length ? "err" : "ok");
    $("c-result").innerHTML = "";
    S.plan = null;
  } catch (e) { setStatus(e.message, "err"); }
}

// ---------- fragging missions ----------
function drawCapPoint() {
  if (!S.map) return;
  S.layers.plan.clearLayers();
  if ($("plan").open && $("p-mission").value === "CAP" && $("p-station").value === "point" && S.capPoint) {
    L.circleMarker(S.capPoint, { radius: 9, color: COLORS.blue, weight: 2, dashArray: "3 3", fill: false })
      .bindTooltip("New CAP station", { className: "map-tip" }).addTo(S.layers.plan);
  }
}

function renderMissions() {
  const sq = S.snap.squadrons.find((x) => x.id === $("p-squadron").value);
  const sel = $("p-mission"), keep = sel.value;
  sel.innerHTML = "";
  for (const m of (sq ? sq.missions : [])) {
    const o = document.createElement("option");
    o.value = o.textContent = m;
    sel.appendChild(o);
  }
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  const cap = sel.value === "CAP";
  $("p-target-row").hidden = cap;
  $("p-station-row").hidden = !cap;
  $("p-when-tot").textContent = cap ? "Time on station…" : "Time on target…";
  $("p-time-label").textContent = cap ? "On station (local)" : "On target (local)";
  drawCapPoint();
  renderLoadouts();
}

// Stored loadouts for the squadron's aircraft, the ones suited to the mission first.
async function renderLoadouts() {
  const sq = S.snap.squadrons.find((x) => x.id === $("p-squadron").value);
  const sel = $("p-loadout"), keep = sel.value, mission = $("p-mission").value;
  if (!sq) return;
  S.loadoutCache = S.loadoutCache || {};
  if (!S.loadoutCache[sq.type]) {
    try { S.loadoutCache[sq.type] = await api(`/api/loadouts?aircraft=${encodeURIComponent(sq.type)}`); }
    catch (e) { S.loadoutCache[sq.type] = []; }
  }
  const list = S.loadoutCache[sq.type];
  sel.innerHTML = '<option value="">Automatic (best match)</option>';
  for (const [title, items] of [[`Suited to ${mission}`, list.filter((l) => l.suits.includes(mission))],
                                ["Other", list.filter((l) => !l.suits.includes(mission))]]) {
    if (!items.length) continue;
    const g = document.createElement("optgroup");
    g.label = title;
    for (const l of items) {
      const o = document.createElement("option");
      o.value = l.name;
      o.textContent = l.name.replace(/^Liberation\s+/i, "");
      g.appendChild(o);
    }
    sel.appendChild(g);
  }
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
  showStores();
}

async function showStores() {
  const sq = S.snap.squadrons.find((x) => x.id === $("p-squadron").value);
  if (!sq) return;
  const q = new URLSearchParams({ aircraft: sq.type, mission: $("p-mission").value, loadout: $("p-loadout").value });
  try {
    const r = await api(`/api/stores?${q}`);
    $("p-stores").innerHTML = "<b></b> ";
    $("p-stores").querySelector("b").textContent = `${r.loadout.replace(/^Liberation\s+/i, "")}:`;
    $("p-stores").append(r.stores.length ? `${r.stores.join(", ")} (each aircraft).` : "guns only.");
  } catch (e) { $("p-stores").textContent = ""; }
}

function renderPlan() {
  const s = S.snap;
  const sqSel = $("p-squadron"), keepSq = sqSel.value;
  sqSel.innerHTML = "";
  for (const sq of s.squadrons) {
    if (sq.side !== "blue" || !sq.total) continue;
    const o = document.createElement("option");
    o.value = sq.id;
    o.textContent = `${sq.type} · ${sq.base} · ${sq.available} ready${s.ai_only.includes(sq.type) ? " · AI" : ""}`;
    sqSel.appendChild(o);
  }
  if ([...sqSel.options].some((o) => o.value === keepSq)) sqSel.value = keepSq;
  renderMissions();
  const stSel = $("p-station"), keepSt = stSel.value;
  stSel.innerHTML = "";
  const pt = document.createElement("option");
  pt.value = "point";
  pt.textContent = S.capPoint ? "Point clicked on the map" : "Point on the map (click the map)";
  stSel.appendChild(pt);
  for (const side of ["blue", "red"]) {
    const g = document.createElement("optgroup");
    g.label = side === "blue" ? "Over a friendly airfield" : "Over an enemy airfield";
    for (const f of s.fields.filter((x) => x.side === side)) {
      const o = document.createElement("option");
      o.value = "field:" + f.name;
      o.textContent = f.name;
      g.appendChild(o);
    }
    stSel.appendChild(g);
  }
  stSel.value = [...stSel.options].some((o) => o.value === keepSt) ? keepSt
    : (s.fields.find((x) => x.side === "blue" && s.front.red.includes(x.name)) ? "field:" + s.fields.find((x) => x.side === "blue" && s.front.red.includes(x.name)).name : "point");
  const tSel = $("p-target"), keepT = tSel.value;
  tSel.innerHTML = "";
  const front = new Set(s.front.blue);
  const zones = [...new Set(s.sites.filter((x) => x.side === "red" && x.alive).map((x) => x.zone))]
    .sort((a, b) => (front.has(b) - front.has(a)) || a.localeCompare(b));
  for (const z of zones) {
    const g = document.createElement("optgroup");
    g.label = `${z} · ${front.has(z) ? "front line" : "deep"}`;
    for (const site of s.sites.filter((x) => x.side === "red" && x.alive && x.zone === z)
                             .sort((a, b) => (ROLE[a.role] || a.role).localeCompare(ROLE[b.role] || b.role))) {
      const o = document.createElement("option");
      o.value = site.id;
      o.textContent = `${ROLE[site.role] || site.role} (${site.id})`;
      g.appendChild(o);
    }
    tSel.appendChild(g);
  }
  if ([...tSel.options].some((o) => o.value === keepT)) tSel.value = keepT;
  const daySel = $("p-day"), keepDay = daySel.value;
  const today = Number(label(clockNow()).split(" ")[1]);
  daySel.innerHTML = "";
  for (let d = today; d <= today + 2; d++) {
    const o = document.createElement("option");
    o.value = String(d);
    o.textContent = `Day ${d}`;
    daySel.appendChild(o);
  }
  if ([...daySel.options].some((o) => o.value === keepDay)) daySel.value = keepDay;
  planNote();
}

function planNote() {
  const sq = S.snap.squadrons.find((x) => x.id === $("p-squadron").value);
  $("p-count").max = sq ? Math.max(1, sq.available) : 12;
  const tot = planTot();
  const cap = $("p-mission").value === "CAP";
  let note = sq && S.snap.ai_only.includes(sq.type) ? `${sq.type} is AI only in DCS; it flies this mission for you. ` : "";
  if (cap) note += "Holds the station for an hour. ";
  if (cap && $("p-station").value === "point" && !S.capPoint) note += "Click the map to place the station. ";
  if (tot !== null) {
    const hour = Number($("p-time").value.split(":")[0]);
    note += `${cap ? "On station" : "On target"} ${label(tot)}${hour >= 21 || hour < 5 ? " (night)" : ""}.`;
  }
  $("p-note").textContent = note;
}

function planTot() {
  if ($("p-when").value !== "tot") return null;
  const [hh, mm] = ($("p-time").value || "02:00").split(":").map(Number);
  const start = new Date(S.snap.start);
  const startOfDay = start.getHours() * 3600 + start.getMinutes() * 60;
  return (Number($("p-day").value) - 1) * 86400 + hh * 3600 + mm * 60 - startOfDay;
}

function pickTarget(site) {
  if (site.side !== "red" || !site.alive) return;
  S.pickedSite = site;
  $("c-picked").textContent = `Pointing at: ${ROLE[site.role] || site.role} near ${site.zone} (${site.id}).`;
  if ($("commander").open && !$("plan").open) {
    setStatus(`Selected ${ROLE[site.role] || site.role} near ${site.zone} for the commander.`);
    return;
  }
  if (!$("plan").open) { $("plan").open = true; renderPlan(); }
  if ($("p-mission").value === "CAP") return;  // a map click places the CAP station instead
  $("p-target").value = site.id;
  setStatus(`Target: ${ROLE[site.role] || site.role} near ${site.zone}.`);
}

async function fragMission() {
  const mission = $("p-mission").value;
  const body = { theatre: S.theatre, squadron: $("p-squadron").value, mission,
                 count: Number($("p-count").value), tot: planTot(), loadout: $("p-loadout").value };
  if (mission === "CAP") {
    const st = $("p-station").value;
    if (st === "point") {
      if (!S.capPoint) { setStatus("Click the map to place the CAP station first.", "err"); return; }
      body.station = S.capPoint;
    } else {
      body.field = st.slice("field:".length);
    }
  } else {
    body.target = $("p-target").value;
  }
  try {
    const res = await api("/api/war/schedule", body);
    S.selected = res.flight;
    S.capPoint = null;
    take(res.war, false);
    const f = res.war.flights.find((x) => x.id === res.flight);
    const when = mission === "CAP" ? `on station ${label(f.station)}` : `on target ${label(f.tot)}`;
    setStatus(`${f.callsign} is on the ATO: takeoff ${label(f.takeoff)}, ${when}.`, "ok");
    drawCapPoint();
  } catch (e) { setStatus(e.message, "err"); }
}

function render(full) {
  renderClock();
  if (!$("plan").open || full) renderPlan();
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
    if (S.offline) { S.offline = false; setStatus("Reconnected.", "ok"); }
  } catch (e) {
    // No server (stopped or not started): freeze the clock where it was and say so.
    if (e instanceof TypeError) {
      if (!S.offline && S.snap) { S.snap.clock = clockNow(); S.snap.paused = true; S.offline = true; renderClock(); }
      setStatus("Mission Builder isn't running. Start it with DCS Mission Builder.bat; this page reconnects by itself.", "err");
    } else {
      setStatus(e.message, "err");
    }
  }
}

function take(snap, full) {
  const first = !S.snap;
  S.snap = snap;
  S.at = performance.now();
  render(full);
  // A flight just came back from DCS: say what happened (also on reopening the page soon after).
  const debrief = [...snap.events].reverse().find((e) => e.debrief);
  if (debrief && debrief.sortie !== S.lastDebrief) {
    const fresh = Date.now() - new Date(debrief.when).getTime() < 15 * 60 * 1000;
    if (!first || fresh) setStatus(debrief.text + " The war is paused there: press Run to carry on.", "ok");
    S.lastDebrief = debrief.sortie;
  }
}

async function clockCmd(body) {
  try { take(await api("/api/war/clock", { theatre: S.theatre, ...body }), false); }
  catch (e) { setStatus(e.message, "err"); }
}

// Jump in now: freeze the war where it is and point at the flights you can take.
async function jumpIn() {
  await clockCmd({ paused: true });
  $("show-blue").checked = true;  // enemy flights stay listed: you'll want to know what's up there
  renderAto();
  const t = clockNow();
  const air = S.snap.flights.filter((f) => f.side === "blue" && f.status === "airborne" && !S.snap.ai_only.includes(f.type)
                                          && inAir(f, t) && t < f.land - 300).length;
  setStatus(`War paused at ${label(t)}. Pick a friendly flight: ${air} in the air now (you start where it is), ` +
            "or a planned one (you start at its takeoff).", "ok");
}

async function fly() {
  const f = S.snap.flights.find((x) => x.id === S.selected);
  if (!f || S.busy) return;
  S.busy = true;
  setStatus(`Building ${f.callsign}'s mission…`);
  try {
    if (!S.snap.paused) await clockCmd({ paused: true });  // the moment you jump in is the moment on the map
    const res = await api("/api/war/fly", { theatre: S.theatre, flight: f.id });
    take(res.war, true);
    setStatus(`Saved ${res.path}. It's in DCS under Mission → My Missions → Generated.`, "ok");
  } catch (e) { setStatus(e.message, "err"); }
  S.busy = false;
}

async function boot() {
  initMap();
  if (S.map) setupBaseMaps();
  setupRings();
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
  $("btn-jump").addEventListener("click", jumpIn);
  $("btn-scrub").addEventListener("click", async () => {
    try { take(await api("/api/war/cancel", { theatre: S.theatre, flight: S.selected }), false); setStatus("Flight scrubbed."); }
    catch (e) { setStatus(e.message, "err"); }
  });
  $("btn-schedule").addEventListener("click", fragMission);
  $("btn-ask").addEventListener("click", askCommander);
  $("c-order").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) askCommander(); });
  $("p-squadron").addEventListener("change", () => { renderMissions(); planNote(); });
  $("p-mission").addEventListener("change", () => { renderMissions(); planNote(); });
  $("p-station").addEventListener("change", () => { drawCapPoint(); planNote(); });
  $("p-loadout").addEventListener("change", showStores);
  $("p-when").addEventListener("change", () => { $("p-tot-row").hidden = $("p-when").value !== "tot"; planNote(); });
  for (const id of ["p-squadron", "p-day", "p-time"]) $(id).addEventListener("change", planNote);
  $("plan").addEventListener("toggle", () => { if ($("plan").open) renderPlan(); drawCapPoint(); });
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
