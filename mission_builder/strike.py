"""Strike generator: a mission built from a story rather than a mission type.

A story places a target, the other flights in the package and the enemy's reaction, and
links them with flags, so what the player does changes what happens next: kill the radar
and the scramble never comes, lose the SEAD flight and the follow-on strike stays home.
Complications are added on top by complexity. Some branches roll in DCS when the mission
starts (condition.Random), so the same .miz can play differently twice.

Every beat writes "MBSTORY|<beat>" to dcs.log, so a flown mission can be read back.
None of the trigger chains here has been flown in DCS yet; see tests/test_strike.py for
what is checked without it.
"""
from __future__ import annotations

from dcs import action, condition, planes, task, vehicles
from dcs.action import Coalition
from dcs.mapping import Point
from dcs.point import PointAction
from dcs.triggers import TriggerOnce, TriggerStart

from . import campaign
from . import generator as G

AD = vehicles.AirDefence
SUCCESS, FAILED = 1, 2
GROUND_KMH = 45
FIRST_FLAG = 100
# Seconds from mission start until the player can be moving, by start type.
START_DELAY = {"Runway (engines running)": 60, "Ramp, hot": 300, "Ramp, cold": 600, "In the air": 0}
TARGET_SAM = {1: "SA-8", 2: "SA-6", 3: "SA-6", 4: "SA-11", 5: "SA-11"}
QRA_TYPE = {1: planes.MiG_21Bis, 2: planes.MiG_23MLD, 3: planes.MiG_29S, 4: planes.MiG_29S, 5: planes.Su_27}


class Story:
    """The mission's flags and triggers, plus the package and enemy pieces stories share."""

    def __init__(self, B, player, heading_out: float, distance: float):
        self.B, self.m, self.b, self.rng = B, B.m, B.b, B.rng
        self.player, self.heading_out, self.distance = player, heading_out, distance
        self.flags: dict[str, int] = {}
        self.eta = START_DELAY.get(B.spec.start, 60) + distance / (55 if B.heli else 222)
        self.enemy_fields = B.enemy_airfields()

    def flag(self, name: str) -> int:
        return self.flags.setdefault(name, FIRST_FLAG + len(self.flags))

    def beat(self, name: str, when, then=(), say: str | None = None, seconds: int = 20) -> None:
        """One trigger: when every condition holds, do `then`, say `say` and log the beat."""
        t = TriggerOnce(comment=name)
        for c in when:
            t.add_condition(c)
        for a in then:
            t.add_action(a)
        if say:
            if not self.B.spec.awacs:
                say = say.replace("OVERLORD", "COMMAND")
            t.add_action(action.MessageToCoalition(Coalition.Blue, self.m.string(say), seconds))
        t.add_action(G.InlineDoScript(f'env.info("MBSTORY|{self.b.story_id}|{name}")'))
        self.m.triggerrules.triggers.append(t)

    def result(self, outcome: str):
        return G.InlineDoScript(f'env.info("MBSTORY|{self.b.story_id}|RESULT|{outcome}")')

    def is_open(self):
        """Conditions true until the mission has been won or lost."""
        return [condition.FlagIsFalse(SUCCESS), condition.FlagIsFalse(FAILED)]

    def succeed(self, when, say: str) -> None:
        self.beat("Mission success", when + self.is_open(), [action.SetFlag(SUCCESS), self.result("success")], say, 30)

    def fail(self, name: str, when, say: str) -> None:
        self.beat(name, when + self.is_open(), [action.SetFlag(FAILED), self.result("failed")], say, 30)

    def zone(self, pos: Point, radius_m: float, name: str):
        return self.m.triggers.add_triggerzone(pos, radius=radius_m, hidden=True, name=self.B.name(name))

    def player_near(self, pos: Point, nm: float, name: str):
        return condition.PartOfGroupInZone(self.player.id, self.zone(pos, nm * 1852, name).id)

    def site(self, roles, far_km=None) -> Point:
        return pick_objective(self.B, roles, far_km)

    def roll(self, name: str, percent: int) -> int:
        """A flag set or not once, at mission start. A once-trigger would keep rolling until it hit."""
        f = self.flag(name)
        t = TriggerStart(comment=name)
        t.add_condition(condition.Random(percent))
        t.add_action(action.SetFlag(f))
        t.add_action(G.InlineDoScript(f'env.info("MBSTORY|{self.b.story_id}|roll {name}")'))
        self.m.triggerrules.triggers.append(t)
        return f

    def nearest_enemy_field(self, pos: Point):
        return min(self.enemy_fields, key=lambda a: a.position.distance_to_point(pos))

    def target(self, name: str, pos: Point, units) -> None:
        self.b.targets.append({"name": name, **G._ll(pos), "units": units})

    def structures(self, count: int, pos: Point, label="Target", site=None) -> list:
        out = []
        for _ in range(count):
            at = pos.point_from_heading(self.rng.uniform(0, 360), self.rng.uniform(80, 400))
            g = self.m.static_group(self.B.red, self.B.name(label), self.rng.choice(G.STRUCTURES), at,
                                    heading=self.rng.uniform(0, 360))
            self.B.track(g.units[0], site)
            out.append(g)
        return out

    def defend(self, pos: Point) -> None:
        for kind in G.POINT_DEFENCE[min(self.B.level, 3 if self.B.heli else 5)]:
            self.B.sam(kind, pos.point_from_heading(self.rng.uniform(0, 360), self.rng.uniform(400, 1500)))

    def qra(self, pos: Point, label="QRA", count=2):
        field_ = self.nearest_enemy_field(pos)
        ptype = QRA_TYPE[self.B.level]
        g = self.B.bandits(label, ptype, count, field_.position, pos, late=True)
        self.b.threats.append({"name": f"{count}x {ptype.id} (alert)", "kind": "AIR", **G._ll(field_.position),
                               "range_nm": None, "note": f"Assessed on alert at {field_.name}."})
        return g, field_

    def wing(self, label: str, ptype, mission: str, maintask, count=2, late=False, offset_km=15.0, alt=7000):
        """A friendly AI flight, in the air near home so it needs no parking."""
        B = self.B
        start = B.home.position.point_from_heading(self.heading_out + self.rng.uniform(-40, 40), offset_km * 1000)
        g = self.m.flight_group_inflight(B.blue, B.name(label), ptype, start, alt, 850, maintask=maintask, group_size=count)
        _, stores = G._arm(g, ptype, mission, maintask)
        g.late_activation = late
        self.b.friendly_air.append({"name": f"{count}x {ptype.id} ({label})", "route": [], "stores": stores})
        return g

    def sead_wing(self, pos: Point, label="Viper"):
        g = self.wing(label, planes.F_16C_50, "SEAD", task.SEAD)
        w = g.add_waypoint(pos, 7000, 850, "SEAD")
        w.tasks.append(task.EngageTargets(max_distance=60000, targets=[task.Targets.All.GroundUnits.AirDefence]))
        g.land_at(self.B.home)
        self._route_note(g)
        return g

    def strike_wing(self, pos: Point, label="Hammer", late=True):
        g = self.wing(label, planes.F_16C_50, "Strike", task.GroundAttack, late=late)
        # On the waypoint before the target: see add_ambient for why.
        g.points[-1].tasks.append(task.Bombing(Point(pos.x, pos.y, self.B.terrain), group_attack=True))
        g.add_waypoint(pos, 7000, 850, "TARGET")
        g.land_at(self.B.home)
        self._route_note(g)
        return g

    def cap_wing(self, pos: Point, label="Eagle"):
        g = self.m.patrol_flight(self.B.blue, self.B.name(label), planes.F_15C, None,
                                 self.B.home.position.point_from_heading(self.heading_out, self.distance * 0.3),
                                 self.B.home.position.point_from_heading(self.heading_out, self.distance * 0.75),
                                 speed=850, altitude=8000, group_size=2)
        G._arm(g, planes.F_15C, "CAP", task.CAP)
        self.b.friendly_air.append({"name": f"2x F-15C ({label})", "route": [G._ll(p.position) for p in g.points]})
        return g

    def _route_note(self, g) -> None:
        for row in self.b.friendly_air:
            if row["name"].endswith(f"({g.name.rsplit(' ', 1)[0]})"):
                row["route"] = [G._ll(p.position) for p in g.points]


# --- stories ---------------------------------------------------------------------------------
# Each takes the objective position, fills in the target, flights and chains, and returns the
# (mission statement, situation, execution) text. The text gives intelligence, not the chain.

def corridor(s: Story, obj: Point, flight: str) -> tuple:
    """SEAD opens the door, then a second strike can go. Lose the SEAD and the door is yours."""
    B = s.B
    n = 2 + B.level // 2
    site = B.site_id(obj)
    targets = s.structures(n, obj, site=site)
    s.target(f"Depot ({n} structures)", obj, sorted({g.units[0].type for g in targets}))
    sam_at = obj.point_from_heading(obj.heading_between_point(B.home.position), 12000 if not B.heli else 5000)
    kind = TARGET_SAM[B.level]
    sam = B.sam(kind, sam_at, threat_note="Guards the approach. Ring is approximate.")
    viper = s.sead_wing(sam_at)
    aimpoint = obj.point_from_heading(s.rng.uniform(0, 360), 1500)
    s.structures(2, aimpoint, "Second aimpoint")
    hammer = s.strike_wing(aimpoint)
    down, clear = s.flag("sead down"), s.flag("sam dead")
    s.beat("SAM dead", [condition.GroupDead(sam.id)], [action.SetFlag(clear)], f"VIPER: {kind} is down. The door is open.")
    s.beat("Follow-on strike launches", [condition.FlagIsTrue(clear)], [action.ActivateGroup(hammer.id)],
           "HAMMER: pushing now for the second aimpoint.")
    s.beat("SEAD lost", [condition.GroupDead(viper.id), condition.FlagIsFalse(clear)], [action.SetFlag(down)],
           f"VIPER is off the net and the {kind} is still up. HAMMER is holding. Kill it or go around it.", 30)
    s.succeed([B.dead_condition(g) for g in targets], "Depot destroyed. RTB.")
    s.defend(obj)
    s.cap_wing(obj)
    return (f"{flight} destroys the depot behind the {kind} belt, following VIPER through the corridor.",
            f"A {G.THREAT_NAMES[kind]} covers the only direct approach to a depot feeding the front.",
            "VIPER (2x F-16C) goes first against the SAM. HAMMER (2x F-16C) follows only once it is confirmed dead. "
            "EAGLE (2x F-15C) holds CAP on the ingress.")


def scud_hunt(s: Story, obj: Point, flight: str) -> tuple:
    """A launcher fires at the deadline. Killing it reveals its partner."""
    B = s.B
    launcher = vehicles.MissilesSS.CHAP_9K720_HE
    aim = B.home.position
    mk = lambda pos: B.m.vehicle_group(B.red, B.name("Launcher"), launcher, pos,
                                       heading=pos.heading_between_point(aim), group_size=1)
    first = mk(obj)
    first.add_trigger_action(task.FireAtPoint(Point(aim.x, aim.y, B.terrain), rounds=1, radius=100))
    other_at = s.site(("missile", "armor", "structure", "shorad"))
    second = mk(other_at)
    second.late_activation = True
    second.add_trigger_action(task.FireAtPoint(Point(aim.x, aim.y, B.terrain), rounds=1, radius=100))
    deadline = round(s.eta * 1.6 + 300)
    fired, found = s.flag("launch"), s.flag("second found")
    s.target("Missile launcher", obj, [launcher.id])
    brg = round(obj.heading_between_point(other_at)) % 360
    nm = round(obj.distance_to_point(other_at) * G.M_TO_NM)
    s.beat("Launch warning", [condition.TimeAfter(max(1, deadline - 300)), condition.GroupAlive(first.id)],
           say="SIGINT: launch crew has been ordered to prepare. Five minutes.")
    s.beat("First launcher dead", [condition.GroupDead(first.id)], [action.SetFlag(found), action.ActivateGroup(second.id)],
           f"Launcher destroyed. Its radio traffic points to a second one, {nm} nm on bearing {brg:03d} from the target.", 40)
    s.fail("Launch", [condition.TimeAfter(deadline), condition.GroupAlive(first.id)], "Launch detected. Missile inbound to base.")
    t_push = TriggerOnce(comment="Fire order")
    t_push.add_condition(condition.TimeAfter(max(1, deadline - G.ISKANDER_SETUP)))
    t_push.add_condition(condition.GroupAlive(first.id))
    t_push.add_action(action.AITaskPush(first.id, 1))
    B.m.triggerrules.triggers.append(t_push)
    s.beat("Second launcher firing", [condition.FlagIsTrue(found), condition.TimeSinceFlag(found, deadline // 2)],
           [action.AITaskPush(second.id, 1), action.SetFlag(fired)], "Second launcher is setting up to fire.")
    s.succeed([condition.FlagIsTrue(found), condition.GroupDead(second.id)], "Both launchers destroyed. RTB.")
    s.defend(obj)
    s.cap_wing(obj)
    return (f"{flight} finds and destroys the missile launcher before it fires on {B.home.name}.",
            f"A launcher is set up within reach of {B.home.name}. SIGINT expects a launch in about {round(deadline / 60)} minutes. "
            "It rarely operates alone.",
            "EAGLE (2x F-15C) holds CAP on the ingress. You are the only strike asset in time.")


def convoy(s: Story, obj: Point, flight: str) -> tuple:
    """A convoy is moving to the front. Stop it before it arrives; hitting it wakes the enemy."""
    B = s.B
    field_ = s.nearest_enemy_field(obj)
    start = field_.position.point_from_heading(field_.position.heading_between_point(obj), 2500)
    air = [vehicles.Unarmed.Ural_375] * (3 + B.level) + ([AD.Strela_1_9P31] if B.level >= 2 else []) + \
          ([AD.ZSU_23_4_Shilka] if B.level >= 3 else [])
    g = B.ground("Convoy", air, start, spread=25)
    g.add_waypoint(obj, PointAction.OnRoad, GROUND_KMH)
    travel = start.distance_to_point(obj) / (GROUND_KMH / 3.6)
    deadline = max(s.eta * 1.5 + 300, travel)
    s.target(f"Supply convoy ({len(air)} vehicles)", start, [u.type for u in g.units])
    held = TriggerStart(comment="Convoy waits")
    held.add_action(action.GroupStop(g.id))
    B.m.triggerrules.triggers.append(held)
    s.beat("Convoy rolls", [condition.TimeAfter(max(2, deadline - travel))], [action.GroupResume(g.id)],
           "ISR: the convoy is on the move.")
    arrive = s.zone(obj, 1500, "Convoy destination")
    qra, from_field = s.qra(start, "Convoy cover")
    s.beat("Convoy under attack", [condition.GroupLifeLess(g.id, 90)], [action.ActivateGroup(qra.id)],
           f"OVERLORD: fighters scrambling from {from_field.name}. They know you are on the convoy.")
    s.fail("Convoy arrives", [condition.PartOfGroupInZone(g.id, arrive.id)], "The convoy has reached the front. Mission failed.")
    s.succeed([condition.GroupLifeLess(g.id, 25)], "Convoy stopped. Whatever is left is not going anywhere. RTB.")
    s.cap_wing(obj)
    return (f"{flight} stops the supply convoy from {field_.name} before it reaches the front.",
            f"Armour at the front is short of fuel. A convoy is forming near {field_.name}.",
            "The convoy route runs by road toward the objective. EAGLE (2x F-15C) holds CAP on the ingress.")


def hvt(s: Story, obj: Point, flight: str) -> tuple:
    """Senior officers at a forward post. An early-warning radar decides whether they run."""
    B = s.B
    guard = [vehicles.Armor.BTR_80, vehicles.Unarmed.Ural_375, vehicles.Armor.BTR_80]
    g = B.ground("HVT party", guard, obj, spread=25, site=B.site_id(obj))
    s.structures(2, obj, "Forward post")
    field_ = s.nearest_enemy_field(obj)
    g.add_waypoint(field_.position, PointAction.OnRoad, 60)
    hold = TriggerStart(comment="HVT stays put")
    hold.add_action(action.GroupStop(g.id))
    B.m.triggerrules.triggers.append(hold)
    ewr_at = s.site(("ewr", "sam", "shorad"), far_km=(120 if B.heli else 300))
    ewr = B.ground("EWR", [AD.X_1L13_EWR], ewr_at, site=B.site_id(ewr_at))
    B.b.threats.append({"name": "EWR (1L13)", "kind": "EWR", **G._ll(ewr_at), "range_nm": None,
                        "note": "If this sees you, the target knows."})
    s.target("HVT party (BTR-80s, truck)", obj, [u.type for u in g.units])
    s.target("Early-warning radar (optional)", ewr_at, ["1L13 EWR"])
    alert = s.flag("alerted")
    qra, from_field = s.qra(obj, "Alert")
    s.beat("Radar killed", [condition.GroupDead(ewr.id), condition.FlagIsFalse(alert)],
           say="Radar is down. The net has gone quiet: they haven't seen you.")
    s.beat("Enemy alerted", [condition.GroupAlive(ewr.id), s.player_near(ewr_at, 60, "EWR coverage")], [action.SetFlag(alert)])
    s.beat("Scramble", [condition.FlagIsTrue(alert)], [action.ActivateGroup(qra.id)],
           f"OVERLORD: you've been seen. Fighters scrambling from {from_field.name}.")
    s.beat("HVT runs", [condition.FlagIsTrue(alert), condition.TimeSinceFlag(alert, 120)], [action.GroupResume(g.id)],
           f"ISR: the party is leaving the post, heading for {field_.name}.")
    s.fail("HVT escapes", [condition.PartOfGroupInZone(g.id, s.zone(field_.position, 1500, "HVT escape").id)],
           f"The party has reached {field_.name} and is airborne. Mission failed.")
    s.succeed([condition.GroupDead(g.id)], "Party destroyed. RTB.")
    s.defend(obj)
    return (f"{flight} destroys the command party at the forward post.",
            f"Senior officers are visiting a post near {field_.name}, for now. An early-warning radar covers the approach.",
            "The radar is not a required target. It is your choice whether to take it first.")


def rescue(s: Story, obj: Point, flight: str) -> tuple:
    """Keep a search party off a downed pilot until the helicopter can come in."""
    B = s.B
    inf = vehicles.Infantry
    pilot = B.m.vehicle_group(B.blue, B.name("Downed pilot"), inf.Soldier_M4, obj)
    away = obj.point_from_heading(obj.heading_between_point(s.nearest_enemy_field(obj).position), 6000)
    team = B.ground("Search party", [vehicles.Armor.BTR_80, inf.Infantry_AK, inf.Infantry_AK, vehicles.Armor.BTR_80],
                    away, spread=30)
    # Slow enough that the player can get there first from any start; ordered not to shoot the pilot.
    team.add_waypoint(obj, PointAction.OffRoad, max(4, 6000 / (s.eta * 1.5 + 300) * 3.6))
    team.points[0].tasks.append(task.OptROE(task.OptROE.Values.WeaponHold))
    aaa = B.sam("ZSU-23-4", obj.point_from_heading(s.rng.uniform(0, 360), 3000))
    s.target("Search party (BTR-80s, infantry)", away, [u.type for u in team.units])
    s.target("Downed pilot: friendly, do not engage", obj, [u.type for u in pilot.units])
    helo = B.m.flight_group_inflight(B.blue, B.name("Pedro"), planes.plane_map.get("UH-1H") or G.dcs.helicopters.UH_1H,
                                     B.home.position.point_from_heading(s.heading_out, 3000), 150, 180, group_size=1)
    helo.late_activation = True
    helo.add_waypoint(obj, 100, 180, "PILOT")
    G._follow_terrain(helo)
    pick_up = s.zone(obj, 400, "Pickup")
    near = s.zone(obj, 300, "Party at pilot")
    clear, pedro = s.flag("area clear"), s.flag("pedro launched")
    s.beat("Area clear", [condition.GroupDead(team.id), condition.GroupDead(aaa.id)], [action.SetFlag(clear)],
           "Area looks clear. PEDRO is launching.")
    s.beat("Pedro launches", [condition.FlagIsTrue(clear)], [action.ActivateGroup(helo.id), action.SetFlag(pedro)])
    s.fail("Pilot captured", [condition.PartOfGroupInZone(team.id, near.id)], "They've got the pilot. Mission failed.")
    s.fail("Pedro lost", [condition.FlagIsTrue(pedro), condition.GroupDead(helo.id)], "PEDRO is down. Mission failed.")
    s.succeed([condition.PartOfGroupInZone(helo.id, pick_up.id)], "PEDRO has the pilot. Good work, RTB.")
    if not B.heli:
        s.cap_wing(obj)
    return (f"{flight} clears the search party and the gun near the pilot, so PEDRO can make the pickup.",
            f"A pilot is down {round(s.distance * G.M_TO_NM)} nm from {B.home.name}. A search party is closing on foot and by vehicle.",
            "PEDRO (UH-1H) launches only when the party and the ZSU are confirmed dead. "
            + ("" if B.heli else "EAGLE (2x F-15C) holds CAP on the ingress."))


def airfield(s: Story, obj: Point, flight: str) -> tuple:
    """Raid an enemy field's operations. Until the ops centre dies, it keeps launching."""
    B = s.B
    field_ = s.nearest_enemy_field(obj)
    pos = field_.position.point_from_heading(s.rng.uniform(0, 360), 1800)
    from dcs.statics import Fortification
    ops = B.m.static_group(B.red, B.name("Ops centre"), Fortification._Command_Center, pos, heading=s.rng.uniform(0, 360))
    fuel = [B.m.static_group(B.red, B.name("Fuel"), Fortification.Fuel_tank,
                             pos.point_from_heading(s.rng.uniform(0, 360), 250 + i * 60)) for i in range(2 + B.level // 2)]
    s.target(f"{field_.name} operations centre", pos, [ops.units[0].type])
    s.target(f"{field_.name} fuel ({len(fuel)} tanks)", pos, [g.units[0].type for g in fuel])
    wave1, _ = s.qra(pos, "First wave")
    wave2, _ = s.qra(pos, "Second wave")
    near = s.player_near(pos, 50, "Field approach")
    launched, ops_dead = s.flag("first wave up"), s.flag("ops dead")
    s.beat("First wave", [near], [action.ActivateGroup(wave1.id), action.SetFlag(launched)], f"OVERLORD: scramble at {field_.name}.")
    s.beat("Ops centre dead", [B.dead_condition(ops)], [action.SetFlag(ops_dead)],
           f"Ops centre destroyed. {field_.name} should be slower now.")
    s.beat("Second wave", [condition.FlagIsTrue(launched), condition.FlagIsFalse(ops_dead), condition.GroupDead(wave1.id)],
           [action.ActivateGroup(wave2.id)], f"OVERLORD: second group launching from {field_.name}. The ops centre is still working.")
    s.succeed([condition.FlagIsTrue(ops_dead)] + [B.dead_condition(g) for g in fuel], f"{field_.name} is out of the fight. RTB.")
    s.defend(pos)
    s.sead_wing(pos)
    s.cap_wing(pos)
    return (f"{flight} destroys the operations centre and fuel at {field_.name}.",
            f"{field_.name} is generating the sorties that are hurting the front.",
            "VIPER (2x F-16C) suppresses the field's defences. EAGLE (2x F-15C) holds CAP on the ingress. "
            "The ops centre directs the field's launches.")


STORIES = {"corridor": (corridor, ("structure", "armor", "missile")),
           "scud_hunt": (scud_hunt, ("missile", "armor", "structure")),
           "convoy": (convoy, ("armor", "shorad", "structure")),
           "hvt": (hvt, ("structure", "armor", "shorad")),
           "rescue": (rescue, ("armor", "shorad", "structure")),
           "airfield": (airfield, ("structure", "armor", "shorad"))}
HELICOPTER_STORIES = ["convoy", "rescue", "hvt"]


# --- complications ---------------------------------------------------------------------------

def ambush(s: Story, obj: Point) -> None:
    """Half the time, fighters try to catch the package on the way out."""
    g, field_ = s.qra(obj, "Egress ambush")
    roll = s.roll("ambush roll", 50)
    s.beat("Egress ambush", [condition.FlagIsTrue(SUCCESS), condition.FlagIsTrue(roll), condition.TimeSinceFlag(SUCCESS, 60)],
           [action.ActivateGroup(g.id)], f"OVERLORD: fighters off {field_.name}, cutting across your egress.")


def raid_on_home(s: Story, obj: Point) -> None:
    """A raid on home goes for the field while the package is out; EAGLE or the player must choose."""
    B = s.B
    far = s.enemy_fields[0].position
    ptype = planes.Su_24M
    g = s.m.flight_group_inflight(B.red, B.name("Raid"), ptype, far, 5000, 800, maintask=task.GroundAttack, group_size=2)
    g.points[-1].tasks.append(task.Bombing(Point(B.home.position.x, B.home.position.y, B.terrain), group_attack=True))
    g.add_waypoint(B.home.position, 5000, 800, "TARGET")
    G._arm(g, ptype, "Strike", task.GroundAttack)
    for u in g.units:
        u.skill = B.skill
    g.late_activation = True
    when = round(s.eta * s.rng.uniform(0.8, 1.3))
    s.beat("Raid on home", [condition.TimeAfter(when)], [action.ActivateGroup(g.id)],
           f"OVERLORD: raid heading for {B.home.name}, two contacts, low. Anyone with missiles left, your call.", 30)


def popup(s: Story, obj: Point) -> None:
    types = [AD.Strela_10M3, AD.ZSU_23_4_Shilka] if s.B.level < 5 else [AD.Tor_9A331, AD.ZSU_23_4_Shilka]
    g = s.B.ground("Pop-up air defence", types, obj.point_from_heading(s.rng.uniform(0, 360), 1200), late=True, spread=50)
    s.beat("Pop-up SAM", [s.player_near(obj, 12 if s.B.heli else 20, "Pop-up")], [action.ActivateGroup(g.id)])
    s.b.notes.append("Intelligence assesses mobile short-range air defence may move into the target area.")


def follow_on(s: Story, obj: Point) -> None:
    """Sometimes the target's capture turns up a second one, if the flight has anything left."""
    at = s.site(("armor", "structure", "shorad"), far_km=300)
    g = s.B.ground("Follow-on target", [vehicles.Armor.BMP_2, vehicles.Unarmed.Ural_375, vehicles.Armor.BTR_80], at, late=True,
                   spread=40, site=s.B.site_id(at))
    roll = s.roll("follow-on roll", 60)
    brg, nm = round(obj.heading_between_point(at)) % 360, round(obj.distance_to_point(at) * G.M_TO_NM)
    s.beat("Follow-on target", [condition.FlagIsTrue(SUCCESS), condition.FlagIsTrue(roll)], [action.ActivateGroup(g.id)],
           f"Fresh tasking if you have stores: vehicles moving {nm} nm on bearing {brg:03d} from the target. Your call.", 40)


COMPLICATIONS = {"ambush": ambush, "raid": raid_on_home, "popup": popup, "follow_on": follow_on}
BY_LEVEL = {1: 0, 2: 1, 3: 2, 4: 2, 5: 3}


# --- build -------------------------------------------------------------------------------------

def pick_objective(B, roles, far_km=None) -> Point:
    far_km = far_km or (90 if B.heli else 260)
    p = B.pick_site(roles, 8 if B.heli else 40, far_km, front=True)
    if p is None:
        field_ = B.rng.choice(B.enemy_airfields()[:3])
        p = B.fallback_point(field_).point_from_heading(B.rng.uniform(0, 360), B.rng.uniform(1500, 6000))
    return p


def build(spec: G.MissionSpec) -> tuple:
    """A pydcs Mission and Briefing for a strike story (`spec.story`, or rolled from the seed)."""
    B = G._Builder(spec)
    m, rng, home = B.m, B.rng, B.home
    if not B.enemy_airfields():
        raise RuntimeError("This scenario has no enemy airfields.")
    rolled = rng.choice(HELICOPTER_STORIES if B.heli else list(STORIES))  # always rolled, so the seed gives the same layout
    story = spec.story if spec.story in STORIES else rolled
    spec.story = story
    make, roles = STORIES[story]
    ptype = G.unit_type(spec.aircraft)
    maintask = G._main_task(ptype, "Strike")

    objective = pick_objective(B, roles)
    heading_out, distance = home.position.heading_between_point(objective), home.position.distance_to_point(objective)
    alt, speed = (150, 200) if B.heli else (6000, 800)
    start = G.START_TYPES[spec.start]
    if start is None:
        player = m.flight_group_inflight(B.blue, "Player Flight", ptype, home.position.point_from_heading(heading_out, 10000),
                                         alt, speed, maintask=maintask, group_size=spec.flight_size)
        player.points[0].name = "Spawn"
    else:
        player = m.flight_group_from_airport(B.blue, "Player Flight", ptype, home, maintask=maintask, start_type=start,
                                             group_size=spec.flight_size)
        player.points[0].name = home.name
    player.units[0].set_player()
    for wingman in player.units[1:]:
        wingman.skill = G.Skill.High
    loadout, stores = G._arm(player, ptype, "Strike", maintask, spec.loadout)
    raw = player.units[0].callsign_dict.get("name", "") if hasattr(player.units[0], "callsign_dict") else ""
    callsign = f"{raw[:-2].upper()} {raw[-2]}" if raw[-2:].isdigit() else (raw or "PLAYER")

    brg, nm = round(heading_out) % 360, round(distance * G.M_TO_NM)
    B.b = b = G.Briefing(
        codename=f"{rng.choice(G.CODENAME_A)} {rng.choice(G.CODENAME_B)}",
        dtg=G._dtg(m.start_time, getattr(B.terrain, "utc_offset", None)),
        theatre=spec.terrain, scenario=B.sc.name if B.sc else "None",
        aircraft=spec.aircraft, flight_size=spec.flight_size, callsign=callsign, mission_type=f"Strike: {story.replace('_', ' ')}",
        complexity=f"{B.level} ({G.COMPLEXITY_NAMES[B.level]})", mission_statement="", loadout=loadout, stores=stores,
        home={"name": home.name, **G._ll(home.position), "uhf": G._mhz(home.atc_radio.uhf_hz) if home.atc_radio else None,
              "vhf": G._mhz(home.atc_radio.vhf_high_hz) if home.atc_radio else None, "tacan": home.tacan,
              "runways": [r.name for r in home.runways]},
        objective={"name": "OBJECTIVE", **G._ll(objective), "brg": brg, "dist_nm": nm},
        enemy_field=B.enemy_airfields()[0].name, story_id=campaign.new_sortie_id())

    s = Story(B, player, heading_out, distance)
    statement, situation, execution = make(s, objective, f"{callsign} flight ({spec.flight_size}x {spec.aircraft})")
    b.mission_statement, b.situation, b.execution = statement, [situation], [execution]
    for name in rng.sample(list(COMPLICATIONS), min(BY_LEVEL[B.level], len(COMPLICATIONS))):
        COMPLICATIONS[name](s, objective)

    B.add_static_watch()
    ip = home.position.point_from_heading(heading_out, max(distance - (8000 if B.heli else 25000), distance * 0.6))
    player.add_waypoint(ip, alt, speed, "IP")
    player.add_waypoint(objective, alt, speed, "OBJECTIVE")
    player.land_at(home).name = "RECOVERY"
    G._follow_terrain(player)

    behind = home.position.point_from_heading(heading_out + 180, 40000)
    if spec.awacs:
        aw = m.awacs_flight(B.blue, "AWACS", planes.E_3A, None, behind, race_distance=60000,
                            heading=round(heading_out + 90) % 360, altitude=9000, frequency=251)
        b.support.append({"role": "AWACS", "callsign": "OVERLORD", "type": "E-3A", "freq": "251.000", "tacan": None,
                          "alt_ft": round(9000 * G.M_TO_FT, -2), "track": [G._ll(p.position) for p in aw.points[1:3]]})
    wx = G.WEATHER[spec.weather]
    if wx is not None:
        m.weather.clouds_preset = wx.value
        m.weather.clouds_base = 2500
    b.weather = {"summary": spec.weather, "cloud_base_ft": round(2500 * G.M_TO_FT, -2) if wx is not None else None,
                 "visibility": "10+ km", "temp_c": round(m.weather.season_temperature)}
    if B.sc:
        for n in B.sc.blue_fields:
            if n in B.terrain.airports:
                B.terrain.airports[n].set_blue()
        for n in B.sc.red_fields:
            if n in B.terrain.airports:
                B.terrain.airports[n].set_red()
        b.front = [{"name": n, "side": side, **G._ll(B.terrain.airports[n].position)}
                   for side, names in (("blue", B.sc.blue_fields), ("red", B.sc.red_fields))
                   for n in names if n in B.terrain.airports]
    if B.tracked_units:
        b.sortie = campaign.new_sortie_id()
        t = TriggerStart(comment="Campaign results")
        t.add_action(G.InlineDoScript(campaign.results_script(b.sortie, B.tracked_units)))
        m.triggerrules.triggers.append(t)
    m.coalition["blue"].bullseye = {"x": home.position.x, "y": home.position.y}
    b.bullseye = {"name": "BULLSEYE", **G._ll(home.position)}
    b.waypoints = G._route(player)
    G.set_liveries(B)
    m.set_sortie_text(f"OPERATION {b.codename}")
    m.set_description_text(situation)
    m.set_description_bluetask_text(statement)
    return m, b
