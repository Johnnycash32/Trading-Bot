import unittest

from trading_bot.copy_trading import CopySettings, CopyStrategyLab, LeadTraderProfile


class CopyTradingTests(unittest.TestCase):
    def profile(self):
        return LeadTraderProfile("p1", "Research profile", 20, 2000, 10, 100000, 20, 1.0, 120, 60, 60, ("BTCUSDT",), 45, True, True)

    def test_evidence_gate_accepts_complete_profile(self):
        result = CopyStrategyLab().assess(self.profile(), CopySettings(preferred_symbols=("BTCUSDT",)))
        self.assertTrue(result.eligible)
        self.assertTrue(result.approval_required)
        self.assertGreater(result.evidence_score, 50)

    def test_incomplete_evidence_is_rejected(self):
        profile = LeadTraderProfile("p2", "Incomplete", 90, 9000, 8, 100000, 10, 2.0, 10, 5, 90, ("BTCUSDT",), 0, True, False)
        result = CopyStrategyLab().assess(profile, CopySettings())
        self.assertFalse(result.eligible)
        self.assertTrue(any("sample" in reason.lower() for reason in result.reasons))
        self.assertTrue(any("slippage" in reason.lower() for reason in result.reasons))

    def test_fixed_ratio_projection(self):
        self.assertEqual(CopyStrategyLab.project_fixed_ratio(100, 20, 1000), 2)


if __name__ == "__main__":
    unittest.main()
