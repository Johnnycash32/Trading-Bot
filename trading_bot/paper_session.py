from __future__ import annotations

from dataclasses import dataclass, field

from .behavior import BehaviorEngine
from .market_data import Candle
from .models import Rejection, Side, TradeProposal
from .risk import RiskEngine
from .strategy import MovingAverageCrossStrategy, Signal
from .exchanges.paper import PaperExchange


@dataclass
class PaperExecutionSession:
    """Evaluate public candles and create simulated orders only."""

    account_equity: float = 10_000.0
    risk_engine: RiskEngine = field(default_factory=RiskEngine)
    strategy: MovingAverageCrossStrategy = field(default_factory=MovingAverageCrossStrategy)
    exchange: PaperExchange = field(default_factory=PaperExchange)
    orders: list[dict[str, object]] = field(default_factory=list)
    history: list[dict[str, object]] = field(default_factory=list)
    positions: list[dict[str, object]] = field(default_factory=list)
    _last_order_signal: dict[str, Signal] = field(default_factory=dict)
    behavior_engine: BehaviorEngine = field(default_factory=BehaviorEngine)

    def _mark_positions(self, symbol: str, mark_price: float) -> list[dict[str, object]]:
        closed: list[dict[str, object]] = []
        for position in self.positions:
            if position["symbol"] != symbol or position["status"] != "open":
                continue
            side = position["side"]
            stop_hit = mark_price <= position["stop_loss"] if side == Side.LONG.value else mark_price >= position["stop_loss"]
            target_hit = mark_price >= position["take_profit"] if side == Side.LONG.value else mark_price <= position["take_profit"]
            if not (stop_hit or target_hit):
                position["mark_price"] = mark_price
                position["unrealized_pnl"] = self._pnl(position, mark_price)
                continue
            position["status"] = "closed"
            position["exit_price"] = position["take_profit"] if target_hit else position["stop_loss"]
            position["realized_pnl"] = self._pnl(position, position["exit_price"])
            self.account_equity += position["realized_pnl"]
            self.history.append(dict(position))
            closed.append(dict(position))
        return closed

    @staticmethod
    def _pnl(position: dict[str, object], price: float) -> float:
        direction = 1 if position["side"] == Side.LONG.value else -1
        return float(position["quantity"]) * (price - float(position["entry_price"])) * direction

    def snapshot(self, symbol: str | None = None, mark_price: float | None = None) -> dict[str, object]:
        positions = [position for position in self.positions if position["status"] == "open" and (symbol is None or position["symbol"] == symbol)]
        return {"equity": self.account_equity, "open_positions": positions, "history": self.history[-20:]}

    def evaluate(self, exchange: str, symbol: str, candles: list[Candle]) -> dict[str, object]:
        if not candles:
            return {"status": "hold", "reason": "No candles were supplied."}

        closed = self._mark_positions(symbol, candles[-1].close)
        signal = self.strategy.signal(candles)
        if signal is Signal.HOLD:
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, open_positions=len(self._open_positions()))
            return {"status": "hold", "signal": signal.value, "reason": "No moving-average crossover.", "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, candles[-1].close)}
        if self._last_order_signal.get(symbol) is signal:
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, open_positions=len(self._open_positions()))
            return {"status": "hold", "signal": signal.value, "reason": "Duplicate signal already simulated.", "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, candles[-1].close)}

        entry = candles[-1].close
        recent = candles[-min(14, len(candles)):]
        average_range = sum(candle.high - candle.low for candle in recent) / len(recent)
        stop_distance = max(average_range * 1.5, entry * 0.002)
        if signal is Signal.LONG:
            side = Side.LONG
            stop_loss, take_profit = entry - stop_distance, entry + stop_distance * 2
        else:
            side = Side.SHORT
            stop_loss, take_profit = entry + stop_distance, entry - stop_distance * 2

        proposal = TradeProposal(
            exchange=exchange,
            symbol=symbol,
            side=side,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            account_equity=self.account_equity,
            open_positions=len(self._open_positions()),
            leverage=1.0,
            estimated_fee=entry * 0.0006,
            estimated_slippage=entry * 0.0005,
        )
        decision = self.risk_engine.validate(proposal)
        if isinstance(decision, Rejection):
            behavior = self.behavior_engine.assess(signal, candles, account_equity=self.account_equity, open_positions=len(self._open_positions()), risk_approved=False, risk_reason=decision.reason)
            return {"status": "rejected", "signal": signal.value, "code": decision.code, "reason": decision.reason, "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, entry)}

        behavior = self.behavior_engine.assess(signal, candles, risk_reward_ratio=decision.risk_reward_ratio, notional=decision.notional, account_equity=self.account_equity, open_positions=len(self._open_positions()))

        order = self.exchange.submit_paper_order(proposal, decision.quantity)
        self._last_order_signal[symbol] = signal
        record = {
            "order_id": order.order_id,
            "status": order.status,
            "exchange": order.exchange,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "entry_price": order.entry_price,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "risk_reward_ratio": decision.risk_reward_ratio,
            "confidence": behavior.confidence,
            "behavior_decision": behavior.decision,
        }
        self.orders.append(record)
        self.positions.append({**record, "status": "open", "mark_price": entry, "unrealized_pnl": 0.0})
        return {"status": "simulated", "signal": signal.value, "order": record, "behavioral": behavior.as_dict(), "closed_positions": closed, **self.snapshot(symbol, entry)}

    def _open_positions(self) -> list[dict[str, object]]:
        return [position for position in self.positions if position["status"] == "open"]
