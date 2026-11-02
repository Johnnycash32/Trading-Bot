from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from time import monotonic
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
    volume: float | None


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


class TwelveDataMarketData(PublicMarketDataClient):
    """Optional read-only multi-asset chart feed; credentials stay server-side."""

    base_url = "https://api.twelvedata.com"
    exchange = "twelvedata"
    INTERVALS = {
        "1": "1min",
        "5": "5min",
        "15": "15min",
        "30": "30min",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1day",
        "W": "1week",
        "M": "1month",
    }
    COMMODITY_SYMBOLS = {
        "XAUUSD": "XAU/USD",
        "XAGUSD": "XAG/USD",
    }
    CACHE_TTL_SECONDS = 60

    def __init__(
        self,
        api_key: str | None = None,
        loader: JsonLoader = load_json,
    ) -> None:
        super().__init__(loader)
        self._api_key = api_key.strip() if api_key and api_key.strip() else self._load_api_key()
        self._response_cache: dict[str, tuple[float, Any]] = {}

    @staticmethod
    def _load_api_key() -> str | None:
        key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
        if key:
            return key
        env_path = Path(__file__).resolve().parents[1] / ".env"
        try:
            lines = env_path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RuntimeError(f"Could not read the local environment file: {exc}") from exc
        for line in lines:
            entry = line.strip()
            if entry.startswith("export "):
                entry = entry[7:].lstrip()
            name, separator, raw_value = entry.partition("=")
            if not separator or name.strip() != "TWELVE_DATA_API_KEY":
                continue
            value = raw_value.strip()
            if value.startswith(("'", '"')):
                closing_quote = value.find(value[0], 1)
                if closing_quote != -1:
                    value = value[1:closing_quote]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            return value or None
        return None

    @property
    def configured(self) -> bool:
        return self._api_key is not None

    @staticmethod
    def _provider_symbol(symbol: str, asset_class: str) -> str:
        normalized = symbol.strip().upper()
        if asset_class == "forex":
            if len(normalized) != 6 or not normalized.isalpha():
                raise ValueError(f"Unsupported Twelve Data forex symbol: {symbol}.")
            return f"{normalized[:3]}/{normalized[3:]}"
        if asset_class == "crypto":
            for quote in ("USDT", "USDC", "USD"):
                if normalized.endswith(quote) and len(normalized) > len(quote):
                    return f"{normalized[:-len(quote)]}/USD"
            raise ValueError(f"Unsupported Twelve Data crypto symbol: {symbol}.")
        if asset_class == "stocks":
            if not normalized.isalnum():
                raise ValueError(f"Unsupported Twelve Data stock symbol: {symbol}.")
            return normalized
        if asset_class == "indices":
            if not normalized.isalnum():
                raise ValueError(f"Unsupported Twelve Data index symbol: {symbol}.")
            return normalized
        if asset_class == "commodities":
            try:
                return TwelveDataMarketData.COMMODITY_SYMBOLS[normalized]
            except KeyError as exc:
                raise ValueError(
                    f"{symbol} needs a provider-specific commodity contract mapping; "
                    "only XAUUSD and XAGUSD spot symbols are mapped."
                ) from exc
        raise ValueError(f"Unsupported asset class: {asset_class}.")

    def _request(self, path: str, params: dict[str, str]) -> Any:
        if not self._api_key:
            raise RuntimeError("Set TWELVE_DATA_API_KEY in the local .env file to enable this market feed.")
        request_params = {**params, "apikey": self._api_key}
        cache_key = f"{path}?{urlencode({key: value for key, value in request_params.items() if key != 'apikey'})}"
        now = monotonic()
        cached = self._response_cache.get(cache_key)
        if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]
        payload = self._get(self.base_url, path, request_params)
        if isinstance(payload, dict) and payload.get("status") == "error":
            message = str(payload.get("message", "The provider rejected the market-data request."))
            raise ValueError(f"Twelve Data: {message}")
        self._response_cache[cache_key] = (now, payload)
        return payload

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Twelve Data returned an invalid candle timestamp.") from exc
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return int(timestamp.timestamp() * 1000)

    def ticker(self, symbol: str = "BTCUSDT", asset_class: str = "crypto") -> Ticker:
        provider_symbol = self._provider_symbol(symbol, asset_class)
        payload = self._request("/quote", {"symbol": provider_symbol, "timezone": "UTC"})
        if not isinstance(payload, dict):
            raise ValueError("Twelve Data returned an invalid quote response.")
        try:
            last = float(payload.get("close", payload.get("price", "")))
            if not isfinite(last) or last <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Twelve Data quote did not contain a valid last price.") from exc
        change_raw = payload.get("percent_change")
        change = None
        if change_raw not in (None, ""):
            try:
                change = float(change_raw)
                if not isfinite(change):
                    change = None
            except (TypeError, ValueError):
                change = None
        timestamp_value = payload.get("timestamp")
        if timestamp_value is None and payload.get("datetime"):
            timestamp_ms = self._timestamp_ms(payload["datetime"])
        else:
            try:
                timestamp_ms = int(float(timestamp_value) * 1000) if timestamp_value is not None else None
            except (TypeError, ValueError, OverflowError):
                timestamp_ms = None
        return Ticker(self.exchange, symbol, last, None, None, change, timestamp_ms)

    def candles(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "60",
        limit: int = 100,
        end_ms: int | None = None,
        asset_class: str = "crypto",
    ) -> list[Candle]:
        try:
            provider_interval = self.INTERVALS[interval]
        except KeyError as exc:
            raise ValueError(f"Twelve Data does not support the {interval} chart interval.") from exc
        params = {
            "symbol": self._provider_symbol(symbol, asset_class),
            "interval": provider_interval,
            "outputsize": str(min(max(int(limit), 1), 5000)),
            "timezone": "UTC",
        }
        if end_ms is not None:
            end_date = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
            params["end_date"] = end_date.strftime("%Y-%m-%d %H:%M:%S")
        payload = self._request("/time_series", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
            raise ValueError("Twelve Data returned an invalid time-series response.")
        candles: list[Candle] = []
        for row in payload["values"]:
            if not isinstance(row, dict):
                raise ValueError("Twelve Data returned an invalid candle.")
            try:
                raw_volume = row.get("volume")
                volume = float(raw_volume) if raw_volume not in (None, "") else None
                if volume is not None and (not isfinite(volume) or volume < 0):
                    raise ValueError("Twelve Data returned an invalid candle volume.")
                candle = Candle(
                    self._timestamp_ms(row["datetime"]),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    volume,
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Twelve Data returned a malformed candle.") from exc
            candles.append(candle)
        candles.sort(key=lambda candle: candle.timestamp_ms)
        return candles
