from __future__ import annotations

from dataclasses import dataclass

from .market_data import Candle
from .strategy import MovingAverageCrossStrategy, Signal


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    position_notional_percent: float = 10.0
    fee_rate: float = 0.0006
    slippage_rate: float = 0.0005


@dataclass(frozen=True)
class BacktestTrade:
    side: Signal
    entry_timestamp_ms: int
    exit_timestamp_ms: int
    entry_price: float
    exit_price: float
    notional: float
    net_pnl: float


@dataclass(frozen=True)
class BacktestReport:
    initial_equity: float
    final_equity: float
    net_pnl: float
    return_percent: float
    max_drawdown_percent: float
    total_trades: int
    winning_trades: int
    win_rate_percent: float
    trades: tuple[BacktestTrade, ...]


def _execution_price(price: float, side: Signal, slippage_rate: float, entering: bool) -> float:
    direction = 1 if side is Signal.LONG else -1
    adjustment = slippage_rate * direction
    if not entering:
        adjustment *= -1
    return price * (1 + adjustment)


def run_backtest(candles: list[Candle], strategy: MovingAverageCrossStrategy, config: BacktestConfig = BacktestConfig()) -> BacktestReport:
    if config.initial_equity <= 0 or config.position_notional_percent <= 0:
        raise ValueError("Backtest capital and position size must be positive")
    if not 0 <= config.fee_rate < 1 or not 0 <= config.slippage_rate < 1:
        raise ValueError("Fee and slippage rates must be between zero and one")

    equity = config.initial_equity
    peak_equity = equity
    max_drawdown = 0.0
    position: tuple[Signal, Candle, float] | None = None
    completed: list[BacktestTrade] = []
    notional = config.initial_equity * config.position_notional_percent / 100

    for index in range(len(candles)):
        history = candles[: index + 1]
        signal = strategy.signal(history)
        if signal is Signal.HOLD:
            continue
        current = candles[index]
        if position is None:
            position = (signal, current, equity)
            continue
        current_side, entry_candle, entry_equity = position
        if signal is current_side:
            continue

        entry_price = _execution_price(entry_candle.close, current_side, config.slippage_rate, True)
        exit_price = _execution_price(current.close, current_side, config.slippage_rate, False)
        quantity = notional / entry_price
        gross_pnl = quantity * (exit_price - entry_price) * (1 if current_side is Signal.LONG else -1)
        fees = notional * config.fee_rate + (quantity * exit_price * config.fee_rate)
        net_pnl = gross_pnl - fees
        equity += net_pnl
        completed.append(BacktestTrade(current_side, entry_candle.timestamp_ms, current.timestamp_ms, entry_price, exit_price, notional, net_pnl))
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100)
        position = (signal, current, equity)

    wins = sum(1 for trade in completed if trade.net_pnl > 0)
    return BacktestReport(config.initial_equity, equity, equity - config.initial_equity, (equity / config.initial_equity - 1) * 100, max_drawdown, len(completed), wins, wins / len(completed) * 100 if completed else 0.0, tuple(completed))
