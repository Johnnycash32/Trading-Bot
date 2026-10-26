import unittest

from trading_bot.models import Rejection, RiskLimits, Side, TradeProposal, ValidatedTrade
from trading_bot.risk import RiskEngine


class RiskEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RiskEngine(RiskLimits())

    def make_proposal(self, **changes):
        values = dict(
            exchange="paper",
            symbol="BTCUSDT",
            side=Side.LONG,
            entry_price=100_000,
            stop_loss=98_500,
            take_profit=103_000,
            account_equity=10_000,
        )
        values.update(changes)
        return TradeProposal(**values)

    def test_valid_trade_is_sized(self):
        result = self.engine.validate(self.make_proposal())
        self.assertIsInstance(result, ValidatedTrade)
        self.assertGreater(result.quantity, 0)
        self.assertGreater(result.risk_reward_ratio, 1)

    def test_wrong_side_stop_is_rejected(self):
        result = self.engine.validate(self.make_proposal(stop_loss=101_000))
        self.assertIsInstance(result, Rejection)
        self.assertEqual(result.code, "invalid_stop")

    def test_daily_loss_limit_is_hard(self):
        result = self.engine.validate(self.make_proposal(current_daily_loss=200))
        self.assertIsInstance(result, Rejection)
        self.assertEqual(result.code, "daily_loss_limit")

    def test_leverage_limit_is_hard(self):
        result = self.engine.validate(self.make_proposal(leverage=3))
        self.assertIsInstance(result, Rejection)
        self.assertEqual(result.code, "leverage_limit")


if __name__ == "__main__":
    unittest.main()
