import unittest
from dynamic_scalp_profile import classify_segment, directional_hype_trigger

class DynamicScalpProfileTests(unittest.TestCase):
    def test_hype_route(self):
        f=dict(trade_count_1s=8,trade_count_5s=30,ret_1s_bps=5,ret_5s_bps=12,
               realized_vol_5s_bps=.8,spread_bps=6,depth_bid_5_usd=5000,depth_ask_5_usd=6000)
        self.assertEqual(classify_segment(f),"HYPE_TREND")

    def test_liquid_route(self):
        f=dict(trade_count_1s=2,trade_count_5s=8,ret_1s_bps=.5,ret_5s_bps=1.2,
               realized_vol_5s_bps=.1,spread_bps=1.5,depth_bid_5_usd=80000,depth_ask_5_usd=70000)
        self.assertEqual(classify_segment(f),"LIQUID_FLAT")

    def test_no_trade_route(self):
        f=dict(trade_count_1s=0,trade_count_5s=1,ret_1s_bps=.1,ret_5s_bps=.2,
               realized_vol_5s_bps=.05,spread_bps=20,depth_bid_5_usd=1000,depth_ask_5_usd=1200)
        self.assertEqual(classify_segment(f),"NO_TRADE")

    def test_hype_direction_trigger(self):
        f=dict(trade_count_1s=9,ret_250ms_bps=1.2,ret_1s_bps=4.0,buy_ratio_1s=.65,spread_bps=4)
        self.assertTrue(directional_hype_trigger("BUY",f))
        self.assertFalse(directional_hype_trigger("SELL",f))

if __name__=="__main__":
    unittest.main()
