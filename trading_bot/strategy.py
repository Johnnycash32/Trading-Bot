from __future__ import annotations

from enum import Enum

from .market_data import Candle
from .models import Side


class Signal(str, Enum):
    HOLD = "hold"
    LONG = "long"
    SHORT = "short"


def moving_average(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


class MovingAverageCrossStrategy:
    """Small, explainable research strategy; not a production recommendation."""

    def __init__(self, fast_period: int = 5, slow_period: int = 12) -> None:
        if fast_period <= 0 or slow_period <= fast_period:
            raise ValueError("slow_period must be greater than fast_period and both must be positive")
        self.fast_period = fast_period
        self.slow_period = slow_period

    def signal(self, candles: list[Candle]) -> Signal:
        closes = [candle.close for candle in candles]
        if len(closes) < self.slow_period + 1:
            return Signal.HOLD
        current_fast = moving_average(closes, self.fast_period)
        current_slow = moving_average(closes, self.slow_period)
        previous_fast = moving_average(closes[:-1], self.fast_period)
        previous_slow = moving_average(closes[:-1], self.slow_period)
        if None in (current_fast, current_slow, previous_fast, previous_slow):
            return Signal.HOLD
        if previous_fast <= previous_slow and current_fast > current_slow:
            return Signal.LONG
        if previous_fast >= previous_slow and current_fast < current_slow:
            return Signal.SHORT
        return Signal.HOLD

    def pullback_signal(self, candles: list[Candle]) -> Signal:
        """Return a continuation signal when price recovers the fast average in-trend."""
        closes = [candle.close for candle in candles]
        if len(closes) < self.slow_period + 1:
            return Signal.HOLD
        current_fast = moving_average(closes, self.fast_period)
        current_slow = moving_average(closes, self.slow_period)
        previous_fast = moving_average(closes[:-1], self.fast_period)
        previous_slow = moving_average(closes[:-1], self.slow_period)
        if None in (current_fast, current_slow, previous_fast, previous_slow):
            return Signal.HOLD
        if current_fast > current_slow and previous_fast > previous_slow:
            if closes[-2] <= previous_fast and closes[-1] > current_fast and closes[-1] > closes[-2]:
                return Signal.LONG
        if current_fast < current_slow and previous_fast < previous_slow:
            if closes[-2] >= previous_fast and closes[-1] < current_fast and closes[-1] < closes[-2]:
                return Signal.SHORT
        return Signal.HOLD
