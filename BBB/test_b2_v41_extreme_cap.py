import inspect
import unittest

import paper
from strategy_cells import tail_extreme_giveback_cap_bps

class B2V41ExtremeCapTests(unittest.TestCase):
    def test_fast_is_untouched(self):
        trail,applied=tail_extreme_giveback_cap_bps(100.0,"ACCEL",40.0,"FAST")
        self.assertEqual(trail,40.0)
        self.assertFalse(applied)

    def test_extreme_caps(self):
        self.assertEqual(tail_extreme_giveback_cap_bps(100.0,"ACCEL",50.0,"EXTREME")[0],30.0)
        self.assertEqual(tail_extreme_giveback_cap_bps(100.0,"FLAT",50.0,"EXTREME")[0],22.0)
        self.assertEqual(tail_extreme_giveback_cap_bps(100.0,"DECEL",50.0,"EXTREME")[0],15.0)

    def test_cap_does_not_widen_existing_tighter_trail(self):
        trail,applied=tail_extreme_giveback_cap_bps(100.0,"DECEL",10.0,"EXTREME")
        self.assertEqual(trail,10.0)
        self.assertFalse(applied)

    def test_v41_tag_and_telemetry(self):
        src=inspect.getsource(paper.PaperTrader)
        self.assertIn('"impl":"b2-shadow-v4.1-extreme-cap"',src)
        self.assertIn('"b2_extreme_cap_applied"',src)
        self.assertIn("trail_before_extreme_cap_bps",src)

if __name__=="__main__":
    unittest.main()
