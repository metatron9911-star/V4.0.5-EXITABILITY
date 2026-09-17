import copy, os, tempfile, unittest
from engine import Store, Experiment, CFG, now_ms

class FakeMarket:
    def __init__(self): self.mult=1.0
    def quote(self, inp, out, raw):
        # Buy: 1 USDC -> 1 token raw unit scale. Sell: token value follows mult.
        buy = inp.startswith('EPjF')
        if buy: val=int(raw)
        else: val=int(raw*self.mult)
        return {'ok':True,'ts_ms':now_ms(),'out_raw':val,'min_raw':val,'impact_pct':0.0}

class V7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=os.path.join(self.tmp.name,'v7_1.sqlite3')
        self.db=Store(self.path,copy.deepcopy(CFG)); self.md=FakeMarket(); self.exp=Experiment(self.db,self.md)
    def tearDown(self): self.db.cx.close(); self.tmp.cleanup()
    def open(self,mint='MINT'):
        self.assertEqual(self.exp.open_cohort(mint,mint,1,6),'COHORT_OPENED')
        return self.db.snapshot()['positions'][-1]
    def test_diversified_first_slot(self):
        budget,reason=self.db.allocation(); self.assertEqual(reason,'DIVERSIFIED_30_RISK_OK'); self.assertAlmostEqual(budget,5,places=4)
    def test_stop_two_percent(self):
        p=self.open(); self.md.mult=.979; self.exp.exit_one(p['id']); p=self.db.snapshot()['positions'][0]
        self.assertEqual(p['remaining_raw'],'0')
    def test_no_stop_at_one_percent(self):
        p=self.open(); self.md.mult=.99; self.exp.exit_one(p['id']); p=self.db.snapshot()['positions'][0]
        self.assertNotEqual(p['remaining_raw'],'0')
    def test_principal_at_100_percent_growth(self):
        p=self.open(); self.md.mult=2.2; self.exp.exit_one(p['id']); p=self.db.snapshot()['positions'][0]
        self.assertTrue(p['half_done']); self.assertGreater(p['received'],p['cost']); self.assertGreater(int(p['remaining_raw']),0)
    def test_runner_stays_without_time_exit(self):
        p=self.open(); self.md.mult=1.5; p0=self.db.snapshot()['positions'][0]
        with self.db.lock,self.db.cx:
            self.db.s['positions'][0]['entry_ms']-=10*3600*1000; self.db.save()
        self.exp.exit_one(p['id']); self.assertGreater(int(self.db.snapshot()['positions'][0]['remaining_raw']),0)

if __name__=='__main__': unittest.main(verbosity=2)
