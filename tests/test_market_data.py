import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from trading_bot.market_data import BinanceMarketData, BybitMarketData, CoinbaseMarketData, TwelveDataMarketData


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

    def test_twelve_data_forex_candles_and_quote_are_normalized(self):
        requests = []

        def loader(url):
            requests.append(url)
            if "/time_series?" in url:
                return {"values": [
                    {"datetime": "2025-01-02 10:00:00", "open": "1.02", "high": "1.04", "low": "1.01", "close": "1.03", "volume": None},
                    {"datetime": "2025-01-02 09:55:00", "open": "1.01", "high": "1.03", "low": "1.00", "close": "1.02", "volume": "5"},
                ]}
            return {"close": "1.03", "percent_change": "0.5", "timestamp": "1735812300"}

        client = TwelveDataMarketData("test-secret", loader)
        candles = client.candles("EURUSD", "5", 2, asset_class="forex")
        ticker = client.ticker("EURUSD", asset_class="forex")

        self.assertEqual([candle.close for candle in candles], [1.02, 1.03])
        self.assertEqual([candle.volume for candle in candles], [5, None])
        self.assertEqual(ticker.last, 1.03)
        self.assertEqual(ticker.change_percent, 0.5)
        self.assertEqual(ticker.timestamp_ms, 1_735_812_300_000)
        time_series_query = parse_qs(urlsplit(requests[0]).query)
        self.assertEqual(time_series_query["symbol"], ["EUR/USD"])
        self.assertEqual(time_series_query["interval"], ["5min"])
        self.assertEqual(parse_qs(urlsplit(requests[1]).query)["symbol"], ["EUR/USD"])

    def test_twelve_data_caches_provider_responses_without_changing_normalization(self):
        calls = []
        client = TwelveDataMarketData("test-secret", lambda url: calls.append(url) or {"close": "250"})

        first = client.ticker("AAPL", asset_class="stocks")
        second = client.ticker("AAPL", asset_class="stocks")

        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_twelve_data_requires_key_and_surfaces_provider_errors(self):
        with patch.object(TwelveDataMarketData, "_load_api_key", return_value=None):
            missing_key_client = TwelveDataMarketData(loader=lambda url: {})
        with self.assertRaisesRegex(RuntimeError, "TWELVE_DATA_API_KEY"):
            missing_key_client.candles("EURUSD", "1", asset_class="forex")

        failing_client = TwelveDataMarketData(
            "test-secret",
            lambda url: {"status": "error", "message": "Invalid API key"},
        )
        with self.assertRaisesRegex(ValueError, "Invalid API key"):
            failing_client.ticker("EURUSD", asset_class="forex")

    def test_twelve_data_rejects_unmapped_symbols_and_intervals(self):
        client = TwelveDataMarketData("test-secret", lambda url: {})
        with self.assertRaisesRegex(ValueError, "mapping"):
            client.candles("CL1!", "5", asset_class="commodities")
        with self.assertRaisesRegex(ValueError, "does not support"):
            client.candles("EURUSD", "3", asset_class="forex")


if __name__ == "__main__":
    unittest.main()
