"""Blue/red geography for each theatre, taken from DCS Liberation's campaign files.

Each Liberation campaign .miz marks which airfields belong to blue and red and
places placeholder units on land (and ships at sea) where it wants SAM sites,
early-warning radars, armour, depots and so on. We only use that as geography:
  * which airfields a side owns (home fields come from blue, enemy air from red),
  * land positions by role, so generated ground units never end up in the sea.
The placeholder unit types are mapped to roles by name below; Liberation's own
meaning for each placeholder may be richer, we only need "what kind of site goes here".

Parsing all campaigns takes ~20 s, so the result is cached in scenarios_cache.json
next to the project and rebuilt when the campaigns folder changes.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

CACHE = Path(__file__).resolve().parent.parent / "scenarios_cache.json"
CACHE_VERSION = 1

# Liberation's YAML names some theatres differently from DCS's terrain folders.
THEATRE_ALIASES = {"Persian Gulf": "PersianGulf", "Mariana Islands": "MarianaIslands", "Marianas": "MarianaIslands"}

ROLES = {
    "armor": {"M-1 Abrams", "M-113", "M1043 HMMWV Armament"},
    "sam": {"S_75M_Volhov", "S-300PS 5P85C ln", "S-300PS 5P85D ln", "S-300PS 64H6E sr", "Patriot ln", "Hawk ln",
            "5p73 s-125 ln"},
    "shorad": {"Vulcan", "2S6 Tunguska", "rapier_fsa_launcher", "M1097 Avenger", "ZSU-23-4 Shilka",
               "Strela-1 9P31", "flak18"},
    "ewr": {"1L13 EWR"},
    "missile": {"Scud_B", "hy_launcher"},
    "structure": {"Tech combine", ".Ammunition depot", "Workshop A", "Comms tower M", "GeneratorF",
                  ".Command Center", "SKP-11"},
    "farp": {"Invisible FARP"},
}


@dataclass
class Scenario:
    name: str                  # Liberation campaign name, e.g. "Caucasus - Black Sea"
    theatre: str               # DCS terrain folder name
    blue_fields: list[str]
    red_fields: list[str]
    # role -> list of [x, y] map positions (pydcs/DCS coordinates)
    blue_sites: dict[str, list[list[float]]] = field(default_factory=dict)
    red_sites: dict[str, list[list[float]]] = field(default_factory=dict)
    blue_ships: list[list[float]] = field(default_factory=list)
    red_ships: list[list[float]] = field(default_factory=list)


def _role(type_name: str) -> str | None:
    for role, names in ROLES.items():
        if type_name in names:
            return role
    return None


def _read_campaign(yaml_path: Path) -> tuple[str, str] | None:
    """(theatre, miz filename) from a campaign YAML, without needing a YAML parser."""
    text = yaml_path.read_text(encoding="utf-8", errors="replace")
    theatre = re.search(r'^theater:\s*"?([^"\n]+)', text, re.M)
    miz = re.search(r'^miz:\s*"?([^"\n]+)', text, re.M)
    name = re.search(r'^name:\s*"?([^"\n]+)', text, re.M)
    if not (theatre and miz and name):
        return None
    t = theatre.group(1).strip()
    return name.group(1).strip(), THEATRE_ALIASES.get(t, t), miz.group(1).strip()


def _parse(miz_path: Path, name: str, theatre: str) -> Scenario:
    from dcs.mission import Mission
    m = Mission()
    m.load_file(str(miz_path))
    sc = Scenario(name=name, theatre=theatre,
                  blue_fields=sorted(n for n, a in m.terrain.airports.items() if a.coalition == "BLUE"),
                  red_fields=sorted(n for n, a in m.terrain.airports.items() if a.coalition == "RED"))
    for side in ("blue", "red"):
        sites: dict[str, list] = getattr(sc, f"{side}_sites")
        ships: list = getattr(sc, f"{side}_ships")
        for country in m.coalition[side].countries.values():
            for g in country.vehicle_group + country.static_group:
                role = _role(g.units[0].type)
                if role:
                    sites.setdefault(role, []).append([round(g.position.x), round(g.position.y)])
            for g in country.ship_group:
                ships.append([round(g.position.x), round(g.position.y)])
    return sc


def load(campaigns_dir: Path, theatres: list[str]) -> dict[str, list[Scenario]]:
    """Scenarios usable for single-player missions, grouped by theatre."""
    if not campaigns_dir.is_dir():
        return {}
    stamp = max((p.stat().st_mtime for p in campaigns_dir.iterdir()), default=0)
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get("version") == CACHE_VERSION and cached.get("stamp") == stamp \
                and cached.get("dir") == str(campaigns_dir):
            return _group([Scenario(**s) for s in cached["scenarios"]], theatres)

    found = []
    for yaml_path in sorted(campaigns_dir.glob("*.yaml")):
        info = _read_campaign(yaml_path)
        if not info:
            continue
        name, theatre, miz = info
        miz_path = campaigns_dir / miz
        if not miz_path.exists():
            continue
        try:
            found.append(_parse(miz_path, name, theatre))
        except Exception:  # a campaign for a map pydcs can't read, or a malformed file
            logging.exception("could not read campaign %s", miz_path.name)
    CACHE.write_text(json.dumps({"version": CACHE_VERSION, "stamp": stamp, "dir": str(campaigns_dir),
                                 "scenarios": [asdict(s) for s in found]}), encoding="utf-8")
    return _group(found, theatres)


def _group(found: list[Scenario], theatres: list[str]) -> dict[str, list[Scenario]]:
    out: dict[str, list[Scenario]] = {}
    for sc in found:
        # A single-player mission needs a blue airfield to start from and a red one to fight.
        if sc.theatre in theatres and sc.blue_fields and sc.red_fields:
            out.setdefault(sc.theatre, []).append(sc)
    for lst in out.values():
        lst.sort(key=lambda s: s.name)
    return out
