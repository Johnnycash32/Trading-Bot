import unittest

from trading_bot.private_accounts import BybitReadOnlyAccount, PrivateAccountConfig, private_account_snapshot


class PrivateAccountTests(unittest.TestCase):
    def test_read_only_private_position_is_lion_approved_for_paper_copy(self) -> None:
        def loader(url, _headers):
            if "/v5/user/query-api" in url:
                return {"retCode": 0, "result": {"readOnly": 1}}
            if "/v5/position/list" in url:
                return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.01", "avgPrice": "80000", "markPrice": "80100", "unrealisedPnl": "1", "stopLoss": "79000", "takeProfit": "82000"}]}}
            return {"retCode": 0, "result": {"list": [{"totalEquity": "10000"}]}}

        snapshot = BybitReadOnlyAccount(PrivateAccountConfig("key", "secret"), loader).snapshot()
        self.assertTrue(snapshot["read_only"])
        self.assertTrue(snapshot["positions"][0]["lion_approved"])
        self.assertEqual(snapshot["positions"][0]["symbol"], "BTCUSDT")

    def test_non_read_only_key_is_rejected(self) -> None:
        def loader(url, _headers):
            return {"retCode": 0, "result": {"readOnly": 0}}

        with self.assertRaises(PermissionError):
            BybitReadOnlyAccount(PrivateAccountConfig("key", "secret"), loader).snapshot()

    def test_missing_credentials_disables_private_monitoring(self) -> None:
        snapshot = private_account_snapshot()
        self.assertFalse(snapshot["configured"])
        self.assertEqual(snapshot["positions"], [])


if __name__ == "__main__":
    unittest.main()
