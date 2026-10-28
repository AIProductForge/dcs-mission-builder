"""Local web app: serves the briefing UI on 127.0.0.1 and a small JSON API.

GET  /                      the briefing page (web/index.html)
GET  /api/options           owned aircraft, maps, choice lists, last session
GET  /api/airports?terrain=&aircraft=&scenario=&side=   the player's side's airfields for that aircraft
GET  /api/loadouts?aircraft=            stored loadouts for that aircraft
GET  /api/stores?aircraft=&mission=&loadout=   what that loadout carries (loadout "" = best match)
GET  /api/campaign                      every map's campaign state (checks dcs.log for results first)
POST /api/generate {values, locks, reroll}  -> {values, briefing}
POST /api/save                               -> {path}; registers the sortie with the map's campaign
POST /api/campaign/check {force}             apply results now (force: even if the flight hasn't ended)
POST /api/campaign/reset {theatre}           start that map's campaign again from its baseline
GET  /war.html                          the campaign map: the air war moving, the ATO, the clock
GET  /api/war?theatre=                  that map's war: clock, airfields, sites, squadrons, flights, log
POST /api/war/clock {theatre, rate, paused}  run, pause or change the clock's speed
POST /api/war/fly {theatre, flight}          take a friendly flight and save its .miz: a planned one at its
                                             takeoff, an airborne one now (jump in), where the map shows it
POST /api/war/release {theatre}              give a taken flight back to the AI
POST /api/war/spectate {theatre}             save a mission with no player aircraft: watch the war in DCS
POST /api/war/schedule {theatre, squadron, mission, count, tot, target | station | field}
                                             frag a mission onto the ATO (tot null = ASAP)
POST /api/war/cancel {theatre, flight}       scrub a friendly flight that hasn't taken off
POST /api/commander/plan {theatre, order, site}   the air commander's courses of action for an order
POST /api/commander/execute {theatre, plan, option}  frag the chosen course of action onto the ATO
GET  /api/current                       the last mission built (for the briefing page)
POST /api/ping                               keeps the server alive while the page is open

The server shuts itself down a minute after the last page stops pinging, so
closing the browser tab is all it takes to quit.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import random
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
WEB = Path(__file__).resolve().parent / "web"
CONFIG_PATH = ROOT / "config.json"
SAVED_GAMES = Path(os.path.expandvars(r"%USERPROFILE%\Saved Games"))
DEFAULT_CONFIG = {
    "dcs_dir": r"F:\DCS World",
    "output_dir": str(SAVED_GAMES / "DCS" / "Missions" / "Generated"),
    "liberation_payloads": str(SAVED_GAMES / "dcs_liberation" / "resources" / "customized_payloads"),
    "liberation_campaigns": str(SAVED_GAMES / "dcs_liberation" / "resources" / "campaigns"),
    "dcs_logs": str(SAVED_GAMES / "DCS" / "Logs"),
    # The air commander's AI: "openrouter" (OPENROUTER_API_KEY) or "anthropic" (ANTHROPIC_API_KEY);
    # model "" = that provider's default.
    "commander": {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"},
    "port": 8765,
    "last": {},
}
IDLE_SHUTDOWN_S = 60
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    for key in ("dcs_dir", "output_dir", "liberation_payloads", "liberation_campaigns", "dcs_logs"):
        config[key] = os.path.expandvars(config[key])
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


class State:
    def __init__(self, config: dict):
        from .inventory import configure_payloads, installed_terrains, owned_aircraft
        self.config = config
        dcs_dir = Path(config["dcs_dir"])
        configure_payloads(dcs_dir, Path(config["liberation_payloads"]))
        from . import generator
        self.gen = generator
        owned = owned_aircraft(dcs_dir)
        self.owned = [a.type_id for a in owned]
        self.modules = {a.type_id: a.module_folder for a in owned}
        self.terrains = installed_terrains(dcs_dir)
        from . import campaign, scenarios
        from .inventory import TERRAINS
        self.campaign_mod = campaign
        # One campaign per map, started from that map's baseline scenario.
        self.baselines, self.campaigns = {}, {}
        for theatre, found in scenarios.load(Path(config["liberation_campaigns"]), self.terrains).items():
            base = campaign.baseline(theatre, found)
            self.baselines[theatre] = base
            self.campaigns[theatre] = campaign.Campaign.load_or_new(theatre, base, TERRAINS[theatre]())
        from . import war
        self.war = war
        war.LOADOUTS = self.loadout_for
        self._blue_types = None
        for theatre, c in self.campaigns.items():
            if "war" not in c.data:
                self.init_war(c)
                c.save()
            elif war.add_bombers(c, self.jet_fields(theatre)):  # wars started before the bombers
                c.save()
            if c.ensure_airbase_defence():  # campaigns started before airbase air defence
                c.save()
            if "war" in c.data and war.migrate(c):
                c.save()
        generator.SCENARIOS.clear()
        for theatre in self.campaigns:
            self.refresh_view(theatre)
        self.active_theatre: str | None = None   # the map the campaign page is showing; only it runs
        self.seen_logs: set[str] = set()
        self.plans: dict[str, dict] = {}   # air commander plans by id, until executed or restart
        self.lock = threading.RLock()
        self.current = None          # (spec, mission, briefing) of the last generate
        self.last_ping = time.time()
        self.page_seen = False

    # Types the player owns that can fly campaign missions, one per module, best known first.
    PREFERRED = ["F-16C_50", "FA-18C_hornet", "F-15C", "F-15ESE", "A-10A", "F-14B", "F-4E-45MC", "Su-27",
                 "MiG-29S", "Su-25T", "J-11A", "Su-33", "AH-64D_BLK_II", "Ka-50_3", "Mi-24P"]

    ROLE_LIMITS = {"F-15C": ["CAP"], "Su-27": ["CAP"], "J-11A": ["CAP"], "Su-33": ["CAP"], "MiG-29S": ["CAP"],
                   "MiG-29A": ["CAP"], "MiG-29G": ["CAP"], "A-10A": ["CAS", "Strike"], "Su-25": ["CAS", "Strike"],
                   "F-14B": ["CAP", "Strike"], "F-15ESE": ["Strike", "CAP"], "F-4E-45MC": ["Strike", "CAP"]}

    def blue_types(self) -> list[tuple[str, list[str], bool]]:
        if self._blue_types is None:
            g = self.gen
            order = {t: i for i, t in enumerate(self.PREFERRED)}
            jets, helis, modules = [], [], set()
            for a in sorted(self.owned, key=lambda a: (order.get(a, 99), a)):
                if self.modules.get(a, a) in modules:
                    continue
                heli = g.is_helicopter(a)
                names = g.loadouts(a)
                missions = [mt for mt in ("CAP", "Strike", "SEAD", "CAS") if any(g._suits(n, mt) for n in names)]
                if heli:
                    missions = [mt for mt in missions if mt in ("Strike", "CAS")]
                if a in self.ROLE_LIMITS:  # loadout names alone would put an F-15C on strike
                    missions = [mt for mt in missions if mt in self.ROLE_LIMITS[a]]
                if not missions:
                    continue
                modules.add(self.modules.get(a, a))
                (helis if heli else jets).append((a, missions, heli))
            self._blue_types = jets[:6] + helis[:1]
        return self._blue_types

    @functools.lru_cache(maxsize=None)
    def loadout_for(self, type_id: str, mission: str, chosen: str = "") -> tuple[str, list[str]]:
        """What a flight of this type carries for this mission: the chosen stored loadout, or the
        generator's best match (the same choice the mission makes)."""
        g = self.gen
        mission = mission if mission in g.PAYLOAD_KEYWORDS else "CAP"
        ut = g.unit_type(type_id)
        pylons, name = g._payload(ut, mission, g._main_task(ut, mission), chosen)
        return (name if pylons else "Guns only"), g.stores_summary(pylons)

    @staticmethod
    def jet_fields(theatre: str) -> set[str]:
        from .inventory import TERRAINS
        t = TERRAINS[theatre]()
        return {n for n, a in t.airports.items() if a.runways and any(p.airplanes for p in a.parking_slots)}

    def init_war(self, c) -> None:
        self.war.init(c, self.blue_types(), self.jet_fields(c.theatre))

    def refresh_view(self, theatre: str) -> None:
        """What the generator sees for this map: the campaign as it stands now."""
        self.gen.SCENARIOS[theatre] = [self.campaigns[theatre].view(self.baselines[theatre])]

    def check_results(self, force: bool = False) -> list[str]:
        """Apply results of flown sorties found in dcs.log. Returns what changed, for the status line."""
        changes = []
        with self.lock:
            logs = Path(self.config["dcs_logs"])
            for theatre, c in self.campaigns.items():
                for sid, rep in self.campaign_mod.scan(logs, c.data["sorties"], self.seen_logs, force).items():
                    sortie = next(s for s in c.data["sorties"] if s["id"] == sid)
                    dead = rep["dead"]
                    ground = c.apply(sortie, dead, rep["source"], rep["hp"])
                    summary = self.war.after_sortie(c, sortie, dead, rep["flown"], ground, rep)
                    changes += summary or ground or [f"{len(dead)} enemy units destroyed."]
                    c.save()
                    self.refresh_view(theatre)
        return changes

    def campaign_state(self) -> dict:
        return {t: c.summary() for t, c in self.campaigns.items()}

    def reset_campaign(self, theatre: str) -> None:
        from .inventory import TERRAINS
        with self.lock:
            c = self.campaign_mod.Campaign.new(theatre, self.baselines[theatre], TERRAINS[theatre]())
            self.init_war(c)
            c.save()
            self.campaigns[theatre] = c
            self.refresh_view(theatre)

    def options(self) -> dict:
        g = self.gen
        return {
            "aircraft": [{"id": a, "module": self.modules[a], "helicopter": g.is_helicopter(a)} for a in self.owned],
            "terrains": self.terrains,
            "mission_types": g.MISSION_TYPES,
            "helicopter_missions": g.HELICOPTER_MISSIONS,
            "campaigns": self.campaign_state(),
            "complexity_names": g.COMPLEXITY_NAMES,
            "starts": list(g.START_TYPES),
            "weather": list(g.WEATHER),
            "skills": list(g.SKILLS),
            "last": self.config.get("last", {}),
            "output_dir": self.config["output_dir"],
        }

    def generate(self, values: dict, locks: set[str]) -> dict:
        g = self.gen
        with self.lock:
            spec = g.randomize(values, locks, self.owned, self.terrains, modules=self.modules)
            c = self.campaigns.get(spec.terrain)
            if c and not c.fields_of("red"):
                raise RuntimeError(f"The {spec.terrain} campaign is won. Reset it to fight there again.")
            spec, mission, briefing = g.build_with_retries(spec, locks, self.owned, self.terrains)
            self.current = (spec, mission, briefing)
            self.config["last"] = {"values": asdict(spec), "locks": sorted(locks)}
            save_config(self.config)
            return {"values": asdict(spec), "briefing": briefing.to_dict()}

    def save(self) -> dict:
        with self.lock:
            if self.current is None:
                raise RuntimeError("Generate a mission first.")
            spec, mission, briefing = self.current
            out_dir = Path(self.config["output_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / self.gen.default_filename(spec, briefing)
            mission.save(str(path))
            c = self.campaigns.get(spec.terrain)
            if c and briefing.sortie and not any(x["id"] == briefing.sortie for x in c.data["sorties"]):
                c.register_sortie(briefing.sortie, str(path), briefing.mission_type, spec.aircraft, briefing.tracked,
                                  flight=spec.flight)
            return {"path": str(path), "sortie": briefing.sortie}

    # --- the air war ------------------------------------------------------------------

    def war_state(self, theatre: str) -> dict:
        with self.lock:
            self.active_theatre = theatre
            # While a flight is out to DCS, look for its results now and then.
            if self.campaigns[theatre].data["war"]["mode"] == "in_dcs" and time.time() - getattr(self, "_checked", 0) > 15:
                self._checked = time.time()
                self.check_results()
            return self.war.snapshot(self.campaigns[theatre])

    def set_clock(self, theatre: str, rate=None, paused=None) -> dict:
        with self.lock:
            w = self.campaigns[theatre].data["war"]
            if rate is not None:
                w["rate"] = max(1, min(256, int(rate)))
            if paused is not None:
                if not paused and w["mode"] == "in_dcs":
                    raise RuntimeError("A flight is out to DCS. Fly it, or give it back to the AI, first.")
                w["paused"] = bool(paused)
            self.campaigns[theatre].save()
            return self.war.snapshot(self.campaigns[theatre])

    def fly(self, theatre: str, flight_id: str) -> dict:
        """Take a planned blue flight: the clock jumps to its takeoff and its mission is saved."""
        g, war = self.gen, self.war
        with self.lock:
            c = self.campaigns[theatre]
            if c.data["war"]["mode"] == "in_dcs":
                raise RuntimeError("Another flight is already out to DCS. Fly it or give it back first.")
            f = war.take_flight(c, flight_id)
            try:
                self.refresh_view(theatre)
                when = war.clock_datetime(c)
                rng = random.Random()
                spec = g.MissionSpec(
                    terrain=theatre, aircraft=f["type"], airport=f["base"], scenario=self.baselines[theatre].name,
                    mission_type=f["mission"], complexity=3, start="Ramp, hot", flight_size=f["count"],
                    date=when.date().isoformat(), hour=when.hour, minute=when.minute,
                    weather=rng.choice(["Clear", "Clear", "Scattered clouds", "Overcast"]), enemy_skill="Good",
                    awacs=True, tanker=False, seed=rng.randrange(1, 2**31), flight=f["id"],
                    loadout=f.get("loadout", "") if f.get("loadout") not in (None, "Default", "Guns only") else "",
                    target_site=f["target"] or "", target_xy=f["target_xy"])
                start = war.player_start(c, f)
                if start["airborne"]:  # joining a flight in the air, where the map shows it
                    spec.start, spec.spawn_xy, spec.rtb = "In the air", start["xy"], start["past_target"]
                ambient = war.ambient_flights(c, f["id"])
                ground = war.ground_sites(c, f, ambient) + war.downed_pilots_ground(c)
                try:
                    mission, briefing = g.build(spec, ambient, ground)
                except g.NoParkingSlotError:
                    spec.start = "In the air"
                    mission, briefing = g.build(spec, ambient, ground)
                self.current = (spec, mission, briefing)
                saved = self.save()
            except Exception:
                war.release_flight(c)
                raise
            c.save()
            return {"path": saved["path"], "values": asdict(spec), "briefing": briefing.to_dict(),
                    "war": war.snapshot(c)}

    def schedule(self, theatre: str, squadron: str, mission: str, count: int, tot, target=None,
                 station=None, field=None, loadout: str = "") -> dict:
        with self.lock:
            c = self.campaigns[theatre]
            if field and not station:  # CAP over an airfield
                station = c.data["field_pos"][field]
            elif station and len(station) == 2 and abs(station[0]) <= 90 and abs(station[1]) <= 180:
                station = self.latlon_to_xy(c, *station)  # a point clicked on the map
            f = self.war.schedule(c, squadron, mission, count, tot, target, station, loadout or "")
            c.save()
            return {"flight": f["id"], "war": self.war.snapshot(c)}

    @staticmethod
    def latlon_to_xy(c, lat: float, lon: float) -> list[float]:
        from dcs.mapping import LatLng, Point
        p = Point.from_latlng(LatLng(lat, lon), c.terrain)
        return [p.x, p.y]

    def cancel(self, theatre: str, flight: str) -> dict:
        with self.lock:
            c = self.campaigns[theatre]
            self.war.cancel(c, flight)
            c.save()
            return self.war.snapshot(c)

    # --- the air commander -------------------------------------------------------------

    def loadout_list(self, type_id: str) -> list[tuple[str, list[str]]]:
        """Every stored loadout for a type with what it carries, for the commander."""
        return [(n, self.loadout_for(type_id, "Strike", n)[1]) for n in self.gen.loadouts(type_id)]

    def commander_plan(self, theatre: str, order: str, site_id: str | None = None) -> dict:
        from . import commander
        order = (order or "").strip()
        if not order:
            raise RuntimeError("Give the commander an order, e.g. 'eliminate the SAM site near Kish'.")
        with self.lock:
            c = self.campaigns[theatre]
            sit = commander.situation(c, self.loadout_list)
            site = next((s for s in c.data["sites"] if s["id"] == site_id), None) if site_id else None
        cfg = {**DEFAULT_CONFIG["commander"], **(self.config.get("commander") or {})}
        plan = commander.ask(order, sit, site, cfg["provider"], cfg["model"])  # slow: don't hold the lock
        with self.lock:
            result = commander.evaluate(self.campaigns[theatre], plan)
            result["order"] = order
            self.plans[result["id"]] = {"theatre": theatre, **result}
            return result

    def commander_execute(self, theatre: str, plan_id: str, option: int) -> dict:
        from . import commander
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan or plan["theatre"] != theatre:
                raise RuntimeError("That plan has expired. Ask the commander again.")
            c = self.campaigns[theatre]
            opt = plan["options"][int(option)]
            done, problems = commander.execute(c, opt)
            if done:
                c.log(f"{self.war.clock_label(c)} · Commander's plan '{opt['name']}' fragged for '{plan['order']}': "
                      + ", ".join(f"{f['callsign']} ({f['count']}x {f['type']}, {f['mission']})" for f in done) + ".",
                      side="blue", t=c.data["war"]["clock"])
            c.save()
            return {"fragged": [f["id"] for f in done], "problems": problems, "war": self.war.snapshot(c)}

    def spectate(self, theatre: str) -> dict:
        """Pause the war and save it as a mission to watch in DCS (no player aircraft)."""
        g, war = self.gen, self.war
        with self.lock:
            c = self.campaigns[theatre]
            began = war.spectate(c)
            try:
                when = war.clock_datetime(c)
                rng = random.Random()
                home = (c.fields_of("blue") or list(c.data["fields"]))[0]
                spec = g.MissionSpec(terrain=theatre, aircraft="F-16C_50", airport=home,
                                     scenario=self.baselines[theatre].name, mission_type="Free flight",
                                     date=when.date().isoformat(), hour=when.hour, minute=when.minute,
                                     weather=rng.choice(["Clear", "Clear", "Scattered clouds"]),
                                     enemy_skill="Good", seed=rng.randrange(1, 2**31))
                ambient = war.ambient_flights(c, "")
                ground = war.spectator_ground(c, ambient) + war.downed_pilots_ground(c)
                mission, briefing = g.build_spectator(spec, ambient, ground)
                out_dir = Path(self.config["output_dir"])
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"Watch_{theatre}_{war.clock_label(c).replace(' ', '_').replace(':', '')}.miz"
                mission.save(str(path))
                if briefing.sortie:
                    c.register_sortie(briefing.sortie, str(path), "Spectate", "", briefing.tracked)
                    c.data["sorties"][-1]["began"] = began
            except Exception:
                war.release_flight(c)
                raise
            c.save()
            return {"path": str(path), "flights": len(ambient), "ground": len(ground), "war": war.snapshot(c)}

    def release(self, theatre: str) -> dict:
        with self.lock:
            c = self.campaigns[theatre]
            self.war.release_flight(c)
            c.save()
            return self.war.snapshot(c)

    def tick(self, dt_real: float) -> None:
        """Run the shown map's war clock (only while a page is open)."""
        with self.lock:
            t = self.active_theatre
            if not t or time.time() - self.last_ping > 30:
                return
            c = self.campaigns[t]
            w = c.data["war"]
            if w["paused"] or w["mode"] != "running":
                return
            if self.war.advance(c, w["clock"] + dt_real * w["rate"]):
                self.refresh_view(t)
            if time.time() - getattr(self, "_saved_at", 0) > 5:
                c.save()
                self._saved_at = time.time()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logging.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/options":
                    return self._json(200, state.options())
                if url.path == "/api/airports":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    heli = state.gen.is_helicopter(q["aircraft"]) if q.get("aircraft") else True
                    return self._json(200, state.gen.airports(q["terrain"], heli, q.get("scenario", ""),
                                                              q.get("side", state.gen.SIDES[0])))
                if url.path == "/api/campaign":
                    state.check_results()
                    return self._json(200, state.campaign_state())
                if url.path == "/api/war":
                    return self._json(200, state.war_state(parse_qs(url.query)["theatre"][0]))
                if url.path == "/api/current":
                    if state.current is None:
                        return self._json(404, {"error": "No mission built yet."})
                    spec, _, briefing = state.current
                    return self._json(200, {"values": asdict(spec), "briefing": briefing.to_dict()})
                if url.path == "/api/stores":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    name, stores = state.loadout_for(q["aircraft"], q.get("mission", "Strike"), q.get("loadout", ""))
                    return self._json(200, {"loadout": name, "stores": stores})
                if url.path == "/api/loadouts":
                    aircraft = parse_qs(url.query)["aircraft"][0]
                    g = state.gen
                    return self._json(200, [{"name": n, "suits": [t for t in g.MISSION_TYPES if g._suits(n, t)]}
                                            for n in g.loadouts(aircraft)])
                name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
                file = (WEB / name).resolve()
                if WEB not in file.parents or not file.is_file():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, file.read_bytes(), CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
            except Exception as e:
                logging.exception("GET %s failed", self.path)
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_POST(self):
            # Only accept same-origin requests from the page this server served.
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return self._json(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                if self.path == "/api/ping":
                    state.last_ping, state.page_seen = time.time(), True
                    return self._json(200, {"ok": True})
                if self.path == "/api/generate":
                    return self._json(200, state.generate(body.get("values", {}), set(body.get("locks", []))))
                if self.path == "/api/save":
                    return self._json(200, state.save())
                if self.path == "/api/campaign/check":
                    changes = state.check_results(bool(body.get("force")))
                    return self._json(200, {"changes": changes, "campaigns": state.campaign_state()})
                if self.path == "/api/war/clock":
                    return self._json(200, state.set_clock(body["theatre"], body.get("rate"), body.get("paused")))
                if self.path == "/api/war/fly":
                    return self._json(200, state.fly(body["theatre"], body["flight"]))
                if self.path == "/api/war/schedule":
                    return self._json(200, state.schedule(body["theatre"], body["squadron"],
                                                          body.get("mission", "Strike"), body.get("count", 2),
                                                          body.get("tot"), body.get("target"), body.get("station"),
                                                          body.get("field"), body.get("loadout", "")))
                if self.path == "/api/war/cancel":
                    return self._json(200, state.cancel(body["theatre"], body["flight"]))
                if self.path == "/api/commander/plan":
                    return self._json(200, state.commander_plan(body["theatre"], body.get("order", ""),
                                                                body.get("site")))
                if self.path == "/api/commander/execute":
                    return self._json(200, state.commander_execute(body["theatre"], body["plan"], body["option"]))
                if self.path == "/api/war/spectate":
                    return self._json(200, state.spectate(body["theatre"]))
                if self.path == "/api/war/release":
                    return self._json(200, state.release(body["theatre"]))
                if self.path == "/api/campaign/reset":
                    state.reset_campaign(body["theatre"])
                    return self._json(200, {"campaigns": state.campaign_state()})
                return self._json(404, {"error": "unknown endpoint"})
            except Exception as e:
                logging.exception("POST %s failed", self.path)
                return self._json(500, {"error": str(e)})

    return Handler


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already in use,
    # which would hide the "already running" case below.
    allow_reuse_address = sys.platform != "win32"


def _clock(state: State) -> None:
    last = time.time()
    while True:
        time.sleep(0.5)
        now = time.time()
        try:
            state.tick(now - last)
        except Exception:
            logging.exception("war clock tick failed")
        last = now


def _watchdog(server: ThreadingHTTPServer, state: State) -> None:
    while True:
        time.sleep(5)
        idle = time.time() - state.last_ping
        # Before the first page load, allow a few minutes for the browser to open.
        if idle > (IDLE_SHUTDOWN_S if state.page_seen else 300):
            logging.info("no page for %.0f s, shutting down", idle)
            server.shutdown()
            return


def main() -> None:
    logging.basicConfig(filename=ROOT / "mission_builder.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # pydcs logs every stock livery it can't parse at ERROR; those are harmless.
    logging.getLogger().addFilter(lambda r: "Could not parse livery" not in r.getMessage())
    config = load_config()
    state = State(config)
    port = int(config.get("port", 8765))
    try:
        server = Server(("127.0.0.1", port), make_handler(state))
    except OSError:
        # Already running (or port taken): just open the existing page.
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    threading.Thread(target=_watchdog, args=(server, state), daemon=True).start()
    threading.Thread(target=_clock, args=(state,), daemon=True).start()
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    logging.info("serving on http://127.0.0.1:%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
