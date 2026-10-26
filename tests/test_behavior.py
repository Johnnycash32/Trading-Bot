import unittest

from trading_bot.behavior import BehaviorEngine
from trading_bot.market_data import Candle
from trading_bot.strategy import Signal


class BehaviorTests(unittest.TestCase):
    def candles(self):
        return [Candle(i * 60_000, value - 1, value + 1, value - 1, value, 100) for i, value in enumerate([100, 100, 100, 100, 80, 80, 80, 110])]

    def test_hold_is_monitor_only(self):
        result = BehaviorEngine().assess(Signal.HOLD, self.candles())
        self.assertEqual(result.decision, "monitor")
        self.assertEqual(result.confidence, 0.0)

    def test_valid_signal_is_paper_simulation_and_approval_gated(self):
        result = BehaviorEngine().assess(Signal.LONG, self.candles(), risk_reward_ratio=2.0, notional=100, account_equity=10_000)
        self.assertEqual(result.decision, "paper_simulate")
        self.assertTrue(result.approval_required)
        self.assertTrue(any(agent["name"] == "Lion" for agent in result.agents))

    def test_risk_failure_rejects_signal(self):
        result = BehaviorEngine().assess(Signal.LONG, self.candles(), risk_approved=False, risk_reason="Position limit")
        self.assertEqual(result.decision, "reject")
        self.assertIn("Position limit", result.reasons)


if __name__ == "__main__":
    unittest.main()
