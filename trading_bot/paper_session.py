from __future__ import annotations

from dataclasses import dataclass, field

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
    _last_order_signal: dict[str, Signal] = field(default_factory=dict)

    def evaluate(self, exchange: str, symbol: str, candles: list[Candle]) -> dict[str, object]:
        if not candles:
            return {"status": "hold", "reason": "No candles were supplied."}

        signal = self.strategy.signal(candles)
        if signal is Signal.HOLD:
            return {"status": "hold", "signal": signal.value, "reason": "No moving-average crossover."}
        if self._last_order_signal.get(symbol) is signal:
            return {"status": "hold", "signal": signal.value, "reason": "Duplicate signal already simulated."}

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
            open_positions=len(self.orders),
            leverage=1.0,
            estimated_fee=entry * 0.0006,
            estimated_slippage=entry * 0.0005,
        )
        decision = self.risk_engine.validate(proposal)
        if isinstance(decision, Rejection):
            return {"status": "rejected", "signal": signal.value, "code": decision.code, "reason": decision.reason}

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
        }
        self.orders.append(record)
        return {"status": "simulated", "signal": signal.value, "order": record}
