"""A persistent campaign per map, started from one Liberation scenario (the baseline).

State lives in campaign/<theatre>.json next to the project:
  * who holds each airfield,
  * every enemy land position ("site") from the baseline, alive or destroyed,
  * sorties: missions saved from the app, and what happened when they were flown.

Each enemy site belongs to the zone of its nearest enemy airfield. An airfield with
no sites of its own gets a garrison on its parking area, so every field has to be
fought for. When every site in a field's zone is destroyed, blue captures the field:
it becomes a home field and the front moves on.

Results come back through dcs.log. Mission scripts can't write files in a stock DCS
install, but env.info() can write log lines, so each mission logs a line per
destroyed tracked unit (see results_script). DCS zips the previous dcs.log when it
starts, so a flight's lines survive later restarts; import_results() reads both.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import secrets
import zipfile
from pathlib import Path

from .scenarios import Scenario

STATE_DIR = Path(__file__).resolve().parent.parent / "campaign"
STATE_VERSION = 1

# One scenario per map. Maps not listed use their first scenario alphabetically.
BASELINES = {
    "Caucasus": "Caucasus - Black Sea",
    "PersianGulf": "Persian Gulf - Operation Noisy Cricket",
    "Syria": "Syria - Operation Allied Sword",
    "Nevada": "Nevada - Exercise Vegas Nerve",
    "MarianaIslands": "Marianas - Operation Velvet Thunder",
}
# A site counts as destroyed when at least this share of the units placed on it in one
# sortie are killed (2 of 4 launchers, 3 of 6 structures...).
DESTROYED_SHARE = 0.5
LOG_TAG = "MBRESULT"


def baseline(theatre: str, scenarios: list[Scenario]) -> Scenario | None:
    by_name = {s.name: s for s in scenarios}
    return by_name.get(BASELINES.get(theatre, "")) or (scenarios[0] if scenarios else None)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Campaign:
    def __init__(self, theatre: str, data: dict):
        self.theatre = theatre
        self.data = data

    # --- creation / storage ------------------------------------------------------------

    @classmethod
    def new(cls, theatre: str, sc: Scenario, terrain) -> "Campaign":
        fields = {n: "blue" for n in sc.blue_fields} | {n: "red" for n in sc.red_fields}
        pos = {n: [round(terrain.airports[n].position.x), round(terrain.airports[n].position.y)]
               for n in fields if n in terrain.airports}
        red_fields = [n for n in sc.red_fields if n in pos]
        sites = []
        for role, pts in sorted(sc.red_sites.items()):
            for x, y in pts:
                zone = min(red_fields, key=lambda n: _dist(pos[n], (x, y))) if red_fields else None
                sites.append({"id": f"R{len(sites) + 1}", "role": role, "x": x, "y": y, "zone": zone, "alive": True})
        for n in red_fields:
            if not any(s["zone"] == n for s in sites):
                a = terrain.airports[n]
                slot = a.parking_slots[len(a.parking_slots) // 2].position if a.parking_slots else a.position
                sites.append({"id": f"R{len(sites) + 1}", "role": "armor", "x": round(slot.x), "y": round(slot.y),
                              "zone": n, "alive": True, "garrison": True})
        data = {"version": STATE_VERSION, "theatre": theatre, "scenario": sc.name,
                "started": dt.datetime.now().isoformat(timespec="seconds"),
                "fields": fields, "field_pos": pos, "sites": sites, "sorties": [], "events": []}
        return cls(theatre, data)

    @staticmethod
    def path(theatre: str) -> Path:
        return STATE_DIR / f"{theatre}.json"

    @classmethod
    def load_or_new(cls, theatre: str, sc: Scenario, terrain) -> "Campaign":
        p = cls.path(theatre)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("version") == STATE_VERSION and data.get("scenario") == sc.name:
                return cls(theatre, data)
        c = cls.new(theatre, sc, terrain)
        c.save()
        return c

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        tmp = self.path(self.theatre).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path(self.theatre))

    # --- current geography -------------------------------------------------------------

    def fields_of(self, side: str) -> list[str]:
        return sorted(n for n, s in self.data["fields"].items() if s == side)

    def alive_sites(self) -> list[dict]:
        return [s for s in self.data["sites"] if s["alive"]]

    def front_zones(self, count: int = 2) -> list[str]:
        """Enemy fields still holding out, nearest blue territory first."""
        blue = [self.data["field_pos"][n] for n in self.fields_of("blue") if n in self.data["field_pos"]]
        red = [n for n in self.fields_of("red") if any(s["zone"] == n for s in self.alive_sites())]
        if not blue:
            return red[:count]
        return sorted(red, key=lambda n: min(_dist(self.data["field_pos"][n], b) for b in blue))[:count]

    def view(self, sc: Scenario) -> Scenario:
        """The baseline scenario as it stands now: current airfield owners, surviving enemy sites."""
        red_sites: dict[str, list] = {}
        ids = {}
        for s in self.alive_sites():
            red_sites.setdefault(s["role"], []).append([s["x"], s["y"]])
            ids[f"{s['x']},{s['y']}"] = s["id"]
        front = set(self.front_zones())
        return Scenario(name=sc.name, theatre=sc.theatre, blue_fields=self.fields_of("blue"),
                        red_fields=self.fields_of("red"), blue_sites=sc.blue_sites, red_sites=red_sites,
                        blue_ships=sc.blue_ships, red_ships=sc.red_ships, red_site_ids=ids,
                        front_sites=sorted(ids[f"{s['x']},{s['y']}"] for s in self.alive_sites()
                                           if s["zone"] in front))

    def summary(self) -> dict:
        zones = []
        for n in self.fields_of("red"):
            mine = [s for s in self.data["sites"] if s["zone"] == n]
            zones.append({"field": n, "alive": sum(s["alive"] for s in mine), "total": len(mine)})
        front = self.front_zones()
        zones.sort(key=lambda z: (z["field"] not in front, z["field"]))
        return {"theatre": self.theatre, "scenario": self.data["scenario"], "started": self.data["started"],
                "blue_fields": self.fields_of("blue"), "red_fields": self.fields_of("red"), "front": front,
                "zones": zones, "sites_alive": len(self.alive_sites()), "sites_total": len(self.data["sites"]),
                "won": not self.fields_of("red"),
                "pending": [s for s in self.data["sorties"] if s["status"] == "pending"][-5:],
                "events": self.data["events"][-12:]}

    # --- sorties -----------------------------------------------------------------------

    def register_sortie(self, sortie_id: str, file: str, mission_type: str, aircraft: str,
                        tracked: dict[str, int]) -> None:
        self.data["sorties"].append({"id": sortie_id, "file": file, "mission": mission_type, "aircraft": aircraft,
                                     "saved": dt.datetime.now().isoformat(timespec="seconds"),
                                     "status": "pending", "tracked": tracked})
        self.save()

    def apply(self, sortie: dict, dead_units: set[str], source: str) -> list[str]:
        """Mark sites destroyed and fields captured from one flown sortie. Returns what changed."""
        killed: dict[str, int] = {}
        for unit in dead_units:
            m = re.match(r"MB:(R\d+):", unit)
            if m:
                killed[m.group(1)] = killed.get(m.group(1), 0) + 1
        by_id = {s["id"]: s for s in self.data["sites"]}
        changes = []
        for site_id, placed in sortie["tracked"].items():
            s = by_id.get(site_id)
            if s and s["alive"] and killed.get(site_id, 0) >= max(1, math.ceil(placed * DESTROYED_SHARE)):
                s["alive"] = False
                changes.append(f"Destroyed {_role_name(s)} near {s['zone']}.")
        for n in self.fields_of("red"):
            if not any(s["alive"] for s in self.data["sites"] if s["zone"] == n):
                self.data["fields"][n] = "blue"
                changes.append(f"Captured {n}.")
        if not self.fields_of("red"):
            changes.append("All enemy airfields captured. The campaign is won.")
        sortie["status"] = "flown"
        sortie["result"] = {"applied": dt.datetime.now().isoformat(timespec="seconds"), "source": source,
                            "units_killed": len(dead_units), "changes": changes}
        when = sortie["result"]["applied"]
        self.data["events"].append({"when": when, "sortie": sortie["id"], "mission": sortie["mission"],
                                    "aircraft": sortie["aircraft"],
                                    "text": " ".join(changes) or f"{len(dead_units)} enemy units destroyed; no site lost."})
        self.save()
        return changes


def _role_name(site: dict) -> str:
    if site.get("garrison"):
        return "the airfield garrison"
    return {"sam": "a SAM site", "shorad": "a short-range air defence site", "ewr": "an early-warning radar",
            "armor": "an armoured unit", "missile": "a missile battery", "structure": "an installation",
            "farp": "a forward helicopter base"}.get(site["role"], "a site")


def new_sortie_id() -> str:
    return secrets.token_hex(4)


# --- mission side: the Lua that reports results --------------------------------------

def results_script(sortie_id: str, unit_names: list[str]) -> str:
    """Lua run at mission start. Logs a line to dcs.log for each tracked unit that dies,
    checked both by event and every 30 s (DCS doesn't always send an event for ground kills)."""
    names = ",".join(json.dumps(n) for n in unit_names)
    return f"""
do
  local sortie = "{sortie_id}"
  local tracked = {{{names}}}
  local dead = {{}}
  local function log(s) env.info("{LOG_TAG}|" .. sortie .. "|" .. s) end
  local function mark(name)
    if name and not dead[name] and string.sub(name, 1, 3) == "MB:" then
      dead[name] = true
      log("DEAD|" .. name)
    end
  end
  local function nameOf(obj)
    if not obj then return nil end
    local ok, n = pcall(function() return obj:getName() end)
    if ok then return n end
    return nil
  end
  local handler = {{}}
  function handler:onEvent(e)
    if e.id == world.event.S_EVENT_DEAD or e.id == world.event.S_EVENT_UNIT_LOST then
      mark(nameOf(e.initiator))
    elseif e.id == world.event.S_EVENT_KILL then
      mark(nameOf(e.target))
    elseif e.id == world.event.S_EVENT_MISSION_END then
      log("END")
    end
  end
  world.addEventHandler(handler)
  local function sweep()
    for _, n in ipairs(tracked) do
      if not dead[n] then
        local u = Unit.getByName(n) or StaticObject.getByName(n)
        local gone = (u == nil)
        if not gone then
          local ok, alive = pcall(function() return u:isExist() and u:getLife() >= 1 end)
          gone = ok and not alive
        end
        if gone then mark(n) end
      end
    end
  end
  timer.scheduleFunction(function(_, t) pcall(sweep); return t + 30 end, nil, timer.getTime() + 30)
  log("START|" .. #tracked)
end
"""


# --- reading results back --------------------------------------------------------------

def _log_texts(logs_dir: Path, since: float, seen_zips: set[str]):
    """(source name, text, finished) for the live dcs.log and archived logs newer than `since`."""
    for z in sorted(logs_dir.glob("dcs.log-*.zip")):
        if z.name in seen_zips or z.stat().st_mtime < since:
            continue
        try:
            with zipfile.ZipFile(z) as zf:
                text = "\n".join(zf.read(i).decode("utf-8", "replace") for i in zf.namelist())
        except (zipfile.BadZipFile, OSError):
            continue
        yield z.name, text, True  # DCS has restarted since, so this flight is over
    live = logs_dir / "dcs.log"
    if live.exists() and live.stat().st_mtime >= since:
        yield "dcs.log", live.read_text(encoding="utf-8", errors="replace"), False


def scan(logs_dir: Path, sorties: list[dict], seen_zips: set[str], force_live: bool = False):
    """Results for pending sorties: {sortie id: (dead unit names, source)}.

    A sortie counts as finished when its END line is logged, when it appears in an
    archived log (DCS restarted after it), or when force_live is set."""
    pending = {s["id"]: s for s in sorties if s["status"] == "pending"}
    if not pending or not logs_dir.is_dir():
        return {}
    since = min(dt.datetime.fromisoformat(s["saved"]).timestamp() for s in pending.values()) - 60
    found: dict[str, tuple[set, str]] = {}
    pat = re.compile(rf"{LOG_TAG}\|([0-9a-f]+)\|(\w+)(?:\|(\S+))?")
    for source, text, finished in _log_texts(logs_dir, since, seen_zips):
        lines: dict[str, dict] = {}
        for m in pat.finditer(text):
            sid, kind, arg = m.groups()
            if sid not in pending or sid in found:
                continue
            rec = lines.setdefault(sid, {"dead": set(), "end": False, "start": False})
            if kind == "DEAD" and arg:
                rec["dead"].add(arg)
            elif kind == "END":
                rec["end"] = True
            elif kind == "START":
                rec["start"] = True
        for sid, rec in lines.items():
            if rec["start"] and (finished or rec["end"] or force_live):
                found[sid] = (rec["dead"], source)
        if finished:
            seen_zips.add(source)
    return found
