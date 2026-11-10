"""Run with: .venv/Scripts/python -m unittest tests.test_campaign_gen
The generated-campaign flow end to end, with a fake dcs.log; nothing here is flown."""
import logging
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.ERROR)
from mission_builder import campaign, campaign_gen as C, server, strike  # noqa: E402

server.save_config = lambda c: None


class Plan(unittest.TestCase):
    def test_lengths_finale_and_no_repeats(self):
        for length, n in C.LENGTHS.items():
            for heli in (False, True):
                ms = C.plan(length, "Normal", heli, random.Random(n))
                stories = [m["story"] for m in ms]
                self.assertEqual(len(ms), n)
                self.assertEqual(stories[-1], C.FINALE["helicopter" if heli else "plane"])
                self.assertFalse([a for a, b in zip(stories, stories[1:]) if a == b], stories)
                if heli:
                    self.assertTrue(set(stories) <= set(strike.HELICOPTER_STORIES))

    def test_difficulty_climbs_and_stays_in_range(self):
        for diff, (base, _) in C.DIFFICULTY.items():
            levels = [m["complexity"] for m in C.plan("Long", diff, False, random.Random(1))]
            self.assertEqual(levels[0], base)
            self.assertEqual(levels, sorted(levels))
            self.assertLessEqual(max(levels), 5)


class Flow(unittest.TestCase):
    def setUp(self):
        random.seed(3)
        self.tmp = Path(tempfile.mkdtemp())
        C.DIR = self.tmp / "ops"
        campaign.STATE_DIR = self.tmp / "campaign"
        (self.tmp / "logs").mkdir()
        config = server.load_config()
        config.update(output_dir=str(self.tmp / "out"), dcs_logs=str(self.tmp / "logs"))
        self.st = server.State(config)

    def test_new_next_save_and_results(self):
        res = self.st.ops_new({"aircraft": "F-16C_50", "terrain": "PersianGulf"}, {"aircraft", "terrain"}, "Short", "Hard")
        c = res["campaign"]
        self.assertEqual((c["total"], c["next"], c["values"]["aircraft"]), (6, 1, "F-16C_50"))
        built = self.st.ops_next(c["id"])
        first = c["missions"][0]
        self.assertEqual(built["values"]["story"], first["story"])
        self.assertEqual(built["values"]["enemy_skill"], "High")
        self.assertIn("1/6", built["briefing"]["mission_type"])
        sid = built["briefing"]["story_id"]
        saved = self.st.save()
        self.assertEqual(saved["op"]["missions"][0]["status"], "saved")
        self.assertTrue(Path(saved["path"]).exists())
        self.assertIsNone(self.st.current_op, "a second Save must not record a second attempt")

        # Lost: flown again with a new layout.
        (self.tmp / "logs" / "dcs.log").write_text(f"x SCRIPTING (Main): MBSTORY|{sid}|RESULT|failed\n", encoding="utf-8")
        out = self.st.ops_check()
        self.assertEqual(len(out["changes"]), 1)
        again = self.st.ops_next(c["id"])
        self.assertEqual(again["values"]["story"], first["story"])
        self.assertNotEqual(again["values"]["seed"], built["values"]["seed"])
        sid2 = again["briefing"]["story_id"]
        self.st.save()

        # Won: the campaign moves on to mission 2.
        (self.tmp / "logs" / "dcs.log").write_text(f"MBSTORY|{sid2}|RESULT|success\n", encoding="utf-8")
        after = self.st.ops_check()["campaigns"][0]
        self.assertEqual((after["won"], after["next"]), (1, 2))
        self.assertEqual(after["missions"][0]["attempts"], 2)

    def test_mark_by_hand(self):
        c = self.st.ops_new({"aircraft": "F-16C_50", "terrain": "PersianGulf"}, {"aircraft", "terrain"}, "Short", "Easy")["campaign"]
        out = self.st.ops_mark(c["id"], 1, "success")["campaigns"][0]
        self.assertEqual(out["next"], 2)

    def test_story_mission_logs_its_result_with_its_id(self):
        c = self.st.ops_new({"aircraft": "F-16C_50", "terrain": "PersianGulf"}, {"aircraft", "terrain"}, "Short", "Easy")["campaign"]
        _, mission, briefing = (self.st.ops_next(c["id"]), *self.st.current[1:])
        codes = [a.code for t in mission.triggerrules.triggers for a in t.actions if hasattr(a, "code")]
        self.assertTrue(any(f"MBSTORY|{briefing.story_id}|RESULT|success" in s for s in codes))


if __name__ == "__main__":
    unittest.main()
