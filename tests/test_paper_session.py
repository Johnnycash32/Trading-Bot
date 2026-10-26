import unittest

from trading_bot.market_data import Candle
from trading_bot.paper_session import PaperExecutionSession
from trading_bot.strategy import MovingAverageCrossStrategy


def candles(closes: list[float]) -> list[Candle]:
    return [Candle(index * 60_000, value - 0.5, value + 1, value - 1, value, 100) for index, value in enumerate(closes)]


class PaperSessionTests(unittest.TestCase):
    def test_insufficient_history_holds(self) -> None:
        result = PaperExecutionSession().evaluate("bybit", "SOLUSDT", candles([100, 101]))
        self.assertEqual(result["status"], "hold")

    def test_crossover_creates_simulated_order(self) -> None:
        values = [100, 100, 100, 100, 80, 80, 80, 110]
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        result = session.evaluate("bybit", "SOLUSDT", candles(values))
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["order"]["status"], "simulated")
        self.assertEqual(len(session.orders), 1)

    def test_duplicate_signal_is_not_repeated(self) -> None:
        values = [100, 100, 100, 100, 80, 80, 80, 110]
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        session.evaluate("bybit", "SOLUSDT", candles(values))
        result = session.evaluate("bybit", "SOLUSDT", candles(values))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(len(session.orders), 1)

    def test_target_closes_open_paper_position(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry_values = [100, 100, 100, 100, 80, 80, 80, 110]
        session.evaluate("bybit", "SOLUSDT", candles(entry_values))
        exit_values = [100, 100, 100, 100, 80, 80, 80, 117]
        result = session.evaluate("bybit", "SOLUSDT", candles(exit_values))
        self.assertEqual(result["closed_positions"][0]["status"], "closed")
        self.assertGreater(result["equity"], 10_000.0)
        self.assertEqual(result["open_positions"], [])


if __name__ == "__main__":
    unittest.main()
