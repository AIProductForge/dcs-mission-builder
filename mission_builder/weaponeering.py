"""Weaponeering: which weapon a strike flight puts on which unit of its target, and how many.

A flight's stores (per aircraft, as the ATO shows them: "20x AGM-86D") and its target's units
(DCS type names) go in; out comes a list of aimpoints, each with a weapon, how many each
aircraft releases on it, and the chance it kills that unit. Units are taken in priority order
(on a SAM site: radars first, then launchers that have their own radar, then the rest); each
gets the best weapon on board for it and enough for GOAL, until the weapons run out.

The mission generator turns the aimpoints into DCS Bombing orders (weapon category and expend
set); the war engine uses the same aimpoints for its odds, so the two agree.

The kill chances are estimates, calibrated where a DCS test gave a number:
  - 2 GBU-27 from F-117s on a T-72 platoon: 1 kill (2026-10-28, test B).
  - 28 AGM-86D spread over 7 T-72s: 1 kill, 1 damaged; 40 on one point: 1 kill (test C).
  - An SA-10 shot down about 10 of 40 incoming AGM-86s (test C).
"""
from __future__ import annotations

import math
import re

GOAL = 0.8              # chance of killing each unit a flight aims at
MAX_AIMPOINTS = 6       # per flight
# Weapons released per aircraft on one aimpoint: DCS's Bombing "expend" values.
EXPEND_STEPS = (("One", 1), ("Two", 2), ("Four", 4))

# Weapon classes: (name, store patterns, DCS weapon category for the order, kill chance per
# weapon by target class). First matching pattern wins.
TARGETS = ("radar", "launcher_radar", "launcher", "aaa", "armor", "soft", "structure")
WEAPONS = [
    ("anti-radiation", r"AGM-88|Kh-31P|Kh-25MP|Kh-58|ALARM", "ARM",
     {"radar": 0.7, "launcher_radar": 0.55}),
    ("cruise missile", r"AGM-86|AGM-158|Kh-59|Kh-55|Kh-65|AGM-84E|SLAM|Storm", "CruiseMissile",
     {"radar": 0.35, "launcher_radar": 0.3, "launcher": 0.3, "aaa": 0.25, "armor": 0.06, "soft": 0.3, "structure": 0.6}),
    ("glide bomb (cluster)", r"AGM-154[AB]", "Guided",
     {"radar": 0.5, "launcher_radar": 0.5, "launcher": 0.5, "aaa": 0.5, "armor": 0.3, "soft": 0.6, "structure": 0.1}),
    ("glide bomb", r"AGM-154", "Guided",
     {"radar": 0.5, "launcher_radar": 0.45, "launcher": 0.45, "aaa": 0.4, "armor": 0.2, "soft": 0.45, "structure": 0.6}),
    ("guided bomb", r"GBU-|KAB-", "GuidedBombs",
     {"radar": 0.6, "launcher_radar": 0.6, "launcher": 0.6, "aaa": 0.55, "armor": 0.5, "soft": 0.6, "structure": 0.7}),
    ("air-to-ground missile", r"AGM-65|AGM-114|Hellfire|Kh-25|Kh-29|Vikhr|9M1|AGM-62", "ASM",
     {"radar": 0.6, "launcher_radar": 0.6, "launcher": 0.6, "aaa": 0.55, "armor": 0.6, "soft": 0.6, "structure": 0.3}),
    ("cluster bomb", r"CBU-|RBK|BL755|Rockeye|BLU-", "ClusterBombs",
     {"radar": 0.45, "launcher_radar": 0.45, "launcher": 0.45, "aaa": 0.45, "armor": 0.3, "soft": 0.55, "structure": 0.05}),
    ("bomb", r"Mk-8|Mk 8|FAB-|OFAB|BetAB|M117|SAMP|BAP|Durandal", "IronBombs",
     {"radar": 0.25, "launcher_radar": 0.25, "launcher": 0.25, "aaa": 0.2, "armor": 0.1, "soft": 0.3, "structure": 0.35}),
    ("rockets", r"S-8|S-13|S-5|S-24|S-25|Hydra|Zuni|LAU-|B-8|UB-|M261|Rkts", "Rockets",
     {"radar": 0.3, "launcher_radar": 0.3, "launcher": 0.3, "aaa": 0.3, "armor": 0.15, "soft": 0.35, "structure": 0.1}),
]
# Which of these get shot down by the defences over the target on the way in (missiles and
# glide bombs released from a distance); bombs come with the aircraft, which face the defences themselves.
INTERCEPTABLE = {"cruise missile", "glide bomb (cluster)", "glide bomb", "anti-radiation"}
# Per weapon: chance to get through = 1 / (1 + this x the air defence over the target).
# From test C: 40 AGM-86 against one SA-10 (defence 2), ~10 shot down -> ~0.75 through.
INTERCEPT = 0.17

PRIORITY = {"radar": 0, "launcher_radar": 1, "launcher": 2, "aaa": 3, "armor": 4, "soft": 5, "structure": 6}


def target_class(type_name: str) -> str:
    """A DCS unit type's target class (see TARGETS)."""
    t = type_name.lower()
    if re.search(r"(^| )(sr|tr|str)$|ewr|cwar|snr|64h6e|40b6m|9s18|p-19|flat face|tin shield|patriot str|nasams_radar", t):
        return "radar"
    if re.search(r"tor |osa |buk ln|9a310|tunguska|2s6|9a331", t):
        return "launcher_radar"
    if re.search(r"(^| )ln$|5p85|2p25|hawk ln|patriot ln|launcher", t):
        return "launcher"
    if re.search(r"zsu|shilka|vulcan|gepard|strela|avenger|igla|stinger|zu-23|chaparral", t):
        return "aaa"
    if re.search(r"t-72|t-80|t-90|t-55|bmp|btr|abrams|m-1 |m1a|m-2|bradley|leopard|lav|mtlb|marder", t):
        return "armor"
    if re.search(r"workshop|tech|tower|bunker|hangar|depot|fortification|command center|comms|factory|structure", t):
        return "structure"
    return "soft"


def weapon_class(store: str):
    """(class name, DCS weapon category, kill chances) for a store label, or None (not air-to-ground)."""
    for name, pattern, category, pk in WEAPONS:
        if re.search(pattern, store):
            return name, category, pk
    return None


def _inventory(stores: list[str]) -> list[dict]:
    """Air-to-ground weapons each aircraft carries: [{label, class, category, pk, per_aircraft}]."""
    out = []
    for s in stores or []:
        m = re.match(r"^(\d+)x\s+(.+)$", s.strip())
        n, label = (int(m[1]), m[2]) if m else (1, s.strip())
        wc = weapon_class(label)
        if wc:
            out.append({"label": label, "class": wc[0], "category": wc[1], "pk": wc[2], "per_aircraft": n})
    return out


def weaponeer(stores: list[str], aircraft: int, units: list[str], defence: float = 0.0,
              accuracy: float = 1.0) -> list[dict]:
    """Aimpoints for a flight of `aircraft` carrying `stores` (per aircraft) against `units` (DCS
    type names, in placement order). `defence`: the air defence over the target (it shoots down
    stand-off weapons); `accuracy`: how much the defences spoil a run for weapons delivered
    from overhead (1 = undisturbed). Returns [{unit (index), target_class, weapon, class,
    category, per_aircraft, pk_each, p_kill}] in the order to attack."""
    inv = _inventory(stores)
    if not inv or aircraft <= 0:
        return []
    left = {w["label"]: w["per_aircraft"] for w in inv}
    order = sorted(range(len(units)), key=lambda i: (PRIORITY[target_class(units[i])], i))
    # Pass 1, spread: one weapon per aircraft on each unit in priority order (every radar of a
    # SAM site gets something before any unit gets a second: a site with one radar left still sees).
    plan = []
    for i in order:
        if len(plan) >= MAX_AIMPOINTS:
            break
        tc = target_class(units[i])
        best = None
        for w in inv:
            if left[w["label"]] <= 0:
                continue
            p = w["pk"].get(tc, 0.0)
            p *= (1 / (1 + INTERCEPT * defence)) if w["class"] in INTERCEPTABLE else accuracy
            if p > 0.05 and (best is None or (p, left[w["label"]]) > (best[1], left[best[0]["label"]])):
                best = (w, p)
        if not best:
            continue
        w, p = best
        left[w["label"]] -= 1
        plan.append({"unit": i, "target_class": tc, "weapon": w["label"], "class": w["class"], "category": w["category"],
                     "per_aircraft": 1, "pk_each": round(p, 3)})
    # Pass 2, concentrate: what's left raises the units still short of GOAL, in priority order,
    # in DCS's expend steps (1 -> 2 -> 4 per aircraft).
    for a in plan:
        for _, k in EXPEND_STEPS[1:]:
            if 1 - (1 - a["pk_each"]) ** (a["per_aircraft"] * aircraft) >= GOAL:
                break
            extra = k - a["per_aircraft"]
            if extra <= 0 or left[a["weapon"]] < extra:
                break
            left[a["weapon"]] -= extra
            a["per_aircraft"] = k
    for a in plan:
        a["expend"] = {1: "One", 2: "Two", 4: "Four"}[a["per_aircraft"]]
        a["p_kill"] = round(1 - (1 - a["pk_each"]) ** (a["per_aircraft"] * aircraft), 3)
    return plan


def p_destroyed(plan: list[dict], units: list[str], needed: float = 0.5, blind_counts: bool = True) -> float:
    """Chance a site counts as destroyed when each aimpoint kills its unit with its p_kill,
    independently: at least `needed` of its units dead, or (an air defence site, blind_counts)
    every one of its radars dead, which leaves it blind and out of the fight."""
    placed = len(units)
    if placed <= 0 or not plan:
        return 0.0
    need = max(1, math.ceil(placed * needed))
    radars = {i for i, u in enumerate(units) if target_class(u) == "radar"} if blind_counts else set()
    total = 0.0
    for mask in range(1 << len(plan)):   # every combination of hits (at most MAX_AIMPOINTS)
        p, dead = 1.0, set()
        for j, a in enumerate(plan):
            if mask >> j & 1:
                p *= a["p_kill"]
                dead.add(a["unit"])
            else:
                p *= 1 - a["p_kill"]
        if len(dead) >= need or (radars and radars <= dead):
            total += p
    return total


def describe(plan: list[dict], units: list[str]) -> str:
    """'2x GBU-27 on the S-300PS 64H6E sr (80%); ...' for a briefing or the ATO."""
    return "; ".join(f"{a['per_aircraft']}x {a['weapon']} each on the {units[a['unit']]} ({round(a['p_kill'] * 100)}%)"
                     for a in plan)
