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


class AlphaVantageMarketData(PublicMarketDataClient):
    """Free Alpha Vantage feed for forex, stocks, commodities, indices. Free tier: 5 req/min, 500/day."""

    base_url = "https://www.alphavantage.co"
    exchange = "alphavantage"
    INTERVALS = {
        "1": "1min",
        "5": "5min",
        "15": "15min",
        "30": "30min",
        "60": "60min",
        "D": "daily",
        "W": "weekly",
        "M": "monthly",
    }
    FOREX_SYMBOLS = {
        "EURUSD": "EUR/USD",
        "GBPUSD": "GBP/USD",
        "USDJPY": "USD/JPY",
        "USDCHF": "USD/CHF",
        "AUDUSD": "AUD/USD",
        "USDCAD": "USD/CAD",
        "NZDUSD": "NZD/USD",
        "EURGBP": "EUR/GBP",
        "EURJPY": "EUR/JPY",
        "GBPJPY": "GBP/JPY",
    }
    COMMODITY_SYMBOLS = {
        "XAUUSD": "XAU/USD",
        "XAGUSD": "XAG/USD",
        "WTIUSD": "WTI/USD",
        "BRENTUSD": "BRENT/USD",
    }
    INDEX_SYMBOLS = {
        "SPX": "SPX",
        "DJI": "DJI",
        "NDX": "NDX",
        "VIX": "VIX",
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
        key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
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
            if not separator or name.strip() != "ALPHA_VANTAGE_API_KEY":
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
            return AlphaVantageMarketData.FOREX_SYMBOLS.get(normalized, normalized)
        if asset_class == "crypto":
            return normalized
        if asset_class == "stocks":
            return normalized
        if asset_class == "indices":
            return AlphaVantageMarketData.INDEX_SYMBOLS.get(normalized, normalized)
        if asset_class == "commodities":
            return AlphaVantageMarketData.COMMODITY_SYMBOLS.get(normalized, normalized)
        raise ValueError(f"Unsupported asset class: {asset_class}.")

    def _request(self, path: str, params: dict[str, str]) -> Any:
        if not self._api_key:
            raise RuntimeError("Set ALPHA_VANTAGE_API_KEY in the local .env file to enable this market feed.")
        request_params = {**params, "apikey": self._api_key}
        cache_key = f"{path}?{urlencode({key: value for key, value in request_params.items() if key != 'apikey'})}"
        now = monotonic()
        cached = self._response_cache.get(cache_key)
        if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
            return cached[1]
        payload = self._get(self.base_url, path, request_params)
        if isinstance(payload, dict) and payload.get("Error Message"):
            raise ValueError(f"Alpha Vantage: {payload['Error Message']}")
        if isinstance(payload, dict) and payload.get("Note"):
            raise ValueError(f"Alpha Vantage rate limit: {payload['Note']}")
        self._response_cache[cache_key] = (now, payload)
        return payload

    @staticmethod
    def _timestamp_ms(value: object) -> int:
        try:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Alpha Vantage returned an invalid candle timestamp.") from exc
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return int(timestamp.timestamp() * 1000)

    def ticker(self, symbol: str = "EURUSD", asset_class: str = "forex") -> Ticker:
        provider_symbol = self._provider_symbol(symbol, asset_class)
        if asset_class == "forex":
            payload = self._request("/query", {"function": "CURRENCY_EXCHANGE_RATE", "from_currency": provider_symbol.split("/")[0], "to_currency": provider_symbol.split("/")[1]})
            rate_data = payload.get("Realtime Currency Exchange Rate", {})
            last = float(rate_data.get("5. Exchange Rate", 0))
            change = float(rate_data.get("9. Change Percent", "0%").replace("%", "")) if rate_data.get("9. Change Percent") else None
            timestamp = int(rate_data.get("6. Last Refreshed", "0").replace("-", "").replace(":", "").replace(" ", "")) * 1000
        elif asset_class == "crypto":
            payload = self._request("/query", {"function": "CRYPTO_INTRADAY", "symbol": provider_symbol, "market": "USD", "interval": "1min"})
            series = payload.get("Time Series Crypto (1min)", {})
            latest = next(iter(series.values())) if series else {}
            last = float(latest.get("4. close", 0))
            change = None
            timestamp = None
        else:
            payload = self._request("/query", {"function": "GLOBAL_QUOTE", "symbol": provider_symbol})
            quote = payload.get("Global Quote", {})
            last = float(quote.get("05. price", 0))
            change = float(quote.get("10. change percent", "0%").replace("%", "")) if quote.get("10. change percent") else None
            timestamp = None
        return Ticker(self.exchange, symbol, last, None, None, change, timestamp)

    def candles(
        self,
        symbol: str = "EURUSD",
        interval: str = "60",
        limit: int = 100,
        end_ms: int | None = None,
        asset_class: str = "forex",
    ) -> list[Candle]:
        try:
            provider_interval = self.INTERVALS[interval]
        except KeyError as exc:
            raise ValueError(f"Alpha Vantage does not support the {interval} chart interval.") from exc
        provider_symbol = self._provider_symbol(symbol, asset_class)
        if asset_class == "forex":
            function = "FX_INTRADAY" if interval != "D" else "FX_DAILY"
            params = {"function": function, "from_symbol": provider_symbol.split("/")[0], "to_symbol": provider_symbol.split("/")[1], "interval": provider_interval, "outputsize": "compact"}
        elif asset_class == "crypto":
            params = {"function": "CRYPTO_INTRADAY", "symbol": provider_symbol, "market": "USD", "interval": provider_interval, "outputsize": "compact"}
        else:
            function = "TIME_SERIES_INTRADAY" if interval != "D" else "TIME_SERIES_DAILY"
            params = {"function": function, "symbol": provider_symbol, "interval": provider_interval, "outputsize": "compact"}
        if end_ms is not None:
            end_date = datetime.fromtimestamp(end_ms / 1000, timezone.utc)
            params["end_date"] = end_date.strftime("%Y-%m-%d")
        payload = self._request("/query", params)
        series_key = next((k for k in payload.keys() if "Time Series" in k or "Time Series" in k), None)
        if not series_key:
            raise ValueError("Alpha Vantage returned an invalid time-series response.")
        series = payload[series_key]
        candles: list[Candle] = []
        for ts_str, row in sorted(series.items()):
            try:
                ts = datetime.fromisoformat(ts_str.replace(" ", "T"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                timestamp_ms = int(ts.timestamp() * 1000)
                open_price = float(row.get("1. open", 0))
                high_price = float(row.get("2. high", 0))
                low_price = float(row.get("3. low", 0))
                close_price = float(row.get("4. close", 0))
                volume = float(row.get("5. volume", 0)) if row.get("5. volume") else None
                if not isfinite(open_price) or not isfinite(high_price) or not isfinite(low_price) or not isfinite(close_price):
                    continue
                candles.append(Candle(timestamp_ms, open_price, high_price, low_price, close_price, volume))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        candles.sort(key=lambda c: c.timestamp_ms)
        return candles[-limit:] if limit else candles


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


class YahooFinanceMarketData(PublicMarketDataClient):
    """Free Yahoo Finance chart/quote feed for US equities, forex, indices, commodities. No API key required."""

    base_url = "https://query1.finance.yahoo.com"
    exchange = "yahoo"
    INTERVALS = {
        "1": "1m",
        "5": "5m",
        "15": "15m",
        "30": "30m",
        "60": "1h",
        "120": "2h",
        "240": "4h",
        "D": "1d",
        "W": "1wk",
        "M": "1mo",
    }
    RANGES = {
        "1": "1d",
        "5": "1d",
        "15": "5d",
        "30": "1mo",
        "60": "1mo",
        "120": "3mo",
        "240": "6mo",
        "D": "2y",
        "W": "5y",
        "M": "max",
    }
    FOREX_SYMBOLS = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "USDJPY=X",
        "USDCHF": "USDCHF=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "USDCAD=X",
        "NZDUSD": "NZDUSD=X",
        "EURGBP": "EURGBP=X",
        "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X",
    }
    COMMODITY_SYMBOLS = {
        "XAUUSD": "GC=F",
        "XAGUSD": "SI=F",
        "WTIUSD": "CL=F",
        "BRENTUSD": "BZ=F",
    }
    INDEX_SYMBOLS = {
        "SPX": "^GSPC",
        "DJI": "^DJI",
        "NDX": "^NDX",
        "VIX": "^VIX",
        "RUT": "^RUT",
    }

    @staticmethod
    def _provider_symbol(symbol: str, asset_class: str = "stocks") -> str:
        normalized = symbol.strip().upper()
        if asset_class == "forex":
            return YahooFinanceMarketData.FOREX_SYMBOLS.get(normalized, f"{normalized}=X")
        if asset_class == "crypto":
            return normalized
        if asset_class == "stocks":
            return normalized
        if asset_class == "indices":
            return YahooFinanceMarketData.INDEX_SYMBOLS.get(normalized, normalized)
        if asset_class == "commodities":
            return YahooFinanceMarketData.COMMODITY_SYMBOLS.get(normalized, normalized)
        raise ValueError(f"Unsupported asset class: {asset_class}.")

    def ticker(self, symbol: str = "AAPL", asset_class: str = "stocks") -> Ticker:
        provider_symbol = self._provider_symbol(symbol, asset_class)
        payload = self._get(self.base_url, f"/v8/finance/chart/{provider_symbol}", {"interval": "1m", "range": "1d"})
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        last = float(meta.get("regularMarketPrice", meta.get("previousClose", 0)))
        change = float(meta.get("regularMarketChangePercent", 0))
        timestamp = int(meta.get("regularMarketTime", 0)) * 1000
        return Ticker(self.exchange, symbol.upper(), last, None, None, change, timestamp)

    def candles(
        self,
        symbol: str = "AAPL",
        interval: str = "60",
        limit: int = 100,
        end_ms: int | None = None,
        asset_class: str = "stocks",
    ) -> list[Candle]:
        try:
            yf_interval = self.INTERVALS[interval]
            yf_range = self.RANGES[interval]
        except KeyError as exc:
            raise ValueError(f"Yahoo Finance does not support the {interval} chart interval.") from exc

        provider_symbol = self._provider_symbol(symbol, asset_class)
        params = {"interval": yf_interval, "range": yf_range}
        if end_ms is not None:
            params["period2"] = str(int(end_ms / 1000))

        payload = self._get(self.base_url, f"/v8/finance/chart/{provider_symbol}", params)
        result = payload["chart"]["result"][0]
        timestamps = result["timestamp"]
        quotes = result["indicators"]["quote"][0]

        candles: list[Candle] = []
        for i, ts in enumerate(timestamps):
            try:
                open_price = quotes["open"][i]
                high_price = quotes["high"][i]
                low_price = quotes["low"][i]
                close_price = quotes["close"][i]
                volume = quotes["volume"][i]
                if None in (open_price, high_price, low_price, close_price):
                    continue
                candle = Candle(
                    timestamp_ms=int(ts) * 1000,
                    open=float(open_price),
                    high=float(high_price),
                    low=float(low_price),
                    close=float(close_price),
                    volume=float(volume) if volume is not None else None,
                )
                candles.append(candle)
            except (IndexError, TypeError, ValueError):
                continue

        candles.sort(key=lambda c: c.timestamp_ms)
        return candles[-limit:] if limit else candles
