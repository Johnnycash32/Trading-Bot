from __future__ import annotations

import json
import unittest
from threading import Barrier, Event
from typing import Callable, Iterator
from urllib.request import Request

import websocket

from trader_sources.base import TraderEvent
from trader_sources.hyperliquid_source import HyperliquidSource


ADDRESS = "0x" + "1" * 40


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.exit_args: tuple[object, object, object] | None = None

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.exit_args = (exc_type, exc_value, traceback)
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class HyperliquidSourceTests(unittest.TestCase):
    def test_discovery_sorts_monthly_pnl_and_rejects_invalid_wallets(self) -> None:
        payload = {
            "leaderboardRows": [
                {
                    "ethAddress": ADDRESS,
                    "accountValue": "1000",
                    "windowPerformances": [["month", {"pnl": "10", "roi": "0.1", "vlm": "500"}]],
                },
                {
                    "ethAddress": "not-a-wallet",
                    "windowPerformances": [["month", {"pnl": "900"}]],
                },
                {
                    "ethAddress": "0x" + "2" * 40,
                    "windowPerformances": [["month", {"pnl": "20"}]],
                },
            ]
        }
        def http_open(request: Request, *, timeout: float) -> FakeResponse:
            self.assertEqual(request.full_url, "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
            self.assertGreater(timeout, 0)
            return FakeResponse(payload)

        source = HyperliquidSource(http_open=http_open)

        traders = source.discover_traders(limit=20)

        self.assertEqual([trader["month_pnl"] for trader in traders], [20.0, 10.0])
        self.assertEqual(traders[0]["source_trader_id"], "0x" + "2" * 40)
        self.assertEqual(traders[1]["month_roi"], 0.1)
        self.assertEqual(traders[1]["discovery_source"], "public_leaderboard_undocumented")

    def test_fill_normalization_infers_position_transitions_from_start_position(self) -> None:
        cases = [
            ("open", 0.0, "B", 2.0, "position_opened", "long"),
            ("increase", 2.0, "B", 1.0, "position_increased", "long"),
            ("reduce", 2.0, "A", 1.0, "position_reduced", "long"),
            ("close", 2.0, "A", 2.0, "position_closed", "long"),
            ("reverse", 2.0, "A", 3.0, "position_reversed", "short"),
        ]
        for trade_id, before, side, quantity, event_type, direction in cases:
            with self.subTest(event_type=event_type):
                resulting_position = before + quantity if side == "B" else before - quantity
                fill = {
                    "coin": "BTC",
                    "side": side,
                    "time": 1_700_000_000_000,
                    "px": "50000",
                    "sz": str(quantity),
                    "tid": trade_id,
                    "startPosition": str(before),
                }
                state = {
                    "time": 1_700_000_001_000,
                    "assetPositions": [
                        {"position": {"coin": "BTC", "szi": str(resulting_position)}}
                    ],
                }

                event = HyperliquidSource.normalize_fill(
                    ADDRESS,
                    fill,
                    state,
                    state_checked_at_ms=1_700_000_001_000,
                    delivery_source="websocket",
                )

                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.direction, direction)
                self.assertEqual(event.source_trader_id, ADDRESS)
                self.assertEqual(event.exchange, "Hyperliquid")
                self.assertTrue(event.source_verified)
                self.assertEqual(event.quantity, quantity)

    def test_fill_without_start_position_is_not_given_an_invented_transition(self) -> None:
        fill = {
            "coin": "BTC",
            "side": "B",
            "time": 1_700_000_000_000,
            "px": "50000",
            "sz": "1",
            "tid": 99,
        }
        state = {"time": 1_700_000_001_000, "assetPositions": []}

        with self.assertRaisesRegex(ValueError, "startPosition"):
            HyperliquidSource.normalize_fill(
                ADDRESS,
                fill,
                state,
                state_checked_at_ms=1_700_000_001_000,
                delivery_source="websocket",
            )

    def test_rest_backfill_and_websocket_snapshot_deduplicate_same_fill(self) -> None:
        fill = {
            "coin": "BTC",
            "side": "B",
            "time": 1_700_000_000_000,
            "px": "50000",
            "sz": "1",
            "tid": 123,
            "startPosition": "0",
        }
        message = json.dumps(
            {
                "channel": "userFills",
                "isSnapshot": True,
                "data": {"user": ADDRESS, "fills": [fill]},
            }
        )

        class FakeWebSocket:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.recv_count = 0
                self.closed = False
                self.timeout: float | None = None

            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout
                return None

            def send(self, payload: str) -> None:
                self.sent.append(payload)

            def recv(self) -> str:
                self.recv_count += 1
                if self.recv_count == 1:
                    return message
                stop_event.set()
                raise websocket.WebSocketTimeoutException()

            def close(self) -> None:
                self.closed = True

        stop_event = Event()
        connection = FakeWebSocket()

        class TestSource(HyperliquidSource):
            def fills_by_time(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
                self_test.assertEqual(address, ADDRESS)
                self_test.assertLessEqual(start_ms, end_ms)
                return [fill]

            def clearinghouse_state(self, address: str) -> dict[str, object]:
                self_test.assertEqual(address, ADDRESS)
                return {
                    "time": 1_700_000_001_000,
                    "assetPositions": [{"position": {"coin": "BTC", "szi": "1"}}],
                }

        self_test = self

        def connect(url: str, *, timeout: float) -> FakeWebSocket:
            self.assertEqual(url, "wss://api.hyperliquid.xyz/ws")
            self.assertGreater(timeout, 0)
            return connection

        source = TestSource(websocket_connect=connect)
        status_updates: list[dict[str, object]] = []

        events = list(source.stream_events([ADDRESS], stop_event, status_updates.append))

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].source_verified)
        self.assertEqual(events[0].delivery_source, "rest_backfill")
        self.assertEqual(
            json.loads(connection.sent[0]),
            {
                "method": "subscribe",
                "subscription": {"type": "userFills", "user": ADDRESS},
            },
        )
        self.assertTrue(connection.closed)
        self.assertEqual(connection.timeout, 1)
        self.assertTrue(
            any(
                update.get("last_backfill_websocket_duplicate_id")
                == f"{ADDRESS}:1700000000000:BTC:123"
                for update in status_updates
            )
        )

    def test_new_websocket_fill_is_labeled_realtime_not_snapshot(self) -> None:
        fill = {
            "coin": "ETH",
            "side": "A",
            "time": 1_700_000_000_000,
            "px": "3000",
            "sz": "0.5",
            "tid": 456,
            "startPosition": "0",
        }
        message = json.dumps(
            {"channel": "userFills", "data": {"user": ADDRESS, "fills": [fill]}}
        )
        stop_event = Event()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.received = False
                self.sent: str | None = None
                self.timeout: float | None = None

            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout
                return None

            def send(self, payload: str) -> None:
                self.sent = payload
                return None

            def recv(self) -> str:
                if not self.received:
                    self.received = True
                    return message
                stop_event.set()
                raise websocket.WebSocketTimeoutException()

            def close(self) -> None:
                return None

        class TestSource(HyperliquidSource):
            def fills_by_time(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
                self_test.assertEqual(address, ADDRESS)
                self_test.assertLessEqual(start_ms, end_ms)
                return []

            def clearinghouse_state(self, address: str) -> dict[str, object]:
                self_test.assertEqual(address, ADDRESS)
                return {"time": 1_700_000_001_000, "assetPositions": []}

        self_test = self
        connection = FakeWebSocket()

        def connect(url: str, *, timeout: float) -> FakeWebSocket:
            self.assertEqual(url, "wss://api.hyperliquid.xyz/ws")
            self.assertGreater(timeout, 0)
            return connection

        events = list(TestSource(websocket_connect=connect).stream_events([ADDRESS], stop_event))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].delivery_source, "websocket")
        self.assertEqual(events[0].event_type, "position_opened")

    def test_discovery_top_twenty_is_split_with_hyperliquid_fifteen_user_limit(self) -> None:
        wallets = [f"0x{index:040x}" for index in range(1, 17)]
        batches: list[list[str]] = []
        barrier = Barrier(2)
        stop_event = Event()

        class TestSource(HyperliquidSource):
            def _stream_batch(
                self,
                addresses: list[str],
                stop_event: Event,
                status_callback: Callable[[dict[str, object]], None] | None,
            ) -> Iterator[TraderEvent]:
                batches.append(addresses)
                if status_callback is not None:
                    status_callback({"connected": True})
                barrier.wait(timeout=2)
                while not stop_event.is_set():
                    stop_event.wait(0.01)
                yield from ()

        def on_status(update: dict[str, object]) -> None:
            if update.get("connected_groups") == 2:
                stop_event.set()

        events = list(TestSource().stream_events(wallets, stop_event, on_status))

        self.assertEqual(events, [])
        self.assertEqual(sorted(len(batch) for batch in batches), [1, 15])
        self.assertEqual({address for batch in batches for address in batch}, set(wallets))


if __name__ == "__main__":
    unittest.main()
