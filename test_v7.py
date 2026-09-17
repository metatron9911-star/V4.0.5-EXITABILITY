"""Offline V7.2 PAPER tests; no keys/orders/network."""
import tempfile, unittest
from pathlib import Path
from engine import Store, Experiment, now_ms

class Quotes:
    def __init__(self): self.multiple=1.0; self.available=True
    def quote(self,a,b,raw):
        if not self.available:return {'ok':False,'reason':'OFFLINE'}
        v=int(int(raw)*self.multiple) if a!='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v' else int(raw)
        return {'ok':True,'ts_ms':now_ms(),'min_raw':v,'out_raw':v,'impact_pct':0}

class V72Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=Store(Path(self.tmp.name)/'t.db'); self.md=Quotes(); self.exp=Experiment(self.db,self.md)
    def tearDown(self): self.db.cx.close(); self.tmp.cleanup()
    def opened(self):
        self.assertEqual(self.exp.open_cohort('MINT','TOK',1,6,1.0),'COHORT_OPENED'); return self.db.snapshot()['positions'][0]
    def test_adaptive_base_size(self):
        b,r=self.db.allocation(1.0); self.assertEqual(r,'ADAPTIVE_RISK_OK'); self.assertAlmostEqual(b,3.75,places=2)
        b2,r2=self.db.allocation(1.6); self.assertEqual(r2,'ADAPTIVE_RISK_OK'); self.assertGreater(b2,b)
    def test_stop_two_percent(self):
        p=self.opened(); self.md.multiple=.97; self.exp.exit_one(p['id']); self.assertEqual(int(self.db.snapshot()['positions'][0]['remaining_raw']),0)
    def test_no_stop_at_one_percent(self):
        p=self.opened(); self.md.multiple=.995; self.exp.exit_one(p['id']); self.assertGreater(int(self.db.snapshot()['positions'][0]['remaining_raw']),0)
    def test_frequent_net_profit_exit(self):
        p=self.opened(); self.md.multiple=1.06; self.exp.exit_one(p['id']); q=self.db.snapshot()['positions'][0]; self.assertEqual(int(q['remaining_raw']),0); self.assertGreater(q['received'],q['cost'])
    def test_excel_nonempty(self):
        from engine import export_excel
        x=export_excel(self.db,1); self.assertGreater(len(x),1000)

if __name__=='__main__': unittest.main(verbosity=2)
