# DCS Mission Builder

Generates single-player `.miz` files for the aircraft you own, without the Mission Editor.

Start it with **DCS Mission Builder.bat**. It opens in your browser at http://127.0.0.1:8765
as a mission briefing document: tasking fields on the left, the briefing (situation, mission,
route table, map, threats, support, command & signal) on the right.

- **Generate** rerolls every field that isn't pinned. Changing a field pins it automatically;
  click the pin to toggle. Pinning the home field also pins its theatre.
- **Apply edits** rebuilds with exactly the current values, keeping the same layout and codename.
- **Save .miz** writes the mission you're looking at to `Saved Games\DCS\Missions\Generated`,
  so it shows up in DCS under Mission → My Missions → Generated.
- The page prints cleanly (Ctrl+P) as a kneeboard-style briefing.
- Closing the tab is enough: the server stops itself a minute later.

The map uses OpenTopoMap tiles and the Leaflet library from the internet. Offline, the map
panel says so and everything else still works.

## What it knows about your install
- **Aircraft**: every flyable type named in an `entry.lua` under `F:\DCS World\Mods\aircraft`.
  DemoMods (trial aircraft) are skipped. Community mods aren't supported yet.
- **Maps**: installed terrains pydcs can write for (not MarianasWWII).
- **Loadouts**: DCS's own Mission Editor loadouts, then DCS Liberation's curated ones
  (read from your Liberation install). The **Loadout** field lists every stored loadout for
  the selected aircraft, with the ones suited to the mission type first; when it isn't pinned,
  Generate picks a random suited one. The briefing lists the stores per aircraft. F-5E, F-86F,
  MiG-15 and TF-51D have no stored loadouts and fly with guns only.

## Scenarios (who owns what)
Geography comes from DCS Liberation's campaign files (read from your Liberation install and
cached in `scenarios_cache.json`). Each scenario says which airfields are blue and which are
red, and where on land each side has SAM sites, radars, armour, depots and so on. You fly from
your side's airfields, enemy aircraft come from theirs, and ground objectives are placed on
their own land positions, never in the sea. **Play as** swaps sides. Some Liberation campaigns
cast the side you might expect to be the enemy as blue (several Persian Gulf ones put blue in
Iran); swap sides or pin a different scenario if you prefer. With no scenario, any airfield can
be home and objectives sit next to an enemy airfield.

## Missions
Free flight, CAP, Intercept, Strike, CAS and SEAD (helicopters: Free flight, Strike, CAS).
Helicopters start from fields near the front and get objectives within reach.

**Complexity** (1–5) scales each mission:
| Level | Air defence at the objective | Elsewhere | Enemy air | Friendly AI |
|---|---|---|---|---|
| 1 Low | none | none | CAP: 2x MiG-21 | none |
| 2 Moderate | ZSU-23-4, SA-9 | none | CAP: 2x MiG-23 | none |
| 3 Medium | SA-8, ZSU-23-4, SA-13 | 1 SAM site, pop-up SHORAD | CAP: 4x MiG-29S; alert pair scrambles | F-15C CAP |
| 4 High | SA-6, SA-15, ZSU-23-4 | 2 SAM sites, EWR, pop-up SHORAD | CAP: MiG-29S + Su-27, second wave | + F-16C SEAD |
| 5 Very high | SA-11, SA-15, SA-19 | 3 SAM sites, EWR, pop-up SA-15 | CAP: Su-27, Su-30, MiG-31, second wave; standing Su-27 CAP | + F-16C SEAD |

SEAD targets grow from an SA-8 (1) to an SA-10 with SA-15 cover (5); Strike targets from 2
structures to 6 plus a Scud battery; CAS enemy armour from 3 to 8 vehicles; Intercept raids from
2 Su-24s to 4 Tu-22M3s and 2 Tu-95s with escorts. Helicopter missions cap air defence at level 3.

Triggers report mission success when the targets are destroyed, and failure when an Intercept
raid reaches your field. Pop-up air defence and enemy scrambles activate as you approach the
objective; the briefing warns about them without saying exactly what or where.

Date matters for some systems: the Modern F-15E SIT/JTIDS mod needs a mission dated 1998 or later.

## Files
- `mission_builder/inventory.py`: owned aircraft, terrains, loadout folders
- `mission_builder/scenarios.py`: blue/red airfields and land positions from Liberation campaigns
- `mission_builder/generator.py`: builds the mission (no UI, can be scripted)
- `mission_builder/server.py`: local web server and JSON API (127.0.0.1 only)
- `mission_builder/web/`: the briefing page (HTML, CSS, JS)
- `mission_builder/app.py`: the old Tkinter window (superseded, still works)
- `config.json`: paths and your last choices
- `.venv`: private Python with pydcs pinned to commit 03dd89f (the version Liberation 15.0.0 uses)
