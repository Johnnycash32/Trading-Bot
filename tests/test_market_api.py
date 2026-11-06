import json
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
from unittest.mock import patch

from trading_bot import server
from trading_bot.market_data import Candle, Ticker, TwelveDataMarketData
from trading_bot.paper_session import PaperExecutionSession
from trading_bot.strategy import MovingAverageCrossStrategy


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
    def test_chart_route_uses_ordered_provider_and_returns_normalized_json(self) -> None:
        class FakeChartProvider:
            priority = ("binance", "kraken", "okx", "bybit", "bitget", "hyperliquid")

            def candles(self, symbol, interval, limit, end_ms, preferred_source):
                self.assert_called = (symbol, interval, limit, end_ms, preferred_source)
                return "okx", [
                    type("Candle", (), {
                        "timestamp_ms": 1_700_000_000_000,
                        "close": 101.0,
                        "as_dict": lambda candle: {
                            "time": candle.timestamp_ms,
                            "timestamp_ms": candle.timestamp_ms,
                            "open": 99.0, "high": 102.0, "low": 98.0, "close": candle.close, "volume": 42.0,
                        },
                    })()
                ], ["binance: unavailable"]

        provider = FakeChartProvider()
        with patch.object(server, "CHART_PROVIDER", provider), patch.object(server, "MARKET_CLIENTS", {}):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/api/chart/SOLUSDT?interval=5&source=okx") as response:
                    body = response.read().decode()
                    payload = json.loads(body)
                    self.assertEqual(response.status, 200)
                    self.assertIn("application/json", response.headers["Content-Type"])
                    self.assertEqual(payload["market_data"]["candle_source"], "okx")
                    self.assertEqual(payload["candles"][0]["time"], 1_700_000_000_000)
                    self.assertEqual(payload["market_data"]["provider_priority"], list(FakeChartProvider.priority))
                    self.assertEqual(provider.assert_called, ("SOLUSDT", "5", 100, None, "okx"))
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    def test_missing_api_route_returns_json_instead_of_html_or_plain_text(self) -> None:
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        thread = Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                urlopen(f"http://127.0.0.1:{http_server.server_port}/api/missing")
            except Exception as error:
                response = error
            self.assertEqual(response.code, 404)
            self.assertIn("application/json", response.headers["Content-Type"])
            self.assertEqual(json.loads(response.read()), {"error": "API endpoint not found"})
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)

    def test_agent_portraits_are_served_from_the_allowlist(self) -> None:
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        thread = Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in ("raccoon", "eagle", "hawk", "lion", "cheetah", "hare"):
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/ui/assets/agents/{name}.jpg") as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                    self.assertEqual(response.read(2), b"\xff\xd8")

            try:
                urlopen(f"http://127.0.0.1:{http_server.server_port}/ui/assets/agents/unknown.jpg")
            except Exception as error:
                response = error
            self.assertEqual(response.code, 404)
        finally:
            http_server.shutdown()
            http_server.server_close()
            thread.join(timeout=2)

    def test_public_copy_candidates_are_real_leaderboard_rows_not_synthetic_profiles(self) -> None:
        class FakeSource:
            def discover_traders(self, limit):
                self.limit = limit
                return [{
                    "source_trader_id": "0x" + "1" * 40,
                    "display_name": "Wallet",
                    "account_value": 1000.0,
                    "month_pnl": 25.0,
                    "month_roi": 0.02,
                }]

        class FakeMonitor:
            source = FakeSource()

        with patch.object(server, "HYPERLIQUID_MONITOR", FakeMonitor()):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/api/copy/candidates") as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["candidates"][0]["profile"]["month_pnl"], 25.0)
                    self.assertIsNone(payload["candidates"][0]["assessment"]["evidence_score"])
                    self.assertFalse(payload["candidates"][0]["assessment"]["eligible"])
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    def test_paper_evaluation_uses_only_candles_that_have_closed(self) -> None:
        candles = [
            Candle(60_000, 99, 102, 98, 101, 42),
            Candle(120_000, 101, 103, 100, 102, 40),
            Candle(180_000, 102, 104, 101, 103, 38),
        ]

        confirmed = server.closed_candles(candles, 60_000, now_ms=180_000)

        self.assertEqual([candle.timestamp_ms for candle in confirmed], [60_000, 120_000])

    def test_paper_evaluation_fails_closed_for_unknown_candle_interval(self) -> None:
        self.assertEqual(server.closed_candles(sample_candles(), None, now_ms=1_800_000_000_000), [])

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

    def test_coinbase_can_be_selected_as_an_independent_chart_feed(self) -> None:
        coinbase = FakeMarketClient(sample_candles(), sample_ticker("coinbase"))
        with patch.object(server, "MARKET_CLIENTS", {"coinbase": coinbase}):
            payload = server.market_payload("coinbase", "SOLUSDT", "1")

        self.assertEqual(payload["market_data"]["candle_source"], "coinbase")
        self.assertEqual(payload["ticker"]["exchange"], "coinbase")

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

    def test_twelve_data_chart_endpoint_routes_asset_class_and_keeps_key_private(self) -> None:
        urls = []
        now = datetime.now(timezone.utc)
        timestamp = int(now.timestamp())

        def loader(url):
            urls.append(url)
            if "/time_series?" in url:
                return {"values": [{
                    "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": "1.1", "high": "1.2", "low": "1.0", "close": "1.15", "volume": "10",
                }]}
            return {"close": "1.15", "timestamp": str(timestamp), "percent_change": "0.3"}

        client = TwelveDataMarketData("server-only-test-key", loader)
        with patch.object(server, "MARKET_CLIENTS", {"twelvedata": client}):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{http_server.server_port}/api/market?exchange=twelvedata&symbol=EURUSD&interval=5&asset_class=forex"
                with urlopen(url) as response:
                    body = response.read().decode()
                    payload = json.loads(body)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["candles"][0]["close"], 1.15)
                    self.assertEqual(payload["ticker"]["last"], 1.15)
                    self.assertEqual(payload["market_data"]["candle_source"], "twelvedata")
                    self.assertNotIn("server-only-test-key", body)
                self.assertIn("symbol=EUR%2FUSD", urls[0])
                self.assertIn("interval=5min", urls[0])
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    def test_market_quote_endpoint_returns_only_normalized_quote(self) -> None:
        bybit = FakeMarketClient(sample_candles(), sample_ticker())
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit}):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/api/market/quote?exchange=bybit&symbol=SOLUSDT") as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["ticker"]["last"], 101)
                    self.assertEqual(payload["exchange"], "bybit")
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    def test_twelve_data_quote_endpoint_routes_forex_and_keeps_key_private(self) -> None:
        urls = []
        client = TwelveDataMarketData(
            "server-only-test-key",
            lambda url: urls.append(url) or {"close": "1.08", "timestamp": "1735812300"},
        )
        with patch.object(server, "MARKET_CLIENTS", {"twelvedata": client}):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{http_server.server_port}/api/market/quote?exchange=twelvedata&symbol=EURUSD&asset_class=forex"
                with urlopen(url) as response:
                    body = response.read().decode()
                    payload = json.loads(body)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(payload["ticker"]["last"], 1.08)
                    self.assertEqual(payload["ticker"]["symbol"], "EURUSD")
                    self.assertNotIn("server-only-test-key", body)
                self.assertIn("symbol=EUR%2FUSD", urls[0])
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)

    def test_demo_copy_positions_hide_marks_when_public_quotes_are_stale_or_unavailable(self) -> None:
        stale = FakeMarketClient(sample_candles(), Ticker("bybit", "BTCUSDT", 101, 100, 102, 0, 1))
        unavailable = FakeMarketClient(sample_candles(), OSError("ticker unavailable"))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": stale, "binance": unavailable}):
            accounts, unrealized_pnl = server.live_copy_accounts()

        positions = [position for account in accounts for position in account.positions]
        self.assertTrue(all(position.mark_price is None for position in positions))
        self.assertTrue(all(position.unrealized_pnl is None for position in positions))
        self.assertEqual([position.mark_status for position in positions], ["stale", "stale", "unavailable"])
        self.assertEqual(unrealized_pnl, 0)

    def test_demo_copy_positions_mark_only_fresh_public_quotes(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        bybit = FakeMarketClient(sample_candles(), Ticker("bybit", "BTCUSDT", 80_200, 80_199, 80_201, 0, now))
        binance = FakeMarketClient(sample_candles(), Ticker("binance", "ETHUSDT", 2_326, 2_325, 2_327, 0, now))
        with patch.object(server, "MARKET_CLIENTS", {"bybit": bybit, "binance": binance}):
            accounts, unrealized_pnl = server.live_copy_accounts()

        positions = [position for account in accounts for position in account.positions]
        self.assertTrue(all(position.mark_status == "live" for position in positions))
        self.assertTrue(all(position.mark_price is not None for position in positions))
        self.assertTrue(all(position.unrealized_pnl is not None for position in positions))
        self.assertNotEqual(unrealized_pnl, 0)

    def test_paper_scan_returns_confirmed_risk_approved_candidates_without_opening_trades(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        first_timestamp = (now_ms // 60_000 - 9) * 60_000
        prices = [100, 100, 100, 100, 80, 80, 80, 110]
        candles = [Candle(first_timestamp + index * 60_000, price - .5, price + 1, price - 1, price, 100) for index, price in enumerate(prices)]
        client = FakeMarketClient(candles, sample_ticker())
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))

        with patch.object(server, "MARKET_CLIENTS", {"bybit": client}), patch.object(server, "PAPER_SESSION", session):
            http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
            thread = Thread(target=http_server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{http_server.server_port}/api/paper/scan?exchange=bybit&interval=1&symbols=SOLUSDT") as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual([item["symbol"] for item in payload["candidates"]], ["SOLUSDT"])
                    self.assertEqual(payload["candidates"][0]["signal"], "long")
                    self.assertEqual(session.orders, [])
                    self.assertEqual(session.positions, [])
            finally:
                http_server.shutdown()
                http_server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
