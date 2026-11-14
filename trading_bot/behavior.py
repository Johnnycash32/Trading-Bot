from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .day_trader_skill import DayTraderSignal, DayTraderSetup
from .market_data import Candle
from .strategy import Signal


# Tiered profit targets: (min_balance, max_balance, target_percent)
# Linear interpolation between brackets
PROFIT_TARGET_TIERS = [
    (0, 100, 5.0),
    (100, 200, 6.0),
    (200, 300, 7.0),
    (300, 1000, 10.0),
    (1000, 10000, 20.0),
    (10000, 100000, 30.0),
    (100000, float('inf'), 30.0),
]


def get_daily_profit_target(current_balance: float) -> float:
    """Calculate daily profit target percentage using linear interpolation.

    Args:
        current_balance: Current account balance in USD

    Returns:
        Target percentage (e.g., 5.5 for 5.5%)
    """
    if current_balance <= 0:
        return PROFIT_TARGET_TIERS[0][2]

    for i, (min_bal, max_bal, target_pct) in enumerate(PROFIT_TARGET_TIERS):
        if min_bal <= current_balance <= max_bal:
            if i == 0 or current_balance == min_bal:
                return target_pct
            # Linear interpolation with previous tier
            prev_min, prev_max, prev_pct = PROFIT_TARGET_TIERS[i - 1]
            # Interpolate between prev_max and min_bal
            ratio = (current_balance - prev_max) / (min_bal - prev_max) if min_bal != prev_max else 0
            return prev_pct + (target_pct - prev_pct) * ratio

    # Above all tiers - return max
    return PROFIT_TARGET_TIERS[-1][2]


def handle_withdrawal(amount: float, current_balance: float, start_of_day_balance: float) -> tuple[float, float]:
    """Handle withdrawal: subtract from balance, recalculate target, adjust start_of_day_balance.

    Args:
        amount: Amount to withdraw
        current_balance: Current account balance before withdrawal
        start_of_day_balance: Balance at start of day (for P&L calculation)

    Returns:
        Tuple of (new_balance, new_start_of_day_balance)
    """
    if amount <= 0:
        return current_balance, start_of_day_balance
    if amount > current_balance:
        raise ValueError("Withdrawal amount exceeds current balance")

    new_balance = current_balance - amount
    # Adjust start_of_day_balance proportionally
    if current_balance > 0:
        ratio = new_balance / current_balance
        new_start_of_day_balance = start_of_day_balance * ratio
    else:
        new_start_of_day_balance = 0.0

    return new_balance, new_start_of_day_balance


def validate_manual_trade_margin(notional: float, account_equity: float, min_margin_percent: float = 5.0) -> tuple[bool, str | None]:
    """Validate manual trade meets minimum margin requirement.

    Args:
        notional: Trade notional value
        account_equity: Total account equity
        min_margin_percent: Minimum margin as percentage of account equity

    Returns:
        Tuple of (is_valid, error_message)
    """
    if account_equity <= 0:
        return False, "Account equity must be positive"
    margin_pct = (notional / account_equity) * 100
    if margin_pct < min_margin_percent:
        return False, f"Manual trades require medium/large margin (min {min_margin_percent:.0f}%). Current: {margin_pct:.1f}%"
    return True, None


@dataclass(frozen=True)
class BehaviorPolicy:
    """Conservative, explainable gates for paper decisions.

    These are behavior controls, not a claim that a signal will be profitable.
    """

    min_confidence: float = 0.60
    min_risk_reward: float = 1.50
    max_daily_loss_percent: float = 2.0
    max_open_positions: int = 3
    large_trade_percent: float = 10.0
    manual_trade_min_margin_percent: float = 5.0


@dataclass(frozen=True)
class BehaviorAssessment:
    decision: str
    confidence: float
    approval_required: bool
    reasons: list[str]
    agents: list[dict[str, str]]
    day_trader_signal: DayTraderSignal | None = None
    day_trader_setup: DayTraderSetup | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "approval_required": self.approval_required,
            "reasons": self.reasons,
            "agents": self.agents,
        }
        if self.day_trader_signal:
            result["day_trader"] = {
                "setup": self.day_trader_signal.setup.value,
                "confidence": round(self.day_trader_signal.confidence, 3),
                "reason": self.day_trader_signal.reason,
                "entry": self.day_trader_signal.entry_price,
                "stop": self.day_trader_signal.stop_loss,
                "target": self.day_trader_signal.take_profit,
            }
        return result


class BehaviorEngine:
    """Turns a strategy result into an observable multi-agent decision."""

    RISK_REWARD_TOLERANCE = 1e-12

    def __init__(self, policy: BehaviorPolicy | None = None) -> None:
        self.policy = policy or BehaviorPolicy()

    @staticmethod
    def _confidence(signal: Signal, candles: list[Candle], risk_reward_ratio: float) -> float:
        if signal is Signal.HOLD or len(candles) < 2:
            return 0.0
        score = 0.55
        closes = [candle.close for candle in candles]
        directional = closes[-1] >= closes[-2] if signal is Signal.LONG else closes[-1] <= closes[-2]
        if directional:
            score += 0.10
        if len(candles) >= 8:
            score += 0.10
        if risk_reward_ratio + BehaviorEngine.RISK_REWARD_TOLERANCE >= 1.5:
            score += 0.10
        if abs(closes[-1] - closes[0]) / max(closes[0], 1e-9) >= 0.01:
            score += 0.05
        return min(score, 0.95)

    def assess(
        self,
        signal: Signal,
        candles: list[Candle],
        *,
        risk_reward_ratio: float = 0.0,
        notional: float = 0.0,
        account_equity: float = 0.0,
        current_daily_loss: float = 0.0,
        open_positions: int = 0,
        risk_approved: bool = True,
        risk_reason: str | None = None,
        auto_trading_enabled: bool = False,
        day_trader_signal: DayTraderSignal | None = None,
    ) -> BehaviorAssessment:
        confidence = self._confidence(signal, candles, risk_reward_ratio)
        reasons: list[str] = []
        if signal is Signal.HOLD:
            reasons.append("No strategy signal; agents continue monitoring.")
        if not candles:
            reasons.append("No market evidence is available.")
        if not risk_approved:
            reasons.append(risk_reason or "Risk commander rejected the proposal.")
        if risk_reward_ratio and risk_reward_ratio + self.RISK_REWARD_TOLERANCE < self.policy.min_risk_reward:
            reasons.append(f"Risk/reward {risk_reward_ratio:.2f} is below {self.policy.min_risk_reward:.2f}.")
        if confidence and confidence < self.policy.min_confidence:
            reasons.append(f"Confidence {confidence:.0%} is below {self.policy.min_confidence:.0%}.")
        daily_loss_limit = account_equity * self.policy.max_daily_loss_percent / 100
        if account_equity > 0 and current_daily_loss >= daily_loss_limit:
            reasons.append("Daily loss guard is active.")
        if open_positions >= self.policy.max_open_positions:
            reasons.append("Position-capacity guard is active.")

        blocked = bool(reasons and signal is not Signal.HOLD)
        large_trade = account_equity > 0 and notional >= account_equity * self.policy.large_trade_percent / 100
        approval_required = large_trade or not auto_trading_enabled
        if large_trade:
            reasons.append("Trade size requires explicit approval before any future live mode.")
        if not auto_trading_enabled:
            reasons.append("Auto-trading is off; this result is paper-only and approval-gated.")

        if signal is Signal.HOLD:
            decision = "monitor"
        elif blocked:
            decision = "reject"
        else:
            decision = "paper_simulate"

        day_trader_setup = day_trader_signal.setup if day_trader_signal else None

        agents = [
            {"name": "Raccoon", "role": "evidence scout", "state": "monitoring" if signal is Signal.HOLD else "scanned", "note": "Collected public candle evidence."},
            {"name": "Eagle", "role": "signal dispatcher", "state": "waiting" if signal is Signal.HOLD else "qualified", "note": f"Signal={signal.value}; confidence={confidence:.0%}."},
            {"name": "Hawk", "role": "chart examiner", "state": "reviewing", "note": f"Risk/reward={risk_reward_ratio:.2f}."},
            {"name": "Lion", "role": "risk commander", "state": "blocked" if blocked else "supervising", "note": reasons[0] if blocked else "Hard risk limits passed."},
            {"name": "Cheetah", "role": "paper executor", "state": "standby" if blocked else "paper-ready", "note": "No live order route is enabled."},
            {"name": "Hare", "role": "notification runner", "state": "alerting" if reasons else "ready", "note": reasons[-1] if reasons else "Decision delivered."},
        ]
        if day_trader_signal:
            agents.append({
                "name": "Owl",
                "role": "day-trader specialist",
                "state": "triggered",
                "note": f"{day_trader_signal.setup.value.replace('_', ' ').title()}: {day_trader_signal.reason}",
            })
        return BehaviorAssessment(decision, confidence, approval_required, reasons, agents, day_trader_signal, day_trader_setup)
