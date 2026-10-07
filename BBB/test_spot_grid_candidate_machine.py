import unittest
from spot_grid_candidate_machine import SpotGridConfig, SpotGridMachine
from spot_quarantine_rescue import RescueConfig, MicroSnapshot

class SpotGridCandidateMachineTests(unittest.TestCase):
    def snap(self,ts,trades,br,imb,r250,r1,mid):
        return MicroSnapshot(ts,trades,br,imb,r250,r1,mid)

    def test_high_churn_quarantines_instead_of_averaging(self):
        m=SpotGridMachine(SpotGridConfig(rescue=RescueConfig(high_churn_trade_count=49)))
        m.open_base("XRPUSDT",100,0.01,0,0)
        a=m.on_safety_candidate(self.snap(1000,50,.5,0,0,0,98))
        self.assertEqual(a,"ENTER_QUARANTINE")
        self.assertEqual(m.basket.levels,1)

    def test_non_high_churn_uses_normal_safety(self):
        m=SpotGridMachine(SpotGridConfig())
        m.open_base("XRPUSDT",100,0.01,0,0)
        a=m.on_safety_candidate(self.snap(1000,20,.5,0,0,0,98))
        self.assertEqual(a,"SAFETY_BUY")
        self.assertEqual(m.basket.levels,2)

    def test_confirmed_rescue_is_single(self):
        cfg=SpotGridConfig(rescue=RescueConfig(quarantine_ms=4000,min_confirmation_votes=3,max_rescue_buys=1))
        m=SpotGridMachine(cfg)
        m.open_base("XRPUSDT",100,0.01,0,0)
        m.on_safety_candidate(self.snap(0,60,.5,0,0,0,98))
        a=m.on_quarantine_snapshots([
            self.snap(3500,60,.45,-.1,-1,-1,97.5),
            self.snap(4200,65,.65,.2,2,2,98.5),
        ])
        self.assertEqual(a,"RESCUE_BUY")
        self.assertTrue(m.basket.rescue_used)

    def test_failed_confirmation_final_cut(self):
        m=SpotGridMachine(SpotGridConfig(rescue=RescueConfig(quarantine_ms=4000)))
        m.open_base("XRPUSDT",100,0.01,0,0)
        m.on_safety_candidate(self.snap(0,60,.5,0,0,0,98))
        a=m.on_quarantine_snapshots([
            self.snap(3500,60,.45,-.1,-1,-1,97.5),
            self.snap(4200,65,.40,-.2,-2,-3,97.0),
        ])
        self.assertEqual(a,"FINAL_CUT")

if __name__=="__main__":
    unittest.main()
