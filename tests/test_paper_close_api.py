from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from trading_bot import server
from trading_bot.market_data import Candle, Ticker
from trading_bot.paper_session import PaperExecutionSession
from trading_bot.strategy import MovingAverageCrossStrategy


def current_time_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class FakeMarketClient:
    def __init__(self, quote: Ticker | Exception) -> None:
        self.quote = quote

    def ticker(self, symbol: str) -> Ticker:
        if isinstance(self.quote, Exception):
            raise self.quote
        return Ticker(
            self.quote.exchange,
            symbol,
            self.quote.last,
            self.quote.bid,
            self.quote.ask,
            self.quote.change_percent,
            self.quote.timestamp_ms,
        )


def quote(exchange: str, *, bid: float, ask: float, timestamp_ms: int | None = None) -> Ticker:
    return Ticker(exchange, "SOLUSDT", (bid + ask) / 2, bid, ask, 0.0, timestamp_ms or current_time_ms())


def create_position(session: PaperExecutionSession, symbol: str) -> str:
    values = [100, 100, 100, 100, 80, 80, 80, 110]
    market_candles = [
        Candle(index * 60_000, value - 0.5, value + 1, value - 1, value, 100)
        for index, value in enumerate(values)
    ]
    return str(session.evaluate("bybit", symbol, market_candles)["order"]["order_id"])


class PaperCloseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        self.patches = [
            patch.object(server, "PAPER_SESSION", self.session),
            patch.object(server, "PAPER_STATE_FILE", Path(self.temporary_directory.name) / "paper-state.json"),
            patch.object(server, "MARKET_CLIENTS", {
                "bybit": FakeMarketClient(quote("bybit", bid=109.9, ask=110.1)),
                "binance": FakeMarketClient(quote("binance", bid=109.8, ask=110.2)),
            }),
            patch.object(server, "COPY_CONTROL", {"auto_trade_enabled": True, "close_request": False, "last_action": ""}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.server_thread = Thread(target=self.http_server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self.base_url = f"http://127.0.0.1:{self.http_server.server_port}"

    def _stop_server(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.server_thread.join(timeout=2)

    def post_json(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()
        with response:
            return response.status, json.loads(response.read())

    def test_auto_copy_blocks_close_then_manual_close_works_when_disabled(self) -> None:
        order_id = create_position(self.session, "SOLUSDT")

        blocked_status, blocked_body = self.post_json("/api/paper/close", {"order_id": order_id})
        self.assertEqual(blocked_status, 409)
        self.assertIn("Turn off auto-copy", blocked_body["error"])
        self.assertEqual(len(self.session.snapshot()["open_positions"]), 1)

        toggle_status, _ = self.post_json("/api/copy/control", {"enabled": False})
        self.assertEqual(toggle_status, 200)
        close_status, close_body = self.post_json("/api/paper/close", {"order_id": order_id})

        self.assertEqual(close_status, 200)
        self.assertEqual(close_body["closed_positions"][0]["exit_reason"], "manual_close")
        self.assertEqual(close_body["closed_positions"][0]["exit_price"], 109.9)
        self.assertEqual(close_body["closed_positions"][0]["exit_price_source"], "bybit_bid")
        self.assertIsInstance(close_body["closed_positions"][0]["exit_quote_timestamp_ms"], int)
        self.assertEqual(close_body["state"]["open_positions"], [])

    def test_close_all_closes_every_position_when_auto_copy_is_off(self) -> None:
        create_position(self.session, "SOLUSDT")
        create_position(self.session, "BTCUSDT")
        self.post_json("/api/copy/control", {"enabled": False})

        status, body = self.post_json("/api/paper/close", {"all": True})

        self.assertEqual(status, 200)
        self.assertEqual(len(body["closed_positions"]), 2)
        self.assertEqual(body["state"]["open_positions"], [])

    def test_close_falls_back_to_fresh_binance_quote_when_bybit_fails(self) -> None:
        order_id = create_position(self.session, "SOLUSDT")
        server.MARKET_CLIENTS["bybit"] = FakeMarketClient(OSError("Bybit unavailable"))
        server.MARKET_CLIENTS["binance"] = FakeMarketClient(quote("binance", bid=108.75, ask=108.85))
        self.post_json("/api/copy/control", {"enabled": False})

        status, body = self.post_json("/api/paper/close", {"order_id": order_id})

        self.assertEqual(status, 200)
        self.assertEqual(body["closed_positions"][0]["exit_price"], 108.75)
        self.assertEqual(body["closed_positions"][0]["exit_price_source"], "binance_bid")

    def test_close_falls_back_when_bybit_quote_is_stale(self) -> None:
        order_id = create_position(self.session, "SOLUSDT")
        server.MARKET_CLIENTS["bybit"] = FakeMarketClient(
            quote("bybit", bid=109.9, ask=110.1, timestamp_ms=current_time_ms() - 10_000)
        )
        server.MARKET_CLIENTS["binance"] = FakeMarketClient(quote("binance", bid=108.75, ask=108.85))
        self.post_json("/api/copy/control", {"enabled": False})

        status, body = self.post_json("/api/paper/close", {"order_id": order_id})

        self.assertEqual(status, 200)
        self.assertEqual(body["closed_positions"][0]["exit_price_source"], "binance_bid")

    def test_stale_quotes_refuse_close_without_mutating_position(self) -> None:
        order_id = create_position(self.session, "SOLUSDT")
        prior_equity = self.session.account_equity
        stale_timestamp = current_time_ms() - 10_000
        server.MARKET_CLIENTS["bybit"] = FakeMarketClient(
            quote("bybit", bid=109.9, ask=110.1, timestamp_ms=stale_timestamp)
        )
        server.MARKET_CLIENTS["binance"] = FakeMarketClient(
            quote("binance", bid=109.8, ask=110.2, timestamp_ms=stale_timestamp)
        )
        self.post_json("/api/copy/control", {"enabled": False})

        status, body = self.post_json("/api/paper/close", {"order_id": order_id})

        self.assertEqual(status, 503)
        self.assertIn("No fresh executable close quote", body["error"])
        self.assertEqual(len(self.session.snapshot()["open_positions"]), 1)
        self.assertEqual(self.session.account_equity, prior_equity)
        self.assertEqual(self.session.snapshot()["history"], [])

    def test_close_all_is_atomic_when_a_later_quote_is_unavailable(self) -> None:
        create_position(self.session, "SOLUSDT")
        create_position(self.session, "BTCUSDT")
        self.post_json("/api/copy/control", {"enabled": False})
        with patch.object(server, "fresh_close_quote", side_effect=[
            (109.9, "bybit_bid", current_time_ms()),
            RuntimeError("BTCUSDT quote unavailable"),
        ]):
            status, body = self.post_json("/api/paper/close", {"all": True})

        self.assertEqual(status, 503)
        self.assertEqual(len(self.session.snapshot()["open_positions"]), 2)
        self.assertEqual(self.session.snapshot()["history"], [])
        self.assertIn("quote unavailable", body["error"])

    def test_short_position_uses_fresh_ask_for_close(self) -> None:
        price, source, timestamp_ms = server.fresh_close_quote({
            "order_id": "PAPER-SHORT",
            "exchange": "bybit",
            "symbol": "SOLUSDT",
            "side": "short",
        })

        self.assertEqual(price, 110.1)
        self.assertEqual(source, "bybit_ask")
        self.assertGreater(timestamp_ms, 0)


if __name__ == "__main__":
    unittest.main()
