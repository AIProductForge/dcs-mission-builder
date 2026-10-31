"""Run with: .venv/Scripts/python -m unittest discover tests
Everything uses temporary campaign files; campaign/ is never touched. Seeded, so runs repeat."""
import logging
import random
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.ERROR)
from mission_builder import campaign, server, systems, war, weaponeering as W  # noqa: E402

server.save_config = lambda c: None


def fresh():
    random.seed(11)
    tmp = Path(tempfile.mkdtemp())
    campaign.STATE_DIR = tmp / "campaign"
    (tmp / "logs").mkdir()
    config = server.load_config()
    config.update(output_dir=str(tmp / "out"), dcs_logs=str(tmp / "logs"))
    st = server.State(config)
    return st, st.campaigns["PersianGulf"], tmp


class Weaponeering(unittest.TestCase):
    def test_radars_first_and_arm_only_on_radars(self):
        sa6 = [u.id for u in systems.SYSTEMS["SA-6"]]
        plan = W.weaponeer(["2x AGM-88C"], 2, sa6)
        self.assertEqual([a["unit"] for a in plan], [0])
        self.assertEqual(plan[0]["category"], "ARM")

    def test_spread_before_concentrating(self):
        sa10 = [u.id for u in systems.SYSTEMS["SA-10"]]
        plan = W.weaponeer(["2x GBU-27"], 2, sa10, accuracy=0.8)
        self.assertEqual([units for units in (sa10[a["unit"]] for a in plan)], sa10[:2])
        self.assertGreater(W.p_destroyed(plan, sa10), 0.3)

    def test_expend_is_a_dcs_step(self):
        for a in W.weaponeer(["20x AGM-86D"], 2, ["T-72B"] * 7):
            self.assertIn(a["expend"], ("One", "Two", "Four"))

    def test_cruise_missiles_poor_against_tanks(self):
        t = ["T-72B"] * 7
        self.assertLess(W.p_destroyed(W.weaponeer(["20x AGM-86D"], 2, t), t), 0.3)


class Engine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.st, cls.c, cls.tmp = fresh()
        cls.w = cls.c.data["war"]
        war.advance(cls.c, 4 * 3600, random.Random(3))

    def test_force_structure(self):
        missiles = [s for s in self.c.data["sites"] if s.get("missile_type")]
        self.assertEqual(len(missiles), war.MISSILE_SITES)
        self.assertEqual({q["type"] for q in self.w["squadrons"] if q.get("support")}, {"E-3A", "A-50"})
        tiers = [s.get("system") for s in self.c.data["sites"] if s["zone"] == "Bandar Abbas Intl" and s.get("airbase_defence")]
        self.assertIn("SA-10", tiers)

    def test_daily_missile_budget(self):
        self.assertLessEqual(self.w["missiles"]["fired"], war.MISSILES_PER_DAY)

    def test_awacs_up(self):
        self.assertTrue(any(f["mission"] == "AWACS" and f["side"] == "blue" for f in self.w["flights"]))

    def test_frag_refuses_automatic_missions(self):
        q = next(x for x in self.w["squadrons"] if x["type"] == "E-3A")
        with self.assertRaises(RuntimeError):
            war.schedule(self.c, q["id"], "AWACS", 1, None)


class Handoff(unittest.TestCase):
    def setUp(self):
        self.st, self.c, self.tmp = fresh()
        self.w = self.c.data["war"]
        war.advance(self.c, 3 * 3600, random.Random(3))

    def log(self, sortie, *lines):
        text = "\n".join(f"x INFO SCRIPTING: MBRESULT|{sortie}|{ln}" for ln in lines)
        (self.tmp / "logs" / "dcs.log").write_text(text + "\n", encoding="utf-8")

    def test_spectator_has_launchers_and_missiles_are_dcs_owned(self):
        for _ in range(6):   # until some weapons fall in the mission's window
            if war.missile_launches(self.c):
                break
            war.advance(self.c, self.w["clock"] + 3600, random.Random(1))
        res = self.st.spectate("PersianGulf")
        txt = zipfile.ZipFile(res["path"]).read("mission").decode("utf-8")
        self.assertIn('"E-3A"', txt)
        self.assertGreater(txt.count("MB:R"), 0)
        sortie = self.c.data["sorties"][-1]["id"]
        marked = [m for m in self.w["missile_flights"] if m.get("in_dcs") == sortie]
        self.assertTrue(marked)
        # 2 hours of mission: DCS's results stand for all of them
        self.log(sortie, "START|5|0", "END|7200")
        self.st.check_results()
        self.assertEqual(self.w["mode"], "running")
        self.assertFalse(any(m.get("in_dcs") for m in self.w["missile_flights"]))
        for m in marked:
            if m["t1"] <= self.w["clock"]:
                self.assertEqual(m.get("fate"), "flown in DCS")

    def test_give_back_returns_missiles_to_the_engine(self):
        self.st.spectate("PersianGulf")
        self.st.release("PersianGulf")
        self.assertFalse(any(m.get("in_dcs") for m in self.w.get("missile_flights", [])))

    def test_blind_sam_counts_destroyed(self):
        self.st.spectate("PersianGulf")
        sortie = self.c.data["sorties"][-1]
        site_id, radars = next((k, v) for k, v in sortie["radars"].items()
                               if next(s for s in self.c.data["sites"] if s["id"] == k)["role"] == "sam")
        self.log(sortie["id"], "START|5|0", *[f"DEAD|{r}" for r in radars], "END|600")
        self.st.check_results()
        self.assertFalse(next(s for s in self.c.data["sites"] if s["id"] == site_id)["alive"])

    def test_ejection_becomes_a_pilot(self):
        f = next(x for x in self.w["flights"] if x["status"] == "airborne" and x["side"] == "blue" and not x["heli"])
        self.st.spectate("PersianGulf")
        sortie = self.c.data["sorties"][-1]["id"]
        before = len(self.w.get("pilots", []))
        self.log(sortie, "START|5|0", f"POS|{f['id']}|300|0|0|6000|{f['count']}|0.5|1",
                 f"EJECT|MBF:{f['id']}:1|310|1000|2000", "END|600")
        self.st.check_results()
        self.assertGreater(len(self.w.get("pilots", [])), before)


if __name__ == "__main__":
    unittest.main()
