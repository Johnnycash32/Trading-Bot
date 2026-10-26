from __future__ import annotations

import json
from threading import Event
from typing import Any, Callable

from .market_data import Candle

try:
    import websocket
except ImportError:
    websocket = None


def _interval_name(interval: str) -> str:
    return {"1": "1m", "5": "5m", "15": "15m", "60": "1h", "240": "4h"}.get(interval, interval)


def parse_bybit_kline(message: dict[str, Any]) -> Candle | None:
    rows = message.get("data") or []
    if not rows:
        return None
    row = rows[0]
    return Candle(int(row[0]), float(row[1]), float(row[5]), float(row[6]), float(row[4]), float(row[7]))


def parse_binance_kline(message: dict[str, Any]) -> Candle | None:
    row = message.get("k")
    if not row:
        return None
    return Candle(int(row["t"]), float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]), float(row["v"]))


class PublicCandleWebSocket:
    """Public candle transport with heartbeat/reconnect; it never sends private orders."""

    URLS = {"bybit": "wss://stream.bybit.com/v5/public/linear", "binance": "wss://fstream.binance.com/ws/{symbol}@kline_{interval}"}

    def __init__(self, exchange: str, connector: Callable[..., Any] | None = None, reconnect_seconds: float = 2.0) -> None:
        if exchange not in self.URLS:
            raise ValueError("Unsupported WebSocket exchange")
        self.exchange = exchange
        self.connector = connector
        self.reconnect_seconds = reconnect_seconds

    def url(self, symbol: str, interval: str) -> str:
        if self.exchange == "bybit":
            return self.URLS[self.exchange]
        return self.URLS[self.exchange].format(symbol=symbol.lower(), interval=_interval_name(interval))

    def _connect(self, symbol: str, interval: str) -> Any:
        if self.connector is not None:
            return self.connector(self.url(symbol, interval), timeout=10)
        if websocket is None:
            raise RuntimeError("Install websocket-client to enable public WebSocket candles.")
        return websocket.create_connection(self.url(symbol, interval), timeout=10)

    def subscribe(self, socket: Any, symbol: str, interval: str) -> None:
        if self.exchange == "bybit":
            socket.send(json.dumps({"op": "subscribe", "args": [f"kline.{interval}.{symbol.upper()}"]}))

    def run(self, symbol: str, interval: str, on_candle: Callable[[Candle], None], stop: Event) -> None:
        while not stop.is_set():
            socket = None
            try:
                socket = self._connect(symbol, interval)
                self.subscribe(socket, symbol, interval)
                socket.settimeout(5)
                while not stop.is_set():
                    try:
                        raw = socket.recv()
                    except Exception as exc:
                        if exc.__class__.__name__ == "WebSocketTimeoutException":
                            socket.send(json.dumps({"op": "ping"}))
                            continue
                        raise
                    if not raw:
                        raise ConnectionError("WebSocket closed without a close message")
                    message = json.loads(raw)
                    candle = parse_bybit_kline(message) if self.exchange == "bybit" else parse_binance_kline(message)
                    if candle is not None:
                        on_candle(candle)
            except Exception:
                if not stop.wait(self.reconnect_seconds):
                    continue
            finally:
                if socket is not None:
                    try:
                        socket.close()
                    except Exception:
                        pass
