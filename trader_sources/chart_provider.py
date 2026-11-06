from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOGGER = logging.getLogger(__name__)
REST_PRIORITY = ("binance", "kraken", "okx", "bybit", "bitget", "hyperliquid")
SUPPORTED_INTERVALS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "720": "12h",
    "D": "1d",
    "W": "1w",
    "M": "1M",
}
INTERVAL_MINUTES = {
    "1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60,
    "120": 120, "240": 240, "360": 360, "720": 720,
    "D": 1_440, "W": 10_080, "M": 43_200,
}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")


@dataclass(frozen=True)
class ChartCandle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    timestamp_ms: int

    def as_dict(self) -> dict[str, int | float | None]:
        return asdict(self)


def _read_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "AYO-paper-chart/1.0"})
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _number(value: object, field: str) -> float:
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"Invalid candle {field}.")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid candle {field}.") from exc
    if not (result == result and abs(result) != float("inf")):
        raise ValueError(f"Invalid candle {field}.")
    return result


def _timestamp_ms(value: object) -> int:
    raw = _number(value, "timestamp")
    return int(raw if raw > 10_000_000_000 else raw * 1000)


class ChartProvider:
    """Public REST candle failover, separate from trader-fill monitoring."""

    priority = REST_PRIORITY

    def __init__(
        self,
        loader: Callable[[str], Any] = _read_json,
        *,
        post_json: Callable[[dict[str, object]], Any] | None = None,
    ) -> None:
        self._load = loader
        self._post_json = post_json or self._default_post_json

    @staticmethod
    def _default_post_json(payload: dict[str, object]) -> Any:
        request = Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _base_symbol(symbol: str) -> str:
        normalized = symbol.upper()
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("Invalid chart symbol.")
        for quote in ("USDT", "USD"):
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return normalized[:-len(quote)]
        return normalized

    @staticmethod
    def _interval(interval: str) -> str:
        try:
            return SUPPORTED_INTERVALS[interval]
        except KeyError as exc:
            raise ValueError("Unsupported chart interval.") from exc

    @classmethod
    def _from_rows(
        cls,
        rows: object,
        *,
        time_index: int = 0,
        open_index: int = 1,
        high_index: int = 2,
        low_index: int = 3,
        close_index: int = 4,
        volume_index: int | None = 5,
        seconds: bool = False,
    ) -> list[ChartCandle]:
        if not isinstance(rows, list):
            raise ValueError("Provider returned an invalid candle list.")
        candles: list[ChartCandle] = []
        for row in rows:
            if not isinstance(row, (list, tuple)):
                continue
            required_indices = [time_index, open_index, high_index, low_index, close_index]
            if volume_index is not None:
                required_indices.append(volume_index)
            if len(row) <= max(required_indices):
                continue
            try:
                timestamp = _timestamp_ms(row[time_index])
                if seconds:
                    timestamp = int(_number(row[time_index], "timestamp") * 1000)
                volume = _number(row[volume_index], "volume") if volume_index is not None else None
                candles.append(
                    ChartCandle(
                        timestamp,
                        _number(row[open_index], "open"),
                        _number(row[high_index], "high"),
                        _number(row[low_index], "low"),
                        _number(row[close_index], "close"),
                        volume,
                        timestamp,
                    )
                )
            except (TypeError, ValueError, OverflowError):
                continue
        candles.sort(key=lambda candle: candle.time)
        if not candles:
            raise ValueError("Provider returned no valid candles.")
        return candles

    def _binance(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        params = {"symbol": f"{base}USDT", "interval": self._interval(interval), "limit": str(limit)}
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        return self._from_rows(self._load(f"https://api.binance.com/api/v3/klines?{urlencode(params)}"))

    def _kraken(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        if interval not in {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W"}:
            raise ValueError("Kraken does not support this chart interval.")
        pair_base = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
        params = {"pair": f"{pair_base}USD", "interval": str(INTERVAL_MINUTES[interval])}
        if end_ms is not None:
            params["since"] = str(max(0, end_ms // 1000 - INTERVAL_MINUTES[interval] * 60 * limit))
        payload = self._load(f"https://api.kraken.com/0/public/OHLC?{urlencode(params)}")
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(f"Kraken returned an error: {payload.get('error') if isinstance(payload, dict) else 'invalid response'}")
        result = payload.get("result")
        rows = next((value for key, value in result.items() if key != "last"), None) if isinstance(result, dict) else None
        candles = self._from_rows(rows, open_index=1, high_index=2, low_index=3, close_index=4, volume_index=6, seconds=True)
        if end_ms is not None:
            candles = [candle for candle in candles if candle.timestamp_ms <= end_ms]
            if not candles:
                raise ValueError("Kraken returned no candles before the requested chart end time.")
        return candles

    def _okx(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        params = {"instId": f"{base}-USDT", "bar": self._interval(interval), "limit": str(limit)}
        if end_ms is not None:
            params["after"] = str(end_ms)
        payload = self._load(f"https://www.okx.com/api/v5/market/candles?{urlencode(params)}")
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise ValueError(f"OKX returned an error: {payload.get('msg') if isinstance(payload, dict) else 'invalid response'}")
        return self._from_rows(payload.get("data"))

    def _bybit(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        params = {"category": "spot", "symbol": f"{base}USDT", "interval": str(INTERVAL_MINUTES[interval]), "limit": str(limit)}
        if end_ms is not None:
            params["end"] = str(end_ms)
        payload = self._load(f"https://api.bybit.com/v5/market/kline?{urlencode(params)}")
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise ValueError(f"Bybit returned an error: {payload.get('retMsg') if isinstance(payload, dict) else 'invalid response'}")
        rows = payload.get("result", {}).get("list") if isinstance(payload.get("result"), dict) else None
        return self._from_rows(rows, open_index=1, high_index=2, low_index=3, close_index=4, volume_index=5)

    def _bitget(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        granularity = "1min" if interval == "1" else self._interval(interval)
        params = {"symbol": f"{base}USDT", "granularity": granularity, "limit": str(limit)}
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        payload = self._load(f"https://api.bitget.com/api/v2/spot/market/candles?{urlencode(params)}")
        if not isinstance(payload, dict) or payload.get("code") != "00000":
            raise ValueError(f"Bitget returned an error: {payload.get('msg') if isinstance(payload, dict) else 'invalid response'}")
        return self._from_rows(payload.get("data"))

    def _hyperliquid(self, base: str, interval: str, limit: int, end_ms: int | None) -> list[ChartCandle]:
        end = end_ms if end_ms is not None else int(time.time() * 1000)
        duration = INTERVAL_MINUTES[interval] * 60_000
        payload = self._post_json(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": base,
                    "interval": self._interval(interval),
                    "startTime": max(0, end - duration * limit),
                    "endTime": end,
                },
            }
        )
        if not isinstance(payload, list):
            raise ValueError("Hyperliquid returned an invalid candle response.")
        return self._from_rows(
            [
                [row.get("t"), row.get("o"), row.get("h"), row.get("l"), row.get("c"), row.get("v")]
                for row in payload if isinstance(row, dict)
            ]
        )

    def candles(
        self,
        symbol: str,
        interval: str = "1",
        limit: int = 100,
        end_ms: int | None = None,
        preferred_source: str | None = None,
    ) -> tuple[str, list[ChartCandle], list[str]]:
        base = self._base_symbol(symbol)
        self._interval(interval)
        if not 1 <= limit <= 500:
            raise ValueError("Chart candle limit must be between 1 and 500.")
        order = list(self.priority)
        if preferred_source is not None:
            if preferred_source not in order:
                raise ValueError("Unsupported chart source.")
            order.remove(preferred_source)
            order.insert(0, preferred_source)
        fetchers = {
            "binance": self._binance,
            "kraken": self._kraken,
            "okx": self._okx,
            "bybit": self._bybit,
            "bitget": self._bitget,
            "hyperliquid": self._hyperliquid,
        }
        failures: list[str] = []
        for source in order:
            try:
                candles = fetchers[source](base, interval, limit, end_ms)
                return source, candles, failures
            except (HTTPError, URLError, OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError, OverflowError) as exc:
                failures.append(f"{source}: {exc}")
                LOGGER.warning("Chart candle provider %s failed for %s: %s", source, symbol, exc)
        raise RuntimeError("All chart providers failed (" + "; ".join(failures) + ").")
