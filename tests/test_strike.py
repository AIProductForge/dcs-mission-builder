"""Run with: .venv/Scripts/python -m unittest tests.test_strike
Checks the strike generator's structure: nothing here can show how the chains play in DCS."""
import logging
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.ERROR)
from dcs.mission import Mission  # noqa: E402
from mission_builder import campaign, server, strike  # noqa: E402
from mission_builder import generator as G  # noqa: E402

server.save_config = lambda c: None


def state():
    random.seed(5)
    tmp = Path(tempfile.mkdtemp())
    campaign.STATE_DIR = tmp / "campaign"
    (tmp / "logs").mkdir()
    config = server.load_config()
    config.update(output_dir=str(tmp / "out"), dcs_logs=str(tmp / "logs"))
    return server.State(config), tmp


def spec_for(st, story, level, seed, start="Runway (engines running)"):
    values = {"terrain": "PersianGulf", "aircraft": "F-16C_50", "mission_type": "Strike", "complexity": level,
              "seed": seed, "start": start, "flight_size": 2, "awacs": True, "story": story}
    spec = G.randomize(values, {"terrain", "aircraft", "mission_type", "complexity", "seed", "start", "flight_size", "awacs"},
                       st.owned, st.terrains)
    spec.story = story
    return spec


class Stories(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st, cls.tmp = state()

    def test_every_story_builds_saves_and_reloads(self):
        for story in strike.STORIES:
            for level in (1, 3, 5):
                with self.subTest(story=story, level=level):
                    m, b = strike.build(spec_for(self.st, story, level, 100 + level))
                    path = self.tmp / f"{story}{level}.miz"
                    m.save(str(path))
                    again = Mission()
                    again.load_file(str(path))
                    self.assertTrue(b.mission_statement)
                    self.assertTrue(b.targets)

    def test_chains_have_no_dangling_flags_or_groups(self):
        for story in strike.STORIES:
            for seed in range(3):
                with self.subTest(story=story, seed=seed):
                    m, _ = strike.build(spec_for(self.st, story, 5, seed + 1))
                    groups = {g.id for c in m.coalition.values() for k in c.countries.values()
                              for g in k.vehicle_group + k.plane_group + k.helicopter_group + k.static_group}
                    statics = {g.id for c in m.coalition.values() for k in c.countries.values() for g in k.static_group}
                    set_, read = {1, 2}, set()
                    for t in m.triggerrules.triggers:
                        if t.comment == "Watch buildings":
                            set_ |= {int(f) for f in re.findall(r"\]=(\d+)", t.actions[0].code)}
                    for t in m.triggerrules.triggers:
                        for a in t.actions:
                            if isinstance(a, G.action.SetFlag):
                                set_.add(a.flag)
                            if isinstance(a, (G.action.ActivateGroup, G.action.GroupStop, G.action.GroupResume)):
                                self.assertIn(a.group, groups)
                        for c in t.rules:
                            if isinstance(c, (G.condition.FlagIsTrue, G.condition.FlagIsFalse, G.condition.TimeSinceFlag)):
                                read.add(c.flag)
                            if isinstance(c, (G.condition.GroupDead, G.condition.GroupAlive)):
                                self.assertIn(c.group, groups)
                                self.assertNotIn(c.group, statics, "GroupDead is false-positive on statics in DCS")
                    self.assertFalse(read - set_, "a flag is read that nothing sets")

    def test_a_random_branch_rolls_only_once(self):
        for story in strike.STORIES:
            for seed in range(4):
                m, _ = strike.build(spec_for(self.st, story, 5, seed + 1))
                for t in m.triggerrules.triggers:
                    if any(isinstance(c, G.condition.Random) for c in t.rules):
                        self.assertIsInstance(t, G.TriggerStart, t.comment)

    def test_old_strike_mission_watches_its_buildings_too(self):
        spec = spec_for(self.st, "", 3, 11)
        m, _ = G.build(spec)
        success = next(t for t in m.triggerrules.triggers if t.comment == "Mission success")
        self.assertFalse([c for c in success.rules if isinstance(c, G.condition.GroupDead)])
        self.assertIn("Watch buildings", [t.comment for t in m.triggerrules.triggers])

    def test_f15e_flies_a_usaf_livery(self):
        values = {"terrain": "PersianGulf", "aircraft": "F-15ESE", "mission_type": "Strike", "complexity": 3, "seed": 5}
        spec = G.randomize(values, set(values), self.st.owned, self.st.terrains)
        for build in (strike.build, lambda s: G.build(s)):
            m, _ = build(spec)
            player = next(g for g in m.coalition["blue"].countries[G.countries.CombinedJointTaskForcesBlue.name].plane_group
                          if g.name == "Player Flight")
            self.assertTrue(player.units[0].livery_id.startswith("usaf"), player.units[0].livery_id)

    def test_every_story_can_end(self):
        for story in strike.STORIES:
            m, _ = strike.build(spec_for(self.st, story, 3, 7))
            names = [t.comment for t in m.triggerrules.triggers]
            self.assertIn("Mission success", names, story)

    def test_scud_deadline_leaves_time_from_a_cold_start(self):
        m, b = strike.build(spec_for(self.st, "scud_hunt", 3, 9, start="Ramp, cold"))
        leave = strike.START_DELAY["Ramp, cold"] + b.objective["dist_nm"] * 1852 / 222
        warn = next(t for t in m.triggerrules.triggers if t.comment == "Launch")
        deadline = next(c.seconds for c in warn.rules if isinstance(c, G.condition.TimeAfter))
        self.assertGreater(deadline, leave)

    def test_same_seed_same_story(self):
        spec = spec_for(self.st, "", 3, 42)
        a, b = strike.build(spec)[1], strike.build(spec)[1]
        self.assertEqual(a.mission_type, b.mission_type)
        self.assertEqual(a.objective, b.objective)

    def test_server_endpoint_keeps_pins_and_rolls_stories(self):
        seen = set()
        for _ in range(12):
            res = self.st.generate_strike({"aircraft": "F-16C_50", "terrain": "PersianGulf"}, {"aircraft", "terrain"})
            self.assertEqual(res["values"]["aircraft"], "F-16C_50")
            seen.add(res["briefing"]["mission_type"])
        self.assertGreater(len(seen), 2)
        self.assertTrue(re.match(r"Strike: ", next(iter(seen))))


if __name__ == "__main__":
    unittest.main()
