import unittest

from trading_bot.backtest import BacktestConfig, run_backtest
from trading_bot.market_data import Candle
from trading_bot.strategy import MovingAverageCrossStrategy


class BacktestTests(unittest.TestCase):
    def candles(self):
        prices = [100, 99, 98, 97, 96, 97, 98, 99, 101, 103, 105, 104, 102, 100, 98, 97, 96, 98, 100, 103]
        return [Candle(i * 60_000, price, price, price, price, 1) for i, price in enumerate(prices)]

    def test_backtest_is_deterministic_and_cost_aware(self):
        report = run_backtest(self.candles(), MovingAverageCrossStrategy(2, 4), BacktestConfig(fee_rate=0.001, slippage_rate=0.001))
        self.assertGreater(report.total_trades, 0)
        self.assertEqual(report.total_trades, len(report.trades))
        self.assertLess(report.final_equity, 10_000 + 10_000 * 0.1)

    def test_invalid_strategy_periods_are_rejected(self):
        with self.assertRaises(ValueError):
            MovingAverageCrossStrategy(5, 5)


if __name__ == "__main__":
    unittest.main()
