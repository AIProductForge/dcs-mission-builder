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
                 ("Mi-24V", ["CAS"], True)]
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
SOF_SQUADRON = ("UH-60A", ["SOF"], 4, 65.0, 60)
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
MISSION_FOR_ROLE = {"sam": "SEAD", "ewr": "SEAD", "structure": "Strike", "missile": "Strike",
                    "armor": "CAS", "shorad": "CAS", "farp": "Strike"}


# (type id, mission, chosen loadout name or "") -> (loadout name, [stores]). Set by the server
# from the generator, so the ATO shows exactly what the mission will load.
LOADOUTS = None


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
           profile: str = "standard") -> tuple[list[dict], dict]:
    kind = "heli" if sq["heli"] else "jet"
    base = c.data["field_pos"][sq["base"]]
    speed, alt = sq.get("speed", SPEED[kind]), sq.get("alt", ALT[kind])
    if profile == "low" and not sq["heli"]:
        alt = LOW_ALT
    pts = [("TAKEOFF", base, speed)]
    marks = {}
    waits = {}
    if mission == "SOF" or profile == "sof":
        lz = _toward(target, base, SOF_STANDOFF)
        pts.append(("LZ", lz, speed))
        pts.append(("TARGET", lz, speed))   # the raid: the team is on the ground near the target
        waits[len(pts) - 1] = SOF_ON_GROUND / 2
        marks["tot"] = len(pts) - 1
        pts.append(("EXTRACT", lz, speed))
        waits[len(pts) - 1] = SOF_ON_GROUND / 2
    elif profile == "standoff":
        launch = _toward(target, base, STANDOFF_RANGE) if _dist(base, target) > STANDOFF_RANGE else base
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
    committed = sum(f["count"] for f in _war(c)["flights"] if f["squadron"] == sq["id"] and _active(f))
    return sq["total"] - committed


def _new_flight(c: Campaign, sq: dict, mission: str, count: int, takeoff: float, target_xy, target_site=None,
                loadout: str = "", profile: str = "standard", commit: bool = True):
    w = _war(c)
    w["seq"] += 1
    route, marks = _route(c, sq, mission, target_xy, takeoff, profile)
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
    _arm_flight(f, loadout)
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
        rate = ATTACKS_PER_PLAN[side]
        w["plans"] = w.get("plans", 0) + (side == "blue")
        attacks = int(rate) if rate >= 1 else int(w["plans"] % round(1 / rate) == 0)
        for _ in range(attacks):
            if not sites:
                break
            rng.shuffle(sites)
            for site in sites[:8]:
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


# --- resolution ---------------------------------------------------------------------------

def _lose(c: Campaign, f: dict, n: int, t: float) -> int:
    n = min(n, f["count"])
    if n <= 0:
        return 0
    f["count"] -= n
    f["losses"] += n
    sq = next(s for s in _war(c)["squadrons"] if s["id"] == f["squadron"])
    sq["total"] = max(0, sq["total"] - n)
    _war(c)["stats"][f"{f['side']}_lost"] += n
    if f["count"] <= 0:
        f["status"], f["end"] = "lost", t
    return n


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
    if profile == "sof" or f["mission"] == "SOF":
        threat = 0.0
        for s in c.alive_sites(other(f["side"])):
            if s["id"] in gone:
                continue
            d = _dist((s["x"], s["y"]), xy)
            threat += (1.0 if s["role"] == "shorad" and d <= 6_000 else
                       0.8 if s["role"] == "armor" and d <= 4_000 else
                       0.3 if s["role"] == "sam" and d <= 15_000 else 0.0)
        return threat + 0.5 * len(_enemy_caps(c, f["side"], xy, t, 60_000))
    low = profile == "low"
    threat = 0.0
    for s in c.alive_sites(other(f["side"])):
        if s["id"] in gone:
            continue
        d = _dist((s["x"], s["y"]), xy)
        if s["role"] == "sam" and d <= 40_000:
            threat += (0.5 if f["mission"] == "SEAD" else 1.0) * (0.6 if f["heli"] else 1.0) * (0.4 if low else 1.0)
        elif s["role"] == "ewr" and d <= 80_000:
            threat += 0.3 * (0.3 if low else 1.0)
        elif s["role"] == "shorad" and d <= 8_000:
            threat += (1.0 if f["heli"] else 0.5) * (2.0 if low else 1.0)
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


def strike_odds(c: Campaign, f: dict, t: float, gone=frozenset(), count: int | None = None) -> tuple[float, float]:
    """(chance the target is destroyed, chance each aircraft is lost) for an attack flight."""
    count = f["count"] if count is None else count
    profile = f.get("profile", "standard")
    if profile == "sof" or f["mission"] == "SOF":
        threat = _threat(c, f, f["target_xy"], t, gone)
        return max(0.15, 0.7 - 0.1 * threat), min(0.3, 0.03 + 0.05 * threat)
    if profile == "standoff":  # danger is at the release point; the weapons do the rest
        threat = _threat(c, f, f.get("launch_xy") or f["target_xy"], t, gone)
        pairs = count / 2
        return 1 - (1 - 0.55) ** pairs, min(0.25, 0.01 + 0.02 * threat)
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
            and not f.get("intercept") and f.get("intercepts", 0) < MAX_INTERCEPTS)


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
            if g["side"] == cap["side"] or tuple(sorted([cap["id"], g["id"]])) in engaged or g.get("aborted"):
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
            plan(c, t, rng)
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
            f["status"], f["end"] = "landed", t
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
    """Give up on flying the taken flight: the AI flies it and the clock can run again."""
    w = _war(c)
    f = next((x for x in w["flights"] if x["id"] == w["player_flight"]), None)
    if f and not f.get("flown"):
        f["player"] = False
    w["mode"], w["player_flight"] = "running", None


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
                        "profile": f.get("profile", "standard"),
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
    return [{k: by_id[i][k] for k in ("id", "side", "role", "x", "y", "zone")} for i in chosen]


def after_sortie(c: Campaign, sortie: dict, dead_units: set[str], flown: float = 0.0,
                 changes: list[str] | None = None) -> list[str]:
    """Apply what happened in DCS: aircraft lost on both sides (units named MBF:<flight>:n),
    then move the war on by the time spent in the mission and log one summary."""
    w = _war(c)
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
            n = _lose(c, f, n, w["clock"])
            if f["side"] == "blue":
                friendly_lost += n
            else:
                enemy_lost += n
    rng = random.Random()
    for s in c.data["sites"]:  # sites destroyed in DCS get repaired like any others
        if not s["alive"] and not s.get("withdrawn") and "repair_at" not in s:
            schedule_repair(s, w["clock"], rng)
    if w["player_flight"] == sortie.get("flight"):
        w["mode"], w["player_flight"] = "running", None
    if not mine:
        return []
    # The war moves on by the time you spent in the mission (it stays paused).
    began = mine.get("joined_at", mine["takeoff"])
    if flown and w["clock"] < began + flown:
        advance(c, began + flown)
    parts = []
    if enemy_lost:
        parts.append(f"{enemy_lost} enemy aircraft shot down")
    ground = [x for x in (changes or []) if not x.startswith("Lost")]
    lost_ground = [x for x in (changes or []) if x.startswith("Lost")]
    parts += [x[0].lower() + x[1:].rstrip(".") for x in ground]
    if friendly_lost:
        parts.append(f"{friendly_lost} friendly aircraft lost")
    parts += [x[0].lower() + x[1:].rstrip(".") for x in lost_ground]
    summary = (f"{clock_label(c)} · Back from DCS after {round(flown / 60)} min: {mine['callsign']} "
               f"({mine['type']}, {mine['mission']}): " + ("; ".join(parts) if parts else "no losses either side") + ".")
    c.log(summary, mine["mission"], mine["type"], side="blue", t=w["clock"], sortie=sortie["id"], debrief=True)
    return [summary]


# --- the map page -------------------------------------------------------------------------

def snapshot(c: Campaign) -> dict:
    w = _war(c)
    for f in w["flights"]:
        if "stores" not in f:
            _arm_flight(f)
    zones = {side: [{"field": n, **c.zone_status(n)} for n in c.fields_of(side)] for side in ("blue", "red")}
    return {
        "theatre": c.theatre, "scenario": c.data["scenario"], "start": w["start"], "clock": w["clock"],
        "clock_label": clock_label(c), "rate": w["rate"], "paused": w["paused"], "mode": w["mode"],
        "player_flight": w["player_flight"], "stats": w["stats"],
        "front": {"blue": c.front_zones("blue"), "red": c.front_zones("red")}, "zones": zones,
        "fields": [{"name": n, "side": side, "ll": c.data["field_ll"][n]} for n, side in sorted(c.data["fields"].items())],
        "sites": [{"id": s["id"], "side": s["side"], "role": s["role"], "ll": s["ll"], "alive": s["alive"],
                   "zone": s["zone"]} for s in c.data["sites"]],
        "squadrons": [dict(sq, available=available(c, sq)) for sq in w["squadrons"]],
        "ai_only": sorted(AI_ONLY),
        "battles": w.get("battles", []),
        "flights": [{k: v for k, v in f.items() if k != "route"} |
                    {"route": [{"name": r["name"], "ll": r["ll"], "t": r["t"], "alt": r.get("alt")}
                               for r in f["route"]]}
                    for f in w["flights"]],
        "events": [e for e in c.data["events"] if "t" in e or e.get("sortie")][-40:],
        "won": not c.fields_of("red"), "lost": not c.fields_of("blue"),
    }
