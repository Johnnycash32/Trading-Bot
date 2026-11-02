import json
import tempfile
import unittest
from pathlib import Path

from trading_bot.broker_connections import (
    BROKER_DIRECTORY,
    BYBIT_TESTNET_URL,
    BrokerRequestStore,
    BybitTestnetAccount,
    BybitTestnetConfig,
)


class BrokerRequestStoreTests(unittest.TestCase):
    def test_request_is_saved_locally_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "broker-requests.json"
            request = BrokerRequestStore(path).add_request(
                "My Regional Broker",
                "testnet",
                "Please support my subaccount.",
            )

            reloaded = BrokerRequestStore(path).list_requests()

        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0], request)
        self.assertEqual(request["status"], "Requested locally")
        self.assertEqual(request["broker_name"], "My Regional Broker")

    def test_request_validates_mode_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BrokerRequestStore(Path(directory) / "requests.json")
            with self.assertRaisesRegex(ValueError, "Choose read-only"):
                store.add_request("Broker", "unrestricted")
            with self.assertRaisesRegex(ValueError, "500 characters"):
                store.add_request("Broker", "read-only", "x" * 501)

    def test_directory_contains_screenshot_brokers_and_request_entry(self) -> None:
        broker_names = {item["name"] for item in BROKER_DIRECTORY}
        expected = {
            "FxPro", "Bybit", "easyMarkets", "Trade Nation", "Deriv", "ThinkMarkets",
            "Pepperstone", "Fusion Markets", "Capital.com", "NinjaTrader", "Tickmill",
            "IC Markets", "WhiteBIT", "ActivTrades", "Kraken", "BingX", "CMC Markets",
            "Gate", "Binance", "P2PRIME", "Crypto.com Exchange", "CoinW", "FP Markets",
        }
        self.assertTrue(expected.issubset(broker_names))
        self.assertEqual(next(item for item in BROKER_DIRECTORY if item["id"] == "bybit")["connection"], "testnet")


class BybitTestnetAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = BybitTestnetConfig("test-key", "test-secret", "My test subaccount")

    def test_snapshot_uses_testnet_and_returns_wallet_without_credentials(self) -> None:
        calls = []

        def loader(url, headers):
            calls.append((url, headers))
            if "/v5/user/query-api" in url:
                return {"retCode": 0, "result": {"readOnly": 0}}
            return {"retCode": 0, "result": {"list": [{"totalEquity": "250.5"}]}}

        snapshot = BybitTestnetAccount(self.config, loader=loader).snapshot()

        self.assertTrue(all(url.startswith(BYBIT_TESTNET_URL) for url, _ in calls))
        self.assertEqual(snapshot["environment"], "TESTNET")
        self.assertEqual(snapshot["account_label"], "My test subaccount")
        self.assertEqual(snapshot["equity_usdt"], 250.5)
        self.assertTrue(snapshot["order_submission_enabled"])
        self.assertNotIn("api_key", snapshot)
        self.assertNotIn("test-key", json.dumps(snapshot))

    def test_read_only_key_cannot_submit_testnet_order(self) -> None:
        calls = []

        def loader(url, _headers):
            calls.append(url)
            return {"retCode": 0, "result": {"readOnly": 1}}

        def poster(*_args):
            self.fail("read-only API key must not reach the order endpoint")

        account = BybitTestnetAccount(self.config, loader=loader, poster=poster)
        with self.assertRaisesRegex(PermissionError, "read-only"):
            account.place_market_order("SOLUSDT", "buy", "0.01")
        self.assertEqual(len(calls), 1)
        self.assertIn(BYBIT_TESTNET_URL, calls[0])

    def test_missing_permission_metadata_does_not_enable_testnet_orders(self) -> None:
        def loader(url, _headers):
            if "/v5/user/query-api" in url:
                return {"retCode": 0, "result": {}}
            return {"retCode": 0, "result": {"list": [{"totalEquity": "250.5"}]}}

        snapshot = BybitTestnetAccount(self.config, loader=loader).snapshot()

        self.assertTrue(snapshot["api_key_read_only"])
        self.assertFalse(snapshot["order_submission_enabled"])

    def test_market_order_is_signed_and_sent_only_to_bybit_testnet(self) -> None:
        calls = []

        def loader(url, _headers):
            self.assertTrue(url.startswith(BYBIT_TESTNET_URL))
            return {"retCode": 0, "result": {"readOnly": 0}}

        def poster(url, headers, body):
            calls.append((url, headers, json.loads(body)))
            return {"retCode": 0, "result": {"orderId": "test-order-id"}}

        result = BybitTestnetAccount(self.config, loader=loader, poster=poster).place_market_order(
            "solusdt",
            "buy",
            "0.01",
        )

        self.assertEqual(result["environment"], "TESTNET")
        self.assertEqual(result["order_id"], "test-order-id")
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertEqual(url, f"{BYBIT_TESTNET_URL}/v5/order/create")
        self.assertEqual(body["category"], "linear")
        self.assertEqual(body["symbol"], "SOLUSDT")
        self.assertEqual(body["side"], "Buy")
        self.assertEqual(body["qty"], "0.01")
        self.assertTrue(body["orderLinkId"].startswith("paperbot-test-"))
        self.assertIn("X-BAPI-SIGN", headers)

    def test_invalid_market_order_inputs_are_rejected(self) -> None:
        account = BybitTestnetAccount(self.config)
        for symbol, side, quantity in (
            ("SOLUSDT;DROP", "buy", "0.01"),
            ("SOLUSDT", "hold", "0.01"),
            ("SOLUSDT", "buy", "-1"),
            ("SOLUSDT", "buy", "NaN"),
            ("SOLUSDT", "buy", "1e10"),
        ):
            with self.subTest(symbol=symbol, side=side, quantity=quantity):
                with self.assertRaises(ValueError):
                    account.place_market_order(symbol, side, quantity)


if __name__ == "__main__":
    unittest.main()
