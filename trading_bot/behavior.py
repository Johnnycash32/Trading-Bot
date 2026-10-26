from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .market_data import Candle
from .strategy import Signal


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


@dataclass(frozen=True)
class BehaviorAssessment:
    decision: str
    confidence: float
    approval_required: bool
    reasons: list[str]
    agents: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "approval_required": self.approval_required,
            "reasons": self.reasons,
            "agents": self.agents,
        }


class BehaviorEngine:
    """Turns a strategy result into an observable multi-agent decision."""

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
        if risk_reward_ratio >= 1.5:
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
    ) -> BehaviorAssessment:
        confidence = self._confidence(signal, candles, risk_reward_ratio)
        reasons: list[str] = []
        if signal is Signal.HOLD:
            reasons.append("No strategy signal; agents continue monitoring.")
        if not candles:
            reasons.append("No market evidence is available.")
        if not risk_approved:
            reasons.append(risk_reason or "Risk commander rejected the proposal.")
        if risk_reward_ratio and risk_reward_ratio < self.policy.min_risk_reward:
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

        agents = [
            {"name": "Raccoon", "role": "evidence scout", "state": "monitoring" if signal is Signal.HOLD else "scanned", "note": "Collected public candle evidence."},
            {"name": "Eagle", "role": "signal dispatcher", "state": "waiting" if signal is Signal.HOLD else "qualified", "note": f"Signal={signal.value}; confidence={confidence:.0%}."},
            {"name": "Hawk", "role": "chart examiner", "state": "reviewing", "note": f"Risk/reward={risk_reward_ratio:.2f}."},
            {"name": "Lion", "role": "risk commander", "state": "blocked" if blocked else "supervising", "note": reasons[0] if blocked else "Hard risk limits passed."},
            {"name": "Cheetah", "role": "paper executor", "state": "standby" if blocked else "paper-ready", "note": "No live order route is enabled."},
            {"name": "Hare", "role": "notification runner", "state": "alerting" if reasons else "ready", "note": reasons[-1] if reasons else "Decision delivered."},
        ]
        return BehaviorAssessment(decision, confidence, approval_required, reasons, agents)
