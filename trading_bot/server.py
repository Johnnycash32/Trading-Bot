from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from threading import Event, RLock, Thread
from urllib.parse import parse_qs, urlsplit

from .broker_connections import BrokerRequestStore, BybitTestnetAccount, BybitTestnetConfig, broker_directory_payload
from .copy_trading import ActiveCopyAccount, ActivePosition, CopySettings, CopyStrategyLab, demo_active_accounts, demo_profiles
from .exchanges.paper import PaperExchange
from .gemini_agent import GeminiAnalysisAgent, gemini_config
from .market_data import BinanceMarketData, BybitMarketData, Candle, CoinbaseMarketData, TwelveDataMarketData
from .paper_copy import (
    PaperCopyEventWorker,
    PaperCopySettings,
    candidate_rejection,
    destination_symbol,
    load_settings,
    save_settings,
)
from .paper_session import PaperExecutionSession
from .private_accounts import private_account_snapshot
from .strategy import MovingAverageCrossStrategy
from trader_sources.chart_provider import ChartProvider
from trader_sources import HyperliquidMonitor
from trader_sources.base import TraderEvent


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
UI_FILE = PROJECT_ROOT / "ui" / "dashboard.html"
AGENT_AVATAR_FILES = {
    f"/ui/assets/agents/{name}.jpg": name
    for name in ("raccoon", "eagle", "hawk", "lion", "cheetah", "hare")
}
PAPER_STATE_FILE = PROJECT_ROOT / ".paper-state.json"
PAPER_COPY_SETTINGS_FILE = PROJECT_ROOT / ".paper-copy-settings.json"
BROKER_REQUESTS_FILE = PROJECT_ROOT / ".broker-requests.json"
BROKER_REQUESTS = BrokerRequestStore(BROKER_REQUESTS_FILE)
MARKET_CLIENTS = {
    "bybit": BybitMarketData(),
    "binance": BinanceMarketData(),
    "coinbase": CoinbaseMarketData(),
    "twelvedata": TwelveDataMarketData(),
}
PAPER_SESSION = PaperExecutionSession.load(PAPER_STATE_FILE)
PAPER_LOCK = RLock()
COPY_LAB = CopyStrategyLab()
COPY_SETTINGS = load_settings(PAPER_COPY_SETTINGS_FILE)
COPY_STATUS: dict[str, object] = {"last_result": None, "last_message": None, "last_event_at": None}
CHART_PROVIDER = ChartProvider()
COPY_CONTROL = {"auto_trade_enabled": False, "close_request": False, "last_action": "Monitoring remains active; new paper copies are paused."}
PAPER_AVAILABLE_BALANCE = 8_420.50
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
COPY_MARK_MAX_AGE_MS = 60_000
COPY_MARK_FUTURE_TOLERANCE_MS = 2_000
PAPER_INTERVALS = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}
BINANCE_INTERVALS = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h",
    "D": "1d", "W": "1w", "M": "1M",
}
INTERVAL_MILLISECONDS = {
    **{str(minutes): minutes * 60_000 for minutes in (1, 3, 5, 15, 30, 60, 120, 240, 360, 720)},
    "D": 86_400_000,
    "W": 604_800_000,
}


def synchronize_daily_profit_lock() -> dict[str, object]:
    with PAPER_LOCK:
        status = PAPER_SESSION.daily_profit_status()
        changed = PAPER_SESSION.apply_daily_profit_lock()
        if status["target_met"]:
            COPY_CONTROL["auto_trade_enabled"] = False
            COPY_CONTROL["close_request"] = False
            target_message = (
                "Daily paper-profit target reached; automatic paper entries are paused. "
                "Manual paper orders now use stricter risk limits."
            )
            if COPY_CONTROL["last_action"] != target_message:
                COPY_CONTROL["last_action"] = target_message
                changed = True
        if changed:
            PAPER_SESSION.save(PAPER_STATE_FILE)
        return status


synchronize_daily_profit_lock()
PAPER_SESSION.save(PAPER_STATE_FILE)
CLOSE_QUOTE_MAX_AGE_MS = 5_000
CLOSE_QUOTE_FUTURE_TOLERANCE_MS = 2_000


def closed_candles(candles: list[Candle], interval_ms: int | None, now_ms: int) -> list[Candle]:
    if interval_ms is None or interval_ms <= 0:
        return []
    return [
        candle
        for candle in candles
        if candle.timestamp_ms + interval_ms <= now_ms
    ]


def market_payload(
    exchange: str,
    symbol: str,
    interval: str,
    end_ms: int | None = None,
    asset_class: str = "crypto",
) -> dict[str, object]:
    """Fetch chart candles with venue failover and treat the live ticker as optional."""

    if exchange == "twelvedata":
        client = MARKET_CLIENTS.get(exchange)
        if not isinstance(client, TwelveDataMarketData):
            raise RuntimeError("Twelve Data feed is not configured in the market client.")
        candles = client.candles(symbol, interval, 120, end_ms, asset_class=asset_class)
        if not candles:
            raise RuntimeError("Twelve Data returned no chart candles.")
        ticker_errors: list[str] = []
        try:
            ticker: dict[str, object] = client.ticker(symbol, asset_class=asset_class).__dict__
            ticker_source = exchange
            ticker_unavailable = False
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
            ticker_errors.append(str(exc))
            ticker = {
                "exchange": exchange,
                "symbol": symbol,
                "last": candles[-1].close,
                "bid": None,
                "ask": None,
                "change_percent": None,
                "timestamp_ms": candles[-1].timestamp_ms,
            }
            ticker_source = "candle_close"
            ticker_unavailable = True
        return {
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "ticker": ticker,
            "candles": [candle.__dict__ for candle in candles],
            "market_data": {
                "candle_source": exchange,
                "ticker_source": ticker_source,
                "fallback": False,
                "ticker_unavailable": ticker_unavailable,
                "candle_errors": [],
                "ticker_errors": ticker_errors,
            },
        }

    venues = [exchange, *(venue for venue in ("bybit", "binance", "coinbase") if venue != exchange)]
    candle_errors: list[str] = []
    candle_source = exchange
    candles = None
    for venue in venues:
        client = MARKET_CLIENTS.get(venue)
        if client is None:
            continue
        venue_interval = BINANCE_INTERVALS.get(interval, interval) if venue == "binance" else interval
        try:
            candles = client.candles(symbol, venue_interval, 120, end_ms)
            if not candles:
                raise ValueError(f"{venue} returned no candles")
            candle_source = venue
            break
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
            candle_errors.append(f"{venue}: {exc}")
    if candles is None:
        raise RuntimeError("No exchange returned chart candles (" + "; ".join(candle_errors) + ").")

    ticker = None
    ticker_source = None
    ticker_errors: list[str] = []
    ticker_venues = [venue for venue in venues if venue in MARKET_CLIENTS]
    for venue in ticker_venues:
        try:
            ticker = MARKET_CLIENTS[venue].ticker(symbol)
            ticker_source = venue
            break
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
            ticker_errors.append(f"{venue}: {exc}")
    if ticker is None:
        latest_candle = candles[-1]
        ticker = {
            "exchange": candle_source,
            "symbol": symbol,
            "last": latest_candle.close,
            "bid": None,
            "ask": None,
            "change_percent": None,
            "timestamp_ms": latest_candle.timestamp_ms,
        }
        ticker_source = "candle_close"
    else:
        ticker = ticker.__dict__

    return {
        "exchange": exchange,
        "symbol": symbol,
        "interval": interval,
        "ticker": ticker,
        "candles": [candle.__dict__ for candle in candles],
        "market_data": {
            "candle_source": candle_source,
            "ticker_source": ticker_source,
            "fallback": candle_source != exchange,
            "ticker_unavailable": ticker_source == "candle_close",
            "candle_errors": candle_errors,
            "ticker_errors": ticker_errors,
        },
    }


def fresh_close_quote(position: dict[str, object]) -> tuple[float, str, int]:
    """Resolve a fresh executable-side public quote, preferring the position venue."""

    exchange = str(position.get("exchange", "")).lower()
    symbol = str(position.get("symbol", ""))
    side = str(position.get("side", "")).lower()
    if exchange not in MARKET_CLIENTS or not SYMBOL_PATTERN.fullmatch(symbol) or side not in {"long", "short"}:
        raise ValueError(f"Open order {position.get('order_id', 'unknown')} has invalid market details.")

    sources = [exchange, *(venue for venue in ("bybit", "binance", "coinbase") if venue != exchange)]
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


def fresh_open_quote(exchange: str, symbol: str, side: str) -> tuple[float, str, int]:
    """Resolve a fresh executable-side public quote for a paper entry."""

    if exchange not in MARKET_CLIENTS or not SYMBOL_PATTERN.fullmatch(symbol) or side not in {"long", "short"}:
        raise ValueError("Choose a supported exchange, symbol, and trade side.")
    sources = [exchange, *(venue for venue in ("bybit", "binance", "coinbase") if venue != exchange)]
    errors: list[str] = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for venue in sources:
        client = MARKET_CLIENTS.get(venue)
        if client is None:
            continue
        try:
            ticker = client.ticker(symbol)
            timestamp_ms = ticker.timestamp_ms
            if timestamp_ms is None:
                raise ValueError("quote has no exchange timestamp")
            age_ms = now_ms - timestamp_ms
            if age_ms < -CLOSE_QUOTE_FUTURE_TOLERANCE_MS or age_ms > CLOSE_QUOTE_MAX_AGE_MS:
                raise ValueError(f"quote is stale ({age_ms} ms old)")
            price = ticker.ask if side == "long" else ticker.bid
            if price is None or not isfinite(price) or price <= 0:
                raise ValueError(f"quote has no valid {side}-entry price")
            return float(price), f"{venue}_{'ask' if side == 'long' else 'bid'}", timestamp_ms
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append(f"{venue}: {exc}")

    raise RuntimeError("No fresh executable entry quote is available (" + "; ".join(errors) + ").")


MAX_COPY_ENTRY_SLIPPAGE_PERCENT = 1.0
COPY_POSITION_MONITOR_INTERVAL_MS = 60_000
COPY_POSITION_MONITOR_PERIOD_SECONDS = 30


def fresh_copy_open_quote(symbol: str, side: str) -> tuple[float, str, int]:
    """Resolve a fresh executable quote from supported USDT perpetual venues only."""

    errors: list[str] = []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for venue in ("bybit", "binance"):
        try:
            ticker = MARKET_CLIENTS[venue].ticker(symbol)
            timestamp_ms = ticker.timestamp_ms
            if timestamp_ms is None:
                raise ValueError("quote has no exchange timestamp")
            age_ms = now_ms - timestamp_ms
            if age_ms < -CLOSE_QUOTE_FUTURE_TOLERANCE_MS or age_ms > CLOSE_QUOTE_MAX_AGE_MS:
                raise ValueError(f"quote is stale ({age_ms} ms old)")
            price = ticker.ask if side == "long" else ticker.bid
            if price is None or not isfinite(price) or price <= 0:
                raise ValueError(f"quote has no valid {side}-entry price")
            return float(price), f"{venue}_{'ask' if side == 'long' else 'bid'}", timestamp_ms
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            errors.append(f"{venue}: {exc}")
    raise RuntimeError("No fresh Bybit or Binance perpetual entry quote is available (" + "; ".join(errors) + ").")


def _copy_event_processed(event_id: str) -> bool:
    if event_id in COPY_SETTINGS.processed_event_ids:
        return True
    return any(
        item.get("source_event_id") == event_id
        for item in (*PAPER_SESSION.orders, *PAPER_SESSION.events)
    )


def _record_copy_result(event: TraderEvent, status: str, message: str, order: dict[str, object] | None = None) -> None:
    global COPY_SETTINGS
    processed_ids = (*COPY_SETTINGS.processed_event_ids, event.event_id)[-20_000:]
    COPY_SETTINGS = replace(COPY_SETTINGS, processed_event_ids=processed_ids)
    details: dict[str, object] = {
        "source_event_id": event.event_id,
        "source_trader_id": event.source_trader_id,
        "instrument": event.instrument,
        "direction": event.direction,
        "message": message,
    }
    if order is not None:
        details["order_id"] = order["order_id"]
        details["symbol"] = order["symbol"]
        details["quantity"] = order["quantity"]
    PAPER_SESSION.record_paper_copy_event(status, **details)
    PAPER_SESSION.save(PAPER_STATE_FILE)
    save_settings(PAPER_COPY_SETTINGS_FILE, COPY_SETTINGS)
    COPY_STATUS.update(
        {
            "last_result": status,
            "last_message": message,
            "last_event_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def process_paper_copy_event(event: TraderEvent) -> None:
    with PAPER_LOCK:
        if not COPY_CONTROL["auto_trade_enabled"] or _copy_event_processed(event.event_id):
            return
        settings = COPY_SETTINGS
        monitor = HYPERLIQUID_MONITOR.snapshot()
        monitored_wallets = {
            str(trader.get("source_trader_id", "")).lower()
            for trader in monitor["traders"][:15]
        }
        rejection = candidate_rejection(
            event,
            settings,
            monitored_wallets,
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        if rejection is not None:
            _record_copy_result(event, "skipped", rejection)
            return
    try:
        symbol = destination_symbol(event.instrument)
        entry_price, quote_source, quote_timestamp_ms = fresh_copy_open_quote(symbol, event.direction)
        slippage_percent = abs(entry_price - event.price) / event.price * 100
        if slippage_percent > MAX_COPY_ENTRY_SLIPPAGE_PERCENT:
            raise ValueError(
                f"Entry quote moved {slippage_percent:.2f}% from the source fill; "
                f"the maximum allowed is {MAX_COPY_ENTRY_SLIPPAGE_PERCENT:.2f}%."
            )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        with PAPER_LOCK:
            if COPY_CONTROL["auto_trade_enabled"] and not _copy_event_processed(event.event_id):
                _record_copy_result(event, "skipped", str(exc))
        return

    with PAPER_LOCK:
        if not COPY_CONTROL["auto_trade_enabled"] or _copy_event_processed(event.event_id):
            return
        latest_monitor = HYPERLIQUID_MONITOR.snapshot()
        latest_wallets = {
            str(trader.get("source_trader_id", "")).lower()
            for trader in latest_monitor["traders"][:15]
        }
        rejection = candidate_rejection(
            event,
            COPY_SETTINGS,
            latest_wallets,
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        )
        if rejection is not None:
            _record_copy_result(event, "skipped", rejection)
            return
        try:
            order = PAPER_SESSION.submit_risk_sized_paper_copy(
                "bybit" if quote_source.startswith("bybit_") else "binance",
                symbol,
                event.direction,
                entry_price,
                COPY_SETTINGS.stop_loss_percent,
                COPY_SETTINGS.take_profit_percent,
                COPY_SETTINGS.leverage,
                event.timestamp_ms - event.timestamp_ms % COPY_POSITION_MONITOR_INTERVAL_MS,
                source_event_id=event.event_id,
                source_trader_id=event.source_trader_id,
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            _record_copy_result(event, "skipped", str(exc))
            return
        saved_order = next(item for item in PAPER_SESSION.orders if item["order_id"] == order["order_id"])
        saved_order["entry_quote_source"] = quote_source
        saved_order["entry_quote_timestamp_ms"] = quote_timestamp_ms
        order["entry_quote_source"] = quote_source
        order["entry_quote_timestamp_ms"] = quote_timestamp_ms
        position = next(item for item in PAPER_SESSION.positions if item["order_id"] == order["order_id"])
        position["entry_quote_source"] = quote_source
        position["entry_quote_timestamp_ms"] = quote_timestamp_ms
        _record_copy_result(
            event,
            "opened",
            f"Opened risk-sized paper {event.direction} {symbol} from a verified live fill.",
            order,
        )


def enqueue_paper_copy_event(event: TraderEvent) -> None:
    with PAPER_LOCK:
        if (
            not COPY_CONTROL["auto_trade_enabled"]
            or event.delivery_source != "websocket"
            or event.event_type != "position_opened"
            or event.source_trader_id not in COPY_SETTINGS.selected_wallets
        ):
            return
    if not COPY_EVENT_WORKER.submit(event):
        LOGGER.error("Paper-copy queue is full; skipped verified fill %s.", event.event_id)
        with PAPER_LOCK:
            COPY_STATUS.update(
                {
                    "last_result": "skipped",
                    "last_message": "Paper-copy queue is full; this fill was not processed.",
                    "last_event_at": datetime.now(timezone.utc).isoformat(),
                }
            )


COPY_EVENT_WORKER = PaperCopyEventWorker(process_paper_copy_event)
HYPERLIQUID_MONITOR = HyperliquidMonitor(event_callback=enqueue_paper_copy_event)


def monitor_open_paper_copies_once() -> None:
    with PAPER_LOCK:
        jobs = {
            (str(position.get("exchange", "")).lower(), str(position.get("symbol", "")).upper())
            for position in PAPER_SESSION.positions
            if position.get("status") == "open"
            and position.get("source_event_id")
            and position.get("exchange")
            and position.get("symbol")
        }

    interval = "1"
    interval_ms = INTERVAL_MILLISECONDS[interval]
    for exchange, symbol in jobs:
        warning_key = f"{exchange}:{symbol}"
        try:
            client = MARKET_CLIENTS.get(exchange)
            if client is None or exchange not in {"bybit", "binance"}:
                raise ValueError(f"No supported paper market feed is available for {exchange} {symbol}.")
            venue_interval = BINANCE_INTERVALS.get(interval, interval) if exchange == "binance" else interval
            candles = client.candles(symbol, venue_interval, 120, None)
            confirmed_candles = closed_candles(
                candles,
                interval_ms,
                int(datetime.now(timezone.utc).timestamp() * 1000),
            )
            if not confirmed_candles:
                raise RuntimeError(f"No fully closed 1-minute candles are available for {symbol}.")

            with PAPER_LOCK:
                still_open = any(
                    position.get("status") == "open"
                    and position.get("source_event_id")
                    and str(position.get("exchange", "")).lower() == exchange
                    and str(position.get("symbol", "")).upper() == symbol
                    for position in PAPER_SESSION.positions
                )
                if not still_open:
                    continue
                result = PAPER_SESSION.evaluate(exchange, symbol, confirmed_candles, interval_ms)
                PAPER_SESSION.save(PAPER_STATE_FILE)
                warnings = dict(COPY_STATUS.get("monitoring_warnings", {}))
                if result.get("monitoring_gap"):
                    warnings[warning_key] = str(result.get("reason", "Paper position monitoring detected a candle gap."))
                else:
                    warnings.pop(warning_key, None)
                COPY_STATUS["monitoring_warnings"] = warnings
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            with PAPER_LOCK:
                warnings = dict(COPY_STATUS.get("monitoring_warnings", {}))
                message = str(exc)
                if warnings.get(warning_key) != message:
                    LOGGER.warning("Paper-copy position monitoring failed for %s: %s", warning_key, exc)
                warnings[warning_key] = message
                COPY_STATUS["monitoring_warnings"] = warnings


def _paper_copy_position_monitor(stop_event: Event) -> None:
    while not stop_event.is_set():
        try:
            monitor_open_paper_copies_once()
        except Exception:
            LOGGER.exception("Paper-copy position monitor failed.")
        if stop_event.wait(COPY_POSITION_MONITOR_PERIOD_SECONDS):
            return


def live_copy_accounts() -> tuple[list[ActiveCopyAccount], float]:
    """Mark synthetic demo positions concurrently using only fresh public quotes."""

    demo_accounts = demo_active_accounts()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    position_jobs = [
        (account, position, "binance" if "binance" in account.exchange.lower() else "bybit")
        for account in demo_accounts
        for position in account.positions
    ]

    def mark_position(job: tuple[ActiveCopyAccount, ActivePosition, str]) -> ActivePosition:
        _, position, exchange = job
        mark_price = None
        unrealized_pnl = None
        mark_status = "unavailable"
        mark_timestamp_ms = None
        try:
            ticker = MARKET_CLIENTS[exchange].ticker(position.symbol)
            mark_timestamp_ms = ticker.timestamp_ms
            age_ms = now_ms - mark_timestamp_ms if mark_timestamp_ms is not None else None
            if (
                ticker.last > 0
                and isfinite(ticker.last)
                and age_ms is not None
                and -COPY_MARK_FUTURE_TOLERANCE_MS <= age_ms <= COPY_MARK_MAX_AGE_MS
            ):
                mark_price = ticker.last
                direction = 1 if position.side == "long" else -1
                unrealized_pnl = position.quantity * (mark_price - position.entry_price) * direction
                mark_status = "live"
            elif age_ms is not None:
                mark_status = "stale"
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError):
            mark_status = "unavailable"
        return ActivePosition(
            position.symbol,
            position.side,
            position.entry_price,
            mark_price,
            position.quantity,
            unrealized_pnl,
            position.stop_loss,
            position.take_profit,
            mark_status,
            mark_timestamp_ms,
        )

    with ThreadPoolExecutor(max_workers=max(1, min(3, len(position_jobs)))) as executor:
        marked_positions = iter(executor.map(mark_position, position_jobs))
        accounts = [
            ActiveCopyAccount(
                account.profile_id,
                account.display_name,
                account.exchange,
                account.status,
                account.copy_mode,
                tuple(next(marked_positions) for _ in account.positions),
            )
            for account in demo_accounts
        ]
    total_unrealized = sum(
        position.unrealized_pnl or 0
        for account in accounts
        for position in account.positions
    )
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
        if request.path == "/api/copy/settings":
            with PAPER_LOCK:
                payload = {
                    "settings": COPY_SETTINGS.as_dict(),
                    "control": dict(COPY_CONTROL),
                    "copy_status": dict(COPY_STATUS),
                }
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        if request.path in ("/", "/index.html", "/Trading%20Bot.html"):
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if request.path in AGENT_AVATAR_FILES:
            avatar_name = AGENT_AVATAR_FILES[request.path]
            avatar_file = UI_FILE.parent / "assets" / "agents" / f"{avatar_name}.jpg"
            self._send(200, avatar_file.read_bytes(), "image/jpeg")
            return
        if request.path == "/api/status":
            payload = {
                "mode": "paper",
                "auto_trading": False,
                "market_data": "public-read-only",
                "exchanges": {
                    "bybit": "public",
                    "binance": "public",
                    "coinbase": "public-spot",
                    "twelvedata": "configured" if MARKET_CLIENTS["twelvedata"].configured else "not-configured",
                },
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
        if request.path.startswith("/api/chart/"):
            query = parse_qs(request.query)
            symbol = request.path.removeprefix("/api/chart/").upper()
            interval = query.get("interval", ["1"])[0]
            history_date = query.get("history_date", [None])[0]
            preferred_source = query.get("source", [None])[0]
            if not SYMBOL_PATTERN.fullmatch(symbol):
                self._send(400, b'{"error":"Invalid chart symbol"}', "application/json; charset=utf-8")
                return
            try:
                source, candles, errors = CHART_PROVIDER.candles(
                    symbol,
                    interval,
                    limit=100,
                    end_ms=history_end_ms(history_date),
                    preferred_source=preferred_source,
                )
                latest = candles[-1]
                ticker = {
                    "exchange": source,
                    "symbol": symbol,
                    "last": latest.close,
                    "bid": None,
                    "ask": None,
                    "change_percent": None,
                    "timestamp_ms": latest.timestamp_ms,
                }
                ticker_source = "candle_close"
                for venue in ("bybit", "binance"):
                    client = MARKET_CLIENTS.get(venue)
                    if client is None:
                        continue
                    try:
                        quote = client.ticker(symbol)
                        ticker = quote.__dict__
                        ticker_source = venue
                        break
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                        LOGGER.warning("Chart quote provider %s failed for %s: %s", venue, symbol, exc)
                self._send(200, json.dumps({
                    "exchange": source,
                    "symbol": symbol,
                    "interval": interval,
                    "ticker": ticker,
                    "candles": [candle.as_dict() for candle in candles],
                    "market_data": {
                        "candle_source": source,
                        "ticker_source": ticker_source,
                        "fallback": source != "binance",
                        "ticker_unavailable": ticker_source == "candle_close",
                        "candle_errors": errors,
                        "ticker_errors": [],
                        "provider_priority": list(CHART_PROVIDER.priority),
                    },
                }).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                message = json.dumps({"error": f"Chart feed temporarily unavailable. Retrying. {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/market":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            symbol = query.get("symbol", ["SOLUSDT"])[0].upper()
            interval = query.get("interval", ["1"])[0]
            asset_class = query.get("asset_class", ["crypto"])[0].lower()
            end_ms = history_end_ms(query.get("history_date", [None])[0])
            if exchange not in MARKET_CLIENTS:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                payload = market_payload(exchange, symbol, interval, end_ms, asset_class)
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                message = json.dumps({"error": f"Public market feed unavailable: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/market/quote":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            symbol = query.get("symbol", ["SOLUSDT"])[0].upper()
            asset_class = query.get("asset_class", ["crypto"])[0].lower()
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported market-data feed"}', "application/json; charset=utf-8")
                return
            try:
                if isinstance(client, TwelveDataMarketData):
                    ticker = client.ticker(symbol, asset_class=asset_class)
                else:
                    ticker = client.ticker(symbol)
                self._send(200, json.dumps({"ticker": ticker.__dict__, "exchange": exchange}).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                message = json.dumps({"error": f"Public quote unavailable: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/evaluate":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            symbol = query.get("symbol", ["SOLUSDT"])[0].upper()
            interval = query.get("interval", ["1"])[0]
            end_ms = history_end_ms(query.get("history_date", [None])[0])
            if end_ms is not None:
                self._send(409, b'{"error":"Paper evaluation is disabled for historical chart data."}', "application/json; charset=utf-8")
                return
            with PAPER_LOCK:
                copied_position = next(
                    (
                        position
                        for position in PAPER_SESSION.positions
                        if position.get("status") == "open"
                        and position.get("source_event_id")
                        and str(position.get("symbol", "")).upper() == symbol
                    ),
                    None,
                )
            if copied_position is not None:
                exchange = str(copied_position["exchange"]).lower()
                interval = "1"
            client = MARKET_CLIENTS.get(exchange)
            if client is None:
                self._send(400, b'{"error":"Unsupported exchange"}', "application/json; charset=utf-8")
                return
            try:
                venue_interval = BINANCE_INTERVALS.get(interval, interval) if exchange == "binance" else interval
                candles = client.candles(symbol, venue_interval, 120, end_ms)
                interval_ms = INTERVAL_MILLISECONDS.get(interval)
                confirmed_candles = closed_candles(
                    candles,
                    interval_ms,
                    int(datetime.now(timezone.utc).timestamp() * 1000),
                )
                with PAPER_LOCK:
                    result: dict[str, object]
                    if confirmed_candles:
                        result = PAPER_SESSION.evaluate(exchange, symbol, confirmed_candles, interval_ms)
                    else:
                        result = {
                            "status": "hold",
                            "signal": "hold",
                            "reason": "Waiting for a fully closed candle; the active candle is not used for signals or paper exits.",
                        }
                    result["daily_profit"] = synchronize_daily_profit_lock()
                    PAPER_SESSION.save(PAPER_STATE_FILE)
                result["symbol"] = symbol
                result["exchange"] = exchange
                self._send(200, json.dumps(result).encode(), "application/json; charset=utf-8")
            except Exception as exc:
                message = json.dumps({"error": f"Paper evaluation or state save failed: {exc}"}).encode()
                self._send(502, message, "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/scan":
            query = parse_qs(request.query)
            exchange = query.get("exchange", ["bybit"])[0].lower()
            interval = query.get("interval", ["1"])[0]
            symbols = list(dict.fromkeys(
                symbol.upper()
                for raw_symbols in query.get("symbols", [])
                for symbol in raw_symbols.split(",")
                if SYMBOL_PATTERN.fullmatch(symbol.upper())
            ))[:20]
            excluded = {
                symbol.upper()
                for raw_symbols in query.get("exclude", [])
                for symbol in raw_symbols.split(",")
                if SYMBOL_PATTERN.fullmatch(symbol.upper())
            }
            symbols = [symbol for symbol in symbols if symbol not in excluded]
            client = MARKET_CLIENTS.get(exchange)
            interval_ms = INTERVAL_MILLISECONDS.get(interval)
            if client is None or exchange not in {"bybit", "binance"}:
                self._send(400, b'{"error":"Choose a supported perpetual market feed for paper signal scanning."}', "application/json; charset=utf-8")
                return
            if interval not in PAPER_INTERVALS or interval_ms is None:
                self._send(400, b'{"error":"Unsupported candle interval"}', "application/json; charset=utf-8")
                return
            if not symbols:
                self._send(400, b'{"error":"Provide at least one supported symbol to scan."}', "application/json; charset=utf-8")
                return

            def preview_symbol(symbol: str) -> dict[str, object]:
                try:
                    venue_interval = BINANCE_INTERVALS.get(interval, interval) if exchange == "binance" else interval
                    candles = client.candles(symbol, venue_interval, 120, None)
                    confirmed = closed_candles(candles, interval_ms, int(datetime.now(timezone.utc).timestamp() * 1000))
                    if not confirmed:
                        return {"symbol": symbol, "reason": "No fully closed candles are available."}
                    with PAPER_LOCK:
                        preview_session = copy(PAPER_SESSION)
                        preview_session.exchange = PaperExchange()
                        preview_session.orders = deepcopy(PAPER_SESSION.orders)
                        preview_session.history = deepcopy(PAPER_SESSION.history)
                        preview_session.positions = deepcopy(PAPER_SESSION.positions)
                        preview_session.balance_history = deepcopy(PAPER_SESSION.balance_history)
                        preview_session.events = deepcopy(PAPER_SESSION.events)
                        preview_session._last_order_candle = deepcopy(PAPER_SESSION._last_order_candle)
                        preview_session._last_evaluated_candle = deepcopy(PAPER_SESSION._last_evaluated_candle)
                    result = preview_session.evaluate(exchange, symbol, confirmed, interval_ms)
                    behavior = result.get("behavioral", {})
                    if result.get("status") != "simulated" or not isinstance(behavior, dict) or behavior.get("decision") != "paper_simulate":
                        return {"symbol": symbol, "reason": str(result.get("reason", "No confirmed risk-approved signal."))}
                    return {
                        "symbol": symbol,
                        "signal": result.get("signal"),
                        "confidence": behavior.get("confidence", 0.0) if isinstance(behavior, dict) else 0.0,
                        "risk_reward_ratio": result.get("order", {}).get("risk_reward_ratio", 0.0),
                        "closed_candle_timestamp_ms": confirmed[-1].timestamp_ms,
                    }
                except Exception as exc:
                    return {"symbol": symbol, "reason": f"Market scan unavailable: {exc}"}

            with ThreadPoolExecutor(max_workers=3) as executor:
                previews = list(executor.map(preview_symbol, symbols))
            candidates = [item for item in previews if "signal" in item]
            candidates.sort(key=lambda item: float(item.get("confidence", 0.0)), reverse=True)
            self._send(200, json.dumps({
                "exchange": exchange,
                "interval": interval,
                "scanned": len(symbols),
                "candidates": candidates,
                "unavailable": [item for item in previews if "signal" not in item],
            }).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/paper/state":
            query = parse_qs(request.query)
            symbol = query.get("symbol", [None])[0]
            try:
                with PAPER_LOCK:
                    synchronize_daily_profit_lock()
                    payload = PAPER_SESSION.snapshot(symbol)
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            except (OSError, ValueError) as exc:
                self._send(500, json.dumps({"error": f"Could not synchronize paper profit policy: {exc}"}).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/copy/candidates":
            try:
                traders = HYPERLIQUID_MONITOR.source.discover_traders(limit=20)
                self._send(200, json.dumps({
                    "mode": "paper",
                    "source": "Hyperliquid public leaderboard",
                    "discovery_source": "public_leaderboard_undocumented",
                    "candidates": [
                        {
                            "profile": {
                                "profile_id": trader["source_trader_id"],
                                "display_name": trader["display_name"],
                                "exchange": "Hyperliquid",
                                "account_value": trader["account_value"],
                                "month_pnl": trader["month_pnl"],
                                "month_roi": trader["month_roi"],
                            },
                            "assessment": {
                                "eligible": False,
                                "evidence_score": None,
                                "reasons": ["Leaderboard performance is discovery data, not a risk assessment or copy recommendation."],
                            },
                        }
                        for trader in traders
                    ],
                }).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError) as exc:
                self._send(502, json.dumps({"error": f"Hyperliquid leaderboard unavailable: {exc}"}).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/copy/active":
            try:
                with PAPER_LOCK:
                    synchronize_daily_profit_lock()
            except (OSError, ValueError) as exc:
                self._send(500, json.dumps({"error": f"Could not synchronize paper profit policy: {exc}"}).encode(), "application/json; charset=utf-8")
                return
            HYPERLIQUID_MONITOR.start()
            payload = HYPERLIQUID_MONITOR.snapshot()
            payload.update(
                {
                    "mode": "paper",
                    "control": dict(COPY_CONTROL),
                }
            )
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/account/summary":
            with PAPER_LOCK:
                payload = PAPER_SESSION.snapshot()["account_summary"]
            self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/private/positions":
            self._send(200, json.dumps(private_account_snapshot()).encode(), "application/json; charset=utf-8")
            return
        if request.path == "/api/brokers":
            try:
                payload = broker_directory_payload(BROKER_REQUESTS)
                self._send(200, json.dumps(payload).encode(), "application/json; charset=utf-8")
            except (OSError, RuntimeError, ValueError) as exc:
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
            return
        if request.path.startswith("/api/"):
            self._send(404, b'{"error":"API endpoint not found"}', "application/json; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        global COPY_SETTINGS
        request = urlsplit(self.path)
        if request.path == "/api/paper/order":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                    return
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"Invalid paper order request"}', "application/json; charset=utf-8")
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"Invalid paper order request"}', "application/json; charset=utf-8")
                return

            if payload.get("action") == "cancel":
                if set(payload) != {"action", "order_id"} or not isinstance(payload.get("order_id"), str):
                    self._send(400, b'{"error":"Provide a pending paper order ID to cancel"}', "application/json; charset=utf-8")
                    return
                with PAPER_LOCK:
                    cancelled = PAPER_SESSION.cancel_pending_order(payload["order_id"])
                    if cancelled is None:
                        self._send(404, b'{"error":"No matching pending paper order"}', "application/json; charset=utf-8")
                        return
                    try:
                        PAPER_SESSION.save(PAPER_STATE_FILE)
                    except (OSError, ValueError) as exc:
                        self._send(500, json.dumps({"error": f"Could not save paper order cancellation: {exc}"}).encode(), "application/json; charset=utf-8")
                        return
                    self._send(200, json.dumps({"order": cancelled, "state": PAPER_SESSION.snapshot()}).encode(), "application/json; charset=utf-8")
                return

            expected_fields = {
                "exchange", "symbol", "side", "order_type", "quantity",
                "entry_price", "stop_loss", "take_profit", "leverage", "interval",
            }
            if set(payload) != expected_fields or any(not isinstance(payload.get(key), str) for key in expected_fields):
                self._send(400, b'{"error":"Paper order fields are invalid"}', "application/json; charset=utf-8")
                return
            exchange = payload["exchange"].lower()
            symbol = payload["symbol"].upper()
            side = payload["side"].lower()
            order_type = payload["order_type"].lower()
            interval = payload["interval"]
            if exchange not in MARKET_CLIENTS or interval not in PAPER_INTERVALS or not SYMBOL_PATTERN.fullmatch(symbol):
                self._send(400, b'{"error":"Choose a supported market, symbol, and chart interval"}', "application/json; charset=utf-8")
                return
            if order_type not in {"market", "limit"}:
                self._send(400, b'{"error":"Only market and limit paper orders are supported"}', "application/json; charset=utf-8")
                return
            try:
                quantity = float(payload["quantity"])
                entry_price = float(payload["entry_price"])
                stop_loss = float(payload["stop_loss"])
                take_profit = float(payload["take_profit"])
                leverage = float(payload["leverage"])
                if order_type == "market":
                    entry_price, quote_source, quote_timestamp_ms = fresh_open_quote(exchange, symbol, side)
                else:
                    quote_source, quote_timestamp_ms = "user_limit_price", None
            except (ValueError, TypeError) as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            except RuntimeError as exc:
                self._send(503, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            interval_ms = INTERVAL_MILLISECONDS.get(interval)
            if interval_ms is None:
                self._send(400, b'{"error":"This chart interval is not available for paper entries"}', "application/json; charset=utf-8")
                return
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            candle_timestamp_ms = now_ms - now_ms % interval_ms
            with PAPER_LOCK:
                try:
                    synchronize_daily_profit_lock()
                except (OSError, ValueError) as exc:
                    self._send(500, json.dumps({"error": f"Could not synchronize paper profit policy: {exc}"}).encode(), "application/json; charset=utf-8")
                    return
                if COPY_CONTROL["auto_trade_enabled"]:
                    self._send(409, b'{"error":"Turn off auto-copy before placing a manual paper order"}', "application/json; charset=utf-8")
                    return
                try:
                    order = PAPER_SESSION.submit_manual_order(
                        exchange,
                        symbol,
                        side,
                        quantity,
                        entry_price,
                        stop_loss,
                        take_profit,
                        leverage,
                        candle_timestamp_ms,
                        order_type,
                    )
                    order["entry_quote_source"] = quote_source
                    order["entry_quote_timestamp_ms"] = quote_timestamp_ms
                    saved_order = next(item for item in PAPER_SESSION.orders if item["order_id"] == order["order_id"])
                    saved_order["entry_quote_source"] = quote_source
                    saved_order["entry_quote_timestamp_ms"] = quote_timestamp_ms
                    if order_type == "market":
                        saved_position = next(item for item in PAPER_SESSION.positions if item["order_id"] == order["order_id"])
                        saved_position["entry_quote_source"] = quote_source
                        saved_position["entry_quote_timestamp_ms"] = quote_timestamp_ms
                    PAPER_SESSION.save(PAPER_STATE_FILE)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                    return
                except (OSError, RuntimeError) as exc:
                    self._send(500, json.dumps({"error": f"Could not save paper order: {exc}"}).encode(), "application/json; charset=utf-8")
                    return
                status_code = 202 if order_type == "limit" else 201
                message = (
                    "Daily paper-profit target is met. This manual paper order passed stricter post-target risk limits. No broker order was sent."
                    if order.get("daily_target_mode") == "post_target_strict"
                    else "Paper order saved. No exchange order was sent."
                )
                self._send(status_code, json.dumps({
                    "order": order,
                    "state": PAPER_SESSION.snapshot(symbol),
                    "message": message,
                }).encode(), "application/json; charset=utf-8")
            return

        if request.path in {
            "/api/brokers/requests",
            "/api/brokers/bybit-testnet/check",
            "/api/brokers/bybit-testnet/order",
        }:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 4096:
                    self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                    return
                raw_payload = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw_payload)
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"Invalid request payload"}', "application/json; charset=utf-8")
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"Invalid request payload"}', "application/json; charset=utf-8")
                return
            if request.path == "/api/brokers/requests":
                broker_name = payload.get("broker_name")
                mode = payload.get("requested_mode")
                notes = payload.get("notes", "")
                if not isinstance(broker_name, str) or not isinstance(mode, str) or not isinstance(notes, str):
                    self._send(400, b'{"error":"Broker name, requested mode, and notes must be text"}', "application/json; charset=utf-8")
                    return
                try:
                    saved = BROKER_REQUESTS.add_request(broker_name, mode, notes)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                    return
                except (OSError, RuntimeError) as exc:
                    self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                    return
                self._send(201, json.dumps({"request": saved}).encode(), "application/json; charset=utf-8")
                return

            if request.path.endswith("/check") and payload:
                self._send(400, b'{"error":"The Bybit testnet check does not accept credentials in the request"}', "application/json; charset=utf-8")
                return
            if request.path.endswith("/order") and set(payload) != {"symbol", "side", "quantity", "confirmed"}:
                self._send(400, b'{"error":"Only symbol, side, quantity, and confirmation are accepted; credentials must remain on the server"}', "application/json; charset=utf-8")
                return
            if request.path.endswith("/order") and payload.get("confirmed") is not True:
                self._send(400, b'{"error":"Explicitly confirm that this order is for Bybit testnet only"}', "application/json; charset=utf-8")
                return
            config = BybitTestnetConfig.from_environment()
            if config is None:
                self._send(409, b'{"error":"Configure dedicated Bybit testnet credentials on the server first"}', "application/json; charset=utf-8")
                return
            account = BybitTestnetAccount(config)
            if request.path.endswith("/check"):
                try:
                    snapshot = account.snapshot()
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    self._send(502, json.dumps({"error": f"Bybit testnet verification failed: {exc}"}).encode(), "application/json; charset=utf-8")
                    return
                self._send(200, json.dumps(snapshot).encode(), "application/json; charset=utf-8")
                return
            symbol = payload.get("symbol")
            side = payload.get("side")
            quantity = payload.get("quantity")
            if not isinstance(symbol, str) or not isinstance(side, str) or not isinstance(quantity, str):
                self._send(400, b'{"error":"Symbol, side, and quantity must be text"}', "application/json; charset=utf-8")
                return
            try:
                order = account.place_market_order(symbol, side, quantity)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            except PermissionError as exc:
                self._send(403, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            except (OSError, RuntimeError, KeyError, TypeError) as exc:
                self._send(502, json.dumps({"error": f"Bybit testnet order failed: {exc}"}).encode(), "application/json; charset=utf-8")
                return
            self._send(200, json.dumps({"order": order}).encode(), "application/json; charset=utf-8")
            return
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
                try:
                    synchronize_daily_profit_lock()
                except (OSError, ValueError) as exc:
                    self._send(500, json.dumps({"error": f"Could not synchronize paper profit policy: {exc}"}).encode(), "application/json; charset=utf-8")
                    return
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
                    synchronize_daily_profit_lock()
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
        if request.path == "/api/copy/settings":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    self._send(400, b'{"error":"Invalid request size"}', "application/json; charset=utf-8")
                    return
                payload = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                self._send(400, b'{"error":"Invalid paper-copy settings"}', "application/json; charset=utf-8")
                return
            expected_fields = {
                "selected_wallets",
                "stop_loss_percent",
                "take_profit_percent",
                "leverage",
            }
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                self._send(400, b'{"error":"Paper-copy settings fields are invalid"}', "application/json; charset=utf-8")
                return
            try:
                settings = PaperCopySettings.from_payload(
                    payload,
                    processed_event_ids=COPY_SETTINGS.processed_event_ids,
                )
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            with PAPER_LOCK:
                if COPY_CONTROL["auto_trade_enabled"]:
                    self._send(409, b'{"error":"Pause auto-trade before changing copy settings"}', "application/json; charset=utf-8")
                    return
                monitored_wallets = {
                    str(trader.get("source_trader_id", "")).lower()
                    for trader in HYPERLIQUID_MONITOR.snapshot()["traders"][:15]
                }
                if any(wallet not in monitored_wallets for wallet in settings.selected_wallets):
                    self._send(409, b'{"error":"Selected wallets must be in the currently monitored top-15 list"}', "application/json; charset=utf-8")
                    return
                try:
                    save_settings(PAPER_COPY_SETTINGS_FILE, settings)
                except (OSError, ValueError) as exc:
                    self._send(500, json.dumps({"error": f"Could not save paper-copy settings: {exc}"}).encode(), "application/json; charset=utf-8")
                    return
                COPY_SETTINGS = settings
                response = {
                    "settings": COPY_SETTINGS.as_dict(),
                    "control": dict(COPY_CONTROL),
                    "copy_status": dict(COPY_STATUS),
                }
                self._send(200, json.dumps(response).encode(), "application/json; charset=utf-8")
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
            try:
                daily_profit = synchronize_daily_profit_lock()
            except (OSError, ValueError) as exc:
                self._send(500, json.dumps({"error": f"Could not synchronize paper profit policy: {exc}"}).encode(), "application/json; charset=utf-8")
                return
            if enabled and daily_profit["target_met"]:
                self._send(409, json.dumps({
                    "error": "The daily paper-profit target has been met. Automatic paper trading cannot be re-enabled today.",
                    "control": COPY_CONTROL,
                    "daily_profit": daily_profit,
                }).encode(), "application/json; charset=utf-8")
                return
            if enabled:
                if not COPY_SETTINGS.selected_wallets:
                    self._send(409, json.dumps({
                        "error": "Select at least one monitored wallet and save paper-copy settings before enabling auto-trade.",
                        "control": {**COPY_CONTROL, "auto_trade_enabled": False},
                    }).encode(), "application/json; charset=utf-8")
                    return
                monitor = HYPERLIQUID_MONITOR.snapshot()
                monitored_wallets = {
                    str(trader.get("source_trader_id", "")).lower()
                    for trader in monitor["traders"][:15]
                }
                if not set(COPY_SETTINGS.selected_wallets).issubset(monitored_wallets):
                    self._send(409, json.dumps({
                        "error": "A selected wallet is no longer in the monitored top-15 list. Update wallet selection before enabling.",
                        "control": {**COPY_CONTROL, "auto_trade_enabled": False},
                    }).encode(), "application/json; charset=utf-8")
                    return
                if not monitor["monitor"].get("connected"):
                    self._send(409, json.dumps({
                        "error": "Wait for the verified Hyperliquid fill stream to connect before enabling auto-trade.",
                        "control": {**COPY_CONTROL, "auto_trade_enabled": False},
                    }).encode(), "application/json; charset=utf-8")
                    return
            COPY_CONTROL["auto_trade_enabled"] = enabled
            COPY_CONTROL["close_request"] = False
            COPY_CONTROL["last_action"] = (
                "Paper copying enabled for new verified position opens from selected wallets."
                if enabled
                else "New paper copies paused; open positions remain available for manual close."
            )
            response = {"mode": "paper", "control": COPY_CONTROL}
            self._send(200, json.dumps(response).encode(), "application/json; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[Trading Bot] {format % args}")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), AppHandler)
    monitor_stop = Event()
    monitor_thread = Thread(
        target=_paper_copy_position_monitor,
        args=(monitor_stop,),
        name="paper-copy-position-monitor",
        daemon=True,
    )
    monitor_thread.start()
    print(f"A.Y.O. AI Trading & Agent Command Centre running at http://{host}:{port}")
    print("Paper mode plus explicitly confirmed Bybit testnet orders; live orders are disabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Trading Bot app.")
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    run()
