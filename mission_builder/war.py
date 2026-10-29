"""The air war between sorties: a campaign clock, squadrons and an air tasking order.

Kept in campaign.data["war"]. Both sides plan flights every campaign hour: a CAP over
the front, and strike/SEAD/CAS against the enemy's front-line sites. Flights take off,
fly their routes and land as the clock runs. Flights nobody flies in DCS are resolved
here when they reach their target (odds from the defences and enemy fighters nearby);
CAPs that meet fight it out. The player can take any planned blue flight: the clock
jumps to its takeoff, pauses, and the mission is built around that moment (see
ambient_flights). DCS results come back through after_sortie.

Everything here works in map coordinates (x north, y east, metres) and campaign
seconds since the war began. Lat/lon is added for the map page.
"""
from __future__ import annotations

import datetime as dt
import math
import random

from .campaign import Campaign, _dist, _role_name, other
from . import weaponeering
from .systems import SYSTEMS, WEIGHT, site_ranges, system_for, units_for

START = "2010-06-01T06:00:00"      # local time on the map when a war begins
SQUADRON_SIZE = 12
REPLENISH_PER_DAY = 3
PLAN_EVERY = 1800                   # the ATO is planned every half hour (a lost CAP is soon replaced)...
PLAN_AHEAD = (15 * 60, 45 * 60)     # ...for takeoffs in this window from the planning time
CAP_ON_STATION = 3600
# Intercepts: every AIR_CHECK seconds each CAP on station looks for enemy flights within DETECT
# metres (chance P_DETECT, less at night and far less against stealth) and flies out to meet
# one at PURSUIT m/s, no further than CHASE from its station; then it returns to its station.
AIR_CHECK = 300
DETECT = 80_000
P_DETECT = {"day": 0.5, "night": 0.35, "stealth": 0.15}   # per check
PURSUIT = 280.0
CHASE = 150_000
DOGFIGHT_KILL = 0.25         # per aircraft, scaled by the other side's share of the ratings
STRIKER_DEFENCE = 0.4        # a strike or transport flight's air-to-air rating
P_ABORT = 0.4                # survivors of an intercepted strike that jettison and go home
BATTLE_MEMORY = 6 * 3600     # how long battle markers are kept
MAX_INTERCEPTS = 2           # per CAP sortie: then it's out of missiles and just holds the station
SPEED = {"jet": 230.0, "heli": 60.0, "orbit": 180.0}   # m/s
ALT = {"jet": 6500, "heli": 120}
RANGE = {"jet": 650_000, "heli": 130_000}
# Attacks per half-hour plan. Red strikes every other plan (one an hour): the baselines give the
# blue side far fewer SAMs, and at two an hour red overran blue in 1-3 days in simulation.
ATTACKS_PER_PLAN = {"blue": 1, "red": 1}
REPAIR_HOURS = (12, 30)             # a destroyed site is back in action this long after, unless its field falls
KEEP_FINISHED = 2 * 3600            # finished flights stay on the ATO this long

# Red squadrons: (type id, missions, helicopter). Blue flies the player's own aircraft.
RED_SQUADRONS = [("MiG-29S", ["CAP"], False), ("Su-27", ["CAP"], False), ("Su-24M", ["Strike", "SEAD"], False),
                 ("Su-25T", ["SEAD", "CAS", "Strike"], False), ("Su-25", ["CAS", "Strike"], False),
                 ("Mi-24V", ["CAS", "CSAR"], True)]
# Air-to-air strength, for CAP fights (unknown types count as 0.7).
RATING = {"F-15C": 1.0, "Su-27": 1.0, "J-11A": 1.0, "Su-33": 0.95, "F-14B": 0.95, "F-14A-135-GR": 0.95,
          "FA-18C_hornet": 0.9, "F-16C_50": 0.9, "MiG-29S": 0.85, "MiG-29A": 0.75, "MiG-29G": 0.75,
          "F-15ESE": 0.8, "F-4E-45MC": 0.65, "MiG-31": 0.7, "MiG-21Bis": 0.5, "F-5E-3": 0.5}
CALLSIGNS = {"blue": ["ENFIELD", "SPRINGFIELD", "UZI", "COLT", "DODGE", "FORD", "CHEVY", "PONTIAC"],
             "red": ["SOKOL", "BERKUT", "GROM", "STRELA", "MOLNIYA", "VOLK", "KOBRA", "TAIGA"]}
# Blue's heavy and stealth bombers (AI only in DCS). They fly only what the player schedules,
# from the rearmost friendly field with a runway. (type, missions, aircraft, cruise m/s, altitude m)
BOMBERS = [("F-117A", ["Strike"], 8, 230.0, 7000), ("B-1B", ["Strike"], 6, 250.0, 7500),
           ("B-52H", ["Strike"], 6, 200.0, 10000)]
BOMBER_RANGE = 3_000_000
# Special forces: an AI transport helicopter squadron that inserts a team near the target.
SOF_SQUADRON = ("UH-60A", ["SOF", "CSAR"], 4, 65.0, 60)
# Downed pilots: share of lost aircraft whose crew eject (helicopter crews less often), how long
# before an unrescued pilot is captured in enemy territory or walks home in friendly territory.
P_EJECT = {"jet": 0.7, "heli": 0.4}
CAPTURE_AFTER = 6 * 3600
WALK_HOME_AFTER = 2 * 3600
CSAR_PER_PLAN = 2
CSAR_TRIES = 2                # rescue attempts per pilot before the side gives up on them
CSAR_RANGE = {"blue": 160_000, "red": 130_000}
SOF_RANGE = 160_000
SOF_STANDOFF = 4_000          # the landing zone is this far short of the target
SOF_ON_GROUND = 40 * 60       # the team works for this long before extraction
AI_ONLY = {b[0] for b in BOMBERS} | {SOF_SQUADRON[0]}
# Attack profiles. "low": under the SAM coverage, into the guns. "standoff": weapons released
# from STANDOFF_RANGE short of the target (needs stand-off weapons). "sof": special forces raid.
PROFILES = ("standard", "low", "standoff", "sof")
LOW_ALT = 150
STANDOFF_RANGE = 110_000
STANDOFF_WEAPONS = ("AGM-86", "AGM-154", "AGM-84", "AGM-158", "AGM-65", "Kh-", "GBU-39", "GBU-53")
STEALTH = {"F-117A"}
# Extra sites a heavy bomber's load can take out around its aimpoint (radius m, max extra sites).
CARPET = {"B-52H": (3000, 2), "B-1B": (2000, 2)}
NIGHT_HOURS = (21, 5)       # local time-on-target from 21:00 to 05:00 counts as night
# Heavily defended targets (SAM threat to an ordinary jet over them, see _ad_defence): the
# planner doesn't send ordinary strike or SEAD flights in; it sends stealth bombers, long-range
# stand-off missiles in numbers (saturation), or ballistic missiles, and goes after the SAM that
# covers a target before the target itself. Below this, ordinary packages as before.
HEAVY_DEFENCE = 1.5
# The chance an AI flight actually attacks a weaponeered aimpoint as planned (finds it, gets
# there, releases); the war's odds only, not the mission. Set so a lightly defended pair's strike
# succeeds about as often as before weaponeering (~65%).
DELIVERY = 0.6
AI_BOMBERS = True             # may the planner use blue's bombers (else they fly only what you frag)
MIN_PACKAGE_HIT = 0.15
STANDOFF_HIT = 0.45             # per pair of stand-off shooters (a heavy bomber counts as two), undefended
STANDOFF_DEFENCE = 0.5          # ...divided by 1 + this x the air defence over the target, which shoots weapons down
# Turnaround: an aircraft flies again this long after landing (fuel, arming, crew rest).
REST = {"bomber": 24 * 3600, "jet": 4 * 3600, "heli": 2 * 3600}
LOSS_COST = 0.6                 # how much one expected aircraft lost weighs against a hit
# Stand-off weapons that outrange the big SAMs, and how far from the target they're released.
LONG_STANDOFF = {"AGM-86": 400_000, "AGM-158": 250_000, "Kh-59": 150_000, "AGM-154": 110_000, "Kh-31": 90_000}
# Ballistic missiles from a missile site (red's Scud batteries): one salvo per site every MISSILE_REST.
MISSILE_REACH = 280_000
MISSILE_REST = 6 * 3600
MISSILE_HIT = 0.35              # a Scud salvo on a point target: inaccurate, nothing in DCS shoots it down
MISSION_FOR_ROLE = {"sam": "SEAD", "ewr": "SEAD", "structure": "Strike", "missile": "Strike",
                    "armor": "CAS", "shorad": "CAS", "farp": "Strike"}


# (type id, mission, chosen loadout name or "") -> (loadout name, [stores]). Set by the server
# from the generator, so the ATO shows exactly what the mission will load.
LOADOUTS = None
# type id -> [(loadout name, [stores])]: every stored loadout, set by the server (for stand-off picks).
LOADOUT_LIST = None
_STANDOFF_CACHE: dict[str, tuple[str, int] | None] = {}


def _standoff_loadout(type_id: str) -> tuple[str, int] | None:
    """The type's loadout with the longest-reaching stand-off weapon, and its release distance."""
    if type_id not in _STANDOFF_CACHE:
        best = None
        for name, stores in (LOADOUT_LIST(type_id) if LOADOUT_LIST else []):
            reach = max((r for k, r in LONG_STANDOFF.items() for st in stores if k in st), default=0)
            if reach and (not best or reach > best[1]):
                best = (name, reach)
        _STANDOFF_CACHE[type_id] = best
    return _STANDOFF_CACHE[type_id]


def _arm_flight(f: dict, chosen: str = "") -> None:
    if LOADOUTS:
        name, stores = LOADOUTS(f["type"], f["mission"], chosen)
        f["loadout"], f["stores"] = name, list(stores)


def _war(c: Campaign) -> dict:
    return c.data["war"]


def clock_label(c: Campaign, t: float | None = None) -> str:
    w = _war(c)
    when = dt.datetime.fromisoformat(w["start"]) + dt.timedelta(seconds=w["clock"] if t is None else t)
    return f"Day {(when.date() - dt.datetime.fromisoformat(w['start']).date()).days + 1} {when:%H:%M}"


def clock_datetime(c: Campaign) -> dt.datetime:
    w = _war(c)
    return dt.datetime.fromisoformat(w["start"]) + dt.timedelta(seconds=w["clock"])


# --- setup --------------------------------------------------------------------------------

def init(c: Campaign, blue_types: list[tuple[str, list[str], bool]], jet_fields: set[str]) -> None:
    """Give both sides squadrons. blue_types: (type, missions, helicopter) the player owns."""
    w = {"start": START, "clock": 0.0, "rate": 16, "paused": True, "mode": "running", "player_flight": None,
         "next_plan": 0.0, "next_replenish": 86400.0, "seq": 0, "squadrons": [], "flights": [],
         "engaged": [], "stats": {"blue_lost": 0, "red_lost": 0, "sorties": 0}}
    c.data["war"] = w
    for side, types in (("blue", blue_types), ("red", RED_SQUADRONS)):
        fields = c.fields_of(side)
        if not fields:
            continue
        enemy = [c.data["field_pos"][n] for n in c.fields_of(other(side))] or [[0, 0]]
        # Fields nearest the enemy first: helicopters go to the closest one.
        by_front = sorted(fields, key=lambda n: min(_dist(c.data["field_pos"][n], e) for e in enemy))
        jets = [n for n in by_front if n in jet_fields] or by_front
        for i, (type_id, missions, heli) in enumerate(types):
            base = by_front[0] if heli else jets[i % len(jets)]
            w["squadrons"].append({"id": f"{side[0].upper()}SQ{i + 1}", "side": side, "type": type_id,
                                   "missions": missions, "heli": heli, "base": base, "total": SQUADRON_SIZE})
    add_bombers(c, jet_fields)


def add_bombers(c: Campaign, jet_fields: set[str]) -> bool:
    """Blue's bomber squadrons, if the war doesn't have them yet. Returns True if any were added."""
    w = _war(c)
    have = {sq["type"] for sq in w["squadrons"]}
    fields = [n for n in c.fields_of("blue") if n in jet_fields] or c.fields_of("blue")
    if not fields or all(b[0] in have for b in BOMBERS + [SOF_SQUADRON[:5]]):
        return False
    if SOF_SQUADRON[0] not in have:  # special forces fly from the field nearest the enemy
        enemy = [c.data["field_pos"][n] for n in c.fields_of("red")] or [[0, 0]]
        front = min(c.fields_of("blue"), key=lambda n: min(_dist(c.data["field_pos"][n], e) for e in enemy))
        type_id, missions, size, speed, alt = SOF_SQUADRON
        w["squadrons"].append({"id": f"BSOF{len(w['squadrons']) + 1}", "side": "blue", "type": type_id,
                               "missions": missions, "heli": True, "base": front, "total": size, "size": size,
                               "manual": True, "speed": speed, "alt": alt})
    if all(b[0] in have for b in BOMBERS):
        return True
    enemy = [c.data["field_pos"][n] for n in c.fields_of("red")] or [[0, 0]]
    rear = max(fields, key=lambda n: min(_dist(c.data["field_pos"][n], e) for e in enemy))
    for type_id, missions, size, speed, alt in BOMBERS:
        if type_id in have:
            continue
        w["squadrons"].append({"id": f"BBM{len(w['squadrons']) + 1}", "side": "blue", "type": type_id,
                               "missions": missions, "heli": False, "base": rear, "total": size, "size": size,
                               "manual": True, "speed": speed, "alt": alt})
    return True


# --- geometry ------------------------------------------------------------------------------

def pos_at(f: dict, t: float) -> tuple[float, float, int]:
    """Position (x, y) and the index of the route leg a flight is on at time t."""
    r = f["route"]
    if t <= r[0]["t"]:
        return r[0]["x"], r[0]["y"], 0
    for i in range(1, len(r)):
        a, b = r[i - 1], r[i]
        if t <= b["t"]:
            k = (t - a["t"]) / max(1e-6, b["t"] - a["t"])
            return a["x"] + (b["x"] - a["x"]) * k, a["y"] + (b["y"] - a["y"]) * k, i
    return r[-1]["x"], r[-1]["y"], len(r) - 1


def _toward(a, b, metres):
    d = _dist(a, b) or 1
    return [a[0] + (b[0] - a[0]) * metres / d, a[1] + (b[1] - a[1]) * metres / d]


def _side_step(a, b, metres):
    """A point `metres` to the right of the line a->b, at b."""
    d = _dist(a, b) or 1
    return [b[0] - (b[1] - a[1]) * metres / d, b[1] + (b[0] - a[0]) * metres / d]


def _route(c: Campaign, sq: dict, mission: str, target, takeoff: float,
           profile: str = "standard", standoff: float = STANDOFF_RANGE) -> tuple[list[dict], dict]:
    kind = "heli" if sq["heli"] else "jet"
    base = c.data["field_pos"][sq["base"]]
    speed, alt = sq.get("speed", SPEED[kind]), sq.get("alt", ALT[kind])
    if profile == "low" and not sq["heli"]:
        alt = LOW_ALT
    pts = [("TAKEOFF", base, speed)]
    marks = {}
    waits = {}
    if mission == "CSAR":
        pts.append(("LZ", target, speed))
        pts.append(("TARGET", target, speed))   # on the ground picking up the pilot
        waits[len(pts) - 1] = 300
        marks["tot"] = len(pts) - 1
        pts.append(("EXTRACT", target, speed))
        waits[len(pts) - 1] = 300
    elif mission == "SOF" or profile == "sof":
        lz = _toward(target, base, SOF_STANDOFF)
        pts.append(("LZ", lz, speed))
        pts.append(("TARGET", lz, speed))   # the raid: the team is on the ground near the target
        waits[len(pts) - 1] = SOF_ON_GROUND / 2
        marks["tot"] = len(pts) - 1
        pts.append(("EXTRACT", lz, speed))
        waits[len(pts) - 1] = SOF_ON_GROUND / 2
    elif profile == "standoff":
        # A target nearer than the weapon's reach: release once clear of the base (a release point
        # on the base itself shares the takeoff time, and the mission drops it with its orders).
        d = _dist(base, target)
        launch = _toward(target, base, standoff) if d > standoff else _toward(base, target, min(30_000, d / 2))
        pts.append(("TARGET", launch, speed))  # weapons release point
        marks["tot"] = len(pts) - 1
        pts.append(("EGRESS", _side_step(target, launch, 20_000), speed))
    elif mission == "CAP":
        a = target
        # The orbit's other end: across the line from base; a station over the base itself gets a fixed leg.
        b = _side_step(base, target, 35_000) if _dist(base, target) > 1000 else [a[0] + 25_000, a[1] + 25_000]
        pts.append(("STATION", a, speed))
        marks["station"] = len(pts) - 1
        legs = max(2, int(CAP_ON_STATION * SPEED["orbit"] / _dist(a, b)))
        for i in range(legs):
            pts.append(("CAP" if i % 2 == 0 else "STATION", b if i % 2 == 0 else a, SPEED["orbit"]))
        marks["off"] = len(pts) - 1
    else:
        stand_off = 8_000 if sq["heli"] else 25_000
        if _dist(base, target) > stand_off * 1.5:
            pts.append(("IP", _toward(target, base, stand_off), speed))
        pts.append(("TARGET", target, speed))
        marks["tot"] = len(pts) - 1
        pts.append(("EGRESS", _side_step(base, target, stand_off), speed))
    pts.append(("LAND", base, speed))
    route, t = [], takeoff
    for i, (name, p, leg_speed) in enumerate(pts):
        if i:
            t += _dist(route[-1]["xy"], p) / leg_speed + waits.get(i, 0)
        route.append({"name": name, "x": round(p[0]), "y": round(p[1]), "t": round(t, 1), "xy": p,
                      "alt": alt, "speed": leg_speed})
    for r in route:
        del r["xy"]
        r["ll"] = c.ll(r["x"], r["y"])
    return route, {k: route[i]["t"] for k, i in marks.items()}


# --- planning -----------------------------------------------------------------------------

def _active(f: dict) -> bool:
    return f["status"] in ("planned", "airborne")


def available(c: Campaign, sq: dict) -> int:
    w = _war(c)
    committed = sum(f["count"] for f in w["flights"] if f["squadron"] == sq["id"] and _active(f))
    committed += sum(n for until, n in sq.get("resting", []) if until > w["clock"])
    return sq["total"] - committed


def _landed(c: Campaign, f: dict, t: float) -> None:
    """A flight is down: its aircraft are turned around (REST) before they fly again."""
    f["status"], f["end"] = "landed", t
    sq = next((q for q in _war(c)["squadrons"] if q["id"] == f["squadron"]), None)
    if sq and f["count"] > 0:
        rest = REST["bomber" if sq.get("manual") else "heli" if sq["heli"] else "jet"]
        sq["resting"] = [r for r in sq.get("resting", []) if r[0] > t] + [[round(t + rest), f["count"]]]


def _new_flight(c: Campaign, sq: dict, mission: str, count: int, takeoff: float, target_xy, target_site=None,
                loadout: str = "", profile: str = "standard", commit: bool = True, standoff: float = STANDOFF_RANGE):
    w = _war(c)
    w["seq"] += 1
    route, marks = _route(c, sq, mission, target_xy, takeoff, profile, standoff)
    names = CALLSIGNS[sq["side"]]
    f = {"id": f"{sq['side'][0].upper()}{w['seq']}", "side": sq["side"], "squadron": sq["id"],
         "callsign": f"{names[w['seq'] % len(names)]} {1 + (w['seq'] // len(names)) % 9}",
         "type": sq["type"], "heli": sq["heli"], "count": count, "start_count": count, "mission": mission,
         "base": sq["base"], "target": target_site, "target_xy": [round(target_xy[0]), round(target_xy[1])],
         "route": route, "takeoff": route[0]["t"], "land": route[-1]["t"], **marks,
         "status": "planned", "resolved": False, "player": False, "losses": 0, "result": ""}
    if target_site:
        site = next(s for s in c.data["sites"] if s["id"] == target_site)
        f["target_name"] = f"{_role_name(site)} near {site['zone']}"
    f["profile"] = profile
    if profile == "standoff":
        f["launch_xy"] = [f["route"][[r["name"] for r in route].index("TARGET")][k] for k in ("x", "y")]
    _arm_flight(f, loadout)
    if target_site and f.get("stores"):   # what it will put on which unit, for the ATO
        units = site_units(next(s for s in c.data["sites"] if s["id"] == target_site))
        plan = weaponeering.weaponeer(f["stores"], count, units)
        f["aimpoints"] = weaponeering.describe(plan, units) or "nothing on board suits this target"
    if commit:
        w["flights"].append(f)
    else:
        w["seq"] -= 1
    return f


def plan(c: Campaign, now: float, rng: random.Random) -> None:
    w = _war(c)
    for side in ("blue", "red"):
        enemy = other(side)
        squadrons = [sq for sq in w["squadrons"] if sq["side"] == side and sq["total"] > 0 and not sq.get("manual")]
        front = c.front_zones(side)
        if not squadrons or not front:
            continue
        own_fields = c.fields_of(side)
        enemy_front = [c.data["field_pos"][n] for n in front]
        centre = [sum(p[0] for p in enemy_front) / len(enemy_front), sum(p[1] for p in enemy_front) / len(enemy_front)]
        own_front = min(own_fields, key=lambda n: _dist(c.data["field_pos"][n], centre))
        lo, hi = now + PLAN_AHEAD[0], now + PLAN_AHEAD[1]

        # CAP: keep one pair over the front.
        covered = any(f["side"] == side and f["mission"] == "CAP" and _active(f) and f.get("off", 0) >= hi
                      for f in w["flights"])
        if not covered:
            station = _toward(c.data["field_pos"][own_front], centre,
                              0.4 * _dist(c.data["field_pos"][own_front], centre))
            cap = [sq for sq in squadrons if "CAP" in sq["missions"] and not sq["heli"] and available(c, sq) >= 2]
            if cap:
                sq = min(cap, key=lambda s: _dist(c.data["field_pos"][s["base"]], station))
                _new_flight(c, sq, "CAP", 2, rng.uniform(lo, lo + 900), station)

        # Attacks on the enemy's front-line sites.
        targeted = {f["target"] for f in w["flights"] if _active(f) and f["target"]}
        sites = [s for s in c.alive_sites(enemy) if s["zone"] in front and s["id"] not in targeted]
        heavy = {s["id"] for s in sites if _ad_defence(c, side, (s["x"], s["y"])) >= HEAVY_DEFENCE}
        rate = ATTACKS_PER_PLAN[side]
        w["plans"] = w.get("plans", 0) + (side == "blue")
        attacks = int(rate) if rate >= 1 else int(w["plans"] % round(1 / rate) == 0)
        for _ in range(attacks):
            if not sites:
                break
            rng.shuffle(sites)
            for site in sites[:8]:
                if site["id"] in heavy:
                    if _plan_heavy(c, side, site, lo, hi, targeted, rng):
                        sites.remove(site)
                        break
                    continue
                wanted = MISSION_FOR_ROLE.get(site["role"], "Strike")
                # Squadrons that fly the right task, and any strike squadron (which can hit any ground target).
                fit = [(sq, wanted if wanted in sq["missions"] else "Strike") for sq in squadrons
                       if (wanted in sq["missions"] or "Strike" in sq["missions"]) and available(c, sq) >= 2
                       and _dist(c.data["field_pos"][sq["base"]], (site["x"], site["y"]))
                       <= RANGE["heli" if sq["heli"] else "jet"]]
                if not fit:
                    continue
                # Prefer the right task, then whoever has the most aircraft free.
                best = max((available(c, sq) + (4 if m == wanted else 0)) for sq, m in fit)
                sq, mission = rng.choice([(sq, m) for sq, m in fit if available(c, sq) + (4 if m == wanted else 0) == best])
                count = 4 if available(c, sq) >= 8 and rng.random() < 0.35 else 2
                _new_flight(c, sq, mission, count, rng.uniform(lo, hi), (site["x"], site["y"]), site["id"])
                sites.remove(site)
                break


def _ad_defence(c: Campaign, side: str, xy, gone=frozenset()) -> float:
    """The enemy air defence over xy as an ordinary jet attacking it meets it: each SAM in
    range by its weight and strength, short-range sites at half."""
    total = 0.0
    for s in c.alive_sites(other(side)):
        if s["id"] in gone or s["role"] not in ("sam", "shorad"):
            continue
        reach, _ = site_ranges(s)
        if reach and _dist((s["x"], s["y"]), xy) <= reach:
            total += s.get("strength", 1.0) * (WEIGHT.get(s.get("system"), 1.0) if s["role"] == "sam" else 0.5)
    return total


def _covering_sams(c: Campaign, side: str, site: dict) -> list[dict]:
    """Enemy SAM sites whose missiles reach this site, strongest first (roll these back first)."""
    xy = (site["x"], site["y"])
    out = [s for s in c.alive_sites(other(side)) if s["role"] == "sam" and s is not site
           and site_ranges(s)[0] and _dist((s["x"], s["y"]), xy) <= site_ranges(s)[0]]
    return sorted(out, key=lambda s: -WEIGHT.get(s.get("system"), 1.0) * s.get("strength", 1.0))


def _plan_heavy(c: Campaign, side: str, site: dict, lo: float, hi: float, targeted: set, rng: random.Random) -> bool:
    """A package for a heavily defended target. The SAM covering it is attacked first (roll-back);
    the means is the best of: stealth bombers, long-range stand-off missiles in numbers, a ballistic
    missile salvo. Returns True if something was sent."""
    w = _war(c)
    cover = [s for s in _covering_sams(c, side, site) if s["id"] not in targeted]
    target = cover[0] if cover and site["role"] != "sam" else site
    xy = (target["x"], target["y"])
    options = []   # (value, kind, args)
    for sq in w["squadrons"]:
        if sq["side"] != side or sq["heli"] or sq["total"] <= 0 or available(c, sq) < 2 or                 (sq.get("manual") and not AI_BOMBERS):
            continue
        reach = BOMBER_RANGE if sq.get("manual") else RANGE["jet"]
        if _dist(c.data["field_pos"][sq["base"]], xy) > reach:
            continue
        mission = "SEAD" if target["role"] in ("sam", "ewr") and "SEAD" in sq["missions"] else "Strike"
        if mission not in sq["missions"]:
            continue
        count = min(4, available(c, sq)) if sq["type"] in STEALTH or sq.get("manual") else 4 if available(c, sq) >= 4 else 2
        tries = []
        if sq["type"] in STEALTH and is_night(c, hi):   # the F-117 strikes by night
            tries.append(("standard", "", STANDOFF_RANGE))
        so = _standoff_loadout(sq["type"])
        if so:
            tries.append(("standoff", so[0], so[1]))
        for profile, loadout, dist in tries:
            f = _new_flight(c, sq, mission, count, rng.uniform(lo, hi), xy, target["id"], loadout=loadout,
                            profile=profile, commit=False, standoff=dist)
            p_hit, p_loss = strike_odds(c, f, f["tot"])
            options.append((p_hit - LOSS_COST * p_loss * count, p_hit, "flight", (sq, mission, count, f, loadout, profile, dist)))
    missiles = [m for m in c.alive_sites(side) if m["role"] == "missile" and side == "red"
                and _dist((m["x"], m["y"]), xy) <= MISSILE_REACH and lo - m.get("fired", -1e9) >= MISSILE_REST]
    if missiles:
        options.append((MISSILE_HIT, MISSILE_HIT, "missile", missiles[0]))
    options = [o for o in options if o[1] >= MIN_PACKAGE_HIT]
    if not options:
        return False
    _, _, kind, args = max(options, key=lambda o: o[0])
    if kind == "missile":
        _missile_salvo(c, args, target, lo, rng)
    else:
        sq, mission, count, f, loadout, profile, dist = args
        _new_flight(c, sq, mission, count, f["takeoff"], xy, target["id"], loadout=loadout, profile=profile,
                    standoff=dist)
    targeted.add(target["id"])
    return True


def _missile_salvo(c: Campaign, battery: dict, target: dict, t: float, rng: random.Random) -> None:
    """A ballistic missile salvo from a missile site: settled when fired (minutes of flight)."""
    battery["fired"] = t
    hit = target["alive"] and rng.random() < MISSILE_HIT * battery.get("strength", 1.0)
    what = f"{_role_name(target)} near {target['zone']}"
    if hit:
        target["alive"] = False
        schedule_repair(target, t, rng)
    c.log(f"{clock_label(c, t)} · Ballistic missile salvo from the missile battery near {battery['zone']} "
          f"{'destroyed' if hit else 'missed'} {what}.", "Missile", "Scud-B", side=battery["side"], t=t)
    if hit:
        for change in c.capture_check(battery["side"], t):
            c.log(f"{clock_label(c, t)} · {change}", side=battery["side"], t=t)
            _after_capture(c, t)


# --- resolution ---------------------------------------------------------------------------

def _lose(c: Campaign, f: dict, n: int, t: float, pilots: bool = True) -> int:
    n = min(n, f["count"])
    if n <= 0:
        return 0
    if pilots:  # crews that get out (DCS reports its own ejections, so it passes pilots=False)
        x, y, _ = pos_at(f, t)
        for _ in range(n):
            if random.random() < P_EJECT["heli" if f["heli"] else "jet"]:
                add_pilot(c, f, x, y, t)
    f["count"] -= n
    f["losses"] += n
    sq = next(s for s in _war(c)["squadrons"] if s["id"] == f["squadron"])
    sq["total"] = max(0, sq["total"] - n)
    _war(c)["stats"][f"{f['side']}_lost"] += n
    if f["count"] <= 0:
        f["status"], f["end"] = "lost", t
    return n


def add_pilot(c: Campaign, f: dict | None, x: float, y: float, t: float, side: str | None = None) -> dict:
    w = _war(c)
    w["pilot_seq"] = w.get("pilot_seq", 0) + 1
    pilot = {"id": f"P{w['pilot_seq']}", "side": side or f["side"], "x": round(x), "y": round(y), "ll": c.ll(x, y),
             "t": round(t, 1), "status": "down", "from": f"{f['callsign']} ({f['type']})" if f else "unknown",
             "csar": None}
    w.setdefault("pilots", []).append(pilot)
    return pilot


def _territory(c: Campaign, xy) -> str:
    """The side whose airfield is nearest: a rough idea of whose ground a pilot came down on."""
    n = min(c.data["field_pos"], key=lambda k: _dist(c.data["field_pos"][k], xy))
    return c.data["fields"][n]


def pilots_tick(c: Campaign, t: float) -> None:
    """Unrescued pilots: captured in enemy territory, home on foot in friendly territory."""
    for pl in _war(c).get("pilots", []):
        if pl["status"] != "down":
            continue
        age = t - pl["t"]
        mine = _territory(c, (pl["x"], pl["y"])) == pl["side"]
        if mine and age >= WALK_HOME_AFTER:
            pl["status"] = "recovered"
            c.log(f"{clock_label(c, t)} · Pilot from {pl['from']} made it back to friendly lines.", side=pl["side"], t=t)
        elif not mine and age >= CAPTURE_AFTER:
            pl["status"] = "captured"
            c.log(f"{clock_label(c, t)} · Pilot from {pl['from']} was captured.", side=pl["side"], t=t)
    w = _war(c)
    w["pilots"] = [pl for pl in w.get("pilots", []) if pl["status"] == "down" or t - pl["t"] < 24 * 3600][-200:]


def plan_csar(c: Campaign, now: float, rng: random.Random) -> None:
    """Send a rescue helicopter (with a CAP over the pickup when fighters are free) to downed pilots."""
    w = _war(c)
    lo = now + PLAN_AHEAD[0]
    for side in ("blue", "red"):
        waiting = [pl for pl in w.get("pilots", []) if pl["side"] == side and pl["status"] == "down" and not pl["csar"]
                   and pl.get("tries", 0) < CSAR_TRIES]
        sent = 0
        for pl in sorted(waiting, key=lambda p: p["t"]):
            if sent >= CSAR_PER_PLAN:
                break
            xy = (pl["x"], pl["y"])
            helos = [sq for sq in w["squadrons"] if sq["side"] == side and "CSAR" in sq["missions"] and sq["total"] > 0
                     and available(c, sq) >= 1 and _dist(c.data["field_pos"][sq["base"]], xy) <= CSAR_RANGE[side]]
            if not helos:
                continue
            sq = min(helos, key=lambda q: _dist(c.data["field_pos"][q["base"]], xy))
            f = _new_flight(c, sq, "CSAR", min(2, available(c, sq)), rng.uniform(lo, lo + 600), xy)
            f["pilot"], f["target_name"] = pl["id"], f"downed pilot from {pl['from']}"
            pl["csar"] = f["id"]
            pl["tries"] = pl.get("tries", 0) + 1
            sent += 1
            escorts = [q for q in w["squadrons"] if q["side"] == side and "CAP" in q["missions"] and not q["heli"]
                       and not q.get("manual") and q["total"] > 0 and available(c, q) >= 2]
            if escorts:  # fighters over the pickup, timed to be there first
                q = min(escorts, key=lambda q: _dist(c.data["field_pos"][q["base"]], xy))
                e = _new_flight(c, q, "CAP", 2, f["takeoff"], xy)
                shift = (f["tot"] - 600) - e["station"]
                for r in e["route"]:
                    r["t"] = round(r["t"] + shift, 1)
                for k in ("takeoff", "land", "station", "off"):
                    e[k] = round(e[k] + shift, 1)
                e["target_name"] = f"cover for {f['callsign']}'s rescue"
                if e["takeoff"] < now:
                    w["flights"].remove(e)
            c.log(f"{clock_label(c, now)} · {f['callsign']} ({f['count']}x {sq['type']}) tasked to rescue the pilot from "
                  f"{pl['from']}.", "CSAR", sq["type"], side=side, t=now)


def resolve_csar(c: Campaign, f: dict, rng: random.Random) -> None:
    t = f["tot"]
    f["resolved"] = True
    pl = next((p for p in _war(c).get("pilots", []) if p["id"] == f.get("pilot")), None)
    if not pl or pl["status"] != "down":
        f["result"] = "the pilot was no longer there"
        return
    threat = _threat(c, f, (pl["x"], pl["y"]), t)
    escort = any(g["side"] == f["side"] and g["mission"] == "CAP" and g["status"] == "airborne"
                 and _dist(pos_at(g, t)[:2], (pl["x"], pl["y"])) <= 60_000 for g in _war(c)["flights"])
    lost = _lose(c, f, sum(rng.random() < min(0.3, 0.03 + 0.05 * threat) for _ in range(f["count"])), t)
    ok = f["count"] > 0 and rng.random() < max(0.2, 0.85 - 0.12 * threat + (0.1 if escort else 0.0))
    if ok:
        pl["status"] = "rescued"
        f["result"] = f"rescued the pilot from {pl['from']}"
    else:
        pl["csar"] = None   # try again next time
        f["result"] = "couldn't reach the pilot"
    if lost:
        f["result"] += f"; lost {lost}"
    c.log(f"{clock_label(c, t)} · {f['callsign']} ({f['start_count']}x {f['type']}, CSAR) {f['result']}.", "CSAR", f["type"],
          side=f["side"], t=t)


def migrate(c: Campaign) -> bool:
    """Older wars: rescue roles for the helicopter squadrons."""
    changed = False
    for sq in _war(c)["squadrons"]:
        if sq["type"] in ("UH-60A", "Mi-24V") and "CSAR" not in sq["missions"]:
            sq["missions"] = sq["missions"] + ["CSAR"]
            changed = True
    for s in c.data["sites"]:  # sites from before each had its DCS system: the one missions placed
        if "system" not in s and system_for(s["side"], s["role"]):
            s["system"] = system_for(s["side"], s["role"])
            changed = True
    w = _war(c)
    if w.get("ejections"):  # ejections recorded before there were downed pilots
        by_id = {f["id"]: f for f in w["flights"]}
        for e in w.pop("ejections"):
            fid = e["unit"].split(":")[1] if e["unit"].startswith("MBF:") else None
            f = by_id.get(fid)
            if f:
                add_pilot(c, f, e["x"], e["y"], e["t"])
            else:
                side = "blue" if fid and fid.startswith("B") else "red"
                add_pilot(c, None, e["x"], e["y"], e["t"], side=side)
        changed = True
    return changed


def downed_pilots_ground(c: Campaign) -> list[dict]:
    """Pilots still down, to place in a mission as a lone soldier where they came down."""
    return [{"id": pl["id"], "side": pl["side"], "role": "pilot", "x": pl["x"], "y": pl["y"], "zone": "", "strength": 1.0}
            for pl in _war(c).get("pilots", []) if pl["status"] == "down"]


def _enemy_caps(c: Campaign, side: str, xy, t: float, radius: float) -> list[dict]:
    out = []
    for g in _war(c)["flights"]:
        if g["side"] != side and g["mission"] == "CAP" and g["status"] == "airborne":
            x, y, _ = pos_at(g, t)
            if _dist((x, y), xy) <= radius:
                out.append(g)
    return out


def is_night(c: Campaign, t: float) -> bool:
    hour = (dt.datetime.fromisoformat(_war(c)["start"]) + dt.timedelta(seconds=t)).hour
    return hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]


def _threat(c: Campaign, f: dict, xy, t: float, gone=frozenset()) -> float:
    """How dangerous it is for this flight over xy at time t. `gone`: site ids to treat as
    already destroyed (for estimating a sequence of strikes)."""
    profile = f.get("profile", "standard")
    if profile == "sof" or f["mission"] in ("SOF", "CSAR"):
        threat = 0.0
        for s in c.alive_sites(other(f["side"])):
            if s["id"] in gone:
                continue
            d = _dist((s["x"], s["y"]), xy)
            reach = site_ranges(s)[0]   # low and slow: the guns and tanks matter, long-range SAMs less
            threat += (0.0 if not reach else
                       1.0 if s["role"] == "shorad" and d <= reach else
                       0.8 if s["role"] == "armor" and d <= reach else
                       0.3 if s["role"] == "sam" and d <= min(15_000, reach) else 0.0)
        return threat + 0.5 * len(_enemy_caps(c, f["side"], xy, t, 60_000))
    low = profile == "low"
    threat = 0.0
    for s in c.alive_sites(other(f["side"])):
        if s["id"] in gone:
            continue
        d = _dist((s["x"], s["y"]), xy)
        k = s.get("strength", 1.0)   # a damaged site fights at reduced strength
        reach, see = site_ranges(s)   # its DCS ranges (systems.py)
        if s["role"] == "sam" and reach and d <= reach:
            threat += k * WEIGHT.get(s.get("system"), 1.0) * (0.5 if f["mission"] == "SEAD" else 1.0) * (0.6 if f["heli"] else 1.0) * (0.4 if low else 1.0)
        elif s["role"] == "ewr" and d <= see:
            threat += k * 0.3 * (0.3 if low else 1.0)
        elif s["role"] == "shorad" and reach and d <= reach:
            threat += k * (1.0 if f["heli"] else 0.5) * (2.0 if low else 1.0)
    fighters = (1.0 if f["heli"] else 1.5) * len(_enemy_caps(c, f["side"], xy, t, 100_000))
    if is_night(c, t):
        fighters *= 0.6     # fewer fighters find you in the dark
    if f["type"] in STEALTH:
        return threat * 0.25 + fighters * 0.2
    if f["type"] in CARPET:
        threat *= 1.3       # big, high and slow
    return threat + fighters


def _who(f: dict) -> str:
    return f"{f['callsign']} ({f['count']}x {f['type']}, {f['mission']})"


def site_units(site: dict) -> list[str]:
    """The DCS unit types a mission places on a site (as systems.py places them), for weaponeering."""
    system = site.get("system") or system_for(site["side"], site["role"])
    if system in SYSTEMS:
        return [u.id for u in units_for(system, site.get("strength", 1.0))]
    return ["Workshop A", "Workshop A"]   # installations and forward bases: two structures


def _weaponeered(c: Campaign, f: dict, t: float, gone, count: int):
    """(chance the target is destroyed, chance each aircraft is lost, aimpoints) from the flight's
    actual weapons against the units on its target (weaponeering.py), or None when the flight
    has no stores on record or no target site."""
    site = next((s for s in c.data["sites"] if s["id"] == f.get("target")), None)
    if not site or not f.get("stores"):
        return None
    standoff = f.get("profile") == "standoff"
    threat = _threat(c, f, f.get("launch_xy") if standoff and f.get("launch_xy") else f["target_xy"], t, gone)
    units = site_units(site)
    plan = weaponeering.weaponeer(f["stores"], count, units, defence=_ad_defence(c, f["side"], f["target_xy"], gone),
                                  accuracy=1.0 if standoff else max(0.3, 1 - 0.08 * threat))
    for a in plan:   # the AI doesn't always find, reach or release on its aimpoint
        a["p_kill"] *= DELIVERY
    p_hit = weaponeering.p_destroyed(plan, units, blind_counts=site["role"] in ("sam", "ewr"))
    return p_hit, min(0.25, 0.01 + 0.02 * threat), plan


def strike_odds(c: Campaign, f: dict, t: float, gone=frozenset(), count: int | None = None) -> tuple[float, float]:
    """(chance the target is destroyed, chance each aircraft is lost) for an attack flight."""
    count = f["count"] if count is None else count
    profile = f.get("profile", "standard")
    if profile == "sof" or f["mission"] == "SOF":
        threat = _threat(c, f, f["target_xy"], t, gone)
        return max(0.15, 0.7 - 0.1 * threat), min(0.3, 0.03 + 0.05 * threat)
    w = _weaponeered(c, f, t, gone, count)
    if w:
        return w[0], w[1]
    # No stores on record (tests without the server's loadouts): the old rule of thumb.
    if profile == "standoff":  # danger is at the release point; the weapons have to get through
        threat = _threat(c, f, f.get("launch_xy") or f["target_xy"], t, gone)
        # Saturation: each pair's weapons get through the defences over the target less often,
        # but a heavy bomber's load counts as two pairs, so numbers win.
        per_pair = STANDOFF_HIT / (1 + STANDOFF_DEFENCE * _ad_defence(c, f["side"], f["target_xy"], gone))
        pairs = count * (2 if f["type"] in CARPET else 1) / 2
        return 1 - (1 - per_pair) ** pairs, min(0.25, 0.01 + 0.02 * threat)
    threat = _threat(c, f, f["target_xy"], t, gone)
    # Odds per pair of aircraft over the target; a heavy bomber carries a pair's worth of bombs or more.
    pairs = count * (2 if f["type"] in CARPET else 1) / 2
    return 1 - (1 - max(0.12, 0.65 - 0.07 * threat)) ** pairs, min(0.25, 0.01 + 0.02 * threat)


def resolve_attack(c: Campaign, f: dict, rng: random.Random) -> None:
    t = f["tot"]
    f["resolved"] = True
    if f.get("aborted"):
        f["result"] = "aborted after being intercepted"
        c.log(f"{clock_label(c, t)} · {_who(f)} aborted the attack after being intercepted.", f["mission"],
              f["type"], side=f["side"], t=t)
        return
    _, p_loss = strike_odds(c, f, t)
    who = _who(f)
    lost = _lose(c, f, sum(rng.random() < p_loss for _ in range(f["count"])), t)
    p_hit, _ = strike_odds(c, f, t)
    site = next((s for s in c.data["sites"] if s["id"] == f["target"]), None)
    parts = []
    if f["count"] and site and site["alive"] and rng.random() < p_hit:
        site["alive"] = False
        schedule_repair(site, t, rng)
        parts.append(f"destroyed {_role_name(site)} near {site['zone']}")
        if f["type"] in CARPET:  # a bomber's load covers more than the aimpoint
            radius, extra = CARPET[f["type"]]
            near = sorted((s for s in c.alive_sites(site["side"]) if _dist((s["x"], s["y"]), (site["x"], site["y"])) <= radius),
                          key=lambda s: _dist((s["x"], s["y"]), (site["x"], site["y"])))
            hit = [s for s in near[:extra] if rng.random() < p_hit]
            for s in hit:
                s["alive"] = False
                schedule_repair(s, t, rng)
            if hit:
                parts.append(f"and {len(hit)} more site{'s' if len(hit) > 1 else ''} nearby")
    elif site and not site["alive"]:
        parts.append("found the target already destroyed")
    else:
        parts.append("missed the target")
    if lost:
        parts.append(f"lost {lost} aircraft")
    f["result"] = "; ".join(parts).replace("; and ", " and ")
    c.log(f"{clock_label(c, t)} · {who} {f['result']}.", f["mission"], f["type"], side=f["side"], t=t)
    for change in c.capture_check(f["side"], t):
        c.log(f"{clock_label(c, t)} · {change}", side=f["side"], t=t)
        _after_capture(c, t)


def resolve_station(c: Campaign, f: dict, rng: random.Random) -> None:
    """A CAP arriving on station: from now on it hunts (see air_check)."""
    f["resolved"] = True


def _fighter(f: dict) -> bool:
    return f["mission"] == "CAP"


def _on_station(f: dict, t: float) -> bool:
    return (f["mission"] == "CAP" and f["status"] == "airborne" and f["count"] > 0 and not f["player"]
            and not f.get("flown") and f.get("station", 1e18) <= t <= f.get("off", 0) - 300
            and not f.get("intercept") and f.get("intercepts", 0) < MAX_INTERCEPTS
            and t >= f.get("dcs_until", 0))


def air_check(c: Campaign, t: float, rng: random.Random) -> None:
    """Each CAP on station looks for enemy flights near it and goes after the nearest it sees."""
    w = _war(c)
    engaged = {tuple(p) for p in w["engaged"]}
    flights = [f for f in w["flights"] if f["status"] == "airborne" and f["count"] > 0 and f["takeoff"] < t]
    for cap in [f for f in flights if _on_station(f, t)]:
        cx, cy, _ = pos_at(cap, t)
        night = is_night(c, t)
        seen = []
        for g in flights:
            if g["side"] == cap["side"] or tuple(sorted([cap["id"], g["id"]])) in engaged or g.get("aborted") \
                    or t < g.get("dcs_until", 0):
                continue
            gx, gy, _ = pos_at(g, t)
            d = _dist((cx, cy), (gx, gy))
            if d > DETECT:
                continue
            p = P_DETECT["stealth"] if g["type"] in STEALTH else P_DETECT["night" if night else "day"]
            if rng.random() < p:
                seen.append((d, g))
        if not seen:
            continue
        _, g = min(seen, key=lambda x: x[0])
        _plan_intercept(c, cap, g, t)


def _plan_intercept(c: Campaign, cap: dict, g: dict, t: float) -> None:
    w = _war(c)
    cx, cy, leg = pos_at(cap, t)
    meet, dt = None, 0.0
    for _ in range(4):  # where the intruder will be when we get there
        gx, gy, _ = pos_at(g, t + dt)
        meet = (gx, gy)
        dt = _dist((cx, cy), meet) / PURSUIT
    station = next(((r["x"], r["y"]) for r in cap["route"] if r["name"] == "STATION"), (cx, cy))
    if t + dt >= g["land"] or _dist(station, meet) > CHASE:
        return  # can't catch it before it's home, or too far from the station to chase
    alt = cap["route"][0]["alt"]
    t_meet = t + dt
    t_back = t_meet + _dist(meet, station) / SPEED["jet"]
    kept = [r for r in cap["route"] if r["t"] <= t]
    later = [r for r in cap["route"] if r["t"] > t_back]
    new = kept + [{"name": "NOW", "x": round(cx), "y": round(cy), "t": round(t, 1), "alt": alt, "speed": PURSUIT},
                  {"name": "INTERCEPT", "x": round(meet[0]), "y": round(meet[1]), "t": round(t_meet, 1), "alt": alt,
                   "speed": PURSUIT},
                  {"name": "STATION", "x": round(station[0]), "y": round(station[1]), "t": round(t_back, 1), "alt": alt,
                   "speed": SPEED["jet"]}]
    if not later or later[-1]["name"] != "LAND":
        base = c.data["field_pos"][cap["base"]]
        later = [r for r in later if r["name"] != "LAND"] + [{
            "name": "LAND", "x": base[0], "y": base[1], "t": round(t_back + _dist(station, base) / SPEED["jet"], 1),
            "alt": alt, "speed": SPEED["jet"]}]
    for r in new[len(kept):]:
        r["ll"] = c.ll(r["x"], r["y"])
    cap["route"] = new + later
    cap["land"] = cap["route"][-1]["t"]
    cap["intercept"] = {"target": g["id"], "t": round(t_meet, 1), "callsign": g["callsign"]}
    cap["intercepts"] = cap.get("intercepts", 0) + 1
    w["engaged"].append(sorted([cap["id"], g["id"]]))


def resolve_intercept(c: Campaign, cap: dict, rng: random.Random) -> None:
    w = _war(c)
    it = cap.pop("intercept")
    t = it["t"]
    g = next((f for f in w["flights"] if f["id"] == it["target"]), None)
    if not g or g["status"] != "airborne" or g["count"] <= 0 or not cap["count"]:
        c.log(f"{clock_label(c, t)} · {cap['callsign']} lost contact with {it['callsign']}.", "CAP", cap["type"],
              side=cap["side"], t=t)
        return
    x, y, _ = pos_at(cap, t)
    rc = RATING.get(cap["type"], 0.7)
    if _fighter(g):
        rg = RATING.get(g["type"], 0.7)
        lc = _lose(c, cap, sum(rng.random() < DOGFIGHT_KILL * rg / (rc + rg) for _ in range(cap["count"])), t)
        lg = _lose(c, g, sum(rng.random() < DOGFIGHT_KILL * rc / (rc + rg) for _ in range(g["count"])), t)
        text = (f"{cap['callsign']} ({cap['type']}) intercepted {g['callsign']} ({g['type']}): "
                f"{lg} shot down, {lc} lost.")
    else:
        p = DOGFIGHT_KILL * 1.2 * rc / (rc + STRIKER_DEFENCE) * (0.5 if g["type"] in STEALTH else 1.0)
        lg = _lose(c, g, sum(rng.random() < p for _ in range(g["count"])), t)
        lc = _lose(c, cap, sum(rng.random() < 0.03 for _ in range(cap["count"])), t)
        aborted = g["count"] > 0 and not g.get("resolved") and rng.random() < P_ABORT
        if aborted:
            g["aborted"] = True
        text = (f"{cap['callsign']} ({cap['type']}) intercepted {g['callsign']} ({g['count'] + lg}x {g['type']}, "
                f"{g['mission']}): {lg} shot down" + (f", {lc} lost" if lc else "")
                + ("; the rest jettisoned and turned for home" if aborted else "") + ".")
    w.setdefault("battles", []).append({"t": t, "ll": c.ll(x, y), "text": text,
                                        "sides": [cap["side"], g["side"]], "kills": lg + lc})
    w["battles"] = [b for b in w["battles"] if b["t"] >= t - BATTLE_MEMORY][-60:]
    c.log(f"{clock_label(c, t)} · {text}", "CAP", cap["type"], side=cap["side"], t=t)


def _after_capture(c: Campaign, t: float) -> None:
    """Squadrons whose base fell move to their side's nearest field; flights planned from it are scrubbed."""
    for sq in _war(c)["squadrons"]:
        if c.data["fields"].get(sq["base"]) == sq["side"]:
            continue
        own = c.fields_of(sq["side"])
        for f in _war(c)["flights"]:
            if f["squadron"] == sq["id"] and f["status"] == "planned":
                f["status"], f["end"] = "cancelled", t
        if not own:
            sq["total"] = 0
            continue
        old = c.data["field_pos"][sq["base"]]
        sq["base"] = min(own, key=lambda n: _dist(c.data["field_pos"][n], old))
        c.log(f"{clock_label(c, t)} · {sq['type']} squadron relocates to {sq['base']}.", side=sq["side"], t=t)


def schedule_repair(site: dict, t: float, rng: random.Random) -> None:
    site["repair_at"] = round(t + rng.uniform(*REPAIR_HOURS) * 3600)


def _repair(c: Campaign, site: dict) -> None:
    site.pop("repair_at", None)
    if site.get("withdrawn") or c.data["fields"].get(site["zone"]) != site["side"]:
        return
    site["alive"] = True
    site.pop("strength", None)


# --- the clock ----------------------------------------------------------------------------

def _next_event(c: Campaign):
    w = _war(c)
    best = (w["next_plan"], "plan", None)
    if w["next_replenish"] < best[0]:
        best = (w["next_replenish"], "replenish", None)
    for s in c.data["sites"]:
        if "repair_at" in s and s["repair_at"] < best[0]:
            best = (s["repair_at"], "repair", s)
    if w.setdefault("next_air", w["clock"]) < best[0]:
        best = (w["next_air"], "air", None)
    for f in w["flights"]:
        if f["status"] == "airborne" and f.get("intercept") and f["intercept"]["t"] < best[0]:
            best = (f["intercept"]["t"], "intercept", f)
        if f["status"] == "planned":
            cand = (f["takeoff"], "takeoff", f)
        elif f["status"] == "airborne" and not f["resolved"] and not f["player"]:
            cand = (f["station"] if f["mission"] == "CAP" else f["tot"], "resolve", f)
        elif f["status"] == "airborne":
            cand = (f["land"], "land", f)
        else:
            continue
        if cand[0] < best[0]:
            best = cand
    return best


def advance(c: Campaign, to_t: float, rng: random.Random | None = None) -> bool:
    """Run the war up to campaign time to_t. Returns True if anything happened."""
    rng = rng or random.Random()
    w = _war(c)
    changed = False
    while True:
        t, kind, f = _next_event(c)
        if t > to_t:
            break
        w["clock"] = max(w["clock"], t)
        changed = True
        if kind == "plan":
            pilots_tick(c, t)
            plan(c, t, rng)
            plan_csar(c, t, rng)
            w["next_plan"] = t + PLAN_EVERY
        elif kind == "replenish":
            for sq in w["squadrons"]:
                if sq["total"] > 0:
                    sq["total"] = min(sq.get("size", SQUADRON_SIZE), sq["total"] + REPLENISH_PER_DAY)
            w["next_replenish"] = t + 86400
        elif kind == "takeoff":
            if c.data["fields"].get(f["base"]) != f["side"]:
                f["status"], f["end"] = "cancelled", t
            else:
                f["status"] = "airborne"
        elif kind == "resolve":
            if f["mission"] == "CAP":
                resolve_station(c, f, rng)
            elif f["mission"] == "CSAR":
                resolve_csar(c, f, rng)
            else:
                resolve_attack(c, f, rng)
        elif kind == "repair":
            _repair(c, f)
        elif kind == "air":
            air_check(c, t, rng)
            w["next_air"] = t + AIR_CHECK
        elif kind == "intercept":
            resolve_intercept(c, f, rng)
        elif kind == "land":
            _landed(c, f, t)
    w["clock"] = max(w["clock"], to_t)
    keep = w["clock"] - KEEP_FINISHED
    before = len(w["flights"])
    w["flights"] = [f for f in w["flights"] if _active(f) or f.get("end", 1e18) >= keep]
    live = {f["id"] for f in w["flights"]}
    w["engaged"] = [p for p in w["engaged"] if p[0] in live and p[1] in live]
    return changed or len(w["flights"]) != before


# --- strikes the player schedules --------------------------------------------------------

def schedule(c: Campaign, squadron_id: str, mission: str, count: int, tot: float | None,
             site_id: str | None = None, station=None, loadout: str = "", profile: str = "standard",
             commit: bool = True) -> dict:
    """Frag a mission onto the ATO: this squadron flies `mission` with `count` aircraft.
    Strike/SEAD/CAS go against an enemy site anywhere (front or deep); CAP holds a station
    ([x, y]) for an hour. `tot` is the campaign time on target / on station (None: as soon as
    possible)."""
    w = _war(c)
    sq = next((x for x in w["squadrons"] if x["id"] == squadron_id and x["side"] == "blue"), None)
    if not sq or sq["total"] <= 0:
        raise RuntimeError("Pick a friendly squadron that still has aircraft.")
    if mission == "CSAR":
        raise RuntimeError("Rescue flights are planned automatically for downed pilots.")
    if mission not in sq["missions"]:
        raise RuntimeError(f"{sq['type']} squadron flies {', '.join(sq['missions'])}, not {mission}.")
    if profile not in PROFILES:
        raise RuntimeError(f"Unknown attack profile {profile!r}.")
    if mission == "SOF":
        profile = "sof"
    elif profile == "sof":
        raise RuntimeError("Special forces raids are flown by the special forces helicopter squadron.")
    count = int(count)
    if count < 1 or count > available(c, sq):
        raise RuntimeError(f"{sq['type']} squadron has {available(c, sq)} aircraft available.")
    site = None
    if mission == "CAP":
        if not station:
            raise RuntimeError("Pick a CAP station: an airfield, or click the map.")
        target = (float(station[0]), float(station[1]))
        where = _station_name(c, target)
    else:
        site = next((s for s in c.data["sites"] if s["id"] == site_id), None)
        if not site or site["side"] != "red" or not site["alive"]:
            raise RuntimeError("Pick an enemy site that is still standing.")
        target = (site["x"], site["y"])
        where = None
    reach = SOF_RANGE if mission == "SOF" else BOMBER_RANGE if sq.get("manual") else RANGE["heli" if sq["heli"] else "jet"]
    if _dist(c.data["field_pos"][sq["base"]], target) > reach:
        raise RuntimeError(f"That's out of range for {sq['type']} from {sq['base']}.")
    f = _new_flight(c, sq, mission, count, w["clock"] + 10 * 60, target, site["id"] if site else None, loadout,
                    profile, commit)
    if profile == "standoff":
        if not any(any(k in st for k in STANDOFF_WEAPONS) for st in f.get("stores", [])):
            if commit:
                w["flights"].remove(f)
            raise RuntimeError(f"A stand-off attack needs stand-off weapons; {sq['type']} loadout "
                               f"{f.get('loadout')!r} carries {', '.join(f.get('stores', [])) or 'none'}.")
        f["launch_xy"] = [f["route"][[r["name"] for r in f["route"]].index("TARGET")][k] for k in ("x", "y")]
    if where:
        f["target_name"] = where
    key = "station" if mission == "CAP" else "tot"
    if tot is not None:  # shift the whole route so it arrives on time
        shift = float(tot) - f[key]
        if shift < 0:
            if commit:
                w["flights"].remove(f)
            raise RuntimeError(f"Too soon: the earliest time {'on station' if key == 'station' else 'on target'} "
                               f"is {clock_label(c, f[key])}.")
        for r in f["route"]:
            r["t"] = round(r["t"] + shift, 1)
        for k in ("takeoff", "land", "tot", "station", "off"):
            if k in f:
                f[k] = round(f[k] + shift, 1)
    f["scheduled"] = True
    f["night"] = is_night(c, f[key])
    if not commit:
        return f
    c.log(f"{clock_label(c)} · Fragged {f['callsign']} ({count}x {sq['type']}, {mission}) "
          f"{'holding' if mission == 'CAP' else 'against'} {f['target_name']}, "
          f"{'on station' if key == 'station' else 'on target'} {clock_label(c, f[key])}"
          f"{' (night)' if f['night'] else ''}"
          + (f", carrying {', '.join(f['stores'])}" if f.get("stores") else "") + ".",
          mission, sq["type"], side="blue", t=w["clock"])
    return f


def _station_name(c: Campaign, xy) -> str:
    """'CAP station 12 nm W of Al Minhad AFB', from the nearest airfield."""
    n = min(c.data["field_pos"], key=lambda k: _dist(c.data["field_pos"][k], xy))
    fx, fy = c.data["field_pos"][n]
    d = _dist((fx, fy), xy)
    if d < 5000:
        return f"station over {n}"
    hdg = (math.degrees(math.atan2(xy[1] - fy, xy[0] - fx)) + 360) % 360   # x north, y east
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return f"station {round(d / 1852)} nm {points[round(hdg / 45) % 8]} of {n}"


def estimate(c: Campaign, flights: list[dict], objective: dict | None = None, trials: int = 400,
             rng: random.Random | None = None) -> dict:
    """Odds for a package of (uncommitted) flights, flown in time-on-target order: each strike
    that lands makes the ones after it safer. objective: {"sites": [ids]} (destroy them all) or
    {"field": name} (capture it). Returns per-flight hit and loss rates and the objective's odds."""
    rng = rng or random.Random(1)
    order = sorted((f for f in flights if f["mission"] != "CAP"), key=lambda f: f["tot"])
    hits = {f["id"]: 0 for f in order}
    losses = {f["id"]: 0.0 for f in order}
    success = 0
    zone = None
    if objective and objective.get("field"):
        zone = objective["field"]
        status = c.zone_status(zone)
        zone_sites = {s["id"]: s for s in c.data["sites"] if s["zone"] == zone and s["side"] == c.data["fields"][zone]}
    for _ in range(trials):
        gone: set[str] = set()
        for f in order:
            p_hit, p_loss = strike_odds(c, f, f["tot"], frozenset(gone))
            lost = sum(rng.random() < p_loss for _ in range(f["count"]))
            losses[f["id"]] += lost
            if lost < f["count"]:
                p_hit, _ = strike_odds(c, f, f["tot"], frozenset(gone), f["count"] - lost)
                if f.get("target") and f["target"] not in gone and rng.random() < p_hit:
                    gone.add(f["target"])
                    hits[f["id"]] += 1
        if objective and objective.get("sites"):
            success += all(sid in gone for sid in objective["sites"])
        elif zone:
            killed = [zone_sites[i] for i in gone if i in zone_sites and zone_sites[i]["alive"]]
            ad = sum(1 for s in killed if s["role"] in ("sam", "ewr"))
            success += (status["destroyed"] + len(killed) >= status["needed"]
                        and status["ad_destroyed"] + ad >= status["ad_needed"])
    return {"flights": {fid: {"hit": hits[fid] / trials, "losses": losses[fid] / trials} for fid in hits},
            "objective": success / trials if objective else None,
            "expected_losses": sum(losses.values()) / trials,
            "trials": trials}


def cancel(c: Campaign, flight_id: str) -> None:
    w = _war(c)
    f = next((x for x in w["flights"] if x["id"] == flight_id), None)
    if not f or f["side"] != "blue" or f["status"] != "planned" or f["player"]:
        raise RuntimeError("Only friendly flights that haven't taken off can be scrubbed.")
    f["status"], f["end"] = "cancelled", w["clock"]
    c.log(f"{clock_label(c)} · Scrubbed {f['callsign']}.", side="blue", t=w["clock"])


# --- flying a flight in DCS ---------------------------------------------------------------

def take_flight(c: Campaign, flight_id: str) -> dict:
    """Hand a blue flight to the player. A planned flight: the clock jumps to its takeoff.
    A flight already in the air: the player joins it now, where it is (f["joined_at"])."""
    w = _war(c)
    f = next((x for x in w["flights"] if x["id"] == flight_id), None)
    if not f or f["side"] != "blue" or f["status"] not in ("planned", "airborne") or not f["count"]:
        raise RuntimeError("That flight can't be flown: pick a planned or airborne friendly flight.")
    if f["type"] in AI_ONLY:
        raise RuntimeError(f"The {f['type']} is AI-only in DCS. Fly an escort or SEAD flight alongside it instead.")
    if f["status"] == "airborne":
        if w["clock"] >= f["land"] - 300:
            raise RuntimeError(f"{f['callsign']} is about to land. Pick another flight.")
        f["joined_at"] = w["clock"]
    else:
        advance(c, f["takeoff"] - 1)
        if f["status"] != "planned":
            raise RuntimeError("That flight was scrubbed before takeoff.")
        w["clock"] = f["takeoff"]
        f.pop("joined_at", None)
    f["player"] = True
    f["status"] = "airborne"
    w["mode"], w["paused"], w["player_flight"] = "in_dcs", True, f["id"]
    w["stats"]["sorties"] += 1
    c.log(f"{clock_label(c)} · You {'join' if 'joined_at' in f else 'take'} {f['callsign']} "
          f"({f['count']}x {f['type']}, {f['mission']}"
          + (f" against {f['target_name']}" if f.get("target_name") else "") + ").", f["mission"], f["type"],
          side="blue", t=w["clock"])
    return f


def release_flight(c: Campaign) -> None:
    """Give up on flying the taken flight (or watching): the AI flies it and the clock can run again."""
    w = _war(c)
    f = next((x for x in w["flights"] if x["id"] == w["player_flight"]), None)
    if f and not f.get("flown"):
        f["player"] = False
    w["mode"], w["player_flight"] = "running", None
    w.pop("spectating", None)


def spectate(c: Campaign) -> float:
    """Pause the war for a mission with no player in it: the war as it is now, to watch in DCS.
    Returns the campaign time the mission starts at."""
    w = _war(c)
    if w["mode"] == "in_dcs":
        raise RuntimeError("A mission is already out to DCS. Fly it, or give it back, first.")
    w["mode"], w["paused"], w["player_flight"] = "in_dcs", True, None
    w["spectating"] = w["clock"]
    c.log(f"{clock_label(c)} · You watch the war in DCS from here.", side="blue", t=w["clock"])
    return w["clock"]


def spectator_ground(c: Campaign, ambient: list[dict], limit: int = 120) -> list[dict]:
    """Ground forces for a spectator mission: everything alive near any flight in it (closest
    first), plus what those flights are going after."""
    picked: dict[str, dict] = {}
    for a in ambient:
        f = next((g for g in _war(c)["flights"] if g["id"] == a["id"]), None)
        if not f:
            continue
        for site in ground_sites(c, f, []):
            picked.setdefault(site["id"], site)
            if len(picked) >= limit:
                break
        if f.get("target"):  # ground_sites leaves out a flight's own target
            site = next((s for s in c.data["sites"] if s["id"] == f["target"] and s["alive"]), None)
            if site:
                picked.setdefault(site["id"], {**{k: site[k] for k in ("id", "side", "role", "x", "y", "zone")},
                                               "strength": site.get("strength", 1.0), "system": site.get("system")})
        if len(picked) >= limit:
            break
    return list(picked.values())[:limit]


def ambient_flights(c: Campaign, player_id: str, horizon: float = 5400) -> list[dict]:
    """Every other flight that is airborne now, or takes off within `horizon` seconds, as the
    mission needs them: current position, remaining route, and when to activate."""
    w = _war(c)
    now = w["clock"]
    out = []
    for f in w["flights"]:
        if f["id"] == player_id or not f["count"]:
            continue
        if f["status"] == "airborne" or (f["status"] == "planned" and f["takeoff"] <= now + horizon):
            at = max(now, f["takeoff"])
            x, y, leg = pos_at(f, at)
            rest = [{"name": r["name"], "x": r["x"], "y": r["y"], "alt": r["alt"], "speed": r["speed"]}
                    for r in f["route"][leg:] if r["t"] > at]
            if not rest:
                continue
            out.append({"id": f["id"], "side": f["side"], "type": f["type"], "count": f["count"], "heli": f["heli"],
                        "mission": f["mission"], "target": f.get("target"), "target_xy": f.get("target_xy"),
                        "loadout": f.get("loadout", ""), "callsign": f["callsign"],
                        "profile": f.get("profile", "standard"), "fuel": f.get("fuel"),
                        # special forces: when the team is on the ground, from mission start
                        "team_delay": round(next((r["t"] for r in f["route"] if r["name"] == "LZ"), at) - now)
                        if f.get("profile") == "sof" else None,
                        "x": x, "y": y, "alt": f["route"][0]["alt"],
                        "speed": f["route"][0]["speed"], "route": rest, "delay": round(at - now),
                        "base": f["base"]})
    return out


def player_start(c: Campaign, f: dict) -> dict:
    """Where the player's flight starts: at its base (planned), or in the air on its route
    (joined): position, the waypoints still ahead, and whether its target is behind it."""
    t = _war(c)["clock"]
    if "joined_at" not in f:
        return {"airborne": False}
    x, y, leg = pos_at(f, t)
    ahead = [r["name"] for r in f["route"][leg:] if r["t"] > t]
    return {"airborne": True, "xy": [round(x), round(y)], "alt": f["route"][0]["alt"],
            "past_target": "TARGET" not in ahead and "STATION" not in ahead and "CAP" not in ahead}


def _seg_dist(p, a, b) -> float:
    """Distance from point p to segment a-b."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return _dist(p, a)
    k = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)))
    return _dist(p, (ax + dx * k, ay + dy * k))


# Ground forces put into a jump-in mission: everything alive near the player's route, enemy
# SAMs and radars whose coverage reaches it, and the targets of the other flights in the mission.
NEAR_ROUTE = 60_000
COVERAGE = {"sam": 45_000, "ewr": 120_000}
MAX_GROUND_SITES = 90


def ground_sites(c: Campaign, f: dict, ambient: list[dict]) -> list[dict]:
    t = _war(c)["clock"]
    x, y, leg = pos_at(f, t)
    path = [(x, y)] + [(r["x"], r["y"]) for r in f["route"][leg:] if r["t"] > t]
    if len(path) < 2:
        path.append(path[0])
    segs = list(zip(path, path[1:]))
    wanted: dict[str, float] = {}
    for s in c.data["sites"]:
        if not s["alive"]:
            continue
        d = min(_seg_dist((s["x"], s["y"]), a, b) for a, b in segs)
        if d <= NEAR_ROUTE or (s["side"] == "red" and d <= COVERAGE.get(s["role"], 0)):
            wanted[s["id"]] = d
    for a in ambient:  # what the other flights are going after (red strikes included)
        g = next((g for g in _war(c)["flights"] if g["id"] == a["id"]), None)
        if g and g.get("target"):
            site = next((s for s in c.data["sites"] if s["id"] == g["target"] and s["alive"]), None)
            if site:
                wanted.setdefault(site["id"], 0.0)
    if f.get("target"):
        wanted.pop(f["target"], None)  # the generator builds the player's own target
    chosen = sorted(wanted, key=wanted.get)[:MAX_GROUND_SITES]
    by_id = {s["id"]: s for s in c.data["sites"]}
    return [{**{k: by_id[i][k] for k in ("id", "side", "role", "x", "y", "zone")},
             "strength": by_id[i].get("strength", 1.0), "system": by_id[i].get("system")} for i in chosen]


def site_systems(c: Campaign) -> dict:
    """For a mission: each live site's system and strength, so a SAM the mission puts on a
    campaign position is the one the campaign has there."""
    return {s["id"]: [s.get("system") or system_for(s["side"], s["role"]), s.get("strength", 1.0)]
            for s in c.data["sites"] if s["alive"] and (s.get("system") or system_for(s["side"], s["role"]))}


def _group_site_changes(changes: list[str], side: str) -> list[str]:
    """'Destroyed a SAM site near Kish', 'Lost part of the garrison near X (3/4 left)'... as one
    phrase per airfield: 'destroyed 2 and damaged 1 enemy positions near Kish Intl'."""
    import re as _re
    out, per = [], {}
    for ch in changes:
        if side == "red":
            m = _re.match(r"(Destroyed|Damaged) .* near (.+?)(?: \(\d+/\d+ left\))?\.$", ch)
        else:
            m = _re.match(r"(Lost part of|Lost) .* near (.+?)(?: \(\d+/\d+ left\))?\.$", ch)
        if m:
            verb = "damaged" if m.group(1) in ("Damaged", "Lost part of") else ("destroyed" if side == "red" else "lost")
            per.setdefault(m.group(2), {}).setdefault(verb, 0)
            per[m.group(2)][verb] += 1
        elif ("Friendly" in ch) == (side == "blue") and "captured" in ch or (side == "blue" and "campaign is" in ch):
            out.append(ch.rstrip(".")[0].lower() + ch.rstrip(".")[1:])
    who = "enemy" if side == "red" else "friendly"
    for zone, verbs in per.items():
        bits = [f"{verb} {n}" for verb, n in verbs.items()]
        total = sum(verbs.values())
        out.append(f"{' and '.join(bits)} {who} position{'s' if total > 1 else ''} near {zone}")
    return out


def _route_from(c: Campaign, f: dict, xy, t: float, fuel: float) -> None:
    """Rebuild an airborne flight's route from where DCS left it at time t: a CAP with time
    and fuel left goes back to its station and finishes its orbit; everyone else goes home."""
    alt = f["route"][0]["alt"]
    speed = SPEED["heli" if f["heli"] else "jet"]
    base = c.data["field_pos"].get(f["base"]) or xy
    pts = [{"name": "DCS", "x": round(xy[0]), "y": round(xy[1]), "t": round(t, 1), "alt": alt, "speed": speed}]
    if f["mission"] == "CAP" and f.get("off", 0) > t + 600 and fuel >= 0.25:
        station = next(((r["x"], r["y"]) for r in f["route"] if r["name"] == "STATION"), xy)
        t_back = t + _dist(xy, station) / speed
        pts.append({"name": "STATION", "x": round(station[0]), "y": round(station[1]), "t": round(t_back, 1),
                    "alt": alt, "speed": speed})
        pts += [dict(r) for r in f["route"] if r["t"] > t_back and r["name"] in ("CAP", "STATION")]
    last = pts[-1]
    pts.append({"name": "LAND", "x": round(base[0]), "y": round(base[1]),
                "t": round(last["t"] + _dist((last["x"], last["y"]), base) / speed, 1), "alt": alt, "speed": speed})
    for r in pts:
        r.setdefault("ll", c.ll(r["x"], r["y"]))
    f["route"] = [r for r in f["route"] if r["t"] < t] + pts
    f["land"] = f["route"][-1]["t"]
    f.pop("intercept", None)


def after_sortie(c: Campaign, sortie: dict, dead_units: set[str], flown: float = 0.0,
                 changes: list[str] | None = None, report: dict | None = None) -> list[str]:
    """Apply what happened in DCS. Aircraft losses (units named MBF:<flight>:n); and, for a
    campaign flight, DCS's end state: flights that were in the mission landed where DCS says,
    or carry on from where DCS left them; the engine doesn't roll dice for them inside the
    window. Then the war moves on by the time spent in the mission and one summary is logged."""
    w = _war(c)
    report = report or {}
    lost: dict[str, int] = {}
    for u in dead_units:
        if u.startswith("MBF:"):
            fid = u.split(":")[1]
            lost[fid] = lost.get(fid, 0) + 1
    mine = next((f for f in w["flights"] if f["id"] == sortie.get("flight")), None)
    friendly_lost = enemy_lost = 0
    for f in w["flights"]:
        n = lost.get(f["id"], 0)
        if f is mine:
            f["flown"], f["resolved"], f["player"] = True, True, False
            f["result"] = "flown in DCS" + (f"; lost {n}" if n else "")
        if n:
            n = _lose(c, f, n, w["clock"], pilots=False)
            if f["side"] == "blue":
                friendly_lost += n
            else:
                enemy_lost += n
    rng = random.Random()
    for s in c.data["sites"]:  # sites destroyed or damaged in DCS get repaired like any others
        if (not s["alive"] and not s.get("withdrawn") or s.get("strength", 1.0) < 1.0) and "repair_at" not in s:
            schedule_repair(s, w["clock"], rng)
    if w["player_flight"] == sortie.get("flight"):
        w["mode"], w["player_flight"] = "running", None
    watched = sortie.get("began") is not None and not mine   # a spectator mission
    if not mine and not watched:
        return []
    began = sortie["began"] if watched else mine.get("joined_at", mine["takeoff"])
    if watched and w.get("spectating") is not None:
        w["mode"], w["player_flight"] = "running", None
        w.pop("spectating", None)
    end_t = began + flown
    # DCS's end state for every flight that was active in the mission.
    landed_away, carried_on = [], 0
    by_id = {f["id"]: f for f in w["flights"]}
    for fid, (pt, x, y, _alt, alive, fuel, air) in (report.get("pos") or {}).items():
        f = by_id.get(fid)
        if not f or f["status"] not in ("airborne", "planned") or f["count"] <= 0:
            continue
        f["dcs_until"] = end_t
        if f["mission"] not in ("CAP", "CSAR") and not f.get("resolved"):  # rescues are settled by the engine
            f["resolved"] = True
            f["result"] = f.get("result") or "flown by the AI in DCS"
        missing = f["count"] - alive
        if missing > 0:  # deaths DCS didn't report as events
            lost_now = _lose(c, f, missing, end_t, pilots=False)
            if f["side"] == "blue":
                friendly_lost += lost_now
            else:
                enemy_lost += lost_now
            if f["count"] <= 0:
                continue
        landing = next((v for k, v in (report.get("lands") or {}).items() if k.startswith(f"MBF:{fid}:")), None)
        if landing and not air:
            place, lt = landing
            _landed(c, f, began + lt)
            f["landed_at"] = place
            if place and place != f["base"]:
                landed_away.append(f"{f['callsign']} landed at {place}")
        elif air:
            f["status"] = "airborne"
            f["fuel"] = fuel
            _route_from(c, f, (x, y), end_t, fuel)
            carried_on += 1
    # Pilots who ejected in DCS are on the ground where they came down.
    flight_of = {f["id"]: f for f in w["flights"]}
    for unit, et, ex, ey in report.get("ejects") or []:
        fid = unit.split(":")[1] if unit.startswith("MBF:") else None
        f = flight_of.get(fid)
        if f:
            add_pilot(c, f, ex, ey, began + et)
    # What each flight that was in the mission did, from DCS's kill and shot reports.
    callsign = {f["id"]: f for f in w["flights"]}
    sites = {s["id"]: s for s in c.data["sites"]}
    kills_by: dict[str, list[str]] = {}
    for victim, killer in report.get("kills") or []:
        fid = killer.split(":")[1]
        if victim.startswith("MB:"):
            s_ = sites.get(victim.split(":")[1])
            what = f"{_role_name(s_).split(' ', 1)[1]} unit near {s_['zone']}" if s_ else "ground unit"
        else:
            g = callsign.get(victim.split(":")[1])
            what = f"a {g['type']} of {g['callsign']}" if g else "an aircraft"
        kills_by.setdefault(fid, []).append(what)
    shots = report.get("shots") or {}
    for fid in (report.get("pos") or {}):
        f = callsign.get(fid)
        if not f:
            continue
        kills = kills_by.get(fid, [])
        fired = shots.get(fid, 0)
        if not kills and not fired and (f["mission"] in ("CAP", "CSAR") or f.get("tot", end_t) < began):
            continue   # nothing to report, or its attack was over before the mission began
        bits = []
        if f["mission"] != "CAP" or fired:
            bits.append(f"released {fired} weapon{'s' if fired != 1 else ''}" if fired else "released no weapons")
        if kills:
            counted: dict[str, int] = {}
            for k in kills:
                counted[k] = counted.get(k, 0) + 1
            bits.append("killed " + ", ".join(f"{n}x {k}" if n > 1 else k for k, n in counted.items()))
        elif f["mission"] in ("Strike", "SEAD", "CAS"):
            bits.append("no hits recorded")
        f["result"] = "; ".join(bits)
        c.log(f"{clock_label(c, end_t)} · In DCS: {f['callsign']} ({f['start_count']}x {f['type']}, {f['mission']}): "
              + "; ".join(bits) + ".", f["mission"], f["type"], side=f["side"], t=end_t)
    # The war moves on by the time you spent in the mission (it stays paused).
    if flown and w["clock"] < end_t:
        advance(c, end_t)
    parts = []
    if enemy_lost:
        parts.append(f"{enemy_lost} enemy aircraft shot down")
    parts += _group_site_changes(changes or [], "red")
    if friendly_lost:
        parts.append(f"{friendly_lost} friendly aircraft lost")
    parts += _group_site_changes(changes or [], "blue")
    parts += landed_away
    if carried_on:
        parts.append(f"{carried_on} flight{'s carry' if carried_on > 1 else ' carries'} on from where DCS left {'them' if carried_on > 1 else 'it'}")
    if report.get("ejects"):
        parts.append(f"{len(report['ejects'])} pilot{'s' if len(report['ejects']) > 1 else ''} ejected")
    who = "watched in DCS" if watched else f"{mine['callsign']} ({mine['type']}, {mine['mission']})"
    summary = (f"{clock_label(c)} · Back from DCS after {round(flown / 60)} min: {who}: "
               + ("; ".join(parts) if parts else "no losses either side") + ".")
    c.log(summary, "" if watched else mine["mission"], "" if watched else mine["type"], side="blue",
          t=w["clock"], sortie=sortie["id"], debrief=True)
    return [summary]


# --- the map page -------------------------------------------------------------------------

def snapshot(c: Campaign) -> dict:
    w = _war(c)
    for f in w["flights"]:
        if "stores" not in f:
            _arm_flight(f)
        if "aimpoints" not in f and f.get("target") and f.get("stores"):   # flights planned before weaponeering
            site = next((s for s in c.data["sites"] if s["id"] == f["target"]), None)
            if site:
                units = site_units(site)
                f["aimpoints"] = weaponeering.describe(weaponeering.weaponeer(f["stores"], f["start_count"], units), units)                     or "nothing on board suits this target"
    zones = {side: [{"field": n, **c.zone_status(n)} for n in c.fields_of(side)] for side in ("blue", "red")}
    return {
        "theatre": c.theatre, "scenario": c.data["scenario"], "start": w["start"], "clock": w["clock"],
        "clock_label": clock_label(c), "rate": w["rate"], "paused": w["paused"], "mode": w["mode"],
        "player_flight": w["player_flight"], "stats": w["stats"], "spectating": w.get("spectating") is not None,
        "front": {"blue": c.front_zones("blue"), "red": c.front_zones("red")}, "zones": zones,
        "fields": [{"name": n, "side": side, "ll": c.data["field_ll"][n]} for n, side in sorted(c.data["fields"].items())],
        "sites": [{"id": s["id"], "side": s["side"], "role": s["role"], "ll": s["ll"], "alive": s["alive"],
                   "zone": s["zone"], "system": s.get("system"), "strength": s.get("strength", 1.0),
                   "reach": site_ranges(s)[0] if s["role"] in ("sam", "shorad") else 0,
                   "see": site_ranges(s)[1] if s["role"] in ("sam", "ewr") else 0} for s in c.data["sites"]],
        "squadrons": [dict(sq, available=available(c, sq)) for sq in w["squadrons"]],
        "ai_only": sorted(AI_ONLY),
        "battles": w.get("battles", []),
        "pilots": w.get("pilots", []),
        "flights": [{k: v for k, v in f.items() if k != "route"} |
                    {"route": [{"name": r["name"], "ll": r["ll"], "t": r["t"], "alt": r.get("alt")}
                               for r in f["route"]]}
                    for f in w["flights"]],
        "events": [e for e in c.data["events"] if "t" in e or e.get("sortie")][-40:],
        "won": not c.fields_of("red"), "lost": not c.fields_of("blue"),
    }
