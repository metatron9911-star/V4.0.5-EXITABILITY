import inspect
import unittest
import paper

class ElcV2ShadowTests(unittest.TestCase):
    def test_threshold_frozen(self):
        src=inspect.getsource(paper.PaperTrader.__init__)
        self.assertIn("self.elc_v2_mae_skip_threshold_bps=-32.223",src)

    def test_real_elc_logic_unchanged(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("early_loser_cut=(not winner_veto) and r2_confirm",src)
        self.assertIn('reason="EARLY_LOSER_CUT"',src)

    def test_shadow_decision_event_present(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("ELC_V2_SHADOW_SKIP",src)
        self.assertIn("float(mae_bps)>float(self.elc_v2_mae_skip_threshold_bps)",src)

    def test_shadow_outcome_uses_existing_60s_counterfactual(self):
        src=inspect.getsource(paper.PaperTrader._update_elc_shadows)
        self.assertIn("ELC_V2_SHADOW_OUTCOME",src)
        self.assertIn("if sec==60 and s.get",src)
        self.assertIn("delta_bps=float(ret)-float(s",src)

if __name__=="__main__":
    unittest.main()
