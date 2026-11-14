from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .market_data import Candle
from .models import Side
from .strategy import Signal, moving_average


class DayTraderSetup(str, Enum):
    HOLD = "hold"
    ORB = "opening_range_breakout"
    FLAG = "bull_flag"
    OVERSOLD_BOUNCE = "oversold_bounce"
    TREND_PULLBACK = "trend_pullback"


@dataclass(frozen=True)
class DayTraderSignal:
    setup: DayTraderSetup
    signal: Signal
    confidence: float
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    metadata: dict[str, Any] | None = None


def _group_by_day(candles: list[Candle]) -> dict[str, list[Candle]]:
    """Group candles by trading day (UTC date)."""
    days: dict[str, list[Candle]] = {}
    for candle in candles:
        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        days.setdefault(day_key, []).append(candle)
    return days


def _get_current_day_candles(candles: list[Candle], min_candles: int = 20) -> list[Candle]:
    """Get candles for the most recent trading day that has enough data."""
    days = _group_by_day(candles)
    if not days:
        return []
    # Sort days and find the most recent one with enough candles
    for day in sorted(days.keys(), reverse=True):
        if len(days[day]) >= min_candles:
            return days[day]
    # Fallback to latest day even if not enough candles
    latest_day = max(days.keys())
    return days[latest_day]


class OpeningRangeBreakout:
    """Opening Range Breakout (ORB) scanner for 5-min and 15-min charts."""

    def __init__(self, range_minutes: int = 15) -> None:
        self.range_minutes = range_minutes

    def scan(self, candles: list[Candle]) -> DayTraderSignal | None:
        # Get current day's candles (need range_minutes + 5 at minimum)
        day_candles = _get_current_day_candles(candles, self.range_minutes + 5)
        if len(day_candles) < self.range_minutes + 5:
            return None

        range_candles = day_candles[:self.range_minutes]
        range_high = max(c.high for c in range_candles)
        range_low = min(c.low for c in range_candles)
        range_mid = (range_high + range_low) / 2

        # Check breakout on current day's candles after the opening range
        for i in range(self.range_minutes, len(day_candles)):
            current = day_candles[i]
            prev = day_candles[i - 1]

            if prev.close <= range_high < current.close:
                risk = current.close - range_mid
                target = current.close + risk * 2.1
                return DayTraderSignal(
                    setup=DayTraderSetup.ORB,
                    signal=Signal.LONG,
                    confidence=0.7,
                    entry_price=current.close,
                    stop_loss=range_mid,
                    take_profit=target,
                    reason=f"ORB long: broke {range_high:.2f} (range {range_low:.2f}-{range_high:.2f})",
                    metadata={"range_high": range_high, "range_low": range_low, "range_mid": range_mid},
                )

            if prev.close >= range_low > current.close:
                risk = range_mid - current.close
                target = current.close - risk * 2.1
                return DayTraderSignal(
                    setup=DayTraderSetup.ORB,
                    signal=Signal.SHORT,
                    confidence=0.7,
                    entry_price=current.close,
                    stop_loss=range_mid,
                    take_profit=target,
                    reason=f"ORB short: broke {range_low:.2f} (range {range_low:.2f}-{range_high:.2f})",
                    metadata={"range_high": range_high, "range_low": range_low, "range_mid": range_mid},
                )

        return None


class BullFlag:
    """Bull flag / bear flag pattern detector."""

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback

    def scan(self, candles: list[Candle]) -> DayTraderSignal | None:
        day_candles = _get_current_day_candles(candles, self.lookback + 5)
        if len(day_candles) < self.lookback + 5:
            return None

        highs = [c.high for c in day_candles]
        lows = [c.low for c in day_candles]
        closes = [c.close for c in day_candles]

        pole_start = -self.lookback
        pole_high = max(highs[pole_start:-5])
        pole_low = min(lows[pole_start:-5])
        pole_size = pole_high - pole_low

        flag_highs = highs[-5:]
        flag_lows = lows[-5:]
        flag_high = max(flag_highs)
        flag_low = min(flag_lows)
        flag_size = flag_high - flag_low

        if flag_size > pole_size * 0.5:
            return None

        current = day_candles[-1]
        prev = day_candles[-2]

        if prev.close <= flag_high < current.close and closes[-1] > closes[-6]:
            risk = current.close - flag_low
            target = current.close + pole_size
            return DayTraderSignal(
                setup=DayTraderSetup.FLAG,
                signal=Signal.LONG,
                confidence=0.65,
                entry_price=current.close,
                stop_loss=flag_low,
                take_profit=target,
                reason=f"Bull flag breakout: pole {pole_size:.2f}, flag {flag_size:.2f}",
                metadata={"pole_size": pole_size, "flag_size": flag_size, "flag_high": flag_high, "flag_low": flag_low},
            )

        if prev.close >= flag_low > current.close and closes[-1] < closes[-6]:
            risk = flag_high - current.close
            target = current.close - pole_size
            return DayTraderSignal(
                setup=DayTraderSetup.FLAG,
                signal=Signal.SHORT,
                confidence=0.65,
                entry_price=current.close,
                stop_loss=flag_high,
                take_profit=target,
                reason=f"Bear flag breakdown: pole {pole_size:.2f}, flag {flag_size:.2f}",
                metadata={"pole_size": pole_size, "flag_size": flag_size, "flag_high": flag_high, "flag_low": flag_low},
            )

        return None


class OversoldBounce:
    """RSI oversold bounce detector (equities mean reversion)."""

    def __init__(self, rsi_period: int = 14, oversold: float = 30, overbought: float = 70) -> None:
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    def _rsi(self, closes: list[float]) -> float | None:
        if len(closes) < self.rsi_period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            if change >= 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-change)
        avg_gain = sum(gains[-self.rsi_period:]) / self.rsi_period
        avg_loss = sum(losses[-self.rsi_period:]) / self.rsi_period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _atr(self, candles: list[Candle]) -> float:
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, len(candles)):
            h = candles[i].high
            l = candles[i].low
            pc = candles[i - 1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0

    def scan(self, candles: list[Candle]) -> DayTraderSignal | None:
        day_candles = _get_current_day_candles(candles, self.rsi_period + 5)
        if len(day_candles) < self.rsi_period + 5:
            return None

        closes = [c.close for c in day_candles]
        rsi = self._rsi(closes)
        if rsi is None:
            return None

        current = day_candles[-1]
        prev = day_candles[-2]

        if rsi < self.oversold and prev.close < current.close:
            atr = self._atr(day_candles[-14:])
            stop = current.low - atr
            target = current.close + (current.close - stop) * 2
            return DayTraderSignal(
                setup=DayTraderSetup.OVERSOLD_BOUNCE,
                signal=Signal.LONG,
                confidence=0.6,
                entry_price=current.close,
                stop_loss=stop,
                take_profit=target,
                reason=f"Oversold bounce: RSI {rsi:.1f} < {self.oversold}",
                metadata={"rsi": rsi, "atr": atr},
            )

        if rsi > self.overbought and prev.close > current.close:
            atr = self._atr(day_candles[-14:])
            stop = current.high + atr
            target = current.close - (stop - current.close) * 2
            return DayTraderSignal(
                setup=DayTraderSetup.OVERSOLD_BOUNCE,
                signal=Signal.SHORT,
                confidence=0.6,
                entry_price=current.close,
                stop_loss=stop,
                take_profit=target,
                reason=f"Overbought rejection: RSI {rsi:.1f} > {self.overbought}",
                metadata={"rsi": rsi, "atr": atr},
            )

        return None


class DayTraderStrategy:
    """Composite strategy running all day-trader setups in priority order."""

    def __init__(self) -> None:
        self.scanners = [
            OpeningRangeBreakout(15),
            BullFlag(20),
            OversoldBounce(),
        ]

    def signal(self, candles: list[Candle]) -> DayTraderSignal | None:
        for scanner in self.scanners:
            result = scanner.scan(candles)
            if result and result.signal is not Signal.HOLD:
                return result
        return None

    def scan_all(self, candles: list[Candle]) -> list[DayTraderSignal]:
        results = []
        for scanner in self.scanners:
            result = scanner.scan(candles)
            if result:
                results.append(result)
        return results