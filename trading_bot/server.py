from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .market_data import BinanceMarketData, BybitMarketData
from .paper_session import PaperExecutionSession


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_FILE = PROJECT_ROOT / "ui" / "dashboard.html"
MARKET_CLIENTS = {"bybit": BybitMarketData(), "binance": BinanceMarketData()}
PAPER_SESSION = PaperExecutionSession()


class AppHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path in ("/", "/index.html", "/Trading%20Bot.html"):
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if request.path == "/api/status":
            payload = {
                "mode": "paper",
                "auto_trading": False,
                "market_data": "public-read-only",
                "exchanges": {"bybit": "not-connected", "binance": "not-connected"},
                "paper_equity": 10_000.0,
                "paper_pnl": 184.20,
                "risk": {"daily_loss_percent": 18, "open_position_percent": 34, "margin_percent": 42},
                "message": "Python paper engine connected. No live orders are permitted.",
            }
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/market":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            symbol = query.get("symbol", ["SOLUSDT"])[0].upper()
            interval = query.get("interval", ["1"])[0]
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                ticker = client.ticker(symbol)
                candles = client.candles(symbol, interval, 120)
                payload = {
                    "exchange": exchange,
                    "symbol": symbol,
                    "interval": interval,
                    "ticker": ticker.__dict__,
                    "candles": [candle.__dict__ for candle in candles],
                }
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            except Exception as exc:  # public feed failures should not crash the paper app
                message = json.dumps({"error": f"Public market feed unavailable: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/evaluate":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            symbol = query.get("symbol", ["SOLUSDT"])[0].upper()
            interval = query.get("interval", ["1"])[0]
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                candles = client.candles(symbol, interval, 120)
                result = PAPER_SESSION.evaluate(exchange, symbol, candles)
                result["symbol"] = symbol
                result["exchange"] = exchange
                self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                message = json.dumps({"error": f"Paper evaluation unavailable: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/state":
            query = parse_qs(request.query)
            symbol = query.get("symbol", [None])[0]
            payload = PAPER_SESSION.snapshot(symbol)
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[Trading Bot] {format % args}")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Trading Bot app running at http://{host}:{port}")
    print("Paper mode only; no live orders or exchange credentials are enabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Trading Bot app.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
