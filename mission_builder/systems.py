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
    "SA-10": [AD.S_300PS_64H6E_sr, AD.S_300PS_40B6M_tr, AD.S_300PS_5P85C_ln, AD.S_300PS_54K6_cp,
              AD.S_300PS_5P85C_ln, AD.S_300PS_5P85D_ln, AD.S_300PS_5P85D_ln],
    "SA-11": [AD.SA_11_Buk_SR_9S18M1, AD.SA_11_Buk_LN_9A310M1, AD.SA_11_Buk_CC_9S470M1,
              AD.SA_11_Buk_LN_9A310M1, AD.SA_11_Buk_LN_9A310M1],
    "SA-15": [AD.Tor_9A331, AD.Tor_9A331],
    "SA-8": [AD.Osa_9A33_ln, AD.Osa_9A33_ln],
    "1L13 EWR": [AD.X_1L13_EWR],
    "55G6 EWR": [AD.X_55G6_EWR],
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

ROLE = {**{name: role for side in DEFAULT.values() for role, name in side.items()},
        "SA-10": "sam", "SA-11": "sam", "SA-15": "sam", "SA-8": "sam", "55G6 EWR": "ewr"}
# How much a SAM site of this system counts in the war's odds, against an SA-6 at 1.0: more
# launchers, longer reach, harder to suppress. A judgement, not DCS data (the ranges are).
WEIGHT = {"SA-10": 2.0, "SA-11": 1.4, "SA-6": 1.0, "Hawk": 1.0, "SA-15": 0.8, "SA-8": 0.7}

# Red airbase air defence by the airfield's importance (blue's is in campaign.AIRBASE_DEFENCE).
RED_AIRBASE_DEFENCE = {
    "major": ["SA-10", "SA-11", "SA-15", "SA-15", "55G6 EWR"],
    "main": ["SA-11", "SA-15", "1L13 EWR"],
    "minor": ["SA-6", "SA-8"],
}
# Fields named here get that tier; other red fields get "main" (or "minor" with no runway).
FIELD_TIER = {
    "PersianGulf": {"Bandar Abbas Intl": "major", "Shiraz Intl": "major", "Kerman": "main",
                    "Kish Intl": "main", "Qeshm Island": "minor", "Abu Musa Island": "minor"},
}


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
