import inspect
import unittest
import paper

class ElcV2HardOverrideTests(unittest.TestCase):
    def test_threshold_frozen(self):
        src=inspect.getsource(paper.PaperTrader.__init__)
        self.assertIn("self.elc_v2_mae_skip_threshold_bps=-32.223",src)

    def test_raw_elc_is_preserved(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("early_loser_cut_raw=(not winner_veto) and r2_confirm",src)

    def test_shallow_mae_suppresses_only_elc(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("elc_v2_hard_skip=(",src)
        self.assertIn("early_loser_cut=early_loser_cut_raw and not elc_v2_hard_skip",src)
        self.assertIn("if move_bps<=-pol.stop_loss_bps:",src)
        self.assertIn('elif early_loser_cut:',src)

    def test_override_is_telemetried(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("ELC_V2_HARD_SKIP",src)
        self.assertIn('"elc_v2_hard_skip":elc_v2_hard_skip',src)
        self.assertIn('"early_loser_confirmed_raw":early_loser_cut_raw',src)

if __name__=="__main__":
    unittest.main()
