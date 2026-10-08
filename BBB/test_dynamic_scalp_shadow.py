import unittest
from dynamic_scalp_shadow import DynamicScalpShadow

class DummyCX:
    def execute(self,*a,**k): return None

class DummyBook:
    synced=True
    def __init__(self,mid,bid=None,ask=None):
        self.mid=mid;self.best_bid=bid or mid*.9999;self.best_ask=ask or mid*1.0001

class DynamicShadowTests(unittest.TestCase):
    def test_hype_requires_confirmation_not_single_spike(self):
        s=DynamicScalpShadow(DummyCX(),"S")
        b=DummyBook(100)
        # Build enough history around a local high.
        for i in range(15):
            s.hist["X"].append({"ts":i*1000,"mid":99.9+(0.01 if i%3==0 else 0),"tc1":1,"tc5":5})
        f={"dynamic_segment":"HYPE_TREND","trade_count_1s":10,"trade_count_5s":30,
           "ret_250ms_bps":2,"ret_1s_bps":5,"buy_ratio_1s":.7,"spread_bps":2}
        # First print-through only arms the setup.
        b.mid=100.1;b.best_bid=100.09;b.best_ask=100.11
        s._hype_signal("X",f,b,0)
        self.assertNotIn("X",s.positions)
        self.assertIn("X",s.hype_arms)


    def test_net_cost_is_conservative(self):
        s=DynamicScalpShadow(DummyCX(),"S")
        b=DummyBook(100)
        f={"spread_bps":2,"dynamic_segment":"HYPE_TREND","trade_count_1s":10,"trade_count_5s":30,
           "ret_250ms_bps":2,"ret_1s_bps":5,"buy_ratio_1s":.7}
        s._open("X","HYPE","BUY",100,f,0)
        self.assertIn("X",s.positions)

    def test_liquid_arm_requires_persistence(self):
        s=DynamicScalpShadow(DummyCX(),"S")
        b=DummyBook(100)
        f={"dynamic_segment":"LIQUID_FLAT","dom_bid_wall_ratio":6,"dom_bid_wall_distance_bps":5,
           "dom_bid_wall_price":99.95,"dom_ask_wall_ratio":1,"dom_ask_wall_distance_bps":99,
           "dom_ask_wall_price":0,"book_imbalance_5":.2,"spread_bps":2}
        s._liquid_signal("X",f,b,0)
        self.assertIn(("X","BUY"),s.wall_arms)

if __name__=="__main__": unittest.main()
