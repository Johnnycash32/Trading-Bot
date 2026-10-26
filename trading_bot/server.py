from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .copy_trading import ActiveCopyAccount, ActivePosition, CopySettings, CopyStrategyLab, demo_active_accounts, demo_profiles
from .market_data import BinanceMarketData, BybitMarketData
from .paper_session import PaperExecutionSession


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_FILE = PROJECT_ROOT / "ui" / "dashboard.html"
MARKET_CLIENTS = {"bybit": BybitMarketData(), "binance": BinanceMarketData()}
PAPER_SESSION = PaperExecutionSession()
COPY_LAB = CopyStrategyLab()
COPY_CONTROL = {"auto_trade_enabled": False, "close_request": False, "last_action": "Monitoring remains active; new paper copies are paused."}
PAPER_AVAILABLE_BALANCE = 8_420.50


def live_copy_accounts() -> tuple[list[ActiveCopyAccount], float]:
    """Return demo copy positions marked against public prices and total unrealized P&L."""

    accounts: list[ActiveCopyAccount] = []
    total_unrealized = 0.0
    for account in demo_active_accounts():
        exchange = "binance" if "binance" in account.exchange.lower() else "bybit"
        client = MARKET_CLIENTS[exchange]
        positions: list[ActivePosition] = []
        for position in account.positions:
            mark_price = position.mark_price
            unrealized_pnl = position.unrealized_pnl
            try:
                mark_price = client.ticker(position.symbol).last
                direction = 1 if position.side == "long" else -1
                unrealized_pnl = position.quantity * (mark_price - position.entry_price) * direction
            except Exception:
                pass
            total_unrealized += unrealized_pnl
            positions.append(ActivePosition(position.symbol, position.side, position.entry_price, mark_price, position.quantity, unrealized_pnl, position.stop_loss, position.take_profit))
        accounts.append(ActiveCopyAccount(account.profile_id, account.display_name, account.exchange, account.status, account.copy_mode, tuple(positions)))
    return accounts, total_unrealized


def history_end_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        end = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int((end.replace(hour=23, minute=59, second=59, microsecond=999000)).timestamp() * 1000)
    except ValueError:
        return None


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
            end_ms = history_end_ms(query.get("history_date", [None])[0])
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                ticker = client.ticker(symbol)
                candles = client.candles(symbol, interval, 120, end_ms)
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
            end_ms = history_end_ms(query.get("history_date", [None])[0])
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                candles = client.candles(symbol, interval, 120, end_ms)
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
        if request.path == "/api/copy/candidates":
            candidates = []
            for profile in demo_profiles():
                assessment = COPY_LAB.assess(profile, CopySettings(preferred_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")))
                candidates.append({"profile": profile.__dict__, "assessment": assessment.as_dict()})
            self._send(200, json.dumps({"mode": "paper", "candidates": candidates}).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/copy/active":
            accounts, unrealized_pnl = live_copy_accounts()
            self._send(200, json.dumps({"mode": "paper", "control": COPY_CONTROL, "accounts": [account.as_dict() for account in accounts], "account_summary": {"starting_balance": 10_000.0, "available_balance": PAPER_AVAILABLE_BALANCE + unrealized_pnl, "equity": 10_000.0 + unrealized_pnl, "unrealized_pnl": unrealized_pnl, "realized_pnl": 0.0}}).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/account/summary":
            _, unrealized_pnl = live_copy_accounts()
            payload = {"starting_balance": 10_000.0, "available_balance": PAPER_AVAILABLE_BALANCE + unrealized_pnl, "equity": 10_000.0 + unrealized_pnl, "unrealized_pnl": unrealized_pnl, "realized_pnl": 0.0}
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path != "/api/copy/control":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            enabled = bool(payload.get("enabled"))
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"error":"Invalid control payload"}', "application/json; charset=utf-8")
            return
        COPY_CONTROL["auto_trade_enabled"] = enabled
        COPY_CONTROL["close_request"] = not enabled
        COPY_CONTROL["last_action"] = "New paper copies enabled." if enabled else "Close request created for active copied positions; monitoring remains active."
        self._send(200, json.dumps({"mode": "paper", "control": COPY_CONTROL}).encode(), "application/json; charset=utf-8")

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
