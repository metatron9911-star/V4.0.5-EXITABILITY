import inspect
import unittest

import paper

class LateRescueShadowTests(unittest.TestCase):
    def test_votes_all_deteriorating_buy(self):
        t=paper.PaperTrader.__new__(paper.PaperTrader)
        f={
            "ret_1s_bps":-1.0,"ret_250ms_bps":-0.5,"ret_5s_bps":-2.0,
            "buy_ratio_1s":0.40,"book_imbalance_5":-0.20,
        }
        self.assertEqual(t._late_rescue_votes("BUY",f),5)

    def test_votes_directional_sell(self):
        t=paper.PaperTrader.__new__(paper.PaperTrader)
        f={
            "ret_1s_bps":1.0,"ret_250ms_bps":0.5,"ret_5s_bps":2.0,
            "buy_ratio_1s":0.60,"book_imbalance_5":0.20,
        }
        self.assertEqual(t._late_rescue_votes("SELL",f),5)

    def test_shadow_is_observation_only(self):
        src=inspect.getsource(paper.PaperTrader._update_late_rescue_shadow)
        self.assertIn("LATE_RESCUE_SHADOW_ARM",src)
        self.assertIn("LATE_RESCUE_SHADOW_EXIT",src)
        self.assertNotIn("close_position(",src)
        self.assertNotIn("UPDATE trades SET",src)

    def test_registered_parameters_match_replay_winner(self):
        src=inspect.getsource(paper.PaperTrader._update_late_rescue_shadow)
        self.assertIn('"floor_bps":5.0',src)
        self.assertIn('"arm_age_frac":0.70',src)
        self.assertIn('"mfe_threshold_bps":30.0',src)
        self.assertIn('"votes_required":3',src)

    def test_real_tail_has_priority(self):
        src=inspect.getsource(paper.PaperTrader._update_late_rescue_shadow)
        self.assertIn("if p.tail_armed:",src)
        self.assertIn("LATE_RESCUE_SHADOW_HANDOFF",src)

if __name__=="__main__":
    unittest.main()
