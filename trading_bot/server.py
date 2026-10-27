from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from threading import RLock
from urllib.parse import parse_qs, urlsplit

from .copy_trading import ActiveCopyAccount, ActivePosition, CopySettings, CopyStrategyLab, demo_active_accounts, demo_profiles
from .gemini_agent import GeminiAnalysisAgent, gemini_config
from .market_data import BinanceMarketData, BybitMarketData
from .paper_session import PaperExecutionSession
from .private_accounts import private_account_snapshot
from .strategy import MovingAverageCrossStrategy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_FILE = PROJECT_ROOT / "ui" / "dashboard.html"
PAPER_STATE_FILE = PROJECT_ROOT / ".paper-state.json"
MARKET_CLIENTS = {"bybit": BybitMarketData(), "binance": BinanceMarketData()}
PAPER_SESSION = PaperExecutionSession.load(PAPER_STATE_FILE)
PAPER_SESSION.save(PAPER_STATE_FILE)
PAPER_LOCK = RLock()
COPY_LAB = CopyStrategyLab()
COPY_CONTROL = {"auto_trade_enabled": False, "close_request": False, "last_action": "Monitoring remains active; new paper copies are paused."}
PAPER_AVAILABLE_BALANCE = 8_420.50
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
PAPER_INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}
CLOSE_QUOTE_MAX_AGE_MS = 5_000
CLOSE_QUOTE_FUTURE_TOLERANCE_MS = 2_000


def fresh_close_quote(position: dict[str, object]) -> tuple[float, str, int]:
    """Resolve a fresh executable-side public quote, preferring the position venue."""

    exchange = str(position.get("exchange", "")).lower()
    symbol = str(position.get("symbol", ""))
    side = str(position.get("side", "")).lower()
    if exchange not in MARKET_CLIENTS or not SYMBOL_PATTERN.fullmatch(symbol) or side not in {"long", "short"}:
        raise ValueError(f"Open order {position.get('order_id', 'unknown')} has invalid market details.")

    sources = [exchange, *(venue for venue in ("bybit", "binance") if venue != exchange)]
    errors: list[str] = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for venue in sources:
        try:
            ticker = MARKET_CLIENTS[venue].ticker(symbol)
            timestamp_ms = ticker.timestamp_ms
            if timestamp_ms is None:
                raise ValueError("quote has no exchange timestamp")
            age_ms = now_ms - timestamp_ms
            if age_ms < -CLOSE_QUOTE_FUTURE_TOLERANCE_MS or age_ms > CLOSE_QUOTE_MAX_AGE_MS:
                raise ValueError(f"quote is stale ({age_ms} ms old)")
            price = ticker.bid if side == "long" else ticker.ask
            if price is None or not isfinite(price) or price <= 0:
                raise ValueError(f"quote has no valid {side}-close price")
            return float(price), f"{venue}_{'bid' if side == 'long' else 'ask'}", timestamp_ms
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(f"{venue}: {exc}")

    raise RuntimeError("No fresh executable close quote is available (" + "; ".join(errors) + ").")


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
        if request.path == "/api/agents":
            try:
                api_key, model = gemini_config()
                payload = {
                    "agents": [
                        {"id": "local", "name": "Local paper agents", "available": True},
                        {"id": "gemini", "name": "Gemini AI", "available": api_key is not None, "model": model},
                    ],
                    "gemini": {"configured": api_key is not None, "model": model},
                    "privacy": "Gemini receives only the selected symbol, interval, public ticker, and recent public candles when you explicitly request analysis.",
                }
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError) as exc:
                message = json.dumps({"error": str(exc)}).encode()
                self._send(500, message, "application/json; charset=utf-8")
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
                with PAPER_LOCK:
                    result = PAPER_SESSION.evaluate(exchange, symbol, candles)
                    PAPER_SESSION.save(PAPER_STATE_FILE)
                result["symbol"] = symbol
                result["exchange"] = exchange
                self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                message = json.dumps({"error": f"Paper evaluation or state save failed: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/state":
            query = parse_qs(request.query)
            symbol = query.get("symbol", [None])[0]
            with PAPER_LOCK:
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
            with PAPER_LOCK:
                payload = PAPER_SESSION.snapshot()["account_summary"]
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/private/positions":
            self._send(200, json.dumps(private_account_snapshot()).encode(), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if request.path == "/api/paper/close":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                    return
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"Invalid close request"}', "application/json; charset=utf-8")
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"Invalid close request"}', "application/json; charset=utf-8")
                return
            close_all = payload.get("all") is True
            order_id = payload.get("order_id")
            if not close_all and (not isinstance(order_id, str) or not order_id):
                self._send(400, b'{"error":"Provide an open paper order_id or set all to true"}', "application/json; charset=utf-8")
                return

            with PAPER_LOCK:
                if COPY_CONTROL["auto_trade_enabled"]:
                    self._send(409, b'{"error":"Turn off auto-copy before manually closing paper positions"}', "application/json; charset=utf-8")
                    return
                open_positions = PAPER_SESSION.snapshot()["open_positions"]
                selected = open_positions if close_all else [
                    position for position in open_positions if position.get("order_id") == order_id
                ]
                if not selected:
                    self._send(404, b'{"error":"No matching open paper position"}', "application/json; charset=utf-8")
                    return

                exit_quotes: dict[str, tuple[float, str, int]] = {}
                try:
                    for position in selected:
                        exit_quotes[str(position["order_id"])] = fresh_close_quote(position)
                except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                    message = json.dumps({"error": f"Could not close paper position: {exc}"}).encode()
                    self._send(503, message, "application/json; charset=utf-8")
                    return

                try:
                    if close_all:
                        closed = PAPER_SESSION.close_all_positions(exit_quotes)
                    else:
                        exit_price, price_source, quote_timestamp_ms = exit_quotes[str(order_id)]
                        closed_position = PAPER_SESSION.close_position(
                            str(order_id),
                            exit_price,
                            price_source=price_source,
                            quote_timestamp_ms=quote_timestamp_ms,
                        )
                        closed = [closed_position] if closed_position is not None else []
                    if not closed:
                        self._send(404, b'{"error":"No matching open paper position"}', "application/json; charset=utf-8")
                        return
                    PAPER_SESSION.save(PAPER_STATE_FILE)
                except (OSError, ValueError) as exc:
                    message = json.dumps({"error": f"Could not close or save paper position: {exc}"}).encode()
                    self._send(500, message, "application/json; charset=utf-8")
                    return
                response = {
                    "closed_positions": closed,
                    "state": PAPER_SESSION.snapshot(),
                    "message": f"Closed {len(closed)} paper position(s) using fresh executable-side public quotes.",
                }
                self._send(200, json.dumps(response).encode(), "application/json; charset=utf-8")
            return
        if request.path in {"/api/agents/gemini/analyze", "/api/agents/local/analyze"}:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                    return
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"Invalid request payload"}', "application/json; charset=utf-8")
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"Invalid request payload"}', "application/json; charset=utf-8")
                return
            exchange = payload.get("exchange")
            symbol = payload.get("symbol")
            interval = payload.get("interval")
            if exchange not in MARKET_CLIENTS:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol):
                self._send(400, b'{"error":"Invalid market symbol"}', "application/json; charset=utf-8")
                return
            if not isinstance(interval, str) or interval not in PAPER_INTERVALS:
                self._send(400, b'{"error":"Unsupported candle interval"}', "application/json; charset=utf-8")
                return
            try:
                client = MARKET_CLIENTS[exchange]
                market_interval = interval
                if exchange == "binance":
                    market_interval = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m", "60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h"}.get(interval, interval.lower())
                ticker = client.ticker(symbol)
                candles = client.candles(symbol, market_interval, 80)
                if not candles:
                    self._send(502, b'{"error":"No public candles are available for this market."}', "application/json; charset=utf-8")
                    return
                if request.path == "/api/agents/local/analyze":
                    closes = [candle.close for candle in candles]
                    change_percent = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0
                    signal = MovingAverageCrossStrategy().signal(candles)
                    direction = "higher" if change_percent > 0 else "lower" if change_percent < 0 else "unchanged"
                    analysis = (
                        f"Local review of {symbol}: last public price {ticker.last:g}; "
                        f"price ended {direction} by {abs(change_percent):.2f}% across the {len(candles)} candles. "
                        f"The moving-average strategy currently reports {signal.value}. "
                        "This is read-only descriptive analysis; it does not create or approve an order."
                    )
                    agent_name, model = "Local paper agents", "moving-average strategy"
                else:
                    api_key, model = gemini_config()
                    if not api_key:
                        self._send(503, b'{"error":"Gemini is not configured. Add GEMINI_API_KEY to the local .env file."}', "application/json; charset=utf-8")
                        return
                    agent = GeminiAnalysisAgent(api_key, model)
                    analysis = agent.analyze(exchange=exchange, symbol=symbol, interval=interval, ticker=ticker, candles=candles)
                    agent_name = "Gemini"
                with PAPER_LOCK:
                    PAPER_SESSION.record_agent_event(agent_name, "complete", symbol=symbol, exchange=exchange, interval=interval, model=model, summary=analysis[:240])
                    PAPER_SESSION.save(PAPER_STATE_FILE)
                self._send(200, json.dumps({"agent": agent_name, "model": model, "symbol": symbol, "exchange": exchange, "analysis": analysis}).encode(), "application/json; charset=utf-8")
            except (RuntimeError, ValueError, OSError) as exc:
                safe_message = str(exc)
                if request.path == "/api/agents/gemini/analyze":
                    with PAPER_LOCK:
                        PAPER_SESSION.record_agent_event("Gemini", "failed", symbol=symbol, exchange=exchange, interval=interval, reason=safe_message)
                        PAPER_SESSION.save(PAPER_STATE_FILE)
                status_code = 502
                self._send(status_code, json.dumps({"error": safe_message}).encode(), "application/json; charset=utf-8")
            return
        if request.path != "/api/copy/control":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                return
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"error":"Invalid control payload"}', "application/json; charset=utf-8")
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            self._send(400, b'{"error":"Control enabled must be a boolean"}', "application/json; charset=utf-8")
            return
        enabled = payload["enabled"]
        with PAPER_LOCK:
            COPY_CONTROL["auto_trade_enabled"] = enabled
            COPY_CONTROL["close_request"] = False
            COPY_CONTROL["last_action"] = "New paper copies enabled." if enabled else "New copies paused; open positions remain available for manual close."
            response = {"mode": "paper", "control": COPY_CONTROL}
            self._send(200, json.dumps(response).encode(), "application/json; charset=utf-8")

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
