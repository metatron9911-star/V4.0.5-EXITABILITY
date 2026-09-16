import os
import sys
import tempfile
import types


def main():
    with tempfile.TemporaryDirectory() as td:
        os.environ["DATA_DIR"] = td
        os.environ["V4_MODE"] = "PAPER"
        os.environ["AUTO_TRADING_ENABLED"] = "false"

        # The smoke test is offline. Railway installs real requests from
        # requirements.txt; this tiny stub only lets us test local strategy logic.
        requests_stub = types.ModuleType("requests")
        requests_stub.Session = lambda: types.SimpleNamespace()
        requests_stub.get = lambda *a, **k: None
        requests_stub.post = lambda *a, **k: None
        sys.modules.setdefault("requests", requests_stub)

        import engine

        e = engine.V4Engine()
        assert engine.CFG["version"] == "4.0.8"
        assert e.hard_safety()["execution"] == "DISABLED_IN_CODE"

        metrics = {
            "price": 0.0001,
            "liquidity": 25000,
            "v5": 2500,
            "buys": 30,
            "sells": 10,
        }
        e.exitability_for_entry = lambda mint, price: (
            False,
            {"ok": False, "reason": "Jupiter HTTP 503"},
        )
        allowed, reason, route, detail = e._entry_gate(
            "TEST_MINT", "TEST", 60, 20, metrics
        )
        assert allowed and reason == "ENTRY_OK_DEX_FALLBACK" and route == "DEX_PROXY"

        opened, open_reason = e.pm.open_paper_with_reason(
            "TEST_MINT", "TEST", metrics["price"]
        )
        assert opened and open_reason == "OPENED"
        assert e.stats()["open"] == 1

        e._record_entry_decision(
            "TEST_MINT", "TEST", "OPEN", reason, 60, 20, metrics, route, detail
        )
        row = e.db.q("SELECT decision,reason,route_status FROM entry_decisions")[-1]
        assert row == {
            "decision": "OPEN",
            "reason": "ENTRY_OK_DEX_FALLBACK",
            "route_status": "DEX_PROXY",
        }
        print("V4.0.8 smoke test: PASS")


if __name__ == "__main__":
    main()
