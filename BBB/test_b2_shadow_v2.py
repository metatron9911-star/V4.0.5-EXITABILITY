import inspect
import unittest

import paper
from strategy_cells import classify_tail_accel_delta, tail_trail_bps_binding


class B2ShadowV2Tests(unittest.TestCase):
    def test_delta_classifier(self):
        self.assertEqual(classify_tail_accel_delta(5.0, 4.5), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(5.0, 4.9), "FLAT")
        self.assertEqual(classify_tail_accel_delta(5.0, 6.0), "DECEL")
        self.assertEqual(classify_tail_accel_delta(0.0, 1.0), "DECEL")

    def test_binding_helper(self):
        self.assertEqual(tail_trail_bps_binding(40.0, 100.0, 38.25, []), 40.0)
        self.assertAlmostEqual(tail_trail_bps_binding(68.0, 170.0, 110.5, [0,1,2]), 59.5)

    def test_v2_semantics_present(self):
        src = inspect.getsource(paper.PaperTrader)
        self.assertIn('"impl":"b2-shadow-v2"', src)
        self.assertIn('"hard_cap_ms":int(pol.max_hold_ms)*6', src)
        self.assertIn('reason="B2_SHADOW_SL"', src)
        self.assertIn('reason="B2_SHADOW_HARD_CAP"', src)
        self.assertIn('A-close NEVER dictates B2 lifetime', src)
        self.assertNotIn('B2_FORCED_', src)

    def test_shadow_is_separate_state(self):
        src = inspect.getsource(paper.PaperTrader)
        self.assertIn('self.tail_state: Dict[str,dict]={}', src)
        self.assertIn('self.b2_tail_shadow: Dict[str,dict]={}', src)
        self.assertIn('self._update_b2_shadows(symbol,f,book)', src)


if __name__ == "__main__":
    unittest.main()
