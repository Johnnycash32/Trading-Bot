from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import ClassVar

from .behavior import BehaviorEngine
from .market_data import Candle
from .models import Rejection, Side, TradeProposal
from .risk import RiskEngine
from .strategy import MovingAverageCrossStrategy, Signal
from .exchanges.paper import PaperExchange


@dataclass
class PaperExecutionSession:
    """Evaluate public candles and create simulated orders only."""

    STATE_VERSION: ClassVar[int] = 1
    FEE_RATE: ClassVar[float] = 0.0006
    SLIPPAGE_RATE: ClassVar[float] = 0.0005
    starting_equity: float = 10_000.0
    account_equity: float = 10_000.0
    risk_engine: RiskEngine = field(default_factory=RiskEngine)
    strategy: MovingAverageCrossStrategy = field(default_factory=MovingAverageCrossStrategy)
    exchange: PaperExchange = field(default_factory=PaperExchange)
    orders: list[dict[str, object]] = field(default_factory=list)
    history: list[dict[str, object]] = field(default_factory=list)
    positions: list[dict[str, object]] = field(default_factory=list)
    balance_history: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    _last_order_candle: dict[str, int] = field(default_factory=dict)
    _last_evaluated_candle: dict[str, int] = field(default_factory=dict)
    behavior_engine: BehaviorEngine = field(default_factory=BehaviorEngine)

    def __post_init__(self) -> None:
        if not self.balance_history:
            self.balance_history.append({
                "timestamp": self._timestamp(),
                "label": "starting_balance",
                "equity": self.account_equity,
                "change": 0.0,
            })

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _record_event(self, event_type: str, **details: object) -> None:
        self.events.append({"timestamp": self._timestamp(), "type": event_type, **details})

    def record_agent_event(self, agent: str, status: str, **details: object) -> None:
        self._record_event("agent_analysis", agent=agent, status=status, **details)

    def _record_balance(self, label: str, change: float) -> None:
        self.balance_history.append({
            "timestamp": self._timestamp(),
            "label": label,
            "equity": self.account_equity,
            "change": change,
        })

    def submit_manual_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        leverage: float,
        candle_timestamp_ms: int,
        order_type: str = "market",
    ) -> dict[str, object]:
        if side not in {Side.LONG.value, Side.SHORT.value}:
            raise ValueError("Choose Long or Short.")
        if order_type not in {"market", "limit"}:
            raise ValueError("Choose a supported paper order type.")
        if not all(isfinite(value) for value in (quantity, entry_price, stop_loss, take_profit, leverage)):
            raise ValueError("Order values must be finite numbers.")
        if quantity <= 0:
            raise ValueError("Order quantity must be greater than zero.")
        if any(position["symbol"] == symbol for position in self._open_positions()):
            raise ValueError("A paper position for this symbol is already open.")
        if any(order["symbol"] == symbol and order["status"] == "pending" for order in self.orders):
            raise ValueError("A paper limit order for this symbol is already pending.")
        if any(position.get("monitoring_status") == "data_gap" for position in self._open_positions()) or any(
            order.get("status") == "data_gap" for order in self.orders
        ):
            raise ValueError("New paper entries are halted until the unresolved market-data gap is reconciled.")

        trade_side = Side(side)
        proposal = TradeProposal(
            exchange=exchange,
            symbol=symbol,
            side=trade_side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            account_equity=self.account_equity,
            current_daily_loss=self._current_daily_loss(),
            open_positions=len(self._open_positions()),
            leverage=leverage,
            estimated_fee=entry_price * self.FEE_RATE * 2,
            estimated_slippage=entry_price * self.SLIPPAGE_RATE * 2,
        )
        decision = self.risk_engine.validate(proposal)
        if isinstance(decision, Rejection):
            self._record_event("paper_rejected", symbol=symbol, side=side, code=decision.code, reason=decision.reason)
            raise ValueError(decision.reason)
        if quantity > decision.quantity:
            raise ValueError(
                f"Quantity exceeds the risk-approved maximum of {decision.quantity:.8f} for the selected stop and leverage."
            )

        paper_order = self.exchange.submit_paper_order(proposal, quantity)
        created_at = self._timestamp()
        record: dict[str, object] = {
            "order_id": paper_order.order_id,
            "status": "pending" if order_type == "limit" else "simulated",
            "order_type": order_type,
            "created_at": created_at,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "initial_stop_loss": stop_loss,
            "initial_stop_distance": abs(entry_price - stop_loss),
            "leverage": leverage,
            "signal_source": "manual_paper_ticket",
            "risk_reward_ratio": decision.risk_reward_ratio,
            "maximum_loss": quantity * (abs(entry_price - stop_loss) + proposal.estimated_fee + proposal.estimated_slippage),
        }
        self.orders.append(record)
        self._last_evaluated_candle[symbol] = max(
            self._last_evaluated_candle.get(symbol, candle_timestamp_ms),
            candle_timestamp_ms,
        )
        self._record_event(
            "paper_order_created",
            symbol=symbol,
            side=side,
            order_id=record["order_id"],
            order_type=order_type,
            status=record["status"],
            quantity=quantity,
        )
        if order_type == "market":
            self._open_manual_position(record, entry_price, candle_timestamp_ms)
        return deepcopy(record)

    def cancel_pending_order(self, order_id: str) -> dict[str, object] | None:
        order = next((item for item in self.orders if item["order_id"] == order_id), None)
        if order is None or order["status"] not in {"pending", "data_gap"}:
            return None
        order["status"] = "cancelled"
        order["cancelled_at"] = self._timestamp()
        self._record_event("paper_order_cancelled", order_id=order_id, symbol=order["symbol"])
        return deepcopy(order)

    def _open_manual_position(self, order: dict[str, object], entry_price: float, candle_timestamp_ms: int) -> None:
        order["entry_price"] = entry_price
        position = {
            **order,
            "status": "open",
            "mark_price": entry_price,
            "unrealized_pnl": 0.0,
            "opened_at": order["created_at"],
            "opened_candle_timestamp_ms": candle_timestamp_ms,
            "trailing_stop_active": False,
        }
        self.positions.append(position)
        self._record_event(
            "position_opened",
            symbol=position["symbol"],
            side=position["side"],
            order_id=position["order_id"],
            entry_price=entry_price,
            quantity=position["quantity"],
        )

    def _fill_pending_orders(self, symbol: str, candle: Candle) -> None:
        for order in self.orders:
            if order["symbol"] != symbol or order["status"] != "pending":
                continue
            side = order["side"]
            touched = candle.low <= float(order["entry_price"]) if side == Side.LONG.value else candle.high >= float(order["entry_price"])
            if not touched:
                continue
            limit_price = float(order["entry_price"])
            fill_price = limit_price
            order["status"] = "simulated"
            order["filled_at"] = self._timestamp()
            order["filled_candle_timestamp_ms"] = candle.timestamp_ms
            order["fill_price"] = fill_price
            self._open_manual_position(order, fill_price, candle.timestamp_ms)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.STATE_VERSION,
            "starting_equity": self.starting_equity,
            "account_equity": self.account_equity,
            "orders": self.orders,
            "history": self.history,
            "positions": self.positions,
            "balance_history": self.balance_history,
            "events": self.events,
            "last_order_candle": self._last_order_candle,
            "last_evaluated_candle": self._last_evaluated_candle,
        }
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(payload, allow_nan=False, indent=2), encoding="utf-8")
        temporary_path.replace(path)

    @classmethod
    def load(cls, path: Path) -> PaperExecutionSession:
        if not path.exists():
            return cls()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read paper state at {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != cls.STATE_VERSION:
            raise ValueError(f"Unsupported or invalid paper state file: {path}")

        list_fields = ("orders", "history", "positions", "balance_history", "events")
        if any(not isinstance(payload.get(key), list) for key in list_fields):
            raise ValueError(f"Invalid paper state records in {path}")
        if any(not isinstance(record, dict) for key in list_fields for record in payload[key]):
            raise ValueError(f"Invalid paper state record in {path}")
        order_candles = payload.get("last_order_candle", {})
        evaluated_candles = payload.get("last_evaluated_candle", {})
        if not isinstance(order_candles, dict) or not isinstance(evaluated_candles, dict):
            raise ValueError(f"Invalid paper order candle state in {path}")
        try:
            session = cls(
                starting_equity=float(payload["starting_equity"]),
                account_equity=float(payload["account_equity"]),
                orders=payload["orders"],
                history=payload["history"],
                positions=payload["positions"],
                balance_history=payload["balance_history"],
                events=payload["events"],
                _last_order_candle={str(symbol): int(timestamp) for symbol, timestamp in order_candles.items()},
                _last_evaluated_candle={str(symbol): int(timestamp) for symbol, timestamp in evaluated_candles.items()},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid paper state values in {path}: {exc}") from exc
        order_numbers = [
            int(str(order["order_id"]).removeprefix("PAPER-"))
            for order in session.orders
            if str(order.get("order_id", "")).startswith("PAPER-")
            and str(order.get("order_id", ""))[6:].isdigit()
        ]
        session.exchange.set_next_order_number(max(order_numbers, default=0) + 1)
        return session

    def _mark_positions(self, symbol: str, candle: Candle) -> list[dict[str, object]]:
        closed: list[dict[str, object]] = []
        for position in self.positions:
            if position["symbol"] != symbol or position["status"] != "open":
                continue
            initial_stop = float(position.get("initial_stop_loss", position["stop_loss"]))
            position.setdefault("initial_stop_loss", initial_stop)
            position.setdefault("initial_stop_distance", abs(float(position["entry_price"]) - initial_stop))
            position.setdefault("trailing_stop_active", False)
            mark_price = candle.close
            if candle.timestamp_ms <= int(position.get("opened_candle_timestamp_ms", -1)):
                position["mark_price"] = mark_price
                position["unrealized_pnl"] = self._pnl(position, mark_price)
                continue
            side = position["side"]
            stop_price = float(position["stop_loss"])
            if position.get("trailing_stop_updated_candle_ms") == candle.timestamp_ms:
                stop_price = float(position["initial_stop_loss"])
            stop_hit = candle.low <= stop_price if side == Side.LONG.value else candle.high >= stop_price
            target_hit = candle.high >= position["take_profit"] if side == Side.LONG.value else candle.low <= position["take_profit"]
            if stop_hit:
                exit_price = min(candle.open, stop_price) if side == Side.LONG.value else max(candle.open, stop_price)
                exit_reason = "trailing_stop" if position.get("trailing_stop_active") and stop_price != float(position["initial_stop_loss"]) else "stop_loss"
            elif target_hit:
                exit_price = float(position["take_profit"])
                exit_reason = "take_profit"
            else:
                exit_price = 0.0
                exit_reason = ""
            if exit_reason:
                result = self.close_position(str(position["order_id"]), exit_price, exit_reason)
                if result is not None:
                    position["closed_candle_timestamp_ms"] = candle.timestamp_ms
                    result["closed_candle_timestamp_ms"] = candle.timestamp_ms
                    if self.history and self.history[-1]["order_id"] == position["order_id"]:
                        self.history[-1]["closed_candle_timestamp_ms"] = candle.timestamp_ms
                    closed.append(result)
                continue

            risk_distance = float(position["initial_stop_distance"])
            cost_buffer = float(position["entry_price"]) * (self.FEE_RATE + self.SLIPPAGE_RATE) * 2
            direction = 1 if side == Side.LONG.value else -1
            activation_distance = risk_distance * 0.75 + cost_buffer
            favorable_move = (mark_price - float(position["entry_price"])) * direction
            if not position.get("trailing_stop_active") and favorable_move >= activation_distance:
                lock_distance = max(cost_buffer + risk_distance * 0.1, 0.0)
                trailing_stop = float(position["entry_price"]) + direction * lock_distance
                position["stop_loss"] = trailing_stop
                position["trailing_stop_active"] = True
                position["trailing_stop_updated_candle_ms"] = candle.timestamp_ms
                for order in self.orders:
                    if order["order_id"] == position["order_id"]:
                        order["stop_loss"] = trailing_stop
                        break
                self._record_event(
                    "trailing_stop_activated",
                    symbol=symbol,
                    side=side,
                    order_id=position["order_id"],
                    stop_loss=trailing_stop,
                    mark_price=mark_price,
                )
            position["mark_price"] = mark_price
            position["unrealized_pnl"] = self._pnl(position, mark_price)
        return closed

    def close_position(
        self,
        order_id: str,
        exit_price: float,
        reason: str = "manual_close",
        price_source: str = "paper_mark",
        quote_timestamp_ms: int | None = None,
    ) -> dict[str, object] | None:
        """Close one open paper position and record its realized result."""
        if not isinstance(exit_price, (int, float)) or not isfinite(exit_price) or exit_price <= 0:
            raise ValueError("Exit price must be a finite positive number.")
        position = next(
            (
                item for item in self.positions
                if item.get("order_id") == order_id and item.get("status") == "open"
            ),
            None,
        )
        if position is None:
            return None

        position["status"] = "closed"
        position["mark_price"] = float(exit_price)
        position["exit_price"] = float(exit_price)
        position["exit_reason"] = reason
        position["exit_price_source"] = price_source
        if quote_timestamp_ms is not None:
            position["exit_quote_timestamp_ms"] = quote_timestamp_ms
        position["closed_at"] = self._timestamp()
        position["realized_pnl"] = self._pnl(position, float(exit_price))
        position["unrealized_pnl"] = 0.0
        self.account_equity += float(position["realized_pnl"])
        self._record_balance("position_closed", float(position["realized_pnl"]))
        self._record_event(
            "position_closed",
            symbol=position["symbol"],
            side=position["side"],
            order_id=order_id,
            exit_reason=reason,
            realized_pnl=float(position["realized_pnl"]),
            exit_price=float(exit_price),
            exit_price_source=price_source,
            exit_quote_timestamp_ms=quote_timestamp_ms,
        )
        for order in self.orders:
            if order["order_id"] == order_id:
                order.update({
                    "status": "closed",
                    "closed_at": position["closed_at"],
                    "exit_price": position["exit_price"],
                    "exit_reason": reason,
                    "realized_pnl": position["realized_pnl"],
                })
                break
        closed = dict(position)
        self.history.append(closed)
        return closed

    def close_all_positions(
        self,
        exit_quotes: dict[str, tuple[float, str, int]],
        reason: str = "manual_close",
    ) -> list[dict[str, object]]:
        """Close all open paper positions with fresh per-order executable quotes."""
        open_positions = self._open_positions()
        missing_quotes = [str(position["order_id"]) for position in open_positions if position["order_id"] not in exit_quotes]
        if missing_quotes:
            raise ValueError(f"Missing exit quotes for open paper orders: {', '.join(missing_quotes)}")
        for position in open_positions:
            price, source, timestamp_ms = exit_quotes[str(position["order_id"])]
            if not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
                raise ValueError(f"Invalid exit price for open paper order {position['order_id']}.")
            if not source or not isinstance(timestamp_ms, int) or timestamp_ms <= 0:
                raise ValueError(f"Invalid exit quote metadata for open paper order {position['order_id']}.")
        return [
            closed
            for position in open_positions
            if (closed := self.close_position(
                str(position["order_id"]),
                exit_quotes[str(position["order_id"])][0],
                reason,
                exit_quotes[str(position["order_id"])][1],
                exit_quotes[str(position["order_id"])][2],
            )) is not None
        ]

    @classmethod
    def _pnl(cls, position: dict[str, object], price: float) -> float:
        direction = 1 if position["side"] == Side.LONG.value else -1
        entry = float(position["entry_price"])
        quantity = float(position["quantity"])
        gross_pnl = quantity * (price - entry) * direction
        round_trip_costs = quantity * (entry + price) * (cls.FEE_RATE + cls.SLIPPAGE_RATE)
        return gross_pnl - round_trip_costs

    def _current_daily_loss(self) -> float:
        today = datetime.now(timezone.utc).date()
        daily_pnl = 0.0
        for trade in self.history:
            closed_at = trade.get("closed_at")
            if not isinstance(closed_at, str):
                continue
            try:
                closed_date = datetime.fromisoformat(closed_at).astimezone(timezone.utc).date()
            except ValueError:
                continue
            if closed_date == today:
                daily_pnl += float(trade.get("realized_pnl", 0.0))
        return max(0.0, -daily_pnl)

    def snapshot(self, symbol: str | None = None, mark_price: float | None = None) -> dict[str, object]:
        positions = [position for position in self.positions if position["status"] == "open" and (symbol is None or position["symbol"] == symbol)]
        history = self.history
        orders = self.orders
        balance_history = self.balance_history
        realized_pnl = [float(item.get("realized_pnl", 0.0)) for item in history]
        unrealized_pnl = sum(float(position.get("unrealized_pnl", 0.0)) for position in positions)
        used_margin = sum(float(position["quantity"]) * float(position["entry_price"]) for position in positions)
        analytics = {
            "total_trades": len(history),
            "wins": sum(1 for item in realized_pnl if item > 0),
            "losses": sum(1 for item in realized_pnl if item < 0),
            "realized_pnl": sum(realized_pnl),
            "average_pnl": sum(realized_pnl) / len(realized_pnl) if realized_pnl else 0.0,
            "best_trade": max(realized_pnl) if realized_pnl else 0.0,
            "worst_trade": min(realized_pnl) if realized_pnl else 0.0,
            "win_rate": (sum(1 for item in realized_pnl if item > 0) / len(realized_pnl)) if realized_pnl else 0.0,
        }
        return {
            "equity": self.account_equity,
            "account_summary": {
                "starting_balance": self.starting_equity,
                "equity": self.account_equity + unrealized_pnl,
                "realized_pnl": self.account_equity - self.starting_equity,
                "unrealized_pnl": unrealized_pnl,
                "available_balance": max(0.0, self.account_equity - used_margin),
            },
            "open_positions": deepcopy(positions),
            "history": deepcopy(history),
            "trade_history": deepcopy(history),
            "orders": deepcopy(orders),
            "balance_history": deepcopy(balance_history),
            "activity": deepcopy(self.events),
            "analytics": analytics,
        }

    def evaluate(self, exchange: str, symbol: str, candles: list[Candle], interval_ms: int | None = None) -> dict[str, object]:
        if not candles:
            return {"status": "hold", "reason": "No candles were supplied."}
        if interval_ms is not None and interval_ms <= 0:
            raise ValueError("Candle interval must be a positive number of milliseconds.")

        ordered_candles = sorted({candle.timestamp_ms: candle for candle in candles}.values(), key=lambda candle: candle.timestamp_ms)
        current_candle = ordered_candles[-1]
        open_for_symbol = [position for position in self._open_positions() if position["symbol"] == symbol]
        pending_for_symbol = [order for order in self.orders if order["symbol"] == symbol and order["status"] in {"pending", "data_gap"}]
        last_evaluated = self._last_evaluated_candle.get(symbol)
        if last_evaluated is None and open_for_symbol:
            opened_timestamps = [
                int(position["opened_candle_timestamp_ms"])
                for position in open_for_symbol
                if position.get("opened_candle_timestamp_ms") is not None
            ]
            if len(opened_timestamps) != len(open_for_symbol):
                return self._halt_for_unverifiable_gap(symbol, open_for_symbol, ordered_candles[0].timestamp_ms, None)
            last_evaluated = min(opened_timestamps)
        if last_evaluated is None:
            self._last_evaluated_candle[symbol] = current_candle.timestamp_ms
            new_candles = [current_candle]
        else:
            new_candles = [candle for candle in ordered_candles if candle.timestamp_ms > last_evaluated]

        expected_interval_ms = interval_ms or self._candle_interval_ms(ordered_candles)
        if open_for_symbol or pending_for_symbol:
            if any(position.get("monitoring_status") == "data_gap" for position in open_for_symbol) or any(
                order.get("status") == "data_gap" for order in pending_for_symbol
            ):
                if not open_for_symbol:
                    return self._halt_pending_orders_for_gap(symbol, pending_for_symbol, ordered_candles[0].timestamp_ms, last_evaluated, record_event=False)
                return self._halt_for_unverifiable_gap(
                    symbol,
                    open_for_symbol,
                    ordered_candles[0].timestamp_ms,
                    last_evaluated,
                    record_event=False,
                )
            previous_timestamp = last_evaluated
            for candle in new_candles:
                if previous_timestamp is not None and candle.timestamp_ms - previous_timestamp != expected_interval_ms:
                    if not open_for_symbol:
                        return self._halt_pending_orders_for_gap(symbol, pending_for_symbol, ordered_candles[0].timestamp_ms, previous_timestamp)
                    return self._halt_for_unverifiable_gap(
                        symbol,
                        open_for_symbol,
                        ordered_candles[0].timestamp_ms,
                        previous_timestamp,
                    )
                previous_timestamp = candle.timestamp_ms

        closed: list[dict[str, object]] = []
        if open_for_symbol:
            for candle in new_candles:
                self._fill_pending_orders(symbol, candle)
                closed.extend(self._mark_positions(symbol, candle))
                self._last_evaluated_candle[symbol] = candle.timestamp_ms
                if closed:
                    self._last_evaluated_candle[symbol] = current_candle.timestamp_ms
                    break
        elif any(order["symbol"] == symbol and order["status"] == "pending" for order in self.orders):
            for candle in new_candles:
                self._fill_pending_orders(symbol, candle)
                self._last_evaluated_candle[symbol] = candle.timestamp_ms
        if closed:
            behavior = self.behavior_engine.assess(
                Signal.HOLD,
                candles,
                account_equity=self.account_equity,
                current_daily_loss=self._current_daily_loss(),
                open_positions=len(self._open_positions()),
            )
            return {
                "status": "hold",
                "signal": Signal.HOLD.value,
                "reason": "Exit completed; waiting for a fresh candle before re-entry.",
                "behavioral": behavior.as_dict(),
                "closed_positions": closed,
                **self.snapshot(symbol, current_candle.close),
            }
        self._last_evaluated_candle[symbol] = current_candle.timestamp_ms
        open_for_symbol = [position for position in self._open_positions() if position["symbol"] == symbol]
        if open_for_symbol:
            return {
                "status": "hold",
                "signal": Signal.HOLD.value,
                "reason": "A paper position for this symbol is already open.",
                "closed_positions": [],
                **self.snapshot(symbol, current_candle.close),
            }
        pending_for_symbol = [order for order in self.orders if order["symbol"] == symbol and order["status"] == "pending"]
        if pending_for_symbol:
            return {
                "status": "hold",
                "signal": Signal.HOLD.value,
                "reason": "A paper limit order is waiting for its entry price.",
                "closed_positions": [],
                **self.snapshot(symbol, current_candle.close),
            }
        if any(position.get("monitoring_status") == "data_gap" for position in self._open_positions()) or any(
            order.get("status") == "data_gap" for order in self.orders
        ):
            reason = "New paper entries are halted while an open position has an unresolved market-data gap."
            return {
                "status": "halted",
                "signal": Signal.HOLD.value,
                "reason": reason,
                "closed_positions": [],
                "monitoring_gap": True,
                **self.snapshot(symbol, current_candle.close),
            }
        signal = self.strategy.signal(candles)
        signal_source = "moving_average_crossover"
        if signal is Signal.HOLD:
            signal = self.strategy.pullback_signal(candles)
            signal_source = "trend_pullback"
        if signal is Signal.HOLD:
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, current_daily_loss=self._current_daily_loss(), open_positions=len(self._open_positions()))
            reason = "No crossover or trend-pullback recovery signal."
            self._record_event("monitor", symbol=symbol, signal=signal.value, reason=reason)
            return {"status": "hold", "signal": signal.value, "reason": reason, "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, current_candle.close)}
        if self._last_order_candle.get(symbol) == current_candle.timestamp_ms:
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, current_daily_loss=self._current_daily_loss(), open_positions=len(self._open_positions()))
            self._record_event("duplicate_signal", symbol=symbol, signal=signal.value, reason="This candle already produced a paper entry.")
            return {"status": "hold", "signal": signal.value, "reason": "This candle already produced a paper entry.", "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, current_candle.close)}

        entry = current_candle.close
        recent = candles[-min(14, len(candles)):]
        average_range = sum(candle.high - candle.low for candle in recent) / len(recent)
        stop_distance = max(average_range, entry * 0.002)
        round_trip_cost_per_unit = entry * (self.FEE_RATE + self.SLIPPAGE_RATE) * 2
        reward_distance = (stop_distance + round_trip_cost_per_unit) * 1.5
        if signal is Signal.LONG:
            side = Side.LONG
            stop_loss, take_profit = entry - stop_distance, entry + reward_distance
        else:
            side = Side.SHORT
            stop_loss, take_profit = entry + stop_distance, entry - reward_distance

        proposal = TradeProposal(
            exchange=exchange,
            symbol=symbol,
            side=side,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            account_equity=self.account_equity,
            current_daily_loss=self._current_daily_loss(),
            open_positions=len(self._open_positions()),
            leverage=1.0,
            estimated_fee=entry * self.FEE_RATE * 2,
            estimated_slippage=entry * self.SLIPPAGE_RATE * 2,
        )
        self._last_order_candle[symbol] = current_candle.timestamp_ms
        decision = self.risk_engine.validate(proposal)
        if isinstance(decision, Rejection):
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, current_daily_loss=self._current_daily_loss(), open_positions=len(self._open_positions()), risk_approved=False, risk_reason=decision.reason)
            self._record_event("paper_rejected", symbol=symbol, signal=signal.value, code=decision.code, reason=decision.reason)
            return {"status": "rejected", "signal": signal.value, "code": decision.code, "reason": decision.reason, "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, entry)}

        behavior = self.behavior_engine.assess(signal, candles, risk_reward_ratio=decision.risk_reward_ratio, notional=decision.notional, account_equity=self.account_equity, current_daily_loss=self._current_daily_loss(), open_positions=len(self._open_positions()))

        order = self.exchange.submit_paper_order(proposal, decision.quantity)
        record = {
            "order_id": order.order_id,
            "status": order.status,
            "created_at": self._timestamp(),
            "exchange": order.exchange,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "entry_price": order.entry_price,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "initial_stop_loss": order.stop_loss,
            "initial_stop_distance": stop_distance,
            "signal_source": signal_source,
            "risk_reward_ratio": decision.risk_reward_ratio,
            "confidence": behavior.confidence,
            "behavior_decision": behavior.decision,
        }
        self.orders.append(record)
        self._record_event("paper_order_created", symbol=symbol, side=record["side"], order_id=record["order_id"], confidence=float(behavior.confidence))
        position = {
            **record,
            "status": "open",
            "mark_price": entry,
            "unrealized_pnl": 0.0,
            "opened_at": record["created_at"],
            "opened_candle_timestamp_ms": current_candle.timestamp_ms,
            "trailing_stop_active": False,
        }
        self.positions.append(position)
        self._record_event("position_opened", symbol=symbol, side=record["side"], order_id=record["order_id"], entry_price=entry, quantity=order.quantity)
        return {"status": "simulated", "signal": signal.value, "order": record, "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, entry)}

    @staticmethod
    def _candle_interval_ms(candles: list[Candle]) -> int:
        differences = [
            later.timestamp_ms - earlier.timestamp_ms
            for earlier, later in zip(candles, candles[1:])
            if later.timestamp_ms > earlier.timestamp_ms
        ]
        return min(differences) if differences else 60_000

    def _halt_for_unverifiable_gap(
        self,
        symbol: str,
        positions: list[dict[str, object]],
        earliest_available_ms: int,
        last_processed_ms: int | None,
        *,
        record_event: bool = True,
    ) -> dict[str, object]:
        reason = "Required market candles are missing. Position monitoring is halted; review and reconcile the open position before any further evaluation."
        for position in positions:
            position["monitoring_status"] = "data_gap"
            position["monitoring_warning"] = reason
        if record_event:
            self._record_event(
                "market_data_gap",
                symbol=symbol,
                order_ids=[position["order_id"] for position in positions],
                last_processed_candle_ms=last_processed_ms,
                earliest_available_candle_ms=earliest_available_ms,
                reason=reason,
            )
        return {
            "status": "halted",
            "signal": Signal.HOLD.value,
            "reason": reason,
            "closed_positions": [],
            "monitoring_gap": True,
            **self.snapshot(symbol),
        }

    def _halt_pending_orders_for_gap(
        self,
        symbol: str,
        orders: list[dict[str, object]],
        earliest_available_ms: int,
        last_processed_ms: int | None,
        *,
        record_event: bool = True,
    ) -> dict[str, object]:
        reason = "Required market candles are missing. Pending limit orders are halted; cancel or reconcile them before placing further paper orders."
        for order in orders:
            order["status"] = "data_gap"
            order["monitoring_warning"] = reason
        if record_event:
            self._record_event(
                "market_data_gap",
                symbol=symbol,
                order_ids=[order["order_id"] for order in orders],
                last_processed_candle_ms=last_processed_ms,
                earliest_available_candle_ms=earliest_available_ms,
                reason=reason,
            )
        return {
            "status": "halted",
            "signal": Signal.HOLD.value,
            "reason": reason,
            "closed_positions": [],
            "monitoring_gap": True,
            **self.snapshot(symbol),
        }

    def _open_positions(self) -> list[dict[str, object]]:
        return [position for position in self.positions if position["status"] == "open"]
