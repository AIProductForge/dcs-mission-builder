"""Build a single-player .miz plus the data for its briefing, using pydcs.

Geography comes from a scenario (a DCS Liberation campaign, see scenarios.py):
the player flies from one of their side's airfields, enemy aircraft come from the
other side's, and ground objectives are put on that side's own land positions.
Without Liberation installed, any airfield can be home and objectives sit next
to an enemy airfield.

Mission types: Free flight, CAP, Intercept, Strike, CAS, SEAD (helicopters:
Free flight, Strike, CAS). Complexity 1-5 scales the air defences, enemy air,
pop-up threats and scrambles, friendly AI flights and the number of targets.
Each mission has triggers that report success, and failure where it applies.

randomize() fills every field the user hasn't pinned; build() turns a full
MissionSpec into a pydcs Mission and a Briefing (plain data for the UI).
"""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import asdict, dataclass, field, fields

import dcs
from dcs import action, condition, countries, planes, task, vehicles
from dcs.action import Coalition
from dcs.cloud_presets import Clouds
from dcs.mapping import Point
from dcs.mission import Mission, StartType
from dcs.statics import Fortification
from dcs.terrain.terrain import NoParkingSlotError
from dcs.triggers import TriggerOnce, TriggerStart
from dcs.unit import Skill

from .inventory import TERRAINS
from . import campaign
from .scenarios import Scenario

START_TYPES = {
    "Runway (engines running)": StartType.Runway,
    "Ramp, hot": StartType.Warm,
    "Ramp, cold": StartType.Cold,
    "In the air": None,
}
MISSION_TYPES = ["Free flight", "CAP", "Intercept", "Strike", "CAS", "SEAD"]
HELICOPTER_MISSIONS = ["Free flight", "Strike", "CAS"]
WEATHER = {"Clear": None, "Scattered clouds": Clouds.LightScattered2, "Overcast": Clouds.Overcast1}
SKILLS = {"Average": Skill.Average, "Good": Skill.Good, "High": Skill.High, "Excellent": Skill.Excellent}
SIDES = ["Blue (as scenario)", "Red (sides swapped)"]
NO_SCENARIO = "None (any airfield)"
COMPLEXITY_NAMES = {1: "Low", 2: "Moderate", 3: "Medium", 4: "High", 5: "Very high"}

# Receivers that refuel from a drogue (probe) rather than a boom.
PROBE_AIRCRAFT = {"FA-18C_hornet", "F-14A-95-GR", "F-14A-135-GR-Early", "F-14A-135-GR", "F-14B",
                  "Su-33", "J-11A"}

M_TO_FT = 3.28084
MPS_TO_KT = 1.94384
M_TO_NM = 1 / 1852

CODENAME_A = ["IRON", "SILENT", "BROKEN", "COLD", "RED", "DESERT", "NIGHT", "STEEL", "GRANITE", "HOLLOW",
              "AMBER", "COPPER", "SHADOW", "WINTER", "BLACK", "ROLLING", "BRIGHT", "QUIET", "OPEN", "FALLEN"]
CODENAME_B = ["ANVIL", "LANCE", "SABRE", "HARBOR", "THUNDER", "TALON", "RIDGE", "VIGIL", "HAMMER", "SPEAR",
              "CANYON", "FALCON", "TEMPEST", "GAUNTLET", "WARDEN", "BASTION", "RAPIER", "SENTRY", "COMPASS", "HALO"]

# Scenarios by theatre; set by the server at startup (empty = no Liberation campaigns).
SCENARIOS: dict[str, list[Scenario]] = {}


@dataclass
class MissionSpec:
    terrain: str
    aircraft: str                  # DCS type id, e.g. "F-15ESE"
    airport: str                   # home airfield
    scenario: str = ""             # Liberation campaign name, or NO_SCENARIO
    side: str = SIDES[0]
    mission_type: str = "CAP"
    complexity: int = 3            # 1 (low) .. 5 (very high)
    loadout: str = ""              # stored loadout name; "" = pick one to suit the mission type
    start: str = "Runway (engines running)"
    flight_size: int = 2
    date: str = "2012-06-01"       # ISO date; kept as text so it round-trips through JSON
    hour: int = 9
    weather: str = "Clear"
    enemy_skill: str = "Good"
    awacs: bool = True
    tanker: bool = False
    seed: int = 0                  # drives every placement choice; same seed = same mission
    minute: int = 0
    # Set when flying a flight from the campaign's air tasking order:
    flight: str = ""               # ATO flight id
    target_site: str = ""          # campaign site id to attack
    target_xy: list = field(default_factory=list)   # [x, y] objective, e.g. a CAP station
    spawn_xy: list = field(default_factory=list)    # joining a flight in the air: where it is now
    rtb: bool = False              # ...and its target is already behind it

    @classmethod
    def from_dict(cls, d: dict) -> "MissionSpec":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


FIELD_NAMES = [f.name for f in fields(MissionSpec)]


def unit_type(type_id: str):
    return dcs.helicopters.helicopter_map.get(type_id) or planes.plane_map[type_id]


def is_helicopter(type_id: str) -> bool:
    return type_id in dcs.helicopters.helicopter_map


def scenario_names(terrain_name: str) -> list[str]:
    return [s.name for s in SCENARIOS.get(terrain_name, [])] or [NO_SCENARIO]


def find_scenario(terrain_name: str, name: str) -> Scenario | None:
    return next((s for s in SCENARIOS.get(terrain_name, []) if s.name == name), None)


def _sides(sc: Scenario | None, side: str):
    """(friendly fields, enemy fields, friendly sites, enemy sites, enemy ships) for the player's side."""
    if sc is None:
        return None, None, {}, {}, []
    if side == SIDES[1]:
        return sc.red_fields, sc.blue_fields, sc.red_sites, sc.blue_sites, sc.blue_ships
    return sc.blue_fields, sc.red_fields, sc.blue_sites, sc.red_sites, sc.red_ships


def airports(terrain_name: str, helicopter: bool = True, scenario: str = "", side: str = SIDES[0]) -> list[str]:
    """Airfields the player can start from: their side's (when a scenario is chosen),
    helicopters anywhere on them, aeroplanes only where there is a runway."""
    t = TERRAINS[terrain_name]()
    friendly, *_ = _sides(find_scenario(terrain_name, scenario), side)
    names = [n for n, a in t.airports.items()
             if (friendly is None or n in friendly)
             and (helicopter or (a.runways and any(p.airplanes for p in a.parking_slots)))]
    return sorted(names)


def _near_front(terrain_name: str, scenario: str, side: str, field_names: list[str], km: float = 90) -> list[str]:
    """Fields within `km` of the nearest enemy land position (helicopters can't fly 150 nm to work)."""
    _, _, _, enemy_sites, _ = _sides(find_scenario(terrain_name, scenario), side)
    pts = [(x, y) for role in enemy_sites.values() for x, y in role]
    if not pts:
        return []
    t = TERRAINS[terrain_name]()
    def gap(name):
        a = t.airports[name].position
        return min(((a.x - x) ** 2 + (a.y - y) ** 2) ** 0.5 for x, y in pts)
    close = [n for n in field_names if gap(n) <= km * 1000]
    return close or sorted(field_names, key=gap)[:2]


def mission_types_for(type_id: str) -> list[str]:
    return HELICOPTER_MISSIONS if is_helicopter(type_id) else MISSION_TYPES


# --- randomizer --------------------------------------------------------------------------

def randomize(values: dict, locks: set[str], owned: list[str], terrains: list[str],
              rng: random.Random | None = None, modules: dict[str, str] | None = None) -> MissionSpec:
    """Keep pinned fields as given and roll everything else. Pinning the airfield
    implies its map and scenario; aircraft, airfield and mission type are kept
    compatible (no jet on a helipad, no helicopter CAP). `modules` maps type id ->
    module folder so each module is equally likely (the F-14 has four variants)."""
    rng = rng or random.Random()
    v = dict(values)
    if "airport" in locks:
        locks = locks | {"terrain", "scenario", "side"}
    if "scenario" in locks:
        locks = locks | {"terrain"}

    def roll(name, choices):
        if name not in locks or v.get(name) in (None, ""):
            v[name] = rng.choice(list(choices))

    roll("terrain", terrains)
    names = scenario_names(v["terrain"])
    if "scenario" not in locks or v.get("scenario") not in names:
        v["scenario"] = rng.choice(names)
    v["side"] = SIDES[0]  # campaigns are flown as blue

    if "aircraft" not in locks:
        candidates = owned
        if "airport" in locks and v["airport"] not in airports(v["terrain"], False, v["scenario"], v["side"]):
            candidates = [a for a in owned if is_helicopter(a)] or owned  # a pinned helipad
        if modules:
            by_module: dict[str, list[str]] = {}
            for a in candidates:
                by_module.setdefault(modules.get(a, a), []).append(a)
            v["aircraft"] = rng.choice(rng.choice(list(by_module.values())))
        else:
            v["aircraft"] = rng.choice(candidates)
    valid_fields = airports(v["terrain"], is_helicopter(v["aircraft"]), v["scenario"], v["side"])
    if not valid_fields:  # e.g. a scenario whose side has only helipads, flying a jet
        valid_fields = airports(v["terrain"], is_helicopter(v["aircraft"]))
    if "airport" not in locks or v.get("airport") not in TERRAINS[v["terrain"]]().airports:
        if is_helicopter(v["aircraft"]):
            valid_fields = _near_front(v["terrain"], v["scenario"], v["side"], valid_fields) or valid_fields
        v["airport"] = rng.choice(valid_fields)

    allowed_missions = mission_types_for(v["aircraft"])
    if "mission_type" not in locks or v.get("mission_type") not in allowed_missions:
        # Prefer missions this aircraft has a suitable stored loadout for (no SEAD in a MiG-15).
        stored = loadouts(v["aircraft"])
        armed = [t for t in allowed_missions if t == "Free flight" or any(_suits(n, t) for n in stored)]
        v["mission_type"] = rng.choice(armed if stored and len(armed) > 1 else allowed_missions)
    roll("complexity", [2, 3, 3, 4, 4, 5])
    roll("start", list(START_TYPES))
    roll("flight_size", [1, 2, 2, 2, 3, 4])
    if "date" not in locks or not v.get("date"):
        start, end = dt.date(2000, 1, 1), dt.date(2022, 12, 31)
        v["date"] = (start + dt.timedelta(days=rng.randrange((end - start).days))).isoformat()
    roll("hour", range(6, 17))
    roll("weather", ["Clear", "Clear", "Scattered clouds", "Overcast"])
    roll("enemy_skill", list(SKILLS))
    roll("awacs", [True, True, True, False])
    roll("tanker", [True, False])
    names = loadouts(v["aircraft"])
    if "loadout" not in locks or v.get("loadout") not in names:
        suited = [n for n in names if _suits(n, v["mission_type"])]
        v["loadout"] = rng.choice(suited or names) if names else ""
    if "seed" not in locks or not v.get("seed"):
        v["seed"] = rng.randrange(1, 2**31)
    for k in ("flight_size", "hour", "complexity"):
        v[k] = int(v[k])
    return MissionSpec.from_dict(v)


# --- loadouts ------------------------------------------------------------------------------

# Loadout names to look for, in order of preference. DCS's own files use task
# words ("CAP", "STRIKE"); Liberation's use "Liberation BARCAP" etc. Flaming
# Cliffs loadouts are named by their weapons, hence the weapon names.
_AIR = ["BARCAP", "TARCAP", "CAP", "FIGHTER SWEEP", "ESCORT", "A-A", "AIR-TO-AIR",
        "AIM-120", "R-77", "R-27", "AIM-7", "R-73", "AIM-9"]
_GROUND = ["STRIKE", "CAS", "BAI", "GROUND ATTACK", "DEAD", "OCA", "KH-", "MK-82", "FAB-", "RBK-", "GBU-", "S-8"]
PAYLOAD_KEYWORDS = {
    "CAP": _AIR,
    "Intercept": ["INTERCEPT"] + _AIR,
    "Strike": _GROUND + ["SEAD"],
    "CAS": ["CAS", "BAI"] + _GROUND,
    "SEAD": ["SEAD", "DEAD", "AGM-88", "KH-58", "KH-25MP"] + _GROUND,
    "Free flight": ["FERRY", "BARCAP", "CAP"],
}


def _main_task(utype, mission_type: str):
    wanted = {"CAP": [task.CAP, task.FighterSweep, task.Intercept],
              "Intercept": [task.Intercept, task.CAP],
              "Strike": [task.GroundAttack, task.PinpointStrike, task.CAS],
              "CAS": [task.CAS, task.GroundAttack],
              "SEAD": [task.SEAD, task.GroundAttack, task.CAS],
              "Free flight": []}[mission_type]
    for t in wanted:
        if t in utype.tasks:
            return t
    return utype.task_default


def loadouts(type_id: str) -> list[str]:
    """Every stored loadout name for this aircraft (DCS's own, then Liberation's)."""
    return sorted((unit_type(type_id).load_payloads() or {}).keys(), key=str.lower)


def _suits(name: str, mission_type: str) -> bool:
    return any(k in name.upper() for k in PAYLOAD_KEYWORDS[mission_type])


def _pylons(payload) -> list:
    pylons = payload["pylons"]
    return [(pylons[k]["num"], {"clsid": pylons[k]["CLSID"]}) for k in pylons]


def stores_summary(pylons) -> list[str]:
    """Human-readable store list, e.g. ['2x AIM-120C AMRAAM', '1x Fuel tank 610 gal']."""
    from dcs.weapons_data import weapon_ids
    counts: dict[str, int] = {}
    for _station, p in pylons or []:
        w = weapon_ids.get(p["clsid"])
        name = (w["name"] if w else p["clsid"]).split(" - ")[0].strip()
        # Multi-carriage racks are named like "GBU-38 * 3": count the bombs, not the rack.
        rack = re.match(r"^(.*?)\s*\*\s*(\d+)$", name)
        counts[rack.group(1) if rack else name] = counts.get(rack.group(1) if rack else name, 0) + \
            (int(rack.group(2)) if rack else 1)
    # A rack's short name ("GBU-38") folds into the full one ("GBU-38(V)1/B") when both appear.
    for short in sorted(counts, key=len):
        longer = next((k for k in counts if k != short and k.startswith(short)), None)
        if longer:
            counts[longer] += counts.pop(short)
    return [f"{n}x {name}" for name, n in counts.items()]


def _payload(utype, mission_type: str, maintask, chosen: str = ""):
    """Pylons as [(station, {"clsid": ...}), ...] plus the loadout's name, or (None, "")."""
    payloads = utype.load_payloads() or {}
    if chosen in payloads:
        return _pylons(payloads[chosen]), chosen
    for keyword in PAYLOAD_KEYWORDS[mission_type]:
        for name, payload in payloads.items():
            if keyword in name.upper():
                return _pylons(payload), name
    return utype.loadout(maintask), "Default"


def _arm(group, utype, mission_type: str, maintask, chosen: str = "") -> tuple[str, list[str]]:
    pylons, name = _payload(utype, mission_type, maintask, chosen)
    for unit in group.units:
        for pylon in pylons or []:
            unit.load_pylon(pylon)
    return (name if pylons else "Guns only"), stores_summary(pylons)


# --- enemy order of battle -----------------------------------------------------------------

AD = vehicles.AirDefence
# SAM sites by tier. Ranges (m) are approximate engagement ranges for the briefing rings.
SAM_SITES = {
    "SA-8": ([AD.Osa_9A33_ln, AD.Osa_9A33_ln], 10_300),
    "SA-6": ([AD.Kub_1S91_str, AD.Kub_2P25_ln, AD.Kub_2P25_ln, AD.Kub_2P25_ln], 25_000),
    "SA-11": ([AD.SA_11_Buk_SR_9S18M1, AD.SA_11_Buk_CC_9S470M1, AD.SA_11_Buk_LN_9A310M1,
               AD.SA_11_Buk_LN_9A310M1, AD.SA_11_Buk_LN_9A310M1], 35_000),
    "SA-15": ([AD.Tor_9A331], 12_000),
    "SA-10": ([AD.S_300PS_64H6E_sr, AD.S_300PS_40B6M_tr, AD.S_300PS_54K6_cp, AD.S_300PS_5P85C_ln,
               AD.S_300PS_5P85C_ln, AD.S_300PS_5P85D_ln], 75_000),
}
SHORAD = {
    "ZSU-23-4": ([AD.ZSU_23_4_Shilka], 2_500),
    "SA-9": ([AD.Strela_1_9P31], 4_200),
    "SA-13": ([AD.Strela_10M3], 5_000),
    "SA-19": ([AD.X_2S6_Tunguska], 8_000),
}
# Point defence around an objective, by complexity.
POINT_DEFENCE = {1: [], 2: ["ZSU-23-4", "SA-9"], 3: ["SA-8", "ZSU-23-4", "SA-13"],
                 4: ["SA-6", "SA-15", "ZSU-23-4"], 5: ["SA-11", "SA-15", "SA-19"]}
# The SAM site that *is* the target on a SEAD mission.
SEAD_TARGET = {1: ["SA-8"], 2: ["SA-6"], 3: ["SA-11"], 4: ["SA-11", "SA-15"], 5: ["SA-10", "SA-15"]}
# Extra SAM sites placed on other enemy SAM positions near the route.
AREA_SAMS = {1: [], 2: [], 3: ["SA-8"], 4: ["SA-6", "SA-8"], 5: ["SA-11", "SA-6", "SA-8"]}
# Enemy fighters: (type, pairs) for a CAP mission, by complexity.
CAP_BANDITS = {1: [(planes.MiG_21Bis, 1)], 2: [(planes.MiG_23MLD, 1)], 3: [(planes.MiG_29S, 2)],
               4: [(planes.MiG_29S, 1), (planes.Su_27, 1)], 5: [(planes.Su_27, 1), (planes.Su_30, 1), (planes.MiG_31, 1)]}
# Bombers for an Intercept mission: list of (type, count), and escort pairs.
BOMBERS = {1: ([(planes.Su_24M, 2)], 0), 2: ([(planes.Tu_22M3, 2)], 0), 3: ([(planes.Tu_22M3, 4)], 0),
           4: ([(planes.Tu_22M3, 4)], 1), 5: ([(planes.Tu_22M3, 4), (planes.Tu_95MS, 2)], 2)}
STRUCTURES = [Fortification.Workshop_A, Fortification.Tech_combine, Fortification.Comms_tower_M,
              Fortification._Command_Center, Fortification.Fuel_tank, Fortification.Hangar_B,
              Fortification.Barracks_2, Fortification.Repair_workshop]
THREAT_NAMES = {"SA-8": "SA-8 GECKO", "SA-6": "SA-6 GAINFUL", "SA-11": "SA-11 GADFLY", "SA-15": "SA-15 GAUNTLET",
                "SA-10": "SA-10 GRUMBLE", "ZSU-23-4": "ZSU-23-4 SHILKA", "SA-9": "SA-9 GASKIN",
                "SA-13": "SA-13 GOPHER", "SA-19": "SA-19 GRISON"}


# --- briefing data -------------------------------------------------------------------------

@dataclass
class Briefing:
    codename: str
    dtg: str
    theatre: str
    scenario: str
    aircraft: str
    flight_size: int
    callsign: str
    mission_type: str
    complexity: str
    mission_statement: str
    loadout: str
    stores: list
    home: dict
    objective: dict
    enemy_field: str
    waypoints: list = field(default_factory=list)
    threats: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    enemy_air: list = field(default_factory=list)
    friendly_air: list = field(default_factory=list)
    support: list = field(default_factory=list)
    bullseye: dict = field(default_factory=dict)
    weather: dict = field(default_factory=dict)
    situation: list = field(default_factory=list)
    execution: list = field(default_factory=list)
    triggers: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    sortie: str = ""                                     # campaign sortie id, reported back in dcs.log
    tracked: dict = field(default_factory=dict)          # enemy site id -> units placed on it
    front: list = field(default_factory=list)            # airfields with their owner, for the map

    def to_dict(self) -> dict:
        return asdict(self)


def _ll(point) -> dict:
    ll = point.latlng()
    return {"lat": round(ll.lat, 5), "lon": round(ll.lng, 5)}


def _mhz(hz) -> str:
    return f"{hz / 1e6:.3f}"


def _dtg(when: dt.datetime, tz) -> str:
    """Military date-time group in Zulu. pydcs gives each terrain's local zone as a tzinfo."""
    offset = tz.utcoffset(when) if isinstance(tz, dt.tzinfo) else dt.timedelta(hours=tz or 0)
    return (when - offset).strftime("%d%H%MZ %b %y").upper()


def _route(group) -> list[dict]:
    out, prev = [], None
    for i, p in enumerate(group.points):
        row = {"n": i, "name": p.name or ("Start" if i == 0 else f"WP{i}"), **_ll(p.position),
               "alt_ft": round(p.alt * M_TO_FT, -2), "speed_kt": round(p.speed * MPS_TO_KT)}
        if prev is not None:
            row["brg"] = round(prev.position.heading_between_point(p.position)) % 360
            row["dist_nm"] = round(prev.position.distance_to_point(p.position) * M_TO_NM, 1)
        out.append(row)
        prev = p
    return out


class InlineDoScript(action.Action):
    """DO SCRIPT with the Lua written inline, the way the DCS 2.9 Mission Editor saves it.
    pydcs's DoScript stores a dictionary key instead, and DCS then runs the key name itself
    as code ("[string "DictKey_Translation_7"]:1: '=' expected near '<eof>'")."""
    predicate = "a_do_script"

    def __init__(self, code: str) -> None:
        super().__init__(InlineDoScript.predicate)
        self.code = code
        self.params.append(code)

    def dict(self):
        d = super().dict()
        d["text"] = self.code
        return d


# --- mission build -------------------------------------------------------------------------

class _Builder:
    """One mission's state while it is being built."""

    def __init__(self, spec: MissionSpec):
        self.spec = spec
        self.rng = random.Random(spec.seed)
        self.terrain = TERRAINS[spec.terrain]()
        self.m = Mission(self.terrain)
        self.m.start_time = dt.datetime.combine(dt.date.fromisoformat(spec.date),
                                                dt.time(int(spec.hour), int(spec.minute)))
        self.blue = countries.CombinedJointTaskForcesBlue()
        self.red = countries.CombinedJointTaskForcesRed()
        self.m.coalition["blue"].add_country(self.blue)
        self.m.coalition["red"].add_country(self.red)
        self.sc = find_scenario(spec.terrain, spec.scenario)
        (self.friendly_fields, self.enemy_fields, self.friendly_sites,
         self.enemy_sites, self.enemy_ships) = _sides(self.sc, spec.side)
        self.heli = is_helicopter(spec.aircraft)
        self.level = max(1, min(5, int(spec.complexity)))
        self.skill = SKILLS[spec.enemy_skill]
        self.home = self.terrain.airports[spec.airport]
        self.used_sites: set[tuple] = set()
        self.target_groups: list = []
        self.n = 0  # unique-name counter
        self.tracked_units: list[str] = []  # units on campaign sites, named "MB:<site>:..."

    def name(self, base: str) -> str:
        self.n += 1
        return f"{base} {self.n}"

    # geography ---------------------------------------------------------------------------

    def enemy_airfields(self):
        names = self.enemy_fields if self.enemy_fields else [n for n in self.terrain.airports if n != self.home.name]
        fields_ = [self.terrain.airports[n] for n in names if n in self.terrain.airports]
        return sorted(fields_, key=lambda a: self.home.position.distance_to_point(a.position))

    def sites(self, *roles) -> list[Point]:
        pts = []
        for role in roles:
            for x, y in self.enemy_sites.get(role, []):
                if (x, y) not in self.used_sites:
                    pts.append(Point(x, y, self.terrain))
        return pts

    def site_id(self, p: Point | None) -> str | None:
        """The campaign site at exactly this position, if any."""
        if p is None or not self.sc:
            return None
        return self.sc.red_site_ids.get(f"{round(p.x)},{round(p.y)}")

    def pick_site(self, roles, near_km, far_km, front: bool = False) -> Point | None:
        """An enemy land position of one of these roles, in range of home, preferring close ones.
        With `front`, positions in the campaign's front-line zones come first."""
        pts = [p for p in self.sites(*roles)
               if near_km * 1000 <= self.home.position.distance_to_point(p) <= far_km * 1000]
        if front and self.sc and self.sc.front_sites:
            pts = [p for p in pts if self.site_id(p) in self.sc.front_sites] or pts
        if not pts:
            return None
        pts.sort(key=lambda p: self.home.position.distance_to_point(p))
        p = self.rng.choice(pts[:5])
        self.used_sites.add((round(p.x), round(p.y)))
        return p

    def fallback_point(self, enemy_field) -> Point:
        """Without scenario positions: just short of the enemy field, toward home (on the field's land)."""
        hdg = enemy_field.position.heading_between_point(self.home.position)
        return enemy_field.position.point_from_heading(hdg, 2500)

    # units ---------------------------------------------------------------------------------

    def track(self, unit, site: str | None) -> None:
        """Name a unit placed on a campaign site so its death can be reported back."""
        if site:
            unit.name = f"MB:{site}:{self.n}:{len(self.tracked_units) + 1}"
            self.tracked_units.append(unit.name)
            self.b.tracked[site] = self.b.tracked.get(site, 0) + 1

    def ground(self, label: str, types, pos: Point, country=None, late=False, spread=60, site=None):
        country = country or self.red
        g = self.m.vehicle_group_platoon(country, self.name(label), types, pos, heading=self.rng.uniform(0, 360))
        for i, u in enumerate(g.units):  # spread units out so a site isn't one stacked pile
            u.position = pos.point_from_heading(self.rng.uniform(0, 360), spread * (i > 0) * (1 + i * 0.5))
            if country is self.red:
                u.skill = self.skill
            self.track(u, site)
        g.late_activation = late
        return g

    def sam(self, kind: str, pos: Point, late=False, threat_note=None, site=None):
        types, rng_m = (SAM_SITES.get(kind) or SHORAD[kind])
        g = self.ground(kind, types, pos, late=late, spread=120 if kind in SAM_SITES else 40, site=site)
        self.b.threats.append({"name": THREAT_NAMES.get(kind, kind), "kind": "SAM" if kind in SAM_SITES else "AAA",
                               **_ll(pos), "range_nm": round(rng_m * M_TO_NM, 1),
                               "note": threat_note or ("Ring is approximate." if kind in SAM_SITES
                                                       else "Short range. Ring is approximate.")})
        return g

    def bandits(self, label, ptype, count, start: Point, patrol_to: Point, alt=None, late=False):
        alt = alt or (500 if self.heli else 7000)
        g = self.m.patrol_flight(self.red, self.name(label), ptype, None, start, patrol_to,
                                 speed=250 if self.heli else 850, altitude=alt, group_size=count)
        for u in g.units:
            u.skill = self.skill
        _arm(g, ptype, "CAP", task.CAP)
        g.late_activation = late
        return g

    # triggers ------------------------------------------------------------------------------

    def message_rule(self, comment: str, conditions, text: str, then=None, seconds=20):
        t = TriggerOnce(comment=comment)
        for c in conditions:
            t.add_condition(c)
        t.add_action(action.MessageToCoalition(Coalition.Blue, self.m.string(text), seconds))
        for a in then or []:
            t.add_action(a)
        self.m.triggerrules.triggers.append(t)
        self.b.triggers.append(f"{comment}: {text}" if comment else text)

    def activate_on_entry(self, comment: str, groups, zone_center: Point, radius_m: float, text: str | None = None,
                          public: bool = False):
        zone = self.m.triggers.add_triggerzone(zone_center, radius=radius_m, hidden=True, name=self.name(comment))
        t = TriggerOnce(comment=comment)
        t.add_condition(condition.PartOfCoalitionInZone("blue", zone.id))
        for g in groups:
            t.add_action(action.ActivateGroup(g.id))
        if text:
            t.add_action(action.MessageToCoalition(Coalition.Blue, self.m.string(text), 15))
        self.m.triggerrules.triggers.append(t)
        if public:
            self.b.triggers.append(f"{comment} when you come within {round(radius_m * M_TO_NM)} nm of the objective.")
        return zone

    def success_when_dead(self, groups, text: str):
        if groups:
            self.message_rule("Mission success", [condition.GroupDead(g.id) for g in groups], text,
                              then=[action.SetFlag(1)], seconds=30)


def _ato_objective(B: "_Builder", spec: MissionSpec) -> Point | None:
    """The objective an ATO flight was planned against: its target site, or its station."""
    if spec.target_site and B.sc:
        for key, sid in B.sc.red_site_ids.items():
            if sid == spec.target_site:
                x, y = (float(v) for v in key.split(","))
                B.used_sites.add((round(x), round(y)))
                return Point(x, y, B.terrain)
    if spec.target_xy:
        return Point(spec.target_xy[0], spec.target_xy[1], B.terrain)
    return None


def add_ambient(B: "_Builder", flights: list[dict]) -> None:
    """The campaign's other flights at mission start: in the air where the campaign map shows
    them, flying the rest of their route; flights not yet launched appear over their base on time."""
    for a in flights:
        country = B.blue if a["side"] == "blue" else B.red
        ptype = unit_type(a["type"])
        mission = a["mission"] if a["mission"] in PAYLOAD_KEYWORDS else "CAP"
        maintask = _main_task(ptype, mission)
        alt, kmh = a["alt"], a["speed"] * 3.6
        g = B.m.flight_group_inflight(country, f"MBF:{a['id']}", ptype, Point(a["x"], a["y"], B.terrain), alt, kmh,
                                      maintask=maintask, group_size=a["count"])
        for i, u in enumerate(g.units):
            u.name = f"MBF:{a['id']}:{i + 1}"
            B.tracked_units.append(u.name)
            if country is B.red:
                u.skill = B.skill
        _arm(g, ptype, mission, maintask)
        for wp in a["route"]:
            p = Point(wp["x"], wp["y"], B.terrain)
            if wp["name"] == "LAND" and a["base"] in B.terrain.airports:
                g.land_at(B.terrain.airports[a["base"]])
                break
            w = g.add_waypoint(p, wp["alt"], wp["speed"] * 3.6, wp["name"])
            if wp["name"] == "STATION" and mission == "CAP":
                w.tasks.append(task.EngageTargets(max_distance=60000, targets=[task.Targets.All.Air]))
            elif wp["name"] == "TARGET" and mission == "SEAD":
                w.tasks.append(task.EngageTargets(max_distance=40000,
                                                  targets=[task.Targets.All.GroundUnits.AirDefence]))
            elif wp["name"] == "TARGET":
                w.tasks.append(task.EngageTargets(max_distance=15000, targets=[task.Targets.All.GroundUnits]))
        if a["delay"] > 0:
            g.late_activation = True
            t = TriggerOnce(comment=f"Launch {a['id']}")
            t.add_condition(condition.TimeAfter(a["delay"]))
            t.add_action(action.ActivateGroup(g.id))
            B.m.triggerrules.triggers.append(t)
        route = [_ll(Point(a["x"], a["y"], B.terrain))] + [_ll(Point(w["x"], w["y"], B.terrain)) for w in a["route"]]
        entry = {"name": f"{a['count']}x {a['type']} ({a['mission']})", "route": route}
        if a["side"] == "blue":
            B.b.friendly_air.append(entry)
        else:
            B.b.enemy_air.append(entry)


# Ground forces for a campaign site, by side and role (see add_ground).
_A = vehicles.Armor
GROUND_UNITS = {
    "red": {"sam": SAM_SITES["SA-6"][0], "ewr": [AD.X_1L13_EWR], "shorad": [AD.ZSU_23_4_Shilka, AD.Strela_10M3],
            "armor": [_A.T_72B, _A.BMP_2, _A.BTR_80], "missile": [vehicles.MissilesSS.Scud_B, vehicles.MissilesSS.Scud_B,
                                                               vehicles.Unarmed.Ural_375]},
    "blue": {"sam": [AD.Hawk_sr, AD.Hawk_tr, AD.Hawk_ln, AD.Hawk_ln], "ewr": [AD.Hawk_cwar],
             "shorad": [AD.Vulcan, AD.M1097_Avenger], "armor": [_A.M_1_Abrams, _A.M_2_Bradley, _A.M_2_Bradley],
             "missile": [vehicles.Artillery.MLRS, vehicles.Artillery.MLRS, vehicles.Unarmed.M_818]},
}
GROUND_THREATS = {"sam": ("SA-6 GAINFUL", 25_000), "shorad": ("SHORAD", 5_000)}


def add_ground(B: "_Builder", sites: list[dict]) -> None:
    """The campaign map's ground forces around the player's route (see war.ground_sites), both
    sides, each unit named after its site so destroying it counts in the campaign."""
    for site in sites:
        if (round(site["x"]), round(site["y"])) in B.used_sites:
            continue  # already placed by the mission itself
        B.used_sites.add((round(site["x"]), round(site["y"])))
        pos = Point(site["x"], site["y"], B.terrain)
        country = B.red if site["side"] == "red" else B.blue
        if site["role"] in ("structure", "farp"):
            for i in range(2):
                g = B.m.static_group(country, B.name(f"{site['side']} {site['role']}"), B.rng.choice(STRUCTURES),
                                     pos.point_from_heading(B.rng.uniform(0, 360), 60 * i), heading=B.rng.uniform(0, 360))
                B.track(g.units[0], site["id"])
            continue
        types = GROUND_UNITS[site["side"]].get(site["role"]) or GROUND_UNITS[site["side"]]["armor"]
        B.ground(f"{site['side']} {site['role']}", types, pos, country=country,
                 spread=120 if site["role"] == "sam" else 60, site=site["id"])
        if site["side"] == "red" and site["role"] in GROUND_THREATS:
            name, rng_m = GROUND_THREATS[site["role"]]
            B.b.threats.append({"name": name, "kind": "SAM" if site["role"] == "sam" else "AAA", **_ll(pos),
                                "range_nm": round(rng_m * M_TO_NM, 1), "note": f"Campaign site near {site['zone']}."})
        elif site["side"] == "red" and site["role"] == "ewr":
            B.b.threats.append({"name": "EWR (1L13)", "kind": "EWR", **_ll(pos), "range_nm": None,
                                "note": f"Early-warning radar near {site['zone']}."})


def build(spec: MissionSpec, ambient: list[dict] | None = None,
          ground: list[dict] | None = None) -> tuple[Mission, "Briefing"]:
    """Build a mission. `ambient`: the campaign's other flights (see war.ambient_flights);
    `ground`: campaign sites to place around the route (see war.ground_sites)."""
    B = _Builder(spec)
    m, rng, home, level = B.m, B.rng, B.home, B.level
    player_type = unit_type(spec.aircraft)
    heli = B.heli
    near_km, far_km = (8, 90) if heli else (40, 260)
    enemy_fields = B.enemy_airfields()
    if not enemy_fields:
        raise RuntimeError("This scenario has no enemy airfields.")
    in_range = [a for a in enemy_fields if near_km * 1000 <= home.position.distance_to_point(a.position) <= far_km * 1000]
    enemy_field = rng.choice((in_range or enemy_fields)[:3])
    cruise_alt = 150 if heli else 6000
    cruise_speed = 200 if heli else 800
    mt = spec.mission_type if spec.mission_type in mission_types_for(spec.aircraft) else "Strike"

    # Objective position, by mission type, on the enemy side's own land.
    roles = {"Strike": ("structure", "missile", "armor", "sam"), "CAS": ("armor", "shorad", "structure"),
             "SEAD": ("sam", "ewr", "shorad"), "CAP": ("ewr", "sam", "shorad", "armor"),
             "Intercept": (), "Free flight": ("armor", "shorad", "structure", "sam")}[mt]
    objective = _ato_objective(B, spec) if spec.flight else None
    if objective is None and roles:
        objective = B.pick_site(roles, near_km, far_km, front=True)
    if objective is None and roles and heli:
        # Nothing of the right kind in helicopter range: take the nearest enemy position of any kind.
        objective = B.pick_site(roles + ("armor", "shorad", "structure", "sam", "ewr", "missile"), 3, 150, front=True)
    if objective is None and mt != "Intercept":
        objective = B.fallback_point(enemy_field)
    if mt == "Intercept":
        # The bombers' target is the home field; the player meets them on the threat axis.
        objective = home.position.point_from_heading(home.position.heading_between_point(enemy_field.position),
                                                     min(80000, home.position.distance_to_point(enemy_field.position) * 0.5))
    heading_out = home.position.heading_between_point(objective)
    distance = home.position.distance_to_point(objective)

    callsign_unit = None
    maintask = _main_task(player_type, mt)
    start = START_TYPES[spec.start]
    if start is None:
        spawn = (Point(spec.spawn_xy[0], spec.spawn_xy[1], B.terrain) if spec.spawn_xy
                 else home.position.point_from_heading(heading_out, 10000))
        player = m.flight_group_inflight(B.blue, "Player Flight", player_type, spawn, cruise_alt,
                                         cruise_speed, maintask=maintask, group_size=spec.flight_size)
        player.points[0].name = "Spawn"
    else:
        player = m.flight_group_from_airport(B.blue, "Player Flight", player_type, home, maintask=maintask,
                                             start_type=start, group_size=spec.flight_size)
        player.points[0].name = home.name
    player.units[0].set_player()
    if spec.flight:  # the campaign tracks this flight's losses too
        for i, u in enumerate(player.units):
            u.name = f"MBF:{spec.flight}:{i + 1}"
            B.tracked_units.append(u.name)
    for wingman in player.units[1:]:
        wingman.skill = Skill.High
    loadout, stores = _arm(player, player_type, mt, maintask, spec.loadout)
    callsign_unit = player.units[0]
    callsign = callsign_unit.callsign_dict.get("name", "") if hasattr(callsign_unit, "callsign_dict") else ""
    callsign = f"{callsign[:-2].upper()} {callsign[-2]}" if callsign[-2:].isdigit() else (callsign or "PLAYER")
    obj_brg, obj_nm = round(heading_out) % 360, round(distance * M_TO_NM)

    obj_site = B.site_id(objective)
    B.b = b = Briefing(
        codename=f"{rng.choice(CODENAME_A)} {rng.choice(CODENAME_B)}",
        dtg=_dtg(m.start_time, getattr(B.terrain, "utc_offset", None)),
        theatre=spec.terrain, scenario=B.sc.name if B.sc else "None",
        aircraft=spec.aircraft, flight_size=spec.flight_size, callsign=callsign, mission_type=mt,
        complexity=f"{level} ({COMPLEXITY_NAMES[level]})", mission_statement="", loadout=loadout, stores=stores,
        home={"name": home.name, **_ll(home.position),
              "uhf": _mhz(home.atc_radio.uhf_hz) if home.atc_radio else None,
              "vhf": _mhz(home.atc_radio.vhf_high_hz) if home.atc_radio else None,
              "tacan": home.tacan, "runways": [r.name for r in home.runways]},
        objective={"name": "OBJECTIVE", **_ll(objective), "brg": obj_brg, "dist_nm": obj_nm},
        enemy_field=enemy_field.name,
    )
    where = f"{obj_nm} nm {obj_brg:03d}°T of {home.name}"
    flight = f"{callsign} flight ({spec.flight_size}x {spec.aircraft})"

    # Route: push, IP short of the objective, objective, recovery. Joining a flight in the
    # air drops the points already behind it.
    ip = home.position.point_from_heading(heading_out, max(distance - (8000 if heli else 25000), distance * 0.6))
    push = home.position.point_from_heading(heading_out, distance * 0.35)
    to_go = player.points[0].position.distance_to_point(objective) if spec.spawn_xy else distance
    if not spec.rtb:
        if to_go > push.distance_to_point(objective):
            player.add_waypoint(push, cruise_alt, cruise_speed, "PUSH")
        if mt in ("Strike", "CAS", "SEAD") and to_go > ip.distance_to_point(objective):
            player.add_waypoint(ip, cruise_alt, cruise_speed, "IP")
        player.add_waypoint(objective, cruise_alt, cruise_speed, "OBJECTIVE")
    player.land_at(home).name = "RECOVERY"

    # ---- objective content ----
    defended_point = objective
    if mt == "Strike":
        n = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6}[level]
        structures = []
        for i in range(n):
            pos = objective.point_from_heading(rng.uniform(0, 360), rng.uniform(80, 400))
            g = m.static_group(B.red, B.name("Target"), rng.choice(STRUCTURES), pos, heading=rng.uniform(0, 360))
            B.track(g.units[0], obj_site)
            structures.append(g)
        B.target_groups += structures
        b.targets.append({"name": f"Military installation ({n} structures)", **_ll(objective),
                          "units": sorted({g.units[0].type for g in structures})})
        if level >= 4:  # a second aimpoint
            second = B.pick_site(("structure", "missile", "armor"), near_km, far_km)
            if second:
                g2 = B.ground("Missile battery", [vehicles.MissilesSS.Scud_B, vehicles.MissilesSS.Scud_B,
                                                  vehicles.Unarmed.Ural_375], second,
                               site=B.site_id(second))
                B.target_groups.append(g2)
                b.targets.append({"name": "Missile battery (secondary)", **_ll(second), "units": [u.type for u in g2.units]})
        b.mission_statement = f"{flight} destroys the military installation at the objective, {where}, and recovers to {home.name}."
        b.situation.append(f"The enemy operates a military installation near {enemy_field.name}.")
    elif mt == "CAS":
        sizes = {1: 3, 2: 4, 3: 5, 4: 6, 5: 8}[level]
        enemy = [vehicles.Armor.T_72B, vehicles.Armor.BMP_2, vehicles.Armor.BTR_80, vehicles.Armor.T_72B3,
                 vehicles.Armor.BMP_3, vehicles.Armor.T_90]
        red_armour = B.ground("Enemy armour", [rng.choice(enemy) for _ in range(sizes)], objective, spread=90,
                              site=obj_site)
        hdg_home = objective.heading_between_point(home.position)
        friendly_pos = objective.point_from_heading(hdg_home, 3000)
        own = [Point(x, y, B.terrain) for x, y in B.friendly_sites.get("armor", [])]
        own = [p for p in own if 1500 <= p.distance_to_point(objective) <= 12000]
        if own:  # the scenario's own friendly front-line position, so it's on land
            friendly_pos = min(own, key=lambda p: p.distance_to_point(objective))
            hdg_home = objective.heading_between_point(friendly_pos)
        blue_armour = B.ground("Friendly troops", [vehicles.Armor.M_1_Abrams, vehicles.Armor.M_2_Bradley,
                                                   vehicles.Armor.M_2_Bradley], friendly_pos, country=B.blue, spread=90)
        blue_armour.add_waypoint(objective.point_from_heading(hdg_home, 1200), dcs.point.PointAction.OffRoad)
        B.target_groups.append(red_armour)
        b.targets.append({"name": f"Enemy armour ({sizes} vehicles)", **_ll(objective), "units": [u.type for u in red_armour.units]})
        b.friendly_air.append({"name": "Friendly troops in contact", **_ll(friendly_pos), "route": []})
        b.mission_statement = (f"{flight} provides close air support to friendly troops in contact at the objective, "
                               f"{where}, destroying the enemy armour, and recovers to {home.name}.")
        b.situation.append(f"An enemy armoured group is in contact with friendly armour {obj_nm} nm from {home.name}. "
                           f"Friendlies are {round(friendly_pos.distance_to_point(objective) * M_TO_NM, 1)} nm from the "
                           "enemy and closing: confirm before releasing.")
    elif mt == "SEAD":
        site = SEAD_TARGET[level]
        groups = [B.sam(k, objective.point_from_heading(rng.uniform(0, 360), 600 if i else 0),
                        threat_note="PRIMARY TARGET. Ring is approximate.", site=obj_site)
                  for i, k in enumerate(site)]
        B.target_groups += groups
        b.targets.append({"name": f"{' + '.join(THREAT_NAMES[k] for k in site)} site", **_ll(objective),
                          "units": [u.type for g in groups for u in g.units]})
        b.mission_statement = (f"{flight} suppresses and destroys the {' / '.join(THREAT_NAMES[k] for k in site)} site "
                               f"at the objective, {where}, opening the corridor for follow-on strikes, and recovers.")
        b.situation.append(f"An enemy surface-to-air missile site covers the approaches to {enemy_field.name}.")
        defended_point = None  # the target is the defence
    elif mt == "CAP" and spec.flight:
        b.mission_statement = f"{flight} holds CAP at the station, {where}, and engages enemy aircraft entering the area."
        b.situation.append("Enemy flights in the area are shown on the map; expect more from the enemy's ATO.")
        defended_point = None
        B.target_groups = []
    elif mt == "CAP":
        waves = []
        for ptype, pairs in CAP_BANDITS[level]:
            for _ in range(pairs):
                waves.append(B.bandits(f"Bandits {ptype.id}", ptype, 2, enemy_field.position, objective))
        B.target_groups += waves
        for g in waves:
            b.enemy_air.append({"name": f"2x {g.units[0].type}", "route": [_ll(p.position) for p in g.points]})
            b.threats.append({"name": f"2x {g.units[0].type}", "kind": "AIR", **_ll(objective), "range_nm": None,
                              "note": f"CAP from {enemy_field.name} toward the objective. Skill: {spec.enemy_skill}."})
        if level >= 4:  # second wave launches when the first is destroyed
            ptype = CAP_BANDITS[level][-1][0]
            wave2 = B.bandits("Second wave", ptype, 2, enemy_field.position, objective, late=True)
            t = TriggerOnce(comment="Second wave")
            for g in waves:
                t.add_condition(condition.GroupDead(g.id))
            t.add_action(action.ActivateGroup(wave2.id))
            if spec.awacs:
                t.add_action(action.MessageToCoalition(Coalition.Blue, m.string(
                    f"OVERLORD: new group airborne from {enemy_field.name}, 2 contacts."), 20))
            m.triggerrules.triggers.append(t)
            b.notes.append("Expect the enemy to reinforce once its first CAP is engaged.")
            waves_for_success = waves + [wave2]
        else:
            waves_for_success = waves
        B.target_groups = waves_for_success
        b.mission_statement = f"{flight} establishes air superiority over the objective, {where}, destroys the enemy CAP, and recovers."
        b.situation.append(f"Enemy fighters from {enemy_field.name} are contesting the airspace near the objective.")
        defended_point = None if level < 3 else objective
    elif mt == "Intercept":
        bomber_list, escort_pairs = BOMBERS[level]
        far = enemy_field.position.point_from_heading(enemy_field.position.heading_between_point(home.position), 10000)
        raid = []
        for ptype, count in bomber_list:
            g = m.flight_group_inflight(B.red, B.name(f"Raid {ptype.id}"), ptype, far, 8000, 750,
                                        maintask=task.GroundAttack, group_size=count)
            wp = g.add_waypoint(home.position, 8000, 750, "Target")
            wp.tasks.append(task.Bombing(home.position, group_attack=True))
            for u in g.units:
                u.skill = B.skill
            raid.append(g)
        escorts = []
        for _ in range(escort_pairs):
            e = m.escort_flight(B.red, B.name("Raid escort"), planes.MiG_29S, None, raid[0], group_size=2)
            for u in e.units:
                u.skill = B.skill
            _arm(e, planes.MiG_29S, "CAP", task.CAP)
            escorts.append(e)
        B.target_groups = raid
        for g in raid + escorts:
            b.enemy_air.append({"name": f"{len(g.units)}x {g.units[0].type}", "route": [_ll(p.position) for p in g.points]})
        b.threats.append({"name": " + ".join(f"{len(g.units)}x {g.units[0].type}" for g in raid), "kind": "AIR",
                          **_ll(objective), "range_nm": None,
                          "note": f"Bomber raid from {enemy_field.name} inbound to {home.name}."
                                  + (f" Escorted by {2 * escort_pairs} fighters." if escort_pairs else "")})
        zone = m.triggers.add_triggerzone(home.position, radius=20000, hidden=True, name="Home defence")
        fail = TriggerOnce(comment="Raid reaches home")
        fail.add_condition(condition.PartOfCoalitionInZone("red", zone.id))
        fail.add_action(action.MessageToCoalition(Coalition.Blue, m.string(
            f"The raid has reached {home.name}. Mission failed."), 30))
        m.triggerrules.triggers.append(fail)
        b.triggers.append(f"Mission failed if the raid gets within 11 nm of {home.name}.")
        b.mission_statement = (f"{flight} intercepts and destroys the bomber raid inbound to {home.name} from "
                               f"{enemy_field.name} before it reaches the field.")
        b.situation.append(f"An enemy bomber raid is inbound from {enemy_field.name}. Expect it within minutes.")
        defended_point = None
    else:  # Free flight
        b.mission_statement = f"{flight} flies a familiarisation route to the objective, {where}, and recovers."
        b.situation.append("No enemy activity is expected. Area is permissive.")
        defended_point = None
        B.target_groups = []

    # ---- defences, enemy air and scrambles, by complexity ----
    if mt != "Free flight":
        top = 3 if heli else 5
        lvl = min(level, top)
        if defended_point is not None:
            for kind in POINT_DEFENCE[lvl]:
                B.sam(kind, defended_point.point_from_heading(rng.uniform(0, 360), rng.uniform(400, 1500)))
        for kind in AREA_SAMS[lvl]:
            pos = B.pick_site(("sam", "shorad"), max(5, near_km / 2), distance * M_TO_NM * 1.852 + 30)
            if pos is not None:
                B.sam(kind, pos, site=B.site_id(pos))
        if level >= 4 and not heli:
            pos = B.pick_site(("ewr",), 0, far_km + 50)
            if pos is not None:
                B.ground("EWR", [AD.X_1L13_EWR], pos, site=B.site_id(pos))
                b.threats.append({"name": "EWR (1L13)", "kind": "EWR", **_ll(pos), "range_nm": None,
                                  "note": "Early-warning radar: the enemy will see you coming."})
        if level >= 3 and mt in ("Strike", "CAS", "SEAD"):
            popup_pos = objective.point_from_heading(rng.uniform(0, 360), 1200)
            types = [AD.Strela_10M3, AD.ZSU_23_4_Shilka] if level < 5 else [AD.Tor_9A331, AD.ZSU_23_4_Shilka]
            popup = B.ground("Pop-up air defence", types, popup_pos, late=True, spread=50)
            B.activate_on_entry("Pop-up threat", [popup], objective, (12 if heli else 20) * 1852)
            b.notes.append("Intelligence assesses mobile short-range air defence may move into the target area.")
        if level >= 3 and mt != "CAP" and not (heli and level < 5):
            qra_field = min(enemy_fields, key=lambda a: a.position.distance_to_point(objective))
            ptype = planes.MiG_29S if level < 5 else planes.Su_27
            qra = B.bandits("QRA", ptype, 2, qra_field.position, objective, late=True)
            text = f"OVERLORD: enemy fighters scrambling from {qra_field.name}." if spec.awacs else None
            B.activate_on_entry("Enemy scramble", [qra], objective, 40 * 1852, text, public=True)
            b.threats.append({"name": f"2x {ptype.id} (alert)", "kind": "AIR", **_ll(qra_field.position),
                              "range_nm": None, "note": f"On alert at {qra_field.name}; launches when you approach."})
        if level >= 5 and mt not in ("CAP", "Intercept") and not heli and not spec.flight:
            pair = B.bandits("Enemy CAP", planes.Su_27, 2, enemy_field.position, objective)
            b.enemy_air.append({"name": "2x Su-27", "route": [_ll(p.position) for p in pair.points]})
            b.threats.append({"name": "2x Su-27", "kind": "AIR", **_ll(objective), "range_nm": None,
                              "note": "Standing enemy CAP over the objective area."})

    # ---- friendly AI flights ----
    if level >= 3 and mt in ("Strike", "CAS", "SEAD", "Intercept") and not heli and not spec.flight:
        cap = m.patrol_flight(B.blue, "Friendly CAP", planes.F_15C, None,
                              home.position.point_from_heading(heading_out, distance * 0.3),
                              home.position.point_from_heading(heading_out, distance * 0.7),
                              speed=850, altitude=8000, group_size=2)
        _arm(cap, planes.F_15C, "CAP", task.CAP)
        b.friendly_air.append({"name": "2x F-15C (friendly CAP)", "route": [_ll(p.position) for p in cap.points]})
        b.execution.append("A friendly F-15C pair (callsign set by DCS) holds CAP along the ingress route.")
    if level >= 4 and mt in ("Strike", "CAS") and not heli and not spec.flight:
        sead = m.flight_group_inflight(B.blue, "Friendly SEAD", planes.F_16C_50,
                                       home.position.point_from_heading(heading_out, 15000), 7000, 850,
                                       maintask=task.SEAD, group_size=2)
        sead.add_waypoint(objective, 7000, 850, "SEAD target area")
        sead.points[-1].tasks.append(task.EngageTargets(max_distance=60000, targets=[task.Targets.All.GroundUnits.AirDefence]))
        sead.land_at(home)
        _arm(sead, planes.F_16C_50, "SEAD", task.SEAD)
        b.friendly_air.append({"name": "2x F-16C (friendly SEAD)", "route": [_ll(p.position) for p in sead.points]})
        b.execution.append("A friendly F-16C SEAD pair pushes ahead of you to suppress air defences at the objective.")

    # ---- success messages ----
    success_text = {"Strike": "Target destroyed. RTB.", "CAS": "Enemy armour destroyed. Friendlies report the area clear. RTB.",
                    "SEAD": "SAM site destroyed. Corridor is open. RTB.", "CAP": "Splash all bandits. Airspace is ours. RTB.",
                    "Intercept": "Raid destroyed. Good work. RTB."}.get(mt)
    if success_text:
        B.success_when_dead(B.target_groups, success_text)

    # ---- support ----
    behind = home.position.point_from_heading(heading_out + 180, 40000)
    if spec.awacs:
        aw = m.awacs_flight(B.blue, "AWACS", planes.E_3A, None, behind, race_distance=60000,
                            heading=round(heading_out + 90) % 360, altitude=9000, frequency=251)
        b.support.append({"role": "AWACS", "callsign": "OVERLORD", "type": "E-3A", "freq": "251.000",
                          "tacan": None, "alt_ft": round(9000 * M_TO_FT, -2),
                          "track": [_ll(p.position) for p in aw.points[1:3]]})
    if spec.tanker and not heli:
        probe = spec.aircraft in PROBE_AIRCRAFT
        tanker_type = planes.KC135MPRS if probe else planes.KC_135
        tk = m.refuel_flight(B.blue, "Tanker", tanker_type, None, behind.point_from_heading(heading_out + 90, 20000),
                             race_distance=50000, heading=round(heading_out + 90) % 360, altitude=7000,
                             frequency=252, tacanchannel="52X")
        b.support.append({"role": f"Tanker ({'drogue' if probe else 'boom'})", "callsign": "TEXACO" if probe else "ARCO",
                          "type": tanker_type.id, "freq": "252.000", "tacan": "52X",
                          "alt_ft": round(7000 * M_TO_FT, -2), "track": [_ll(p.position) for p in tk.points[1:3]]})

    wx = WEATHER[spec.weather]
    if wx is not None:
        m.weather.clouds_preset = wx.value
        m.weather.clouds_base = 2500
    b.weather = {"summary": spec.weather,
                 "cloud_base_ft": round(2500 * M_TO_FT, -2) if wx is not None else None,
                 "visibility": "10+ km", "temp_c": round(m.weather.season_temperature)}

    if ground:
        add_ground(B, ground)
        b.notes.append(f"Campaign ground forces: {len(ground)} positions along your route are in the mission, "
                       "both sides; enemy SAMs and radars covering the route are among them.")
    if spec.spawn_xy:
        b.notes.append("You join this flight in the air, where the campaign map shows it."
                       + (" Its target is behind it: recover to base." if spec.rtb else ""))
    if ambient:
        add_ambient(B, ambient)
        airborne = sum(1 for a in ambient if a["delay"] == 0)
        b.notes.append(f"Campaign ATO: {airborne} other flights are airborne at mission start and "
                       f"{len(ambient) - airborne} more launch during the mission.")
    if B.sc:
        for n in B.sc.blue_fields:
            if n in B.terrain.airports:
                B.terrain.airports[n].set_blue()
        for n in B.sc.red_fields:
            if n in B.terrain.airports:
                B.terrain.airports[n].set_red()
        b.front = [{"name": n, "side": side, **_ll(B.terrain.airports[n].position)}
                   for side, names in (("blue", B.sc.blue_fields), ("red", B.sc.red_fields))
                   for n in names if n in B.terrain.airports]
    if B.tracked_units:
        b.sortie = campaign.new_sortie_id()
        t = TriggerStart(comment="Campaign results")
        t.add_action(InlineDoScript(campaign.results_script(b.sortie, B.tracked_units)))
        m.triggerrules.triggers.append(t)
        b.notes.append("Campaign mission: enemy losses at the objective and nearby sites are logged and "
                       "applied to the campaign after the flight.")

    m.coalition["blue"].bullseye = {"x": home.position.x, "y": home.position.y}
    b.bullseye = {"name": "BULLSEYE", **_ll(home.position)}
    b.waypoints = _route(player)
    held = (f"Friendly forces hold {len(B.sc.blue_fields)} airfields, the enemy {len(B.sc.red_fields)}. "
            f"Operations from {home.name}. ") if B.sc else f"Friendly forces hold {home.name}. "
    b.situation.append(held +
                       ("AWACS OVERLORD provides picture calls. " if spec.awacs else "No AWACS support. ") +
                       ("Tanker available." if any("Tanker" in s["role"] for s in b.support) else ""))
    if spec.aircraft == "F-15ESE" and int(spec.date[:4]) < 1998:
        b.notes.append("Mission is dated before 1998: the Modern F-15E JTIDS/SIT datalink will be inactive.")
    if B.sc is None:
        b.notes.append("No scenario: objectives are placed next to an enemy airfield and may not match real territory.")

    m.set_sortie_text(f"OPERATION {b.codename}")
    m.set_description_text(" ".join(b.situation))
    m.set_description_bluetask_text(b.mission_statement)
    return m, b


def build_with_retries(spec: MissionSpec, locks: set[str], owned: list[str], terrains: list[str],
                       attempts: int = 6) -> tuple[MissionSpec, Mission, Briefing]:
    """Build; if the home field has no parking for this flight, reroll unpinned fields and retry."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            mission, briefing = build(spec)
            return spec, mission, briefing
        except NoParkingSlotError as e:
            last_error = e
            if {"airport", "aircraft", "flight_size"} <= locks:
                break
            spec = randomize(asdict(spec), (locks - {"airport", "seed"}) if "airport" not in locks else locks - {"seed"},
                             owned, terrains)
    raise RuntimeError(f"No parking for this flight at {spec.airport}: {last_error}. "
                       "Unpin the airfield or reduce the flight size.")


def default_filename(spec: MissionSpec, briefing: Briefing | None = None) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    name = briefing.codename.title().replace(" ", "") if briefing else spec.mission_type.replace(" ", "")
    return f"{name}_{spec.aircraft}_{spec.mission_type.replace(' ', '')}_{spec.terrain}_{stamp}.miz"
