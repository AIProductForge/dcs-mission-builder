# Mission ⇄ campaign handoff (built 2026-10-27; not yet confirmed in a DCS flight)

Principle: **DCS is the truth for whatever is in the mission, for as long as the mission ran.**
The campaign engine owns everything else: the parts of the war that weren't in the mission, and
all time outside it.

## 1. What the mission reports (new lines in dcs.log)

All via `env.info` (works in a stock DCS install, no file access). Same `MBRESULT|<sortie>|...`
prefix as now.

| Line | When | Content |
|---|---|---|
| `START\|n\|t` | mission start | number of tracked units, mission time |
| `DEAD\|unit` | a tracked unit dies (as now) | unit name |
| `LAND\|unit\|airbase\|t` | a tracked aircraft lands (`S_EVENT_LAND`) | where and when |
| `EJECT\|unit\|t\|x\|y` | a pilot ejects (`S_EVENT_EJECTION`) | for CSAR later |
| `POS\|group\|t\|x\|y\|alt\|alive\|fuel\|air` | every 5 min of mission time, and at END | one line per tracked group: position of its lead unit, units alive, lead's fuel 0-1, airborne or not |
| `HP\|site\|alive\|placed` | at END | each campaign site's surviving units out of those placed |
| `END\|t` | mission end (as now) | mission time |

Tracked = every group the campaign put in the mission: AI flights (`MBF:<flight>`), the player's
flight, and the ground units on campaign sites (`MB:<site>:...`). About 70 groups on a big map
→ roughly 70 lines per 5 minutes; an hour-long mission adds about 1,000 short lines to dcs.log.

DCS scripting calls used (to be verified against the DCS scripting docs while building):
`Group.getByName`, `Group:getUnits`, `Unit:getPoint`, `Unit:inAir`, `Unit:getFuel`,
`Unit:getLife`, `Unit:getLife0`, `world.addEventHandler` with `S_EVENT_LAND` and
`S_EVENT_EJECTION`, `timer.scheduleFunction`, `timer.getTime`.

## 2. What the campaign does with it (mission → campaign)

The mission covered a window: from its start (the moment you took or joined the flight) to
`END` (or the last `POS` snapshot if DCS crashed or the mission was quit without an END line).

1. **Clock**: moves to start + mission time (as now).
2. **Flights that were in the mission are DCS-owned for the window.** The engine doesn't roll
   dice for anything they did in it (their strikes, CAP fights, intercepts); DCS's results stand.
   - All units dead → **lost**.
   - `LAND` at a field → **landed there** (which may not be its home field; it rebases there if
     its side holds the field, otherwise it's lost).
   - Still airborne at the end → its route is **rebuilt from its last reported position**: to
     the next waypoint it hadn't reached (by name), or straight home if its target is behind it.
     Low fuel (< 25%) → straight home.
3. **Sites**: as now, a site is destroyed when half its placed units died; new: a site that
   survived damaged keeps a **strength** (alive/placed). Strength cuts its weight in the odds
   (a SAM site at half strength counts half) and the next mission places only the surviving
   units. Repairs restore strength over 12-30 hours like destroyed sites.
4. **Flights that weren't in the mission** run under the engine for the window as usual (the
   clock advance resolves them).
5. **Ejections**: logged with position (for a CSAR mission type later); no other effect yet.
6. **Summary**: the "Back from DCS" debrief adds landings away from home, flights repositioned,
   and sites damaged.

## 3. Campaign → mission (state into DCS)

Already there: flights placed where the map shows them with their remaining route, sites on
their positions, aircraft counts, loadouts. New:

- **Damaged sites** are placed with their surviving units only.
- **Fuel**: flights that were repositioned after a mission keep their reported fuel (as
  initial fuel of the group) so a fighter at 30% fuel doesn't come back full.

## 4. Edge cases

- **No END line** (crash, alt-F4): use the last `POS` snapshot and its time as the window end;
  units not reported dead are alive.
- **Mission quit early**: END comes early; the window is just shorter.
- **Saved mission never flown**: as now, it stays pending; flying it later applies it against
  the campaign as it is then (sites already dead stay dead; flights no longer on the ATO are
  ignored).
- **Same mission flown twice**: the second flight's lines carry the same sortie id; only the
  first is applied (as now).

## 5. How it gets tested

- The Lua script runs in LuaJIT against a stand-in DCS API (as for the current results script),
  including the trigger path DCS uses.
- Replays of synthetic logs: a flight landing elsewhere, a flight still airborne, a damaged
  SAM, a crash without END.
- Then one real flight in DCS to confirm the events and calls behave as expected.

## Not in this step

DCS-based ranges per system (next step), AWACS (after that), integrated air defence scripts,
calibrating the engine against DCS outcomes.
