from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeadTraderProfile:
    """A normalized, paper-only profile; not a wallet-scraping record."""

    profile_id: str
    display_name: str
    roi_percent: float
    pnl: float
    max_drawdown_percent: float
    aum: float
    copy_traders: int
    sharpe_ratio: float
    trading_days: int
    closed_trades: int
    win_rate_percent: float
    symbols: tuple[str, ...]
    out_of_sample_days: int
    fees_included: bool
    slippage_included: bool


@dataclass(frozen=True)
class CopySettings:
    mode: str = "fixed_amount"
    copy_amount: float = 100.0
    preferred_symbols: tuple[str, ...] = ()
    max_entry_slippage_percent: float = 1.0
    leverage: float = 1.0
    portfolio_stop_loss_percent: float = 10.0
    close_on_stop: bool = True


@dataclass(frozen=True)
class CopyAssessment:
    eligible: bool
    evidence_score: float
    estimated_order_amount: float
    approval_required: bool
    reasons: tuple[str, ...]
    controls: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "evidence_score": round(self.evidence_score, 1),
            "estimated_order_amount": round(self.estimated_order_amount, 2),
            "approval_required": self.approval_required,
            "reasons": list(self.reasons),
            "controls": list(self.controls),
        }


class CopyStrategyLab:
    """Research copy settings without reproducing private exchange endpoints."""

    def __init__(self, *, min_trading_days: int = 30, min_closed_trades: int = 30, max_drawdown_percent: float = 20.0, min_sharpe_ratio: float = 0.5) -> None:
        self.min_trading_days = min_trading_days
        self.min_closed_trades = min_closed_trades
        self.max_drawdown_percent = max_drawdown_percent
        self.min_sharpe_ratio = min_sharpe_ratio

    def assess(self, profile: LeadTraderProfile, settings: CopySettings) -> CopyAssessment:
        reasons: list[str] = []
        if settings.mode not in {"fixed_amount", "fixed_ratio"}:
            reasons.append("Copy mode must be fixed_amount or fixed_ratio.")
        if settings.copy_amount <= 0:
            reasons.append("Copy amount must be greater than zero.")
        if profile.trading_days < self.min_trading_days:
            reasons.append("Trading history is shorter than the minimum evidence window.")
        if profile.closed_trades < self.min_closed_trades:
            reasons.append("Closed-trade sample is too small for a research decision.")
        if profile.max_drawdown_percent > self.max_drawdown_percent:
            reasons.append("Maximum drawdown exceeds the configured research limit.")
        if profile.sharpe_ratio < self.min_sharpe_ratio:
            reasons.append("Risk-adjusted performance is below the configured research threshold.")
        if profile.out_of_sample_days <= 0:
            reasons.append("No out-of-sample evidence is available.")
        if not profile.fees_included or not profile.slippage_included:
            reasons.append("Fees and slippage must be included before a candidate is eligible.")
        if settings.preferred_symbols and not set(settings.preferred_symbols).intersection(profile.symbols):
            reasons.append("The preferred-symbol allowlist has no overlap with this profile.")
        if settings.max_entry_slippage_percent <= 0 or settings.max_entry_slippage_percent > 3:
            reasons.append("Entry slippage cap must be between 0 and 3 percent.")
        if settings.leverage <= 0 or settings.leverage > 10:
            reasons.append("Paper leverage must be between 1x and 10x.")
        if settings.portfolio_stop_loss_percent <= 0:
            reasons.append("Portfolio stop-loss must be greater than zero.")

        drawdown_score = max(0.0, 1.0 - profile.max_drawdown_percent / max(self.max_drawdown_percent, 1.0))
        sharpe_score = min(max(profile.sharpe_ratio / 2.0, 0.0), 1.0)
        history_score = min(profile.trading_days / 365.0, 1.0) * 0.5 + min(profile.closed_trades / 200.0, 1.0) * 0.5
        evidence_score = (drawdown_score * 0.35 + sharpe_score * 0.30 + history_score * 0.25 + (0.10 if profile.out_of_sample_days >= 30 else 0.0)) * 100
        eligible = not reasons
        controls = (
            f"{settings.mode} copy sizing",
            f"symbol allowlist: {', '.join(settings.preferred_symbols) if settings.preferred_symbols else 'profile symbols'}",
            f"maximum entry slippage: {settings.max_entry_slippage_percent:.2f}%",
            f"fixed leverage: {settings.leverage:g}x",
            f"portfolio stop-loss: {settings.portfolio_stop_loss_percent:.2f}%",
            "close positions when portfolio stop is reached" if settings.close_on_stop else "leave positions for self-management when stopped",
        )
        return CopyAssessment(eligible, evidence_score, settings.copy_amount, True, tuple(reasons), controls)

    @staticmethod
    def project_fixed_ratio(copy_amount: float, lead_initial_margin: float, lead_available_balance: float) -> float:
        if copy_amount <= 0 or lead_initial_margin < 0 or lead_available_balance <= 0:
            raise ValueError("Copy amount must be positive and lead balance must be available.")
        return copy_amount * lead_initial_margin / lead_available_balance


def demo_profiles() -> list[LeadTraderProfile]:
    """Clearly synthetic profiles for the local paper UI."""

    return [
        LeadTraderProfile("paper-trend-01", "Trend Research Alpha", 28.4, 2840.0, 12.8, 125000.0, 184, 1.12, 214, 96, 61.0, ("BTCUSDT", "ETHUSDT", "SOLUSDT"), 72, True, True),
        LeadTraderProfile("paper-momentum-02", "Momentum Lab Beta", 46.2, 4620.0, 29.4, 84000.0, 97, 0.38, 180, 41, 68.0, ("BTCUSDT", "XRPUSDT"), 0, True, False),
    ]
