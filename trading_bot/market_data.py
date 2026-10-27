from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


JsonLoader = Callable[[str], Any]


@dataclass(frozen=True)
class Ticker:
    exchange: str
    symbol: str
    last: float
    bid: float | None
    ask: float | None
    change_percent: float | None
    timestamp_ms: int | None


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def load_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "trading-bot-paper/0.1"})
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


class PublicMarketDataClient:
    """Read-only market-data boundary. It never creates or signs private requests."""

    def __init__(self, loader: JsonLoader = load_json) -> None:
        self._load = loader

    def _get(self, base_url: str, path: str, params: dict[str, str]) -> Any:
        return self._load(f"{base_url}{path}?{urlencode(params)}")


class BybitMarketData(PublicMarketDataClient):
    base_url = "https://api.bybit.com"
    exchange = "bybit"

    def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        payload = self._get("https://api.bybit.com", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        row = payload["result"]["list"][0]
        return Ticker(self.exchange, symbol, float(row["lastPrice"]), float(row["bid1Price"]), float(row["ask1Price"]), float(row["price24hPcnt"]) * 100, int(payload.get("time", 0)) or None)

    def candles(self, symbol: str = "BTCUSDT", interval: str = "60", limit: int = 100, end_ms: int | None = None) -> list[Candle]:
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)}
        if end_ms is not None:
            params["end"] = str(end_ms)
        payload = self._get(self.base_url, "/v5/market/kline", params)
        rows = reversed(payload["result"]["list"])
        return [Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])) for row in rows]


class BinanceMarketData(PublicMarketDataClient):
    base_url = "https://fapi.binance.com"
    exchange = "binance"

    def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        payload = self._get(self.base_url, "/fapi/v1/ticker/24hr", {"symbol": symbol})
        bid = payload.get("bidPrice")
        ask = payload.get("askPrice")
        return Ticker(self.exchange, symbol, float(payload["lastPrice"]), float(bid) if bid else None, float(ask) if ask else None, float(payload["priceChangePercent"]), int(payload.get("closeTime", 0)) or None)

    def candles(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 100, end_ms: int | None = None) -> list[Candle]:
        params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        payload = self._load(f"{self.base_url}/fapi/v1/klines?{urlencode(params)}")
        return [Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])) for row in payload]


class CoinbaseMarketData(PublicMarketDataClient):
    """Public spot candles and quotes, normalized to the shared market-data model."""

    base_url = "https://api.exchange.coinbase.com"
    exchange = "coinbase"
    GRANULARITY_SECONDS = {
        "1": 60,
        "5": 300,
        "15": 900,
        "60": 3600,
        "360": 21_600,
        "D": 86_400,
    }

    @staticmethod
    def _product_id(symbol: str) -> str:
        normalized = symbol.upper()
        if normalized.endswith("USDT"):
            normalized = normalized[:-4] + "USD"
        if not normalized.endswith("USD") or len(normalized) <= 3:
            raise ValueError(f"Coinbase public spot candles are unavailable for {symbol}.")
        base = normalized[:-3]
        if not base.isalnum():
            raise ValueError(f"Invalid Coinbase product symbol: {symbol}.")
        return f"{base}-USD"

    def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        product = self._product_id(symbol)
        row = self._get(self.base_url, f"/products/{product}/ticker", {})
        timestamp = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))
        return Ticker(
            self.exchange,
            symbol,
            float(row["price"]),
            float(row["bid"]),
            float(row["ask"]),
            None,
            int(timestamp.timestamp() * 1000),
        )

    def candles(self, symbol: str = "BTCUSDT", interval: str = "1", limit: int = 100, end_ms: int | None = None) -> list[Candle]:
        try:
            granularity = self.GRANULARITY_SECONDS[interval]
        except KeyError as exc:
            raise ValueError(f"Coinbase does not support the {interval} chart interval.") from exc
        product = self._product_id(symbol)
        count = min(max(int(limit), 1), 300)
        end = datetime.fromtimestamp(end_ms / 1000, timezone.utc) if end_ms is not None else datetime.now(timezone.utc)
        start = end - timedelta(seconds=granularity * count)
        params = {
            "granularity": str(granularity),
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
        }
        payload = self._get(self.base_url, f"/products/{product}/candles", params)
        if not isinstance(payload, list):
            raise ValueError("Coinbase returned an invalid candle response.")
        return [
            Candle(int(row[0]) * 1000, float(row[3]), float(row[2]), float(row[1]), float(row[4]), float(row[5]))
            for row in reversed(payload)
        ]
