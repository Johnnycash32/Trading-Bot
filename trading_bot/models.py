from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade_percent: float = 0.5
    max_daily_loss_percent: float = 2.0
    max_open_positions: int = 3
    max_leverage: float = 2.0
    max_notional_percent: float = 25.0


@dataclass(frozen=True)
class TradeProposal:
    exchange: str
    symbol: str
    side: Side
    entry_price: float
    stop_loss: float
    take_profit: float
    account_equity: float
    current_daily_loss: float = 0.0
    open_positions: int = 0
    leverage: float = 1.0
    estimated_fee: float = 0.0
    estimated_slippage: float = 0.0


@dataclass(frozen=True)
class ValidatedTrade:
    proposal: TradeProposal
    quantity: float
    notional: float
    maximum_loss: float
    risk_reward_ratio: float


@dataclass(frozen=True)
class Rejection:
    code: str
    reason: str


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    exchange: str
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    status: str = "simulated"
