import json
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
from unittest.mock import patch

from trading_bot import server
from trading_bot.market_data import Candle, Ticker


class FakeMarketClient:
    def __init__(
        self,
        candles: list[Candle] | Exception,
        ticker: Ticker | Exception,
    ) -> None:
        self.candle_result = candles
        self.ticker_result = ticker
        self.requested_intervals: list[str] = []

    def candles(self, symbol: str, interval: str, limit: int, end_ms: int | None) -> list[Candle]:
        self.requested_intervals.append(interval)
        if isinstance(self.candle_result, Exception):
            raise self.candle_result
        return self.candle_result

    def ticker(self, symbol: str) -> Ticker:
        if isinstance(self.ticker_result, Exception):
            raise self.ticker_result
        return self.ticker_result


def sample_candles() -> list[Candle]:
    return [Candle(1_700_000_000_000, 99, 102, 98, 101, 42)]


def sample_ticker(exchange: str = "bybit") -> Ticker:
    return Ticker(exchange, "SOLUSDT", 101, 100.9, 101.1, 1.5, 1_700_000_000_000)


class MarketApiTests(unittest.TestCase):
    def test_chart_candles_fail_over_to_other_venue(self) -> None:
        bybit = FakeMarketClient(OSError("Bybit timed out"), OSError("Bybit timed out"))
        binance = FakeMarketClient(sample_candles(), sample_ticker("binance"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            payload = server.market_payload("bybit", "SOLUSDT", "1")

        self.assertEqual(payload["candles"][0]["close"], 101)
        self.assertEqual(payload["market_data"]["candle_source"], "binance")
        self.assertTrue(payload["market_data"]["fallback"])
        self.assertEqual(binance.requested_intervals, ["1m"])

    def test_chart_is_returned_when_ticker_is_unavailable(self) -> None:
        bybit = FakeMarketClient(sample_candles(), OSError("ticker request timed out"))
        binance = FakeMarketClient(OSError("candle request failed"), OSError("ticker request failed"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            payload = server.market_payload("bybit", "SOLUSDT", "1")

        self.assertEqual(payload["candles"][0]["close"], 101)
        self.assertEqual(payload["ticker"]["last"], 101)
        self.assertIsNone(payload["ticker"]["change_percent"])
        self.assertTrue(payload["market_data"]["ticker_unavailable"])
        self.assertEqual(payload["market_data"]["ticker_source"], "candle_close")

    def test_binance_requested_timeframe_is_translated(self) -> None:
        bybit = FakeMarketClient(OSError("Bybit unavailable"), OSError("Bybit unavailable"))
        binance = FakeMarketClient(sample_candles(), sample_ticker("binance"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            server.market_payload("bybit", "SOLUSDT", "60")

        self.assertEqual(binance.requested_intervals, ["1h"])

    def test_chart_error_is_raised_only_when_both_candle_feeds_fail(self) -> None:
        bybit = FakeMarketClient(OSError("TLS handshake timed out"), OSError("ticker timed out"))
        binance = FakeMarketClient(OSError("DNS lookup failed"), OSError("ticker lookup failed"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            with self.assertRaisesRegex(RuntimeError, "No exchange returned chart candles"):
                server.market_payload("bybit", "SOLUSDT", "1")

    def test_market_endpoint_returns_chart_when_ticker_is_unavailable(self) -> None:
        bybit = FakeMarketClient(sample_candles(), OSError("ticker request timed out"))
        binance = FakeMarketClient(OSError("candle request failed"), OSError("ticker request failed"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/api/market?exchange=bybit&symbol=SOLUSDT&interval=1") as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["candles"][0]["close"], 101)
                    self.assertTrue(payload["market_data"]["ticker_unavailable"])
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
