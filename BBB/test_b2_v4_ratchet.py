import inspect
import unittest

import paper
import worker
from strategy_cells import (
    tail_ratchet_stop_bps,
    classify_tail_accel_delta,
)


class B2V4RatchetTests(unittest.TestCase):
    def test_ratchet_never_moves_backwards(self):
        cand, stop = tail_ratchet_stop_bps(None, 60.0, 25.0, 15.0)
        self.assertEqual(cand, 35.0)
        self.assertEqual(stop, 35.0)

        cand2, stop2 = tail_ratchet_stop_bps(stop, 70.0, 40.0, 15.0)
        self.assertEqual(cand2, 30.0)
        self.assertEqual(stop2, 35.0)

    def test_ratchet_raises_when_candidate_improves(self):
        _, stop = tail_ratchet_stop_bps(None, 60.0, 25.0, 15.0)
        cand2, stop2 = tail_ratchet_stop_bps(stop, 100.0, 40.0, 15.0)
        self.assertEqual(cand2, 60.0)
        self.assertEqual(stop2, 60.0)

    def test_floor_is_binding_candidate(self):
        cand, stop = tail_ratchet_stop_bps(None, 100.0, 80.0, 45.0)
        self.assertEqual(cand, 45.0)
        self.assertEqual(stop, 45.0)

    def test_v31_delta_classifier_preserved(self):
        self.assertEqual(classify_tail_accel_delta(-1.0, -2.0), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(-3.0, -1.5), "DECEL")

    def test_shadow_feed_is_pinned(self):
        src=inspect.getsource(worker)
        self.assertIn("shadow_held", src)
        self.assertIn("feed.set_symbols(set(new_symbols)|held|shadow_held)", src)

    def test_prod_A_path_not_replaced(self):
        src=inspect.getsource(paper.PaperTrader)
        self.assertIn("self.tail_state", src)
        self.assertIn("B2-v4 profit-lock ratchet", src)
        self.assertIn('"impl":"b2-shadow-v4-ratchet"', src)


if __name__=="__main__":
    unittest.main()
