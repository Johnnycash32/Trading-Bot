import unittest

from trading_bot.websocket_market import PublicCandleWebSocket, parse_binance_kline, parse_bybit_kline


class WebSocketMarketTests(unittest.TestCase):
    def test_bybit_kline_is_normalized(self) -> None:
        candle = parse_bybit_kline({"data": [[1700000000000, "100", "105", "95", "103", "106", "94", "42", "0"]]})
        self.assertEqual((candle.open, candle.high, candle.low, candle.close, candle.volume), (100, 106, 94, 103, 42))

    def test_binance_kline_is_normalized(self) -> None:
        candle = parse_binance_kline({"k": {"t": 1700000000000, "o": "100", "h": "105", "l": "95", "c": "103", "v": "42"}})
        self.assertEqual((candle.open, candle.high, candle.low, candle.close, candle.volume), (100, 105, 95, 103, 42))

    def test_urls_use_public_market_streams(self) -> None:
        self.assertIn("stream.bybit.com/v5/public/linear", PublicCandleWebSocket("bybit").url("SOLUSDT", "1"))
        self.assertIn("solusdt@kline_1m", PublicCandleWebSocket("binance").url("SOLUSDT", "1"))


if __name__ == "__main__":
    unittest.main()
