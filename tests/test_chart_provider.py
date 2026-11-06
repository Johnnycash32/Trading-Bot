import unittest
from unittest.mock import patch

from trader_sources.chart_provider import ChartProvider, REST_PRIORITY


class ChartProviderTests(unittest.TestCase):
    def test_rest_failover_uses_priority_and_normalizes_kraken_ohlc(self) -> None:
        urls: list[str] = []

        def loader(url: str) -> object:
            urls.append(url)
            if "api.binance.com" in url:
                raise OSError("Binance blocked for failover test")
            return {
                "error": [],
                "result": {
                    "XXBTZUSD": [[1_700_000_000, "100", "105", "95", "103", "101", "42", 7]],
                    "last": 1_700_000_060,
                },
            }

        provider = ChartProvider(loader)

        source, candles, errors = provider.candles("BTCUSDT", "1")

        self.assertEqual(source, "kraken")
        self.assertEqual(len(candles), 1)
        self.assertEqual(
            (candles[0].time, candles[0].open, candles[0].high, candles[0].low, candles[0].close, candles[0].volume),
            (1_700_000_000_000, 100, 105, 95, 103, 42),
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("api.binance.com/api/v3/klines", urls[0])
        self.assertIn("api.kraken.com/0/public/OHLC", urls[1])

    def test_provider_order_is_explicit_and_unmodified(self) -> None:
        self.assertEqual(REST_PRIORITY, ("binance", "kraken", "okx", "bybit", "bitget", "hyperliquid"))

    def test_two_failed_providers_fall_through_to_okx(self) -> None:
        urls: list[str] = []

        def loader(url: str) -> object:
            urls.append(url)
            if "binance.com" in url or "kraken.com" in url:
                raise OSError("intentionally unavailable in failover test")
            return {
                "code": "0",
                "data": [["1700000000000", "100", "105", "95", "103", "42", "0", "0", "1"]],
            }

        provider = ChartProvider(loader)

        with self.assertLogs("trader_sources.chart_provider", level="WARNING") as logs:
            source, candles, errors = provider.candles("BTCUSDT", "1")

        self.assertEqual(source, "okx")
        self.assertEqual(candles[0].close, 103)
        self.assertEqual(len(errors), 2)
        self.assertIn("provider binance failed", logs.output[0])
        self.assertIn("provider kraken failed", logs.output[1])
        self.assertIn("api/v5/market/candles", urls[2])

    def test_preferred_live_source_is_tried_before_later_fallbacks(self) -> None:
        provider = ChartProvider(lambda _url: [])
        calls: list[str] = []

        def binance(*_args: object) -> list[object]:
            calls.append("binance")
            raise OSError("offline")

        def kraken(*_args: object) -> list[object]:
            calls.append("kraken")
            raise OSError("offline")

        def okx(*_args: object) -> list[object]:
            calls.append("okx")
            return [object()]

        with (
            patch.object(provider, "_binance", binance),
            patch.object(provider, "_kraken", kraken),
            patch.object(provider, "_okx", okx),
        ):
            source, candles, errors = provider.candles("BTCUSDT", preferred_source="okx")

        self.assertEqual(source, "okx")
        self.assertEqual(len(candles), 1)
        self.assertEqual(calls, ["okx"])
        self.assertEqual(errors, [])

    def test_invalid_symbols_intervals_and_sources_are_rejected(self) -> None:
        provider = ChartProvider(lambda _url: [])
        for kwargs in (
            {"symbol": "BTC/USDT"},
            {"symbol": "BTCUSDT", "interval": "invalid"},
            {"symbol": "BTCUSDT", "preferred_source": "unknown"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                provider.candles(**kwargs)


if __name__ == "__main__":
    unittest.main()
