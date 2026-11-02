import json
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from trading_bot import server
from trading_bot.broker_connections import BrokerRequestStore, BybitTestnetConfig


class BrokerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store_patch = patch.object(
            server,
            "BROKER_REQUESTS",
            BrokerRequestStore(Path(self.temporary.name) / "requests.json"),
        )
        self.store_patch.start()
        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.thread = Thread(target=self.http_server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.http_server.server_port}"

    def tearDown(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=2)
        self.store_patch.stop()
        self.temporary.cleanup()

    def post_json(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_any_named_broker_request_is_saved_locally(self) -> None:
        status, response = self.post_json(
            "/api/brokers/requests",
            {"broker_name": "A broker not in the list", "requested_mode": "testnet", "notes": "Futures subaccount"},
        )

        self.assertEqual(status, 201)
        self.assertEqual(response["request"]["status"], "Requested locally")
        with urlopen(f"{self.base_url}/api/brokers") as result:
            directory = json.loads(result.read())
        self.assertEqual(directory["requests"][0]["broker_name"], "A broker not in the list")
        self.assertNotIn("api_key", json.dumps(directory))

    def test_live_request_is_a_support_request_not_a_live_connection(self) -> None:
        status, response = self.post_json(
            "/api/brokers/requests",
            {"broker_name": "Future Broker", "requested_mode": "live", "notes": ""},
        )

        self.assertEqual(status, 201)
        self.assertEqual(response["request"]["requested_mode"], "live")
        self.assertEqual(response["request"]["status"], "Requested locally")

    def test_invalid_request_mode_is_rejected(self) -> None:
        status, response = self.post_json(
            "/api/brokers/requests",
            {"broker_name": "Broker", "requested_mode": "anything-goes", "notes": ""},
        )

        self.assertEqual(status, 400)
        self.assertIn("Choose read-only", response["error"])

    def test_testnet_check_requires_server_side_credentials(self) -> None:
        with patch.object(BybitTestnetConfig, "from_environment", return_value=None):
            status, response = self.post_json("/api/brokers/bybit-testnet/check", {})

        self.assertEqual(status, 409)
        self.assertIn("dedicated Bybit testnet credentials", response["error"])

    def test_testnet_order_endpoint_uses_server_credentials(self) -> None:
        class FakeAccount:
            def __init__(self, _config):
                pass

            def place_market_order(self, symbol, side, quantity):
                return {
                    "environment": "TESTNET",
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "order_id": "test-order-id",
                }

        with (
            patch.object(BybitTestnetConfig, "from_environment", return_value=BybitTestnetConfig("server-key", "server-secret")),
            patch.object(server, "BybitTestnetAccount", FakeAccount),
        ):
            status, response = self.post_json(
                "/api/brokers/bybit-testnet/order",
                {"symbol": "SOLUSDT", "side": "Buy", "quantity": "0.01", "confirmed": True},
            )

        self.assertEqual(status, 200)
        self.assertEqual(response["order"]["environment"], "TESTNET")
        self.assertEqual(response["order"]["order_id"], "test-order-id")
        self.assertNotIn("server-secret", json.dumps(response))

    def test_testnet_order_requires_explicit_confirmation(self) -> None:
        status, response = self.post_json(
            "/api/brokers/bybit-testnet/order",
            {"symbol": "SOLUSDT", "side": "Buy", "quantity": "0.01", "confirmed": False},
        )

        self.assertEqual(status, 400)
        self.assertIn("Explicitly confirm", response["error"])

    def test_order_endpoint_never_accepts_client_supplied_credentials(self) -> None:
        status, response = self.post_json(
            "/api/brokers/bybit-testnet/order",
            {"api_key": "must-not-be-used", "symbol": "SOLUSDT", "side": "Buy", "quantity": "0.01", "confirmed": True},
        )

        self.assertEqual(status, 400)
        self.assertIn("credentials must remain on the server", response["error"])


if __name__ == "__main__":
    unittest.main()
