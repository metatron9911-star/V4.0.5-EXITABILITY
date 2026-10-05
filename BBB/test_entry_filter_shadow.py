import inspect
import unittest
import paper

class EntryFilterShadowTests(unittest.TestCase):
    def test_threshold_is_frozen(self):
        src=inspect.getsource(paper.PaperTrader.on_snapshot)
        self.assertIn("entry_filter_threshold=83.08",src)
        self.assertIn("entry_filter_would_reject=entry_filter_score < entry_filter_threshold",src)

    def test_shadow_does_not_block_entry(self):
        src=inspect.getsource(paper.PaperTrader.on_snapshot)
        i=src.index("entry_filter_would_reject=entry_filter_score < entry_filter_threshold")
        tail=src[i:i+2200]
        self.assertIn("INSERT INTO trades",tail)
        self.assertNotIn("if entry_filter_would_reject:return",tail.replace(" ",""))

    def test_outcome_uses_actual_net(self):
        src=inspect.getsource(paper.PaperTrader._manage_open)
        self.assertIn("ENTRY_FILTER_SHADOW_OUTCOME",src)
        self.assertIn('"saved_net_if_rejected_usd":saved_net_if_rejected',src)
        self.assertIn('"actual_winner":bool(float(net)>0)',src)

if __name__=="__main__":
    unittest.main()
