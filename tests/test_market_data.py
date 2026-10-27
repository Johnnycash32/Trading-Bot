import unittest

from trading_bot.market_data import BinanceMarketData, BybitMarketData, CoinbaseMarketData


class MarketDataTests(unittest.TestCase):
    def test_bybit_ticker_is_normalized(self):
        def loader(url):
            self.assertIn("api.bybit.com/v5/market/tickers", url)
            return {"time": "1700000000000", "result": {"list": [{"lastPrice": "100", "bid1Price": "99", "ask1Price": "101", "price24hPcnt": "0.05"}]}}

        ticker = BybitMarketData(loader).ticker()
        self.assertEqual(ticker.exchange, "bybit")
        self.assertEqual(ticker.last, 100)
        self.assertEqual(ticker.change_percent, 5)

    def test_binance_candles_are_normalized(self):
        rows = [[1700000000000, "100", "110", "90", "105", "12"]]
        candles = BinanceMarketData(lambda url: rows).candles(limit=1)
        self.assertEqual(candles[0].close, 105)
        self.assertEqual(candles[0].volume, 12)

    def test_coinbase_candles_are_normalized_and_reversed_to_ascending_order(self):
        rows = [
            [1700000060, 98, 109, 99, 108, 25],
            [1700000000, 90, 105, 91, 100, 12],
        ]
        urls = []
        client = CoinbaseMarketData(lambda url: urls.append(url) or rows)

        result = client.candles("SOLUSDT", "1", 2)

        self.assertIn("/products/SOL-USD/candles?", urls[0])
        self.assertEqual([item.timestamp_ms for item in result], [1700000000000, 1700000060000])
        self.assertEqual((result[0].open, result[0].high, result[0].low, result[0].close, result[0].volume), (91, 105, 90, 100, 12))

    def test_coinbase_rejects_intervals_that_would_be_mislabeled(self):
        client = CoinbaseMarketData(lambda url: [])
        with self.assertRaisesRegex(ValueError, "does not support"):
            client.candles("BTCUSDT", "3", 100)


if __name__ == "__main__":
    unittest.main()
