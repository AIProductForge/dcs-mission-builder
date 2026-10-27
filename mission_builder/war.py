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
PLAN_EVERY = 3600                   # the ATO is planned hourly...
PLAN_AHEAD = (15 * 60, 75 * 60)     # ...for takeoffs in this window from the planning time
CAP_ON_STATION = 3600
SPEED = {"jet": 230.0, "heli": 60.0, "orbit": 180.0}   # m/s
ALT = {"jet": 6500, "heli": 120}
RANGE = {"jet": 650_000, "heli": 130_000}
ATTACKS_PER_HOUR = {"blue": 2, "red": 1}
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
MISSION_FOR_ROLE = {"sam": "SEAD", "ewr": "SEAD", "structure": "Strike", "missile": "Strike",
                    "armor": "CAS", "shorad": "CAS", "farp": "Strike"}


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


def _route(c: Campaign, sq: dict, mission: str, target, takeoff: float) -> tuple[list[dict], dict]:
    kind = "heli" if sq["heli"] else "jet"
    base = c.data["field_pos"][sq["base"]]
    pts = [("TAKEOFF", base, SPEED[kind])]
    marks = {}
    if mission == "CAP":
        a, b = target, _side_step(base, target, 35_000)
        pts.append(("STATION", a, SPEED[kind]))
        marks["station"] = len(pts) - 1
        legs = max(2, int(CAP_ON_STATION * SPEED["orbit"] / _dist(a, b)))
        for i in range(legs):
            pts.append(("CAP" if i % 2 == 0 else "STATION", b if i % 2 == 0 else a, SPEED["orbit"]))
        marks["off"] = len(pts) - 1
    else:
        stand_off = 8_000 if sq["heli"] else 25_000
        if _dist(base, target) > stand_off * 1.5:
            pts.append(("IP", _toward(target, base, stand_off), SPEED[kind]))
        pts.append(("TARGET", target, SPEED[kind]))
        marks["tot"] = len(pts) - 1
        pts.append(("EGRESS", _side_step(base, target, stand_off), SPEED[kind]))
    pts.append(("LAND", base, SPEED[kind]))
    route, t = [], takeoff
    for i, (name, p, speed) in enumerate(pts):
        if i:
            t += _dist(route[-1]["xy"], p) / speed
        route.append({"name": name, "x": round(p[0]), "y": round(p[1]), "t": round(t, 1), "xy": p,
                      "alt": ALT[kind], "speed": speed})
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


def _new_flight(c: Campaign, sq: dict, mission: str, count: int, takeoff: float, target_xy, target_site=None):
    w = _war(c)
    w["seq"] += 1
    route, marks = _route(c, sq, mission, target_xy, takeoff)
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
    w["flights"].append(f)
    return f


def plan(c: Campaign, now: float, rng: random.Random) -> None:
    w = _war(c)
    for side in ("blue", "red"):
        enemy = other(side)
        squadrons = [sq for sq in w["squadrons"] if sq["side"] == side and sq["total"] > 0]
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
        for _ in range(ATTACKS_PER_HOUR[side]):
            if not sites:
                break
            rng.shuffle(sites)
            for site in sites[:8]:
                mission = MISSION_FOR_ROLE.get(site["role"], "Strike")
                fit = [sq for sq in squadrons if mission in sq["missions"] and available(c, sq) >= 2
                       and _dist(c.data["field_pos"][sq["base"]], (site["x"], site["y"]))
                       <= RANGE["heli" if sq["heli"] else "jet"]]
                if not fit and mission == "CAS":  # anyone who can hit ground targets
                    mission = "Strike"
                    fit = [sq for sq in squadrons if "Strike" in sq["missions"] and available(c, sq) >= 2
                           and _dist(c.data["field_pos"][sq["base"]], (site["x"], site["y"])) <= RANGE["jet"]]
                if not fit:
                    continue
                sq = rng.choice(fit)
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


def _threat(c: Campaign, f: dict, xy, t: float) -> float:
    threat = 0.0
    for s in c.alive_sites(other(f["side"])):
        d = _dist((s["x"], s["y"]), xy)
        if s["role"] == "sam" and d <= 40_000:
            threat += (0.5 if f["mission"] == "SEAD" else 1.0) * (0.6 if f["heli"] else 1.0)
        elif s["role"] == "ewr" and d <= 80_000:
            threat += 0.3
        elif s["role"] == "shorad" and d <= 8_000:
            threat += 1.0 if f["heli"] else 0.5
    threat += (1.0 if f["heli"] else 1.5) * len(_enemy_caps(c, f["side"], xy, t, 100_000))
    return threat


def _who(f: dict) -> str:
    return f"{f['callsign']} ({f['count']}x {f['type']}, {f['mission']})"


def resolve_attack(c: Campaign, f: dict, rng: random.Random) -> None:
    t = f["tot"]
    f["resolved"] = True
    threat = _threat(c, f, f["target_xy"], t)
    p_loss = min(0.25, 0.01 + 0.02 * threat)
    p_hit = max(0.12, 0.65 - 0.07 * threat)
    who = _who(f)
    lost = _lose(c, f, sum(rng.random() < p_loss for _ in range(f["count"])), t)
    site = next((s for s in c.data["sites"] if s["id"] == f["target"]), None)
    parts = []
    if f["count"] and site and site["alive"] and rng.random() < p_hit:
        site["alive"] = False
        schedule_repair(site, t, rng)
        parts.append(f"destroyed {_role_name(site)} near {site['zone']}")
    elif site and not site["alive"]:
        parts.append("found the target already destroyed")
    else:
        parts.append("missed the target")
    if lost:
        parts.append(f"lost {lost} aircraft")
    f["result"] = "; ".join(parts)
    c.log(f"{clock_label(c, t)} · {who} {f['result']}.", f["mission"], f["type"], side=f["side"], t=t)
    for change in c.capture_check(f["side"], t):
        c.log(f"{clock_label(c, t)} · {change}", side=f["side"], t=t)
        _after_capture(c, t)


def resolve_station(c: Campaign, f: dict, rng: random.Random) -> None:
    """A CAP arriving on station fights any enemy CAP in the area."""
    t = f["station"]
    f["resolved"] = True
    w = _war(c)
    x, y, _ = pos_at(f, t)
    for g in _enemy_caps(c, f["side"], (x, y), t, 120_000):
        pair = sorted([f["id"], g["id"]])
        if pair in w["engaged"] or not g["count"] or not f["count"]:
            continue
        w["engaged"].append(pair)
        rf, rg = RATING.get(f["type"], 0.7), RATING.get(g["type"], 0.7)
        lf = _lose(c, f, sum(rng.random() < 0.12 * rg / (rf + rg) for _ in range(f["count"])), t)
        lg = _lose(c, g, sum(rng.random() < 0.12 * rf / (rf + rg) for _ in range(g["count"])), t)
        c.log(f"{clock_label(c, t)} · Air battle: {f['callsign']} ({f['type']}) vs {g['callsign']} ({g['type']}). "
              f"Losses {lf} and {lg}.", "CAP", f["type"], side=f["side"], t=t)


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
    for f in w["flights"]:
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
                    sq["total"] = min(SQUADRON_SIZE, sq["total"] + REPLENISH_PER_DAY)
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
        elif kind == "land":
            f["status"], f["end"] = "landed", t
    w["clock"] = max(w["clock"], to_t)
    keep = w["clock"] - KEEP_FINISHED
    before = len(w["flights"])
    w["flights"] = [f for f in w["flights"] if _active(f) or f.get("end", 1e18) >= keep]
    live = {f["id"] for f in w["flights"]}
    w["engaged"] = [p for p in w["engaged"] if p[0] in live and p[1] in live]
    return changed or len(w["flights"]) != before


# --- flying a flight in DCS ---------------------------------------------------------------

def take_flight(c: Campaign, flight_id: str) -> dict:
    """Jump the clock to a planned blue flight's takeoff and hand it to the player."""
    w = _war(c)
    f = next((x for x in w["flights"] if x["id"] == flight_id), None)
    if not f or f["side"] != "blue" or f["status"] != "planned":
        raise RuntimeError("That flight can't be flown: pick a planned friendly flight.")
    advance(c, f["takeoff"] - 1)
    if f["status"] != "planned":
        raise RuntimeError("That flight was scrubbed before takeoff.")
    w["clock"] = f["takeoff"]
    f["player"] = True
    f["status"] = "airborne"
    w["mode"], w["paused"], w["player_flight"] = "in_dcs", True, f["id"]
    w["stats"]["sorties"] += 1
    c.log(f"{clock_label(c)} · You take {f['callsign']} ({f['count']}x {f['type']}, {f['mission']}"
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
                        "mission": f["mission"], "x": x, "y": y, "alt": f["route"][0]["alt"],
                        "speed": f["route"][0]["speed"], "route": rest, "delay": round(at - now),
                        "base": f["base"]})
    return out


def after_sortie(c: Campaign, sortie: dict, dead_units: set[str]) -> list[str]:
    """Losses logged in DCS: the player's flight and any AI flight that was in the mission."""
    w = _war(c)
    lost: dict[str, int] = {}
    for u in dead_units:
        if u.startswith("MBF:"):
            fid = u.split(":")[1]
            lost[fid] = lost.get(fid, 0) + 1
    notes = []
    for f in w["flights"]:
        n = lost.get(f["id"], 0)
        if f["id"] == sortie.get("flight"):
            f["flown"], f["resolved"], f["player"] = True, True, False
            f["result"] = "flown in DCS" + (f"; lost {n}" if n else "")
        if n:
            n = _lose(c, f, n, w["clock"])
            notes.append(f"{f['callsign']} lost {n} aircraft in DCS.")
    rng = random.Random()
    for s in c.data["sites"]:  # sites destroyed in DCS get repaired like any others
        if not s["alive"] and not s.get("withdrawn") and "repair_at" not in s:
            schedule_repair(s, w["clock"], rng)
    if w["player_flight"] == sortie.get("flight"):
        w["mode"], w["player_flight"] = "running", None
    for note in notes:
        c.log(f"{clock_label(c)} · {note}", side="blue", t=w["clock"])
    return notes


# --- the map page -------------------------------------------------------------------------

def snapshot(c: Campaign) -> dict:
    w = _war(c)
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
        "flights": [{k: v for k, v in f.items() if k != "route"} |
                    {"route": [{"name": r["name"], "ll": r["ll"], "t": r["t"]} for r in f["route"]]}
                    for f in w["flights"]],
        "events": [e for e in c.data["events"] if "t" in e or e.get("sortie")][-40:],
        "won": not c.fields_of("red"), "lost": not c.fields_of("blue"),
    }
