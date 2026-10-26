"""Work out what the user can fly and where, from the DCS install itself.

Owned aircraft: every flyable pydcs type whose exact name is quoted in an
installed module's entry.lua under <DCS>/Mods/aircraft. DemoMods (trial or
unowned aircraft) are deliberately not scanned.
Terrains: folders under <DCS>/Mods/terrains that pydcs can generate for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from dcs import helicopters, planes, terrain

# pydcs terrain classes keyed by the DCS terrain folder name.
TERRAINS = {
    "Caucasus": terrain.Caucasus,
    "Nevada": terrain.Nevada,
    "PersianGulf": terrain.PersianGulf,
    "Syria": terrain.Syria,
    "MarianaIslands": terrain.MarianaIslands,
    "Normandy": terrain.Normandy,
    "TheChannel": terrain.TheChannel,
    "Falklands": terrain.Falklands,
    "Sinai": terrain.Sinai,
    "Kola": terrain.Kola,
    "Germany": terrain.Germany,
}


@dataclass(frozen=True)
class OwnedAircraft:
    type_id: str          # DCS type name, e.g. "F-15ESE"
    module_folder: str    # folder under Mods/aircraft it was found in
    is_helicopter: bool

    @property
    def unit_type(self):
        return helicopters.helicopter_map.get(self.type_id) or planes.plane_map[self.type_id]


def configure_payloads(dcs_dir: Path, liberation_payloads: Path | None = None) -> None:
    """Point pydcs at loadout files. Its own registry lookup resolves the wrong
    folder on this machine, which leaves every pylon empty. Modern modules
    (F-15E, Hornet, Viper...) ship no loadout .lua that pydcs can read, so
    DCS Liberation's curated loadouts are used as a fallback when present.
    Must run before any mission is built (pydcs caches the scan)."""
    from dcs.unittype import PayloadDirectories
    dirs = [dcs_dir / "MissionEditor" / "data" / "scripts" / "UnitPayloads"]
    dirs += sorted(p for p in (dcs_dir / "Mods" / "aircraft").glob("*/*")
                   if p.is_dir() and p.name.lower() == "unitpayloads")
    PayloadDirectories._dcs = dirs
    PayloadDirectories._mod = []
    if liberation_payloads is not None and liberation_payloads.is_dir():
        PayloadDirectories.set_fallback(liberation_payloads)


def owned_aircraft(dcs_dir: Path) -> list[OwnedAircraft]:
    flyable = {k: False for k, v in planes.plane_map.items() if v.flyable}
    flyable.update({k: True for k, v in helicopters.helicopter_map.items() if v.flyable})
    found: dict[str, OwnedAircraft] = {}
    for entry in sorted((dcs_dir / "Mods" / "aircraft").glob("*/entry.lua")):
        text = entry.read_text(encoding="utf-8", errors="replace")
        quoted = set(re.findall(r"""["']([^"'\r\n]+)["']""", text))
        for type_id in quoted & flyable.keys():
            found.setdefault(type_id, OwnedAircraft(type_id, entry.parent.name, flyable[type_id]))
    return sorted(found.values(), key=lambda a: a.type_id.lower())


def installed_terrains(dcs_dir: Path) -> list[str]:
    folder = dcs_dir / "Mods" / "terrains"
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name in TERRAINS)
