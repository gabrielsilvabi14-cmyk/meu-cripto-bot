import unittest
from app.trading.risk import RiskConfig, position_size
from app.trading.paper import PaperAccount
from app.integrations.gmgn import normalize_token

class TestRisk(unittest.TestCase):
    def test_size_includes_costs(self):
        self.assertEqual(position_size(100, RiskConfig(), 2), 14.29)
    def test_allocation_cap(self):
        self.assertEqual(position_size(100, RiskConfig(stop_pct=1), 0), 20)
    def test_paper_round_trip(self):
        a = PaperAccount()
        a.buy("test-token", "eth", 1, 20000)
        self.assertEqual(len(a.positions), 1)
        a.sell("test-token", 1)
        self.assertLess(a.cash_usd, 100)
        self.assertEqual(len(a.positions), 0)
    def test_unknown_security_not_verified(self):
        x = normalize_token({"address":"test"}, "eth")
        self.assertEqual(x.security, {})
        self.assertIsNone(x.liquidity_usd)

if __name__ == "__main__":
    unittest.main()
