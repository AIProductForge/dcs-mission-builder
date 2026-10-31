"""Build DCS test missions for what only DCS can answer. python tools/test_missions.py
Saved in the Generated folder; every shot/hit/kill also goes to dcs.log as MBTEST lines.
  F  SA-10 question: F1 SA-10 + SA-11 side by side, F-16 with HARMs attacks as the campaign does.
     F2 the same site, the F-16 holds fire. Does a HARM launch keep the SA-10 quiet?
  G  Weapons with Bombing orders: G1 Hornet AGM-154A on a T-72 platoon, G2 F-16 AGM-88 (ARM category)
     on an SA-6 radar, G3 Su-24M Kh-31P (ARM) on a Hawk radar, G4 Tu-95MS Kh-555 at 507 km."""
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dcs  # noqa: E402
from dcs import task, vehicles  # noqa: E402
from dcs.mapping import Point  # noqa: E402
from dcs.terrain import PersianGulf  # noqa: E402
from dcs.triggers import TriggerStart  # noqa: E402

from mission_builder import server  # noqa: E402
from mission_builder.inventory import configure_payloads  # noqa: E402
_cfg = server.load_config()
configure_payloads(Path(_cfg["dcs_dir"]), Path(_cfg["liberation_payloads"]))
from mission_builder import generator as G, systems  # noqa: E402

LUA = """
local function n(o) if o and o.getName then local ok, r = pcall(o.getName, o); if ok and r then return tostring(r) end end return "-" end
local function ty(o) if o and o.getTypeName then local ok, r = pcall(o.getTypeName, o); if ok and r then return tostring(r) end end return "-" end
local names = {[world.event.S_EVENT_SHOT]="SHOT", [world.event.S_EVENT_HIT]="HIT", [world.event.S_EVENT_KILL]="KILL", [world.event.S_EVENT_DEAD]="DEAD"}
local h = {}
function h:onEvent(e)
  local k = names[e.id]
  if k then env.info(string.format("MBTEST|%s|%.0f|%s|%s|%s|%s|%s", k, timer.getTime(), n(e.initiator), ty(e.initiator), ty(e.weapon), n(e.target), ty(e.target))) end
end
world.addEventHandler(h)
env.info("MBTEST|START")
"""
rng = random.Random(1)
terrain = PersianGulf()
OUT = Path(_cfg["output_dir"])


def new():
    m = dcs.Mission(terrain)
    m.start_time = dt.datetime(2010, 6, 2, 18, 30)
    return m, m.country("Russia"), m.country("USA")


def site(m, country, name, types, x, y):
    pos = Point(x, y, terrain)
    g = m.vehicle_group_platoon(country, name, types, pos, heading=0)
    for i, u in enumerate(g.units):
        u.position = pos.point_from_heading(rng.uniform(0, 360), 120 * (i > 0) * (1 + i * 0.5))
        u.skill = dcs.unit.Skill.Good
    return g


def flight(m, country, name, type_id, mission, start, alt, kmh, loadout=""):
    ut = G.unit_type(type_id)
    mt = G._main_task(ut, mission)
    g = m.flight_group_inflight(country, name, ut, Point(start[0], start[1], terrain), alt, kmh, maintask=mt, group_size=2)
    print(name, G._arm(g, ut, mission, mt, loadout))
    return g


def finish(m, title, path):
    t = TriggerStart(comment="MB test event log")
    t.add_action(G.InlineDoScript(LUA))
    m.triggerrules.triggers.append(t)
    m.set_sortie_text(title)
    m.set_description_text(title + ". Let it run as long as the notes say, then quit.")
    m.save(str(OUT / path))
    print("saved", OUT / path)


X = 300_000
m, red, blue = new()
for n, y, harm in ((1, -100_000, True), (2, 60_000, False)):
    site(m, red, f"F{n} SA-10", systems.SYSTEMS["SA-10"], X, y)
    site(m, red, f"F{n} SA-11", systems.SYSTEMS["SA-11"], X - 1500, y + 1500)
    f = flight(m, blue, f"F{n} F-16", "F-16C_50", "SEAD", (X - 100_000, y), 6500, 830)
    if harm:
        f.points[0].tasks.append(task.EngageTargets(max_distance=40000, targets=[task.Targets.All.GroundUnits.AirDefence]))
    else:
        f.points[0].tasks.append(task.OptROE(task.OptROE.Values.WeaponHold))
    f.add_waypoint(Point(X, y, terrain), 6500, 830, "OVER")
    f.add_waypoint(Point(X - 100_000, y + 20_000, terrain), 6500, 830, "HOME")
finish(m, "MB TEST F: silent SA-10", "MB_Test_F_SA10.miz")

m, red, blue = new()
AP = terrain.airports
t1 = site(m, red, "G1 T-72s", [vehicles.Armor.T_72B] * 4, X, -100_000)
f = flight(m, blue, "G1 Hornet JSOW", "FA-18C_hornet", "Strike", (X - 150_000, -100_000), 8000, 830, "Liberation DEAD")
f.points[0].tasks.append(task.Bombing(Point(X, -100_000, terrain), weapon_type=task.WeaponType.Guided, expend=task.Expend.One, group_attack=True))
f.add_waypoint(Point(X - 150_000, -80_000, terrain), 8000, 830, "HOME")
t2 = site(m, red, "G2 SA-6", systems.SYSTEMS["SA-6"], X, 0)
f = flight(m, blue, "G2 F-16 HARM", "F-16C_50", "SEAD", (X - 120_000, 0), 8000, 830)
f.points[0].tasks.append(task.Bombing(Point(X, 0, terrain), weapon_type=task.WeaponType.ARM, expend=task.Expend.One, group_attack=True))
f.add_waypoint(Point(X - 120_000, 20_000, terrain), 8000, 830, "HOME")
t3 = site(m, blue, "G3 Hawk", systems.SYSTEMS["Hawk"], X, 100_000)
f = flight(m, red, "G3 Su-24 Kh-31P", "Su-24M", "SEAD", (X + 120_000, 100_000), 8000, 830, "ANTISHIP")
f.points[0].tasks.append(task.Bombing(Point(X, 100_000, terrain), weapon_type=task.WeaponType.ARM, expend=task.Expend.One, group_attack=True))
f.add_waypoint(Point(X + 120_000, 120_000, terrain), 8000, 830, "HOME")
kish = AP["Kish Intl"].position
site(m, blue, "G4 Abrams", [vehicles.Armor.M_1_Abrams] * 3, kish.x + 1500, kish.y + 1500)
ker = AP["Kerman"].position
f = flight(m, red, "G4 Tu-95MS", "Tu-95MS", "Strike", (ker.x, ker.y), 9000, 700, "STRIKE")
f.points[0].tasks.append(task.Bombing(Point(kish.x + 1500, kish.y + 1500, terrain), expend=task.Expend.Two, group_attack=True))
f.add_waypoint(Point(ker.x + 60_000, ker.y, terrain), 9000, 700, "HOME")
finish(m, "MB TEST G: weapons with Bombing orders", "MB_Test_G_Weapons.miz")
