"""Build a single-player .miz plus the data for its briefing, using pydcs.

Layout of every mission:
  * The player's flight (blue, Combined Joint Task Forces Blue) at a chosen
    home airfield, or in the air near it.
  * An objective 60-200 km away (25-70 km for helicopters), near an enemy
    airfield (red, CJTF Red):
      - Free flight: nothing hostile, just a round-trip route.
      - CAP:         an enemy fighter pair patrolling toward the player.
      - Strike:      an armour column guarded by short-range air defence.
  * Optional friendly AWACS orbiting behind the home field, and a tanker
    matched to the player's refuelling system.
Everything is era-neutral (1980s+ hardware) so it fits any mission date.

randomize() fills every field the user hasn't locked; build() turns a full
MissionSpec into a pydcs Mission and a Briefing (plain data for the UI).
"""
from __future__ import annotations

import datetime as dt
import random
import re
from dataclasses import asdict, dataclass, field, fields

import dcs
from dcs import countries, planes, task, vehicles
from dcs.cloud_presets import Clouds
from dcs.mission import Mission, StartType
from dcs.terrain.terrain import NoParkingSlotError
from dcs.unit import Skill

from .inventory import TERRAINS

START_TYPES = {
    "Runway (engines running)": StartType.Runway,
    "Ramp, hot": StartType.Warm,
    "Ramp, cold": StartType.Cold,
    "In the air": None,
}
MISSION_TYPES = ["Free flight", "CAP", "Strike"]
WEATHER = {"Clear": None, "Scattered clouds": Clouds.LightScattered2, "Overcast": Clouds.Overcast1}
SKILLS = {"Average": Skill.Average, "Good": Skill.Good, "High": Skill.High, "Excellent": Skill.Excellent}

# Receivers that refuel from a drogue (probe) rather than a boom.
PROBE_AIRCRAFT = {"FA-18C_hornet", "F-14A-95-GR", "F-14A-135-GR-Early", "F-14A-135-GR", "F-14B",
                  "Su-33", "J-11A"}

# Approximate engagement ranges used for the briefing's threat rings (metres).
THREAT_RANGES = {"Osa 9A33 ln": 10_300, "ZSU-23-4 Shilka": 2_500}

M_TO_FT = 3.28084
MPS_TO_KT = 1.94384
M_TO_NM = 1 / 1852

CODENAME_A = ["IRON", "SILENT", "BROKEN", "COLD", "RED", "DESERT", "NIGHT", "STEEL", "GRANITE", "HOLLOW",
              "AMBER", "COPPER", "SHADOW", "WINTER", "BLACK", "ROLLING", "BRIGHT", "QUIET", "OPEN", "FALLEN"]
CODENAME_B = ["ANVIL", "LANCE", "SABRE", "HARBOR", "THUNDER", "TALON", "RIDGE", "VIGIL", "HAMMER", "SPEAR",
              "CANYON", "FALCON", "TEMPEST", "GAUNTLET", "WARDEN", "BASTION", "RAPIER", "SENTRY", "COMPASS", "HALO"]


@dataclass
class MissionSpec:
    terrain: str
    aircraft: str                  # DCS type id, e.g. "F-15ESE"
    airport: str                   # home airfield
    mission_type: str = "CAP"
    start: str = "Runway (engines running)"
    flight_size: int = 2
    date: str = "2012-06-01"       # ISO date; kept as text so it round-trips through JSON
    hour: int = 9
    weather: str = "Clear"
    enemy_skill: str = "Good"
    loadout: str = ""              # stored loadout name; "" = pick one to suit the mission type
    awacs: bool = True
    tanker: bool = False
    seed: int = 0                  # drives every placement choice; same seed = same mission

    @classmethod
    def from_dict(cls, d: dict) -> "MissionSpec":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


FIELD_NAMES = [f.name for f in fields(MissionSpec)]


def unit_type(type_id: str):
    return dcs.helicopters.helicopter_map.get(type_id) or planes.plane_map[type_id]


def is_helicopter(type_id: str) -> bool:
    return type_id in dcs.helicopters.helicopter_map


def airports(terrain_name: str, helicopter: bool = True) -> list[str]:
    """Airfields the aircraft can start from: helicopters anywhere, aeroplanes need a runway."""
    t = TERRAINS[terrain_name]()
    return sorted(n for n, a in t.airports.items()
                  if helicopter or (a.runways and any(p.airplanes for p in a.parking_slots)))


# --- randomizer --------------------------------------------------------------------------

def randomize(values: dict, locks: set[str], owned: list[str], terrains: list[str],
              rng: random.Random | None = None, modules: dict[str, str] | None = None) -> MissionSpec:
    """Keep locked fields as given and roll everything else. Locking the airfield
    implies its map; aircraft and airfield are kept compatible (no jet on a helipad).
    `modules` maps type id -> module folder so each module is equally likely
    (the F-14 module alone has four variants)."""
    rng = rng or random.Random()
    v = dict(values)
    if "airport" in locks:
        locks = locks | {"terrain"}

    def roll(name, choices):
        if name not in locks or v.get(name) in (None, ""):
            v[name] = rng.choice(list(choices))

    roll("terrain", terrains)
    if "aircraft" not in locks:
        candidates = owned
        if "airport" in locks:  # a locked helipad rules out aeroplanes
            if v["airport"] not in airports(v["terrain"], helicopter=False):
                candidates = [a for a in owned if is_helicopter(a)] or owned
        if modules:
            by_module: dict[str, list[str]] = {}
            for a in candidates:
                by_module.setdefault(modules.get(a, a), []).append(a)
            v["aircraft"] = rng.choice(rng.choice(list(by_module.values())))
        else:
            v["aircraft"] = rng.choice(candidates)
    valid_fields = airports(v["terrain"], helicopter=is_helicopter(v["aircraft"]))
    if "airport" not in locks or v.get("airport") not in TERRAINS[v["terrain"]]().airports:
        v["airport"] = rng.choice(valid_fields)
    roll("mission_type", MISSION_TYPES)
    roll("start", list(START_TYPES))
    roll("flight_size", [1, 2, 2, 2, 3, 4])
    if "date" not in locks or not v.get("date"):
        start, end = dt.date(2000, 1, 1), dt.date(2022, 12, 31)
        v["date"] = (start + dt.timedelta(days=rng.randrange((end - start).days))).isoformat()
    roll("hour", range(6, 17))
    roll("weather", ["Clear", "Clear", "Scattered clouds", "Overcast"])
    roll("enemy_skill", list(SKILLS))
    names = loadouts(v["aircraft"])
    if "loadout" not in locks or v.get("loadout") not in names:
        suited = [n for n in names if _suits(n, v["mission_type"])]
        v["loadout"] = rng.choice(suited or names) if names else ""
    roll("awacs", [True, True, True, False])
    roll("tanker", [True, False])
    if "seed" not in locks or not v.get("seed"):
        v["seed"] = rng.randrange(1, 2**31)
    v["flight_size"] = int(v["flight_size"])
    v["hour"] = int(v["hour"])
    return MissionSpec.from_dict(v)


# --- loadouts ------------------------------------------------------------------------------

# Loadout names to look for, in order of preference. DCS's own files use task
# words ("CAP", "STRIKE"); Liberation's use "Liberation BARCAP" etc.
PAYLOAD_KEYWORDS = {
    # Flaming Cliffs loadouts are named by their weapons, hence the weapon names.
    "CAP": ["BARCAP", "TARCAP", "CAP", "FIGHTER SWEEP", "ESCORT", "A-A", "AIR-TO-AIR",
            "AIM-120", "R-77", "R-27", "AIM-7", "R-73", "AIM-9"],
    "Strike": ["CAS", "BAI", "STRIKE", "GROUND ATTACK", "DEAD", "SEAD", "OCA", "KH-", "MK-82", "FAB-", "RBK-",
               "GBU-", "S-8"],
    "Free flight": ["FERRY", "BARCAP", "CAP"],
}


def _main_task(utype, mission_type: str):
    wanted = {"CAP": [task.CAP, task.FighterSweep, task.Intercept],
              "Strike": [task.CAS, task.GroundAttack, task.PinpointStrike],
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
        counts[rack.group(1) if rack else name] = counts.get(rack.group(1) if rack else name, 0) +             (int(rack.group(2)) if rack else 1)
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
                pylons = payload["pylons"]
                return [(pylons[k]["num"], {"clsid": pylons[k]["CLSID"]}) for k in pylons], name
    return utype.loadout(maintask), "Default"


def _arm(group, utype, mission_type: str, maintask, chosen: str = "") -> tuple[str, list[str]]:
    pylons, name = _payload(utype, mission_type, maintask, chosen)
    for unit in group.units:
        for pylon in pylons or []:
            unit.load_pylon(pylon)
    return (name if pylons else "Guns only"), stores_summary(pylons)


# --- briefing data -------------------------------------------------------------------------

@dataclass
class Briefing:
    codename: str
    dtg: str
    theatre: str
    aircraft: str
    flight_size: int
    callsign: str
    mission_type: str
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
    support: list = field(default_factory=list)
    bullseye: dict = field(default_factory=dict)
    weather: dict = field(default_factory=dict)
    situation: list = field(default_factory=list)
    notes: list = field(default_factory=list)

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


# --- mission build -------------------------------------------------------------------------

def _pick_enemy_airport(terrain, home, rng, near_km, far_km):
    candidates = [a for a in terrain.airports.values()
                  if a is not home and near_km * 1000 <= home.position.distance_to_point(a.position) <= far_km * 1000]
    if not candidates:  # small or sparse maps: take the farthest field there is
        candidates = sorted((a for a in terrain.airports.values() if a is not home),
                            key=lambda a: home.position.distance_to_point(a.position))[-1:]
    return rng.choice(candidates)


def build(spec: MissionSpec) -> tuple[Mission, Briefing]:
    rng = random.Random(spec.seed)
    terrain = TERRAINS[spec.terrain]()
    m = Mission(terrain)
    m.start_time = dt.datetime.combine(dt.date.fromisoformat(spec.date), dt.time(int(spec.hour), 0))

    blue = countries.CombinedJointTaskForcesBlue()
    red = countries.CombinedJointTaskForcesRed()
    m.coalition["blue"].add_country(blue)
    m.coalition["red"].add_country(red)

    player_type = unit_type(spec.aircraft)
    heli = is_helicopter(spec.aircraft)
    home = terrain.airports[spec.airport]
    near_km, far_km = (25, 70) if heli else (60, 200)
    enemy_field = _pick_enemy_airport(terrain, home, rng, near_km, far_km)
    heading_out = home.position.heading_between_point(enemy_field.position)
    distance = home.position.distance_to_point(enemy_field.position)
    # Objective sits short of the enemy field so the player isn't flying over its runway.
    objective = home.position.point_from_heading(heading_out, distance * 0.8)
    cruise_alt = 300 if heli else 6000
    cruise_speed = 200 if heli else 800

    maintask = _main_task(player_type, spec.mission_type)
    start = START_TYPES[spec.start]
    if start is None:
        spawn = home.position.point_from_heading(heading_out, 10000)
        player = m.flight_group_inflight(blue, "Player Flight", player_type, spawn, cruise_alt,
                                         cruise_speed, maintask=maintask, group_size=spec.flight_size)
        player.points[0].name = "Spawn"
    else:
        player = m.flight_group_from_airport(blue, "Player Flight", player_type, home, maintask=maintask,
                                             start_type=start, group_size=spec.flight_size)
        player.points[0].name = home.name
    player.units[0].set_player()
    for wingman in player.units[1:]:
        wingman.skill = Skill.High
    loadout, stores = _arm(player, player_type, spec.mission_type, maintask, spec.loadout)
    player.add_waypoint(home.position.point_from_heading(heading_out, distance * 0.4), cruise_alt, cruise_speed, "PUSH")
    player.add_waypoint(objective, cruise_alt, cruise_speed, "OBJECTIVE")
    player.land_at(home).name = "RECOVERY"

    skill = SKILLS[spec.enemy_skill]
    callsign = player.units[0].callsign_dict.get("name", "") if hasattr(player.units[0], "callsign_dict") else ""
    callsign = f"{callsign[:-2].upper()} {callsign[-2]}" if callsign[-2:].isdigit() else (callsign or "PLAYER")
    obj_brg = round(heading_out) % 360
    obj_nm = round(distance * 0.8 * M_TO_NM)

    b = Briefing(
        codename=f"{rng.choice(CODENAME_A)} {rng.choice(CODENAME_B)}",
        dtg=_dtg(m.start_time, getattr(terrain, "utc_offset", None)),
        theatre=spec.terrain, aircraft=spec.aircraft, flight_size=spec.flight_size, callsign=callsign,
        mission_type=spec.mission_type, mission_statement="", loadout=loadout, stores=stores,
        home={"name": home.name, **_ll(home.position),
              "uhf": _mhz(home.atc_radio.uhf_hz) if home.atc_radio else None,
              "vhf": _mhz(home.atc_radio.vhf_high_hz) if home.atc_radio else None,
              "tacan": home.tacan, "runways": [r.name for r in home.runways]},
        objective={"name": "OBJECTIVE", **_ll(objective), "brg": obj_brg, "dist_nm": obj_nm},
        enemy_field=enemy_field.name,
    )

    if spec.mission_type == "CAP":
        fighter = planes.MiG_29S if not heli else dcs.helicopters.Mi_24V
        bandits = m.patrol_flight(red, "Bandits", fighter, None, enemy_field.position, objective,
                                  speed=cruise_speed, altitude=cruise_alt + (0 if heli else 1000), group_size=2)
        for u in bandits.units:
            u.skill = skill
        _arm(bandits, fighter, "CAP", task.CAP)
        b.mission_statement = (f"{callsign} flight ({spec.flight_size}x {spec.aircraft}) conducts a combat air patrol "
                               f"at the objective, {obj_nm} nm {obj_brg:03d}°T of {home.name}, to deny enemy air "
                               f"activity, then recovers to {home.name}.")
        b.enemy_air.append({"name": f"2x {fighter.id}", "route": [_ll(p.position) for p in bandits.points]})
        b.threats.append({"name": f"2x {fighter.id}", "kind": "AIR", **_ll(objective), "range_nm": None,
                          "note": f"Patrolling from {enemy_field.name} toward the objective. Skill: {spec.enemy_skill}."})
        b.situation.append(f"Enemy fighters from {enemy_field.name} are contesting the airspace near the objective.")
    elif spec.mission_type == "Strike":
        column_pos = objective.point_from_heading(rng.uniform(0, 360), 1500)
        armour = m.vehicle_group_platoon(red, "Armour Column", [vehicles.Armor.T_72B, vehicles.Armor.T_72B,
                                         vehicles.Armor.BMP_2, vehicles.Armor.BMP_2, vehicles.Armor.BTR_80],
                                         column_pos, heading=rng.uniform(0, 360))
        sam_pos = column_pos.point_from_heading(rng.uniform(0, 360), 800)
        sam = m.vehicle_group_platoon(red, "Air Defence", [vehicles.AirDefence.Osa_9A33_ln,
                                      vehicles.AirDefence.ZSU_23_4_Shilka], sam_pos)
        for g in (armour, sam):
            for u in g.units:
                u.skill = skill
        b.mission_statement = (f"{callsign} flight ({spec.flight_size}x {spec.aircraft}) destroys the enemy armour "
                               f"column at the objective, {obj_nm} nm {obj_brg:03d}°T of {home.name}, then "
                               f"recovers to {home.name}.")
        b.targets.append({"name": "Armour column", **_ll(column_pos),
                          "units": [u.type for u in armour.units]})
        for u in sam.units:
            rng_m = THREAT_RANGES.get(u.type)
            b.threats.append({"name": {"Osa 9A33 ln": "SA-8 GECKO", "ZSU-23-4 Shilka": "ZSU-23-4 SHILKA"}.get(u.type, u.type),
                              "kind": "SAM" if "Osa" in u.type else "AAA", **_ll(u.position),
                              "range_nm": round(rng_m * M_TO_NM, 1) if rng_m else None,
                              "note": "Mobile, radar-guided. Ring is approximate." if "Osa" in u.type
                              else "Radar-guided gun. Stay above 10,000 ft or outside 1.5 nm."})
        b.situation.append(f"An enemy armour column is assembling near {enemy_field.name}, "
                           f"covered by short-range air defence.")
    else:
        b.mission_statement = (f"{callsign} flight ({spec.flight_size}x {spec.aircraft}) flies a familiarisation "
                               f"route to the objective, {obj_nm} nm {obj_brg:03d}°T of {home.name}, and recovers.")
        b.situation.append("No enemy activity is expected. Area is permissive.")

    behind = home.position.point_from_heading(heading_out + 180, 40000)
    if spec.awacs:
        aw = m.awacs_flight(blue, "AWACS", planes.E_3A, None, behind, race_distance=60000,
                            heading=round(heading_out + 90) % 360, altitude=9000, frequency=251)
        b.support.append({"role": "AWACS", "callsign": "OVERLORD", "type": "E-3A", "freq": "251.000",
                          "tacan": None, "alt_ft": round(9000 * M_TO_FT, -2),
                          "track": [_ll(p.position) for p in aw.points[1:3]]})
    if spec.tanker and not heli:
        probe = spec.aircraft in PROBE_AIRCRAFT
        tanker_type = planes.KC135MPRS if probe else planes.KC_135
        tk = m.refuel_flight(blue, "Tanker", tanker_type, None, behind.point_from_heading(heading_out + 90, 20000),
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

    m.coalition["blue"].bullseye = {"x": home.position.x, "y": home.position.y}
    b.bullseye = {"name": "BULLSEYE", **_ll(home.position)}
    b.waypoints = _route(player)
    b.situation.append("Friendly forces hold " + home.name + ". " +
                       ("AWACS OVERLORD provides picture calls. " if spec.awacs else "No AWACS support. ") +
                       ("Tanker available." if any("Tanker" in s["role"] for s in b.support) else ""))
    if spec.aircraft == "F-15ESE" and int(spec.date[:4]) < 1998:
        b.notes.append("Mission is dated before 1998: the Modern F-15E JTIDS/SIT datalink will be inactive.")

    m.set_sortie_text(f"OPERATION {b.codename}")
    m.set_description_text(" ".join(b.situation))
    m.set_description_bluetask_text(b.mission_statement)
    return m, b


def build_with_retries(spec: MissionSpec, locks: set[str], owned: list[str], terrains: list[str],
                       attempts: int = 6) -> tuple[MissionSpec, Mission, Briefing]:
    """Build; if the home field has no parking for this flight, reroll unlocked fields and retry."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            mission, briefing = build(spec)
            return spec, mission, briefing
        except NoParkingSlotError as e:
            last_error = e
            if "airport" in locks and "aircraft" in locks and "flight_size" in locks:
                break
            spec = randomize(asdict(spec), locks - {"airport", "seed"} if "airport" not in locks else locks - {"seed"},
                             owned, terrains)
    raise RuntimeError(f"No parking for this flight at {spec.airport}: {last_error}. "
                       "Unlock the airfield or reduce the flight size.")


def default_filename(spec: MissionSpec, briefing: Briefing | None = None) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    name = briefing.codename.title().replace(" ", "") if briefing else spec.mission_type.replace(" ", "")
    return f"{name}_{spec.aircraft}_{spec.mission_type.replace(' ', '')}_{spec.terrain}_{stamp}.miz"
