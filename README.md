# DCS Mission Builder

Generates single-player `.miz` files for the aircraft you own, without the Mission Editor.

Start it with **DCS Mission Builder.bat**. It opens in your browser at http://127.0.0.1:8765
as a mission briefing document: tasking fields on the left, the briefing (situation, mission,
route table, map, threats, support, command & signal) on the right.

- **Generate** rerolls every field that isn't pinned. Changing a field pins it automatically;
  click the pin to toggle. Pinning the home field also pins its map.
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

## Campaigns (one per map)
Each map runs one persistent campaign, started from one DCS Liberation campaign file used as the
baseline (read from your Liberation install and cached in `scenarios_cache.json`):

| Map | Baseline | You start with |
|---|---|---|
| Caucasus | Black Sea | Georgia's coast (Kutaisi, Senaki, Kobuleti, Sukhumi, Gudauta) |
| Persian Gulf | Operation Noisy Cricket | the UAE (Al Dhafra, Al Minhad) against six Iranian fields |
| Syria | Operation Allied Sword | Haifa and Ramat David against seven Syrian/Lebanese fields |
| Nevada | Exercise Vegas Nerve | Tonopah against Nellis, Creech, Groom Lake, Boulder City |
| Marianas | Operation Velvet Thunder | Guam against Rota, Saipan, Tinian |

The baseline gives each side its airfields and land positions (SAM sites, radars, armour,
depots...). Every position belongs to the zone of its side's nearest airfield, and every airfield's
zone holds at least 12 positions (topped up with a garrison on the field).

- A position is destroyed when at least half of the units placed on it in one sortie are killed.
  The owner repairs it 12–30 campaign hours later unless its airfield falls first.
- An airfield is captured when 60% of the positions in its zone are destroyed, and 60% of its
  SAM and early-warning radar positions are among them (armour alone won't take a field).
  What's left of the zone withdraws, the new owner moves in a garrison of 8, and the field
  can't change hands again for 24 campaign hours.
- Take every enemy airfield to win; lose all of yours and the campaign is lost.
  **Reset map** starts that map's campaign again.

## The air war (campaign map)
**Campaign map** (`war.html`, linked from the Tasking panel) shows the war as it runs, in the
spirit of Falcon BMS's dynamic campaign:

- **Clock.** Run or pause it at 1×, 4×, 16×, 64× or 256×. It runs only while the page is open.
- **Squadrons.** Blue flies aircraft you own (one squadron per module, up to six jets and a
  helicopter); red flies MiG-29S, Su-27, Su-24M, Su-25T, Su-25 and Mi-24V. Squadrons start with
  12 aircraft and get 3 back a day.
- **ATO.** Every half hour each side plans: a CAP over the front when none is covering it (so a
  CAP that's shot down is replaced quickly), plus a strike, SEAD or CAS against the other side's
  front-line positions (two attacks an hour each). Squadrons that fly the right task go first;
  any strike squadron can take a ground target when they're busy. The map shows each flight
  from takeoff to landing; click one for its route, target and result. **Coverage rings** (top right
  of the map) show the air defence of either side: SAMs (40 km), short-range air defence (8 km) and
  early-warning radar (80 km). The radii are the ranges the war's odds use, not each system's exact
  DCS range. **Map** switches the base map (topographic, satellite, plain) and adds a hillshade
  relief overlay. A selected flight shows its **terrain profile**: ground height along its route
  (Copernicus 90 m elevation data via Open-Meteo) against its altitude, with the lowest clearance;
  stretches within 100 m of the ground are red. Legs at or below 500 m are flown above ground
  level in DCS (RADIO altitude), so low-level and helicopter routes follow the terrain.
- **AI results.** Flights nobody flies are resolved when they reach their target: hit and loss
  odds depend on the SAMs, short-range air defence and radar around the target and on enemy
  CAP nearby.
- **Intercepts.** Every 5 campaign minutes each CAP on station looks for enemy flights within
  80 km (50% chance per look by day, 35% at night, 15% against the F-117), flies out to meet the
  nearest one it sees (no further than 150 km from its station), fights, and returns to its
  station; at most two intercepts per sortie. Fighter against fighter, both sides can lose
  aircraft (weighted by aircraft quality); against strikers, transports and bombers the CAP does
  the killing, and survivors may jettison and abort the attack. Each air battle leaves a burst
  on the map for 20 campaign minutes (red when aircraft were lost); hover it for what happened.
- **Jump in now.** Pauses the war where it is. Pick a friendly flight whose aircraft you own:
  one in the air starts you in the air where the map shows it, with the rest of its route (if its
  target is already behind it, you fly it home); a planned one (**Fly this flight**) moves the clock
  to its takeoff and starts you at its base. The `.miz` is saved straight away and holds:
  - every other flight airborne at that moment, where the map shows it, and flights launching in
    the next 90 minutes appearing over their bases on time;
  - the campaign's ground forces around your route, both sides: every live position within about
    60 km of it, enemy SAMs (45 km) and early-warning radars (120 km) whose coverage reaches it, and
    the targets of the other flights in the mission, red strikes included (up to 90 positions);
  - your own target or CAP station, the airfield owners, and the time of day.
  **Briefing** opens it on the Tasking page. When the results come back (units destroyed on both
  sides and aircraft lost, from `dcs.log`), the war moves on by the time you spent in the mission
  and stays paused there, and the map shows a "Back from DCS" summary. **Give back to AI**
  hands the flight back unflown.
- **Frag a mission.** Add your own missions to the ATO (a "frag": the order that adds or
  changes a mission on an ATO). Pick any friendly squadron and any task it flies: Strike, SEAD or
  CAS against any enemy site, on the front line or deep behind it, or CAP over a station (over a
  friendly or enemy airfield, or a point you click on the map; it holds for an hour). Set the
  number of aircraft and either "as soon as possible" or a time on target / on station (e.g.
  Day 2 02:00 for a night strike: the route is timed back from it). Click an enemy site on the
  map to target it. **Loadout** picks one of the aircraft's stored loadouts (suited ones first) or
  leaves it automatic, and shows what it carries. **Scrub this flight** removes a friendly flight
  that hasn't taken off.
- **Ordnance.** Every flight on the ATO records its loadout when it's planned; click a flight to
  see what each aircraft carries. A mission built from the campaign loads the same loadout on
  every flight in it (yours included), and its briefing lists what each friendly flight carries
  and an assessed load for each enemy flight. The war's own strike results don't depend on
  ordnance: they come from type, flight size and the defences.
- **Airbase air defence.** Each friendly airfield has 3 SAM sites and an early-warning radar on top
  of its zone (Hawk batteries in DCS), so red's two strikes an hour meet real defences.
- **Bombers.** Blue also has an F-117A (8), B-1B (6) and B-52H (6) squadron at its rearmost
  airfield with a runway. They are AI-only in DCS, and they fly only missions you frag. The
  F-117 mostly slips past SAMs and fighters; B-1B and B-52 cells hit harder and can take out up
  to two more sites within 2-3 km of the aimpoint, but they are easier to shoot down. To fly
  with them, frag your own flight with a matching time on target (an F-15E two minutes
  behind a B-52 cell, say) and fly that: the bombers are in your mission as AI.
- **Air commander.** Give an order in your own words ("eliminate the SAM site near Kish",
  "capture Abu Musa Island", "hit their radar tonight without risking my jets"), optionally after
  clicking an enemy site on the map. The commander (an AI model, see below) reads the
  campaign: airfields and what each still needs to fall, enemy sites and the air defence covering
  them, your squadrons with their loadouts and range, enemy fighters up. It comes back with 3-5
  courses of action, each a set of frag orders: for example a stealth night strike, a SEAD-escorted
  package, a low-level attack, a stand-off missile strike, a special forces raid. Every frag is
  checked against the campaign (squadron, aircraft available, range, loadout, timing) and the odds
  shown (objective achieved, hit chance per flight, expected losses) come from the war's own model,
  not from the AI. **Execute** frags that option onto the ATO.
  - Profiles: *low level* flies under long-range SAM coverage but into the guns; *stand-off*
    releases from about 110 km and needs stand-off weapons (AGM-86, AGM-154, AGM-84, AGM-65...);
    *special forces* is a UH-60A squadron (AI only) that lands a team 4 km from the target, which
    works it for about 40 minutes (in DCS the team appears at the landing zone and moves in).
  - The AI is a setting in `config.json`: `"commander": {"provider": "openrouter", "model":
    "deepseek/deepseek-v4.1-flash"}` is the default (key in the `OPENROUTER_API_KEY` environment
    variable); `{"provider": "anthropic", "model": "claude-opus-5-5"}` uses Claude instead (key in
    `ANTHROPIC_API_KEY`). Restart Mission Builder after changing it. Each order is one API call; the
    situation sent is about 6,000 tokens on the Gulf, Nevada and Marianas and 12,000-14,000 on
    Caucasus and Syria; DeepSeek answers run up to about 19,000 tokens with its reasoning (about $0.002-0.013 an order in tests). The page shows the tokens, reasoning and OpenRouter cost after each answer.
- **Night.** A time on target between 21:00 and 05:00 counts as night: enemy fighters are less
  effective, and the mission you fly starts at that hour.

Left alone, the war moves slowly and not always your way; your sorties are what tip it.

**Mission ⇄ campaign handoff.** DCS is the truth for whatever was in the mission, for as long as it
ran. Besides kills, the mission reports every 5 minutes (and at the end) where each campaign flight
is, how many aircraft it has left, its fuel and whether it's airborne; landings (and where);
ejections; and how many units of each campaign site survived. When it comes back, flights that
were in it land where DCS landed them or carry on from where DCS left them (a CAP with time and
fuel left returns to its station; others head home), the engine doesn't roll dice for them inside
that window, and damaged sites keep a strength (counted at that strength in the odds and placed
with their surviving units in the next mission) until repaired. If DCS crashes before the end,
the last position report closes the window. See .

**Results from DCS.** Saved missions log each kill of a tracked unit to
`Saved Games\DCS\Logs\dcs.log` (a mission-start script using `env.info`, which works in a
stock DCS install; no MissionScripting.lua changes). The app reads the live log and DCS's
archived `dcs.log-*.zip` files. A flight is applied when it ends (the mission-end event, or the
next DCS start); **Apply now** / **Apply results** applies the flight in progress from what's
logged so far. Missions made on the Tasking page count too (ground kills only).

Campaign state is kept in `campaign/<map>.json` (not in git).

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
- `mission_builder/campaign.py`: per-map campaign state, the results script, reading results from dcs.log
- `mission_builder/war.py`: the air war: clock, squadrons, ATO planning, auto-resolution, plan odds
- `mission_builder/commander.py`: the air commander (Claude API): situation, courses of action, checks
- `mission_builder/generator.py`: builds the mission (no UI, can be scripted)
- `mission_builder/server.py`: local web server and JSON API (127.0.0.1 only)
- `mission_builder/web/`: the briefing page (`index.html`) and the campaign map (`war.html`)
- `mission_builder/app.py`: the old Tkinter window (superseded, still works)
- `config.json`: paths (including `dcs_logs`) and your last choices
- `campaign/`: each map's campaign state
- `.venv`: private Python with pydcs pinned to commit 03dd89f (the version Liberation 15.0.0 uses)
