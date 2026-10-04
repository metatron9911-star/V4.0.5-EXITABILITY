import inspect
import unittest

import paper
from strategy_cells import classify_tail_accel_delta


class B2ShadowV3Tests(unittest.TestCase):
    def test_raw_classifier_still_unchanged(self):
        self.assertEqual(classify_tail_accel_delta(5.0, 4.5), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(5.0, 4.9), "FLAT")
        self.assertEqual(classify_tail_accel_delta(5.0, 6.0), "DECEL")

    def test_d2_classifier_any_sign(self):
        self.assertEqual(classify_tail_accel_delta(-1.0, -2.0), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(-3.0, -1.5), "DECEL")
        self.assertEqual(classify_tail_accel_delta(0.2, 0.0), "FLAT")
        self.assertEqual(classify_tail_accel_delta(-0.2, 0.2), "DECEL")

    def test_v3_semantics_present(self):
        src=inspect.getsource(paper.PaperTrader)
        self.assertIn('"impl":"b2-shadow-v3"', src)
        self.assertIn('"warmup_target":3', src)
        self.assertIn('"decel_streak":0', src)
        self.assertIn('decel_streak>=2', src)
        self.assertIn('first 3 post-arm ticks are forced FLAT', src)
        self.assertIn('A-close NEVER dictates B2 lifetime', src)
        self.assertNotIn('B2_FORCED_', src)

    def test_v3_records_raw_and_effective_state(self):
        src=inspect.getsource(paper.PaperTrader)
        self.assertIn('raw_accel=classify_tail_accel_delta', src)
        self.assertIn('st["raw_accel"]=raw_accel', src)
        self.assertIn('"raw_accel_final":st.get("raw_accel")', src)
        self.assertIn('"warmup_ticks":st.get("warmup_ticks",0)', src)
        self.assertIn('"decel_streak":st.get("decel_streak",0)', src)


if __name__=="__main__":
    unittest.main()
