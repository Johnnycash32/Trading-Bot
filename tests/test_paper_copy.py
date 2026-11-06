from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from trader_sources.base import TraderEvent
from trading_bot import server
from trading_bot.market_data import Candle, Ticker
from trading_bot.models import RiskLimits
from trading_bot.paper_copy import (
    PaperCopySettings,
    candidate_rejection,
    destination_symbol,
    load_settings,
    save_settings,
)
from trading_bot.paper_session import PaperExecutionSession
from trading_bot.risk import RiskEngine


WALLET = "0x" + "1" * 40


def current_time_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def opened_event(**updates: object) -> TraderEvent:
    timestamp = current_time_ms()
    values: dict[str, object] = {
        "source_trader_id": WALLET,
        "exchange": "Hyperliquid",
        "delivery_source": "websocket",
        "instrument": "BTC",
        "direction": "long",
        "price": 50_000.0,
        "quantity": 0.02,
        "timestamp": datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).isoformat(),
        "event_id": f"{WALLET}:{timestamp}:BTC:1",
        "event_type": "position_opened",
        "source_verified": True,
        "timestamp_ms": timestamp,
        "state_timestamp_ms": timestamp + 100,
        "state_checked_at_ms": timestamp,
        "state_position_size": 0.02,
    }
    values.update(updates)
    return TraderEvent(**values)


class PaperCopySettingsTests(unittest.TestCase):
    def test_settings_validate_wallets_brackets_and_leverage(self) -> None:
        settings = PaperCopySettings.from_payload(
            {
                "selected_wallets": [WALLET.upper().replace("0X", "0x")],
                "stop_loss_percent": 2,
                "take_profit_percent": 4.5,
                "leverage": 1,
            }
        )

        self.assertEqual(settings.selected_wallets, (WALLET,))
        self.assertEqual(settings.as_dict()["leverage"], 1.0)
        for update, message in (
            ({"selected_wallets": ["not-a-wallet"]}, "wallet"),
            ({"stop_loss_percent": 0}, "Stop-loss"),
            ({"take_profit_percent": 4.4}, "twice the stop"),
            ({"leverage": 3}, "between 1x and 2x"),
        ):
            payload = {
                "selected_wallets": [WALLET],
                "stop_loss_percent": 2,
                "take_profit_percent": 4.5,
                "leverage": 1,
                **update,
            }
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                PaperCopySettings.from_payload(payload)

    def test_settings_round_trip_processed_event_ids(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "copy.json"
            settings = PaperCopySettings((WALLET,), 2, 4.5, 1, ("event-1",))

            save_settings(path, settings)
            restored = load_settings(path)
            saved_ids = json.loads(path.read_text(encoding="utf-8"))["processed_event_ids"]

        self.assertEqual(restored, settings)
        self.assertEqual(saved_ids, ["event-1"])

    def test_candidate_accepts_only_fresh_verified_selected_position_opens(self) -> None:
        settings = PaperCopySettings((WALLET,))
        event = opened_event()

        self.assertIsNone(candidate_rejection(event, settings, {WALLET}, now_ms=current_time_ms()))
        cases = (
            ({"source_verified": False}, "not source-verified"),
            ({"delivery_source": "rest_backfill"}, "Only new WebSocket"),
            ({"event_type": "position_increased"}, "Only new position opens"),
            ({"source_trader_id": "0x" + "2" * 40}, "not selected"),
            ({"timestamp_ms": current_time_ms() - 10_000}, "stale"),
            ({"state_position_size": -0.02}, "does not confirm"),
        )
        for updates, message in cases:
            with self.subTest(message=message):
                rejection = candidate_rejection(
                    opened_event(**updates),
                    settings,
                    {WALLET},
                    now_ms=current_time_ms(),
                )
                self.assertIn(message, rejection or "")

    def test_only_simple_usdt_perpetual_instruments_are_mapped(self) -> None:
        self.assertEqual(destination_symbol("BTC"), "BTCUSDT")
        for instrument in ("xyz:TSLA", "BTC/USD", ""):
            with self.subTest(instrument=instrument), self.assertRaises(ValueError):
                destination_symbol(instrument)


class FakeTickerClient:
    def __init__(self, exchange: str, *, timestamp_ms: int | None = None, offset: float = 0) -> None:
        self.exchange = exchange
        self.timestamp_ms = timestamp_ms or current_time_ms()
        self.offset = offset

    def ticker(self, symbol: str) -> Ticker:
        return Ticker(
            self.exchange,
            symbol,
            50_000 + self.offset,
            49_999 + self.offset,
            50_001 + self.offset,
            0,
            self.timestamp_ms,
        )


class FakeCandleClient(FakeTickerClient):
    def __init__(self, exchange: str, candles: list[Candle]) -> None:
        super().__init__(exchange)
        self._candles = candles

    def candles(self, symbol: str, interval: str, limit: int, end_ms: int | None) -> list[Candle]:
        return self._candles


class PaperCopyExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.session = PaperExecutionSession()
        self.settings = PaperCopySettings((WALLET,))
        self.monitor = type(
            "FakeMonitor",
            (),
            {
                "snapshot": lambda _: {
                    "traders": [{"source_trader_id": WALLET}],
                    "events": [],
                    "monitor": {"connected": True},
                }
            },
        )()
        self.patches = [
            patch.object(server, "PAPER_SESSION", self.session),
            patch.object(server, "PAPER_STATE_FILE", Path(self.temporary_directory.name) / "paper-state.json"),
            patch.object(server, "PAPER_COPY_SETTINGS_FILE", Path(self.temporary_directory.name) / "copy-settings.json"),
            patch.object(server, "COPY_SETTINGS", self.settings),
            patch.object(server, "COPY_STATUS", {}),
            patch.object(server, "COPY_CONTROL", {"auto_trade_enabled": True, "close_request": False, "last_action": ""}),
            patch.object(server, "HYPERLIQUID_MONITOR", self.monitor),
            patch.object(server, "MARKET_CLIENTS", {
                "bybit": FakeTickerClient("bybit"),
                "binance": FakeTickerClient("binance", offset=1),
            }),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_verified_live_open_uses_fresh_quote_risk_sizing_and_deduplication(self) -> None:
        event = opened_event()

        server.process_paper_copy_event(event)
        server.process_paper_copy_event(event)

        self.assertEqual(len(self.session.orders), 1)
        order = self.session.orders[0]
        self.assertEqual(order["signal_source"], "verified_hyperliquid_paper_copy")
        self.assertEqual(order["source_event_id"], event.event_id)
        self.assertEqual(order["source_trader_id"], WALLET)
        self.assertEqual(order["entry_quote_source"], "bybit_ask")
        self.assertAlmostEqual(
            (order["entry_price"] - order["stop_loss"]) / order["entry_price"] * 100,
            2,
        )
        self.assertAlmostEqual(
            (order["take_profit"] - order["entry_price"]) / order["entry_price"] * 100,
            4.5,
        )
        self.assertLessEqual(order["maximum_loss"], self.session.account_equity * 0.005 + 1e-8)
        self.assertEqual(
            self.session.positions[0]["opened_candle_timestamp_ms"],
            event.timestamp_ms - event.timestamp_ms % server.COPY_POSITION_MONITOR_INTERVAL_MS,
        )
        self.assertEqual(server.COPY_STATUS["last_result"], "opened")

    def test_server_monitor_closes_copied_positions_using_one_minute_brackets(self) -> None:
        now_ms = current_time_ms()
        current_minute = now_ms - now_ms % server.COPY_POSITION_MONITOR_INTERVAL_MS
        last_evaluated = current_minute - 2 * server.COPY_POSITION_MONITOR_INTERVAL_MS
        self.session.submit_risk_sized_paper_copy(
            "bybit",
            "BTCUSDT",
            "long",
            50_000,
            2,
            4.5,
            1,
            last_evaluated,
            source_event_id="verified-event",
            source_trader_id=WALLET,
        )
        candles = [
            Candle(last_evaluated, 50_000, 50_100, 49_900, 50_000, 10),
            Candle(last_evaluated + server.COPY_POSITION_MONITOR_INTERVAL_MS, 50_000, 50_100, 48_900, 49_000, 10),
        ]
        clients = {
            "bybit": FakeCandleClient("bybit", candles),
            "binance": FakeTickerClient("binance"),
        }

        with patch.object(server, "MARKET_CLIENTS", clients):
            server.monitor_open_paper_copies_once()

        self.assertEqual(self.session.positions[0]["status"], "closed")
        self.assertEqual(self.session.orders[0]["status"], "closed")
        self.assertEqual(self.session.history[0]["exit_reason"], "stop_loss")
        self.assertTrue(Path(server.PAPER_STATE_FILE).exists())
        self.assertEqual(server.COPY_STATUS["monitoring_warnings"], {})

    def test_server_monitor_surfaces_candle_feed_failures(self) -> None:
        timestamp = current_time_ms() - current_time_ms() % server.COPY_POSITION_MONITOR_INTERVAL_MS
        self.session.submit_risk_sized_paper_copy(
            "bybit",
            "BTCUSDT",
            "long",
            50_000,
            2,
            4.5,
            1,
            timestamp,
            source_event_id="verified-event",
            source_trader_id=WALLET,
        )

        class UnavailableCandleClient(FakeTickerClient):
            def candles(self, symbol: str, interval: str, limit: int, end_ms: int | None) -> list[Candle]:
                raise RuntimeError("market feed unavailable")

        with patch.object(server, "MARKET_CLIENTS", {
            "bybit": UnavailableCandleClient("bybit"),
            "binance": FakeTickerClient("binance"),
        }):
            server.monitor_open_paper_copies_once()

        self.assertIn("bybit:BTCUSDT", server.COPY_STATUS["monitoring_warnings"])
        self.assertIn("market feed unavailable", server.COPY_STATUS["monitoring_warnings"]["bybit:BTCUSDT"])
        self.assertEqual(self.session.positions[0]["status"], "open")

    def test_disabled_or_stale_copy_signals_never_open_positions(self) -> None:
        server.COPY_CONTROL["auto_trade_enabled"] = False
        server.process_paper_copy_event(opened_event())
        self.assertEqual(self.session.orders, [])

        server.COPY_CONTROL["auto_trade_enabled"] = True
        server.process_paper_copy_event(opened_event(timestamp_ms=current_time_ms() - 10_000))
        self.assertEqual(self.session.orders, [])
        self.assertEqual(server.COPY_STATUS["last_result"], "skipped")

    def test_risk_engine_position_cap_rejects_copy(self) -> None:
        self.session.risk_engine = RiskEngine(RiskLimits(max_open_positions=0))

        server.process_paper_copy_event(opened_event())

        self.assertEqual(self.session.orders, [])
        self.assertEqual(server.COPY_STATUS["last_result"], "skipped")
        self.assertTrue(any(event["type"] == "paper_rejected" for event in self.session.events))

    def test_entry_slippage_above_cap_is_skipped(self) -> None:
        clients = {
            "bybit": FakeTickerClient("bybit", offset=1_000),
            "binance": FakeTickerClient("binance", offset=1_000),
        }
        with patch.object(server, "MARKET_CLIENTS", clients):
            server.process_paper_copy_event(opened_event())

        self.assertEqual(self.session.orders, [])
        self.assertIn("moved", str(server.COPY_STATUS["last_message"]))


class PaperCopyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.monitor = type(
            "FakeMonitor",
            (),
            {
                "connected": True,
                "snapshot": lambda instance: {
                    "traders": [{"source_trader_id": WALLET}],
                    "events": [],
                    "monitor": {"connected": instance.connected},
                },
            },
        )()
        self.patches = [
            patch.object(server, "HYPERLIQUID_MONITOR", self.monitor),
            patch.object(server, "COPY_SETTINGS", PaperCopySettings()),
            patch.object(server, "COPY_CONTROL", {"auto_trade_enabled": False, "close_request": False, "last_action": ""}),
            patch.object(server, "PAPER_COPY_SETTINGS_FILE", Path(self.temporary_directory.name) / "copy-settings.json"),
            patch.object(server, "PAPER_SESSION", PaperExecutionSession()),
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

    def test_settings_endpoint_saves_wallet_selection_and_custom_brackets(self) -> None:
        status, response = self.post_json(
            "/api/copy/settings",
            {
                "selected_wallets": [WALLET],
                "stop_loss_percent": 1.5,
                "take_profit_percent": 3.5,
                "leverage": 1.5,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["settings"]["selected_wallets"], [WALLET])
        self.assertEqual(response["settings"]["stop_loss_percent"], 1.5)
        self.assertTrue(Path(server.PAPER_COPY_SETTINGS_FILE).exists())
        self.assertFalse(response["control"]["auto_trade_enabled"])

    def test_control_requires_saved_wallet_selection_and_connected_stream(self) -> None:
        status, response = self.post_json("/api/copy/control", {"enabled": True})
        self.assertEqual(status, 409)
        self.assertIn("Select at least one", response["error"])

        status, _ = self.post_json(
            "/api/copy/settings",
            {
                "selected_wallets": [WALLET],
                "stop_loss_percent": 2,
                "take_profit_percent": 4.5,
                "leverage": 1,
            },
        )
        self.assertEqual(status, 200)
        self.monitor.connected = False
        status, response = self.post_json("/api/copy/control", {"enabled": True})
        self.assertEqual(status, 409)
        self.assertIn("fill stream to connect", response["error"])

        self.monitor.connected = True
        status, response = self.post_json("/api/copy/control", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(response["control"]["auto_trade_enabled"])

    def test_control_request_schema_remains_enabled_boolean(self) -> None:
        status, _ = self.post_json(
            "/api/copy/settings",
            {
                "selected_wallets": [WALLET],
                "stop_loss_percent": 2,
                "take_profit_percent": 4.5,
                "leverage": 1,
            },
        )
        self.assertEqual(status, 200)

        status, response = self.post_json("/api/copy/control", {"enabled": False})

        self.assertEqual(status, 200)
        self.assertFalse(response["control"]["auto_trade_enabled"])
