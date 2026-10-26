import unittest

from trading_bot.market_data import BinanceMarketData, BybitMarketData


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


if __name__ == "__main__":
    unittest.main()
