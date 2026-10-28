"""Air defence and ground systems the campaign places in DCS, with the ranges DCS gives them.

Ranges come from pydcs's unit data (`threat_range`, `detection_range`, in metres), which is
generated from DCS's own unit database, so the war's odds and the map's rings use the same
numbers as the units DCS flies against. A site's ranges are those of the units actually
placed: a damaged site keeps the first units of its list (see `units_for`), so a SAM that has
lost its launchers still sees but no longer shoots.
"""
from __future__ import annotations

from dcs import vehicles

AD, _A = vehicles.AirDefence, vehicles.Armor

# name: units in the order they survive damage (the radar a site can't work without first).
SYSTEMS: dict[str, list] = {
    "SA-6": [AD.Kub_1S91_str, AD.Kub_2P25_ln, AD.Kub_2P25_ln, AD.Kub_2P25_ln],
    "Hawk": [AD.Hawk_sr, AD.Hawk_tr, AD.Hawk_ln, AD.Hawk_ln],
    "1L13 EWR": [AD.X_1L13_EWR],
    "Hawk CWAR": [AD.Hawk_cwar],
    "ZSU-23-4 / SA-13": [AD.ZSU_23_4_Shilka, AD.Strela_10M3],
    "Vulcan / Avenger": [AD.Vulcan, AD.M1097_Avenger],
    "T-72 company": [_A.T_72B, _A.BMP_2, _A.BTR_80],
    "Abrams company": [_A.M_1_Abrams, _A.M_2_Bradley, _A.M_2_Bradley],
    "Scud battery": [vehicles.MissilesSS.Scud_B, vehicles.MissilesSS.Scud_B, vehicles.Unarmed.Ural_375],
    "MLRS battery": [vehicles.Artillery.MLRS, vehicles.Artillery.MLRS, vehicles.Unarmed.M_818],
}
# What each side places for each role (these are the systems the missions placed before sites
# had a system of their own, so older campaigns keep their balance).
DEFAULT = {
    "red": {"sam": "SA-6", "ewr": "1L13 EWR", "shorad": "ZSU-23-4 / SA-13", "armor": "T-72 company",
            "missile": "Scud battery"},
    "blue": {"sam": "Hawk", "ewr": "Hawk CWAR", "shorad": "Vulcan / Avenger", "armor": "Abrams company",
             "missile": "MLRS battery"},
}

ROLE = {name: role for side in DEFAULT.values() for role, name in side.items()}


def system_for(side: str, role: str) -> str | None:
    return DEFAULT[side].get(role)


def units_for(system: str, strength: float = 1.0) -> list:
    """The units placed for a site of this system at this strength (survivors only)."""
    types = SYSTEMS[system]
    return types[:max(1, round(len(types) * strength))]


def ranges(system: str | None, strength: float = 1.0) -> tuple[int, int]:
    """(threat, detection) range in metres of a site's placed units: how far it can shoot an
    aircraft, how far its radars see one."""
    if not system or system not in SYSTEMS:
        return 0, 0
    units = units_for(system, strength)
    return (max(getattr(u, "threat_range", 0) or 0 for u in units),
            max(getattr(u, "detection_range", 0) or 0 for u in units))


def site_ranges(site: dict) -> tuple[int, int]:
    return ranges(site.get("system") or system_for(site["side"], site["role"]), site.get("strength", 1.0))
