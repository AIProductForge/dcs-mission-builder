"""A persistent campaign per map, started from one Liberation scenario (the baseline).

State lives in campaign/<theatre>.json next to the project:
  * who holds each airfield,
  * every land position ("site") of both sides from the baseline, alive or destroyed,
  * sorties: missions saved from the app, and what happened when they were flown,
  * the air war (clock, squadrons, flights): see war.py.

Each site belongs to the zone of its side's nearest airfield. An airfield with no
sites of its own gets a garrison on its parking area, so every field has to be
fought for. When 60% of a field's zone is destroyed, including 60% of its SAM and
radar sites, the other side captures the field: what's left of the zone withdraws,
the new owner gets a garrison there, and the front moves on.

Results come back through dcs.log. Mission scripts can't write files in a stock DCS
install, but env.info() can write log lines, so each mission logs a line per
destroyed tracked unit (see results_script). DCS zips the previous dcs.log when it
starts, so a flight's lines survive later restarts; scan() reads both.
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
STATE_VERSION = 2

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
# An airfield falls when this share of its zone's sites is destroyed, and the same
# share of its SAM and early-warning radar sites is among them (armour alone won't do).
CAPTURE_SHARE = 0.6
AIR_DEFENCE_ROLES = {"sam", "ewr"}
# Every airfield's zone holds at least this many sites (topped up with a garrison on the
# field's parking areas), and a newly captured field gets a garrison of NEW_GARRISON.
MIN_ZONE = 12
NEW_GARRISON = 8
CONSOLIDATE = 24 * 3600   # campaign seconds a captured field can't be retaken
GARRISON_ROLES = ["armor", "shorad", "sam", "armor", "shorad", "armor", "sam", "armor", "shorad", "armor",
                  "sam", "armor"]
# Air defence around each blue airfield, on top of its zone (the Liberation baselines give the
# blue side few SAMs, so red strikes met almost nothing). Also added when blue takes a field.
AIRBASE_DEFENCE = {"blue": ["sam", "sam", "sam", "ewr"], "red": []}
LOG_TAG = "MBRESULT"


def baseline(theatre: str, scenarios: list[Scenario]) -> Scenario | None:
    by_name = {s.name: s for s in scenarios}
    return by_name.get(BASELINES.get(theatre, "")) or (scenarios[0] if scenarios else None)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def other(side: str) -> str:
    return "red" if side == "blue" else "blue"


class Campaign:
    def __init__(self, theatre: str, data: dict, terrain=None):
        self.theatre = theatre
        self.data = data
        self.terrain = terrain  # pydcs terrain, for map coordinates; not saved

    def ll(self, x: float, y: float) -> list[float]:
        from dcs.mapping import Point
        p = Point(x, y, self.terrain).latlng()
        return [round(p.lat, 5), round(p.lng, 5)]

    # --- creation / storage ------------------------------------------------------------

    @classmethod
    def new(cls, theatre: str, sc: Scenario, terrain) -> "Campaign":
        c = cls(theatre, {}, terrain)
        fields = {n: "blue" for n in sc.blue_fields if n in terrain.airports}
        fields |= {n: "red" for n in sc.red_fields if n in terrain.airports}
        pos, garrison = {}, {}
        for n in fields:
            a = terrain.airports[n]
            pos[n] = [round(a.position.x), round(a.position.y)]
            # Garrison positions: parking spots spread over the field (always on land).
            slots = [p.position for p in a.parking_slots] or [a.position]
            step = max(1, len(slots) // MIN_ZONE)
            garrison[n] = [[round(p.x), round(p.y)] for p in slots[::step][:MIN_ZONE]]
        c.data = {"version": STATE_VERSION, "theatre": theatre, "scenario": sc.name,
                  "started": dt.datetime.now().isoformat(timespec="seconds"),
                  "fields": fields, "field_pos": pos, "garrison_pos": garrison,
                  "field_ll": {n: c.ll(*p) for n, p in pos.items()},
                  "sites": [], "sorties": [], "events": []}
        for side, by_role in (("red", sc.red_sites), ("blue", sc.blue_sites)):
            own = [n for n, s in fields.items() if s == side]
            for role, pts in sorted(by_role.items()):
                for x, y in pts:
                    zone = min(own, key=lambda n: _dist(pos[n], (x, y))) if own else None
                    c.add_site(side, role, x, y, zone)
            for n in own:
                c.garrison(n, side, MIN_ZONE - sum(1 for s in c.data["sites"] if s["zone"] == n))
        c.ensure_airbase_defence()
        return c

    def ensure_airbase_defence(self) -> bool:
        """Give each airfield its side's airbase air defence if it doesn't have it yet (new
        campaigns, older campaign files, fields just captured). Returns True if any was added."""
        added = False
        for n, side in self.data["fields"].items():
            roles = AIRBASE_DEFENCE.get(side) or []
            if not roles or any(s.get("airbase_defence") and s["zone"] == n and s["side"] == side
                                for s in self.data["sites"]):
                continue
            spots = self.data["garrison_pos"][n][::-1]  # the other end of the field from the garrison
            for i, role in enumerate(roles):
                x, y = spots[i % len(spots)]
                self.add_site(side, role, x, y, n, garrison=True)["airbase_defence"] = True
            added = True
        return added

    def add_site(self, side: str, role: str, x: float, y: float, zone: str | None, garrison: bool = False) -> dict:
        prefix = "R" if side == "red" else "B"
        n = sum(1 for s in self.data["sites"] if s["side"] == side) + 1
        site = {"id": f"{prefix}{n}", "side": side, "role": role, "x": x, "y": y, "ll": self.ll(x, y),
                "zone": zone, "alive": True}
        if garrison:
            site["garrison"] = True
        self.data["sites"].append(site)
        return site

    def garrison(self, field_name: str, side: str, count: int) -> None:
        spots = self.data["garrison_pos"][field_name]
        for i in range(max(0, count)):
            x, y = spots[i % len(spots)]
            self.add_site(side, GARRISON_ROLES[i % len(GARRISON_ROLES)], x, y, field_name, garrison=True)

    @staticmethod
    def path(theatre: str) -> Path:
        return STATE_DIR / f"{theatre}.json"

    @classmethod
    def load_or_new(cls, theatre: str, sc: Scenario, terrain) -> "Campaign":
        p = cls.path(theatre)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("version") == STATE_VERSION and data.get("scenario") == sc.name:
                return cls(theatre, data, terrain)
        c = cls.new(theatre, sc, terrain)
        c.save()
        return c

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        tmp = self.path(self.theatre).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path(self.theatre))

    # --- current geography -------------------------------------------------------------

    def fields_of(self, side: str) -> list[str]:
        return sorted(n for n, s in self.data["fields"].items() if s == side)

    def alive_sites(self, side: str = "red") -> list[dict]:
        return [s for s in self.data["sites"] if s["alive"] and s["side"] == side]

    def front_zones(self, attacker: str = "blue", count: int = 2) -> list[str]:
        """The defender's fields still holding out, nearest the attacker's territory first."""
        defender = other(attacker)
        own = [self.data["field_pos"][n] for n in self.fields_of(attacker)]
        held = {s["zone"] for s in self.alive_sites(defender)}
        theirs = [n for n in self.fields_of(defender) if n in held]
        if not own:
            return theirs[:count]
        return sorted(theirs, key=lambda n: min(_dist(self.data["field_pos"][n], p) for p in own))[:count]

    def view(self, sc: Scenario) -> Scenario:
        """The baseline scenario as it stands now, as the player (blue) sees it."""
        red_sites: dict[str, list] = {}
        blue_sites: dict[str, list] = {}
        ids = {}
        for s in self.alive_sites("red"):
            red_sites.setdefault(s["role"], []).append([s["x"], s["y"]])
            ids[f"{s['x']},{s['y']}"] = s["id"]
        for s in self.alive_sites("blue"):
            blue_sites.setdefault(s["role"], []).append([s["x"], s["y"]])
        front = set(self.front_zones("blue"))
        return Scenario(name=sc.name, theatre=sc.theatre, blue_fields=self.fields_of("blue"),
                        red_fields=self.fields_of("red"), blue_sites=blue_sites, red_sites=red_sites,
                        blue_ships=sc.blue_ships, red_ships=sc.red_ships, red_site_ids=ids,
                        front_sites=sorted(s["id"] for s in self.alive_sites("red") if s["zone"] in front))

    def zone_status(self, field_name: str) -> dict:
        """How far an airfield's zone is from falling (see CAPTURE_SHARE)."""
        side = self.data["fields"][field_name]
        mine = [s for s in self.data["sites"] if s["zone"] == field_name and s["side"] == side]
        ad = [s for s in mine if s["role"] in AIR_DEFENCE_ROLES]
        need = math.ceil(len(mine) * CAPTURE_SHARE)
        ad_need = math.ceil(len(ad) * CAPTURE_SHARE)
        destroyed = sum(not s["alive"] for s in mine)
        ad_destroyed = sum(not s["alive"] for s in ad)
        return {"alive": len(mine) - destroyed, "total": len(mine), "destroyed": destroyed, "needed": need,
                "ad_total": len(ad), "ad_destroyed": ad_destroyed, "ad_needed": ad_need,
                "capturable": bool(mine) and destroyed >= need and ad_destroyed >= ad_need}

    def capture_check(self, attacker: str, now: float | None = None) -> list[str]:
        """Hand every defender field whose zone has fallen to the attacker. `now` is the war's
        clock: a field captured less than CONSOLIDATE ago can't change hands again."""
        changes = []
        taken = self.data.setdefault("captured_at", {})
        for n in self.fields_of(other(attacker)):
            if not self.zone_status(n)["capturable"]:
                continue
            if now is not None and n in taken and now - taken[n] < CONSOLIDATE:
                continue
            if now is not None:
                taken[n] = now
            self.data["fields"][n] = attacker
            for s in self.data["sites"]:  # what's left of the zone withdraws with the field
                if s["zone"] == n and (s["alive"] or "repair_at" in s):
                    s["alive"], s["withdrawn"] = False, True
                    s.pop("repair_at", None)
            self.garrison(n, attacker, NEW_GARRISON)
            self.ensure_airbase_defence()
            changes.append(f"{'Friendly' if attacker == 'blue' else 'Enemy'} forces captured {n}.")
        if changes and not self.fields_of(other(attacker)):
            changes.append("The campaign is won." if attacker == "blue" else "The campaign is lost.")
        return changes

    def summary(self) -> dict:
        zones = [{"field": n, **self.zone_status(n)} for n in self.fields_of("red")]
        front = self.front_zones("blue")
        zones.sort(key=lambda z: (z["field"] not in front, z["field"]))
        red = [s for s in self.data["sites"] if s["side"] == "red"]
        return {"theatre": self.theatre, "scenario": self.data["scenario"], "started": self.data["started"],
                "blue_fields": self.fields_of("blue"), "red_fields": self.fields_of("red"), "front": front,
                "zones": zones, "sites_alive": len(self.alive_sites("red")), "sites_total": len(red),
                "won": not self.fields_of("red"), "lost": not self.fields_of("blue"),
                "pending": [s for s in self.data["sorties"] if s["status"] == "pending"][-5:],
                "events": self.data["events"][-12:]}

    def log(self, text: str, mission: str = "", aircraft: str = "", **extra) -> None:
        self.data["events"].append({"when": dt.datetime.now().isoformat(timespec="seconds"), "text": text,
                                    "mission": mission, "aircraft": aircraft, **extra})
        del self.data["events"][:-300]

    # --- sorties -----------------------------------------------------------------------

    def register_sortie(self, sortie_id: str, file: str, mission_type: str, aircraft: str,
                        tracked: dict[str, int], flight: str = "") -> None:
        self.data["sorties"].append({"id": sortie_id, "file": file, "mission": mission_type, "aircraft": aircraft,
                                     "saved": dt.datetime.now().isoformat(timespec="seconds"),
                                     "status": "pending", "tracked": tracked, "flight": flight})
        self.save()

    def apply(self, sortie: dict, dead_units: set[str], source: str, hp: dict | None = None) -> list[str]:
        """Mark sites destroyed and fields captured from one flown sortie. Returns what changed.
        Aircraft losses (units named MBF:<flight>:n) are handled by war.after_sortie."""
        killed: dict[str, int] = {}
        for unit in dead_units:
            m = re.match(r"MB:([RB]\d+):", unit)
            if m:
                killed[m.group(1)] = killed.get(m.group(1), 0) + 1
        by_id = {s["id"]: s for s in self.data["sites"]}
        changes = []
        for site_id, placed in sortie["tracked"].items():
            s = by_id.get(site_id)
            if s and s["alive"] and killed.get(site_id, 0) >= max(1, math.ceil(placed * DESTROYED_SHARE)):
                s["alive"] = False
                s.pop("strength", None)
                changes.append(f"{'Destroyed' if s['side'] == 'red' else 'Lost'} {_role_name(s)} near {s['zone']}.")
            elif s and s["alive"] and hp and site_id in hp:
                left, there = hp[site_id]
                if 0 < left < there:  # survived damaged: it fights (and is placed) at reduced strength
                    s["strength"] = round(s.get("strength", 1.0) * left / there, 2)
                    changes.append(f"{'Damaged' if s['side'] == 'red' else 'Lost part of'} {_role_name(s)} near "
                                   f"{s['zone']} ({left}/{there} left).")
        clock = self.data.get("war", {}).get("clock")
        changes += self.capture_check("blue", clock) + self.capture_check("red", clock)
        sortie["status"] = "flown"
        sortie["result"] = {"applied": dt.datetime.now().isoformat(timespec="seconds"), "source": source,
                            "units_killed": len(dead_units), "changes": changes}
        enemy_kills = sum(1 for u in dead_units if u.startswith("MB:R"))
        if not sortie.get("flight"):  # campaign flights get one summary from war.after_sortie
            self.log(" ".join(changes) or f"{enemy_kills} enemy ground units destroyed; no site lost.",
                     sortie["mission"], sortie["aircraft"], sortie=sortie["id"])
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

POS_EVERY = 300   # mission seconds between position reports


def results_script(sortie_id: str, unit_names: list[str]) -> str:
    """Lua run at mission start: reports what happens to the campaign's units in dcs.log.

    DEAD|unit          a tracked unit died (by event, or found gone by the 30 s sweep after
                       having been seen alive: units that haven't been activated yet don't count)
    LAND|unit|place|t  a tracked aircraft landed (place: airbase, FARP or ship name)
    EJECT|unit|t|x|y   a tracked aircraft's pilot ejected
    POS|flight|t|x|y|alt|alive|fuel|air   every POS_EVERY s and at the end, per flight seen alive
    HP|site|alive|placed                   at the end, per campaign site
    START|n|t / END|t
    Campaign coordinates are pydcs's: x = DCS x, y = DCS z."""
    names = ",".join(json.dumps(n) for n in unit_names)
    return f"""
do
  local sortie = "{sortie_id}"
  local tracked = {{{names}}}
  local dead, seen = {{}}, {{}}
  local flights, sites = {{}}, {{}}
  for _, n in ipairs(tracked) do
    local fid = string.match(n, "^MBF:([^:]+):")
    local sid = string.match(n, "^MB:([^:]+):")
    if fid then flights[fid] = flights[fid] or {{}}; table.insert(flights[fid], n) end
    if sid then sites[sid] = sites[sid] or {{}}; table.insert(sites[sid], n) end
  end
  local function log(s) env.info("{LOG_TAG}|" .. sortie .. "|" .. s) end
  local function mark(name)
    if name and not dead[name] and string.sub(name, 1, 2) == "MB" then
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
  local function object(n) return Unit.getByName(n) or StaticObject.getByName(n) end
  local function alive(n)
    if dead[n] then return false end
    local u = object(n)
    if u == nil then return false end
    local ok, a = pcall(function() return u:isExist() and u:getLife() >= 1 end)
    return ok and a
  end
  local function report()
    local t = timer.getTime()
    for fid, list in pairs(flights) do
      local n, lead = 0, nil
      for _, name in ipairs(list) do
        if alive(name) then n = n + 1; lead = lead or Unit.getByName(name) end
      end
      local ok, p = pcall(function() return lead and lead:getPoint() end)
      if lead and ok and p then
        local okf, fuel = pcall(function() return lead:getFuel() end)
        local oka, air = pcall(function() return lead:inAir() end)
        log(string.format("POS|%s|%.0f|%.0f|%.0f|%.0f|%d|%.2f|%d", fid, t, p.x, p.z, p.y, n,
                          (okf and fuel) or 0, (oka and air) and 1 or 0))
      end
    end
  end
  local function health()
    for sid, list in pairs(sites) do
      local n = 0
      for _, name in ipairs(list) do if alive(name) then n = n + 1 end end
      log(string.format("HP|%s|%d|%d", sid, n, #list))
    end
  end
  local handler = {{}}
  function handler:onEvent(e)
    if e.id == world.event.S_EVENT_DEAD or e.id == world.event.S_EVENT_UNIT_LOST then
      mark(nameOf(e.initiator))
    elseif e.id == world.event.S_EVENT_KILL then
      mark(nameOf(e.target))
    elseif e.id == world.event.S_EVENT_LAND then
      local n = nameOf(e.initiator)
      if n and string.sub(n, 1, 4) == "MBF:" then
        log(string.format("LAND|%s|%s|%.0f", n, nameOf(e.place) or "", timer.getTime()))
      end
    elseif e.id == world.event.S_EVENT_EJECTION then
      local n = nameOf(e.initiator)
      if n and string.sub(n, 1, 4) == "MBF:" then
        local ok, p = pcall(function() return e.initiator:getPoint() end)
        log(string.format("EJECT|%s|%.0f|%.0f|%.0f", n, timer.getTime(), (ok and p and p.x) or 0, (ok and p and p.z) or 0))
      end
    elseif e.id == world.event.S_EVENT_MISSION_END then
      pcall(report)
      pcall(health)
      log("END|" .. string.format("%.0f", timer.getTime()))
    end
  end
  world.addEventHandler(handler)
  local function sweep()
    for _, n in ipairs(tracked) do
      if not dead[n] then
        if alive(n) then seen[n] = true
        elseif seen[n] then mark(n) end   -- gone after having been seen alive
      end
    end
  end
  timer.scheduleFunction(function(_, t) pcall(sweep); return t + 30 end, nil, timer.getTime() + 1)
  timer.scheduleFunction(function(_, t) pcall(report); return t + {POS_EVERY} end, nil, timer.getTime() + 5)
  log("START|" .. #tracked .. "|" .. string.format("%.0f", timer.getTime()))
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


_STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def _new_report() -> dict:
    return {"dead": set(), "end": False, "start": False, "first": None, "last": None, "mission_time": None,
            "pos": {}, "lands": {}, "ejects": [], "hp": {}, "last_pos_t": 0.0}


def scan(logs_dir: Path, sorties: list[dict], seen_zips: set[str], force_live: bool = False) -> dict:
    """Results for pending sorties: {sortie id: report}. A report has "dead" (unit names),
    "source", "flown" (mission seconds), "pos" ({flight: last (t, x, y, alt, alive, fuel, air)}),
    "lands" ({unit: (place, t)}), "ejects", "hp" ({site: (alive, placed)}).

    A sortie counts as finished when its END line is logged, when it appears in an archived log
    (DCS restarted after it), or when force_live is set. Seconds flown is the mission time logged
    at END, else the last position report's time, else the span of the sortie's log lines."""
    pending = {s["id"]: s for s in sorties if s["status"] == "pending"}
    if not pending or not logs_dir.is_dir():
        return {}
    since = min(dt.datetime.fromisoformat(s["saved"]).timestamp() for s in pending.values()) - 60
    found: dict[str, dict] = {}
    tag = f"{LOG_TAG}|"
    for source, text, finished in _log_texts(logs_dir, since, seen_zips):
        reports: dict[str, dict] = {}
        for line in text.splitlines():
            i = line.find(tag)
            if i < 0:
                continue
            parts = line[i + len(tag):].rstrip().split("|")
            if len(parts) < 2:
                continue
            sid, kind, args = parts[0], parts[1], parts[2:]
            if sid not in pending or sid in found:
                continue
            rec = reports.setdefault(sid, _new_report())
            stamp = _STAMP.match(line)
            if stamp:
                when = dt.datetime.fromisoformat(stamp.group(1))
                rec["first"] = rec["first"] or when
                rec["last"] = when
            try:
                if kind == "DEAD" and args:
                    rec["dead"].add(args[0])
                elif kind == "START":
                    rec["start"] = True
                elif kind == "END":
                    rec["end"] = True
                    if args and args[0].replace(".", "", 1).isdigit():
                        rec["mission_time"] = float(args[0])
                elif kind == "POS" and len(args) >= 8:
                    t, x, y, alt, alive, fuel, air = (float(v) for v in args[1:8])
                    rec["pos"][args[0]] = (t, x, y, alt, int(alive), fuel, bool(int(air)))
                    rec["last_pos_t"] = max(rec["last_pos_t"], t)
                elif kind == "LAND" and len(args) >= 3:
                    rec["lands"][args[0]] = (args[1], float(args[2]))
                elif kind == "EJECT" and len(args) >= 4:
                    rec["ejects"].append((args[0], float(args[1]), float(args[2]), float(args[3])))
                elif kind == "HP" and len(args) >= 3:
                    rec["hp"][args[0]] = (int(args[1]), int(args[2]))
            except ValueError:
                continue  # a malformed line: skip it, keep the rest
        for sid, rec in reports.items():
            if rec["start"] and (finished or rec["end"] or force_live):
                flown = rec["mission_time"] or rec["last_pos_t"] or None
                if not flown and rec["first"] and rec["last"]:
                    flown = (rec["last"] - rec["first"]).total_seconds()
                rec["flown"], rec["source"] = flown or 0.0, source
                found[sid] = rec
        if finished:
            seen_zips.add(source)
    return found
