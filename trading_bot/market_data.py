from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


JsonLoader = Callable[[str], dict[str, Any]]


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
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class PublicMarketDataClient:
    """Read-only market-data boundary. It never creates or signs private requests."""

    def __init__(self, loader: JsonLoader = load_json) -> None:
        self._load = loader

    def _get(self, base_url: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        return self._load(f"{base_url}{path}?{urlencode(params)}")


class BybitMarketData(PublicMarketDataClient):
    base_url = "https://api.bybit.com"
    exchange = "bybit"

    def ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        payload = self._get("https://api.bybit.com", "/v5/market/tickers", {"category": "linear", "symbol": symbol})
        row = payload["result"]["list"][0]
        return Ticker(self.exchange, symbol, float(row["lastPrice"]), float(row["bid1Price"]), float(row["ask1Price"]), float(row["price24hPcnt"]) * 100, int(payload.get("time", 0)) or None)

    def candles(self, symbol: str = "BTCUSDT", interval: str = "60", limit: int = 100) -> list[Candle]:
        payload = self._get(self.base_url, "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)})
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

    def candles(self, symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 100) -> list[Candle]:
        payload = self._load(f"{self.base_url}/fapi/v1/klines?{urlencode({'symbol': symbol, 'interval': interval, 'limit': str(limit)})}")
        return [Candle(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])) for row in payload]
