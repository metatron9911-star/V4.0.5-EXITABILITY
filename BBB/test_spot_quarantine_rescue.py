import unittest
from spot_quarantine_rescue import RescueConfig,MicroSnapshot,QuarantineState,should_quarantine,evaluate_quarantine

class RescueTests(unittest.TestCase):
    def snap(self,ts,trades,br,imb,r250,r1,mid):
        return MicroSnapshot(ts,trades,br,imb,r250,r1,mid)

    def test_only_high_churn_enters_quarantine(self):
        cfg=RescueConfig()
        self.assertFalse(should_quarantine(self.snap(0,49,.6,.1,1,1,100),cfg))
        self.assertTrue(should_quarantine(self.snap(0,50,.6,.1,1,1,100),cfg))

    def test_holds_before_quarantine_elapsed(self):
        cfg=RescueConfig(quarantine_ms=4000)
        st=QuarantineState(armed_ms=1000,initial_trade_count_1s=55,initial_mid=100,best_mid=100,last_mid=100)
        d,_,meta=evaluate_quarantine(st,[self.snap(3000,60,.6,.1,1,1,100.5)],cfg)
        self.assertEqual(d,"HOLD_QUARANTINE")

    def test_confirmed_recovery_allows_one_rescue(self):
        cfg=RescueConfig(quarantine_ms=4000,min_confirmation_votes=3,max_rescue_buys=1)
        st=QuarantineState(armed_ms=0,initial_trade_count_1s=60,initial_mid=100,best_mid=100,last_mid=100)
        snaps=[
            self.snap(3500,62,.54,-.05,-.2,-.4,99.8),
            self.snap(4200,65,.62,.15,1.2,2.0,100.4),
        ]
        d,_,meta=evaluate_quarantine(st,snaps,cfg)
        self.assertEqual(d,"RESCUE_BUY")

    def test_failed_recovery_cuts(self):
        cfg=RescueConfig(quarantine_ms=4000,min_confirmation_votes=3)
        st=QuarantineState(armed_ms=0,initial_trade_count_1s=60,initial_mid=100,best_mid=100,last_mid=100)
        snaps=[
            self.snap(3500,65,.45,-.2,-1,-2,99.5),
            self.snap(4200,70,.42,-.3,-2,-3,99.0),
        ]
        d,_,meta=evaluate_quarantine(st,snaps,cfg)
        self.assertEqual(d,"FINAL_CUT")

    def test_never_second_rescue(self):
        cfg=RescueConfig(quarantine_ms=4000,max_rescue_buys=1)
        st=QuarantineState(armed_ms=0,rescue_buys=1,initial_trade_count_1s=60,initial_mid=100,best_mid=100,last_mid=100)
        snaps=[
            self.snap(3500,70,.6,.1,1,1,100),
            self.snap(4200,72,.7,.2,2,2,101),
        ]
        d,_,meta=evaluate_quarantine(st,snaps,cfg)
        self.assertEqual(d,"FINAL_CUT")

if __name__=="__main__":
    unittest.main()
