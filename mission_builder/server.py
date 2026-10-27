"""Local web app: serves the briefing UI on 127.0.0.1 and a small JSON API.

GET  /                      the briefing page (web/index.html)
GET  /api/options           owned aircraft, maps, choice lists, last session
GET  /api/airports?terrain=&aircraft=&scenario=&side=   the player's side's airfields for that aircraft
GET  /api/loadouts?aircraft=            stored loadouts for that aircraft
GET  /api/campaign                      every map's campaign state (checks dcs.log for results first)
POST /api/generate {values, locks, reroll}  -> {values, briefing}
POST /api/save                               -> {path}; registers the sortie with the map's campaign
POST /api/campaign/check {force}             apply results now (force: even if the flight hasn't ended)
POST /api/campaign/reset {theatre}           start that map's campaign again from its baseline
POST /api/ping                               keeps the server alive while the page is open

The server shuts itself down a minute after the last page stops pinging, so
closing the browser tab is all it takes to quit.
"""
from __future__ import annotations

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
        generator.SCENARIOS.clear()
        for theatre in self.campaigns:
            self.refresh_view(theatre)
        self.seen_logs: set[str] = set()
        self.lock = threading.RLock()
        self.current = None          # (spec, mission, briefing) of the last generate
        self.last_ping = time.time()
        self.page_seen = False

    def refresh_view(self, theatre: str) -> None:
        """What the generator sees for this map: the campaign as it stands now."""
        self.gen.SCENARIOS[theatre] = [self.campaigns[theatre].view(self.baselines[theatre])]

    def check_results(self, force: bool = False) -> list[str]:
        """Apply results of flown sorties found in dcs.log. Returns what changed, for the status line."""
        changes = []
        with self.lock:
            logs = Path(self.config["dcs_logs"])
            for theatre, c in self.campaigns.items():
                for sid, (dead, source) in self.campaign_mod.scan(logs, c.data["sorties"], self.seen_logs,
                                                                  force).items():
                    sortie = next(s for s in c.data["sorties"] if s["id"] == sid)
                    changes += c.apply(sortie, dead, source) or [f"{len(dead)} enemy units destroyed."]
                    self.refresh_view(theatre)
        return changes

    def campaign_state(self) -> dict:
        return {t: c.summary() for t, c in self.campaigns.items()}

    def reset_campaign(self, theatre: str) -> None:
        from .inventory import TERRAINS
        with self.lock:
            c = self.campaign_mod.Campaign.new(theatre, self.baselines[theatre], TERRAINS[theatre]())
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
                c.register_sortie(briefing.sortie, str(path), briefing.mission_type, spec.aircraft, briefing.tracked)
            return {"path": str(path), "sortie": briefing.sortie}


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
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    logging.info("serving on http://127.0.0.1:%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
